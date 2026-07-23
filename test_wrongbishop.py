#!/usr/bin/env python3
"""FI-76 (WB-01) wrong-bishop rook-pawn clamp -- FEN gate.

Two halves, because the clamp is a bit-exact TWIN and a one-sided port is
the failure mode that silently desyncs the eval oracle:

  Part A  engine.py's Python mirror, off vs on, over every FEN (static eval,
          in-process -- this is where the corner arithmetic is pinned).
  Part B  csearch.c's clamp through a real depth-8 cengine search, one
          SUBPROCESS per config (csearch.so's toggles are process-wide, and
          cengine's fingerprint guard refuses two configs in one process).

The suite is half draws (the clamp must fire) and half near-misses (it must
NOT). The near-misses are the point: a gate that fires on a won position is
a real Elo leak, and a draws-only suite cannot see it.

KNOWN LIMITATION, measured 2026-07-23 and deliberately NOT asserted below:
the clamp does not propagate to the ROOT of a bishop draw. Every node along
the PV evaluates 0, yet a d8+ search still returns ~+1476, because the one
drawing reply is a quiet king move and the deep tree prunes it -- at the
node itself the engine plays it and scores 0, but ordering/pruning discards
it a few plies down. Stockfish scores the same FEN +1.06 at depth 24 (and
0.00 on the bishopless one), so this is a property of the distance-1 corner
rule, not of this port. The bishopless case DOES propagate, and is asserted.

Run: python3 test_wrongbishop.py
"""
import json
import subprocess
import sys

import chess

# --- dead draws: the clamp MUST fire -----------------------------------
# Squares are a1=0..h8=63; a8 and h1 are light, a1 and h8 are dark, so the
# "wrong" bishop is dark for an a8 corner and light for an h8 corner.
DRAWS = [
    # White: doubled a-pawns, DARK bishop (a8 is light), Black king home.
    ("k7/8/8/PK6/P7/8/8/2B5 w - - 0 1",
     "white a-pawns + dark bishop, black Ka8"),
    # Same with the defender on b7 -- Chebyshev 1 from a8, still home.
    ("8/1k6/8/PK6/P7/8/8/2B5 b - - 0 1",
     "defender on b7 (distance 1), black to move"),
    # h-file mirror: h8 is dark, so the wrong bishop here is LIGHT.
    ("7k/8/8/6KP/7P/8/8/5B2 w - - 0 1",
     "white h-pawns + light bishop, black Kh8"),
    # Colours reversed: Black is the strong side, promoting on a1 (dark),
    # so its wrong bishop is light.
    ("2b5/8/8/8/pk6/p7/8/K7 w - - 0 1",
     "black a-pawns + light bishop, white Ka1"),
    # No bishop at all: bare rook pawns vs a king on the corner. Gating
    # only the one-bishop case pays the search to shed the bishop into
    # exactly this position, so it has to be clamped too.
    ("k7/8/8/PK6/P7/8/8/8 w - - 0 1",
     "white a-pawns, NO bishop, black Ka8"),
]

# --- near misses: the clamp must NOT fire ------------------------------
# NOTE: "must not fire" is a claim about the GATE, not about the position.
# Some of these are still theoretical draws (the defender can walk home);
# the distance-1 test is deliberately stricter than the theory.
NEAR = [
    ("k7/8/8/PK6/P7/8/8/1B6 w - - 0 1",
     "RIGHT-coloured bishop (b1 is light, covers a8) -- genuinely winning",
     True),
    ("k7/8/8/2K5/1P6/8/8/2B5 w - - 0 1",
     "pawn on the b-file, not a rook file",
     True),
    ("4k3/8/8/PK6/P7/8/8/2B5 w - - 0 1",
     "defender on e8, four files from the corner",
     False),
    ("k7/7p/8/PK6/P7/8/8/2B5 w - - 0 1",
     "defender is not bare (black pawn h7)",
     False),
    ("k7/8/8/PK6/P7/8/8/2B3N1 w - - 0 1",
     "strong side also has a knight",
     False),
    ("k7/8/8/PK6/P7/8/8/2B4B w - - 0 1",
     "strong side has two bishops",
     False),
]

# The third field above is "the clamp can never fire ANYWHERE in this
# position's tree", which is what makes a search-level off==on comparison
# legitimate. Where a capture or a king walk could reach a clamped node,
# the search scores are allowed to differ -- only the static eval is pinned.


def _py_scores(on):
    """Static eval, White's perspective, with the Python mirror on/off."""
    import engine
    eng = engine.Engine()
    eng.use_wrongbishop = on
    return [eng._evaluate_static(chess.Board(f))
            for f, *_ in DRAWS + NEAR]


def _c_scores(on):
    """csearch.c static eval and depth-8 search score, White's perspective.

    The static half is the real oracle differential -- csearch_eval_white is
    eval_white() itself, so comparing it to engine.py's _evaluate_static is
    what proves the two ports agree rather than merely behaving similarly.
    """
    import ctypes
    import cengine
    cengine.Engine.WRONGBISHOP = on          # class attr: read at construction
    eng = cengine.Engine()
    eng.use_book = eng.use_tb = False        # FB-20: no book/TB short-circuit
    B = ctypes.c_uint64
    eng._lib.csearch_eval_white.argtypes = [B] * 8 + [ctypes.c_int] * 2 + [B]
    eng._lib.csearch_eval_white.restype = ctypes.c_int
    static, searched = [], []
    for fen, *_ in DRAWS + NEAR:
        board = chess.Board(fen)
        static.append(eng._lib.csearch_eval_white(*cengine.Engine._bargs(board)))
        eng.get_best_move(board, 8)
        searched.append(eng.last_score)
    return {"static": static, "search": searched}


def _c_scores_subprocess(on):
    r = subprocess.run([sys.executable, __file__, "--c", "on" if on else "off"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"C-side subprocess failed:\n{r.stdout}\n{r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def main():
    fens = DRAWS + NEAR
    nd = len(DRAWS)
    fails = []

    print("== Part A: engine.py Python mirror (static eval) ==")
    off, on = _py_scores(False), _py_scores(True)
    for i, (fen, why, *_) in enumerate(fens):
        drawn = i < nd
        ok = (on[i] == 0 and off[i] != 0) if drawn else (on[i] == off[i])
        print(f"  {'PASS' if ok else 'FAIL'}  off={off[i]:+6d} on={on[i]:+6d}"
              f"  {'draw ' if drawn else 'near '} {why}")
        if not ok:
            fails.append(f"A/{i} {why}: off={off[i]} on={on[i]}")

    print("== Part B: csearch.c clamp (subprocess per config) ==")
    coff, con = _c_scores_subprocess(False), _c_scores_subprocess(True)

    print("  -- static eval must equal engine.py's, bit for bit --")
    for i, (fen, why, *_) in enumerate(fens):
        ok = coff["static"][i] == off[i] and con["static"][i] == on[i]
        print(f"  {'PASS' if ok else 'FAIL'}  C off={coff['static'][i]:+6d} "
              f"on={con['static'][i]:+6d}  py off={off[i]:+6d} on={on[i]:+6d}"
              f"  {why}")
        if not ok:
            fails.append(f"B-static/{i} {why}: C={con['static'][i]} py={on[i]}")

    print("  -- depth-8 search (see KNOWN LIMITATION in the docstring) --")
    for i, (fen, why, *rest) in enumerate(fens):
        drawn = i < nd
        if drawn:
            if "NO bishop" not in why:       # bishop draws do not propagate
                print(f"  note  off={coff['search'][i]:+6d} "
                      f"on={con['search'][i]:+6d}  draw  {why}"
                      "  (root does not collapse -- expected)")
                continue
            ok = con["search"][i] == 0 and abs(coff["search"][i]) >= 200
        elif rest and rest[0]:               # clamp unreachable in the tree
            ok = con["search"][i] == coff["search"][i]
        else:
            continue                         # tree may legitimately differ
        print(f"  {'PASS' if ok else 'FAIL'}  off={coff['search'][i]:+6d} "
              f"on={con['search'][i]:+6d}  {'draw ' if drawn else 'near '} {why}")
        if not ok:
            fails.append(f"B-search/{i} {why}: off={coff['search'][i]} "
                         f"on={con['search'][i]}")

    if fails:
        print("\n== FAILURES ==")
        for f in fails:
            print("  " + f)
        return 1
    print("\n== FI-76 FEN gate: ALL PASSED ==")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--c":
        print(json.dumps(_c_scores(sys.argv[2] == "on")))
        sys.exit(0)
    sys.exit(main())
