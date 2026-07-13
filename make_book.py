#!/usr/bin/env python3
"""Opening-book generator: many warm Stockfish workers -> Polyglot .bin.

Asymmetric branching is the whole trick: keep the top-N candidates on YOUR
side (that's your repertoire's variety) but only the best reply on the
opponent's side (you can't pick their move, one good answer per node is
enough). 4-wide *everywhere* is 4^10 ~= 1M positions = weeks; 4-wide on one
side is 4^5 ~= 1000 positions = hours. Same "top 4", 1000x the cost.

NUMA note: DON'T give one Stockfish 200 cores -- Lazy SMP scales badly past
~30-60 threads and cross-socket TT traffic can make time-to-depth WORSE. Run
many small instances (WORKERS x THREADS_PER), each on a different line. Book
building is embarrassingly parallel across positions.
"""

import argparse
import os
import signal
import struct
import time
import multiprocessing as mp

import chess
import chess.engine
import chess.polyglot

# ---- config (these are just the CLI DEFAULTS; every one has a flag) --------
# --side white|black|both : whose moves branch wide. "both" keeps our-topn on
#   EVERY move (a general book for either colour); white/black keep our-topn on
#   that side and their-topn on the opponent.
OUR_SIDE = "white"         # default for --side
OUR_TOPN = 5               # candidates kept on our move (branching)  --our-topn
THEIR_TOPN = 2             # replies kept on the opponent's move     --their-topn
MARGIN_CP = 40             # drop kept moves this far below best (cp)  --margin
MAX_PLIES = 12             # book depth in half-moves                 --plies
SEED_FIRST_MOVES = ["a3", "Nc3", "b3", "c3", "c4", "d3", "d4",
                    "e3", "e4", "f3", "f4", "g3", "Nf3", "h3", "h4"]

_SIDES = {"white": chess.WHITE, "black": chess.BLACK, "both": None}

# ---- per-worker Stockfish (created once per process, stays warm) -----------
_ENGINE = None
_CFG = {}

# ---- Ctrl+C save-early flag (main process only) ----------------------------
_ABORT = False


def _request_stop(signum, frame):
    """SIGINT handler for the MAIN process: just flip the flag. The build loop
    polls it and saves what it has -- no exception is thrown into the Pool."""
    global _ABORT
    if not _ABORT:
        print("\n[Ctrl+C] finishing in-flight results, then saving early "
              "(Ctrl+C again to abort hard)...", flush=True)
    _ABORT = True


def _worker_init(sf_path, threads, hash_mb, depth, side, our_topn,
                 their_topn, margin):
    # NOTE: the branching knobs MUST be passed in here, not read from module
    # globals -- macOS spawns workers by re-importing this module, so a global
    # mutated in __main__ never reaches them. _CFG is per-process, so it does.
    # Workers IGNORE SIGINT so Ctrl+C is handled only by the main process --
    # otherwise the signal races into the pool and terminate() deadlocks on the
    # outstanding imap tasks. terminate() then SIGTERMs the workers cleanly, and
    # each worker's Stockfish exits on its own when its stdin pipe closes.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    global _ENGINE, _CFG
    _ENGINE = chess.engine.SimpleEngine.popen_uci(sf_path)
    _ENGINE.configure({"Threads": threads, "Hash": hash_mb})
    _CFG = {"depth": depth, "side": side, "our_topn": our_topn,
            "their_topn": their_topn, "margin": margin}


def _analyse(fen):
    """Return (fen, [(uci, relative_cp), ...]) -- top moves after pruning."""
    board = chess.Board(fen)
    side = _CFG["side"]                   # None == "both" -> wide on every move
    if side is None or board.turn == side:
        topn = _CFG["our_topn"]
    else:
        topn = _CFG["their_topn"]
    infos = _ENGINE.analyse(
        board, chess.engine.Limit(depth=_CFG["depth"]), multipv=topn)
    kept = []
    best = None
    for info in infos:
        pv = info.get("pv")
        if not pv:
            continue
        cp = info["score"].relative.score(mate_score=100000)
        if best is None:
            best = cp
        elif cp < best - _CFG["margin"]:  # margin cut (MultiPV is eval-sorted)
            break
        kept.append((pv[0].uci(), cp))
    return fen, kept


# ---- Polyglot writer (python-chess reads .bin but can't write it) ----------
def _encode_move(board, move):
    to_sq = move.to_square
    if board.is_castling(move):          # polyglot encodes castling king->rook
        rank = chess.square_rank(move.from_square)
        rook_file = 7 if chess.square_file(move.to_square) > 4 else 0
        to_sq = chess.square(rook_file, rank)
    enc = to_sq | (move.from_square << 6)
    if move.promotion:                   # chess KNIGHT=2..QUEEN=5 -> poly 1..4
        enc |= (move.promotion - 1) << 12
    return enc


def _write_polyglot(path, entries):
    # entries: list of (zobrist_key, encoded_move, weight); must be key-sorted
    entries.sort(key=lambda e: (e[0], e[1]))
    with open(path, "wb") as f:
        for key, move, weight in entries:
            f.write(struct.pack(">QHHI", key, move, weight, 0))


# ---- reporting helpers -----------------------------------------------------
def _white_pov_cp(board, cp):
    """A python-chess relative score is from the side-to-move's POV; flip it to
    White's POV so evals read consistently (+ = good for White)."""
    return cp if board.turn == chess.WHITE else -cp


def _fmt_cp(cp):
    """cp (White POV) -> '+0.30' / '-1.20' / '#3' (mate). _analyse encodes mate
    with mate_score=100000, so |cp| near 100000 is a forced mate."""
    if abs(cp) >= 90000:
        n = (100000 - abs(cp) + 1) // 2          # full moves to mate
        return f"#{'' if cp > 0 else '-'}{n}"
    return f"{cp / 100:+.2f}"


def _numbered(sans):
    """['e4','e5','Nf3'] -> '1.e4 e5 2.Nf3' (line starts on White's move)."""
    out = []
    for i, s in enumerate(sans):
        out.append(f"{i // 2 + 1}.{s}" if i % 2 == 0 else s)
    return " ".join(out)


def _main_line(best_by_key, seed_boards):
    """Follow the top book move from the seed with the best White-POV eval.
    Returns (san_list, root_cp_white, leaf_cp_white) or None if nothing booked."""
    scored = []
    for b in seed_boards:
        k = chess.polyglot.zobrist_hash(b)
        if k in best_by_key:
            scored.append((_white_pov_cp(b, best_by_key[k][1]), b))
    if not scored:
        return None
    scored.sort(key=lambda t: t[0], reverse=True)
    root_cp, seed = scored[0]
    board = seed.copy()
    sans, tmp = [], chess.Board()               # SAN of the seed move(s) so far
    for mv in board.move_stack:
        sans.append(tmp.san(mv)); tmp.push(mv)
    leaf_cp = root_cp
    for _ in range(MAX_PLIES):                   # then walk best-eval book moves
        k = chess.polyglot.zobrist_hash(board)
        if k not in best_by_key:
            break
        uci, cp = best_by_key[k]
        leaf_cp = _white_pov_cp(board, cp)
        mv = chess.Move.from_uci(uci)
        sans.append(board.san(mv)); board.push(mv)
    return sans, root_cp, leaf_cp


# ---- driver: breadth-first, one parallel batch per ply ---------------------
def build(args):
    root = chess.Board()
    frontier, seen, seed_boards = [], set(), []
    for san in SEED_FIRST_MOVES:
        b = root.copy()
        try:
            b.push_san(san)
        except chess.IllegalMoveError:
            print(f"skipping illegal seed move: {san!r}")
            continue
        frontier.append(b.fen())
        seed_boards.append(b)          # keep the real board (move_stack intact;
                                       # chess.Board(fen) would drop the 1st move)

    side = _SIDES[args.side]         # None == "both"
    if args.side == "both":
        branch = f"both sides top-{args.our_topn}"
    else:
        branch = (f"{args.side} top-{args.our_topn} / their top-"
                  f"{args.their_topn}")
    print("=" * 64)
    print("Pygin opening-book build")
    print(f"  stockfish : {args.stockfish}")
    print(f"  depth     : {args.depth}      plies (deep): {MAX_PLIES}")
    print(f"  branching : {branch}, keep within {args.margin}cp of best")
    print(f"  compute   : {args.workers} workers x {args.threads} threads, "
          f"{args.hash} MB hash each")
    print(f"  seeds     : {len(frontier)} first moves -> {args.out}")
    print("=" * 64)

    entries = []
    best_by_key = {}                 # key -> (best_uci, best_cp) for the report
    total_pos = 0
    t0 = time.perf_counter()
    # Ctrl+C = save early. A SIGINT just sets _ABORT (below); we poll imap with
    # a 1s timeout so the main thread is never blocked uninterruptibly, then
    # break and fall through to the write with whatever's collected. Doing it
    # via a flag (not by letting KeyboardInterrupt tear through the Pool's
    # internals) is what avoids the terminate()-deadlock. Every entry is a
    # complete (key, move, weight), so a partial run is a valid, shallower book.
    global _ABORT
    _ABORT = False
    prev = signal.signal(signal.SIGINT, _request_stop)
    try:
        with mp.Pool(args.workers, _worker_init,
                     (args.stockfish, args.threads, args.hash, args.depth,
                      side, args.our_topn, args.their_topn, args.margin)) as pool:
            for ply in range(MAX_PLIES):
                frontier = [f for f in frontier
                            if chess.polyglot.zobrist_hash(chess.Board(f)) not in seen]
                if not frontier or _ABORT:
                    break
                t_ply = time.perf_counter()
                nxt, done = [], 0
                it = pool.imap_unordered(_analyse, frontier)
                while True:
                    try:
                        fen, kept = it.next(timeout=1.0)
                    except mp.TimeoutError:
                        if _ABORT:
                            break
                        continue
                    except StopIteration:
                        break
                    board = chess.Board(fen)
                    key = chess.polyglot.zobrist_hash(board)
                    if key in seen:
                        continue
                    seen.add(key)
                    done += 1
                    if kept:
                        best_by_key[key] = (kept[0][0], kept[0][1])
                    best = kept[0][1] if kept else 0
                    for uci, cp in kept:
                        move = chess.Move.from_uci(uci)
                        # ponytail: weight = linear falloff from best, best->1000
                        weight = max(1, min(65535, 1000 - (best - cp)))
                        entries.append((key, _encode_move(board, move), weight))
                        child = board.copy()
                        child.push(move)
                        nxt.append(child.fen())
                    if _ABORT:
                        break
                dt = time.perf_counter() - t_ply
                total_pos += done
                rate = done / dt if dt > 0 else 0.0
                print(f"ply {ply+1:>2}/{MAX_PLIES}: {done:>6} positions | "
                      f"{dt:7.1f}s | {rate:6.1f} pos/s | "
                      f"{1000*dt/done if done else 0:6.0f} ms/pos | "
                      f"{len(entries):>7} entries")
                if _ABORT:
                    print(f"[Ctrl+C] stopping early after ply {ply+1} -- "
                          f"saving {len(entries)} entries to {args.out}...")
                    pool.terminate()
                    break
                frontier = nxt
    finally:
        signal.signal(signal.SIGINT, prev)   # restore default handler

    _write_polyglot(args.out, entries)

    # --- summary / perf report ---
    total_t = time.perf_counter() - t0
    size = os.path.getsize(args.out) if os.path.exists(args.out) else 0
    avg_ms = (1000 * total_t / total_pos) if total_pos else 0.0
    rate = (total_pos / total_t) if total_t > 0 else 0.0
    print("=" * 64)
    print(f"DONE  {len(entries)} entries / {len(seen)} positions "
          f"-> {args.out}  ({size/1024:.1f} KB)")
    print(f"  wall time      : {total_t:.1f}s")
    print(f"  avg / position : {avg_ms:.0f} ms   ({rate:.1f} pos/s across "
          f"{args.workers} workers)")
    ml = _main_line(best_by_key, seed_boards)
    if ml:
        sans, root_cp, leaf_cp = ml
        print(f"  main line      : {_numbered(sans)}")
        print(f"  eval (White)   : {_fmt_cp(root_cp)} after opening move "
              f"-> {_fmt_cp(leaf_cp)} at book exit ({len(sans)} plies deep)")
    print("=" * 64)


def _selftest():
    # round-trip the encoder through python-chess's reader on a real move
    import tempfile, os
    b = chess.Board()
    mv = b.parse_san("e4")
    key = chess.polyglot.zobrist_hash(b)
    fd, path = tempfile.mkstemp(suffix=".bin")
    os.close(fd)
    _write_polyglot(path, [(key, _encode_move(b, mv), 1)])
    with chess.polyglot.open_reader(path) as r:
        assert r.find(b).move == mv, "encoder/reader mismatch"
    # castling too (king e1 -> g1 must encode as e1h1 internally)
    b2 = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    cm = b2.parse_san("O-O")
    _write_polyglot(path, [(chess.polyglot.zobrist_hash(b2), _encode_move(b2, cm), 1)])
    with chess.polyglot.open_reader(path) as r:
        assert r.find(b2).move == cm, "castling encode broken"
    os.remove(path)
    print("selftest ok")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stockfish", default="stockfish", help="path to stockfish binary")
    p.add_argument("--out", default="book.bin", help="output Polyglot .bin path")
    p.add_argument("--depth", type=int, default=22, help="search depth for each position")
    p.add_argument("--workers", type=int, default=8,
                   help="parallel SF instances (NOT cores)")
    p.add_argument("--threads", type=int, default=28,
                   help="cores PER instance; workers*threads <= total cores")
    p.add_argument("--hash", type=int, default=20480, help="MB per instance")
    p.add_argument("--plies", type=int, default=MAX_PLIES,
                   help=f"book depth in half-moves (default {MAX_PLIES})")
    p.add_argument("--side", choices=list(_SIDES), default=OUR_SIDE,
                   help="whose moves branch wide; 'both' = wide everywhere "
                        f"(default {OUR_SIDE})")
    p.add_argument("--our-topn", type=int, default=OUR_TOPN,
                   help=f"candidates kept on our move (default {OUR_TOPN})")
    p.add_argument("--their-topn", type=int, default=THEIR_TOPN,
                   help=f"replies kept on opponent's move (default {THEIR_TOPN})")
    p.add_argument("--margin", type=int, default=MARGIN_CP,
                   help=f"drop kept moves this far below best, cp (default {MARGIN_CP})")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()
    if a.selftest:
        _selftest()
    else:
        MAX_PLIES = a.plies
        build(a)
