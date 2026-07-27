#!/usr/bin/env python3
"""FI-89: the search's repetition rule must agree with the ARBITER's.

    python3 testing/test_rep_strict.py

`match.py` adjudicates with python-chess's `can_claim_threefold_repetition()`,
which needs THREE occurrences. `is_repetition` draws on the FIRST match at any
distance, so a node repeating a pre-root game position ONCE returns the
contempt draw before movegen and cuts the whole subtree. Two costs, both losing
half-points: the losing side banks a draw the opponent can simply decline, and
the winning side is blind past the repetition -- repeat-once-then-deviate is
the standard way to win clock time and to triangulate.

`REP_STRICT` adopts Stockfish's `Position::is_draw` split. This file is the
executable statement of what that means, and every case is cross-checked
against python-chess rather than against my own reading of the rule:

    match INSIDE the tree (k < ply)   -> draws on the first match, unchanged
    match at or before the ROOT       -> needs a SECOND match further back

The controls that make it a gate rather than a demo:
  * with the toggle OFF every case must answer exactly as today -- if the
    "before" column ever stops disagreeing with the arbiter, this test is
    measuring nothing and the bug it describes is gone by other means;
  * the in-tree case must answer 1 in BOTH modes -- a fix that also changed
    in-tree behaviour would be a different, much riskier change;
  * the probe must not leak its strict flag into a later call.
"""
import ctypes
import os
import sys

import chess

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

lib = ctypes.CDLL(os.path.join(_ROOT, "csearch.so"))
lib.csearch_abi.restype = ctypes.c_int
lib.cs_board_key.restype = ctypes.c_uint64
lib.cs_board_key.argtypes = [ctypes.c_uint64] * 8 + [ctypes.c_int,
                                                     ctypes.c_int,
                                                     ctypes.c_uint64]
lib.cs_rep_probe.restype = ctypes.c_int
lib.cs_rep_probe.argtypes = [ctypes.POINTER(ctypes.c_uint64), ctypes.c_int,
                             ctypes.POINTER(ctypes.c_uint64), ctypes.c_int,
                             ctypes.c_uint64, ctypes.c_int, ctypes.c_int]

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILED.append(name)


def key_of(b):
    return lib.cs_board_key(b.pawns, b.knights, b.bishops, b.rooks, b.queens,
                            b.kings, b.occupied_co[chess.WHITE],
                            b.occupied_co[chess.BLACK],
                            1 if b.turn == chess.WHITE else 0,
                            -1 if b.ep_square is None else b.ep_square,
                            b.castling_rights)


def push(b, uci):
    """python-chess's push() applies ANY move without checking legality, so a
    wrong-side move builds a garbage position in silence -- which is exactly
    how the first draft of case 2 broke. Assert instead."""
    mv = chess.Move.from_uci(uci)
    assert mv in b.legal_moves, f"{uci} is not legal in {b.fen()}"
    b.push(mv)


def arr(vals):
    return (ctypes.c_uint64 * max(1, len(vals)))(*vals)


def probe(path, ply, hist, key, hmc, strict):
    return lib.cs_rep_probe(arr(path), ply, arr(hist), len(hist),
                            ctypes.c_uint64(key), hmc, 1 if strict else 0)


def shuffle_game(n_returns):
    """Knights out and back `n_returns` times from the start position.

    Returns (board, keys-of-every-position-in-order). Each full cycle
    Nf3-Nf6-Ng1-Ng8 returns BOTH sides to the starting arrangement, so the
    start position recurs once per cycle and the halfmove clock never resets.
    """
    b = chess.Board()
    keys = [key_of(b)]
    for _ in range(n_returns):
        for mv in ("g1f3", "g8f6", "f3g1", "f6g8"):
            push(b, mv)
            keys.append(key_of(b))
    return b, keys


def main():
    print(f"csearch abi {lib.csearch_abi()}")
    if lib.csearch_abi() < 31:
        sys.exit("csearch.so predates FI-89 (abi 31) -- run ./setup.sh")

    # ---- the arbiter's own answer, so the target is not my reading of it ---
    b1, _ = shuffle_game(1)          # start position seen TWICE
    b2, _ = shuffle_game(2)          # start position seen THREE times
    check("arbiter: 2nd occurrence is NOT claimable",
          not b1.can_claim_threefold_repetition(),
          f"hmc={b1.halfmove_clock}")
    check("arbiter: 3rd occurrence IS claimable",
          b2.can_claim_threefold_repetition(),
          f"hmc={b2.halfmove_clock}")

    # ---- case 1: the match is PRE-ROOT (ply 0, history only) --------------
    # The root IS the repeated position; every earlier occurrence is game
    # history. This is the population the fix targets.
    _, keys1 = shuffle_game(1)
    root_key, hist1 = keys1[-1], list(reversed(keys1[:-1]))
    hmc1 = b1.halfmove_clock
    off = probe([root_key], 0, hist1, root_key, hmc1, strict=False)
    on = probe([root_key], 0, hist1, root_key, hmc1, strict=True)
    check("pre-root 2nd occurrence: OFF says draw (today's behaviour)", off == 1)
    check("pre-root 2nd occurrence: ON agrees with the arbiter (no draw)",
          on == 0)
    check("  ...and OFF is the one that DISAGREES with the arbiter",
          off != (1 if b1.can_claim_threefold_repetition() else 0))

    _, keys2 = shuffle_game(2)
    root_key2, hist2 = keys2[-1], list(reversed(keys2[:-1]))
    hmc2 = b2.halfmove_clock
    check("pre-root 3rd occurrence: ON says draw, as the arbiter does",
          probe([root_key2], 0, hist2, root_key2, hmc2, strict=True) == 1)
    check("pre-root 3rd occurrence: OFF says draw too (unchanged)",
          probe([root_key2], 0, hist2, root_key2, hmc2, strict=False) == 1)

    # ---- case 2: the match is INSIDE the tree (k < ply) -------------------
    # Both sides demonstrably chose these moves, so either can repeat again.
    # SF draws on the first match here and so must we, in BOTH modes.
    #
    # The repeated position must NOT be the root, or this silently becomes
    # case 3 (my first draft made exactly that mistake: a plain shuffle from
    # the start position puts the match at k == ply, which is the root arm).
    # So: one irreversible move first, THEN the cycle -- the node at ply 5
    # matches path[1], k = 4 < 5, strictly inside the tree, and path[1] occurs
    # exactly twice so a third-occurrence rule would have to answer 0.
    b = chess.Board()
    path = [key_of(b)]
    push(b, "a2a3")                         # irreversible: resets hmc
    path.append(key_of(b))
    # BLACK moves first from here -- the cycle order follows the side to move,
    # not the order it happens to take from the start position.
    for mv in ("g8f6", "g1f3", "f6g8", "f3g1"):
        push(b, mv)
        path.append(key_of(b))
    ply, node_key, hmc = len(path) - 1, path[-1], b.halfmove_clock
    assert path[ply] == path[1] and path[1] != path[0], "case 2 setup"
    assert hmc == 4, hmc
    for strict in (False, True):
        check(f"in-tree 2-fold still draws (strict={int(strict)})",
              probe(path, ply, [], node_key, hmc, strict) == 1,
              f"k=4 < ply={ply}")

    # ---- case 3: the boundary. k == ply is the ROOT, not "in tree" -------
    # Off by one here would silently keep today's behaviour at the root, which
    # is precisely the position the fix is about.
    _, keys = shuffle_game(1)
    path_b = list(keys)
    ply_b = len(path_b) - 1                 # match sits at k == ply (= root)
    check("boundary: a match AT the root needs the third (k == ply)",
          probe(path_b, ply_b, [], path_b[-1], ply_b, strict=True) == 0,
          "k == ply must take the strict arm, not the in-tree one")

    # ---- case 4: no repetition at all ------------------------------------
    b = chess.Board()
    for mv in ("e2e4", "e7e5", "g1f3", "b8c6"):
        push(b, mv)
    plain = [key_of(chess.Board())] + []
    for strict in (False, True):
        check(f"no repetition -> 0 (strict={int(strict)})",
              probe([key_of(b)], 0, plain, key_of(b), 4, strict) == 0)

    # ---- case 5: the probe must not leak its flag ------------------------
    _, keys1b = shuffle_game(1)
    rk, h = keys1b[-1], list(reversed(keys1b[:-1]))
    probe([rk], 0, h, rk, hmc1, strict=True)
    check("probe restores the strict flag (no leak into the next call)",
          probe([rk], 0, h, rk, hmc1, strict=False) == 1)

    print(f"\n{'FI-89 rep-strict: ALL CHECKS PASSED' if not FAILED else 'FAILED: ' + ', '.join(FAILED)}")
    return 1 if FAILED else 0


def engagement(pgn_paths):
    """How often does the strict arm actually change the answer, on REAL games?

    The entry's own instruction is "count the engagement rate before spending a
    slot" -- FI-48, FI-63 and P-33 were all closed pre-A/B on exactly this
    question, saving three slots. No search needed: replay each game, and at
    every position ask the probe both ways with that position's true game
    history. Disagreements are the population the change can act on.

    This measures the ROOT-level rate, which is the dominant one by
    construction -- the strict arm needs k >= ply, so it is concentrated at
    shallow plies in high-halfmove-clock positions and is structurally absent
    from deep middlegame nodes.
    """
    import glob
    import chess.pgn
    games = pos = eligible = differ = 0
    hmc_hist = []
    for pat in pgn_paths:
        for path in sorted(glob.glob(pat)) or [pat]:
            with open(path, encoding="utf-8", errors="replace") as fh:
                while True:
                    g = chess.pgn.read_game(fh)
                    if g is None:
                        break
                    games += 1
                    b = g.board()
                    keys = [key_of(b)]
                    for mv in g.mainline_moves():
                        b.push(mv)
                        pos += 1
                        k = key_of(b)
                        hmc = b.halfmove_clock
                        if hmc >= 4:
                            eligible += 1
                            hist = list(reversed(keys))
                            a = probe([k], 0, hist, k, hmc, strict=False)
                            c = probe([k], 0, hist, k, hmc, strict=True)
                            if a != c:
                                differ += 1
                                hmc_hist.append(hmc)
                        keys.append(k)
    print(f"\nFI-89 engagement over real games")
    print(f"  games / positions        : {games:,} / {pos:,}")
    print(f"  hmc >= 4 (can engage)    : {eligible:,}  "
          f"({eligible / max(1, pos) * 100:.1f}% of positions)")
    print(f"  answer CHANGES            : {differ:,}  "
          f"({differ / max(1, pos) * 100:.3f}% of positions, "
          f"{differ / max(1, eligible) * 100:.3f}% of eligible)")
    if hmc_hist:
        hmc_hist.sort()
        print(f"  halfmove clock at those   : min {hmc_hist[0]}, "
              f"median {hmc_hist[len(hmc_hist) // 2]}, max {hmc_hist[-1]}")
    else:
        print("  -- no position in this sample would change. A slot spent here "
              "would measure nothing.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--engagement":
        sys.exit(engagement(sys.argv[2:]))
    sys.exit(main())
