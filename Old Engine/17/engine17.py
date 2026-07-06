"""
engine.py
=========

A self-contained chess engine built on top of the ``chess`` library (used
only for board representation, move generation and legality checking -- *not*
for evaluation or search).

Search features
---------------
* **Negamax + alpha-beta** core with **Principal Variation Search (PVS)**.
* **Iterative deepening** reusing the previous iteration's PV move, killers,
  history scores and the transposition table. Partial-iteration results are
  preserved: if time runs out mid-depth, the best root move evaluated so far
  is used rather than always falling back to the last completed depth.
* **Aspiration windows** around the previous score for deeper iterations.
* **Transposition table** keyed by the board's internal position key
  (cheap and collision-safe -- see the performance note below) storing
  depth + bound (exact / lower / upper) + best move, with mate-score
  distance correction.
* **Quiescence search** with stand-pat, delta pruning and check evasions,
  plus a *lazy* stand-pat: the expensive positional eval terms are skipped
  when the cheap material+PST base already proves a >= beta cutoff (exact, so
  the search is unchanged).
* **Pruning / selectivity**: null-move pruning, reverse-futility (static
  null-move) pruning, futility pruning at frontier nodes, late-move
  reductions (LMR -- a log(depth)*log(move) reduction table) and late-move
  pruning (LMP -- dropping the quiet tail at shallow non-PV nodes once
  enough quiet moves have been searched without a cutoff).
* **Extensions**: check extension (post-push, own budget so it cannot be
  starved by other extensions), single-reply / forced-move extension, and
  passed-pawn push extension (5th rank or beyond). Each draws on a separate
  budget cap so one type cannot crowd out another.
* **Move ordering**: TT move, MVV-LVA captures, promotions, killer moves, the
  counter-move heuristic and the history heuristic (with a history malus that
  penalises quiet moves searched before the cutoff, keeping stale scores from
  dominating ordering), with **Static Exchange Evaluation (SEE)** demoting
  losing captures and pruning them in quiescence.
* **Opening book**: optional Polyglot ``.bin`` book consulted before search,
  with uniformly-random move selection among all book entries for opening variety.
* **Endgame tablebase**: optional online Syzygy probe (the free Lichess
  tablebase API) at the root for positions with few enough pieces. A hit
  returns the provably-optimal move and skips the search entirely; it is never
  queried inside the search. To avoid paying network latency where it buys
  nothing, the probe is skipped for positions the search already nails faster
  than the API responds -- dead-drawn insufficient material and overwhelming
  pawnless mop-up wins (a lone king vs a major piece, e.g. KQK / KRK) -- so the
  tablebase is spent only on genuinely tricky endings (pawns, or a defending
  piece, where the win/draw verdict or technique can actually be wrong). Any
  network error / timeout / illegal response falls back to a normal search, and
  the network wait is bounded by the move's time budget so a miss cannot lose on
  time. Disabled with ``use_tb = False`` for fully offline / benchmark play.
* **Endgame / draws**: an endgame "mop-up" term that drives the weak king to
  the edge to convert won endings (KQK / KRK / KQ-vs-P), and contempt-scored
  repetition detection so a clearly winning side avoids draws while a losing
  side is happy to hold them.

Evaluation
----------
A tapered hand-crafted evaluation (HCE): material + piece-square tables
blended middlegame<->endgame by game phase, plus pawn structure (doubled /
isolated / passed / backward), king safety (pawn shield, open files, attacker
count), mobility, rook on open / semi-open file, the bishop pair and a tempo
bonus. Returned in centipawns from White's perspective (positive favours
White); ``_evaluate_stm`` flips it to the side to move for negamax.

The cheap base half (material + PST + phase + tempo) is maintained
*incrementally* via an accumulator updated by a small per-move delta on every
make/unmake (``use_incremental_eval``) instead of being rescanned per node; the
accumulator is byte-for-byte identical to the from-scratch scan. The
pawn-structure term -- a pure function of the pawn bitboards and phase -- is
memoized in a pawn hash keyed on ``(white pawns, black pawns, phase)``. Several
further eval/search refinements (pin penalty, trade-down simplification,
recapture extension, alternative TT-replacement schemes, quiescence SEE
ordering) exist as off-by-default A/B toggles in ``__init__``; see the per-flag
verdicts there.

NNUE
----
A real NNUE evaluation is **not** integrated. Pure-Python NNUE inference would
require an incremental accumulator and ~hundreds of multiply-accumulates per
node; at the few-tens-of-thousands of nodes/second this interpreter manages
that would make the engine *slower*, not stronger, and it would need a C
extension (e.g. a Stockfish binding) to be worthwhile -- which defeats the
"from scratch in Python" goal. Instead the hand-crafted evaluation below was
substantially expanded (backward pawns, per-piece mobility, king-zone attacker
counts, rook files, bishop pair, tempo) as the practical substitute.

Strength
--------
Benchmarked against Stockfish configured for 2350 Elo at 0.5 s/move on CPython:
six independent 1 000-game runs (6 000 games total) produced a pooled score of
48.7 % → ~2341 Elo (-9 ± 9 at 95 % CI). All six runs fell within 16 Elo of
each other, confirming the result is stable and reproducible. Draw rate ≈ 33 %
(lower than typical for near-equal engines; bullet-territory blunders on both
sides explain the decisiveness). PyPy runs ~1.5× faster and will score somewhat
higher at the same wall-clock time control.

PERFORMANCE NOTE (regression fix)
---------------------------------
The earlier version had several issues that ballooned the node count and the
per-node cost. They are fixed here and flagged inline with ``# FIX``:

1. ``chess.polyglot.zobrist_hash(board)`` was called for the TT key at *every*
   node. It rebuilds the hash from scratch (~50k/s here). Switching the TT key
   to ``board._transposition_key()`` (~1.2M/s, ~22x faster) removed the single
   biggest per-node cost. Zobrist hashing is now only used for the book probe.
2. The root searched every move with a *full, un-narrowed* window (alpha was
   never raised), so root pruning was effectively disabled. The root now uses
   PVS and raises alpha, while still supporting the random tiebreak.
3. Move ordering called ``board.gives_check(move)`` for *every* legal move --
   one of python-chess's more expensive calls. It is removed from ordering;
   check detection now happens once, cheaply, after the move is pushed.
4. PVS, LMR, reverse-futility and futility pruning (claimed in the old
   docstring but not actually present) are implemented, cutting the tree hard.

OPTIMIZATION CHANGELOG (2026-06-22 / 2026-06-23 / 2026-06-24)
---------------------------------------------------------------
Implemented improvements #2-#9 from the optimization reference (#10 built as a
separate optional Cython build; #12 in progress). #1 was investigated and
deliberately NOT applied -- see the note below.

#1  python-chess C extension ("chess._speedup") -- SKIPPED (does not exist).
    The reference assumed python-chess ships an optional compiled C extension
    that, if missing, leaves you on a slow pure-Python fallback. This is a
    false premise: python-chess (Niklas Fiekas, here v1.11.2) is PURE PYTHON
    by design -- the installed package contains only .py files, no _speedup
    module and no .so. ``import chess._speedup`` raises ModuleNotFoundError
    because there is nothing to import, not because a build is missing.
    ``pip install chess --force-reinstall`` reinstalls the identical pure
    package. There is no C extension to enable, so nothing was changed.

#2  ``piece_at`` -> ``piece_type_at`` in the MVV-LVA value lookups of
    order_moves, _capture_moves, _see AND the quiescence delta/SEE prune (the
    last is the hottest, quiescence being ~50% of nodes; the reference listed
    only the first three but the same pattern and gain apply there).
    ``piece_at`` allocates a full Piece object just to read ``.piece_type``;
    ``piece_type_at`` returns the bare int. Behaviour-preserving: verified the
    ordered move lists and SEE values are identical to the old piece_at logic,
    and that ``PIECE_VALUES.get(None, 0) == 0`` reproduces ``... if p else 0``.
    (``_is_passed_pawn_push`` keeps ``piece_at`` -- it needs ``.color`` too.)

#3  Pawn-structure hash (``_pawn_cache``). ``_pawn_structure_bb`` is a pure
    function of its arguments, so its result is memoized. NOTE: the reference's
    suggested key ``(board.pawns, occupied_co[WHITE])`` would be a BUG here --
    the passed-pawn bonus is phase-tapered, so the score also depends on
    ``phase``. The key used is the exact argument tuple ``(wp, bp, phase)``,
    which is provably correct. The cache persists across moves (pure function)
    and is size-capped.

#4  Lichess Syzygy endgame tablebase probe (``_tb_probe``). At the root only
    (never inside the search), positions with <= ``tb_max_pieces`` pieces are
    looked up via the free Lichess tablebase API, returning a provably-optimal
    move and skipping the search. Lichess returns moves pre-sorted best-first
    for the side to move, so ``moves[0]`` is played (this already accounts for
    cursed wins / blessed losses, so no fragile WDL/DTZ tie-break logic is
    needed). Triviality guard (``_tb_trivial_win`` + insufficient-material): the
    ~150-400 ms round-trip is skipped for positions the search converts faster
    than the API answers -- dead-drawn insufficient material and pawnless lone-
    king-vs-major mop-up wins (KQK / KRK / KQRK ...) -- so the probe is spent
    only on complex endings (pawns, or a defending piece). KBN-vs-K stays
    probed: pawnless but a hard mate the center-distance mop-up can botch.
    Safety: any network error / timeout / illegal or empty response falls back
    to a normal search; the probe's network wait is bounded by half the move's
    time budget so a tablebase miss can never lose on time. Toggle with
    ``use_tb`` (set False for fully offline play / benchmarks). Verified live
    against the real API (2026-06-22): KQK/KRK/KRRK skip instantly; KPvK,
    KBNvK, KQvKP, KRPvKR all probe and return correct WDL + a legal move.

#5  IIR -- Internal Iterative Reduction. When _negamax reaches a node at
    depth >= 4 with no TT move, it has no ordering information and would waste
    full-depth budget on a poorly-sorted list. IIR reduces depth by 1 in that
    case. Applied after RFP/null-move/futility so those heuristics still use the
    original depth; only the recursive move-loop searches shallower. Standard in
    virtually every modern engine. Expected: +5-15 Elo at negligible code cost.
    Benchmark against the pre-IIR build at fixed nodes to verify move-quality
    (node counts will drop -- that is normal and desired). Toggle: remove the
    ``depth >= 4 and tt_move is None and not in_check`` guard to disable.

#6  Probcut -- REMOVED after testing (2026-06-23). Measured +4 ±12 Elo at
    1s/move (3000 games) and ~0 at 500ms -- two TCs, both null. The two-stage
    qsearch-probe implementation was correct and showed real node reductions
    (~30-47% on positional middlegames), but the savings did not convert to
    search strength at any tested TC. Code removed; the design and the bug
    history (static-eval-mid-exchange sign error) are documented in git.

#7  LMR divisor 2.25 -> 2.0. The LMR table formula
    ``int(0.75 + log(d)*log(m) / k)`` controls aggressiveness: smaller k =
    larger reductions = fewer nodes but higher risk of missing a good late
    move. Changing from 2.25 to 2.0 is a ~12 % increase in reductions.
    Overnight param sweep (5 variants × 1300 games @ 500ms, 2026-06-23):
    statistical tie across all variants (all within ±19 Elo). LMR 2.0 kept
    as the noise-peak; LMR 1.75 (+1 Elo over 2.0) not folded -- pure noise
    and adds tactical risk.

#8  C evaluation module for ``_mobility_king_safety_bb`` (2026-06-24).
    The hottest eval function (11.4 % of search time) ported to C and loaded
    via ``ctypes`` (stdlib, no install). Build: ``python3 eval_build.py``
    (clang -O2 -shared -fPIC). The C implementation uses precomputed attack
    tables (knight, king, pawn) with Dumb7Fill slider attacks and
    __builtin_ctzll / __builtin_popcountll for bit-scanning.
    ``_USE_C_EVAL`` module flag: falls back to the Python implementation if
    ``eval_c.so`` is absent. Tuning constants are synced from class attributes
    at ``__init__`` time via ``set_mobility_params()``. Correctness verified:
    0 / 10 000 positions differ from the Python path. NPS: v15 baseline =
    21 369 → #8 = 27 507 = +28.7 % (behaviour-preserving; identical node
    counts at fixed depth). Snapshot v15 = pre-#8 baseline.

#9  C legal move generator (2026-06-24).  ``list(board.legal_moves)`` replaced
    by a C generator (``movegen.so``, ctypes; build ``python3 movegen_build.py``)
    in BOTH order_moves and _capture_moves.  python-chess is kept for push/pop
    and game state -- only move LISTING is in C.  Iterative (Dumb7Fill) slider
    attacks + a make-free legality filter (king-attacked test on the post-move
    occupancy).  CRITICAL detail: the C output reproduces python-chess's exact
    generate_pseudo_legal_moves order (non-pawns by descending square, castling
    K-then-Q, pawn captures, single/double pushes, ep; promotions Q,R,B,N), so
    after order_moves' stable sort the equal-score tie-break is unchanged --
    the search is byte-identical, NOT merely set-equal.  (A prior *staged*
    movegen that reordered quiet ties lost ~20 Elo; see the __init__ note.)
    In-check nodes: generate_legal returns -1 and order_moves falls back to
    board.legal_moves (python-chess uses a different evasion order there, not
    worth replicating); _capture_moves is never called in check.
    Correctness gates: perft matches published counts to depth 6 on the full
    standard suite (startpos d6 = 119 060 324, Kiwipete d5 = 193 690 690, ...);
    ORDERED move list equals board.legal_moves on 76k non-check positions and
    the capture list equals _capture_moves' input on 76k more; search verified
    byte-identical (same move + node count, fixed RNG seed) with the generator
    on vs off.  NPS: +24.8 % at fixed depth (order_moves alone was +9.8 %;
    the quiescence capture generator added the rest).  Toggle ``use_c_movegen``
    (module flag ``_USE_C_MOVEGEN`` False if ``movegen.so`` is absent ->
    pure-python path).  Snapshot v16 = pre-#9 baseline.

#10 Cython search core (2026-06-24).  engine.py is compiled UNCHANGED into a
    separate ``engine_cy`` extension via ``python3 engine_cy_build.py`` (it
    regenerates engine_cy.pyx from this file each build -- no second source).
    engine.py itself is untouched and stays the pure-Python source + the only
    thing PyPy runs.  Verified byte-identical.  NPS (fair: 5 warmups,
    best-of-3, depth-7 suite): PyPy engine.py (warmed) = 77.8k > Cython
    engine_cy = 71.3k > CPython-pure engine.py = 62.4k.  So Cython is +14 %
    over CPython but -8 % vs a WARMED PyPy, because the hot path is dominated
    by python-chess board ops (push/pop/is_capture) that PyPy JITs and Cython
    cannot (external, C-API speed).  KEPT as an optional no-warmup CPython
    build (wins on cold/short searches and where PyPy isn't used); NOT folded
    into engine.py.  The structural fix is #12 (below): an own bitboard board
    layer turns those opaque python-chess calls into compilable int ops, which
    is what lets BOTH Cython and PyPy finally take off.

#12 Own bitboard board layer (IN PROGRESS, started 2026-06-24).  Replaces
    python-chess Board in the hot path with a pure-Python ``FastBoard`` (int
    bitboards, no object allocation) so move-gen / make-unmake / check tests
    are raw integer ops -- removing the python-chess ceiling that bounds
    #8/#9/#10.  Pure Python (not cdef) so it helps PyPy (JIT) AND Cython
    (compilable int ops) without breaking PyPy.  Phase 1 = FastBoard core +
    perft, gated against python-chess perft on the standard suite + many
    random positions (the movegen/make-unmake logic is ported from the
    already perft-verified movegen.c).  Snapshot v17 = pre-#12 baseline.
"""

import ctypes
import json
import math
import os
import random
import time
import urllib.parse
import urllib.request

import chess
import chess.polyglot

# --- #8: C evaluation module for mobility + king-safety ------------------- #
_EVAL_C_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'eval_c.so')
try:
    _eval_lib = ctypes.CDLL(_EVAL_C_PATH)
    _eval_lib.set_mobility_params.argtypes = [ctypes.c_int] * 11
    _eval_lib.set_mobility_params.restype = None
    _eval_lib.mobility_king_safety.argtypes = [
        ctypes.c_uint64, ctypes.c_uint64,   # occ_w, occ_b
        ctypes.c_uint64, ctypes.c_uint64,   # knights, bishops
        ctypes.c_uint64, ctypes.c_uint64,   # rooks, queens
        ctypes.c_uint64, ctypes.c_uint64,   # wp, bp
        ctypes.c_int, ctypes.c_int,         # wksq, bksq (-1 if absent)
        ctypes.c_int,                       # phase
    ]
    _eval_lib.mobility_king_safety.restype = ctypes.c_int
    _USE_C_EVAL = True
except (OSError, AttributeError):
    # OSError: .so missing / unloadable. AttributeError: .so loads but is a
    # stale build missing an expected symbol. Either way, fall back to Python.
    _USE_C_EVAL = False


# --- #9: C legal move generator -------------------------------------------- #
# Replaces list(board.legal_moves) in order_moves. The C side emits moves in
# python-chess's exact generate_pseudo_legal_moves order (perft-verified to
# depth 6; ordered-equal to board.legal_moves on 76k non-check positions), so
# the swap is byte-identical -- same nodes/scores/move. When the side to move
# is in check, generate_legal returns -1 and we fall back to board.legal_moves
# (python-chess uses a different evasion order there; not worth replicating).
_MOVEGEN_C_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'movegen.so')
try:
    _mg_lib = ctypes.CDLL(_MOVEGEN_C_PATH)
    _mg_lib.generate_legal.argtypes = [
        ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64,
        ctypes.c_uint64, ctypes.c_uint64,   # pawns,knights,bishops,rooks,queens,kings
        ctypes.c_uint64, ctypes.c_uint64,   # occ_w, occ_b
        ctypes.c_int, ctypes.c_int,         # turn, ep (-1 if none)
        ctypes.c_uint64,                    # castling rights
        ctypes.POINTER(ctypes.c_uint32),    # out buffer
    ]
    _mg_lib.generate_legal.restype = ctypes.c_int
    _mg_lib.generate_captures.argtypes = _mg_lib.generate_legal.argtypes
    _mg_lib.generate_captures.restype = ctypes.c_int
    _MG_BUF = (ctypes.c_uint32 * 256)()
    _USE_C_MOVEGEN = True
except (OSError, AttributeError):
    _USE_C_MOVEGEN = False


def _c_legal_moves(board, _gen=(_mg_lib.generate_legal if _USE_C_MOVEGEN else None),
                   _buf=(_MG_BUF if _USE_C_MOVEGEN else None), _Move=chess.Move):
    """Legal moves (in python-chess order) from the C generator, or None if the
    side to move is in check -- the caller then uses board.legal_moves. The
    output buffer is fully decoded into Move objects before returning, so the
    single shared buffer is safe across the recursive search."""
    n = _gen(board.pawns, board.knights, board.bishops, board.rooks,
             board.queens, board.kings,
             board.occupied_co[True], board.occupied_co[False],
             int(board.turn),
             board.ep_square if board.ep_square is not None else -1,
             board.clean_castling_rights(), _buf)
    if n < 0:
        return None
    return [_Move(_buf[i] & 63, (_buf[i] >> 6) & 63, (_buf[i] >> 12) & 7 or None)
            for i in range(n)]


def _c_capture_moves(board, _gen=(_mg_lib.generate_captures if _USE_C_MOVEGEN else None),
                     _buf=(_MG_BUF if _USE_C_MOVEGEN else None), _Move=chess.Move):
    """Captures + promotions (in _capture_moves' exact order) from C. Only
    called when the side to move is not in check (quiescence guarantees it)."""
    n = _gen(board.pawns, board.knights, board.bishops, board.rooks,
             board.queens, board.kings,
             board.occupied_co[True], board.occupied_co[False],
             int(board.turn),
             board.ep_square if board.ep_square is not None else -1,
             board.clean_castling_rights(), _buf)
    return [_Move(_buf[i] & 63, (_buf[i] >> 6) & 63, (_buf[i] >> 12) & 7 or None)
            for i in range(n)]


class _TimeUp(Exception):
    """Raised inside the search to abort once the time budget is spent."""


# Transposition-table bound flags.
TT_EXACT = 0
TT_LOWER = 1   # fail-high: the true score is >= the stored value
TT_UPPER = 2   # fail-low:  the true score is <= the stored value


class Engine:
    """Negamax/PVS engine with TT, quiescence, ID, pruning and an opening book."""

    # ------------------------------------------------------------------ #
    # Material values (centipawns) -- used by MVV-LVA / delta pruning.
    # ------------------------------------------------------------------ #
    PIECE_VALUES = {
        chess.PAWN: 100,
        chess.KNIGHT: 320,
        chess.BISHOP: 330,
        chess.ROOK: 500,
        chess.QUEEN: 900,
        chess.KING: 20000,
    }


    # ------------------------------------------------------------------ #
    # Piece-square tables (PeSTO) -- separate middlegame / endgame tables
    # blended by game phase (tapered eval). Written from a8..h1 for WHITE;
    # white pieces look up table[square_mirror(sq)], black look up table[sq].
    # ------------------------------------------------------------------ #
    MG_PAWN_TABLE = [
            0,   0,   0,   0,   0,   0,  0,   0,
            98, 134,  61,  95,  68, 126, 34, -11,
            -6,   7,  26,  31,  65,  56, 25, -20,
            -14,  13,   6,  21,  23,  12, 17, -23,
            -27,  -2,  -5,  12,  17,   6, 10, -25,
            -26,  -4,  -4, -10,   3,   3, 33, -12,
            -35,  -1, -20, -23, -15,  24, 38, -22,
            0,   0,   0,   0,   0,   0,  0,   0,
    ]
    EG_PAWN_TABLE = [
            0,   0,   0,   0,   0,   0,   0,   0,
            178, 173, 158, 134, 147, 132, 165, 187,
            94, 100,  85,  67,  56,  53,  82,  84,
            32,  24,  13,   5,  -2,   4,  17,  17,
            13,   9,  -3,  -7,  -7,  -8,   3,  -1,
            4,   7,  -6,   1,   0,  -5,  -1,  -8,
            13,   8,   8,  10,  13,   0,   2,  -7,
            0,   0,   0,   0,   0,   0,   0,   0,
    ]
    MG_KNIGHT_TABLE = [
            -167, -89, -34, -49,  61, -97, -15, -107,
            -73, -41,  72,  36,  23,  62,   7,  -17,
            -47,  60,  37,  65,  84, 129,  73,   44,
            -9,  17,  19,  53,  37,  69,  18,   22,
            -13,   4,  16,  13,  28,  19,  21,   -8,
            -23,  -9,  12,  10,  19,  17,  25,  -16,
            -29, -53, -12,  -3,  -1,  18, -14,  -19,
            -105, -21, -58, -33, -17, -28, -19,  -23,
    ]
    EG_KNIGHT_TABLE = [
            -58, -38, -13, -28, -31, -27, -63, -99,
            -25,  -8, -25,  -2,  -9, -25, -24, -52,
            -24, -20,  10,   9,  -1,  -9, -19, -41,
            -17,   3,  22,  22,  22,  11,   8, -18,
            -18,  -6,  16,  25,  16,  17,   4, -18,
            -23,  -3,  -1,  15,  10,  -3, -20, -22,
            -42, -20, -10,  -5,  -2, -20, -23, -44,
            -29, -51, -23, -15, -22, -18, -50, -64,
    ]
    MG_BISHOP_TABLE = [
            -29,   4, -82, -37, -25, -42,   7,  -8,
            -26,  16, -18, -13,  30,  59,  18, -47,
            -16,  37,  43,  40,  35,  50,  37,  -2,
            -4,   5,  19,  50,  37,  37,   7,  -2,
            -6,  13,  13,  26,  34,  12,  10,   4,
            0,  15,  15,  15,  14,  27,  18,  10,
            4,  15,  16,   0,   7,  21,  33,   1,
            -33,  -3, -14, -21, -13, -12, -39, -21,
    ]
    EG_BISHOP_TABLE = [
            -14, -21, -11,  -8, -7,  -9, -17, -24,
            -8,  -4,   7, -12, -3, -13,  -4, -14,
            2,  -8,   0,  -1, -2,   6,   0,   4,
            -3,   9,  12,   9, 14,  10,   3,   2,
            -6,   3,  13,  19,  7,  10,  -3,  -9,
            -12,  -3,   8,  10, 13,   3,  -7, -15,
            -14, -18,  -7,  -1,  4,  -9, -15, -27,
            -23,  -9, -23,  -5, -9, -16,  -5, -17,
    ]
    MG_ROOK_TABLE = [
            32,  42,  32,  51, 63,  9,  31,  43,
            27,  32,  58,  62, 80, 67,  26,  44,
            -5,  19,  26,  36, 17, 45,  61,  16,
            -24, -11,   7,  26, 24, 35,  -8, -20,
            -36, -26, -12,  -1,  9, -7,   6, -23,
            -45, -25, -16, -17,  3,  0,  -5, -33,
            -44, -16, -20,  -9, -1, 11,  -6, -71,
            -19, -13,   1,  17, 16,  7, -37, -26,
    ]
    EG_ROOK_TABLE = [
           13, 10, 18, 15, 12,  12,   8,   5,
            11, 13, 13, 11, -3,   3,   8,   3,
            7,  7,  7,  5,  4,  -3,  -5,  -3,
            4,  3, 13,  1,  2,   1,  -1,   2,
            3,  5,  8,  4, -5,  -6,  -8, -11,
            -4,  0, -5, -1, -7, -12,  -8, -16,
            -6, -6,  0,  2, -9,  -9, -11,  -3,
            -9,  2,  3, -1, -5, -13,   4, -20,
    ]
    MG_QUEEN_TABLE = [
            -28,   0,  29,  12,  59,  44,  43,  45,
            -24, -39,  -5,   1, -16,  57,  28,  54,
            -13, -17,   7,   8,  29,  56,  47,  57,
            -27, -27, -16, -16,  -1,  17,  -2,   1,
            -9, -26,  -9, -10,  -2,  -4,   3,  -3,
            -14,   2, -11,  -2,  -5,   2,  14,   5,
            -35,  -8,  11,   2,   8,  15,  -3,   1,
            -1, -18,  -9,  10, -15, -25, -31, -50,
    ]
    EG_QUEEN_TABLE = [
            -9,  22,  22,  27,  27,  19,  10,  20,
            -17,  20,  32,  41,  58,  25,  30,   0,
            -20,   6,   9,  49,  47,  35,  19,   9,
            3,  22,  24,  45,  57,  40,  57,  36,
            -18,  28,  19,  47,  31,  34,  39,  23,
            -16, -27,  15,   6,   9,  17,  10,   5,
            -22, -23, -30, -16, -16, -23, -36, -32,
            -33, -28, -22, -43,  -5, -32, -20, -41,
    ]
    MG_KING_TABLE = [
            -65,  23,  16, -15, -56, -34,   2,  13,
            29,  -1, -20,  -7,  -8,  -4, -38, -29,
            -9,  24,   2, -16, -20,   6,  22, -22,
            -17, -20, -12, -27, -30, -25, -14, -36,
            -49,  -1, -27, -39, -46, -44, -33, -51,
            -14, -14, -22, -46, -44, -30, -15, -27,
            1,   7,  -8, -64, -43, -16,   9,   8,
            -15,  36,  12, -54,   8, -28,  24,  14,
    ]
    EG_KING_TABLE = [
            -74, -35, -18, -18, -11,  15,   4, -17,
            -12,  17,  14,  17,  17,  38,  23,  11,
            10,  17,  23,  15,  20,  45,  44,  13,
            -8,  22,  24,  27,  26,  33,  26,   3,
            -18,  -4,  21,  24,  27,  23,   9, -11,
            -19,  -3,  11,  21,  23,  16,   7,  -9,
            -27, -11,   4,  13,  14,   4,  -5, -17,
            -53, -34, -21, -11, -28, -14, -24, -43
    ]

    # Material values -- tuned on lichess-big3-resolved.book (7.15M WDL positions).
    # EG values are notably higher than MG for minor pieces, reflecting their
    # increased relative value as the board empties.
    MG_VALUES = {
        chess.PAWN: 89, chess.KNIGHT: 353, chess.BISHOP: 356,
        chess.ROOK: 489, chess.QUEEN: 1148, chess.KING: 0,
    }
    EG_VALUES = {
        chess.PAWN: 108, chess.KNIGHT: 335, chess.BISHOP: 328,
        chess.ROOK: 570, chess.QUEEN: 1020, chess.KING: 0,
    }
    # Game-phase contribution of each piece type. The maximum (full opening
    # material) is 24, used to blend the middlegame and endgame scores.
    PHASE_WEIGHTS = {
        chess.PAWN: 0, chess.KNIGHT: 1, chess.BISHOP: 1,
        chess.ROOK: 2, chess.QUEEN: 4, chess.KING: 0,
    }
    PHASE_MAX = 24

    # ------------------------------------------------------------------ #
    # Search bookkeeping / scoring constants
    # ------------------------------------------------------------------ #
    MATE_SCORE = 1_000_000
    MATE_THRESHOLD = MATE_SCORE - 1_000     # scores above this represent mate
    MAX_PLY = 100                            # hard recursion safety cap
    TT_MAX_ENTRIES = 1_500_000               # cap persisted TT memory (~few hundred MB)
    INF = 10_000_000                         # finite "infinity" for windows

    NULL_MOVE_R = 2                          # base null-move reduction
    LMR_MIN_MOVE = 3                         # first move index eligible for LMR
    MAX_EXTENSIONS = 5                      # non-check extension plies per line
    # Check extensions get their OWN budget so a line full of recaptures can't
    # starve them -- that was why long checking/mating sequences (especially in
    # the endgame) stopped being extended. Generous, but still capped (and
    # MAX_PLY caps total recursion) so perpetual-check trees can't explode.
    MAX_CHECK_EXT = 5

    ASPIRATION_MIN_DEPTH = 4                 # use aspiration from this depth
    ASPIRATION_DELTA = 30                    # initial half-window (centipawns)

    # Pruning margins (centipawns).
    FUTILITY_MARGIN = {1: 150, 2: 320}       # frontier futility by depth
    RFP_MARGIN = 90                          # reverse-futility margin per depth
    DELTA_MARGIN = 120                       # quiescence delta-pruning safety
    # Lazy-eval margin: a safe upper bound on the magnitude of the positional
    # terms (pawn structure / mobility / king safety / mop-up / pins). Measured
    # max ~320 over thousands of diverse positions; 400 leaves a buffer. The
    # quiescence stand-pat skips those expensive terms when the cheap
    # material+PST base alone already proves a >= beta cutoff by this margin --
    # which is exact, so the search is byte-for-byte unchanged.
    LAZY_MARGIN = 400

    # Random tiebreak: among root moves within this many centipawns of the
    # best, one is chosen at random. Keeps equal positions from cycling
    # without ever preferring a measurably worse move.
    TIEBREAK_MARGIN = 5

    # Move-ordering score for a capture that SEE proves loses material. Placed
    # *below* the killer (800_000) and counter-move (780_000) bands so a losing
    # capture is tried after those quiet refutations, but still above the quiet
    # history scores. The (negative) SEE value is added so worse captures sort
    # lower among themselves.
    SEE_LOSING_CAPTURE = 700_000

    # ------------------------------------------------------------------ #
    # Evaluation weights (centipawns)
    # Tuned via coordinate descent on lichess-big3-resolved.book (7.15M WDL
    # positions, K=122).  Values below reflect a selective merge: material,
    # pawn-structure penalties, king-safety MG/EG split, and bishop-pair EG
    # were all taken from the tuner.  PASSED_PAWN_MG and MOBILITY_KNIGHT were
    # kept at hand-tuned values because the tuner collapsed them to
    # chess-senseless lows (WDL signal is weak for those terms in busy MG
    # positions).
    #
    # Bayesian game-based tuning (chess-tuning-tools, Jun 2026): ran ~500
    # iterations (100 games each, 5+0.05 TC) on 8 structural params.  GP
    # converged to an optimum of ~+12 Elo but CI straddled 0.  Direct match
    # (1000 games) showed candidate -8 ±22 Elo vs current values — no
    # improvement confirmed.  Current values retained.
    # ------------------------------------------------------------------ #
    # Bishop pair bonus: worth more in the endgame (fewer pieces to block diags).
    BISHOP_PAIR_MG = 32
    BISHOP_PAIR_EG = 55
    ROOK_OPEN_FILE = 17
    ROOK_SEMIOPEN_FILE = 11
    # Tempo is high by convention -- WDL tuning consistently finds 15-20 optimal.
    TEMPO = 20

    DOUBLED_PAWN = 13
    ISOLATED_PAWN = 10
    BACKWARD_PAWN = 10
    # Penalty for a piece pinned (absolutely, to its own king): it cannot move
    # off the pin line, so its real mobility/usefulness is far below what the
    # raw mobility term credits it, and it is a standing tactical target.
    PIN_PENALTY = {
        chess.PAWN: 6, chess.KNIGHT: 14, chess.BISHOP: 14,
        chess.ROOK: 18, chess.QUEEN: 22, chess.KING: 0,
    }
    # Passed-pawn bonus indexed by the pawn's rank *from its own side* (0..7).
    # MG table is hand-tuned (tuner collapsed ranks 5-7 to ~20, which is wrong).
    # EG table is tuner output -- back-rank passers are heavily rewarded.
    PASSED_PAWN_MG = [0, 10, 17, 25, 40, 65, 105, 0]
    PASSED_PAWN_EG = [0, 1, 6, 31, 45, 45, 45, 0]

    # Per-piece mobility weight (centipawns per reachable square).
    # Knight kept at 4 -- tuner found 1, but WDL signal for knight mobility is
    # thin in the middlegame; 4 is consistent with other HCE engines.
    MOBILITY_WEIGHT = {
        chess.KNIGHT: 4, chess.BISHOP: 3, chess.ROOK: 2, chess.QUEEN: 1,
    }
    # King-safety MG/EG split: attack/shield matter in MG only; in EG the king
    # should be active, so EG penalties are near zero.
    KING_RING_ATTACK_MG = 13
    KING_RING_ATTACK_EG = 0
    KING_SHIELD_MG = 5
    KING_SHIELD_EG = 2
    KING_OPEN_FILE_MG = 28
    KING_OPEN_FILE_EG = 2

    # Endgame "mop-up": when one side has a decisive non-pawn material edge in
    # an endgame, drive the weak king toward a corner and march the strong king
    # in. Pure guidance (well under a pawn) so it never overrides real material;
    # it just breaks the eval ties that previously let KQK / KRK / KQK-vs-P
    # shuffle without making progress.
    MOPUP_MIN_ADV = 400          # min non-pawn material edge (cp) to engage
    MOPUP_CMD_WEIGHT = 12        # x weak-king centre-distance (0..6) -> push to edge
    MOPUP_KING_WEIGHT = 5        # x king closeness (0..~13) -> bring our king up
    # Strong weights for the bare-king mating eval: the king-driving gradient
    # must dominate the search (and the noise of extra winning material) so the
    # engine actually corners the king instead of shuffling. Kept well under a
    # minor piece so it never tempts sacrificing real material for "mop-up shape".
    MOPUP_STRONG_CMD_WEIGHT = 32
    MOPUP_STRONG_KING_WEIGHT = 16

    # "Trade down when ahead": once a side leads by a clear margin it should
    # exchange pieces (heading for a won endgame). SIMPLIFY_WEIGHT cp is handed
    # to the leader for every minor/major piece already off the board, but only
    # when the material lead is at least SIMPLIFY_THRESHOLD. Pure guidance (well
    # under a piece) so it never tempts a real material sacrifice.
    SIMPLIFY_THRESHOLD = 200       # min material lead (cp) to engage
    SIMPLIFY_WEIGHT = 10           # cp per piece already traded off

    # Contempt: avoid draws (repetitions / 50-move) while clearly winning and
    # accept them while clearly losing. Only applied when the side to move is
    # ahead / behind by at least DRAW_AVOID_MARGIN centipawns of material, so a
    # balanced position still scores a draw as 0.
    CONTEMPT = 50
    DRAW_AVOID_MARGIN = 200

    def __init__(self):
        # Middlegame / endgame piece-square tables keyed by piece type.
        self.mg_tables = {
            chess.PAWN: self.MG_PAWN_TABLE,
            chess.KNIGHT: self.MG_KNIGHT_TABLE,
            chess.BISHOP: self.MG_BISHOP_TABLE,
            chess.ROOK: self.MG_ROOK_TABLE,
            chess.QUEEN: self.MG_QUEEN_TABLE,
            chess.KING: self.MG_KING_TABLE,
        }
        self.eg_tables = {
            chess.PAWN: self.EG_PAWN_TABLE,
            chess.KNIGHT: self.EG_KNIGHT_TABLE,
            chess.BISHOP: self.EG_BISHOP_TABLE,
            chess.ROOK: self.EG_ROOK_TABLE,
            chess.QUEEN: self.EG_QUEEN_TABLE,
            chess.KING: self.EG_KING_TABLE,
        }

        # --- Precomputed pawn-evaluation masks (built once) ------------- #
        # These turn the per-pawn structure tests (isolated / backward /
        # passed) into O(1) bitboard intersections in the hot evaluation path.
        self._build_pawn_masks()

        # --- Search results, exposed to the UI after each move ---------- #
        self.nodes_searched = 0
        self.last_score = 0
        self.last_depth = 0
        self.last_pv = ""           # principal variation of the last search
        self.pv_uci = True         # PV format: False = SAN (Nf3), True = UCI (g1f3)

        # --- Internal search bookkeeping (reset every search) ----------- #
        self.nodes = 0
        self.tt = {}                # pos-key -> (depth, flag, value, best_move, static_eval, gen)
        self.killers = {}           # ply -> [killer_1, killer_2]
        self.history = {}           # (color, from_sq, to_sq) -> score
        self.countermoves = {}      # (prev_from, prev_to) -> refutation move
        self.start_time = 0.0
        self.time_limit = None
        # Best root move seen so far in the current (possibly incomplete) ID
        # iteration. Updated after each fully-evaluated root move in _search_root
        # so that if _TimeUp fires mid-iteration we can still use the partial
        # result rather than falling back to the previous depth's move.
        self._partial_root_move = None

        # Incremental-eval accumulator (material + PST + phase, White's view, raw
        # uncapped phase). Maintained move-by-move via _make/_unmake instead of
        # the from-scratch scan loop in _eval_base_white. `_acc_valid` is True
        # only while a search is maintaining it; external evaluate_position calls
        # (acc not maintained) fall back to the scan loop. See use_incremental_eval.
        self._acc = (0, 0, 0)
        self._acc_stack = []
        self._root_acc = (0, 0, 0)
        self._acc_valid = False

        # #3 Pawn-structure hash. _pawn_structure_bb is a *pure* function of
        # (wp, bp, phase), so its result is memoized on exactly that tuple --
        # the only correct key (the passed-pawn bonus is phase-tapered, so a
        # pawns-only key would be wrong). Pawn positions change in only a small
        # fraction of nodes, giving a high hit rate. The cache persists across
        # moves and searches (the function never depends on search state) and is
        # cleared wholesale once it exceeds PAWN_CACHE_MAX to bound memory.
        self._pawn_cache = {}
        self.PAWN_CACHE_MAX = 200_000

        # Static Exchange Evaluation toggle. Used both at search time and by the
        # benchmark, which flips it off to measure the with/without-SEE delta on
        # the same code path.
        self.use_see = True

        # Late-move-reduction mode. When True, reductions are drawn from the
        # log(depth)*log(move_index) table below (more aggressive at deeper
        # plies / later moves, which buys search depth); when False the original
        # fixed 1/2-ply scheme is used. Benchmarked via bench_nps.py at ~-32%
        # nodes for the same fixed depth with no tactical regression, so it is
        # on by default; the flag stays for A/B comparisons.
        self.lmr_aggressive = True
        self._lmr_table = [[0] * 64 for _ in range(64)]
        for d in range(1, 64):
            for m in range(1, 64):
                self._lmr_table[d][m] = int(0.75 + math.log(d) * math.log(m) / 2.0)

        # Capture-sequence (recapture) extension mode -- searches an unresolved
        # exchange one ply deeper so the engine does not mis-judge a capture by
        # stopping in the middle of a trade:
        #   'off' - never extend recaptures
        #   'all' - extend every same-square recapture
        #   'see' - extend only sound recaptures (SEE >= 0), keeping the
        #           extension limited to genuine exchanges (the "limit it" form).
        # Benchmarked (bench_nps.py): the quiescence search already resolves
        # capture sequences at the leaves, so extending them again in the main
        # search cost ~35% more nodes (on top of LMR) for no tactical gain --
        # WAC solve-rate and fixed-depth tactic scores were identical for all
        # three modes. So it is 'off' by default; the flag stays for A/B / game
        # testing, where match results are the final arbiter. (~doubles nodes on
        # tactical positions when 'all' -> 'off' nearly halves the tree.)
        self.recapture_ext = 'off'

        # Penalise absolutely-pinned pieces in the evaluation (see
        # _pin_penalty_bb). A/B over 800 games: pin-ON scored 49.5% vs pin-OFF
        # (-3.5 Elo, within noise) while costing ~6% nps -- so it is OFF by
        # default. Flag kept for future A/B if the term is improved.
        self.use_pin_eval = False

        # "Trade down when ahead" eval term (see SIMPLIFY_*). A/B verdict: ON
        # scored 47.9% (-14 Elo) over 800 games @0.8s -> it HURTS (likely trades
        # into drawn endings), so OFF. (A noisy 100-game/0.15s run had said +;
        # the full match overturned it.) Flag kept if the term is reworked.
        self.use_simplify = False

        # History malus ("history gravity"): on a quiet beta-cutoff, the move
        # that caused the cutoff is rewarded (depth^2, as before) AND every
        # quiet move that was searched *before* it at this node -- and so failed
        # to cut -- is penalised by the same magnitude. This stops moves that
        # repeatedly look good in ordering but never actually refute anything
        # from keeping a stale high history score, sharpening quiet-move
        # ordering over time. The reward/penalty are damped toward zero
        # ("gravity") so the score can't run away. Standard technique; kept
        # behind a flag for A/B (nodes / move-agreement / match Elo) per the
        # one-feature-at-a-time workflow.
        self.use_history_malus = True
        self.HISTORY_MAX = 1 << 14   # clamp |history| so a key can't dominate

        # Skip the (otherwise wasted) full static eval at PV nodes -- see the
        # note in _negamax. Behaviour-preserving, so it is a pure speedup; the
        # flag exists only so the benchmark can A/B the saved eval calls and
        # confirm the search is byte-for-byte identical with it on vs off.
        self.lazy_pv_eval = True

        # Late-move (move-count) pruning. At a shallow, non-PV, not-in-check
        # node, once this many QUIET moves have been searched without beating
        # alpha, stop searching the rest -- the ordering (TT/captures/killers/
        # counter/history, all tried first) makes it very unlikely a late quiet
        # move matters. Unlike lazy_pv_eval this is LOSSY: it can prune the rare
        # quiet move that was actually the only refutation (incl. quiet checks,
        # which are not filtered out here -- testing for check needs a push,
        # which defeats the point). So the table is conservative and the only
        # honest verdict is a self-play match (engine_battle.py), not node
        # counts. Captures/promotions are never pruned: they all sort ABOVE
        # every quiet in order_moves, so by the time a quiet is reached the rest
        # of the list is quiet too -- which is why a plain `break` is safe.
        # A/B verdict (200 games @0.75+0.5, LMP-ON vs an identical LMP-OFF
        # build): ON scored 53.5% = +24 +/- 49 Elo -- positive but inside the
        # noise band, so not significant on its own. The mechanism is clear and
        # measured, though: ON averaged depth 8.0 vs 7.4 (+0.6 ply, +1 median
        # ply) at the same clock, on ~16% fewer nodes/move (~14% lower NPS, as
        # cheap near-leaf quiets are exactly what gets pruned). Decisive games
        # ran 49-35 for ON. Kept ON: deeper search, no tactical regression
        # (identical WAC solve count in bench), no sign of harm over 200 games.
        self.use_lmp = True
        self.LMP_MAX_DEPTH = 3
        self.LMP_COUNT = {1: 6, 2: 10, 3: 14}   # quiets searched before pruning

        # Cache the side-to-move static eval in the TT entry and reuse it on a
        # hit instead of recomputing the full positional eval. The static eval
        # is a deterministic function of the position and the TT key is
        # collision-free, so the cached value is always exactly the value the
        # eval would return -- this is a pure speedup that cannot change the
        # search (same nodes/scores/move). It attacks the NPS cost that LMP's
        # extra interior nodes add, since every non-PV node that pruning needs
        # an eval for and finds in the TT now skips _evaluate_stm. The flag lets
        # the benchmark A/B the saved eval calls on identical code.
        # Measured (fixed depth, ON vs OFF): byte-for-byte identical search
        # (same move/score/node count), ~22% fewer _evaluate_stm calls, which
        # nets ~+3.5% NPS -- eval is only a fraction of per-node cost (movegen /
        # ordering / SEE / push-pop / the quiescence lazy-base eval are not
        # touched), so the call saving doesn't translate 1:1 to speed. Pure,
        # safe speedup with no behavioural change -> kept ON; no match needed.
        self.tt_cached_eval = True

        # SEE-refined quiescence capture ordering (see _capture_moves). Pure
        # reordering -> value-preserving. A/B verdict: a STRICT no-op here --
        # value AND node count were identical (+0.0%) on every test position.
        # Quiescence already SEE-PRUNES losing captures inside the loop, so
        # demoting them in the ordering changes nothing about which nodes are
        # visited, while the sound captures keep their MVV-LVA order. So it only
        # adds wasted SEE calls during ordering -> OFF. (Reordering the *sound*
        # captures by SEE could move nodes, but that needs SEE for every capture
        # on the quiescence hot path -- near-certainly net-negative on time.)
        self.use_qsee_order = False

        # Depth-preferred TT replacement with per-search aging (see the store in
        # _negamax). Correctness-safe regardless (TT entries are always valid
        # bounds; keeping or dropping any never makes the search wrong). A/B
        # verdict (16-ply Ruy Lopez line, depth 7, persistent TT): depth-
        # preferred searched ~+6% MORE nodes and was slower than the original
        # always-replace. Always-replace keeps the freshest best_move/bound,
        # which orders the current search region better than a deeper-but-staler
        # entry -- and ordering quality dominates node count more than the odd
        # extra cutoff a deeper entry buys. So always-replace (OFF) wins. Flag +
        # `_tt_gen` kept for a future smarter scheme (e.g. a two-tier bucket:
        # one depth-preferred slot + one always-replace slot per key).
        self.use_tt_depth_replace = False
        self._tt_gen = 0

        # Two-tier ("two-deep") TT bucket: store TWO entries per position -- a
        # depth-preferred slot (keeps the deepest result, demoting the old deep
        # entry rather than discarding it) and an always-replace slot (keeps the
        # freshest). Aims for best-of-both over plain always-replace vs depth-
        # preferred. Correctness-safe (every stored entry is still a valid bound).
        # A/B verdict (4 diverse 16-ply lines, depth 7, persistent TT): a WASH --
        # ~1% fewer nodes and ~0.7% faster wall-time, but NPS a hair LOWER (the
        # extra probe/store work eats most of the node saving), all inside the
        # noise. Notably it does NOT regress like the single-slot depth-preferred
        # (use_tt_depth_replace) did. Given it also costs ~2x TT memory per key,
        # the marginal gain doesn't justify it -> OFF. Kept (with _tt_get/
        # _tt_store hiding the format) as the one TT scheme worth a self-play
        # match if that ~1% is ever worth chasing. NOTE: turning this ON ~doubles
        # TT memory; consider halving TT_MAX_ENTRIES to keep the cap honest.
        self.use_tt_two_tier = False

        # Incremental evaluation: maintain the (material + PST + phase) base
        # accumulator with a small per-move delta on each make/unmake (see
        # _make / _move_delta) instead of recomputing the full bitboard scan in
        # _eval_base_white at every node. Behaviour-preserving -- the cached
        # accumulator is byte-for-byte what the scan would produce -- so it is a
        # pure speedup (nodes/scores/move identical); the flag lets the benchmark
        # A/B the NPS gain. Only the cheap "base" half is incremental; the
        # positional terms (pawns/mobility/king-safety) are still recomputed.
        # Verified: _move_delta exact over 46k move/position pairs (all move types
        # incl. ep/castle/promo), search byte-identical on vs off (move/score/
        # nodes) across 37 positions; measured ~+10.5% NPS at fixed depth (nodes
        # identical). Runs under PyPy too, so it stacks with the ~1.5x there and
        # -- unlike PyPy -- it also speeds up the CPython GUI directly. Kept ON.
        self.use_incremental_eval = True

        # NOTE: staged (lazy) move generation was implemented and A/B'd here, and
        # REMOVED. It was ~+6% NPS but LOST a self-play match decisively (-27 and
        # -19 +/- 24 Elo over ~800 games each, two independent matchups): yielding
        # moves in stages reorders the ties among equal-scored quiets differently
        # from board.legal_moves, and that natural order is a better tiebreak for
        # the order-dependent prunes (LMR/LMP), so search quality dropped more
        # than the speed helped. Don't re-add it without also making the quiet
        # tiebreak match the eager order -- which defeats the point. order_moves
        # (eager, full sort) is the move source for the whole search.

        # #9: C legal move generator for order_moves. Byte-identical to
        # board.legal_moves (same eager order -> same LMR/LMP tiebreaks, unlike
        # the staged experiment above), so nodes/scores/move are unchanged; only
        # the per-node move-gen cost drops. In-check nodes fall back to
        # board.legal_moves. Set False to A/B the NPS gain (or if movegen.so is
        # absent the module flag is already False -> pure-python path).
        self.use_c_movegen = _USE_C_MOVEGEN

        # --- Real-time search logging ----------------------------------- #
        self.on_depth = None        # called after each completed ID iteration
        self.on_final = None        # called once the move is chosen
        self.search_log = []

        # --- Opening book (Polyglot .bin) ------------------------------- #
        # Drop a Polyglot book next to the .py files (or the working dir) under
        # one of these names; the first that opens is used. Free books include
        # Performance.bin / Titans.bin / gm2600.bin / baron30.bin / komodo.bin
        # (e.g. from https://github.com/niklasf/python-chess test data or the
        # many "polyglot book" mirrors). Set ``use_book = False`` to disable.
        self.use_book = True
        self._book_reader = None
        self._book_resolved = False
        self.book_path = None
        self._book_candidates = [
            "Elo2400.bin", "Performance.bin", "Titans.bin", "book.bin",
            "gm2600.bin", "baron30.bin", "komodo.bin",
        ]

        # --- #4 Endgame tablebase (Lichess Syzygy API) ------------------ #
        # At the ROOT ONLY, positions with few pieces are looked up in the free
        # Lichess Syzygy tablebase (https://tablebase.lichess.ovh), which hosts
        # provably-perfect WDL/DTZ data for <= 7 pieces. A hit returns the
        # optimal move and skips the search entirely. It is NEVER queried inside
        # the search (that would fire thousands of HTTP requests/sec). Any
        # network error, timeout, illegal or empty response falls back to a
        # normal search, so play is unaffected when offline. Set
        # ``use_tb = False`` for fully offline play / reproducible benchmarks.
        self.use_tb = False
        self.tb_max_pieces = 7              # Lichess hosts 7-man Syzygy
        self.tb_timeout = 1.0               # max seconds to wait on the network
        self.tb_url = "https://tablebase.lichess.ovh/standard?fen="
        self._tb_cache = {}                 # fen -> (wdl, Move) | None
        self.TB_CACHE_MAX = 50_000
        self.TB_SCORE_UNIT = 1000           # display cp per WDL step (for the UI)

        # --- #8: sync C eval constants to current class attributes --------- #
        if _USE_C_EVAL:
            _eval_lib.set_mobility_params(
                self.MOBILITY_WEIGHT[chess.KNIGHT],
                self.MOBILITY_WEIGHT[chess.BISHOP],
                self.MOBILITY_WEIGHT[chess.ROOK],
                self.MOBILITY_WEIGHT[chess.QUEEN],
                self.PHASE_MAX,
                self.KING_SHIELD_MG,     self.KING_SHIELD_EG,
                self.KING_RING_ATTACK_MG, self.KING_RING_ATTACK_EG,
                self.KING_OPEN_FILE_MG,  self.KING_OPEN_FILE_EG,
            )

    # ================================================================== #
    # Pawn-mask precomputation (one-time, used by the evaluation)
    # ================================================================== #
    def _build_pawn_masks(self):
        self._file_bb = [int(chess.BB_FILES[f]) for f in range(8)]
        self._adj_files_bb = [
            ((self._file_bb[f - 1] if f > 0 else 0)
             | (self._file_bb[f + 1] if f < 7 else 0))
            for f in range(8)
        ]
        # passed[color][sq]: enemy-pawn squares that would block/guard the pawn
        # (own file + adjacent files, all ranks strictly ahead of the pawn).
        # support[color][sq]: friendly-pawn squares on adjacent files at or
        # behind the pawn's rank (a pawn there can advance to defend it).
        # stop_atk[color][sq]: enemy-pawn squares that attack the pawn's stop
        # square (the square one rank ahead).
        self._passed_mask = {chess.WHITE: [0] * 64, chess.BLACK: [0] * 64}
        self._support_mask = {chess.WHITE: [0] * 64, chess.BLACK: [0] * 64}
        self._stop_atk_mask = {chess.WHITE: [0] * 64, chess.BLACK: [0] * 64}
        for sq in range(64):
            f = sq & 7
            r = sq >> 3
            for color in (chess.WHITE, chess.BLACK):
                ahead = range(r + 1, 8) if color == chess.WHITE else range(0, r)
                behind = range(0, r + 1) if color == chess.WHITE else range(r, 8)
                passed = support = stop = 0
                for nf in (f - 1, f, f + 1):
                    if not (0 <= nf < 8):
                        continue
                    for nr in ahead:
                        passed |= 1 << (nr * 8 + nf)
                for nf in (f - 1, f + 1):
                    if not (0 <= nf < 8):
                        continue
                    for nr in behind:
                        support |= 1 << (nr * 8 + nf)
                stop_r = r + 1 if color == chess.WHITE else r - 1
                if 0 <= stop_r < 8:
                    # An enemy pawn attacks 'stop' if it sits one rank further
                    # ahead on an adjacent file (it captures backwards onto stop).
                    atk_r = stop_r + 1 if color == chess.WHITE else stop_r - 1
                    if 0 <= atk_r < 8:
                        for nf in (f - 1, f + 1):
                            if 0 <= nf < 8:
                                stop |= 1 << (atk_r * 8 + nf)
                self._passed_mask[color][sq] = passed
                self._support_mask[color][sq] = support
                self._stop_atk_mask[color][sq] = stop

        # Centre Manhattan distance per square: 0 on the four centre squares,
        # up to 6 in the corners. The endgame mop-up term uses this to push the
        # weak king toward an edge / corner where it can be mated.
        _edge = [3, 2, 1, 0, 0, 1, 2, 3]
        self._center_manhattan = [
            _edge[sq & 7] + _edge[sq >> 3] for sq in range(64)
        ]

    # ================================================================== #
    # Opening book
    # ================================================================== #
    def _get_book_reader(self):
        """Lazily open the first available Polyglot book; cache the reader."""
        if self._book_resolved:
            return self._book_reader
        self._book_resolved = True
        search_dirs = [os.getcwd()]
        try:
            search_dirs.append(os.path.dirname(os.path.abspath(__file__)))
        except NameError:
            pass
        for directory in search_dirs:
            for name in self._book_candidates:
                path = os.path.join(directory, name)
                if os.path.isfile(path):
                    try:
                        self._book_reader = chess.polyglot.open_reader(path)
                        self.book_path = path
                        return self._book_reader
                    except Exception:
                        self._book_reader = None
        return self._book_reader

    def _book_move(self, board):
        """Return a legal Polyglot book move for ``board`` or ``None``.

        Picks *uniformly at random* among all book entries for the position
        (not weighted by the book's move weights). Uniform selection maximises
        opening variety so repeated games don't keep walking the same main line
        -- the engine still only ever plays book (i.e. sound) moves.
        """
        if not self.use_book:
            return None
        reader = self._get_book_reader()
        if reader is None:
            return None
        try:
            entries = [e for e in reader.find_all(board)
                       if e.move in board.legal_moves]
        except Exception:
            return None      # read error
        if not entries:
            return None      # position not in book
        return random.choice(entries).move

    # ================================================================== #
    # #4 Endgame tablebase (Lichess Syzygy API)
    # ================================================================== #
    def _tb_probe(self, board, timeout):
        """Probe the Lichess Syzygy tablebase for the optimal move in ``board``.

        Returns ``(wdl, move)`` -- ``wdl`` in {2,1,0,-1,-2} from the side to
        move's perspective (2=win, 1=cursed win, 0=draw, -1=blessed loss,
        -2=loss), ``move`` the move to play -- or ``None`` on any miss: too many
        pieces, network error, timeout, or an empty / unparseable / illegal
        response. NEVER raises: every failure path returns None so the caller
        falls back to a normal search.

        The Lichess API returns ``moves`` already sorted best-first for the side
        to move, so ``moves[0]`` is the move to play. Trusting that ordering --
        rather than re-deriving a best move from raw WDL/DTZ -- is what makes
        cursed wins / blessed losses (50-move-rule edge cases) correct without
        any fragile tie-break logic of our own.
        """
        if not self.use_tb or timeout <= 0:
            return None
        if board.occupied.bit_count() > self.tb_max_pieces:
            return None
        # Don't spend a network round-trip on positions the search already
        # nails faster than the API responds: dead-drawn insufficient material
        # (the engine returns the draw instantly) and overwhelming pawnless
        # mop-up wins (lone king vs a major piece -- KQK / KRK etc., which the
        # mop-up term is purpose-built to convert). The tablebase is reserved
        # for genuinely tricky endings (pawns, or a defending piece) where the
        # HCE's win/draw verdict or technique could actually be wrong.
        if board.is_insufficient_material() or self._tb_trivial_win(board):
            return None

        fen = board.fen()
        if fen in self._tb_cache:
            return self._tb_cache[fen]

        try:
            url = self.tb_url + urllib.parse.quote(fen)
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None                      # offline / timeout / bad response

        moves = data.get("moves")
        if not moves:
            return None                      # no data for this position
        best_uci = moves[0].get("uci")
        if not best_uci:
            return None
        try:
            move = chess.Move.from_uci(best_uci)
        except Exception:
            return None
        if move not in board.legal_moves:    # paranoia: never play an illegal move
            return None

        wdl = data.get("wdl")
        if wdl is None:                      # WDL occasionally absent -> derive from category
            wdl = {"win": 2, "cursed-win": 1, "draw": 0,
                   "blessed-loss": -1, "loss": -2}.get(data.get("category"), 0)

        result = (wdl, move)
        if len(self._tb_cache) >= self.TB_CACHE_MAX:
            self._tb_cache.clear()
        self._tb_cache[fen] = result
        return result

    def _tb_trivial_win(self, board):
        """True if the position is an overwhelming, pawnless mop-up win that the
        search converts faster than the tablebase round-trip: one side is a bare
        lone king while the other holds a major piece (queen or rook).

        Any pawn makes it 'complex' (KP vs K, KQ vs KP, ... -- where the verdict
        can flip on precise play), so a pawn never counts as trivial. Minor-only
        wins are NOT skipped either: KBN vs K is pawnless but a hard mate that
        the generic center-distance mop-up can botch within the 50-move rule, so
        the tablebase still earns its latency there.
        """
        if board.pawns:
            return False                     # pawns present -> not trivial, probe it
        majors = board.queens | board.rooks
        if not majors:
            return False                     # only minors (KBN/KBB/KNN) -> probe
        w = board.occupied_co[chess.WHITE]
        b = board.occupied_co[chess.BLACK]
        # One side reduced to just its king, the other holding a major piece.
        return ((w.bit_count() == 1 and bool(b & majors))
                or (b.bit_count() == 1 and bool(w & majors)))

    # ================================================================== #
    # Evaluation (hand-crafted, tapered)
    # ================================================================== #
    def evaluate_position(self, board):
        """Public, terminal-aware static evaluation (White's perspective)."""
        if board.is_checkmate():
            return -self.MATE_SCORE if board.turn == chess.WHITE else self.MATE_SCORE
        if (board.is_stalemate()
                or board.is_insufficient_material()
                or board.is_seventyfive_moves()
                or board.is_fivefold_repetition()):
            return 0
        return self._evaluate_static(board)

    def _evaluate_stm(self, board):
        """Static evaluation relative to the side to move (for negamax)."""
        white = self._evaluate_static(board)
        return white if board.turn == chess.WHITE else -white

    def _evaluate_static(self, board):
        """Tapered material + PST plus positional terms, White's perspective.

        Split into a cheap base (material + PST + phase + tempo) and the
        expensive positional terms, so quiescence can evaluate lazily (see
        ``_qs_stand_pat``) without ever recomputing the base.
        """
        base, ctx = self._eval_base_white(board)
        return base + self._eval_positional_white(board, ctx)

    def _eval_base_white(self, board):
        """Cheap half: tapered material + PST + tempo (White's perspective).

        Single bitboard pass (no per-call ``piece_map``/``board.pieces``
        rebuilds). Returns ``(base_score, ctx)`` where ctx carries the bitboards
        and phase the positional half needs, so nothing is recomputed.
        """
        occ_w = board.occupied_co[chess.WHITE]
        occ_b = board.occupied_co[chess.BLACK]
        pawns = board.pawns
        knights = board.knights
        bishops = board.bishops
        rooks = board.rooks
        queens = board.queens
        kings = board.kings

        # mg / eg / (raw) phase: from the live accumulator while a search is
        # maintaining it, else the from-scratch bitboard scan.
        if self.use_incremental_eval and self._acc_valid:
            mg, eg, phase = self._acc
        else:
            mg, eg, phase = self._compute_acc(board)

        if phase > self.PHASE_MAX:
            phase = self.PHASE_MAX
        # Truncate toward zero (not floor) so the blend is exactly symmetric:
        # eval(pos) == -eval(mirror(pos)). Floor division would skew negatives.
        num = mg * phase + eg * (self.PHASE_MAX - phase)
        score = -((-num) // self.PHASE_MAX) if num < 0 else num // self.PHASE_MAX
        # Tempo (cheap, folded into the base so the lazy bound is exact).
        score += self.TEMPO if board.turn == chess.WHITE else -self.TEMPO
        ctx = (occ_w, occ_b, pawns, knights, bishops, rooks, queens, kings,
               pawns & occ_w, pawns & occ_b, phase)
        return score, ctx

    # ------------------------------------------------------------------ #
    # Incremental-eval accumulator (material + PST + raw phase, White's view).
    # ------------------------------------------------------------------ #
    def _compute_acc(self, board):
        """From-scratch (mg, eg, phase) -- the bitboard scan. RAW phase (the
        PHASE_MAX cap is applied later in _eval_base_white, so deltas stay
        reversible)."""
        occ_w = board.occupied_co[chess.WHITE]
        occ_b = board.occupied_co[chess.BLACK]
        mg = eg = phase = 0
        for pt, bb_all in ((chess.PAWN, board.pawns), (chess.KNIGHT, board.knights),
                           (chess.BISHOP, board.bishops), (chess.ROOK, board.rooks),
                           (chess.QUEEN, board.queens), (chess.KING, board.kings)):
            mgt = self.mg_tables[pt]
            egt = self.eg_tables[pt]
            mv = self.MG_VALUES[pt]
            ev = self.EG_VALUES[pt]
            pw = self.PHASE_WEIGHTS[pt]
            for sq in chess.scan_forward(bb_all & occ_w):
                idx = sq ^ 56            # == chess.square_mirror(sq)
                mg += mv + mgt[idx]
                eg += ev + egt[idx]
                phase += pw
            for sq in chess.scan_forward(bb_all & occ_b):
                mg -= mv + mgt[sq]
                eg -= ev + egt[sq]
                phase += pw
        return (mg, eg, phase)

    def _piece_contrib(self, pt, color, sq):
        """A single piece's (d_mg, d_eg, d_phase) contribution, White's view:
        material + PST signed by colour; phase always positive (it counts total
        material on the board, both sides)."""
        if color == chess.WHITE:
            idx = sq ^ 56
            return (self.MG_VALUES[pt] + self.mg_tables[pt][idx],
                    self.EG_VALUES[pt] + self.eg_tables[pt][idx],
                    self.PHASE_WEIGHTS[pt])
        return (-(self.MG_VALUES[pt] + self.mg_tables[pt][sq]),
                -(self.EG_VALUES[pt] + self.eg_tables[pt][sq]),
                self.PHASE_WEIGHTS[pt])

    def _move_delta(self, board, move, acc):
        """``acc`` updated for playing ``move`` on ``board`` (read BEFORE the
        push). Mirrors exactly what _compute_acc would return on the resulting
        position -- handles captures, en passant, promotions and castling."""
        mg, eg, phase = acc
        color = board.turn
        frm = move.from_square
        to = move.to_square
        mover_pt = board.piece_type_at(frm)

        # Mover leaves `frm`.
        a, b, c = self._piece_contrib(mover_pt, color, frm)
        mg -= a; eg -= b; phase -= c

        # Captured piece (if any) leaves the board.
        if board.is_en_passant(move):
            cap_sq = to + (-8 if color == chess.WHITE else 8)
            a, b, c = self._piece_contrib(chess.PAWN, not color, cap_sq)
            mg -= a; eg -= b; phase -= c
        elif board.is_capture(move):
            a, b, c = self._piece_contrib(board.piece_type_at(to), not color, to)
            mg -= a; eg -= b; phase -= c

        # Mover (or the promoted piece) arrives on `to`.
        new_pt = move.promotion if move.promotion else mover_pt
        a, b, c = self._piece_contrib(new_pt, color, to)
        mg += a; eg += b; phase += c

        # Castling: the rook moves too.
        if board.is_castling(move):
            if board.is_kingside_castling(move):
                r_from = chess.H1 if color == chess.WHITE else chess.H8
                r_to = chess.F1 if color == chess.WHITE else chess.F8
            else:
                r_from = chess.A1 if color == chess.WHITE else chess.A8
                r_to = chess.D1 if color == chess.WHITE else chess.D8
            a, b, c = self._piece_contrib(chess.ROOK, color, r_from)
            mg -= a; eg -= b; phase -= c
            a, b, c = self._piece_contrib(chess.ROOK, color, r_to)
            mg += a; eg += b; phase += c

        return (mg, eg, phase)

    def _make(self, board, move):
        """Push ``move`` and (when maintaining the accumulator) update it."""
        if self.use_incremental_eval and self._acc_valid:
            self._acc_stack.append(self._acc)
            self._acc = self._move_delta(board, move, self._acc)
        board.push(move)

    def _make_null(self, board):
        """Push a null move -- material/PST/phase are unchanged (only the side
        to move flips, and the tempo term reads board.turn live)."""
        if self.use_incremental_eval and self._acc_valid:
            self._acc_stack.append(self._acc)
        board.push(chess.Move.null())

    def _unmake(self, board):
        """Pop the last move and restore the accumulator."""
        board.pop()
        if self.use_incremental_eval and self._acc_valid:
            self._acc = self._acc_stack.pop()

    def _eval_positional_white(self, board, ctx):
        """Expensive half: pawn structure / mobility / king safety / mop-up /
        pins, summed (White's perspective). ``ctx`` comes from
        ``_eval_base_white`` so no bitboards are recomputed."""
        (occ_w, occ_b, pawns, knights, bishops, rooks, queens, kings,
         wp, bp, phase) = ctx
        # "Mating" scenario: one side is down to a lone king (+ pawns).
        lone_loser = ((occ_w & ~kings & ~pawns) == 0) != ((occ_b & ~kings & ~pawns) == 0)
        if lone_loser:
            # Dedicated mating evaluation -- skip the noisy positional terms and
            # use a strong mop-up (see the long note this replaced).
            return self._mopup_bb(occ_w, occ_b, knights, bishops,
                                  rooks, queens, kings, strong=True)
        delta = self._pawn_structure_bb(wp, bp, phase)
        delta += self._rook_files_bb(rooks, occ_w, occ_b, wp, bp)
        delta += self._bishop_pair_bb(bishops, occ_w, occ_b, phase)
        if self.use_pin_eval:
            delta += self._pin_penalty_bb(board, occ_w, occ_b)
        if self.use_simplify:
            delta += self._simplify_bb(occ_w, occ_b, pawns, knights,
                                       bishops, rooks, queens)
        if phase <= 6:
            delta += self._mobility_bb(board, occ_w, occ_b,
                                       knights, bishops, rooks, queens)
            delta += self._mopup_bb(occ_w, occ_b, knights, bishops,
                                    rooks, queens, kings)
        else:
            # Combined pass: one attacks_mask per piece feeds BOTH mobility and
            # the king-ring attacker count (previously 16 separate, expensive
            # attackers_mask calls). Identical result, fewer board queries.
            delta += self._mobility_king_safety_bb(
                board, occ_w, occ_b, knights, bishops, rooks, queens, wp, bp, phase)
        return delta

    def _qs_stand_pat(self, board, beta):
        """Stand-pat eval for quiescence, evaluated lazily: if the cheap base
        already proves stand_pat >= beta by LAZY_MARGIN (>= the max positional
        swing), skip the expensive positional terms. The cutoff is identical to
        the full eval, so the search is unchanged -- just faster."""
        base, ctx = self._eval_base_white(board)
        base_stm = base if board.turn == chess.WHITE else -base
        if base_stm - self.LAZY_MARGIN >= beta:
            return base_stm                          # full eval is also >= beta
        full = base + self._eval_positional_white(board, ctx)
        return full if board.turn == chess.WHITE else -full

    def _is_endgame(self, board):
        phase = (((board.knights | board.bishops).bit_count()) * 1
                 + (board.rooks.bit_count()) * 2
                 + (board.queens.bit_count()) * 4)
        return phase <= 6

    # ------------------------------------------------------------------ #
    # Pawn structure: doubled, isolated, backward and passed pawns.
    # O(1) per pawn using masks precomputed in __init__.
    # ------------------------------------------------------------------ #
    def _pawn_structure_bb(self, wp, bp, phase):
        # #3 Pawn hash: this whole function is a pure function of (wp, bp,
        # phase). Return the memoized score if seen before. ``is not None``
        # (not truthiness) so a legitimately-cached 0 score is still a hit.
        key = (wp, bp, phase)
        cached = self._pawn_cache.get(key)
        if cached is not None:
            return cached
        score = 0
        pm = self.PHASE_MAX
        ppm = self.PASSED_PAWN_MG
        ppe = self.PASSED_PAWN_EG
        for own, opp, sign, color in ((wp, bp, 1, chess.WHITE),
                                      (bp, wp, -1, chess.BLACK)):
            for f in range(8):
                c = (own & self._file_bb[f]).bit_count()
                if c > 1:                                  # doubled
                    score -= sign * self.DOUBLED_PAWN * (c - 1)
            passed = self._passed_mask[color]
            support = self._support_mask[color]
            stopatk = self._stop_atk_mask[color]
            for sq in chess.scan_forward(own):
                f = sq & 7
                if not (own & self._adj_files_bb[f]):      # isolated
                    score -= sign * self.ISOLATED_PAWN
                elif not (own & support[sq]) and (opp & stopatk[sq]):
                    score -= sign * self.BACKWARD_PAWN     # backward
                if not (opp & passed[sq]):                  # passed
                    r = sq >> 3
                    rel = r if color == chess.WHITE else 7 - r
                    bonus = (ppm[rel] * phase + ppe[rel] * (pm - phase)) // pm
                    score += sign * bonus
        # #3 Memoize. Cap memory by dropping the whole cache when it grows too
        # large (cheaper and simpler than per-entry eviction; the cache refills
        # quickly and correctness is unaffected because the function is pure).
        if len(self._pawn_cache) >= self.PAWN_CACHE_MAX:
            self._pawn_cache.clear()
        self._pawn_cache[key] = score
        return score

    def _is_passed_pawn(self, board, square, color):
        """Per-call passed-pawn test (used only by the rare push extension)."""
        opp = board.pawns & board.occupied_co[not color]
        return not (opp & self._passed_mask[color][square])

    # ------------------------------------------------------------------ #
    # King safety: pawn shield, open file near the king, attacker count.
    # ------------------------------------------------------------------ #
    def _king_safety_bb(self, board, occ_w, occ_b, wp, bp):
        score = 0
        for color in (chess.WHITE, chess.BLACK):
            sign = 1 if color == chess.WHITE else -1
            ksq = board.king(color)
            if ksq is None:
                continue
            ring = chess.BB_KING_ATTACKS[ksq]
            own = occ_w if color == chess.WHITE else occ_b
            own_pawns = wp if color == chess.WHITE else bp
            shield = (ring & own).bit_count()
            attackers = 0
            for sq in chess.scan_forward(ring):
                attackers += board.attackers_mask(not color, sq).bit_count()
            score += sign * shield * self.KING_SHIELD_MG
            score -= sign * attackers * self.KING_RING_ATTACK_MG
            if not (own_pawns & self._file_bb[ksq & 7]):    # open king file
                score -= sign * self.KING_OPEN_FILE_MG
        return score

    # ------------------------------------------------------------------ #
    # Combined mobility + king safety (non-endgame). One attacks_mask per
    # sliding/knight piece serves both terms, so the king-ring attacker count
    # no longer needs 16 separate attackers_mask calls. The result is identical
    # to _mobility_bb + _king_safety_bb (verified node-for-node).
    # ------------------------------------------------------------------ #
    def _mobility_king_safety_bb(self, board, occ_w, occ_b,
                                 knights, bishops, rooks, queens, wp, bp, phase):
        if _USE_C_EVAL:
            wksq = board.king(chess.WHITE)
            bksq = board.king(chess.BLACK)
            return _eval_lib.mobility_king_safety(
                occ_w, occ_b, knights, bishops, rooks, queens, wp, bp,
                wksq if wksq is not None else -1,
                bksq if bksq is not None else -1,
                phase,
            )
        am = board.attacks_mask
        wksq = board.king(chess.WHITE)
        bksq = board.king(chess.BLACK)
        wring = chess.BB_KING_ATTACKS[wksq] if wksq is not None else 0
        bring = chess.BB_KING_ATTACKS[bksq] if bksq is not None else 0

        score = 0
        w_ring_att = 0          # incidences of black pieces attacking White's ring
        b_ring_att = 0          # incidences of white pieces attacking Black's ring
        for pt, bb in ((chess.KNIGHT, knights), (chess.BISHOP, bishops),
                       (chess.ROOK, rooks), (chess.QUEEN, queens)):
            wt = self.MOBILITY_WEIGHT[pt]
            for sq in chess.scan_forward(bb & occ_w):
                a = am(sq)
                score += wt * (a & ~occ_w).bit_count()
                b_ring_att += (a & bring).bit_count()
            for sq in chess.scan_forward(bb & occ_b):
                a = am(sq)
                score -= wt * (a & ~occ_b).bit_count()
                w_ring_att += (a & wring).bit_count()

        # Pawn and enemy-king ring incidences (not covered by the loop above).
        if wring:
            for sq in chess.scan_forward(bp):
                w_ring_att += (chess.BB_PAWN_ATTACKS[chess.BLACK][sq] & wring).bit_count()
            if bksq is not None:
                w_ring_att += (chess.BB_KING_ATTACKS[bksq] & wring).bit_count()
        if bring:
            for sq in chess.scan_forward(wp):
                b_ring_att += (chess.BB_PAWN_ATTACKS[chess.WHITE][sq] & bring).bit_count()
            if wksq is not None:
                b_ring_att += (chess.BB_KING_ATTACKS[wksq] & bring).bit_count()

        # King safety terms (shield / attacker penalty / open file), White POV.
        # Tapered: these terms matter more in the middlegame than the endgame.
        pm = self.PHASE_MAX
        shield_val   = (self.KING_SHIELD_MG   * phase + self.KING_SHIELD_EG   * (pm - phase)) // pm
        ring_val     = (self.KING_RING_ATTACK_MG * phase + self.KING_RING_ATTACK_EG * (pm - phase)) // pm
        open_val     = (self.KING_OPEN_FILE_MG * phase + self.KING_OPEN_FILE_EG * (pm - phase)) // pm
        if wksq is not None:
            score += (wring & occ_w).bit_count() * shield_val
            score -= w_ring_att * ring_val
            if not (wp & self._file_bb[wksq & 7]):
                score -= open_val
        if bksq is not None:
            score -= (bring & occ_b).bit_count() * shield_val
            score += b_ring_att * ring_val
            if not (bp & self._file_bb[bksq & 7]):
                score += open_val
        return score

    # ------------------------------------------------------------------ #
    # Mobility: per-piece reachable-square count (own pieces excluded).
    # ------------------------------------------------------------------ #
    def _mobility_bb(self, board, occ_w, occ_b, knights, bishops, rooks, queens):
        score = 0
        am = board.attacks_mask
        for pt, bb in ((chess.KNIGHT, knights), (chess.BISHOP, bishops),
                       (chess.ROOK, rooks), (chess.QUEEN, queens)):
            w = self.MOBILITY_WEIGHT[pt]
            for sq in chess.scan_forward(bb & occ_w):
                score += w * (am(sq) & ~occ_w).bit_count()
            for sq in chess.scan_forward(bb & occ_b):
                score -= w * (am(sq) & ~occ_b).bit_count()
        return score

    # ------------------------------------------------------------------ #
    # Rook on open / semi-open file, and the bishop-pair bonus.
    # ------------------------------------------------------------------ #
    def _rook_files_bb(self, rooks, occ_w, occ_b, wp, bp):
        score = 0
        for sq in chess.scan_forward(rooks & occ_w):
            fmask = self._file_bb[sq & 7]
            if not (wp & fmask):
                score += self.ROOK_OPEN_FILE if not (bp & fmask) else self.ROOK_SEMIOPEN_FILE
        for sq in chess.scan_forward(rooks & occ_b):
            fmask = self._file_bb[sq & 7]
            if not (bp & fmask):
                score -= self.ROOK_OPEN_FILE if not (wp & fmask) else self.ROOK_SEMIOPEN_FILE
        return score

    def _pinned_for(self, board, color, own_occ):
        """Bitboard of ``color``'s pieces absolutely pinned to their own king.

        Same logic as python-chess's internal ``_slider_blockers`` but for an
        explicit colour (that method hard-codes the side to move), so it works
        for both kings in one static evaluation."""
        king = board.king(color)
        if king is None:
            return 0
        rq = board.rooks | board.queens
        bq = board.bishops | board.queens
        snipers = ((chess.BB_RANK_ATTACKS[king][0] & rq)
                   | (chess.BB_FILE_ATTACKS[king][0] & rq)
                   | (chess.BB_DIAG_ATTACKS[king][0] & bq)) & board.occupied_co[not color]
        occupied = board.occupied
        blockers = 0
        for sniper in chess.scan_forward(snipers):
            between = chess.between(king, sniper) & occupied
            if between and not (between & (between - 1)):   # exactly one piece between
                blockers |= between
        return blockers & own_occ

    def _pin_penalty_bb(self, board, occ_w, occ_b):
        """Penalty for absolutely-pinned pieces (pinned to their own king)."""
        score = 0
        for sq in chess.scan_forward(self._pinned_for(board, chess.WHITE, occ_w)):
            score -= self.PIN_PENALTY.get(board.piece_type_at(sq), 0)
        for sq in chess.scan_forward(self._pinned_for(board, chess.BLACK, occ_b)):
            score += self.PIN_PENALTY.get(board.piece_type_at(sq), 0)
        return score

    def _simplify_bb(self, occ_w, occ_b, pawns, knights, bishops, rooks, queens):
        """Encourage the materially-leading side to trade pieces (head for a won
        endgame): once the lead is decisive, reward the leader for every
        minor/major piece already off the board. White's perspective."""
        def mat(occ):
            return (100 * (pawns & occ).bit_count()
                    + 320 * (knights & occ).bit_count()
                    + 330 * (bishops & occ).bit_count()
                    + 500 * (rooks & occ).bit_count()
                    + 900 * (queens & occ).bit_count())
        diff = mat(occ_w) - mat(occ_b)
        if abs(diff) < self.SIMPLIFY_THRESHOLD:
            return 0
        pieces = (knights | bishops | rooks | queens).bit_count()   # 0..14
        sign = 1 if diff > 0 else -1
        return sign * self.SIMPLIFY_WEIGHT * (14 - pieces)

    def _bishop_pair_bb(self, bishops, occ_w, occ_b, phase):
        pm = self.PHASE_MAX
        bp = (self.BISHOP_PAIR_MG * phase + self.BISHOP_PAIR_EG * (pm - phase)) // pm
        score = 0
        if (bishops & occ_w).bit_count() >= 2:
            score += bp
        if (bishops & occ_b).bit_count() >= 2:
            score -= bp
        return score

    # ------------------------------------------------------------------ #
    # Endgame mop-up: drive the weak king to the edge, bring our king up.
    # Engaged only with a decisive non-pawn material edge, so it cannot
    # distort balanced or drawish endings.
    # ------------------------------------------------------------------ #
    def _mopup_bb(self, occ_w, occ_b, knights, bishops, rooks, queens, kings,
                  strong=False):
        npm_w = (320 * (knights & occ_w).bit_count()
                 + 330 * (bishops & occ_w).bit_count()
                 + 500 * (rooks & occ_w).bit_count()
                 + 900 * (queens & occ_w).bit_count())
        npm_b = (320 * (knights & occ_b).bit_count()
                 + 330 * (bishops & occ_b).bit_count()
                 + 500 * (rooks & occ_b).bit_count()
                 + 900 * (queens & occ_b).bit_count())
        adv = npm_w - npm_b
        if abs(adv) < self.MOPUP_MIN_ADV:
            return 0
        wk = (kings & occ_w).bit_length() - 1      # white king square
        bk = (kings & occ_b).bit_length() - 1      # black king square
        loser = bk if adv > 0 else wk
        md = abs((wk & 7) - (bk & 7)) + abs((wk >> 3) - (bk >> 3))  # king Manhattan
        # ``strong`` (bare-king mating) cranks the weights so the king-driving
        # gradient dominates the search instead of being washed out by the noise
        # of the extra winning material -- which is what made the engine shuffle
        # a winning K+Q(+B/P) vs K into a draw.
        cmd_w = self.MOPUP_CMD_WEIGHT
        king_w = self.MOPUP_KING_WEIGHT
        if strong:
            cmd_w = self.MOPUP_STRONG_CMD_WEIGHT
            king_w = self.MOPUP_STRONG_KING_WEIGHT
        bonus = cmd_w * self._center_manhattan[loser] + king_w * (14 - md)
        return bonus if adv > 0 else -bonus

    # ------------------------------------------------------------------ #
    # Material / contempt helpers (used by the draw-avoidance logic).
    # ------------------------------------------------------------------ #
    def _material_diff_stm(self, board):
        """Side-to-move material balance in centipawns (+ve = stm is ahead)."""
        occ_w = board.occupied_co[chess.WHITE]
        occ_b = board.occupied_co[chess.BLACK]

        def side(occ):
            return (100 * (board.pawns & occ).bit_count()
                    + 320 * (board.knights & occ).bit_count()
                    + 330 * (board.bishops & occ).bit_count()
                    + 500 * (board.rooks & occ).bit_count()
                    + 900 * (board.queens & occ).bit_count())

        diff = side(occ_w) - side(occ_b)
        return diff if board.turn == chess.WHITE else -diff

    def _draw_score(self, board):
        """Contempt-adjusted draw value from the side-to-move's perspective.

        Negative when the side to move is clearly ahead (so it steers away from
        repetitions / 50-move draws while winning) and positive when clearly
        behind (so it is happy to hold the draw). Zero when roughly balanced,
        matching a normal draw."""
        diff = self._material_diff_stm(board)
        if diff >= self.DRAW_AVOID_MARGIN:
            return -self.CONTEMPT
        if diff <= -self.DRAW_AVOID_MARGIN:
            return self.CONTEMPT
        return 0

    # ================================================================== #
    # Move ordering
    # ================================================================== #
    def order_moves(self, board, tt_move=None, ply=0, counter=None):
        """Legal moves ordered best-first: TT move, MVV-LVA captures /
        promotions, killers, then the history heuristic for quiet moves.

        FIX: ``board.gives_check`` is intentionally *not* used here -- it is
        expensive and was a major source of the ordering cost. Check detection
        is done once, cheaply, after the move is pushed in the search.
        """
        killers = self.killers.get(ply, [])
        color = board.turn
        scored = []
        # #9: C move generator (byte-identical order); None => in check, so use
        # python-chess's evasion order. Falls back to board.legal_moves when off.
        if self.use_c_movegen:
            moves = _c_legal_moves(board)
            if moves is None:
                moves = board.legal_moves
        else:
            moves = board.legal_moves
        for move in moves:
            score = 0
            if tt_move is not None and move == tt_move:
                score = 2_000_000
            elif board.is_capture(move):
                if board.is_en_passant(move):
                    victim_value = self.PIECE_VALUES[chess.PAWN]
                else:
                    # #2: piece_type_at returns the bare int (no Piece alloc);
                    # .get(None, 0) reproduces the old "if victim else 0".
                    victim_value = self.PIECE_VALUES.get(board.piece_type_at(move.to_square), 0)
                mover_value = self.PIECE_VALUES.get(board.piece_type_at(move.from_square), 0)
                score = 1_000_000 + victim_value * 16 - mover_value   # MVV-LVA
                if move.promotion:
                    score += self.PIECE_VALUES.get(move.promotion, 0)
                # SEE: demote captures that lose material below the killer /
                # counter-move band, so a losing capture is no longer tried
                # ahead of a quiet refutation. Only run SEE when the mover
                # outweighs the victim -- otherwise the exchange cannot lose
                # material (SEE >= 0) and the call would be wasted.
                elif self.use_see and mover_value > victim_value:
                    see = self._see(board, move)
                    if see < 0:
                        score = self.SEE_LOSING_CAPTURE + see
            elif move.promotion:
                score = 900_000 + self.PIECE_VALUES.get(move.promotion, 0)
            elif move in killers:
                score = 800_000 - killers.index(move)
            elif counter is not None and move == counter:
                score = 780_000             # counter-move heuristic (just below killers)
            else:
                score = self.history.get((color, move.from_square, move.to_square), 0)
            scored.append((score, move))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [move for _, move in scored]

    def _capture_moves(self, board):
        """Legal captures and promotions, ordered best-first (for quiescence).

        Uses python-chess's dedicated capture generator plus the (rare)
        non-capturing promotions, instead of generating *all* legal moves and
        discarding the quiet ones -- a real saving on the quiescence hot path,
        where most legal moves are quiet.

        Ordering is MVV-LVA, refined by SEE when ``use_qsee_order`` is on: a
        capture whose mover outweighs its victim (so the exchange *might* lose
        material) is run through SEE and, if it loses, demoted below every sound
        capture. That is exactly where plain MVV-LVA misleads (e.g. QxP onto a
        defended pawn looks like a pawn win but is a queen loss). SEE is computed
        only for that questionable subset -- clearly-winning captures
        (victim >= mover, SEE >= 0) never pay for it -- so the cost is bounded.
        Pure reordering: the value quiescence returns is unchanged (the loop's
        delta/SEE prunes are exact), only the node count moves.
        """
        # #9: C capture generator (byte-identical order to the python-chess
        # path below). Only reached when not in check, so no evasion handling.
        if self.use_c_movegen:
            moves = _c_capture_moves(board)
        else:
            own = board.occupied_co[board.turn]
            promo_rank = chess.BB_RANK_8 if board.turn == chess.WHITE else chess.BB_RANK_1
            moves = list(board.generate_legal_captures())       # incl. e.p. + capture-promos
            for m in board.generate_legal_moves(board.pawns & own, promo_rank):
                if not board.is_capture(m):                      # non-capturing promotions
                    moves.append(m)

        scored = []
        for move in moves:
            is_cap = board.is_capture(move)
            if board.is_en_passant(move):
                victim_value = self.PIECE_VALUES[chess.PAWN]
            elif is_cap:
                # #2: piece_type_at -> bare int, no Piece allocation.
                victim_value = self.PIECE_VALUES.get(board.piece_type_at(move.to_square), 0)
            else:
                victim_value = 0
            mover_value = self.PIECE_VALUES.get(board.piece_type_at(move.from_square), 0)
            promo = self.PIECE_VALUES.get(move.promotion, 0) if move.promotion else 0
            score = victim_value * 16 - mover_value + promo      # MVV-LVA base
            # SEE refinement for questionable captures only (see docstring).
            if (self.use_qsee_order and is_cap and not move.promotion
                    and self.use_see and mover_value > victim_value):
                see = self._see(board, move)
                if see < 0:                                      # losing -> sink it
                    score = self.SEE_LOSING_CAPTURE + see - 1_000_000
            scored.append((score, move))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [move for _, move in scored]

    # ------------------------------------------------------------------ #
    # Static Exchange Evaluation (SEE)
    # ------------------------------------------------------------------ #
    def _see(self, board, move):
        """Net material (centipawns) won by the capture ``move`` if both sides
        keep recapturing on its target square with their least-valuable
        attacker.

        Positive => the capture wins material; negative => it loses material
        even under the optimal recapture sequence. Each side may stand pat at
        any point (the ``-max`` fold below), so a capture is never scored worse
        than its best stopping point. X-ray attackers behind a removed piece are
        picked up automatically because the attacker set is recomputed against
        the shrinking occupancy after every capture.
        """
        to_sq = move.to_square
        from_sq = move.from_square
        values = self.PIECE_VALUES

        if board.is_en_passant(move):
            target_value = values[chess.PAWN]
            ep_sq = to_sq + (-8 if board.turn == chess.WHITE else 8)
        else:
            # #2: piece_type_at -> bare int (no Piece alloc); None => not a capture.
            victim_pt = board.piece_type_at(to_sq)
            if victim_pt is None:
                return 0                       # not a capture
            target_value = values[victim_pt]
            ep_sq = None

        attacker_pt = board.piece_type_at(from_sq)
        if attacker_pt is None:
            return 0
        attacker_value = values[attacker_pt]

        # Occupancy with the initial attacker (and any e.p. victim) removed.
        occupied = board.occupied & ~chess.BB_SQUARES[from_sq]
        if ep_sq is not None:
            occupied &= ~chess.BB_SQUARES[ep_sq]

        side = not board.turn                  # side to recapture next
        attackers = self._see_attackers(board, to_sq, occupied)

        gain = [0] * 32
        gain[0] = target_value
        d = 0
        while True:
            d += 1
            gain[d] = attacker_value - gain[d - 1]
            side_attackers = attackers & board.occupied_co[side] & occupied
            if not side_attackers:
                break
            lva_sq, attacker_value = self._least_valuable_attacker(board, side_attackers)
            occupied &= ~chess.BB_SQUARES[lva_sq]
            attackers = self._see_attackers(board, to_sq, occupied)
            side = not side
            if d >= 31:                        # safety: never overflow gain[]
                break

        # Fold the gain list back to the root capture, honouring each side's
        # option to stop capturing rather than continue into a losing exchange.
        while d > 1:
            d -= 1
            gain[d - 1] = -max(-gain[d - 1], gain[d])
        return gain[0]

    def _see_attackers(self, board, square, occupied):
        """Bitboard of every piece (either colour) in ``occupied`` that attacks
        ``square``. Recomputed against a shrinking occupancy inside the SEE swap
        loop so x-ray (battery) attackers appear naturally as front pieces are
        removed."""
        bishops_queens = board.bishops | board.queens
        rooks_queens = board.rooks | board.queens
        diag = chess.BB_DIAG_ATTACKS[square][chess.BB_DIAG_MASKS[square] & occupied]
        rank = chess.BB_RANK_ATTACKS[square][chess.BB_RANK_MASKS[square] & occupied]
        file_ = chess.BB_FILE_ATTACKS[square][chess.BB_FILE_MASKS[square] & occupied]
        attackers = (
            (chess.BB_KNIGHT_ATTACKS[square] & board.knights)
            | (chess.BB_KING_ATTACKS[square] & board.kings)
            | (chess.BB_PAWN_ATTACKS[chess.BLACK][square]
               & board.pawns & board.occupied_co[chess.WHITE])
            | (chess.BB_PAWN_ATTACKS[chess.WHITE][square]
               & board.pawns & board.occupied_co[chess.BLACK])
            | (diag & bishops_queens)
            | ((rank | file_) & rooks_queens)
        )
        return attackers & occupied

    def _recapture_at(self, board, move, is_capture, last_cap_sq):
        """True if ``move`` recaptures on the square the parent just captured on
        *and* the configured ``recapture_ext`` mode admits it (see __init__)."""
        if not is_capture or last_cap_sq is None or move.to_square != last_cap_sq:
            return False
        mode = self.recapture_ext
        if mode == "off":
            return False
        if mode == "see":
            return self._see(board, move) >= 0
        return True                                   # "all"

    def _least_valuable_attacker(self, board, attackers):
        """(square, value) of the cheapest piece in ``attackers`` (the caller
        has already masked it down to a single colour)."""
        for piece_type, bb in (
            (chess.PAWN, board.pawns), (chess.KNIGHT, board.knights),
            (chess.BISHOP, board.bishops), (chess.ROOK, board.rooks),
            (chess.QUEEN, board.queens), (chess.KING, board.kings),
        ):
            subset = attackers & bb
            if subset:
                return chess.lsb(subset), self.PIECE_VALUES[piece_type]
        return None, 0

    def _store_killer(self, move, ply):
        slot = self.killers.setdefault(ply, [])
        if move in slot:
            return
        slot.insert(0, move)
        del slot[2:]

    def _update_history(self, color, move, bonus):
        """Nudge the (color, from, to) history score by ``bonus`` (which may be
        negative for a malus), damped toward zero so it can never run away.

        Gravity: ``new = old + bonus - old*|bonus|/HISTORY_MAX``. Near zero this
        is just ``old + bonus``; as ``|old|`` approaches HISTORY_MAX the pull
        back toward zero cancels the bonus, bounding the score to that range."""
        key = (color, move.from_square, move.to_square)
        old = self.history.get(key, 0)
        self.history[key] = old + bonus - old * abs(bonus) // self.HISTORY_MAX

    # ================================================================== #
    # Iterative-deepening driver
    # ================================================================== #
    def get_best_move(self, board, depth):
        """Best move via iterative deepening to a fixed ``depth`` (no clock)."""
        return self._search(board, max_depth=max(1, depth), time_limit=None)

    def get_best_move_timed(self, board, time_limit, max_depth=10):
        """Best move via iterative deepening bounded by ``time_limit`` seconds."""
        return self._search(board, max_depth=max_depth, time_limit=time_limit)

    def _search(self, board, max_depth, time_limit):
        # Reset per-move statistics and tables.
        self.nodes = 0
        # Transposition table persists ACROSS moves so the previous move's tree
        # (the opponent usually plays an expected reply) is reused -- a sizable
        # speedup in the middlegame/endgame. It is dropped only after an
        # irreversible move: halfmove_clock == 0 means the last move was a pawn
        # move or capture, so no earlier position can ever recur and those
        # entries are dead. A size cap bounds memory. Mate scores are stored
        # position-relative (see _tt_value_to/from), so they stay valid across
        # searches. (killers/history/countermoves are still reset each move.)
        if board.halfmove_clock == 0 or len(self.tt) > self.TT_MAX_ENTRIES:
            self.tt = {}
        # New search generation: entries written by earlier moves now count as
        # "old" and become freely replaceable under depth-preferred replacement.
        self._tt_gen += 1
        self.killers = {}
        self.history = {}
        self.countermoves = {}
        self.start_time = time.time()
        self.time_limit = time_limit
        self.search_log = []

        # Repetition tracking: count every position that has occurred from the
        # start of the game up to the root, so re-reaching any of them during
        # the search is detected as a repetition and scored as a draw (with
        # contempt -- see _draw_score). This is what lets a winning engine steer
        # away from threefold repetitions instead of shuffling into one.
        self._path = {}
        hist = board.copy()
        k = hist._transposition_key()
        self._path[k] = self._path.get(k, 0) + 1
        while hist.move_stack:
            hist.pop()
            k = hist._transposition_key()
            self._path[k] = self._path.get(k, 0) + 1

        root = board.copy()
        root_turn = root.turn
        legal = list(root.legal_moves)
        if not legal:
            self.nodes_searched = 0
            self.last_score = 0
            self.last_depth = 0
            return None

        # --- Opening book: play instantly if the position is in the book --- #
        book = self._book_move(root)
        if book is not None:
            self.nodes_searched = 0
            self.last_score = 0
            self.last_depth = 0
            record = {"depth": 0, "move": book.uci(), "score": 0,
                      "nodes": 0, "time_ms": 0, "book": True}
            self.search_log.append(record)
            if self.on_depth is not None:
                self.on_depth(record)
            final = dict(record)
            final["final"] = True
            if self.on_final is not None:
                self.on_final(final)
            return book

        # --- #4 Endgame tablebase: play the provably-optimal move instantly --
        # Bound the network wait so a tablebase MISS can never overrun the
        # move's time budget (a hit returns almost instantly anyway): wait at
        # most half the remaining clock when timed, else the full tb_timeout.
        tb_to = self.tb_timeout
        if time_limit is not None:
            elapsed = time.time() - self.start_time
            tb_to = min(tb_to, max(0.0, (time_limit - elapsed) * 0.5))
        tb = self._tb_probe(root, tb_to)
        if tb is not None:
            wdl, tb_move = tb                  # tb_move already verified legal in _tb_probe
            score_white = (wdl if root_turn == chess.WHITE else -wdl) * self.TB_SCORE_UNIT
            self.nodes_searched = 0
            self.last_score = score_white
            self.last_depth = 0
            record = {"depth": 0, "move": tb_move.uci(), "score": score_white,
                      "nodes": 0, "time_ms": 0, "tb": True, "wdl": wdl}
            self.search_log.append(record)
            if self.on_depth is not None:
                self.on_depth(record)
            final = dict(record)
            final["final"] = True
            if self.on_final is not None:
                self.on_final(final)
            return tb_move

        best_move = legal[0]
        best_score_white = 0
        reached_depth = 0
        pv_move = None
        prev_score = None

        # Seed the incremental-eval accumulator for the root and arm it; every
        # node from here maintains it via _make/_unmake. _search_root re-anchors
        # to _root_acc each iteration. Disarmed after the loop (below) so
        # external evaluate_position() calls fall back to the from-scratch scan.
        # _TimeUp is caught inside the loop, so the disarm always runs.
        self._root_acc = self._compute_acc(root)
        self._acc = self._root_acc
        self._acc_stack = []
        self._acc_valid = True

        for depth in range(1, max_depth + 1):
            self._partial_root_move = None   # clear partial result for this depth
            try:
                score, move = self._search_root_aspiration(
                    root, depth, pv_move, prev_score)
            except _TimeUp:
                # Use the best root move found so far in the incomplete iteration
                # if any root moves were fully evaluated before time ran out.
                # Rationale: the PV move is always searched first, so either
                # (a) it's still best -> same move, no change, or (b) something
                # better was found -> take it. Only fall back to the previous
                # depth's move when no root move was completed at all this depth.
                if self._partial_root_move is not None:
                    best_move = self._partial_root_move
                break

            if move is not None:
                best_move = move
                pv_move = move
                reached_depth = depth
                prev_score = score
                best_score_white = score if root_turn == chess.WHITE else -score

                pv = self._extract_pv(root, best_move, depth)
                self.last_pv = pv
                record = {
                    "depth": depth,
                    "move": best_move.uci(),
                    "score": best_score_white,
                    "nodes": self.nodes,
                    "time_ms": int((time.time() - self.start_time) * 1000),
                    "pv": pv,
                }
                self.search_log.append(record)
                if self.on_depth is not None:
                    self.on_depth(record)

            if abs(score) > self.MATE_THRESHOLD:
                break       # forced mate found
            if time_limit is not None and (time.time() - self.start_time) >= time_limit:
                break

        # Disarm the accumulator: outside the search, evaluate_position() must
        # use the from-scratch scan (the live acc is only valid mid-search).
        self._acc_valid = False

        self.nodes_searched = self.nodes
        self.last_score = best_score_white
        self.last_depth = reached_depth

        final_record = {
            "depth": reached_depth,
            "move": best_move.uci() if best_move is not None else "----",
            "score": best_score_white,
            "nodes": self.nodes,
            "time_ms": int((time.time() - self.start_time) * 1000),
            "pv": self.last_pv,
            "final": True,
        }
        if self.on_final is not None:
            self.on_final(final_record)
        return best_move

    def _extract_pv(self, board, first_move, max_len):
        """Reconstruct the principal variation (the line behind ``first_move``)
        by walking best-moves out of the transposition table, as a string like
        ``Nf3 Nc6 Bb5 a6`` (SAN) or ``g1f3 b8c6 ...`` (UCI, when ``pv_uci`` is
        set). Cheap (once per completed depth) and best-effort -- it stops when
        the TT has no entry, the move is stale, or a position repeats."""
        b = board.copy()
        out = []
        seen = set()
        mv = first_move
        while mv is not None and len(out) < max_len:
            if mv not in b.legal_moves:
                break
            try:
                out.append(mv.uci() if self.pv_uci else b.san(mv))
            except Exception:
                break
            b.push(mv)
            key = b._transposition_key()
            if key in seen:                  # repetition -> stop the walk
                break
            seen.add(key)
            entry = self._tt_get(key)
            mv = entry[3] if entry is not None else None
        return " ".join(out)

    # ------------------------------------------------------------------ #
    # Aspiration-window wrapper around the root search.
    # ------------------------------------------------------------------ #
    def _search_root_aspiration(self, board, depth, pv_move, prev_score):
        """Search the root with an aspiration window for deeper iterations.

        FIX: real aspiration windows (absent before). They narrow the window
        around the previous score and re-search only on fail-high/low, which
        is a net win once move ordering is good. We widen geometrically and
        fall back to a full window so we never loop forever.
        """
        if (depth < self.ASPIRATION_MIN_DEPTH or prev_score is None
                or abs(prev_score) >= self.MATE_THRESHOLD):
            return self._search_root(board, depth, pv_move, -self.INF, self.INF)

        delta = self.ASPIRATION_DELTA
        alpha = prev_score - delta
        beta = prev_score + delta
        while True:
            score, move = self._search_root(board, depth, pv_move, alpha, beta)
            if score <= alpha:                       # fail low: widen downward
                alpha = max(-self.INF, score - delta)
            elif score >= beta:                      # fail high: widen upward
                beta = min(self.INF, score + delta)
            else:
                return score, move
            delta *= 2
            if delta >= 2 * self.ASPIRATION_DELTA * 32:   # give up -> full window
                return self._search_root(board, depth, pv_move, -self.INF, self.INF)

    # ------------------------------------------------------------------ #
    # Root search: PVS over root moves with a random tiebreak among the
    # moves within TIEBREAK_MARGIN of the best.
    # ------------------------------------------------------------------ #
    def _search_root(self, board, depth, pv_move, alpha, beta):
        best_value = -self.INF
        best_move = None
        # (value, move, exact): `exact` is True only when `value` is a real
        # score, not a PVS scout upper bound. See the tiebreak note below.
        results = []
        a = alpha
        first = True

        # Each iteration / aspiration re-search starts from the root: re-anchor
        # the incremental accumulator so it can't drift across iterations.
        self._acc = self._root_acc
        self._acc_stack = []

        for move in self.order_moves(board, pv_move, 0):
            is_capture = board.is_capture(move)
            self._make(board, move)
            child_last_cap = move.to_square if is_capture else None

            if first:
                value = -self._negamax(board, depth - 1, -beta, -a, 1,
                                       self.MAX_EXTENSIONS, child_last_cap)
                exact = True                 # full-window search -> exact score
            else:
                # PVS: scout with a null window, re-search on a fail-high.
                value = -self._negamax(board, depth - 1, -a - 1, -a, 1,
                                       self.MAX_EXTENSIONS, child_last_cap)
                if a < value < beta:
                    value = -self._negamax(board, depth - 1, -beta, -a, 1,
                                           self.MAX_EXTENSIONS, child_last_cap)
                    exact = True             # re-searched full window -> exact
                else:
                    # Scout fail-low: `value` is only an upper bound (<= a).
                    # The true score may be far lower, so it must NOT be
                    # trusted for the tiebreak.
                    exact = False
            self._unmake(board)

            results.append((value, move, exact))
            if value > best_value:
                best_value = value
                best_move = move
            self._partial_root_move = best_move   # track best seen so far this depth
            if value > a:
                a = value
            first = False
            if a >= beta:               # fail-high (aspiration re-search upstream)
                break

        # Random tiebreak among moves whose EXACT score is within the margin of
        # the best. Fail-low scout moves are excluded (their score is only an
        # upper bound), so a measurably worse move can never be chosen. This was
        # the bug behind weak picks like f2f3/h2h3: scout upper bounds landed
        # inside the margin and were wrongly treated as ties.
        if best_move is not None and best_value < self.MATE_THRESHOLD:
            near = [m for v, m, ex in results
                    if ex and v >= best_value - self.TIEBREAK_MARGIN]
            if len(near) > 1:
                best_move = random.choice(near)
        return best_value, best_move

    # ================================================================== #
    # Negamax with alpha-beta, TT, PVS, pruning and quiescence
    # ================================================================== #
    def _negamax(self, board, depth, alpha, beta, ply, ext_budget, last_cap_sq,
                 prev_move=None, chk_budget=None):
        if chk_budget is None:
            chk_budget = self.MAX_CHECK_EXT
        self.nodes += 1
        self._check_time()

        if ply >= self.MAX_PLY:
            return self._evaluate_stm(board)

        # Cheap draw detection.
        if board.is_insufficient_material() or board.halfmove_clock >= 100:
            return 0

        alpha_orig = alpha
        # FIX: cheap internal position key instead of zobrist_hash (~22x faster).
        key = board._transposition_key()

        # Repetition: this exact position (same pieces, rights, side to move)
        # already occurred earlier on this line or in the game history. Treat
        # the first repetition as a draw -- contempt-scored so a winning side
        # avoids it and a losing side seeks it.
        if self._path.get(key):
            return self._draw_score(board)

        # --- Transposition-table probe --------------------------------- #
        tt_entry = self._tt_get(key)
        tt_move = None
        tt_eval = None                # cached static eval from a prior visit
        if tt_entry is not None:
            tt_depth, tt_flag, tt_value, tt_move, tt_eval, _tt_entry_gen = tt_entry
            if tt_depth >= depth:
                value = self._tt_value_from(tt_value, ply)
                if tt_flag == TT_EXACT:
                    return value
                if tt_flag == TT_LOWER and value > alpha:
                    alpha = value
                elif tt_flag == TT_UPPER and value < beta:
                    beta = value
                if alpha >= beta:
                    return value

        # --- Leaf: quiescence search ----------------------------------- #
        if depth <= 0:
            return self._quiescence(board, alpha, beta, ply)

        in_check = board.is_check()
        is_pv = (beta - alpha) > 1            # wide window => PV node

        # Static eval is needed for the pruning heuristics below; compute it
        # lazily. Skip it while in check (meaningless there) AND at PV nodes:
        # its only consumers -- reverse-futility, null-move and futility pruning
        # (plus the all-pruned fallback) -- are every one gated on `not is_pv`,
        # so at a PV node the full positional eval is computed and never read.
        # Skipping it there removes that wasted work and changes nothing about
        # the search (same nodes, scores and move). `lazy_pv_eval` lets the
        # benchmark A/B the eval-call count on identical code.
        static_eval = None
        if not in_check and not (self.lazy_pv_eval and is_pv):
            if self.tt_cached_eval and tt_eval is not None:
                static_eval = tt_eval                 # reuse: identical to recompute
            else:
                static_eval = self._evaluate_stm(board)

        # --- Reverse futility / static null-move pruning --------------- #
        # If we are already far above beta on the static score at a shallow
        # depth, assume the position holds and prune.
        if (not is_pv and not in_check and depth <= 4
                and abs(beta) < self.MATE_THRESHOLD
                and static_eval - self.RFP_MARGIN * depth >= beta):
            return static_eval

        # --- Null-move pruning ----------------------------------------- #
        if (depth >= 3 and not in_check and not is_pv
                and static_eval >= beta
                and self._has_non_pawn_material(board, board.turn)
                and beta < self.MATE_THRESHOLD):
            r = self.NULL_MOVE_R + (depth // 6)
            self._make_null(board)
            null_score = -self._negamax(board, depth - 1 - r, -beta, -beta + 1,
                                        ply + 1, ext_budget, None, None, chk_budget)
            self._unmake(board)
            if null_score >= beta:
                return beta            # fail-hard; don't return false mates

        # --- Frontier futility pruning flag ---------------------------- #
        futile = (not is_pv and not in_check and depth in self.FUTILITY_MARGIN
                  and abs(alpha) < self.MATE_THRESHOLD
                  and static_eval + self.FUTILITY_MARGIN[depth] <= alpha)

        # IIR: flying blind at full depth wastes nodes — reduce by 1 when we
        # have no TT move to guide ordering.
        if depth >= 4 and tt_move is None and not in_check:
            depth -= 1

        # Counter-move heuristic: the quiet move that last refuted this exact
        # predecessor move is tried early (it often refutes it again).
        counter = None
        if prev_move is not None:
            counter = self.countermoves.get((prev_move.from_square, prev_move.to_square))
        moves = self.order_moves(board, tt_move, ply, counter)
        if not moves:
            return -self.MATE_SCORE + ply if in_check else 0

        # One-reply extension: in a forced line where only a single legal move
        # exists, search it one ply deeper (bounded by ext_budget). This keeps
        # the engine from cutting off forcing endgame mating sequences early.
        single_reply = len(moves) == 1

        best_value = -self.INF
        best_move = None
        move_index = 0
        # Quiet moves actually searched at this node, in order. On a quiet
        # beta-cutoff the last one is the refutation (it gets the history bonus)
        # and the earlier ones get the history malus (see use_history_malus).
        searched_quiets = []
        color = board.turn

        # Mark this position as on the current path so a deeper transposition
        # back to it is seen as a repetition. Restored after the loop.
        self._path[key] = self._path.get(key, 0) + 1
        for move in moves:
            is_capture = board.is_capture(move)
            is_quiet = not is_capture and not move.promotion

            # Futility: at a frontier node, skip quiet moves that cannot raise
            # alpha (always keep at least the first move so we have a score).
            if futile and is_quiet and move_index > 0:
                move_index += 1
                continue

            # Late-move pruning: once enough quiet moves have been searched at a
            # shallow non-PV node without improving alpha, give up on the rest.
            # `searched_quiets` holds the quiets already searched (captures/
            # promos all sort first, so once we reach a quiet the remaining list
            # is all quiet -> a plain break drops exactly the late quiet tail).
            if (self.use_lmp and is_quiet and not is_pv and not in_check
                    and depth <= self.LMP_MAX_DEPTH
                    and abs(alpha) < self.MATE_THRESHOLD
                    and len(searched_quiets) >= self.LMP_COUNT[depth]):
                break

            # --- Extensions (single-reply / recapture / passed-pawn; check below) -- #
            # Non-check extensions draw on ext_budget; the check extension draws
            # on its own chk_budget so the two never starve each other.
            extension = 0
            is_check_ext = False
            if ext_budget > 0:
                if single_reply:
                    extension = 1
                elif (self._recapture_at(board, move, is_capture, last_cap_sq)):
                    extension = 1            # capture-sequence (recapture) extension
                elif self._is_passed_pawn_push(board, move):
                    extension = 1

            self._make(board, move)
            gives_check = board.is_check()
            if extension == 0 and gives_check and chk_budget > 0:
                extension = 1            # check extension (own budget, cheap post-push test)
                is_check_ext = True
            child_last_cap = move.to_square if is_capture else None
            new_depth = depth - 1 + extension
            child_ext = ext_budget - (0 if is_check_ext else extension)
            child_chk = chk_budget - (extension if is_check_ext else 0)

            # --- Late-move reductions for quiet, late, non-checking moves -- #
            reduction = 0
            if (depth >= 3 and move_index >= self.LMR_MIN_MOVE
                    and extension == 0 and is_quiet
                    and not in_check and not gives_check):
                if self.lmr_aggressive:
                    # log(depth)*log(move_index) reduction: small for early/
                    # shallow moves, growing for late moves at high depth.
                    reduction = self._lmr_table[min(depth, 63)][min(move_index, 63)]
                    if is_pv and reduction > 0:
                        reduction -= 1        # reduce less on PV nodes
                    # Never reduce a move into (or past) quiescence: keep at
                    # least one ply of real search so the scout is meaningful.
                    if reduction > new_depth - 1:
                        reduction = max(0, new_depth - 1)
                else:
                    reduction = 1
                    if move_index >= 6 and depth >= 6:
                        reduction = 2
                    if is_pv and reduction > 0:
                        reduction -= 1        # reduce less on PV nodes

            if move_index == 0:
                value = -self._negamax(board, new_depth, -beta, -alpha,
                                       ply + 1, child_ext, child_last_cap, move, child_chk)
            else:
                # PVS scout (optionally reduced), then re-search if it surprises.
                value = -self._negamax(board, new_depth - reduction, -alpha - 1, -alpha,
                                       ply + 1, child_ext, child_last_cap, move, child_chk)
                if reduction and value > alpha:
                    value = -self._negamax(board, new_depth, -alpha - 1, -alpha,
                                           ply + 1, child_ext, child_last_cap, move, child_chk)
                if alpha < value < beta:
                    value = -self._negamax(board, new_depth, -beta, -alpha,
                                           ply + 1, child_ext, child_last_cap, move, child_chk)
            self._unmake(board)
            if is_quiet:
                searched_quiets.append(move)

            if value > best_value:
                best_value = value
                best_move = move
            if value > alpha:
                alpha = value
            if alpha >= beta:
                if is_quiet:
                    self._store_killer(move, ply)
                    bonus = depth * depth
                    self._update_history(color, move, bonus)         # reward refutation
                    # Malus: every quiet searched earlier here failed to cut.
                    if self.use_history_malus:
                        for q in searched_quiets[:-1]:
                            self._update_history(color, q, -bonus)
                    if prev_move is not None:        # record refutation as counter-move
                        self.countermoves[(prev_move.from_square, prev_move.to_square)] = move
                break
            move_index += 1

        # Leaving this node: restore the repetition path counter.
        self._path[key] -= 1

        # Everything was futility-pruned except (possibly) nothing meaningful:
        # fall back to a static score so we never return -INF.
        if best_move is None:
            return static_eval if static_eval is not None else self._evaluate_stm(board)

        # --- Store in the transposition table -------------------------- #
        if best_value <= alpha_orig:
            flag = TT_UPPER
        elif best_value >= beta:
            flag = TT_LOWER
        else:
            flag = TT_EXACT
        # Cache static_eval (may be None at in-check / PV-skipped nodes; a later
        # visit that needs it will recompute when the cached slot is None).
        self._tt_store(key, (depth, flag, self._tt_value_to(best_value, ply),
                             best_move, static_eval, self._tt_gen))
        return best_value

    def _is_passed_pawn_push(self, board, move):
        """True if ``move`` advances a (5th-rank-or-beyond) passed pawn."""
        piece = board.piece_at(move.from_square)
        if piece is None or piece.piece_type != chess.PAWN:
            return False
        rank = chess.square_rank(move.to_square)
        advanced = rank >= 4 if piece.color == chess.WHITE else rank <= 3
        if not advanced:
            return False
        return self._is_passed_pawn(board, move.to_square, piece.color)

    # ------------------------------------------------------------------ #
    # Quiescence search: stand-pat + delta pruning over noisy moves.
    # ------------------------------------------------------------------ #
    def _quiescence(self, board, alpha, beta, ply):
        self.nodes += 1
        self._check_time()

        if ply >= self.MAX_PLY:
            return self._evaluate_stm(board)
        if board.is_insufficient_material():
            return 0

        in_check = board.is_check()

        if in_check:
            # Must consider every evasion (else we could stand-pat out of mate).
            moves = self.order_moves(board, None, ply)
            if not moves:
                return -self.MATE_SCORE + ply        # checkmate
            best = -self.INF
            for move in moves:
                self._make(board, move)
                score = -self._quiescence(board, -beta, -alpha, ply + 1)
                self._unmake(board)
                if score > best:
                    best = score
                if score > alpha:
                    alpha = score
                if alpha >= beta:
                    return beta
            return best

        # Stand-pat: we can decline to capture and keep the static score.
        # Lazy: skips the expensive positional terms when the cheap base already
        # proves a >= beta cutoff (exact, so the search is unchanged).
        stand_pat = self._qs_stand_pat(board, beta)
        if stand_pat >= beta:
            return beta
        # Delta pruning: if even the biggest swing can't reach alpha, give up.
        if stand_pat + self.PIECE_VALUES[chess.QUEEN] + self.DELTA_MARGIN < alpha:
            return alpha
        if stand_pat > alpha:
            alpha = stand_pat

        for move in self._capture_moves(board):
            # Per-move delta + SEE pruning on the captured material.
            if not move.promotion:
                if board.is_en_passant(move):
                    victim_value = self.PIECE_VALUES[chess.PAWN]
                else:
                    # #2: piece_type_at -> bare int (quiescence is ~50% of nodes,
                    # so this is the hottest of the piece_at->piece_type_at spots).
                    victim_value = self.PIECE_VALUES.get(board.piece_type_at(move.to_square), 0)
                if stand_pat + victim_value + self.DELTA_MARGIN < alpha:
                    continue
                # SEE pruning: drop captures that lose material outright. Only
                # worth checking when the mover outweighs the victim (else SEE
                # >= 0). This branch is never reached while in check -- the
                # in-check evasion search above returns before this loop.
                mover_value = self.PIECE_VALUES.get(board.piece_type_at(move.from_square), 0)
                if (self.use_see and mover_value > victim_value
                        and self._see(board, move) < 0):
                    continue
            self._make(board, move)
            score = -self._quiescence(board, -beta, -alpha, ply + 1)
            self._unmake(board)
            if score >= beta:
                return beta
            if score > alpha:
                alpha = score
        return alpha

    # ================================================================== #
    # Helpers
    # ================================================================== #
    def _has_non_pawn_material(self, board, color):
        for piece_type in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN):
            if board.pieces(piece_type, color):
                return True
        return False

    def _check_time(self):
        """Abort the search (via exception) once the time budget is used up."""
        if self.time_limit is None:
            return
        if (self.nodes & 1023) == 0 and (time.time() - self.start_time) >= self.time_limit:
            raise _TimeUp()

    # Mate scores are stored relative to the current ply so a mate found at a
    # different depth keeps a consistent distance when reused from the TT.
    def _tt_value_to(self, value, ply):
        if value > self.MATE_THRESHOLD:
            return value + ply
        if value < -self.MATE_THRESHOLD:
            return value - ply
        return value

    def _tt_value_from(self, value, ply):
        if value > self.MATE_THRESHOLD:
            return value - ply
        if value < -self.MATE_THRESHOLD:
            return value + ply
        return value

    # ------------------------------------------------------------------ #
    # Transposition-table access -- hides the one-slot vs two-tier format.
    # An entry is the 6-tuple (depth, flag, value, move, static_eval, gen).
    # ------------------------------------------------------------------ #
    def _tt_get(self, key):
        """Best (deepest) stored entry for ``key``, or None.

        With two-tier storage the slot holds (deep, fresh); the deep slot is the
        deeper of the two by construction, so it is the right one for both the
        cutoff and the move/eval hint. Returns a single 6-tuple either way."""
        slot = self.tt.get(key)
        if slot is None:
            return None
        if not self.use_tt_two_tier:
            return slot                       # single-entry format
        deep, fresh = slot
        if deep is None:
            return fresh
        if fresh is None:
            return deep
        return deep if deep[0] >= fresh[0] else fresh

    def _tt_store(self, key, entry):
        """Insert ``entry`` (a 6-tuple) under the active replacement policy."""
        if self.use_tt_two_tier:
            slot = self.tt.get(key)
            if slot is None:
                self.tt[key] = (entry, None)
                return
            deep, _fresh = slot
            # New entry takes the depth slot when the slot is empty, holds a
            # leftover from an earlier search, or is at least as deep -- the
            # displaced deep entry drops into the always-replace slot. Otherwise
            # the new (shallower) entry just refreshes the always-replace slot.
            if deep is None or deep[5] != entry[5] or entry[0] >= deep[0]:
                self.tt[key] = (entry, deep)
            else:
                self.tt[key] = (deep, entry)
            return
        if self.use_tt_depth_replace:
            old = self.tt.get(key)
            if (old is None or old[5] != self._tt_gen
                    or entry[0] >= old[0] or entry[1] == TT_EXACT):
                self.tt[key] = entry
            return
        self.tt[key] = entry                  # original always-replace scheme
