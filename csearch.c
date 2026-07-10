/* csearch.c -- ISOLATED C search-core prototype (roadmap #29/#30, phase 1-2).
 * Board layer extracted verbatim from movegen.c (static, perft-verified);
 * material + full mobility/king-safety eval + fixed-depth alpha-beta appended
 * below to measure the real per-node NPS ceiling for the GO/NO-GO gate.
 * Does NOT touch the shipped movegen.so/eval_c.so.
 *
 * Build (links eval_c.c for the mobility/king-safety term + Constants.c):
 *   clang -O3 -march=native -shared -fPIC -w -I. \
 *         -o csearch.so csearch.c eval_c.c Constants.c -lm -lpthread
 *
 * GATE RESULT (2026-07-08): full-eval C alpha-beta ~13.5M nodes/s vs the
 * Python engine's ~90k = ~150x. GO for phase 3 (full C search core). */

#include <stdint.h>
#include <stdio.h>
#include "Constants.h"   /* #2.1/#2.2: magic tables + INBETWEEN_BITBOARDS */

#define WHITE 1
#define BLACK 0

/* ---------- file masks (a1=bit0 .. h8=bit63) ----------------------------- */
static const uint64_t FILE_A = 0x0101010101010101ULL;
static const uint64_t FILE_H = 0x8080808080808080ULL;
static const uint64_t RANK_2 = 0x000000000000FF00ULL;
static const uint64_t RANK_7 = 0x00FF000000000000ULL;
static const uint64_t RANK_4 = 0x00000000FF000000ULL;
static const uint64_t RANK_5 = 0x000000FF00000000ULL;
static const uint64_t RANK_1 = 0x00000000000000FFULL;
static const uint64_t RANK_8 = 0xFF00000000000000ULL;

/* ---------- precomputed leaper tables ------------------------------------ */
/* W-15: knight/king tables are bit-identical to Constants.c's
 * KNIGHT_ATTACKS/KING_ATTACKS (verified), so alias those const tables; only
 * the PAWN table stays runtime-built (Constants' pawn tables use the
 * opposite "attacked-by" convention). */
#define KNIGHT_ATT KNIGHT_ATTACKS
#define KING_ATT   KING_ATTACKS
static uint64_t PAWN_ATT[2][64];   /* [WHITE]/[BLACK] */
static int tables_ready = 0;

/* --- step-5 eval masks (built once alongside PAWN_ATT; ports of
 * engine.py's _build_pawn_masks) ------------------------------------------ */
static uint64_t FILE_BB8[8], ADJ_FILES[8];
static uint64_t PASSED_MASK[2][64];    /* enemy pawns that stop/guard a passer */
static uint64_t SUPPORT_MASK[2][64];   /* own pawns adjacent, at-or-behind */
static uint64_t STOPATK_MASK[2][64];   /* enemy pawns attacking the stop square */
static int CENTER_MANH[64];            /* centre Manhattan distance, 0..6 */

/* C-06: runs once at .so load (constructor) -- see eval_c.c's note; the
 * exported generators no longer pay an init call + branch per invocation. */
__attribute__((constructor))
static void init_tables(void)
{
    if (tables_ready) return;
    for (int sq = 0; sq < 64; sq++) {
        uint64_t b = 1ULL << sq;
        /* W-15: knight/king aliased to Constants.c; only pawn stays here. */
        PAWN_ATT[WHITE][sq] = ((b << 9) & ~FILE_A) | ((b << 7) & ~FILE_H);
        PAWN_ATT[BLACK][sq] = ((b >> 7) & ~FILE_A) | ((b >> 9) & ~FILE_H);
    }
    for (int f = 0; f < 8; f++) FILE_BB8[f] = FILE_A << f;
    for (int f = 0; f < 8; f++)
        ADJ_FILES[f] = (f > 0 ? FILE_BB8[f - 1] : 0) | (f < 7 ? FILE_BB8[f + 1] : 0);
    static const int edge[8] = {3, 2, 1, 0, 0, 1, 2, 3};
    for (int sq = 0; sq < 64; sq++)
        CENTER_MANH[sq] = edge[sq & 7] + edge[sq >> 3];
    for (int sq = 0; sq < 64; sq++) {
        int f = sq & 7, r = sq >> 3;
        for (int color = 0; color < 2; color++) {
            uint64_t passed = 0, support = 0, stop = 0;
            int alo = (color == WHITE) ? r + 1 : 0;   /* ranks strictly ahead */
            int ahi = (color == WHITE) ? 8 : r;
            for (int nf = f - 1; nf <= f + 1; nf++) {
                if (nf < 0 || nf > 7) continue;
                for (int nr = alo; nr < ahi; nr++) passed |= 1ULL << (nr * 8 + nf);
            }
            int blo = (color == WHITE) ? 0 : r;       /* ranks at-or-behind */
            int bhi = (color == WHITE) ? r + 1 : 8;
            for (int nf = f - 1; nf <= f + 1; nf += 2) {
                if (nf < 0 || nf > 7) continue;
                for (int nr = blo; nr < bhi; nr++) support |= 1ULL << (nr * 8 + nf);
            }
            int stop_r = (color == WHITE) ? r + 1 : r - 1;
            if (stop_r >= 0 && stop_r < 8) {
                int atk_r = (color == WHITE) ? stop_r + 1 : stop_r - 1;
                if (atk_r >= 0 && atk_r < 8)
                    for (int nf = f - 1; nf <= f + 1; nf += 2)
                        if (nf >= 0 && nf <= 7) stop |= 1ULL << (atk_r * 8 + nf);
            }
            PASSED_MASK[color][sq] = passed;
            SUPPORT_MASK[color][sq] = support;
            STOPATK_MASK[color][sq] = stop;
        }
    }
    tables_ready = 1;
}

/* ---------- packed move word layout (#2.3) -------------------------------- *
 * Original layout (still readable -- no bits moved):
 *   bits  0- 5 : from          (6)
 *   bits  6-11 : to            (6)
 *   bits 12-14 : promo PT      (3)   0 = no promo, else 2..5 (N,B,R,Q)
 * Added in #2.3 (free, were unused):
 *   bits 15-17 : mover PT      (3)   1..6 (P,N,B,R,Q,K)
 *   bits 18-20 : victim PT     (3)   0 = none, else 1..6
 *                                    (victim != 0 IS the capture predicate)
 *   bit  21    : ep flag       (1)   en-passant capture
 *   bits 22-31 : reserved for #2.6 TAG_CHECK
 */
#define MV_SHIFT_MOVER   15
#define MV_SHIFT_VICTIM  18
#define MV_BIT_EP        (1U << 21)
#define MV_MASK_MOVER    (7U << MV_SHIFT_MOVER)
#define MV_MASK_VICTIM   (7U << MV_SHIFT_VICTIM)
#define MOVE_TAG(from,to,promo,mover,victim,ep) ((uint32_t)( \
        (from) | ((to)<<6) | ((promo)<<12) | \
        ((mover)<<MV_SHIFT_MOVER) | ((victim)<<MV_SHIFT_VICTIM) | \
        ((ep) ? MV_BIT_EP : 0U)))

/* Piece type IDs match python-chess: 1=P 2=N 3=B 4=R 5=Q 6=K. */
#define PT_PAWN   1
#define PT_KNIGHT 2
#define PT_BISHOP 3
#define PT_ROOK   4
#define PT_QUEEN  5
#define PT_KING   6

/* board_piece_type_at: defined right after the Board typedef below. */

/* ---------- slider attacks: magic bitboards (#2.2) ------------------------ *
 * Drop-in replacement for the previous Dumb7Fill bodies; same signature so
 * gen_legal / generate_captures / attacked / sq_attacked_by_them just get
 * faster. Verified byte-identical against the iterative version on random
 * occupancies before this swap landed; perft re-tested below in build.
 * Tables (ROOK_*, BISHOP_*) live in Constants.c.
 */
static inline uint64_t rook_attacks(int sq, uint64_t occ)
{
    occ &= ROOK_MASKS[sq];
    occ *= ROOK_MAGIC_NUMBERS[sq];
    occ >>= 64 - ROOK_REL_BITS[sq];
    return ROOK_ATTACKS[sq][occ];
}

static inline uint64_t bishop_attacks(int sq, uint64_t occ)
{
    occ &= BISHOP_MASKS[sq];
    occ *= BISHOP_MAGIC_NUMBERS[sq];
    occ >>= 64 - BISHOP_REL_BITS[sq];
    return BISHOP_ATTACKS[sq][occ];
}

/* ---------- board struct ------------------------------------------------- */
typedef struct {
    uint64_t pawns, knights, bishops, rooks, queens, kings;
    uint64_t occ[2];          /* occ[BLACK], occ[WHITE] */
    int turn;                 /* WHITE / BLACK */
    int ep;                   /* en-passant target square, or -1 */
    uint64_t castling;        /* bitboard of rook home squares with rights */
} Board;

static Board make_board(uint64_t pawns, uint64_t knights, uint64_t bishops,
                        uint64_t rooks, uint64_t queens, uint64_t kings,
                        uint64_t occ_w, uint64_t occ_b,
                        int turn, int ep, uint64_t castling)
{
    Board b;
    b.pawns = pawns; b.knights = knights; b.bishops = bishops;
    b.rooks = rooks; b.queens = queens; b.kings = kings;
    b.occ[BLACK] = occ_b; b.occ[WHITE] = occ_w;
    b.turn = turn; b.ep = ep; b.castling = castling;
    return b;
}

/* #2.3: piece type at `sq`, or 0 if empty. Used to encode victim PT inside
 * the move word at emit time so the Python search loop never has to call
 * board.piece_type_at / board.is_capture again per move. ~5 bit-AND tests,
 * usually short-circuits early -- only run for capture targets. */
static inline int board_piece_type_at(const Board* b, int sq)
{
    uint64_t bb = 1ULL << sq;
    if (b->pawns   & bb) return PT_PAWN;
    if (b->knights & bb) return PT_KNIGHT;
    if (b->bishops & bb) return PT_BISHOP;
    if (b->rooks   & bb) return PT_ROOK;
    if (b->queens  & bb) return PT_QUEEN;
    if (b->kings   & bb) return PT_KING;
    return 0;
}

/* Is `sq` attacked by `them`, given occupancy `occ`?  `us` = colour of the
 * piece on sq (for the pawn-attack table lookup).  p/n/bq/rq/k are THEM's
 * pawns / knights / bishops+queens / rooks+queens / king bitboards. */
static int attacked(int sq, uint64_t occ, int us,
                    uint64_t p, uint64_t n, uint64_t bq, uint64_t rq, uint64_t k)
{
    if (KNIGHT_ATT[sq] & n)            return 1;
    if (KING_ATT[sq]   & k)            return 1;
    if (PAWN_ATT[us][sq] & p)          return 1;
    /* C-09: skip the magic lookups when no such sliders exist -- frequent
     * in endgames, and attacked() runs per candidate move via legal(). */
    if (bq && (bishop_attacks(sq, occ) & bq))  return 1;
    if (rq && (rook_attacks(sq, occ)   & rq))  return 1;
    return 0;
}

/* Would moving from->to (is_ep: en-passant) leave our king in check? */
static int legal(const Board* b, int from, int to, int is_ep)
{
    int us = b->turn, them = us ^ 1;
    uint64_t fb = 1ULL << from, tb = 1ULL << to;
    uint64_t occ = b->occ[0] | b->occ[1];
    uint64_t occ2 = (occ ^ fb) | tb;          /* mover leaves from, lands on to */
    uint64_t capmask = tb;                    /* enemy square removed by the move */
    if (is_ep) {
        int capsq = (us == WHITE) ? to - 8 : to + 8;
        capmask = 1ULL << capsq;
        occ2 &= ~capmask;                     /* the e.p.-captured pawn vanishes */
    }
    uint64_t themocc = b->occ[them] & ~capmask;
    uint64_t kbb = b->kings & b->occ[us];
    if (!(fb & b->kings) && !kbb)
        return 1;   /* kingless side (test positions): no king to expose; ctzll(0) is UB */
    int ksq = (fb & b->kings) ? to : __builtin_ctzll(kbb);
    uint64_t p  = b->pawns   & themocc;
    uint64_t n  = b->knights & themocc;
    uint64_t bq = (b->bishops | b->queens) & themocc;
    uint64_t rq = (b->rooks   | b->queens) & themocc;
    uint64_t k  = b->kings   & themocc;
    return !attacked(ksq, occ2, us, p, n, bq, rq, k);
}

/* Is `sq` attacked by the side NOT to move, on the current board? (castling) */
static int sq_attacked_by_them(const Board* b, int sq)
{
    int us = b->turn, them = us ^ 1;
    uint64_t occ = b->occ[0] | b->occ[1];
    uint64_t to = b->occ[them];
    uint64_t p  = b->pawns   & to;
    uint64_t n  = b->knights & to;
    uint64_t bq = (b->bishops | b->queens) & to;
    uint64_t rq = (b->rooks   | b->queens) & to;
    uint64_t k  = b->kings   & to;
    return attacked(sq, occ, us, p, n, bq, rq, k);
}

/* Is the side to move currently in check? */
static int in_check(const Board* b)
{
    int us = b->turn;
    uint64_t kbb = b->kings & b->occ[us];
    if (!kbb) return 0;   /* kingless side (test positions): ctzll(0) is UB */
    int ksq = __builtin_ctzll(kbb);
    return sq_attacked_by_them(b, ksq);
}

/* ---------- legal move generation (python-chess pseudo-legal order) ------- */
/* Emits in exactly generate_pseudo_legal_moves order, minus illegal moves.
 * Correct SET in every position; correct ORDER when not in check. */
static int gen_legal(const Board* b, uint32_t* out)
{
    int us = b->turn, them = us ^ 1, cnt = 0;
    uint64_t own = b->occ[us], enemy = b->occ[them], occ = own | enemy;
    uint64_t empty = ~occ;
    uint64_t t, a;
    int from, to;

    /* 1. non-pawn piece moves: all of (N|B|R|Q|K), descending from-square;
     *    for each, targets descending. (King's normal moves included here.)
     *    #2.3: mover_pt comes free from the same if/else that picked the
     *    attack set; victim_pt is non-zero only when `to` hits enemy. */
    uint64_t nonpawns = (b->knights | b->bishops | b->rooks | b->queens | b->kings) & own;
    for (t = nonpawns; t; t &= ~(1ULL << from)) {
        from = 63 - __builtin_clzll(t);
        uint64_t fb = 1ULL << from, att;
        int mover_pt;
        if      (b->knights & fb) { att = KNIGHT_ATT[from];               mover_pt = PT_KNIGHT; }
        else if (b->kings   & fb) { att = KING_ATT[from];                 mover_pt = PT_KING;   }
        else if (b->bishops & fb) { att = bishop_attacks(from, occ);      mover_pt = PT_BISHOP; }
        else if (b->rooks   & fb) { att = rook_attacks(from, occ);        mover_pt = PT_ROOK;   }
        else                      { att = rook_attacks(from, occ) |
                                          bishop_attacks(from, occ);      mover_pt = PT_QUEEN;  }
        for (a = att & ~own; a; a &= ~(1ULL << to)) {
            to = 63 - __builtin_clzll(a);
            if (legal(b, from, to, 0)) {
                int victim_pt = ((1ULL << to) & enemy) ? board_piece_type_at(b, to) : 0;
                out[cnt++] = MOVE_TAG(from, to, 0, mover_pt, victim_pt, 0);
            }
        }
    }

    /* 2. castling: descending rook-square => king side before queen side.
     *    Mover is the king; never a capture. */
    {
        int e = (us == WHITE) ? 4 : 60;
        int ks_rook = (us == WHITE) ? 7 : 63;
        int qs_rook = (us == WHITE) ? 0 : 56;
        if (b->castling & (1ULL << ks_rook)) {
            int f = e + 1, g = e + 2;
            if (!(occ & ((1ULL << f) | (1ULL << g)))
                && !sq_attacked_by_them(b, e)
                && !sq_attacked_by_them(b, f)
                && !sq_attacked_by_them(b, g))
                out[cnt++] = MOVE_TAG(e, g, 0, PT_KING, 0, 0);
        }
        if (b->castling & (1ULL << qs_rook)) {
            int d = e - 1, c = e - 2, n2 = e - 3;
            if (!(occ & ((1ULL << d) | (1ULL << c) | (1ULL << n2)))
                && !sq_attacked_by_them(b, e)
                && !sq_attacked_by_them(b, d)
                && !sq_attacked_by_them(b, c))
                out[cnt++] = MOVE_TAG(e, c, 0, PT_KING, 0, 0);
        }
    }

    uint64_t pawns = b->pawns & own;

    /* 3. pawn captures (+ capture-promotions Q,R,B,N), descending from/to.
     *    Mover always PT_PAWN; victim is whatever sits on `to` (never
     *    empty here -- target was filtered by `& enemy`). */
    for (t = pawns; t; t &= ~(1ULL << from)) {
        from = 63 - __builtin_clzll(t);
        for (a = PAWN_ATT[us][from] & enemy; a; a &= ~(1ULL << to)) {
            to = 63 - __builtin_clzll(a);
            int promo = (us == WHITE) ? (to >= 56) : (to < 8);
            if (promo) {
                if (legal(b, from, to, 0)) {
                    int victim_pt = board_piece_type_at(b, to);
                    out[cnt++] = MOVE_TAG(from, to, 5, PT_PAWN, victim_pt, 0);
                    out[cnt++] = MOVE_TAG(from, to, 4, PT_PAWN, victim_pt, 0);
                    out[cnt++] = MOVE_TAG(from, to, 3, PT_PAWN, victim_pt, 0);
                    out[cnt++] = MOVE_TAG(from, to, 2, PT_PAWN, victim_pt, 0);
                }
            } else if (legal(b, from, to, 0)) {
                int victim_pt = board_piece_type_at(b, to);
                out[cnt++] = MOVE_TAG(from, to, 0, PT_PAWN, victim_pt, 0);
            }
        }
    }

    /* 4. single pawn pushes (+ promotions Q,R,B,N), descending to-square. */
    uint64_t single = (us == WHITE) ? ((pawns << 8) & empty) : ((pawns >> 8) & empty);
    for (a = single; a; a &= ~(1ULL << to)) {
        to = 63 - __builtin_clzll(a);
        from = (us == WHITE) ? to - 8 : to + 8;
        int promo = (us == WHITE) ? (to >= 56) : (to < 8);
        if (promo) {
            if (legal(b, from, to, 0)) {
                out[cnt++] = MOVE_TAG(from, to, 5, PT_PAWN, 0, 0);
                out[cnt++] = MOVE_TAG(from, to, 4, PT_PAWN, 0, 0);
                out[cnt++] = MOVE_TAG(from, to, 3, PT_PAWN, 0, 0);
                out[cnt++] = MOVE_TAG(from, to, 2, PT_PAWN, 0, 0);
            }
        } else if (legal(b, from, to, 0)) {
            out[cnt++] = MOVE_TAG(from, to, 0, PT_PAWN, 0, 0);
        }
    }

    /* 5. double pawn pushes, descending to-square. */
    uint64_t dbl = (us == WHITE) ? ((single << 8) & empty & RANK_4)
                                 : ((single >> 8) & empty & RANK_5);
    for (a = dbl; a; a &= ~(1ULL << to)) {
        to = 63 - __builtin_clzll(a);
        from = (us == WHITE) ? to - 16 : to + 16;
        if (legal(b, from, to, 0)) out[cnt++] = MOVE_TAG(from, to, 0, PT_PAWN, 0, 0);
    }

    /* 6. en passant, descending capturer-square. Victim is always a pawn. */
    if (b->ep >= 0 && !((1ULL << b->ep) & occ)) {
        for (t = pawns & PAWN_ATT[them][b->ep]; t; t &= ~(1ULL << from)) {
            from = 63 - __builtin_clzll(t);
            if (legal(b, from, b->ep, 1))
                out[cnt++] = MOVE_TAG(from, b->ep, 0, PT_PAWN, PT_PAWN, 1);
        }
    }
    return cnt;
}

/* P-22: noisy-only generation for quiescence -- exactly gen_legal's subset
 * of moves qsearch searches when NOT in check (victim || promotion), in the
 * same relative order, so the search tree is node-identical: section 1
 * restricted to `att & enemy`, castling skipped (never noisy), section 3
 * (pawn captures + capture-promos) unchanged, section 4 restricted to
 * promotion pushes, double pushes skipped, en passant unchanged. Only valid
 * when not in check (same caveat as gen_legal's ordering). */
static int gen_noisy(const Board* b, uint32_t* out)
{
    int us = b->turn, them = us ^ 1, cnt = 0;
    uint64_t own = b->occ[us], enemy = b->occ[them], occ = own | enemy;
    uint64_t empty = ~occ;
    uint64_t t, a;
    int from, to;

    uint64_t nonpawns = (b->knights | b->bishops | b->rooks | b->queens | b->kings) & own;
    for (t = nonpawns; t; t &= ~(1ULL << from)) {
        from = 63 - __builtin_clzll(t);
        uint64_t fb = 1ULL << from, att;
        int mover_pt;
        if      (b->knights & fb) { att = KNIGHT_ATT[from];               mover_pt = PT_KNIGHT; }
        else if (b->kings   & fb) { att = KING_ATT[from];                 mover_pt = PT_KING;   }
        else if (b->bishops & fb) { att = bishop_attacks(from, occ);      mover_pt = PT_BISHOP; }
        else if (b->rooks   & fb) { att = rook_attacks(from, occ);        mover_pt = PT_ROOK;   }
        else                      { att = rook_attacks(from, occ) |
                                          bishop_attacks(from, occ);      mover_pt = PT_QUEEN;  }
        for (a = att & enemy; a; a &= ~(1ULL << to)) {      /* captures only */
            to = 63 - __builtin_clzll(a);
            if (legal(b, from, to, 0))
                out[cnt++] = MOVE_TAG(from, to, 0, mover_pt,
                                      board_piece_type_at(b, to), 0);
        }
    }

    uint64_t pawns = b->pawns & own;

    for (t = pawns; t; t &= ~(1ULL << from)) {              /* pawn captures */
        from = 63 - __builtin_clzll(t);
        for (a = PAWN_ATT[us][from] & enemy; a; a &= ~(1ULL << to)) {
            to = 63 - __builtin_clzll(a);
            int promo = (us == WHITE) ? (to >= 56) : (to < 8);
            if (promo) {
                if (legal(b, from, to, 0)) {
                    int victim_pt = board_piece_type_at(b, to);
                    out[cnt++] = MOVE_TAG(from, to, 5, PT_PAWN, victim_pt, 0);
                    out[cnt++] = MOVE_TAG(from, to, 4, PT_PAWN, victim_pt, 0);
                    out[cnt++] = MOVE_TAG(from, to, 3, PT_PAWN, victim_pt, 0);
                    out[cnt++] = MOVE_TAG(from, to, 2, PT_PAWN, victim_pt, 0);
                }
            } else if (legal(b, from, to, 0)) {
                out[cnt++] = MOVE_TAG(from, to, 0, PT_PAWN,
                                      board_piece_type_at(b, to), 0);
            }
        }
    }

    /* promotion pushes only: single pushes landing on the last rank */
    uint64_t last = (us == WHITE) ? 0xFF00000000000000ULL : 0xFFULL;
    uint64_t single = ((us == WHITE) ? ((pawns << 8) & empty)
                                     : ((pawns >> 8) & empty)) & last;
    for (a = single; a; a &= ~(1ULL << to)) {
        to = 63 - __builtin_clzll(a);
        from = (us == WHITE) ? to - 8 : to + 8;
        if (legal(b, from, to, 0)) {
            out[cnt++] = MOVE_TAG(from, to, 5, PT_PAWN, 0, 0);
            out[cnt++] = MOVE_TAG(from, to, 4, PT_PAWN, 0, 0);
            out[cnt++] = MOVE_TAG(from, to, 3, PT_PAWN, 0, 0);
            out[cnt++] = MOVE_TAG(from, to, 2, PT_PAWN, 0, 0);
        }
    }

    if (b->ep >= 0 && !((1ULL << b->ep) & occ)) {           /* en passant */
        for (t = pawns & PAWN_ATT[them][b->ep]; t; t &= ~(1ULL << from)) {
            from = 63 - __builtin_clzll(t);
            if (legal(b, from, b->ep, 1))
                out[cnt++] = MOVE_TAG(from, b->ep, 0, PT_PAWN, PT_PAWN, 1);
        }
    }
    return cnt;
}

/* P-22: does the side to move have ANY legal quiet move? Early-exit; called
 * only when gen_noisy found nothing, to preserve qsearch's exact stalemate
 * semantics (full-gen n==0 -> draw score BEFORE stand-pat). Castling and
 * double pushes are deliberately skipped: castling legal implies the K->f
 * king step is legal (f empty + not attacked), and a legal double push
 * implies the single push is legal (same file, same pin ray, intermediate
 * square empty by definition) -- both are subsumed by the scans below. */
static int has_legal_quiet(const Board* b)
{
    int us = b->turn, cnt_from, to;
    uint64_t own = b->occ[us], occ = own | b->occ[us ^ 1];
    uint64_t empty = ~occ;
    uint64_t t, a;

    uint64_t nonpawns = (b->knights | b->bishops | b->rooks | b->queens | b->kings) & own;
    for (t = nonpawns; t; t &= ~(1ULL << cnt_from)) {
        cnt_from = 63 - __builtin_clzll(t);
        uint64_t fb = 1ULL << cnt_from, att;
        if      (b->knights & fb) att = KNIGHT_ATT[cnt_from];
        else if (b->kings   & fb) att = KING_ATT[cnt_from];
        else if (b->bishops & fb) att = bishop_attacks(cnt_from, occ);
        else if (b->rooks   & fb) att = rook_attacks(cnt_from, occ);
        else                      att = rook_attacks(cnt_from, occ)
                                      | bishop_attacks(cnt_from, occ);
        for (a = att & empty; a; a &= ~(1ULL << to)) {
            to = 63 - __builtin_clzll(a);
            if (legal(b, cnt_from, to, 0)) return 1;
        }
    }
    uint64_t pawns = b->pawns & own;
    uint64_t single = (us == WHITE) ? ((pawns << 8) & empty) : ((pawns >> 8) & empty);
    for (a = single; a; a &= ~(1ULL << to)) {
        to = 63 - __builtin_clzll(a);
        int from = (us == WHITE) ? to - 8 : to + 8;
        if (legal(b, from, to, 0)) return 1;
    }
    return 0;
}

/* P-23: the capture / quiet halves of gen_legal for staged ordering, each
 * emitting its subset in gen_legal's exact relative order (the ordering
 * sort is stable, so tie order IS generation order and the split must
 * preserve it). Note the split differs from P-22's noisy/quiet split:
 * v35 ordering scores NON-CAPTURE promotions as quiets (by history), so
 * gen_captures excludes promotion pushes and gen_quiets includes them.
 * Only valid when not in check (same caveat as gen_legal's ordering). */
static int gen_captures(const Board* b, uint32_t* out)
{
    int us = b->turn, them = us ^ 1, cnt = 0;
    uint64_t own = b->occ[us], enemy = b->occ[them], occ = own | enemy;
    uint64_t t, a;
    int from, to;

    uint64_t nonpawns = (b->knights | b->bishops | b->rooks | b->queens | b->kings) & own;
    for (t = nonpawns; t; t &= ~(1ULL << from)) {
        from = 63 - __builtin_clzll(t);
        uint64_t fb = 1ULL << from, att;
        int mover_pt;
        if      (b->knights & fb) { att = KNIGHT_ATT[from];               mover_pt = PT_KNIGHT; }
        else if (b->kings   & fb) { att = KING_ATT[from];                 mover_pt = PT_KING;   }
        else if (b->bishops & fb) { att = bishop_attacks(from, occ);      mover_pt = PT_BISHOP; }
        else if (b->rooks   & fb) { att = rook_attacks(from, occ);        mover_pt = PT_ROOK;   }
        else                      { att = rook_attacks(from, occ) |
                                          bishop_attacks(from, occ);      mover_pt = PT_QUEEN;  }
        for (a = att & enemy; a; a &= ~(1ULL << to)) {
            to = 63 - __builtin_clzll(a);
            if (legal(b, from, to, 0))
                out[cnt++] = MOVE_TAG(from, to, 0, mover_pt,
                                      board_piece_type_at(b, to), 0);
        }
    }
    uint64_t pawns = b->pawns & own;
    for (t = pawns; t; t &= ~(1ULL << from)) {              /* pawn captures */
        from = 63 - __builtin_clzll(t);
        for (a = PAWN_ATT[us][from] & enemy; a; a &= ~(1ULL << to)) {
            to = 63 - __builtin_clzll(a);
            int promo = (us == WHITE) ? (to >= 56) : (to < 8);
            if (promo) {
                if (legal(b, from, to, 0)) {
                    int victim_pt = board_piece_type_at(b, to);
                    out[cnt++] = MOVE_TAG(from, to, 5, PT_PAWN, victim_pt, 0);
                    out[cnt++] = MOVE_TAG(from, to, 4, PT_PAWN, victim_pt, 0);
                    out[cnt++] = MOVE_TAG(from, to, 3, PT_PAWN, victim_pt, 0);
                    out[cnt++] = MOVE_TAG(from, to, 2, PT_PAWN, victim_pt, 0);
                }
            } else if (legal(b, from, to, 0)) {
                out[cnt++] = MOVE_TAG(from, to, 0, PT_PAWN,
                                      board_piece_type_at(b, to), 0);
            }
        }
    }
    if (b->ep >= 0 && !((1ULL << b->ep) & occ)) {           /* en passant */
        for (t = pawns & PAWN_ATT[them][b->ep]; t; t &= ~(1ULL << from)) {
            from = 63 - __builtin_clzll(t);
            if (legal(b, from, b->ep, 1))
                out[cnt++] = MOVE_TAG(from, b->ep, 0, PT_PAWN, PT_PAWN, 1);
        }
    }
    return cnt;
}

static int gen_quiets(const Board* b, uint32_t* out)
{
    int us = b->turn, cnt = 0;
    uint64_t own = b->occ[us], occ = own | b->occ[us ^ 1];
    uint64_t empty = ~occ;
    uint64_t t, a;
    int from, to;

    uint64_t nonpawns = (b->knights | b->bishops | b->rooks | b->queens | b->kings) & own;
    for (t = nonpawns; t; t &= ~(1ULL << from)) {
        from = 63 - __builtin_clzll(t);
        uint64_t fb = 1ULL << from, att;
        int mover_pt;
        if      (b->knights & fb) { att = KNIGHT_ATT[from];               mover_pt = PT_KNIGHT; }
        else if (b->kings   & fb) { att = KING_ATT[from];                 mover_pt = PT_KING;   }
        else if (b->bishops & fb) { att = bishop_attacks(from, occ);      mover_pt = PT_BISHOP; }
        else if (b->rooks   & fb) { att = rook_attacks(from, occ);        mover_pt = PT_ROOK;   }
        else                      { att = rook_attacks(from, occ) |
                                          bishop_attacks(from, occ);      mover_pt = PT_QUEEN;  }
        for (a = att & empty; a; a &= ~(1ULL << to)) {
            to = 63 - __builtin_clzll(a);
            if (legal(b, from, to, 0))
                out[cnt++] = MOVE_TAG(from, to, 0, mover_pt, 0, 0);
        }
    }
    {                                                       /* castling */
        int e = (us == WHITE) ? 4 : 60;
        int ks_rook = (us == WHITE) ? 7 : 63;
        int qs_rook = (us == WHITE) ? 0 : 56;
        if (b->castling & (1ULL << ks_rook)) {
            int f = e + 1, g = e + 2;
            if (!(occ & ((1ULL << f) | (1ULL << g)))
                && !sq_attacked_by_them(b, e)
                && !sq_attacked_by_them(b, f)
                && !sq_attacked_by_them(b, g))
                out[cnt++] = MOVE_TAG(e, g, 0, PT_KING, 0, 0);
        }
        if (b->castling & (1ULL << qs_rook)) {
            int d = e - 1, c = e - 2, n2 = e - 3;
            if (!(occ & ((1ULL << d) | (1ULL << c) | (1ULL << n2)))
                && !sq_attacked_by_them(b, e)
                && !sq_attacked_by_them(b, d)
                && !sq_attacked_by_them(b, c))
                out[cnt++] = MOVE_TAG(e, c, 0, PT_KING, 0, 0);
        }
    }
    uint64_t pawns = b->pawns & own;
    uint64_t single = (us == WHITE) ? ((pawns << 8) & empty) : ((pawns >> 8) & empty);
    for (a = single; a; a &= ~(1ULL << to)) {   /* pushes INCLUDING promos */
        to = 63 - __builtin_clzll(a);
        from = (us == WHITE) ? to - 8 : to + 8;
        int promo = (us == WHITE) ? (to >= 56) : (to < 8);
        if (promo) {
            if (legal(b, from, to, 0)) {
                out[cnt++] = MOVE_TAG(from, to, 5, PT_PAWN, 0, 0);
                out[cnt++] = MOVE_TAG(from, to, 4, PT_PAWN, 0, 0);
                out[cnt++] = MOVE_TAG(from, to, 3, PT_PAWN, 0, 0);
                out[cnt++] = MOVE_TAG(from, to, 2, PT_PAWN, 0, 0);
            }
        } else if (legal(b, from, to, 0)) {
            out[cnt++] = MOVE_TAG(from, to, 0, PT_PAWN, 0, 0);
        }
    }
    uint64_t dbl = (us == WHITE) ? ((single << 8) & empty & RANK_4)
                                 : ((single >> 8) & empty & RANK_5);
    for (a = dbl; a; a &= ~(1ULL << to)) {
        to = 63 - __builtin_clzll(a);
        from = (us == WHITE) ? to - 16 : to + 16;
        if (legal(b, from, to, 0)) out[cnt++] = MOVE_TAG(from, to, 0, PT_PAWN, 0, 0);
    }
    return cnt;
}

/* P-23: reconstruct + validate a 15-bit move key (from|to<<6|promo<<12)
 * against the CURRENT position, without generating. Returns the full
 * tagged move word iff gen_legal would emit exactly this move, else 0 --
 * the acceptance set must match gen_legal branch-for-branch, because the
 * staged stream replaces membership-in-the-generated-array as the
 * legality filter for TT/killer/counter moves. */
static uint32_t move_from_key(const Board* b, uint32_t key)
{
    if (!key) return 0;
    int from = key & 63, to = (key >> 6) & 63, promo = (key >> 12) & 7;
    if (from == to) return 0;
    int us = b->turn, them = us ^ 1;
    uint64_t fb = 1ULL << from, tb = 1ULL << to;
    uint64_t own = b->occ[us], enemy = b->occ[them], occ = own | enemy;
    if (!(own & fb) || (own & tb)) return 0;
    int mover = board_piece_type_at(b, from);
    int victim = (enemy & tb) ? board_piece_type_at(b, to) : 0;
    if (promo && mover != PT_PAWN) return 0;

    if (mover != PT_PAWN) {
        uint64_t att;
        if      (mover == PT_KNIGHT) att = KNIGHT_ATT[from];
        else if (mover == PT_KING)   att = KING_ATT[from];
        else if (mover == PT_BISHOP) att = bishop_attacks(from, occ);
        else if (mover == PT_ROOK)   att = rook_attacks(from, occ);
        else                         att = rook_attacks(from, occ)
                                         | bishop_attacks(from, occ);
        if (att & tb) {
            if (!legal(b, from, to, 0)) return 0;
            return MOVE_TAG(from, to, 0, mover, victim, 0);
        }
        if (mover != PT_KING || victim) return 0;
        {                                           /* castling two-step */
            int e = (us == WHITE) ? 4 : 60;
            if (from != e) return 0;
            if (to == e + 2) {
                int ks_rook = (us == WHITE) ? 7 : 63;
                int f = e + 1, g = e + 2;
                if (!(b->castling & (1ULL << ks_rook))) return 0;
                if (occ & ((1ULL << f) | (1ULL << g))) return 0;
                if (sq_attacked_by_them(b, e) || sq_attacked_by_them(b, f)
                        || sq_attacked_by_them(b, g)) return 0;
                return MOVE_TAG(e, g, 0, PT_KING, 0, 0);
            }
            if (to == e - 2) {
                int qs_rook = (us == WHITE) ? 0 : 56;
                int d = e - 1, c = e - 2, n2 = e - 3;
                if (!(b->castling & (1ULL << qs_rook))) return 0;
                if (occ & ((1ULL << d) | (1ULL << c) | (1ULL << n2))) return 0;
                if (sq_attacked_by_them(b, e) || sq_attacked_by_them(b, d)
                        || sq_attacked_by_them(b, c)) return 0;
                return MOVE_TAG(e, c, 0, PT_KING, 0, 0);
            }
            return 0;
        }
    }

    /* pawn */
    int last = (us == WHITE) ? (to >= 56) : (to < 8);
    if (last ? (promo < 2 || promo > 5) : (promo != 0)) return 0;
    if (PAWN_ATT[us][from] & tb) {
        if (victim) {
            if (!legal(b, from, to, 0)) return 0;
            return MOVE_TAG(from, to, promo, PT_PAWN, victim, 0);
        }
        if (to == b->ep && !(tb & occ)) {           /* en passant */
            if (!legal(b, from, to, 1)) return 0;
            return MOVE_TAG(from, to, 0, PT_PAWN, PT_PAWN, 1);
        }
        return 0;
    }
    int fwd = (us == WHITE) ? from + 8 : from - 8;
    if (to == fwd) {
        if (occ & tb) return 0;
        if (!legal(b, from, to, 0)) return 0;
        return MOVE_TAG(from, to, promo, PT_PAWN, 0, 0);
    }
    int dbl2 = (us == WHITE) ? from + 16 : from - 16;
    int start = (us == WHITE) ? (from >= 8 && from < 16)
                              : (from >= 48 && from < 56);
    if (to == dbl2 && start) {
        if ((occ & (1ULL << fwd)) || (occ & tb)) return 0;
        if (!legal(b, from, to, 0)) return 0;
        return MOVE_TAG(from, to, 0, PT_PAWN, 0, 0);
    }
    return 0;
}

/* ---------- exported: generate_legal ------------------------------------- */
/* Returns move count, or -1 if the side to move is in check (caller should
 * fall back to python-chess to preserve the evasion move order). */
static void apply_move(Board* b, uint32_t mv)
{
    int from = mv & 63, to = (mv >> 6) & 63, promo = (mv >> 12) & 7;
    int us = b->turn, them = us ^ 1;
    uint64_t fb = 1ULL << from, tb = 1ULL << to;

    int movpt;                                  /* 1=P 2=N 3=B 4=R 5=Q 6=K */
    if      (b->pawns   & fb) movpt = 1;
    else if (b->knights & fb) movpt = 2;
    else if (b->bishops & fb) movpt = 3;
    else if (b->rooks   & fb) movpt = 4;
    else if (b->queens  & fb) movpt = 5;
    else                      movpt = 6;

    uint64_t capmask = tb;
    if (movpt == 1 && to == b->ep && !(b->occ[them] & tb))
        capmask = 1ULL << ((us == WHITE) ? to - 8 : to + 8);

    uint64_t ncap = ~capmask;
    b->pawns &= ncap; b->knights &= ncap; b->bishops &= ncap;
    b->rooks &= ncap; b->queens &= ncap;
    b->occ[them] &= ncap;

    uint64_t nfrom = ~fb;
    b->pawns &= nfrom; b->knights &= nfrom; b->bishops &= nfrom;
    b->rooks &= nfrom; b->queens &= nfrom; b->kings &= nfrom;

    int finalpt = promo ? promo : movpt;
    switch (finalpt) {
        case 2:  b->knights |= tb; break;
        case 3:  b->bishops |= tb; break;
        case 4:  b->rooks   |= tb; break;
        case 5:  b->queens  |= tb; break;
        case 6:  b->kings   |= tb; break;
        default: b->pawns   |= tb; break;
    }
    b->occ[us] = (b->occ[us] & nfrom) | tb;

    if (movpt == 6 && (to - from == 2 || from - to == 2)) {
        int rf, rt;
        if (to > from) { rf = (us == WHITE) ? 7 : 63; rt = (us == WHITE) ? 5 : 61; }
        else           { rf = (us == WHITE) ? 0 : 56; rt = (us == WHITE) ? 3 : 59; }
        uint64_t rfb = 1ULL << rf, rtb = 1ULL << rt;
        b->rooks   = (b->rooks   & ~rfb) | rtb;
        b->occ[us] = (b->occ[us] & ~rfb) | rtb;
    }

    uint64_t cr = b->castling;
    if (movpt == 6)
        cr &= (us == WHITE) ? ~((1ULL << 0) | (1ULL << 7))
                            : ~((1ULL << 56) | (1ULL << 63));
    cr &= ~fb;
    cr &= ~capmask;
    b->castling = cr;

    b->ep = (movpt == 1 && (to - from == 16 || from - to == 16)) ? (from + to) / 2 : -1;
    b->turn = them;
}


/* ====================================================================== *
 * Phase-2 prototype eval + fixed-depth alpha-beta (GO/NO-GO measurement).
 *
 * Eval here is MATERIAL ONLY -- deliberately the cheapest possible per-node
 * eval, so this measures the OPTIMISTIC NPS ceiling. The real static eval
 * (mobility / king safety / pawns) is strictly heavier, so if even this
 * material-only C search does not clear the Python engine by a wide margin,
 * the full core cannot either. Move ordering: MVV-LVA from the victim tag
 * already packed into the move word by gen_legal (bits 18-20).
 * ====================================================================== */
static const int PIECE_VAL[7] = {0, 100, 320, 330, 500, 900, 0};  /* by PT */

static int eval_material_stm(const Board* b)
{
    int us = b->turn, them = us ^ 1;
    uint64_t mine = b->occ[us], theirs = b->occ[them];
    int score = 0;
    score += 100 * __builtin_popcountll(b->pawns   & mine);
    score += 320 * __builtin_popcountll(b->knights & mine);
    score += 330 * __builtin_popcountll(b->bishops & mine);
    score += 500 * __builtin_popcountll(b->rooks   & mine);
    score += 900 * __builtin_popcountll(b->queens  & mine);
    score -= 100 * __builtin_popcountll(b->pawns   & theirs);
    score -= 320 * __builtin_popcountll(b->knights & theirs);
    score -= 330 * __builtin_popcountll(b->bishops & theirs);
    score -= 500 * __builtin_popcountll(b->rooks   & theirs);
    score -= 900 * __builtin_popcountll(b->queens  & theirs);
    return score;
}

/* Honest-gate eval: material + the expensive mobility/king-safety term --
 * the SAME eval_c.c function the real engine calls per node, here linked
 * directly (no ctypes crossing). Its O(pieces) attack-generation loops are
 * where per-node eval cost concentrates, so including it makes the NPS
 * number representative rather than optimistic. (Compiled-in default params;
 * even a zero weight still runs the loops, so the COST the gate measures is
 * real regardless of the returned value.) */
extern int mobility_king_safety(uint64_t occ_w, uint64_t occ_b,
    uint64_t knights, uint64_t bishops, uint64_t rooks, uint64_t queens,
    uint64_t wp, uint64_t bp, uint64_t kings, int phase);

static int game_phase(const Board* b)
{
    int ph = __builtin_popcountll(b->knights | b->bishops)
           + 2 * __builtin_popcountll(b->rooks)
           + 4 * __builtin_popcountll(b->queens);
    return ph > 24 ? 24 : ph;
}

/* ====================================================================== *
 * Phase-3 step 5: FULL static eval -- port of engine.py's _evaluate_static.
 *
 * White-perspective terms, stm sign applied at the end:
 *   base  : tapered material + PST (White reads sq^56, Black reads sq) +
 *           tempo; the mg/eg blend truncates toward zero exactly like the
 *           Python original (C integer division IS trunc-toward-zero).
 *   mopup : lone-loser strong mop-up SHORTCUT -- when one side is a bare
 *           king(+pawns) and the other leads by >= MOPUP_MIN_ADV non-pawn
 *           material, it REPLACES all positional terms (engine.py returns
 *           early the same way, so the weak mop-up folded into
 *           mobility_king_safety can never double-count).
 *   pawns : doubled / isolated / backward / passed (tapered passer bonus,
 *           V-06-style precomputed [phase][rel] table).
 *   mks   : eval_c.c's mobility_king_safety, linked in -- mobility, king
 *           safety, rook files, bishop pair, rook-on-7th, threats and the
 *           weak mop-up. Its params are process globals: the host must sync
 *           them through the same exported set_* calls engine.py uses
 *           (csearch.so carries its OWN copy of those globals).
 *
 * Tables/params arrive via csearch_set_eval so engine.py stays the single
 * source of truth (a retune cannot desync this copy). Until it is called,
 * eval_full_stm falls back to the phase-2 gate eval (material + mks) so
 * the earlier NPS harnesses keep working unchanged.
 * ====================================================================== */
static int g_eval_ready = 0;
static int g_mg_pst[7][64], g_eg_pst[7][64];        /* by PT 1..6 */
static int g_mg_val[7], g_eg_val[7], g_phase_w[7];
static int g_tempo, g_doubled, g_isolated, g_backward;
static int g_passed_taper[25][8];                   /* [phase][rel rank] */
static int g_mopup_min, g_mopup_scmd, g_mopup_sking;

void csearch_set_eval(const int* mg_pst, const int* eg_pst,  /* [6*64] P,N,B,R,Q,K */
                      const int* mg_val, const int* eg_val,  /* [7] by PT, [0] unused */
                      const int* phase_w,                    /* [7] by PT */
                      int tempo, int doubled, int isolated, int backward,
                      const int* passed_mg, const int* passed_eg,  /* [8] by rel rank */
                      int mopup_min, int mopup_strong_cmd, int mopup_strong_king)
{
    for (int pt = 1; pt <= 6; pt++)
        for (int sq = 0; sq < 64; sq++) {
            g_mg_pst[pt][sq] = mg_pst[(pt - 1) * 64 + sq];
            g_eg_pst[pt][sq] = eg_pst[(pt - 1) * 64 + sq];
        }
    for (int pt = 0; pt < 7; pt++) {
        g_mg_val[pt] = mg_val[pt];
        g_eg_val[pt] = eg_val[pt];
        g_phase_w[pt] = phase_w[pt];
    }
    g_tempo = tempo; g_doubled = doubled;
    g_isolated = isolated; g_backward = backward;
    g_mopup_min = mopup_min;
    g_mopup_scmd = mopup_strong_cmd; g_mopup_sking = mopup_strong_king;
    for (int ph = 0; ph <= 24; ph++)
        for (int rel = 0; rel < 8; rel++)
            g_passed_taper[ph][rel] =
                (passed_mg[rel] * ph + passed_eg[rel] * (24 - ph)) / 24;
    g_eval_ready = 1;
}

/* Doubled / isolated / backward / passed, White's perspective. */
static int pawn_structure(uint64_t wp, uint64_t bp, int phase)
{
    int s = 0;
    const int* taper = g_passed_taper[phase];
    for (int f = 0; f < 8; f++) {
        int c = __builtin_popcountll(wp & FILE_BB8[f]);
        if (c > 1) s -= g_doubled * (c - 1);
        c = __builtin_popcountll(bp & FILE_BB8[f]);
        if (c > 1) s += g_doubled * (c - 1);
    }
    for (uint64_t t = wp; t; t &= t - 1) {
        int sq = __builtin_ctzll(t), f = sq & 7;
        if (!(wp & ADJ_FILES[f]))                    s -= g_isolated;
        else if (!(wp & SUPPORT_MASK[WHITE][sq])
                 && (bp & STOPATK_MASK[WHITE][sq]))  s -= g_backward;
        if (!(bp & PASSED_MASK[WHITE][sq]))          s += taper[sq >> 3];
    }
    for (uint64_t t = bp; t; t &= t - 1) {
        int sq = __builtin_ctzll(t), f = sq & 7;
        if (!(bp & ADJ_FILES[f]))                    s += g_isolated;
        else if (!(bp & SUPPORT_MASK[BLACK][sq])
                 && (wp & STOPATK_MASK[BLACK][sq]))  s += g_backward;
        if (!(wp & PASSED_MASK[BLACK][sq]))          s -= taper[7 - (sq >> 3)];
    }
    return s;
}

static int g_simp_thresh = 0, g_simp_weight = 10;   /* v30 simplify port;
                                                     * 0 = off (default) */
static int eval_white(const Board* b)
{
    uint64_t occ_w = b->occ[WHITE], occ_b = b->occ[BLACK];
    const uint64_t bbs[7] = {0, b->pawns, b->knights, b->bishops,
                             b->rooks, b->queens, b->kings};
    int mg = 0, eg = 0, phase = 0;
    for (int pt = 1; pt <= 6; pt++) {
        const int* mgt = g_mg_pst[pt];
        const int* egt = g_eg_pst[pt];
        int mv = g_mg_val[pt], ev = g_eg_val[pt], pw = g_phase_w[pt];
        for (uint64_t t = bbs[pt] & occ_w; t; t &= t - 1) {
            int i = __builtin_ctzll(t) ^ 56;         /* White reads mirrored */
            mg += mv + mgt[i]; eg += ev + egt[i]; phase += pw;
        }
        for (uint64_t t = bbs[pt] & occ_b; t; t &= t - 1) {
            int i = __builtin_ctzll(t);
            mg -= mv + mgt[i]; eg -= ev + egt[i]; phase += pw;
        }
    }
    if (phase > 24) phase = 24;
    int score = (mg * phase + eg * (24 - phase)) / 24;
    score += (b->turn == WHITE) ? g_tempo : -g_tempo;

    /* lone-loser strong mop-up shortcut (replaces ALL positional terms).
     * Kingless test positions skip it (Python would index [-1]; C won't). */
    uint64_t wk = b->kings & occ_w, bk = b->kings & occ_b;
    int lone_w = (occ_w & ~b->kings & ~b->pawns) == 0;
    int lone_b = (occ_b & ~b->kings & ~b->pawns) == 0;
    if (lone_w != lone_b && wk && bk) {
        int npm_w = 320 * __builtin_popcountll(b->knights & occ_w)
                  + 330 * __builtin_popcountll(b->bishops & occ_w)
                  + 500 * __builtin_popcountll(b->rooks   & occ_w)
                  + 900 * __builtin_popcountll(b->queens  & occ_w);
        int npm_b = 320 * __builtin_popcountll(b->knights & occ_b)
                  + 330 * __builtin_popcountll(b->bishops & occ_b)
                  + 500 * __builtin_popcountll(b->rooks   & occ_b)
                  + 900 * __builtin_popcountll(b->queens  & occ_b);
        int adv = npm_w - npm_b;
        if ((adv < 0 ? -adv : adv) >= g_mopup_min) {
            int wks = __builtin_ctzll(wk), bks = __builtin_ctzll(bk);
            int loser = (adv > 0) ? bks : wks;
            int df = (wks & 7) - (bks & 7), dr = (wks >> 3) - (bks >> 3);
            int md = (df < 0 ? -df : df) + (dr < 0 ? -dr : dr);
            int bonus = g_mopup_scmd * CENTER_MANH[loser]
                      + g_mopup_sking * (14 - md);
            return score + ((adv > 0) ? bonus : -bonus);
        }
    }

    score += pawn_structure(b->pawns & occ_w, b->pawns & occ_b, phase);
    score += mobility_king_safety(occ_w, occ_b, b->knights, b->bishops,
                                  b->rooks, b->queens,
                                  b->pawns & occ_w, b->pawns & occ_b,
                                  b->kings, phase);
    /* simplify (port of _simplify_bb; NOT applied on the lone-loser path
     * above, matching Python's early return): FULL material diff incl.
     * pawns at the classic 100..900 values (_npm), reward the leader per
     * minor/major already traded off. */
    if (g_simp_thresh > 0) {
        int mw = 100 * __builtin_popcountll(b->pawns   & occ_w)
               + 320 * __builtin_popcountll(b->knights & occ_w)
               + 330 * __builtin_popcountll(b->bishops & occ_w)
               + 500 * __builtin_popcountll(b->rooks   & occ_w)
               + 900 * __builtin_popcountll(b->queens  & occ_w);
        int mb = 100 * __builtin_popcountll(b->pawns   & occ_b)
               + 320 * __builtin_popcountll(b->knights & occ_b)
               + 330 * __builtin_popcountll(b->bishops & occ_b)
               + 500 * __builtin_popcountll(b->rooks   & occ_b)
               + 900 * __builtin_popcountll(b->queens  & occ_b);
        int diff = mw - mb;
        if ((diff < 0 ? -diff : diff) >= g_simp_thresh) {
            int pieces = __builtin_popcountll(b->knights | b->bishops
                                              | b->rooks | b->queens);
            score += (diff > 0 ? 1 : -1) * g_simp_weight * (14 - pieces);
        }
    }
    return score;
}

static int eval_full_stm(const Board* b)
{
    if (!g_eval_ready) {                /* phase-2 gate eval (fallback) */
        int mat = eval_material_stm(b);
        uint64_t wp = b->pawns & b->occ[WHITE], bp = b->pawns & b->occ[BLACK];
        int white_pos = mobility_king_safety(b->occ[WHITE], b->occ[BLACK],
            b->knights, b->bishops, b->rooks, b->queens, wp, bp, b->kings,
            game_phase(b));
        return mat + ((b->turn == WHITE) ? white_pos : -white_pos);
    }
    int w = eval_white(b);
    return (b->turn == WHITE) ? w : -w;
}

/* Exported oracle entry for the differential test: White-perspective full
 * static eval of an arbitrary position (compare vs _evaluate_static). */
int csearch_eval_white(uint64_t pawns, uint64_t knights, uint64_t bishops,
                       uint64_t rooks, uint64_t queens, uint64_t kings,
                       uint64_t occ_w, uint64_t occ_b,
                       int turn, int ep, uint64_t castling)
{
    Board b = make_board(pawns, knights, bishops, rooks, queens, kings,
                         occ_w, occ_b, turn, ep, castling);
    return eval_white(&b);
}

#include <string.h>

static __thread uint64_t g_nodes;   /* per-thread; helpers aggregate on exit */
#define CS_INF    30000
#define CS_MAXPLY 64
#define HIST_MAX  16384

/* SEE (exchange evaluation) from eval_c.c -- demotes losing captures. */
extern int see(uint64_t pawns, uint64_t knights, uint64_t bishops, uint64_t rooks,
               uint64_t queens, uint64_t kings, uint64_t occ_w, uint64_t occ_b,
               int turn, int from_sq, int to_sq, int is_ep);

/* Phase-3 step 1: move-ordering state (reset per search). history is
 * [color][from<<6|to]; killers[ply] and counter[prev_from<<6|prev_to] hold a
 * move's 15-bit key (from|to<<6|promo<<12). A real move key is never 0
 * (from==to is illegal), so 0 doubles as the "empty" sentinel.
 * __thread (Lazy SMP): each search thread keeps its OWN ordering state --
 * only the TT is shared between threads. Zero-initialised per new thread;
 * the main thread's copy is reset by cs_search_begin exactly as before. */
static __thread int      g_history[2][4096];
static __thread uint32_t g_killers[CS_MAXPLY][2];
static __thread uint32_t g_counter[4096];

/* Q-01: continuation history (v30 #1.6, deferred at phase-3 step 1 and
 * never landed). Quiet ordering adds two context scores on top of butterfly
 * history: g_cont1 keyed by the PREVIOUS move (the opponent move that led
 * here), g_cont2 by the move TWO back (our own previous move); both indexed
 * by (mover_pt<<6 | to) of predecessor and candidate -- the compact
 * piece-to form (448x448 int16 per table, ~800KB __thread each) instead of
 * v30's sparse from-to dicts. g_ctx[ply] holds the (pt<<6|to) of the move
 * that ENTERED ply (0 = none: root, null-move children). Same gravity rule
 * and HIST_MAX as butterfly history; updated at quiet beta cutoffs with the
 * same malus sweep. Band check: |history + cont1 + cont2| <= 3*16384 --
 * still far inside the quiet band (< ORD_COUNTER 700k, > ORD_BADCAP).
 * Deviations from v30 (documented): root context starts empty (v30 seeds
 * the real previous game move); qsearch ordering reads no cont scores
 * (g_ctx is only maintained by negamax, and captures dominate there).
 * set_cont_hist(0) restores v36's search node-exactly. */
static int g_cont_hist = 1;
void set_cont_hist(int v) { g_cont_hist = v; }
#define CTX_N 448                       /* (pt 1..6)<<6 | to; 0 = none */
static __thread int16_t g_cont1[CTX_N][CTX_N];
static __thread int16_t g_cont2[CTX_N][CTX_N];
static __thread uint16_t g_ctx[CS_MAXPLY + 8];

static inline void cont_update(int16_t* row, int key, int bonus)
{
    int h = row[key];
    int ab = bonus < 0 ? -bonus : bonus;
    row[key] = (int16_t)(h + bonus - h * ab / HIST_MAX);
}

/* 0 = plain MVV-LVA baseline (for value-identity verification), 1 = full. */
static int g_order_mode = 1;
void set_order_mode(int m) { g_order_mode = m; }

#define ORD_TT       2000000
#define ORD_CAPTURE  1000000
#define ORD_KILLER0   900000
#define ORD_KILLER1   800000
#define ORD_COUNTER   700000
#define ORD_BADCAP   (-900000)

/* --- Phase-3 step 2: C-array transposition table ---------------------- *
 * Fixed-size, always-allocated, 24-byte entries; depth-preferred replace;
 * ply-relative mate encoding. Position key is an O(1) mix hash of the board
 * state (the 6 piece bitboards fully define piece placement; occ[WHITE]
 * splits colour; + castling/turn/ep). The full key is stored and checked on
 * probe, so a hash collision is rejected, never trusted. */
#include <stdlib.h>
#define TT_BITS 21
#define TT_SIZE (1u << TT_BITS)
#define TT_MASK (TT_SIZE - 1u)
#define TT_EXACT 0
#define TT_LOWER 1
#define TT_UPPER 2
#define MATE_THRESH (CS_INF - 1000)

/* Lockless-SMP entry format (24 bytes): the stored key is XOR-folded with
 * both data words, so a TORN write from a racing thread (Lazy SMP shares
 * this table with no locks) fails the probe's key reconstruction and reads
 * as a miss instead of corrupt data -- the classic Stockfish scheme. In
 * single-thread use the encoding is transparent (same semantics as the
 * plain struct it replaced). */
typedef struct {
    uint64_t key_x;     /* key ^ d1 ^ d2 */
    uint64_t d1;        /* value (low 32, signed) | move15 << 32 */
    uint64_t d2;        /* depth (low 16, signed) | flag << 16 | gen << 32 */
} TTEntry;

#define TT_VALUE(e)  ((int)(int32_t)(uint32_t)((e).d1))
#define TT_MOVE(e)   ((uint32_t)((e).d1 >> 32))
#define TT_DEPTH(e)  ((int)(int16_t)(uint16_t)((e).d2))
#define TT_FLAG(e)   ((int)(uint16_t)((e).d2 >> 16))
#define TT_GEN(e)    ((int)(uint16_t)((e).d2 >> 32))

static TTEntry* g_tt = NULL;
static int g_use_tt = 1;
static int g_gen = 0;       /* step 6: bumped per root search; old-gen entries
                             * are freely replaceable (TT now PERSISTS across
                             * ID iterations and moves; see cs_tt_reset). */
void set_use_tt(int v) { g_use_tt = v; }

void cs_tt_reset(void)
{
    if (g_tt) memset(g_tt, 0, TT_SIZE * sizeof(TTEntry));
    g_gen = 0;
}

static inline void tt_store_raw(TTEntry* t, uint64_t key, int value,
                                uint32_t move, int depth, int flag)
{
    uint64_t d1 = (uint64_t)(uint32_t)value | ((uint64_t)move << 32);
    uint64_t d2 = (uint64_t)(uint16_t)depth
                | ((uint64_t)(uint16_t)flag << 16)
                | ((uint64_t)(uint16_t)g_gen << 32);
    t->d1 = d1; t->d2 = d2; t->key_x = key ^ d1 ^ d2;
}

/* Snapshot the slot and reconstruct its key; 0 = miss (or torn write). */
static inline int tt_load(const TTEntry* t, uint64_t key, TTEntry* out)
{
    TTEntry e = *t;
    if ((e.key_x ^ e.d1 ^ e.d2) != key) return 0;
    *out = e;
    return 1;
}

/* EP-01: FIDE-exact ep in the position hash -- DORMANT, default OFF.
 * board_key mixes the RAW ep square, which is set after EVERY double push;
 * but per FIDE (and python-chess's _transposition_key, which the match
 * arbiter's threefold claims use) the ep right is part of the position only
 * if an ep capture is actually LEGAL. A phantom ep therefore splits one
 * real position across two keys: repetitions can be MISSED (g_path/g_hist
 * compare keys) and TT sharing is needlessly split. With the filter on, ep
 * hashes only when some own pawn can legally play the ep capture -- exactly
 * has_legal_en_passant. cs_board_key shares this path, so the driver's
 * game-history keys stay consistent with the search either way.
 * DEFAULT OFF: set_ep_filter(0) is node-exact with v34, and P-04's A/B vs
 * v34 is still in flight -- flipping this changes every tree, so it queues
 * for its own A/B at the next boundary (correctness-positive: strictly more
 * accurate repetition detection, strictly more TT sharing). */
static int g_ep_filter = 0;
void set_ep_filter(int v) { g_ep_filter = v; }

/* Does the ep right grant at least one LEGAL move? Mirrors gen_legal's ep
 * block exactly (same occupancy guard, same capturer set, same legal()). */
static int ep_grants_move(const Board* b)
{
    if (b->ep < 0 || ((1ULL << b->ep) & (b->occ[0] | b->occ[1]))) return 0;
    int us = b->turn, them = us ^ 1;
    for (uint64_t t = b->pawns & b->occ[us] & PAWN_ATT[them][b->ep];
         t; t &= t - 1)
        if (legal(b, __builtin_ctzll(t), b->ep, 1)) return 1;
    return 0;
}

static inline uint64_t board_key(const Board* b)
{
    int ep = (g_ep_filter && b->ep >= 0 && !ep_grants_move(b)) ? -1 : b->ep;
    uint64_t h = 0x9E3779B97F4A7C15ULL, x;
    #define MIX(v) x = (v); h ^= x; h *= 0xFF51AFD7ED558CCDULL; h ^= h >> 29;
    MIX(b->pawns) MIX(b->knights) MIX(b->bishops)
    MIX(b->rooks) MIX(b->queens) MIX(b->kings)
    MIX(b->occ[WHITE]) MIX(b->castling)
    MIX((uint64_t)b->turn | ((uint64_t)(ep + 1) << 8))
    #undef MIX
    return h;
}

static void order_moves(const Board* b, uint32_t* mv, int n, int ply,
                        uint32_t counter_key, uint32_t tt_move, int use_cont)
{
    int color = b->turn, full = g_order_mode;
    uint32_t k0 = g_killers[ply][0], k1 = g_killers[ply][1];
    int sc[256];
    for (int i = 0; i < n; i++) {
        uint32_t m = mv[i];
        int from = m & 63, to = (m >> 6) & 63;
        int victim = (m >> MV_SHIFT_VICTIM) & 7;
        int mover  = (m >> MV_SHIFT_MOVER) & 7;
        int s;
        if (full && tt_move && (m & 0x7FFF) == tt_move) {
            s = ORD_TT;                                /* TT move first */
        } else if (victim) {
            s = ORD_CAPTURE + victim * 100 - mover;   /* MVV-LVA */
            if (full && mover > victim) {              /* maybe losing -> SEE */
                int sv = see(b->pawns, b->knights, b->bishops, b->rooks,
                             b->queens, b->kings, b->occ[WHITE], b->occ[BLACK],
                             color, from, to, (m & MV_BIT_EP) ? 1 : 0);
                if (sv < 0) s = ORD_BADCAP + sv;
            }
        } else if (!full) {
            s = 0;                                     /* baseline: gen order */
        } else {
            uint32_t key = m & 0x7FFF;
            if      (key == k0)          s = ORD_KILLER0;
            else if (key == k1)          s = ORD_KILLER1;
            else if (key == counter_key) s = ORD_COUNTER;
            else {
                s = g_history[color][(from << 6) | to];
                if (use_cont && g_cont_hist) {       /* Q-01 */
                    int ck = (mover << 6) | to;
                    int p1 = g_ctx[ply];
                    if (p1) s += g_cont1[p1][ck];
                    if (ply >= 1) {
                        int p2 = g_ctx[ply - 1];
                        if (p2) s += g_cont2[p2][ck];
                    }
                }
            }
        }
        sc[i] = s;
    }
    for (int i = 1; i < n; i++) {   /* stable insertion sort, score desc */
        uint32_t xm = mv[i]; int xs = sc[i], j = i - 1;
        while (j >= 0 && sc[j] < xs) { mv[j+1]=mv[j]; sc[j+1]=sc[j]; j--; }
        mv[j+1] = xm; sc[j+1] = xs;
    }
}

/* gravity update toward +-HIST_MAX (same shape as the Python history tables) */
static inline void hist_update(int color, int fromto, int bonus)
{
    int *h = &g_history[color][fromto];
    int ab = bonus < 0 ? -bonus : bonus;
    *h += bonus - (*h) * ab / HIST_MAX;
}

/* --- P-23: staged move ordering ---------------------------------------- *
 * v35 generates + scores + sorts EVERY move at EVERY negamax node, but most
 * nodes cut off after the first move or two -- the rest of the generation,
 * SEE calls and sorting were pure waste. Staged emission produces the SAME
 * stream as order_moves' stable sort UNDER IDENTICAL STATE (class bands
 * never interleave: TT 2M > captures ~1M > killers 900k/800k > counter
 * 700k > quiet history |h|<=16384 > bad captures < -900k; ties break by
 * generation order, which each stage preserves -- proven by VERIFY mode
 * over ~1M nodes), but generates each class only when the search actually
 * reaches it: a TT-move cutoff never generates anything at all.
 * DELIBERATE TREE CHANGE vs v35: quiets are scored when their stage runs,
 * AFTER earlier subtrees mutated global history -- later stages see
 * FRESHER history than v35's node-entry snapshot, so live trees diverge
 * (often smaller). P-23 is therefore a search-behavior feature judged by
 * A/B, not a pure-speed change.
 * CONFIRMED into v36 (2026-07-10): +24.67 +/-6.8 over 10k @45+0.1 vs
 * Old Engine/35 (53.55%, pair ratio 1.39, norm +47.51) -- the second-
 * biggest single feature of the C era after P-14.
 * TT/killer/counter moves are validated by move_from_key (acceptance ==
 * gen_legal membership); killers/counter must reconstruct as QUIET, since
 * a capture with the same key was already emitted (and scored) as a
 * capture, exactly like v35's victim-first scoring.
 * set_staged: 0 = v35 monolithic path, 1 = staged (default),
 * 2 = VERIFY mode -- searches with the v35 path but builds the staged
 * stream at every eligible node and aborts on the first mismatch (the
 * strongest oracle: stream equality implies node identity). Staged engages
 * only at !in_chk, full-ordering, P-43-off nodes; others use v35's path. */
static int g_staged = 1;
void set_staged(int v) { g_staged = v; }

typedef struct {
    const Board* b;
    int ply;
    uint32_t counter_key, tt_key;
    int stage;                          /* 0 tt, 1 caps, 2 k0, 3 k1, 4 cnt,
                                         * 5 quiets, 6 badcaps, 7 done */
    uint32_t k0, k1;                    /* killer keys snapshot */
    uint32_t cap[192]; int csc[192]; int ncap, icap;   /* B-07: adversarial
    * gen_captures ceiling computes to ~128 exactly; 192 = headroom */
    uint32_t bad[192]; int bsc[192]; int nbad, ibad;
    uint32_t qt[256];  int qsc[256]; int nqt, iqt;
} Stager;

static void stager_init(Stager* st, const Board* b, int ply,
                        uint32_t counter_key, uint32_t tt_move)
{
    st->b = b; st->ply = ply;
    st->counter_key = counter_key;
    st->tt_key = tt_move & 0x7FFF;
    st->stage = 0;
    st->k0 = g_killers[ply][0]; st->k1 = g_killers[ply][1];
    st->ncap = st->icap = st->nbad = st->ibad = st->nqt = st->iqt = 0;
}

/* stable insertion sort of (mv, sc) pairs, score desc -- same comparator
 * and tie behavior as order_moves' sort, applied per class. */
static void stager_sort(uint32_t* mv, int* sc, int n)
{
    for (int i = 1; i < n; i++) {
        uint32_t xm = mv[i]; int xs = sc[i], j = i - 1;
        while (j >= 0 && sc[j] < xs) { mv[j+1]=mv[j]; sc[j+1]=sc[j]; j--; }
        mv[j+1] = xm; sc[j+1] = xs;
    }
}

static uint32_t stager_next(Stager* st)
{
    const Board* b = st->b;
    int color = b->turn;
    for (;;) {
        switch (st->stage) {
        case 0: {                                    /* TT move */
            st->stage = 1;
            uint32_t mv = move_from_key(b, st->tt_key);
            if (mv) return mv;
            break;
        }
        case 1: {                                    /* good captures */
            if (st->icap == 0 && st->ncap == 0) {    /* lazy generation */
                uint32_t raw[192];                   /* B-07 headroom */
                int n = gen_captures(b, raw);
                for (int i = 0; i < n; i++) {
                    uint32_t m = raw[i];
                    if ((m & 0x7FFF) == st->tt_key) continue;  /* emitted */
                    int from = m & 63, to = (m >> 6) & 63;
                    int victim = (m >> MV_SHIFT_VICTIM) & 7;
                    int mover  = (m >> MV_SHIFT_MOVER) & 7;
                    int s = ORD_CAPTURE + victim * 100 - mover;
                    if (mover > victim) {            /* maybe losing -> SEE */
                        int sv = see(b->pawns, b->knights, b->bishops,
                                     b->rooks, b->queens, b->kings,
                                     b->occ[WHITE], b->occ[BLACK],
                                     color, from, to, (m & MV_BIT_EP) ? 1 : 0);
                        if (sv < 0) {
                            st->bad[st->nbad] = m;
                            st->bsc[st->nbad++] = ORD_BADCAP + sv;
                            continue;
                        }
                    }
                    st->cap[st->ncap] = m;
                    st->csc[st->ncap++] = s;
                }
                stager_sort(st->cap, st->csc, st->ncap);
            }
            if (st->icap < st->ncap) return st->cap[st->icap++];
            st->stage = 2;
            break;
        }
        case 2: {                                    /* killer 0 */
            st->stage = 3;
            uint32_t k = st->k0;
            if (k && k != st->tt_key) {
                uint32_t mv = move_from_key(b, k);
                if (mv && !((mv >> MV_SHIFT_VICTIM) & 7)) return mv;
            }
            break;
        }
        case 3: {                                    /* killer 1 */
            st->stage = 4;
            uint32_t k = st->k1;
            if (k && k != st->tt_key && k != st->k0) {
                uint32_t mv = move_from_key(b, k);
                if (mv && !((mv >> MV_SHIFT_VICTIM) & 7)) return mv;
            }
            break;
        }
        case 4: {                                    /* counter move */
            st->stage = 5;
            uint32_t k = st->counter_key;
            if (k && k != st->tt_key && k != st->k0 && k != st->k1) {
                uint32_t mv = move_from_key(b, k);
                if (mv && !((mv >> MV_SHIFT_VICTIM) & 7)) return mv;
            }
            break;
        }
        case 5: {                                    /* quiets by history */
            if (st->iqt == 0 && st->nqt == 0) {      /* lazy generation */
                uint32_t raw[256];
                int n = gen_quiets(b, raw);
                for (int i = 0; i < n; i++) {
                    uint32_t m = raw[i];
                    uint32_t key = m & 0x7FFF;
                    if (key == st->tt_key || key == st->k0
                            || key == st->k1 || key == st->counter_key)
                        continue;                    /* already emitted */
                    int from = m & 63, to = (m >> 6) & 63;
                    int s = g_history[color][(from << 6) | to];
                    if (g_cont_hist) {               /* Q-01 (negamax-only) */
                        int ck = ((int)((m >> MV_SHIFT_MOVER) & 7) << 6) | to;
                        int p1 = g_ctx[st->ply];
                        if (p1) s += g_cont1[p1][ck];
                        if (st->ply >= 1) {
                            int p2 = g_ctx[st->ply - 1];
                            if (p2) s += g_cont2[p2][ck];
                        }
                    }
                    st->qt[st->nqt] = m;
                    st->qsc[st->nqt++] = s;
                }
                stager_sort(st->qt, st->qsc, st->nqt);
            }
            if (st->iqt < st->nqt) return st->qt[st->iqt++];
            st->stage = 6;
            break;
        }
        case 6: {                                    /* bad captures */
            if (st->ibad == 0 && st->nbad > 1)
                stager_sort(st->bad, st->bsc, st->nbad);
            if (st->ibad < st->nbad) return st->bad[st->ibad++];
            st->stage = 7;
            break;
        }
        default:
            return 0;
        }
    }
}

/* --- Phase-3 step 6: root-driver support -------------------------------- *
 * Time abort, game-history repetition, 50-move clock, insufficient material
 * and contempt draws -- everything the search needs from ROOT/GAME state,
 * fed per move by the Python driver (cengine.py) via cs_search_begin. */
#include <time.h>

static inline uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static uint64_t g_deadline = 0;         /* absolute ns; 0 = no time limit */
static volatile int g_abort = 0;        /* deadline / cs_stop: unwinds ALL threads */
static volatile int g_hstop = 0;        /* main root finished: helpers unwind */
static __thread int g_is_helper = 0;    /* set at helper-thread entry */

/* True while THIS thread must abandon its search: global abort (deadline /
 * cs_stop), or a Lazy-SMP helper whose main iteration finished. While
 * unwinding, every child value is garbage -- callers must discard it AND
 * must not store into the shared TT (a stopped helper that kept accepting
 * its children's 0s used to flood the TT with garbage entries at real
 * depths, poisoning every later search: 4-thread play missed forced mates
 * that 1-thread found instantly). */
#define CS_UNWINDING() (g_abort || (g_is_helper && g_hstop))

/* Node-entry poll (negamax + qsearch): every 4096 nodes check the clock.
 * At ~2.5M nps that is ~1.6 ms granularity -- finer than v30's poll.
 * Helpers additionally unwind when the main thread's iteration is done. */
#define CS_TIME_CHECK() do { \
        if ((g_nodes & 4095) == 0 && g_deadline && now_ns() >= g_deadline) \
            g_abort = 1; \
        if (CS_UNWINDING()) return 0; \
    } while (0)

/* Host-requested abort (UCI `stop`): same unwind path as the deadline. */
void cs_stop(void) { g_abort = 1; }

static uint64_t g_helper_nodes = 0;     /* Lazy-SMP helper node aggregate
                                         * (atomic adds on helper exit) */

/* Repetition state. g_path[ply] holds the board key of every negamax node on
 * the current line (g_path[0] = root); g_hist holds keys of game positions
 * BEFORE the root, most recent first, as far back as the root's halfmove
 * clock reaches (older positions can never recur). A position repeats if its
 * key appears an even number of plies back within the reversible window --
 * the first repetition scores as a contempt draw (v30's _path semantics).
 * g_path is per-thread (each SMP thread walks its own line); g_hist is
 * written once per move in cs_search_begin and read-only during search. */
#define CS_HIST_MAX 128
static __thread uint64_t g_path[CS_MAXPLY + 8];
static uint64_t g_hist[CS_HIST_MAX];
static int g_nhist = 0;

static int g_contempt = 50, g_draw_margin = 200;
void csearch_set_draw(int contempt, int margin)
{
    g_contempt = contempt; g_draw_margin = margin;
}

/* v30 ``use_simplify`` port, re-gated for the >=500cp re-test (the 200cp
 * original A/B'd -14 Elo in the Python era -- it traded into drawn endings;
 * at a decisive threshold that failure mode shrinks to nothing). threshold
 * 0 = off (the default; the term then cannot touch the eval). NOTE: mostly
 * invisible to ADJUDICATED matches (WDL calls wins near the same cp band);
 * its real surface is unadjudicated play -- odds vs Stockfish, GUI games. */
void csearch_set_simplify(int threshold, int weight)
{
    g_simp_thresh = threshold; g_simp_weight = weight;
}

/* Contempt-adjusted draw value, side-to-move view (port of _draw_score):
 * negative when stm is clearly ahead on material (avoid the draw), positive
 * when clearly behind (seek it). */
static int draw_score(const Board* b)
{
    int us = b->turn, them = us ^ 1;
    uint64_t mine = b->occ[us], theirs = b->occ[them];
    int diff = 0;
    diff += 100 * (__builtin_popcountll(b->pawns & mine)
                 - __builtin_popcountll(b->pawns & theirs));
    diff += 320 * (__builtin_popcountll(b->knights & mine)
                 - __builtin_popcountll(b->knights & theirs));
    diff += 330 * (__builtin_popcountll(b->bishops & mine)
                 - __builtin_popcountll(b->bishops & theirs));
    diff += 500 * (__builtin_popcountll(b->rooks & mine)
                 - __builtin_popcountll(b->rooks & theirs));
    diff += 900 * (__builtin_popcountll(b->queens & mine)
                 - __builtin_popcountll(b->queens & theirs));
    if (diff >= g_draw_margin)  return -g_contempt;
    if (diff <= -g_draw_margin) return g_contempt;
    return 0;
}

/* Insufficient material; caller guarantees no pawns/rooks/queens anywhere
 * (v30's cheap pre-filter). Port of python-chess's per-colour rule under
 * that pre-filter. */
static int insufficient_material(const Board* b)
{
    const uint64_t DARK = 0xAA55AA55AA55AA55ULL;
    for (int c = 0; c < 2; c++) {
        uint64_t occ = b->occ[c];
        if (b->knights & occ) {              /* K+N max, vs bare king only */
            if (__builtin_popcountll(occ) > 2) return 0;
            if (b->occ[c ^ 1] & ~b->kings)     return 0;
        } else if (b->bishops & occ) {       /* all bishops one colour, no N */
            if (b->knights)                    return 0;
            if ((b->bishops & DARK) && (b->bishops & ~DARK)) return 0;
        }
    }
    return 1;
}

/* Repetition scan for the node at `ply` with key `key` and halfmove clock
 * `hmc`: same-side positions 4, 6, ... plies back, first through the search
 * path, then on into the game history. */
static inline int is_repetition(uint64_t key, int ply, int hmc)
{
    for (int k = 4; k <= hmc; k += 2) {
        uint64_t past;
        if (k <= ply)                        past = g_path[ply - k];
        else if (k - ply - 1 < g_nhist)      past = g_hist[k - ply - 1];
        else                                 break;
        if (past == key) return 1;
    }
    return 0;
}

/* --- Phase-3 step 3: pruning ------------------------------------------ */
#include <math.h>
static int g_prune = 1;                 /* 0 = no pruning (verification) */
void set_prune(int v) { g_prune = v; }

/* P-03: Internal Iterative Reduction. A node with meaningful depth but NO
 * TT move has poor ordering ahead of it -- search it one ply shallower;
 * the TT-fed revisit (same key, now with a move) gets the full depth.
 * Toggle for the A/B vs the frozen Old Engine/31 baseline. */
static int g_iir = 1;
void set_iir(int v) { g_iir = v; }
#define IIR_MIN_DEPTH 4

/* P-01: check extensions -- a move that gives check gets +1 ply, drawn from
 * a per-line budget so a perpetual-check line can't explode the tree (v30's
 * MAX_CHECK_EXT recipe; the budget flows down the line, spent only when an
 * extension fires). LMR never touches these moves (it requires
 * !gives_check), so extension and reduction are mutually exclusive.
 * set_check_ext(0) restores v33's search node-exactly. */
static int g_check_ext = 1;
void set_check_ext(int v) { g_check_ext = v; }
#define CHECK_EXT_MAX 5
/* P-47: the budget itself is runtime-settable. 5 (the v30/CHECK_EXT_MAX
 * recipe) reproduces v36 node-exactly; the raise-to-8 candidate queues for
 * its own A/B (tree-changing: deeper check lines). */
static int g_check_ext_budget = CHECK_EXT_MAX;
void set_check_ext_budget(int v)
{
    g_check_ext_budget = v < 0 ? 0 : (v > 32 ? 32 : v);
}

/* PV-01: triangular PV table -- the PV is collected DURING the search (each
 * PV node prepends its best move to its child's line when the score lands
 * inside the window) instead of being reconstructed from the TT afterwards,
 * so the emitted PV can no longer be truncated/spliced by TT eviction.
 * Node-exact: pure bookkeeping at is_pv alpha-raises, zero search decisions
 * read it. Sized for qsearch's recursion guard (CS_MAXPLY + 60).
 *
 * PV-02 (set_pv_exact, default OFF, tree-changing -- own A/B): the remaining
 * truncation source is the TT itself, which cuts PV nodes off via EXACT hits
 * and bound-narrowing before their line is walked. pv_exact skips the whole
 * TT-cutoff block at PV nodes (the standard strong-engine rule; the TT move
 * is still used for ordering), making the collected PV complete end-to-end. */
#define PV_MAX (CS_MAXPLY + 62)
static __thread uint32_t g_pv[PV_MAX][PV_MAX];
static __thread uint8_t  g_pv_len[PV_MAX];
static __thread uint32_t g_root_pv[PV_MAX];
static __thread int      g_root_pv_len = 0;
static int g_pv_exact = 0;
void set_pv_exact(int v) { g_pv_exact = v ? 1 : 0; }

static inline void pv_store(int ply, uint32_t m)
{
    int cl = (ply + 1 < PV_MAX) ? g_pv_len[ply + 1] : 0;
    if (cl > PV_MAX - 1) cl = PV_MAX - 1;
    g_pv[ply][0] = m;
    memcpy(&g_pv[ply][1], g_pv[ply + 1], (size_t)cl * sizeof(uint32_t));
    g_pv_len[ply] = (uint8_t)(cl + 1);
}

/* Driver-side PV fetch: the last completed in-window root search's line,
 * as 15-bit move keys. Returns the number of moves written. */
int cs_get_pv(uint32_t* out, int maxn)
{
    int n = g_root_pv_len < maxn ? g_root_pv_len : maxn;
    for (int i = 0; i < n; i++) out[i] = g_root_pv[i] & 0x7FFF;
    return n;
}

/* P-43: single-reply / forced-move extension -- a node with exactly one legal
 * move is forced, so search that move one ply deeper. Own per-line budget,
 * separate from the check budget so neither starves the other (v30 keeps
 * these budgets apart too). A forced node has width 1, so the extension
 * deepens a single line without widening the tree -- inherently cheap; it can
 * stack with a check extension on the same move (still just one line).
 * set_single_reply(0) restores v34's search node-exactly.
 * A/B vs v34 (2026-07-09, 20k games pooled @45+0.1): +3.5 +/-4.8 -- positive
 * on every secondary signal but sub-significant even at 20k. KEPT-MARGINAL,
 * DORMANT (default OFF, user call): the mechanism is monotone-safe and may be
 * re-enabled/re-tested at a longer TC, but it does not earn default-on now. */
static int g_single_reply = 0;
void set_single_reply(int v) { g_single_reply = v; }
#define SR_EXT_MAX 5

/* P-04: "improving" heuristic (v30's exact recipe, engine.py ~3986-4310).
 * A per-thread eval stack records each ply's static eval on the way down;
 * `improving` = the side to move's static eval beat their own eval two plies
 * ago. Three uses, all gated so set_improving(0) restores v34 node-exactly:
 *   - RFP margin becomes RFP_MARGIN * (depth - improving): an improving node
 *     prunes one ply deeper for the same eval (not-improving == v34),
 *   - frontier futility margin widens by RFP_MARGIN/2 when NOT improving
 *     (a declining node cuts more frontier quiets; improving == v34),
 *   - LMR adds +1 to the reduction on quiets when NOT improving
 *     (improving == v34).
 * In-check plies record SEVAL_NONE; a missing ply-2 reference reads as
 * not-improving (v30's conservative default; its check-eval-proxy refinement
 * is NOT ported in v1 -- separate toggle if this pays). PV nodes compute the
 * static eval only while the toggle is on (v34 skips it there).
 * A/B vs v34 (2026-07-09, 10k @45+0.1): +0.38 +/-6.8, ptnml symmetric -- a
 * dead NULL despite -56% nodes / +1 ply of depth: at this TC the deeper
 * search saw nothing the shallower one didn't (the edge is the clock, not
 * the tree). DORMANT (default OFF); re-test only at a longer TC. */
static int g_improving = 0;
void set_improving(int v) { g_improving = v; }
#define SEVAL_NONE INT32_MIN
static __thread int g_seval[CS_MAXPLY];

/* P-26: runtime-tunable selectivity constants. Defaults are the shipped v34
 * values (formerly #defines, verified node-exact after the conversion);
 * chess-tuning-tools drives them through cuci.py's UCI options. The setters
 * are meant for engine startup / setoption time, not mid-search. */
static int g_rfp_margin   = 80;         /* per ply, reverse-futility */
static int g_rfp_depth    = 6;          /* RFP fires at depth <= this */
static int g_fut_margin   = 150;        /* frontier futility */
static int g_delta_margin = 200;        /* qsearch delta pruning */
static int g_lmp[4]       = {0, 6, 10, 14};       /* by depth 1..3 */
static int g_null_base    = 2;          /* null-move R = base + depth/div */
static int g_null_div     = 6;
static double g_lmr_div   = 2.0;        /* LMR = 0.75 + ln(d)*ln(m)/div */
void set_rfp(int margin, int depth)  { g_rfp_margin = margin; g_rfp_depth = depth; }
void set_fut_margin(int v)           { g_fut_margin = v; }
void set_delta_margin(int v)         { g_delta_margin = v; }
void set_lmp(int d1, int d2, int d3) { g_lmp[1] = d1; g_lmp[2] = d2; g_lmp[3] = d3; }
void set_null_move(int base, int divi) { g_null_base = base; g_null_div = divi > 0 ? divi : 6; }

static int g_lmr[64][64];
static int g_lmr_ready = 0;
static void init_lmr(void)
{
    for (int d = 1; d < 64; d++)
        for (int m = 1; m < 64; m++)
            g_lmr[d][m] = (int)(0.75 + log((double)d) * log((double)m) / g_lmr_div);
    g_lmr_ready = 1;
}
void set_lmr_div(int x100) { g_lmr_div = x100 / 100.0; init_lmr(); }

/* null move: pass the turn, clear the (single-move) ep right. */
static inline void make_null(Board* b) { b->turn ^= 1; b->ep = -1; }

/* side has a knight/bishop/rook/queen (null-move zugzwang guard). */
static inline int has_non_pawn(const Board* b, int side)
{
    return (b->knights | b->bishops | b->rooks | b->queens) & b->occ[side] ? 1 : 0;
}

/* --- Phase-3 step 4: quiescence --------------------------------------- *
 * Resolve noisy moves (captures + promotions) at the leaves so the static
 * eval isn't fooled by a pending exchange. Stand-pat, SEE-pruned losing
 * captures, delta pruning; when in check, search ALL evasions (never
 * stand-pat out of a mate). Fail-soft. */
static int g_qsearch = 1;
void set_qsearch(int v) { g_qsearch = v; }

/* P-22: qsearch generates noisy moves only (gen_noisy) instead of all legal
 * moves and skipping the quiets -- the hottest loop in the engine stops
 * paying for moves it never searches. NODE-IDENTICAL by construction (same
 * noisy subset, same relative order, stalemate semantics preserved via
 * has_legal_quiet); set_qgen(0) restores the full-gen code path. */
static int g_qgen = 1;
void set_qgen(int v) { g_qgen = v; }

/* P-46: lazy qsearch generation -- eval + stand-pat run BEFORE movegen, so
 * the many nodes that exit at stand-pat never pay for generation at all.
 * Needs P-22's gen_noisy/has_legal_quiet split (hence gated on g_qgen too);
 * value-identical at every node => node-identical trees, pure speed.
 * set_qs_lazy(0) restores the v35 order (gen first). */
static int g_qs_lazy = 1;
void set_qs_lazy(int v) { g_qs_lazy = v; }

/* P-44: quiescence TT probe/store. The node majority lives in qsearch, and
 * until now it never touched the transposition table: every qsearch node
 * recomputed the full static eval and re-resolved exchanges the warm P-14
 * table had already seen. Probe BEFORE movegen/eval (a hit skips the whole
 * node); any stored depth cuts here (negamax stores depth>=1, qsearch 0).
 * Stores go in at depth 0 with the gen-aware rule, so a qsearch entry can
 * never displace a same-key negamax entry (depth-preferred) -- it fills
 * empty/stale slots. The TT move also seeds qsearch ordering. Same
 * ply-relative mate encoding as negamax. set_qs_tt(0) restores v34's
 * search node-exactly.
 * CONFIRMED into v35 (2026-07-10): isolation A/B vs the P-22 base (both
 * sides equally fast) +8.06 +/-6.8 over 10k @45+0.1, CI clear of zero --
 * the persistent warm table across a game delivered what the flat
 * cold-ladder time-to-depth bench could not show. */
static int g_qs_tt = 1;
void set_qs_tt(int v) { g_qs_tt = v; }

static inline void qs_tt_store(uint64_t key, int val, int ply, uint32_t move,
                               int flag)
{
    TTEntry* t = &g_tt[key & TT_MASK];
    TTEntry cur = *t;
    uint64_t ck = cur.key_x ^ cur.d1 ^ cur.d2;
    int replace = (ck == key)
                ? (TT_DEPTH(cur) <= 0)
                : (TT_GEN(cur) != (int)(uint16_t)g_gen || TT_DEPTH(cur) <= 0);
    if (!replace) return;
    int sv = val;
    if (sv >= MATE_THRESH) sv += ply;                /* node -> ply-relative */
    else if (sv <= -MATE_THRESH) sv -= ply;
    tt_store_raw(t, key, sv, move & 0x7FFF, 0, flag);
}

static int qsearch(Board* b, int alpha, int beta, int ply, int in_chk)
{
    g_nodes++;
    g_pv_len[ply] = 0;     /* PV-01: every exit path leaves a valid (empty)
                            * line -- a stale slot would splice wrong moves
                            * into the parent's PV. ply < PV_MAX: the guard
                            * below caps recursion at CS_MAXPLY + 60. */
    CS_TIME_CHECK();
    if (in_chk < 0) in_chk = in_check(b);
    if (ply >= CS_MAXPLY + 60)                       /* hard recursion guard */
        return in_chk ? 0 : eval_full_stm(b);
    int is_pv = (beta - alpha) > 1;

    /* P-44: TT probe -- before movegen AND eval, so a hit costs nothing. */
    int alpha_orig = alpha;
    uint64_t key = 0;
    uint32_t tt_move = 0;
    int use_qtt = g_use_tt && g_qs_tt && g_tt != NULL;
    if (use_qtt) {
        key = board_key(b);
        TTEntry e;
        if (tt_load(&g_tt[key & TT_MASK], key, &e)) {
            int v = TT_VALUE(e);                     /* ply-relative -> node */
            if (v >= MATE_THRESH) v -= ply;
            else if (v <= -MATE_THRESH) v += ply;
            int fl = TT_FLAG(e);
            if (!(g_pv_exact && is_pv)) {            /* PV-02: PV nodes walk on */
                if (fl == TT_EXACT) return v;
                if (fl == TT_LOWER && v >= beta) return v;
                if (fl == TT_UPPER && v <= alpha) return v;
            }
            tt_move = TT_MOVE(e);
        }
    }

    int color = b->turn, best, stand = 0;
    uint32_t moves[256];
    int n;
    if (in_chk) {
        n = gen_legal(b, moves);                     /* full evasions */
        if (n == 0) return -CS_INF + ply;            /* checkmate */
        best = -CS_INF;
    } else if (g_qs_lazy && g_qgen) {
        /* P-46: eval + stand-pat BEFORE generation -- a large share of
         * qsearch nodes exit right here, and their movegen was pure waste.
         * Stalemate semantics preserved exactly: before returning stand we
         * confirm a legal move exists (early-exit quiet scan first -- the
         * common instant hit -- then the noisy list for locked positions);
         * no legal move at all is still a 0 draw, never an eval.
         * VALUE-IDENTICAL to the v35 path at every node => node-identical. */
        stand = eval_full_stm(b);
        if (stand >= beta) {                         /* fail-soft stand-pat */
            if (has_legal_quiet(b) || gen_noisy(b, moves) > 0) {
                if (use_qtt && !CS_UNWINDING())      /* P-44: cache the cutoff */
                    qs_tt_store(key, stand, ply, 0, TT_LOWER);
                return stand;
            }
            return 0;                                /* stalemate: draw, not eval */
        }
        n = gen_noisy(b, moves);
        if (n == 0 && !has_legal_quiet(b))
            return 0;                                /* stalemate: draw, not eval */
        if (stand > alpha) alpha = stand;
        best = stand;
    } else {
        /* P-22: noisy-only generation; stalemate still detected BEFORE the
         * stand-pat return (empty noisy list + no legal quiet = stalemate),
         * exactly like the full-gen n==0 test it replaces. */
        n = g_qgen ? gen_noisy(b, moves) : gen_legal(b, moves);
        if (n == 0 && (!g_qgen || !has_legal_quiet(b)))
            return 0;                                /* stalemate: draw, not eval */
        stand = eval_full_stm(b);
        if (stand >= beta) {                         /* fail-soft stand-pat */
            if (use_qtt && !CS_UNWINDING())          /* P-44: cache the cutoff */
                qs_tt_store(key, stand, ply, 0, TT_LOWER);
            return stand;
        }
        if (stand > alpha) alpha = stand;
        best = stand;
    }

    uint32_t bm = 0;                                 /* P-44: best move found */
    order_moves(b, moves, n, ply < CS_MAXPLY ? ply : 0, 0, tt_move, 0);
    for (int i = 0; i < n; i++) {
        uint32_t m = moves[i];
        int victim   = (m >> MV_SHIFT_VICTIM) & 7;
        int is_promo = (m >> 12) & 7;
        if (!in_chk) {
            if (!victim && !is_promo) continue;      /* quiets: not in qsearch */
            if (victim && !is_promo) {               /* pure capture */
                /* Q-02: SEE only when the mover outranks the victim -- with
                 * mover <= victim the worst case after the recapture is
                 * victim - mover >= 0, so SEE can never be negative and the
                 * skip below can never fire. Node-identical; the ordering's
                 * SEE (order_moves / Stager) uses the same gate. */
                int mover = (m >> MV_SHIFT_MOVER) & 7;
                if (mover > victim) {
                    int from = m & 63, to = (m >> 6) & 63;
                    int sv = see(b->pawns, b->knights, b->bishops, b->rooks,
                                 b->queens, b->kings, b->occ[WHITE], b->occ[BLACK],
                                 color, from, to, (m & MV_BIT_EP) ? 1 : 0);
                    if (sv < 0) continue;            /* skip losing captures */
                }
                if (stand + PIECE_VAL[victim] + g_delta_margin <= alpha)
                    continue;                        /* delta pruning */
            }
        }
        Board c = *b;
        apply_move(&c, m);
        int v = -qsearch(&c, -beta, -alpha, ply + 1, in_check(&c));
        if (CS_UNWINDING()) return 0;                /* v is garbage: unwind */
        if (is_pv && v > alpha && v < beta)          /* PV-01: in-window best */
            pv_store(ply, m);
        if (v > best) { best = v; bm = m; }
        if (v > alpha) alpha = v;
        if (alpha >= beta) break;
    }
    /* P-44: store the resolved node (bm==0 when stand-pat stayed best). */
    if (use_qtt && !CS_UNWINDING()) {
        int flag = (best <= alpha_orig) ? TT_UPPER
                 : (best >= beta)       ? TT_LOWER : TT_EXACT;
        qs_tt_store(key, best, ply, bm, flag);
    }
    return best;
}

static int negamax(Board* b, int depth, int alpha, int beta, int ply,
                   uint32_t prev12, int in_chk, int hmc, int chk, int srb)
{
    g_nodes++;
    g_pv_len[ply] = 0;     /* PV-01: see qsearch -- every exit path must
                            * leave a valid (empty) line. ply <= CS_MAXPLY
                            * here, well inside PV_MAX. */
    CS_TIME_CHECK();
    if (in_chk < 0) in_chk = in_check(b);

    /* Hard ply bound (BUG-03): memory safety must not depend on the
     * Python-side depth cap -- search_bench takes an uncapped depth, and
     * future extensions may push ply past the root depth. g_killers is
     * sized [CS_MAXPLY] and g_path [CS_MAXPLY+8]; stop strictly below. */
    if (ply >= CS_MAXPLY)
        return in_chk ? 0 : eval_full_stm(b);

    /* --- step 6: game-state draws (v30 order: before the TT probe) --- */
    if (!(b->pawns | b->rooks | b->queens) && insufficient_material(b))
        return draw_score(b);
    if (hmc >= 100 && !in_chk)               /* 50-move (in check: play on --
                                              * the mate/stalemate result of
                                              * the position takes priority) */
        return draw_score(b);
    uint64_t key = board_key(b);
    g_path[ply] = key;
    if (hmc >= 4 && is_repetition(key, ply, hmc))
        return draw_score(b);

    if (depth <= 0)
        return g_qsearch ? qsearch(b, alpha, beta, ply, in_chk)
                         : eval_full_stm(b);

    /* --- TT probe -------------------------------------------------- */
    uint32_t tt_move = 0;
    TTEntry* tte = NULL;
    if (g_use_tt) {
        tte = &g_tt[key & TT_MASK];
        TTEntry e;
        if (tt_load(tte, key, &e)) {
            tt_move = TT_MOVE(e);
            /* PV-02: at PV nodes skip the whole cutoff/narrowing block (the
             * EXACT return AND the bound-narrowing both truncate the
             * collected PV); the TT move above still orders. */
            if (TT_DEPTH(e) >= depth
                    && !(g_pv_exact && (beta - alpha) > 1)) {
                int v = TT_VALUE(e);                /* ply-relative -> node */
                if (v >= MATE_THRESH) v -= ply;
                else if (v <= -MATE_THRESH) v += ply;
                if (TT_FLAG(e) == TT_EXACT) return v;
                if (TT_FLAG(e) == TT_LOWER && v > alpha) alpha = v;
                else if (TT_FLAG(e) == TT_UPPER && v < beta) beta = v;
                if (alpha >= beta) return v;
            }
        }
    }
    int alpha_orig = alpha;                          /* AFTER the TT narrowing */
    int is_pv = (beta - alpha) > 1;

    /* P-03: IIR -- no TT move here, so ordering is blind; go shallower.
     * (Not in check: reduced-depth evasion search is a tactical risk.) */
    if (g_iir && depth >= IIR_MIN_DEPTH && !tt_move && !in_chk)
        depth--;

    /* static eval (for pruning); meaningless in check, unused at PV nodes
     * (P-04 additionally computes it at PV nodes to feed the eval stack). */
    int static_eval = (!in_chk && (!is_pv || g_improving)) ? eval_full_stm(b) : 0;

    /* P-04: record this ply's eval and compare to our own two plies ago.
     * Every ancestor on the current path wrote its slot on the way down, so
     * ply-2 is always fresh; the root loop writes g_seval[0]. */
    int improving = 0;
    if (g_improving) {
        g_seval[ply] = in_chk ? SEVAL_NONE : static_eval;
        if (!in_chk && ply >= 2 && g_seval[ply - 2] != SEVAL_NONE)
            improving = static_eval > g_seval[ply - 2];
    }

    /* --- pre-move pruning (non-PV, not in check) ------------------- */
    if (g_prune && !is_pv && !in_chk && abs(beta) < MATE_THRESH) {
        /* reverse futility / static null-move (P-04: an improving node
         * prunes one ply deeper for the same eval; off/not-improving = v34) */
        if (depth <= g_rfp_depth && static_eval - g_rfp_margin * (depth - improving) >= beta)
            return static_eval;
        /* null-move pruning (hmc 0 below the null: repetition/50-move
         * cannot be tracked across a non-move, so disable them there) */
        if (depth >= 3 && static_eval >= beta && has_non_pawn(b, b->turn)) {
            int R = g_null_base + depth / g_null_div;
            Board c = *b; make_null(&c);
            g_ctx[ply + 1] = 0;              /* Q-01: null = no context */
            int ns = -negamax(&c, depth - 1 - R, -beta, -beta + 1, ply + 1,
                              0xFFFFFFFF, 0, 0, chk, srb);
            if (CS_UNWINDING()) return 0;            /* ns is garbage */
            if (ns >= beta) return beta;
        }
    }

    uint32_t counter_key = (prev12 != 0xFFFFFFFF) ? g_counter[prev12] : 0;

    /* P-23: staged ordering engages at not-in-check, full-ordering nodes
     * with P-43 off (single-reply needs the total move count up front);
     * everything else keeps the v35 generate-all path. */
    int staged = (g_staged == 1 && g_order_mode == 1 && !in_chk
                  && !g_single_reply);
    Stager st;
    uint32_t moves[256];
    int n = 0, sr_ext = 0;
    if (staged) {
        stager_init(&st, b, ply, counter_key, tt_move);
    } else {
        n = gen_legal(b, moves);
        if (n == 0)
            return in_chk ? -CS_INF + ply : 0;       /* ply-relative mate */
        /* P-43: single-reply extension -- node-level, fires when this node
         * has exactly one legal move (spends from the srb budget). */
        sr_ext = (g_single_reply && n == 1 && srb > 0) ? 1 : 0;

        if (g_staged == 2 && g_order_mode == 1 && !in_chk && !g_single_reply) {
            /* VERIFY mode: the staged stream must equal order_moves' sorted
             * array move-for-move at every eligible node. */
            uint32_t ref[256];
            for (int i = 0; i < n; i++) ref[i] = moves[i];
            order_moves(b, ref, n, ply, counter_key, tt_move, 1);
            Stager vs;
            stager_init(&vs, b, ply, counter_key, tt_move);
            for (int i = 0; i < n; i++) {
                uint32_t sm = stager_next(&vs);
                if (sm != ref[i]) {
                    fprintf(stderr, "P-23 VERIFY MISMATCH ply=%d i=%d/%d "
                            "staged=%08x ref=%08x\n", ply, i, n, sm, ref[i]);
                    abort();
                }
            }
            if (stager_next(&vs) != 0) {
                fprintf(stderr, "P-23 VERIFY: staged stream longer than "
                        "gen_legal (n=%d)\n", n);
                abort();
            }
        }
        order_moves(b, moves, n, ply, counter_key, tt_move, 1);
    }

    int color = b->turn, best = -CS_INF;
    uint32_t best_move = 0;
    uint32_t quiets[256]; uint16_t quiets_ck[256]; int nq = 0;
    int lmp_lim = (g_prune && !is_pv && !in_chk && depth <= 3) ? g_lmp[depth] : 999;
    for (int i = 0; ; i++) {
        uint32_t m;
        if (staged) {
            m = stager_next(&st);
            if (!m) break;
        } else {
            if (i >= n) break;
            m = moves[i];
        }
        if (i == 0) best_move = m;
        int victim = (m >> MV_SHIFT_VICTIM) & 7;
        int mover  = (m >> MV_SHIFT_MOVER) & 7;
        int fromto = (m & 63) << 6 | ((m >> 6) & 63);
        int quiet  = !victim && !((m >> 12) & 7);    /* not capture/promo */
        int child_hmc = (victim || mover == PT_PAWN) ? 0 : hmc + 1;

        if (quiet && best > -MATE_THRESH && nq >= lmp_lim)
            continue;                                /* late-move pruning */

        Board c = *b;
        apply_move(&c, m);
        g_ctx[ply + 1] = (uint16_t)((mover << 6) | ((m >> 6) & 63));  /* Q-01 */
        int gives_check = in_check(&c);

        if (g_prune && quiet && !is_pv && !in_chk && !gives_check && depth == 1
                && best > -MATE_THRESH
                && static_eval + g_fut_margin
                   + ((g_improving && !improving) ? g_rfp_margin / 2 : 0) <= alpha)
            continue;    /* frontier futility (P-04: declining node cuts more) */

        if (quiet) {
            quiets_ck[nq] = (uint16_t)((mover << 6) | ((m >> 6) & 63));
            quiets[nq++] = fromto;
        }

        /* P-01 check extension (never combines with LMR: R needs !gives_check)
         * + P-43 single-reply (node-level; can stack, but n==1 => one line). */
        int ext = (g_check_ext && gives_check && chk > 0) ? 1 : 0;
        int nd = depth - 1 + ext + sr_ext;
        int child_chk = chk - ext;
        int child_srb = srb - sr_ext;

        /* late-move reduction on quiet, late, non-checking moves */
        int R = 0;
        if (g_prune && depth >= 3 && i >= 3 && quiet && !in_chk && !gives_check) {
            R = g_lmr[depth < 64 ? depth : 63][i < 64 ? i : 63];
            if (is_pv && R) R--;
            if (g_improving && !improving) R++;      /* P-04: sharpen declining lines */
            if (R > depth - 2) R = depth - 2;
            if (R < 0) R = 0;
        }

        uint32_t cp = (ply + 1 < CS_MAXPLY) ? (uint32_t)fromto : 0xFFFFFFFF;
        int v;
        if (i == 0) {
            v = -negamax(&c, nd, -beta, -alpha, ply + 1, cp, gives_check, child_hmc, child_chk, child_srb);
        } else {                                     /* PVS scout (reduced) */
            v = -negamax(&c, nd - R, -alpha - 1, -alpha, ply + 1, cp, gives_check, child_hmc, child_chk, child_srb);
            if (R && v > alpha)                      /* reduced scout beat alpha */
                v = -negamax(&c, nd, -alpha - 1, -alpha, ply + 1, cp, gives_check, child_hmc, child_chk, child_srb);
            if (v > alpha && v < beta)               /* full-window PV re-search */
                v = -negamax(&c, nd, -beta, -alpha, ply + 1, cp, gives_check, child_hmc, child_chk, child_srb);
        }
        if (CS_UNWINDING()) return 0;                /* v is garbage: unwind */
        if (is_pv && v > alpha && v < beta)          /* PV-01: in-window best;
                                                      * the last (re)search was
                                                      * full-window, so the
                                                      * child line is fresh */
            pv_store(ply, m);

        if (v > best) { best = v; best_move = m; }
        if (v > alpha) alpha = v;
        if (alpha >= beta) {                         /* fail-hard cutoff */
            if (quiet) {
                int bonus = depth * depth;
                hist_update(color, fromto, bonus);
                for (int q = 0; q < nq - 1; q++)
                    hist_update(color, quiets[q], -bonus);
                if (g_cont_hist) {                   /* Q-01: same rule */
                    int ck = (mover << 6) | ((m >> 6) & 63);
                    int p1 = g_ctx[ply];
                    int p2 = (ply >= 1) ? g_ctx[ply - 1] : 0;
                    if (p1) cont_update(g_cont1[p1], ck, bonus);
                    if (p2) cont_update(g_cont2[p2], ck, bonus);
                    for (int q = 0; q < nq - 1; q++) {
                        if (p1) cont_update(g_cont1[p1], quiets_ck[q], -bonus);
                        if (p2) cont_update(g_cont2[p2], quiets_ck[q], -bonus);
                    }
                }
                uint32_t kk = m & 0x7FFF;
                if (ply < CS_MAXPLY && g_killers[ply][0] != kk) {
                    g_killers[ply][1] = g_killers[ply][0];
                    g_killers[ply][0] = kk;
                }
                if (prev12 != 0xFFFFFFFF) g_counter[prev12] = kk;
            }
            break;
        }
    }

    /* P-23: on the staged path mate/stalemate is discovered by exhaustion --
     * no stage produced a single legal move. (best_move can only stay 0 with
     * zero moves streamed: the first streamed move is never skipped, since
     * LMP/futility both require best > -MATE_THRESH.) Return BEFORE the TT
     * store, exactly like the v35 n==0 path. */
    if (staged && best_move == 0)
        return in_chk ? -CS_INF + ply : 0;

    /* --- TT store: gen-aware depth-preferred, ply-relative mates ---- *
     * Same key: deeper-or-equal wins. Different key: an entry from an older
     * search generation is freely replaceable, a current-gen one only for
     * deeper-or-equal depth (the TT persists across ID iterations/moves).
     * Never store while unwinding (belt-and-braces: the loop returns before
     * reaching here, but a garbage store would poison EVERY later search
     * through the shared, persistent table). */
    if (g_use_tt && !CS_UNWINDING()) {
        TTEntry cur = *tte;
        uint64_t cur_key = cur.key_x ^ cur.d1 ^ cur.d2;
        int replace = (cur_key == key)
                    ? (TT_DEPTH(cur) <= depth)
                    : (TT_GEN(cur) != (int)(uint16_t)g_gen
                       || TT_DEPTH(cur) <= depth);
        if (replace) {
            int flag = (best <= alpha_orig) ? TT_UPPER
                     : (best >= beta)       ? TT_LOWER : TT_EXACT;
            int sv = best;
            if (sv >= MATE_THRESH) sv += ply;
            else if (sv <= -MATE_THRESH) sv -= ply;
            /* 0x7FFF, not 0xFFFF: bit 15 is the mover PT's low bit
             * (MV_SHIFT_MOVER = 15). Storing it made the probe-side
             * `(m & 0x7FFF) == tt_move` never match for odd mover PTs
             * (pawn/bishop/queen) -- TT-move ordering was silently dead
             * for those movers. */
            tt_store_raw(tte, key, sv, best_move & 0x7FFF, depth, flag);
        }
    }
    return best;
}

/* --- Phase-3 step 6: per-move / per-iteration entry points ------------- *
 * The Python driver (cengine.py) calls, per game move:
 *     cs_search_begin(history_keys, n, budget_seconds)     once
 *     cs_search_root(board..., depth, window, ...)         per ID iteration
 * cs_search_begin resets the per-move state exactly like v30 does (killers/
 * history/countermoves per move; TT persists -- the driver calls
 * cs_tt_reset() only after an irreversible root move), arms the deadline
 * and stores the game-history keys for repetition detection. */
void cs_search_begin(const uint64_t* hist, int nhist, double budget_sec)
{
    g_nodes = 0;
    g_abort = 0;
    g_deadline = (budget_sec > 0.0)
               ? now_ns() + (uint64_t)(budget_sec * 1e9) : 0;
    g_nhist = 0;
    if (hist) {
        if (nhist > CS_HIST_MAX) nhist = CS_HIST_MAX;
        for (int i = 0; i < nhist; i++) g_hist[i] = hist[i];
        g_nhist = nhist;
    }
    memset(g_history, 0, sizeof(g_history));
    memset(g_killers, 0, sizeof(g_killers));
    memset(g_counter, 0, sizeof(g_counter));
    memset(g_cont1, 0, sizeof(g_cont1));     /* Q-01: same per-move lifecycle */
    memset(g_cont2, 0, sizeof(g_cont2));
    memset(g_ctx, 0, sizeof(g_ctx));
    g_helper_nodes = 0;                  /* Lazy-SMP helper node aggregate */
    if (g_tt == NULL) {
        g_tt = (TTEntry*)calloc(TT_SIZE, sizeof(TTEntry));
        if (g_tt == NULL) {                  /* Q-13: degrade, don't segfault */
            fprintf(stderr, "csearch: TT calloc(%zu) failed -- searching "
                    "without a transposition table\n",
                    (size_t)TT_SIZE * sizeof(TTEntry));
            g_use_tt = 0;
        }
    }
    if (!g_lmr_ready) init_lmr();
    g_gen = (g_gen + 1) & 0x7FFF;        /* old entries become replaceable */
}

/* Root PVS body, shared by the main thread and the Lazy-SMP helpers: full
 * width inside [alpha, beta), no reductions or pruning (root moves are few
 * and important). Returns the best move's 15-bit key; *out_done counts
 * root moves fully searched (a stop mid-move leaves it short). */
static uint32_t root_search(const Board* rb, int depth, int alpha, int beta,
                            uint32_t prev_key, int hmc,
                            int* out_score, int* out_done)
{
    Board b = *rb;
    uint64_t key = board_key(&b);
    g_path[0] = key;
    g_root_pv_len = 0;     /* PV-01: a fail-low iteration leaves it empty --
                            * the driver falls back to the TT walk then */
    g_ctx[0] = 0;              /* Q-01: no game-prev context at root (v30
                                * seeds the real previous move; deviation) */
    /* P-04: seed the eval stack -- the ply-2 reference for ply-2 nodes.
     * Per-thread (__thread), and root_search is the shared entry for the
     * main thread and every SMP helper, so each thread seeds its own. */
    if (g_improving)
        g_seval[0] = in_check(&b) ? SEVAL_NONE : eval_full_stm(&b);
    int alpha_orig = alpha;

    uint32_t moves[256];
    int n = gen_legal(&b, moves);
    *out_done = 0;
    if (n == 0) {                        /* mate/stalemate at the root */
        *out_score = in_check(&b) ? -CS_INF : 0;
        return 0;
    }
    order_moves(&b, moves, n, 0, 0, prev_key, 1);

    int best = -CS_INF;
    uint32_t best_move = moves[0];
    for (int i = 0; i < n; i++) {
        uint32_t m = moves[i];
        int victim = (m >> MV_SHIFT_VICTIM) & 7;
        int mover  = (m >> MV_SHIFT_MOVER) & 7;
        int child_hmc = (victim || mover == PT_PAWN) ? 0 : hmc + 1;
        Board c = b;
        apply_move(&c, m);
        int gc = in_check(&c);
        g_ctx[1] = (uint16_t)((((m >> MV_SHIFT_MOVER) & 7) << 6)
                              | ((m >> 6) & 63));    /* Q-01 child context */
        uint32_t cp = (uint32_t)((m & 63) << 6 | ((m >> 6) & 63));
        int v;
        if (i == 0) {
            v = -negamax(&c, depth - 1, -beta, -alpha, 1, cp, gc, child_hmc, g_check_ext_budget, SR_EXT_MAX);
        } else {
            v = -negamax(&c, depth - 1, -alpha - 1, -alpha, 1, cp, gc, child_hmc, g_check_ext_budget, SR_EXT_MAX);
            if (v > alpha && v < beta)
                v = -negamax(&c, depth - 1, -beta, -alpha, 1, cp, gc, child_hmc, g_check_ext_budget, SR_EXT_MAX);
        }
        if (g_abort || (g_is_helper && g_hstop))
            break;                                   /* v is garbage */
        (*out_done)++;
        if (v > alpha && v < beta) {                 /* PV-01: root prepend */
            int cl = g_pv_len[1];
            g_root_pv[0] = m;
            memcpy(&g_root_pv[1], g_pv[1], (size_t)cl * sizeof(uint32_t));
            g_root_pv_len = cl + 1;
        }
        if (v > best) { best = v; best_move = m; }
        if (v > alpha) alpha = v;
        if (alpha >= beta) break;                    /* aspiration fail-high */
    }

    /* Root TT store (feeds the next iteration's ordering + the PV walk). */
    if (g_use_tt && !g_abort && !(g_is_helper && g_hstop) && *out_done > 0) {
        int flag = (best <= alpha_orig) ? TT_UPPER
                 : (best >= beta)       ? TT_LOWER : TT_EXACT;
        tt_store_raw(&g_tt[key & TT_MASK], key, best,
                     best_move & 0x7FFF, depth, flag);
    }
    *out_score = best;
    return best_move & 0x7FFF;   /* 15-bit move key: from|to<<6|promo<<12 */
}

/* --- Lazy SMP ----------------------------------------------------------- *
 * set_threads(N): each cs_search_root spawns N-1 helper pthreads running
 * the SAME root search (alternating depth / depth+1, full window), stopped
 * when the main thread's iteration completes. The only communication is
 * the shared lockless TT; every other piece of search state is __thread.
 * Helper results are discarded -- their value is the TT fill. */
#include <pthread.h>
static int g_threads = 1;
void set_threads(int n) { g_threads = (n < 1) ? 1 : (n > 64 ? 64 : n); }

typedef struct { Board b; int depth, hmc; uint32_t prev; } HelperArg;

static void* helper_entry(void* p)
{
    HelperArg* a = (HelperArg*)p;
    g_is_helper = 1;
    g_nodes = 0;
    int score, done;
    root_search(&a->b, a->depth, -CS_INF, CS_INF, a->prev, a->hmc,
                &score, &done);
    __atomic_fetch_add(&g_helper_nodes, g_nodes, __ATOMIC_RELAXED);
    return NULL;
}

/* One ID iteration: root PVS inside [alpha, beta), plus Lazy-SMP helpers
 * when set_threads(N>1). Returns the best move's 15-bit key. Outputs:
 * *out_score = fail-soft root score, *out_nodes = nodes since
 * cs_search_begin (all threads), *out_done = root moves fully searched,
 * *out_aborted = deadline/stop hit (if so, the returned move/score cover
 * only the *out_done completed moves -- the PV move is searched first, so
 * *out_done >= 1 makes the result usable: v30's partial-iteration rule).
 * `prev_key` = previous iteration's best move (or 0), ordered first.
 * `hmc` = the root's halfmove clock. */
uint32_t cs_search_root(uint64_t pawns, uint64_t knights, uint64_t bishops,
                        uint64_t rooks, uint64_t queens, uint64_t kings,
                        uint64_t occ_w, uint64_t occ_b,
                        int turn, int ep, uint64_t castling,
                        int depth, int alpha, int beta,
                        uint32_t prev_key, int hmc,
                        uint64_t* out_nodes, int* out_score,
                        int* out_done, int* out_aborted)
{
    Board b = make_board(pawns, knights, bishops, rooks, queens, kings,
                         occ_w, occ_b, turn, ep, castling);

    /* Helpers only pay off once the tree is non-trivial. */
    pthread_t tids[64];
    HelperArg args[64];
    int nh = (g_threads > 1 && depth >= 4) ? g_threads - 1 : 0;
    g_hstop = 0;
    /* BUG-05: darwin gives secondary threads a 512 KB stack (the main
     * thread gets 8 MB); a deep line's negamax+qsearch frames (~2-3 KB
     * each, moves[256]/quiets[256] buffers) get thin there. Match the
     * main thread. */
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setstacksize(&attr, 8u << 20);
    for (int i = 0; i < nh; i++) {
        args[i].b = b;
        args[i].depth = depth + (i & 1);   /* half at depth, half deeper */
        args[i].hmc = hmc;
        args[i].prev = prev_key;
        if (pthread_create(&tids[i], &attr, helper_entry, &args[i]) != 0) {
            nh = i;                        /* spawn failed: run what we got */
            break;
        }
    }
    pthread_attr_destroy(&attr);

    int score, done;
    uint32_t mv = root_search(&b, depth, alpha, beta, prev_key, hmc,
                              &score, &done);

    if (nh) {
        g_hstop = 1;
        for (int i = 0; i < nh; i++) pthread_join(tids[i], NULL);
        g_hstop = 0;
    }
    *out_done = done;
    *out_aborted = g_abort ? 1 : 0;
    *out_nodes = g_nodes + __atomic_load_n(&g_helper_nodes, __ATOMIC_RELAXED);
    *out_score = score;
    return mv;
}

/* Test export (BUG-06): the insufficient-material port decides DRAWS but
 * was outside the 3M eval oracle's coverage -- this exposes the exact
 * check negamax runs (pre-filter + port) for a differential vs
 * python-chess's is_insufficient_material. Not called by cengine (no abi
 * bump needed). */
int cs_insufficient_material(uint64_t pawns, uint64_t knights, uint64_t bishops,
                             uint64_t rooks, uint64_t queens, uint64_t kings,
                             uint64_t occ_w, uint64_t occ_b,
                             int turn, int ep, uint64_t castling)
{
    Board b = make_board(pawns, knights, bishops, rooks, queens, kings,
                         occ_w, occ_b, turn, ep, castling);
    if (b.pawns | b.rooks | b.queens) return 0;    /* negamax pre-filter */
    return insufficient_material(&b);
}

/* Board key export: the Python driver computes game-history keys with the
 * SAME hash the search uses (it cannot reproduce board_key itself). */
uint64_t cs_board_key(uint64_t pawns, uint64_t knights, uint64_t bishops,
                      uint64_t rooks, uint64_t queens, uint64_t kings,
                      uint64_t occ_w, uint64_t occ_b,
                      int turn, int ep, uint64_t castling)
{
    Board b = make_board(pawns, knights, bishops, rooks, queens, kings,
                         occ_w, occ_b, turn, ep, castling);
    return board_key(&b);
}

/* TT best-move probe (15-bit key, or 0): the driver's PV extraction. */
uint32_t cs_tt_probe_move(uint64_t pawns, uint64_t knights, uint64_t bishops,
                          uint64_t rooks, uint64_t queens, uint64_t kings,
                          uint64_t occ_w, uint64_t occ_b,
                          int turn, int ep, uint64_t castling)
{
    if (g_tt == NULL) return 0;
    Board b = make_board(pawns, knights, bishops, rooks, queens, kings,
                         occ_w, occ_b, turn, ep, castling);
    uint64_t key = board_key(&b);
    TTEntry e;
    return tt_load(&g_tt[key & TT_MASK], key, &e) ? (TT_MOVE(e) & 0x7FFF) : 0;
}

/* Exported: fixed-depth alpha-beta (compat wrapper kept for the NPS/verify
 * harnesses: fresh TT + per-move state, full window, no deadline). Returns
 * the best root move's 15-bit key; nodes/score via out-params. */
uint32_t search_bench(uint64_t pawns, uint64_t knights, uint64_t bishops,
                      uint64_t rooks, uint64_t queens, uint64_t kings,
                      uint64_t occ_w, uint64_t occ_b,
                      int turn, int ep, uint64_t castling,
                      int depth, uint64_t* out_nodes, int* out_score)
{
    cs_search_begin(NULL, 0, 0.0);
    if (g_tt) memset(g_tt, 0, TT_SIZE * sizeof(TTEntry));  /* fresh TT */
    int done, aborted;
    return cs_search_root(pawns, knights, bishops, rooks, queens, kings,
                          occ_w, occ_b, turn, ep, castling,
                          depth, -CS_INF, CS_INF, 0, 0,
                          out_nodes, out_score, &done, &aborted);
}

int csearch_abi(void) { return 6; }   /* 6 = cs_get_pv (PV-01) + set_pv_exact
                                       * + set_check_ext_budget; 5 = Lazy SMP
                                       * (set_threads) + cs_stop */
