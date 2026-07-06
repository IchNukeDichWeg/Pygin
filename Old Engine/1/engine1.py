"""
engine.py
=========

A self-contained chess engine built from scratch on top of the `chess`
library (used only for board representation, move generation and legality
checking -- *not* for evaluation or search).

Search features
---------------
* **Negamax + Alpha-Beta** core (scores relative to the side to move, which
  keeps null-move and transposition-table bound handling clean).
* **Iterative deepening** -- searches depth 1, 2, 3, ... reusing the best
  move from the previous iteration to improve move ordering, and supporting
  an optional wall-clock time budget (used for the GUI's "dynamic depth").
* **Transposition table** -- a Zobrist-keyed cache of previously searched
  positions with depth + bound (exact / lower / upper) information.
* **Quiescence search** at leaf nodes -- only "noisy" moves (captures,
  promotions, and all evasions while in check) are explored, which removes
  the horizon effect from hanging captures.
* **Null-move pruning** in non-zugzwang positions to cut the tree.
* **Move ordering** -- TT move first, then MVV-LVA captures, promotions,
  killer moves and checks.

Evaluation
----------
Material, piece-square tables, king safety, pawn structure (doubled /
isolated / passed) and mobility -- all from White's perspective in
centipawns (positive favours White).
"""

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
    """Negamax/alpha-beta engine with TT, quiescence, ID and null-move."""

    # ------------------------------------------------------------------ #
    # Material values (centipawns)
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
    # Piece-square tables (PeSTO's evaluation -- separate middlegame and
    # endgame tables for every piece, blended via a game-phase weight so the
    # evaluation "tapers" smoothly from the opening into the endgame).
    #
    # Tables are written in board reading order from a8 (top-left) to h1
    # (bottom-right) for WHITE. A white piece on python-chess square ``sq``
    # looks up ``table[chess.square_mirror(sq)]`` (python-chess square 0 is
    # a1); a black piece looks up ``table[sq]`` directly (vertical mirror).
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

    # Large finite mate score; faster mates score higher via a ply offset.
    MATE_SCORE = 1_000_000
    MATE_THRESHOLD = MATE_SCORE - 1_000   # scores above this represent mate
    MAX_PLY = 100                          # hard recursion safety cap

    # Per-square mobility bonus (centipawns) and null-move reduction.
    MOBILITY_WEIGHT = 1
    NULL_MOVE_R = 2

    # Maximum number of search-extension plies allowed along a single line
    # (bounds the cost of check / capture / passed-pawn-push extensions).
    MAX_EXTENSIONS = 16

    # Random tiebreak amplitude (centipawns) added to root move scores so
    # near-equal moves are chosen with a little variety, preventing the
    # engine from stalling or cycling between equivalent moves. 0.5cp ==
    # 0.005 pawns, matching the requested +/-0.001..0.005 range.
    TIEBREAK = 0.5

    def __init__(self):
        # Lookup of middlegame / endgame piece-square tables by piece type.
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

        # --- Search state, exposed to the UI after each move ------------ #
        self.nodes_searched = 0     # positions evaluated for the last move
        self.last_score = 0         # search score (centipawns, White's view)
        self.last_depth = 0         # depth actually reached

        # --- Internal search bookkeeping (reset every search) ----------- #
        self.nodes = 0
        self.tt = {}                # zobrist -> (depth, flag, value, best_move)
        self.killers = {}           # ply -> [killer_move_1, killer_move_2]
        self.start_time = 0.0
        self.time_limit = None

        # --- Real-time search logging ----------------------------------- #
        # ``on_depth(record)`` is called after every completed iterative-
        # deepening iteration and ``on_final(record)`` once the move is
        # chosen. Each record is a dict with depth / move (uci) / score
        # (centipawns, White's view) / nodes (cumulative) / time_ms. The GUI
        # uses these to stream the engine log to the screen and a file.
        self.on_depth = None
        self.on_final = None
        self.search_log = []        # list of per-depth records for last move

    # ================================================================== #
    # Evaluation
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

    def _evaluate_static(self, board):
        """Tapered material + piece-square score plus positional terms.

        Material and piece-square values are accumulated separately for the
        middlegame and endgame, then blended by the current game phase (24 =
        full opening material, 0 = bare kings). Pawn structure, king safety
        and mobility are layered on top. Returned from White's perspective
        (positive favours White). Terminal checks are handled by the search.
        """
        mg = 0      # middlegame score (White's perspective)
        eg = 0      # endgame score
        phase = 0   # accumulated game phase

        for square, piece in board.piece_map().items():
            piece_type = piece.piece_type
            phase += self.PHASE_WEIGHTS[piece_type]
            if piece.color == chess.WHITE:
                idx = chess.square_mirror(square)
                mg += self.MG_VALUES[piece_type] + self.mg_tables[piece_type][idx]
                eg += self.EG_VALUES[piece_type] + self.eg_tables[piece_type][idx]
            else:
                idx = square
                mg -= self.MG_VALUES[piece_type] + self.mg_tables[piece_type][idx]
                eg -= self.EG_VALUES[piece_type] + self.eg_tables[piece_type][idx]

        phase = min(phase, self.PHASE_MAX)
        # Blend: more middlegame weight when lots of material remains.
        score = (mg * phase + eg * (self.PHASE_MAX - phase)) // self.PHASE_MAX

        # Additional positional terms (not tapered).
        endgame = phase <= 6
        score += self._pawn_structure(board)
        score += self._king_safety(board, endgame)
        score += self._mobility(board)
        return score

    def _evaluate_stm(self, board):
        """Static evaluation relative to the side to move (for negamax)."""
        white_score = self._evaluate_static(board)
        return white_score if board.turn == chess.WHITE else -white_score

    def _is_endgame(self, board):
        """True in low-material positions (used for king-safety gating)."""
        phase = 0
        for _, piece in board.piece_map().items():
            phase += self.PHASE_WEIGHTS[piece.piece_type]
        return phase <= 6

    # ------------------------------------------------------------------ #
    # Pawn structure: doubled, isolated and passed pawns.
    # ------------------------------------------------------------------ #
    def _pawn_structure(self, board):
        score = 0
        for color in (chess.WHITE, chess.BLACK):
            sign = 1 if color == chess.WHITE else -1
            pawns = list(board.pieces(chess.PAWN, color))
            files = [chess.square_file(sq) for sq in pawns]

            for file_index in range(8):
                count = files.count(file_index)
                if count > 1:                       # doubled pawns
                    score -= sign * 20 * (count - 1)

            for sq in pawns:
                file_index = chess.square_file(sq)
                if (file_index - 1) not in files and (file_index + 1) not in files:
                    score -= sign * 15              # isolated pawn
                if self._is_passed_pawn(board, sq, color):
                    score += sign * 25              # passed pawn
        return score

    def _is_passed_pawn(self, board, square, color):
        file_index = chess.square_file(square)
        rank_index = chess.square_rank(square)
        for enemy_sq in board.pieces(chess.PAWN, not color):
            ef = chess.square_file(enemy_sq)
            er = chess.square_rank(enemy_sq)
            if abs(ef - file_index) <= 1:
                if color == chess.WHITE and er > rank_index:
                    return False
                if color == chess.BLACK and er < rank_index:
                    return False
        return True

    # ------------------------------------------------------------------ #
    # King safety: reward a friendly shield, punish a king on an open file.
    # ------------------------------------------------------------------ #
    def _king_safety(self, board, endgame):
        if endgame:
            return 0
        score = 0
        for color in (chess.WHITE, chess.BLACK):
            sign = 1 if color == chess.WHITE else -1
            king_sq = board.king(color)
            if king_sq is None:
                continue
            kf = chess.square_file(king_sq)
            kr = chess.square_rank(king_sq)

            shield = 0
            for df in (-1, 0, 1):
                for dr in (-1, 0, 1):
                    if df == 0 and dr == 0:
                        continue
                    nf, nr = kf + df, kr + dr
                    if 0 <= nf < 8 and 0 <= nr < 8:
                        neighbour = board.piece_at(chess.square(nf, nr))
                        if neighbour is not None and neighbour.color == color:
                            shield += 1
            score += sign * shield * 8

            if not self._file_has_friendly_pawn(board, kf, color):
                score -= sign * 25
        return score

    def _file_has_friendly_pawn(self, board, file_index, color):
        for rank_index in range(8):
            piece = board.piece_at(chess.square(file_index, rank_index))
            if piece and piece.piece_type == chess.PAWN and piece.color == color:
                return True
        return False

    # ------------------------------------------------------------------ #
    # Mobility: difference in attacked squares, a cheap activity proxy.
    # ------------------------------------------------------------------ #
    def _mobility(self, board):
        white_mobility = 0
        black_mobility = 0
        for square, piece in board.piece_map().items():
            count = len(board.attacks(square))
            if piece.color == chess.WHITE:
                white_mobility += count
            else:
                black_mobility += count
        return (white_mobility - black_mobility) * self.MOBILITY_WEIGHT

    # ================================================================== #
    # Move ordering
    # ================================================================== #
    def order_moves(self, board, tt_move=None, ply=0):
        """Return legal moves ordered best-first for alpha-beta efficiency.

        Priority: transposition-table move, then MVV-LVA captures and
        promotions, killer moves, and checks.
        """
        killers = self.killers.get(ply, [])
        scored = []
        for move in board.legal_moves:
            score = 0
            if tt_move is not None and move == tt_move:
                score += 1_000_000

            if board.is_capture(move):
                if board.is_en_passant(move):
                    victim_value = self.PIECE_VALUES[chess.PAWN]
                else:
                    victim = board.piece_at(move.to_square)
                    victim_value = self.PIECE_VALUES.get(victim.piece_type, 0) if victim else 0
                mover = board.piece_at(move.from_square)
                mover_value = self.PIECE_VALUES.get(mover.piece_type, 0) if mover else 0
                score += 100_000 + victim_value * 10 - mover_value   # MVV-LVA

            if move.promotion:
                score += 90_000 + self.PIECE_VALUES.get(move.promotion, 0)

            if move in killers:
                score += 80_000

            if board.gives_check(move):
                score += 50_000

            scored.append((score, move))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [move for _, move in scored]

    def _capture_moves(self, board):
        """Legal captures and promotions, MVV-LVA ordered (for quiescence)."""
        scored = []
        for move in board.legal_moves:
            if not (board.is_capture(move) or move.promotion):
                continue
            if board.is_en_passant(move):
                victim_value = self.PIECE_VALUES[chess.PAWN]
            elif board.is_capture(move):
                victim = board.piece_at(move.to_square)
                victim_value = self.PIECE_VALUES.get(victim.piece_type, 0) if victim else 0
            else:
                victim_value = 0
            mover = board.piece_at(move.from_square)
            mover_value = self.PIECE_VALUES.get(mover.piece_type, 0) if mover else 0
            promo = self.PIECE_VALUES.get(move.promotion, 0) if move.promotion else 0
            scored.append((victim_value * 10 - mover_value + promo, move))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [move for _, move in scored]

    def _store_killer(self, move, ply):
        slot = self.killers.setdefault(ply, [])
        if move in slot:
            return
        slot.insert(0, move)
        del slot[2:]   # keep at most two killer moves per ply

    # ================================================================== #
    # Search: iterative deepening driver
    # ================================================================== #
    def get_best_move(self, board, depth):
        """Best move via iterative deepening to a fixed ``depth`` (no clock)."""
        return self._search(board, max_depth=max(1, depth), time_limit=None)

    def get_best_move_timed(self, board, time_limit, max_depth=10):
        """Best move via iterative deepening bounded by ``time_limit`` seconds.

        Used when the GUI depth is set to 0 ("dynamic depth"): it searches
        progressively deeper until the time budget is spent, keeping the best
        move from the deepest completed (or safely aborted) iteration.
        """
        return self._search(board, max_depth=max_depth, time_limit=time_limit)

    def _search(self, board, max_depth, time_limit):
        # Reset per-move statistics and tables.
        self.nodes = 0
        self.tt = {}
        self.killers = {}
        self.start_time = time.time()
        self.time_limit = time_limit
        self.search_log = []

        root = board.copy()
        root_turn = root.turn
        legal = list(root.legal_moves)
        if not legal:
            self.nodes_searched = 0
            self.last_score = 0
            self.last_depth = 0
            return None

        best_move = legal[0]
        best_score_white = 0
        reached_depth = 0
        pv_move = None

        for depth in range(1, max_depth + 1):
            try:
                score, move = self._search_root(root, depth, pv_move)
            except _TimeUp:
                # Out of time mid-iteration: keep the previous depth's result.
                break

            if move is not None:
                best_move = move
                pv_move = move          # seed next iteration's move ordering
                reached_depth = depth
                # Convert the side-to-move score to White's perspective for UI.
                best_score_white = score if root_turn == chess.WHITE else -score

                # Emit a real-time log record for this completed depth.
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

            # Stop conditions between iterations.
            if abs(score) > self.MATE_THRESHOLD:
                break                    # forced mate found; no need to go deeper
            if time_limit is not None and (time.time() - self.start_time) >= time_limit:
                break

        self.nodes_searched = self.nodes
        self.last_score = best_score_white
        self.last_depth = reached_depth

        # Emit the final-move record.
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

    def _search_root(self, board, depth, pv_move):
        """One full-width search at the root; returns (score, best_move).

        A tiny random tiebreak is added to each move's score *only for the
        selection* (not for alpha/beta, which use true scores). This breaks
        ties between near-equal moves so the engine doesn't get stuck
        repeating or cycling between equivalent choices.
        """
        alpha = -float("inf")
        beta = float("inf")
        best_value = -float("inf")        # true score of the chosen move
        best_jittered = -float("inf")     # jittered score used for selection
        best_move = None

        for move in self.order_moves(board, pv_move, 0):
            board.push(move)
            value = -self._negamax(board, depth - 1, -beta, -alpha, 1,
                                   self.MAX_EXTENSIONS, None)
            board.pop()

            # Selection uses a jittered score; reporting/pruning use the true one.
            jittered = value + random.uniform(-self.TIEBREAK, self.TIEBREAK)
            if jittered > best_jittered:
                best_jittered = jittered
                best_value = value
                best_move = move
            # NOTE: alpha is deliberately NOT raised here. Each root move is
            # searched with the full window so its score is exact -- needed for
            # a correct random tiebreak and to avoid sibling moves failing high
            # against a near-mate alpha and falsely tying the mate score.
        return best_value, best_move

    # ================================================================== #
    # Negamax with alpha-beta, TT, null-move and quiescence
    # ================================================================== #
    def _negamax(self, board, depth, alpha, beta, ply, ext_budget, last_cap_sq):
        """Negamax with alpha-beta, TT, null-move, quiescence and extensions.

        ``ext_budget`` caps how many extension plies a single line may add;
        ``last_cap_sq`` is the square the previous move captured on (used to
        detect recaptures for the capture extension).
        """
        self.nodes += 1
        self._check_time()

        if ply >= self.MAX_PLY:
            return self._evaluate_stm(board)

        # --- Cheap draw detection -------------------------------------- #
        if board.is_insufficient_material() or board.halfmove_clock >= 100:
            return 0

        alpha_orig = alpha
        key = chess.polyglot.zobrist_hash(board)

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

        # --- Null-move pruning (skip a turn; if still >= beta, prune) --- #
        if (depth >= 3 and not in_check
                and self._has_non_pawn_material(board, board.turn)
                and beta < self.MATE_THRESHOLD):
            board.push(chess.Move.null())
            null_score = -self._negamax(board, depth - 1 - self.NULL_MOVE_R,
                                        -beta, -beta + 1, ply + 1,
                                        ext_budget, None)
            board.pop()
            if null_score >= beta:
                return beta

        # --- Main move loop -------------------------------------------- #
        best_value = -float("inf")
        best_move = None
        moves = self.order_moves(board, tt_move, ply)

        if not moves:
            # No legal moves: checkmate (in check) or stalemate (a draw).
            return -self.MATE_SCORE + ply if in_check else 0

        for move in moves:
            is_capture = board.is_capture(move)

            # --- Depth extensions for promising moves ------------------ #
            # (Chess Programming Wiki: check / capture(recapture) / passed-
            # pawn-push extensions.) Capped by ext_budget to bound cost.
            extension = 0
            if ext_budget > 0:
                if is_capture and last_cap_sq is not None and move.to_square == last_cap_sq:
                    extension = 1                      # recapture extension
                elif self._is_passed_pawn_push(board, move):
                    extension = 1                      # passed-pawn-push extension

            board.push(move)
            # Check extension: the move gives check (cheap to test post-push).
            if extension == 0 and ext_budget > 0 and board.is_check():
                extension = 1
            child_last_cap = move.to_square if is_capture else None
            value = -self._negamax(board, depth - 1 + extension, -beta, -alpha,
                                   ply + 1, ext_budget - extension, child_last_cap)
            board.pop()

            if value > best_value:
                best_value = value
                best_move = move
            if value > alpha:
                alpha = value
            if alpha >= beta:
                # Beta cut-off; remember quiet cutoff moves as killers.
                if not is_capture and not move.promotion:
                    self._store_killer(move, ply)
                break

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
        """True if ``move`` advances a (sufficiently far) passed pawn.

        Restricted to pawns that have reached the 5th rank or beyond (from
        the mover's side) so the extension stays cheap and focused on the
        dangerous pushes that actually warrant a deeper look.
        """
        piece = board.piece_at(move.from_square)
        if piece is None or piece.piece_type != chess.PAWN:
            return False
        rank = chess.square_rank(move.to_square)
        advanced = rank >= 4 if piece.color == chess.WHITE else rank <= 3
        if not advanced:
            return False
        return self._is_passed_pawn(board, move.to_square, piece.color)

    # ------------------------------------------------------------------ #
    # Quiescence search: only explore noisy moves to avoid the horizon
    # effect (a leaf where a capture is about to swing material).
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
            # When in check we must consider every evasion (else we could
            # "stand pat" out of a forced mate).
            moves = self.order_moves(board, None, ply)
            if not moves:
                return -self.MATE_SCORE + ply        # checkmate
        else:
            # Stand-pat: assume we can at least keep the static score.
            stand_pat = self._evaluate_stm(board)
            if stand_pat >= beta:
                return beta
            if stand_pat > alpha:
                alpha = stand_pat
            moves = self._capture_moves(board)

        for move in moves:
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

    # Mate scores are stored relative to the current ply so that a mate found
    # at a different depth keeps a consistent distance when reused from the TT.
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
