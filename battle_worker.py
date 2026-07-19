"""
battle_worker.py
================
Subprocess worker for the Engine Battle GUI (``engine_battle.py``).

Each engine in a battle runs in its OWN operating-system process, spawned by the
GUI. Running engines out-of-process means

* a crashing engine cannot take the GUI down with it, and
* a hung or runaway search can be force-killed (``Process.terminate``) so the
  Depth-mode safety cap and the Time-mode watchdog can actually be enforced.

Communication is a tiny pickled-object protocol over a ``multiprocessing.Pipe``
-- this is deliberately *not* UCI; the GUI manages everything directly.

The worker loads a user-supplied engine ``.py`` file by path and expects the
``Engine`` class API used throughout this project::

    Engine().get_best_move(board, depth)                    -> chess.Move | None
    Engine().get_best_move_timed(board, seconds, max_depth)  -> chess.Move | None

with the attributes ``nodes_searched`` / ``last_score`` / ``last_depth`` (and the
constants ``MATE_SCORE`` / ``MATE_THRESHOLD``) populated after each search.

Protocol
--------
parent -> worker:
    ("move", fen, mode, value, max_depth)   mode in {"time", "depth"}
                                            value = milliseconds (time) or plies
                                            fen may also be (start_fen, [ucis])
                                            -- the game's full move history, so
                                            the engine's repetition detection
                                            works (a bare FEN leaves it blind
                                            to game-level threefolds)
    ("quit",)
worker -> parent:
    ("ready",)                  sent once, after the engine loads successfully
    ("fatal", traceback_str)    engine file failed to import / instantiate
    ("ok", result_dict)         a move was found
    ("error", traceback_str)    the engine raised while searching

result_dict keys: uci, depth, nodes, time_ms, nps, score_cp (side-to-move POV,
or None for a mate score), mate (signed full-moves, or None), info (a
synthesized UCI-style "info ..." string for the battle log).
"""

import importlib.util
import time
import traceback


def _load_engine(path):
    """Import an engine .py file by path and return a fresh Engine() instance."""
    spec = importlib.util.spec_from_file_location("battle_engine_mod", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load a Python module from {path!r}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "Engine"):
        raise AttributeError(f"{path!r} does not define an `Engine` class")
    return module.Engine()


def _format_info(depth, score_cp, mate, nodes, nps, time_ms):
    """Build a UCI-style 'info ...' line for the per-move log."""
    score = f"mate {mate}" if mate is not None else f"cp {score_cp}"
    return (f"info depth {depth} score {score} nodes {nodes} "
            f"nps {nps} time {time_ms}")


def engine_worker(conn, engine_path, use_book, pv_uci=False, book_path=None):
    """Process entry point: load the engine, then serve move requests forever."""
    import chess  # imported only in the child process
    import signal

    # Ctrl-C reaches the whole foreground process GROUP, so this child gets a
    # SIGINT of its own. Handling it here is pure noise: KeyboardInterrupt is a
    # BaseException, so the `except Exception` around the search below does NOT
    # catch it -- an interrupt landing mid-search escaped engine_worker and
    # multiprocessing printed a full traceback per engine process. Ignore it and
    # let the parent drive shutdown (quit message / terminate()), which is the
    # only shutdown path that also frees the SMP pool at the bottom of this file.
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):
        pass                     # not the main thread / platform without SIGINT

    try:
        engine = _load_engine(engine_path)
        try:
            engine.use_book = use_book
        except Exception:
            pass            # engine may not expose a book; that is fine
        if book_path:
            # Per-engine book override (match.py --book1/--book2): naming a
            # book implies wanting it, so it also turns use_book on. Must be
            # set BEFORE the first probe -- the reader resolves lazily and
            # honours engine.book_path first.
            try:
                engine.book_path = book_path
                engine.use_book = True
            except Exception:
                pass        # engine without a book attribute: ignore
        try:
            engine.pv_uci = pv_uci      # PV log format (SAN vs UCI)
        except Exception:
            pass            # older engines may not expose it; fine
        conn.send(("ready",))
    except Exception:
        conn.send(("fatal", traceback.format_exc()))
        return

    mate_score = int(getattr(engine, "MATE_SCORE", 1_000_000))
    mate_threshold = int(getattr(engine, "MATE_THRESHOLD", mate_score - 1_000))

    while True:
        try:
            msg = conn.recv()
        except (EOFError, KeyboardInterrupt, BrokenPipeError, OSError):
            break                        # parent closed the pipe -- exit quietly
        if not msg or msg[0] == "quit":
            break
        if msg[0] == "calibrate":
            # --nodes mode (opt-in): measure this build's bench NPS once so
            # match.py can scale per-side node budgets -- fixed nodes would
            # otherwise systematically flatter NPS-costly candidates (house
            # pricing ~1 Elo per 1%% NPS). Same 6-FEN suite as cuci's bench
            # (duplicated here: this worker must not import cuci for
            # arbitrary engine paths), book/tb/threads forced off like
            # run_bench, cold TT per position where the engine exposes one.
            _CAL_FENS = [
                "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
                "r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 3 3",
                "r4rk1/1pp1qppp/p1np1n2/2b1p1B1/2B1P1b1/P1NP1N2/1PP1QPPP/R4RK1 w - - 0 10",
                "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
                "8/2k5/3p4/p2P1p2/P2P1P2/8/8/4K3 w - - 0 1",
                "r2q1rk1/pP1p2pp/Q4n2/bbp1p3/Np6/1B3NBn/pPPP1PPP/R3K2R b KQ - 0 1",
            ]
            try:
                sv = (getattr(engine, "use_book", False),
                      getattr(engine, "use_tb", False),
                      getattr(engine, "smp_workers", 1))
                try:
                    engine.use_book = engine.use_tb = False
                    engine.smp_workers = 1
                except Exception:
                    pass
                tot_nodes, t0 = 0, time.perf_counter()
                for _f in _CAL_FENS:
                    try:
                        engine._lib.cs_tt_reset()
                    except Exception:
                        pass
                    engine.get_best_move(chess.Board(_f), 11)
                    tot_nodes += int(getattr(engine, "nodes_searched", 0) or 0)
                dt = max(1e-9, time.perf_counter() - t0)
                try:
                    (engine.use_book, engine.use_tb, engine.smp_workers) = sv
                except Exception:
                    pass
                conn.send(("ok", {"nps": tot_nodes / dt, "nodes": tot_nodes}))
            except Exception:
                try:
                    conn.send(("error", traceback.format_exc()))
                except (BrokenPipeError, OSError, EOFError):
                    break
            continue
        if msg[0] != "move":
            continue

        _, fen, mode, value, max_depth = msg
        try:
            if isinstance(fen, tuple):
                # (start_fen, [ucis]): rebuild the board WITH its move stack so
                # the engine's _path repetition tracking sees the whole game --
                # bare-FEN requests left engines threefold-blind in matches
                # (won positions could shuffle into arbiter draws).
                _start_fen, _ucis = fen
                board = chess.Board(_start_fen)
                for _u in _ucis:
                    board.push(chess.Move.from_uci(_u))
            else:
                board = chess.Board(fen)
            white_to_move = board.turn == chess.WHITE

            # perf_counter, not time.time(): this elapsed feeds match.py's
            # clock bookkeeping and the NPS line -- the engines themselves
            # time on the monotonic clock (engine.py P-09), so an NTP step
            # mid-search would desync the two.
            t0 = time.perf_counter()
            if mode == "time":
                move = engine.get_best_move_timed(board, value / 1000.0, max_depth)
            elif mode == "nodes":
                # --nodes mode: fixed node budget via FB-09. The assert
                # refuses pre-FB-09 builds, which silently IGNORE the attr
                # and would play unlimited (a wrong result, not a crash).
                assert hasattr(engine, "node_limit"), \
                    "engine lacks FB-09 node_limit -- cannot play --nodes"
                engine.node_limit = int(value)
                try:
                    move = engine.get_best_move(board, max_depth)
                finally:
                    engine.node_limit = None
            else:
                move = engine.get_best_move(board, int(value))
            elapsed = time.perf_counter() - t0

            nodes = int(getattr(engine, "nodes_searched", 0) or 0)
            depth = int(getattr(engine, "last_depth", 0) or 0)
            white_score = int(getattr(engine, "last_score", 0) or 0)
            time_ms = int(elapsed * 1000)
            nps = int(nodes / elapsed) if elapsed > 0 else 0

            # The engine reports the score from White's point of view; convert it
            # to the moving side's point of view for a UCI-correct info line.
            stm = white_score if white_to_move else -white_score
            mate = None
            score_cp = stm
            if abs(stm) >= mate_threshold:
                plies = mate_score - abs(stm)
                full_moves = (plies + 1) // 2
                mate = full_moves if stm > 0 else -full_moves
                score_cp = None

            pv = str(getattr(engine, "last_pv", "") or "")
            result = ("ok", {
                "uci": move.uci() if move is not None else None,
                "depth": depth,
                "nodes": nodes,
                "time_ms": time_ms,
                "nps": nps,
                "score_cp": score_cp,
                "mate": mate,
                "pv": pv,
                "info": _format_info(depth, score_cp, mate, nodes, nps, time_ms),
            })
        except Exception:
            result = ("error", traceback.format_exc())
        # Send OUTSIDE the compute try: a real engine error becomes an ("error",
        # ...) row, but a dead pipe (parent ended the match / SPRT early-stop /
        # Ctrl-C) must exit quietly -- NOT get caught above and re-sent, which
        # cascaded into the BrokenPipeError double-traceback spam.
        try:
            conn.send(result)
        except (BrokenPipeError, OSError, EOFError):
            break

    # #13: shut down the engine's lazy SMP pool (if it created one) so its
    # shared-memory segment is unlinked rather than leaked when this worker exits.
    pool = getattr(engine, "_smp_pool", None)
    if pool is not None:
        try:
            pool.close()
        except Exception:
            pass
