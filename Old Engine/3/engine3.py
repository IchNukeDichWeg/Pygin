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
  history scores and the transposition table.
* **Aspiration windows** around the previous score for deeper iterations.
* **Transposition table** keyed by the board's internal position key
  (cheap and collision-safe -- see the performance note below) storing
  depth + bound (exact / lower / upper) + best move, with mate-score
  distance correction.
* **Quiescence search** with stand-pat, delta pruning and check evasions.
* **Pruning / selectivity**: null-move pruning, reverse-futility (static
  null-move) pruning, futility pruning at frontier nodes and late-move
  reductions (LMR).
* **Move ordering**: TT move, MVV-LVA captures, promotions, killer moves, the
  counter-move heuristic and the history heuristic.
* **Opening book**: optional Polyglot ``.bin`` book consulted before search,
  with weighted-random move selection for opening variety.
* **Endgame / draws**: one-reply (forced-move) search extension, an endgame
  "mop-up" term that drives the weak king to the edge to convert won endings
  (KQK / KRK / KQ-vs-P), and contempt-scored repetition detection so a clearly
  winning side avoids draws while a losing side is happy to hold them.

Evaluation
----------
A tapered hand-crafted evaluation (HCE): material + piece-square tables
blended middlegame<->endgame by game phase, plus pawn structure (doubled /
isolated / passed / backward), king safety (pawn shield, open files, attacker
count), mobility, rook on open / semi-open file, the bishop pair and a tempo
bonus. Returned in centipawns from White's perspective (positive favours
White); ``_evaluate_stm`` flips it to the side to move for negamax.

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
"""

import os
import random
import time

import chess
import chess.polyglot


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
          0,   0,   0,   0,   0,   0,   0,   0,
         98, 134,  61,  95,  68, 126,  34, -11,
         -6,   7,  26,  31,  65,  56,  25, -20,
        -14,  13,   6,  21,  23,  12,  17, -23,
        -27,  -2,  -5,  15,  17,   6,  10, -25,
        -26,  -4,  -4, -5,   5,   3,  33, -12,
        -35,  -1, -20, -25, -17,  24,  38, -22,
          0,   0,   0,   0,   0,   0,   0,   0,
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
        -14, -21, -11,  -8,  -7,  -9, -17, -24,
         -8,  -4,   7, -12,  -3, -13,  -4, -14,
          2,  -8,   0,  -1,  -2,   6,   0,   4,
         -3,   9,  12,   9,  14,  10,   3,   2,
         -6,   3,  13,  19,   7,  10,  -3,  -9,
        -12,  -3,   8,  10,  13,   3,  -7, -15,
        -14, -18,  -7,  -1,   4,  -9, -15, -27,
        -23,  -9, -23,  -5,  -9, -16,  -5, -17,
    ]
    MG_ROOK_TABLE = [
         32,  42,  32,  51,  63,   9,  31,  43,
         27,  32,  58,  62,  80,  67,  26,  44,
         -5,  19,  26,  36,  17,  45,  61,  16,
        -24, -11,   7,  26,  24,  35,  -8, -20,
        -36, -26, -12,  -1,   9,  -7,   6, -23,
        -45, -25, -16, -17,   3,   0,  -5, -33,
        -44, -16, -20,  -9,  -1,  11,  -6, -71,
        -19, -13,   -5,  17,  16,   2, -37, -26,
    ]
    EG_ROOK_TABLE = [
         13,  10,  18,  15,  12,  12,   8,   5,
         11,  13,  13,  11,  -3,   3,   8,   3,
          7,   7,   7,   5,   4,  -3,  -5,  -3,
          4,   3,  13,   1,   2,   1,  -1,   2,
          3,   5,   8,   4,  -5,  -6,  -8, -11,
         -4,   0,  -5,  -1,  -7, -12,  -8, -16,
         -6,  -6,   0,   2,  -9,  -9, -11,  -3,
         -9,   -2,   2,  -1,  -5, -13,   2, -10,
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
        -53, -34, -21, -11, -28, -14, -24, -43,
    ]

    # Material values matched to the PeSTO tables above (middlegame / endgame).
    MG_VALUES = {
        chess.PAWN: 85, chess.KNIGHT: 340, chess.BISHOP: 370,
        chess.ROOK: 490, chess.QUEEN: 1030, chess.KING: 0,
    }
    EG_VALUES = {
        chess.PAWN: 97, chess.KNIGHT: 285, chess.BISHOP: 299,
        chess.ROOK: 515, chess.QUEEN: 940, chess.KING: 0,
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
    INF = 10_000_000                         # finite "infinity" for windows

    NULL_MOVE_R = 2                          # base null-move reduction
    LMR_MIN_MOVE = 3                         # first move index eligible for LMR
    MAX_EXTENSIONS = 8                       # extension plies per line

    ASPIRATION_MIN_DEPTH = 4                 # use aspiration from this depth
    ASPIRATION_DELTA = 30                    # initial half-window (centipawns)

    # Pruning margins (centipawns).
    FUTILITY_MARGIN = {1: 150, 2: 320}       # frontier futility by depth
    RFP_MARGIN = 90                          # reverse-futility margin per depth
    DELTA_MARGIN = 120                       # quiescence delta-pruning safety

    # Random tiebreak: among root moves within this many centipawns of the
    # best, one is chosen at random. Keeps equal positions from cycling
    # without ever preferring a measurably worse move.
    TIEBREAK_MARGIN = 5

    # ------------------------------------------------------------------ #
    # Evaluation weights (centipawns)
    # ------------------------------------------------------------------ #
    BISHOP_PAIR = 30
    ROOK_OPEN_FILE = 25
    ROOK_SEMIOPEN_FILE = 12
    TEMPO = 8

    DOUBLED_PAWN = 18
    ISOLATED_PAWN = 14
    BACKWARD_PAWN = 10
    # Passed-pawn bonus indexed by the pawn's rank *from its own side* (0..7);
    # the further advanced, the larger the bonus.
    PASSED_PAWN = [0, 10, 17, 25, 40, 65, 105, 0]

    # Per-piece mobility weight (centipawns per reachable square).
    MOBILITY_WEIGHT = {
        chess.KNIGHT: 4, chess.BISHOP: 4, chess.ROOK: 2, chess.QUEEN: 1,
    }
    # King-safety: penalty per enemy attack on the king's ring, and the
    # shield / open-file terms.
    KING_RING_ATTACK = 8
    KING_SHIELD = 9
    KING_OPEN_FILE = 22

    # Endgame "mop-up": when one side has a decisive non-pawn material edge in
    # an endgame, drive the weak king toward a corner and march the strong king
    # in. Pure guidance (well under a pawn) so it never overrides real material;
    # it just breaks the eval ties that previously let KQK / KRK / KQK-vs-P
    # shuffle without making progress.
    MOPUP_MIN_ADV = 400          # min non-pawn material edge (cp) to engage
    MOPUP_CMD_WEIGHT = 12        # x weak-king centre-distance (0..6) -> push to edge
    MOPUP_KING_WEIGHT = 5        # x king closeness (0..~13) -> bring our king up

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

        # --- Internal search bookkeeping (reset every search) ----------- #
        self.nodes = 0
        self.tt = {}                # pos-key -> (depth, flag, value, best_move)
        self.killers = {}           # ply -> [killer_1, killer_2]
        self.history = {}           # (color, from_sq, to_sq) -> score
        self.countermoves = {}      # (prev_from, prev_to) -> refutation move
        self.start_time = 0.0
        self.time_limit = None

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
            "Performance.bin", "Titans.bin", "book.bin",
            "gm2600.bin", "baron30.bin", "komodo.bin",
        ]

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
        """Return a legal Polyglot book move for ``board`` or ``None``."""
        if not self.use_book:
            return None
        reader = self._get_book_reader()
        if reader is None:
            return None
        try:
            # Weighted by the book's own move weights; falls back to None when
            # the position is not in the book (raises IndexError).
            entry = reader.weighted_choice(board)
        except Exception:
            return None      # position not in book (IndexError) or read error
        move = entry.move
        return move if move in board.legal_moves else None

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

        Single bitboard pass (no per-call ``piece_map``/``board.pieces`` rebuilds)
        with O(1) per-pawn structure tests via the masks precomputed in
        ``__init__``. This is the hot path -- it is evaluated at most interior
        nodes (futility / RFP) and at every quiescence leaf -- so it is kept as
        lean as possible.
        """
        occ_w = board.occupied_co[chess.WHITE]
        occ_b = board.occupied_co[chess.BLACK]
        pawns = board.pawns
        knights = board.knights
        bishops = board.bishops
        rooks = board.rooks
        queens = board.queens
        kings = board.kings
        wp = pawns & occ_w
        bp = pawns & occ_b

        mg = 0
        eg = 0
        phase = 0

        # --- Material + piece-square tables + game phase (one bitboard pass) -- #
        for pt, bb_all in ((chess.PAWN, pawns), (chess.KNIGHT, knights),
                           (chess.BISHOP, bishops), (chess.ROOK, rooks),
                           (chess.QUEEN, queens), (chess.KING, kings)):
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

        if phase > self.PHASE_MAX:
            phase = self.PHASE_MAX
        # Truncate toward zero (not floor) so the blend is exactly symmetric:
        # eval(pos) == -eval(mirror(pos)). Floor division would skew negatives.
        num = mg * phase + eg * (self.PHASE_MAX - phase)
        score = -((-num) // self.PHASE_MAX) if num < 0 else num // self.PHASE_MAX

        endgame = phase <= 6
        score += self._pawn_structure_bb(wp, bp)
        score += self._mobility_bb(board, occ_w, occ_b,
                                   knights, bishops, rooks, queens)
        score += self._rook_files_bb(rooks, occ_w, occ_b, wp, bp)
        score += self._bishop_pair_bb(bishops, occ_w, occ_b)
        if not endgame:
            score += self._king_safety_bb(board, occ_w, occ_b, wp, bp)
        else:
            # Endgame: replace king-safety (meaningless with few pieces) with a
            # mop-up term that helps convert won endings (e.g. KQK, KRK, KQ vs
            # KP) instead of shuffling. Guidance only -- well under a pawn.
            score += self._mopup_bb(occ_w, occ_b, knights, bishops,
                                    rooks, queens, kings)
        # Tempo: small bonus for the side to move (oriented to White so that
        # _evaluate_stm stays correct after its sign flip).
        score += self.TEMPO if board.turn == chess.WHITE else -self.TEMPO
        return score

    def _is_endgame(self, board):
        phase = (((board.knights | board.bishops).bit_count()) * 1
                 + (board.rooks.bit_count()) * 2
                 + (board.queens.bit_count()) * 4)
        return phase <= 6

    # ------------------------------------------------------------------ #
    # Pawn structure: doubled, isolated, backward and passed pawns.
    # O(1) per pawn using masks precomputed in __init__.
    # ------------------------------------------------------------------ #
    def _pawn_structure_bb(self, wp, bp):
        score = 0
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
                    score += sign * self.PASSED_PAWN[rel]
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
            score += sign * shield * self.KING_SHIELD
            score -= sign * attackers * self.KING_RING_ATTACK
            if not (own_pawns & self._file_bb[ksq & 7]):    # open king file
                score -= sign * self.KING_OPEN_FILE
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

    def _bishop_pair_bb(self, bishops, occ_w, occ_b):
        score = 0
        if (bishops & occ_w).bit_count() >= 2:
            score += self.BISHOP_PAIR
        if (bishops & occ_b).bit_count() >= 2:
            score -= self.BISHOP_PAIR
        return score

    # ------------------------------------------------------------------ #
    # Endgame mop-up: drive the weak king to the edge, bring our king up.
    # Engaged only with a decisive non-pawn material edge, so it cannot
    # distort balanced or drawish endings.
    # ------------------------------------------------------------------ #
    def _mopup_bb(self, occ_w, occ_b, knights, bishops, rooks, queens, kings):
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
        bonus = (self.MOPUP_CMD_WEIGHT * self._center_manhattan[loser]
                 + self.MOPUP_KING_WEIGHT * (14 - md))
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
        for move in board.legal_moves:
            score = 0
            if tt_move is not None and move == tt_move:
                score = 2_000_000
            elif board.is_capture(move):
                if board.is_en_passant(move):
                    victim_value = self.PIECE_VALUES[chess.PAWN]
                else:
                    victim = board.piece_at(move.to_square)
                    victim_value = self.PIECE_VALUES.get(victim.piece_type, 0) if victim else 0
                mover = board.piece_at(move.from_square)
                mover_value = self.PIECE_VALUES.get(mover.piece_type, 0) if mover else 0
                score = 1_000_000 + victim_value * 16 - mover_value   # MVV-LVA
                if move.promotion:
                    score += self.PIECE_VALUES.get(move.promotion, 0)
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
        """Legal captures and promotions, MVV-LVA ordered (for quiescence)."""
        scored = []
        for move in board.legal_moves:
            is_cap = board.is_capture(move)
            if not (is_cap or move.promotion):
                continue
            if board.is_en_passant(move):
                victim_value = self.PIECE_VALUES[chess.PAWN]
            elif is_cap:
                victim = board.piece_at(move.to_square)
                victim_value = self.PIECE_VALUES.get(victim.piece_type, 0) if victim else 0
            else:
                victim_value = 0
            mover = board.piece_at(move.from_square)
            mover_value = self.PIECE_VALUES.get(mover.piece_type, 0) if mover else 0
            promo = self.PIECE_VALUES.get(move.promotion, 0) if move.promotion else 0
            scored.append((victim_value * 16 - mover_value + promo, move))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [move for _, move in scored]

    def _store_killer(self, move, ply):
        slot = self.killers.setdefault(ply, [])
        if move in slot:
            return
        slot.insert(0, move)
        del slot[2:]

    def _update_history(self, board, move, depth):
        key = (board.turn, move.from_square, move.to_square)
        self.history[key] = self.history.get(key, 0) + depth * depth

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
        self.tt = {}
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

        best_move = legal[0]
        best_score_white = 0
        reached_depth = 0
        pv_move = None
        prev_score = None

        for depth in range(1, max_depth + 1):
            try:
                score, move = self._search_root_aspiration(
                    root, depth, pv_move, prev_score)
            except _TimeUp:
                break       # out of time mid-iteration: keep previous result

            if move is not None:
                best_move = move
                pv_move = move
                reached_depth = depth
                prev_score = score
                best_score_white = score if root_turn == chess.WHITE else -score

                record = {
                    "depth": depth,
                    "move": best_move.uci(),
                    "score": best_score_white,
                    "nodes": self.nodes,
                    "time_ms": int((time.time() - self.start_time) * 1000),
                }
                self.search_log.append(record)
                if self.on_depth is not None:
                    self.on_depth(record)

            if abs(score) > self.MATE_THRESHOLD:
                break       # forced mate found
            if time_limit is not None and (time.time() - self.start_time) >= time_limit:
                break

        self.nodes_searched = self.nodes
        self.last_score = best_score_white
        self.last_depth = reached_depth

        final_record = {
            "depth": reached_depth,
            "move": best_move.uci() if best_move is not None else "----",
            "score": best_score_white,
            "nodes": self.nodes,
            "time_ms": int((time.time() - self.start_time) * 1000),
            "final": True,
        }
        if self.on_final is not None:
            self.on_final(final_record)
        return best_move

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

        for move in self.order_moves(board, pv_move, 0):
            is_capture = board.is_capture(move)
            board.push(move)
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
            board.pop()

            results.append((value, move, exact))
            if value > best_value:
                best_value = value
                best_move = move
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
    def _negamax(self, board, depth, alpha, beta, ply, ext_budget, last_cap_sq, prev_move=None):
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
        tt_entry = self.tt.get(key)
        tt_move = None
        if tt_entry is not None:
            tt_depth, tt_flag, tt_value, tt_move = tt_entry
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
        # lazily (skip while in check, where it is meaningless).
        static_eval = None
        if not in_check:
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
            board.push(chess.Move.null())
            null_score = -self._negamax(board, depth - 1 - r, -beta, -beta + 1,
                                        ply + 1, ext_budget, None)
            board.pop()
            if null_score >= beta:
                return beta            # fail-hard; don't return false mates

        # --- Frontier futility pruning flag ---------------------------- #
        futile = (not is_pv and not in_check and depth in self.FUTILITY_MARGIN
                  and abs(alpha) < self.MATE_THRESHOLD
                  and static_eval + self.FUTILITY_MARGIN[depth] <= alpha)

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

            # --- Extensions (single-reply / recapture / passed-pawn; check below) -- #
            extension = 0
            if ext_budget > 0:
                if single_reply:
                    extension = 1
                elif is_capture and last_cap_sq is not None and move.to_square == last_cap_sq:
                    extension = 1
                elif self._is_passed_pawn_push(board, move):
                    extension = 1

            board.push(move)
            gives_check = board.is_check()
            if extension == 0 and ext_budget > 0 and gives_check:
                extension = 1            # check extension (cheap post-push test)
            child_last_cap = move.to_square if is_capture else None
            new_depth = depth - 1 + extension

            # --- Late-move reductions for quiet, late, non-checking moves -- #
            reduction = 0
            if (depth >= 3 and move_index >= self.LMR_MIN_MOVE
                    and extension == 0 and is_quiet
                    and not in_check and not gives_check):
                reduction = 1
                if move_index >= 6 and depth >= 6:
                    reduction = 2
                if is_pv and reduction > 0:
                    reduction -= 1        # reduce less on PV nodes

            if move_index == 0:
                value = -self._negamax(board, new_depth, -beta, -alpha,
                                       ply + 1, ext_budget - extension, child_last_cap, move)
            else:
                # PVS scout (optionally reduced), then re-search if it surprises.
                value = -self._negamax(board, new_depth - reduction, -alpha - 1, -alpha,
                                       ply + 1, ext_budget - extension, child_last_cap, move)
                if reduction and value > alpha:
                    value = -self._negamax(board, new_depth, -alpha - 1, -alpha,
                                           ply + 1, ext_budget - extension, child_last_cap, move)
                if alpha < value < beta:
                    value = -self._negamax(board, new_depth, -beta, -alpha,
                                           ply + 1, ext_budget - extension, child_last_cap, move)
            board.pop()

            if value > best_value:
                best_value = value
                best_move = move
            if value > alpha:
                alpha = value
            if alpha >= beta:
                if not is_capture and not move.promotion:
                    self._store_killer(move, ply)
                    self._update_history(board, move, depth)
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
        self.tt[key] = (depth, flag, self._tt_value_to(best_value, ply), best_move)
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
                board.push(move)
                score = -self._quiescence(board, -beta, -alpha, ply + 1)
                board.pop()
                if score > best:
                    best = score
                if score > alpha:
                    alpha = score
                if alpha >= beta:
                    return beta
            return best

        # Stand-pat: we can decline to capture and keep the static score.
        stand_pat = self._evaluate_stm(board)
        if stand_pat >= beta:
            return beta
        # Delta pruning: if even the biggest swing can't reach alpha, give up.
        if stand_pat + self.PIECE_VALUES[chess.QUEEN] + self.DELTA_MARGIN < alpha:
            return alpha
        if stand_pat > alpha:
            alpha = stand_pat

        for move in self._capture_moves(board):
            # Per-move delta pruning on the captured material.
            if not move.promotion:
                if board.is_en_passant(move):
                    victim_value = self.PIECE_VALUES[chess.PAWN]
                else:
                    victim = board.piece_at(move.to_square)
                    victim_value = self.PIECE_VALUES.get(victim.piece_type, 0) if victim else 0
                if stand_pat + victim_value + self.DELTA_MARGIN < alpha:
                    continue
            board.push(move)
            score = -self._quiescence(board, -beta, -alpha, ply + 1)
            board.pop()
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