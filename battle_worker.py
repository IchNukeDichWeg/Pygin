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
        except (EOFError, KeyboardInterrupt):
            break
        if not msg or msg[0] == "quit":
            break
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

            t0 = time.time()
            if mode == "time":
                move = engine.get_best_move_timed(board, value / 1000.0, max_depth)
            else:
                move = engine.get_best_move(board, int(value))
            elapsed = time.time() - t0

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
            conn.send(("ok", {
                "uci": move.uci() if move is not None else None,
                "depth": depth,
                "nodes": nodes,
                "time_ms": time_ms,
                "nps": nps,
                "score_cp": score_cp,
                "mate": mate,
                "pv": pv,
                "info": _format_info(depth, score_cp, mate, nodes, nps, time_ms),
            }))
        except Exception:
            conn.send(("error", traceback.format_exc()))

    # #13: shut down the engine's lazy SMP pool (if it created one) so its
    # shared-memory segment is unlinked rather than leaked when this worker exits.
    pool = getattr(engine, "_smp_pool", None)
    if pool is not None:
        try:
            pool.close()
        except Exception:
            pass
