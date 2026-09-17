#!/usr/bin/env python3
"""bench/imbalance_probe.py -- is our eval systematically optimistic about
material imbalance?

    python3 bench/imbalance_probe.py                    # default: the ladder PGNs
    python3 bench/imbalance_probe.py --depth 8 --n 150
    python3 bench/imbalance_probe.py --pgn 'some/*.pgn' --out data/imbalance.json

WHAT THIS ANSWERS. A single blunder position proves nothing -- 2026-09-17 the
engine played Rxe4 in
`1r1q1rk1/p3p2p/2p2bp1/3p4/N3bP2/7P/PPPQ1BP1/1K1RR3 w - - 0 1`,
a 2.7-pawn error, and the post-capture position read +7 at depth 6 where the
reference read -216. That is one position. This probe asks whether the pattern
holds across many.

THE CONTROL IS THE WHOLE DESIGN. Our eval carries a general offset against the
reference (the root position above reads -41 for us and 0.00 for it), so a raw
error on exchange-down positions means nothing on its own. Every run therefore
scores a BALANCED bucket from the same games at the same depth, and the result
is the imbalance bucket's error MINUS the balanced bucket's. That difference is
the only number here that says anything about imbalance specifically.

MATCHED DEPTH, NOT MATCHED STRENGTH. Both engines search the SAME fixed depth.
The question is not "who is stronger" but "does our eval point the search the
wrong way at the depth a fast game actually reaches", so a deeper reference
would measure something else.

BUCKETS are exact piece-count signatures, not a material sum: exchange-down is
one rook fewer AND one minor more, pawns and queens level. A raw -2 would also
catch two-pawns-down, which is a different question.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "lib")]
import chess
import chess.pgn

PIECES = (chess.PAWN, chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)


class Uci:
    """One long-lived UCI process. Reused across every position -- our engine
    pays 1-2s of startup and NNUE load, which would dominate the run."""

    def __init__(self, cmd, name, opts=()):
        self.name = name
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL,
                                  text=True, bufsize=1, cwd=ROOT)
        self._send("uci")
        self._until("uciok")
        for o in opts:
            self._send(o)
        self._ready()

    def _send(self, s):
        self.p.stdin.write(s + "\n")
        self.p.stdin.flush()

    def _until(self, token):
        for line in self.p.stdout:
            if line.startswith(token):
                return line
        raise RuntimeError(f"{self.name}: died waiting for {token}")

    def _ready(self):
        self._send("isready")
        self._until("readyok")

    def score(self, fen, depth):
        """Centipawns from the side to move's point of view, or None for a
        mate score (a mate is not an eval error, it is a different claim)."""
        self._send("position fen " + fen)
        self._send(f"go depth {depth}")
        cp = None
        for line in self.p.stdout:
            if line.startswith("info ") and " score " in line:
                t = line.split()
                si = t.index("score")
                cp = int(t[si + 2]) if t[si + 1] == "cp" else None
            if line.startswith("bestmove"):
                break
        return cp

    def close(self):
        try:
            self._send("quit")
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()


def signature(board, side):
    """(pawns, knights, bishops, rooks, queens) difference, side minus other."""
    return tuple(len(board.pieces(p, side)) - len(board.pieces(p, not side))
                 for p in PIECES)


def bucket_of(sig):
    pawns, knights, bishops, rooks, queens = sig
    minors = knights + bishops
    if sig == (0, 0, 0, 0, 0):
        return "balanced"
    if pawns == 0 and queens == 0 and rooks == -1 and minors == 1:
        return "exchange_down"
    if pawns == 0 and queens == 0 and rooks == 1 and minors == -1:
        return "exchange_up"
    return None


def harvest(paths, per_game, min_fullmove, settle, want=None):
    """Positions from real games, labelled by the side-to-move's imbalance.

    Settled only: the same signature must have held for `settle` plies, so we
    score positions where the imbalance is a fact of the position rather than
    the middle of a capture sequence."""
    out, t0 = [], time.time()
    for i, path in enumerate(paths, 1):
        bar(i, len(paths), t0, f"harvest ({len(out)} found)")
        if want and sum(1 for b, _ in out if b == "exchange_down") >= want \
                and sum(1 for b, _ in out if b == "exchange_up") >= want \
                and sum(1 for b, _ in out if b == "balanced") >= want:
            print("\n   enough in every bucket, stopping the harvest early")
            break
        with open(path, errors="replace") as fh:
            while True:
                game = chess.pgn.read_game(fh)
                if game is None:
                    break
                board = game.board()
                history, taken = [], 0
                for move in game.mainline_moves():
                    board.push(move)
                    stm = board.turn
                    history.append(signature(board, stm))
                    if (taken >= per_game or board.is_check()
                            or board.fullmove_number < min_fullmove
                            or len(history) <= settle):
                        continue
                    # every one of the last `settle` plies, seen from THIS
                    # side, must carry the same signature
                    sigs = {history[-1]}
                    for back in range(2, settle + 1, 2):
                        sigs.add(history[-back - 1] if back <= len(history) - 1
                                 else history[0])
                    if len(sigs) != 1:
                        continue
                    b = bucket_of(history[-1])
                    if b:
                        out.append((b, board.fen()))
                        taken += 1
    return out


def bar(done, total, t0, label):
    if total <= 0:
        return
    el = time.time() - t0
    rate = done / el if el > 0 else 0
    eta = (total - done) / rate if rate > 0 else 0
    msg = (f"  {label}  {done}/{total} ({100 * done / total:4.1f}%)  "
           f"{rate:.1f}/s  elapsed {el / 60:.1f}m  ETA {eta / 60:.1f}m")
    if sys.stdout.isatty():
        print("\r" + msg + "   ", end="", flush=True)
    elif done % 25 == 0 or done == total:
        print(msg, flush=True)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pgn", default="cengine_vs_anchor_*.pgn",
                    help="glob of PGNs to harvest positions from")
    ap.add_argument("--depth", type=int, default=8,
                    help="fixed depth for BOTH engines (default 8: the band "
                         "where the 2026-09-17 blunder lived)")
    ap.add_argument("--n", type=int, default=150, help="positions per bucket")
    ap.add_argument("--sf", default="/opt/homebrew/bin/stockfish",
                    help="reference engine binary")
    ap.add_argument("--ours", default="cuci.py")
    ap.add_argument("--per-game", type=int, default=2)
    ap.add_argument("--max-files", type=int, default=0,
                    help="cap how many PGNs are opened (0 = all)")
    ap.add_argument("--min-fullmove", type=int, default=10)
    ap.add_argument("--settle", type=int, default=4,
                    help="plies the signature must have held")
    ap.add_argument("--out", default="data/imbalance_probe.json")
    a = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(ROOT, a.pgn)))
    if not paths:
        ap.error(f"no PGNs matched {a.pgn!r}")
    if a.max_files:
        paths = paths[:a.max_files]
    print(f"-> harvesting from {len(paths)} PGN(s)")
    found = harvest(paths, a.per_game, a.min_fullmove, a.settle, want=a.n)
    if sys.stdout.isatty():
        print()

    by = {}
    for b, fen in found:
        by.setdefault(b, [])
        if len(by[b]) < a.n:
            by[b].append(fen)
    for b in ("balanced", "exchange_down", "exchange_up"):
        print(f"   {b:<14} {len(by.get(b, [])):4d} positions")
    if "balanced" not in by:
        print("-> no balanced control positions: the difference this probe "
              "reports would be meaningless. Stopping.")
        return 1

    total = sum(len(v) for v in by.values())
    print(f"-> scoring {total} positions x 2 engines at depth {a.depth}")
    ours = Uci([sys.executable, a.ours], "ours")
    ref = Uci([a.sf], "reference")
    rows, done, t0 = {}, 0, time.time()
    try:
        for b, fens in by.items():
            errs = []
            for fen in fens:
                o, r = ours.score(fen, a.depth), ref.score(fen, a.depth)
                if o is not None and r is not None:
                    errs.append({"fen": fen, "ours": o, "ref": r,
                                 "err": o - r})
                done += 1
                bar(done, total, t0, b)
            rows[b] = errs
        if sys.stdout.isatty():
            print()
    finally:
        ours.close()
        ref.close()

    def mean(xs):
        return sum(xs) / len(xs) if xs else float("nan")

    def sem(xs):
        """Standard error of the mean. Without it these numbers are just
        big-looking integers -- a 70cp difference over 400 noisy positions
        needs its margin attached to mean anything."""
        if len(xs) < 2:
            return float("nan")
        m = mean(xs)
        var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
        return (var / len(xs)) ** 0.5

    print("\n== signed eval error, ours minus reference, "
          "side-to-move POV ==")
    print("   positive = WE ARE MORE OPTIMISTIC than the reference\n")
    ctrl = [e["err"] for e in rows.get("balanced", [])]
    base, base_se = mean(ctrl), sem(ctrl)
    summary = {}
    for b in ("balanced", "exchange_down", "exchange_up"):
        es = [e["err"] for e in rows.get(b, [])]
        if not es:
            continue
        m, se = mean(es), sem(es)
        med = sorted(es)[len(es) // 2]
        d = m - base
        d_se = (se ** 2 + base_se ** 2) ** 0.5
        rel = "" if b == "balanced" else \
            f"   vs control {d:+7.1f} +/- {1.96 * d_se:.1f}"
        print(f"   {b:<14} n={len(es):<4} mean {m:+7.1f} +/- {1.96 * se:4.1f}"
              f"  median {med:+7.1f}{rel}")
        summary[b] = {"n": len(es), "mean": m, "margin95": 1.96 * se,
                      "median": med,
                      "vs_control": None if b == "balanced" else d,
                      "vs_control_margin95": None if b == "balanced"
                      else 1.96 * d_se}
    print("\n   The 'vs control' column is the result. A raw mean carries our "
          "eval's\n   general offset against the reference; only the "
          "difference from the\n   balanced bucket is specific to imbalance.")

    out = os.path.join(ROOT, a.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump({"conditions": {"depth": a.depth, "ours": a.ours,
                                  "reference": a.sf, "pgn": a.pgn,
                                  "settle": a.settle,
                                  "when": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
                   "summary": summary, "rows": rows}, fh, indent=1)
    print(f"\n-> wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
