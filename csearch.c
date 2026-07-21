/* csearch.c -- the C search core: the SHIPPED engine's entire per-node loop.
 * cengine.py is the Python root driver (iterative deepening, time policy,
 * book/TB, eval-param sync); cuci.py the UCI host. Everything per-node lives
 * here: the board layer (extracted from movegen.c, perft-verified), staged
 * move ordering, the lockless array TT (persistent + Lazy-SMP-shared),
 * pruning, quiescence and the FULL static eval -- a bit-exact port of
 * engine.py's _evaluate_static (differential-verified over 3M positions),
 * with every tunable synced from the live engine.py instance at startup.
 * Feature toggles carry their A/B verdicts inline below; the driver-side
 * summary is cengine.py's docstring.
 *
 * Build: ./setup.sh -- or directly (setup.sh picks -mcpu=native on ARM /
 * Apple Silicon, -march=native on x86):
 *   clang -O3 -mcpu=native -shared -fPIC -w -I. \
 *         -o csearch.so csearch.c eval_c.c Constants.c -lm -lpthread
 * (csearch.c single-TU-includes NNUE/nnue.c -- the FI-15 NNUE build-out,
 * dormant behind set_use_nnue/cengine.USE_NNUE, default 0 = byte-exact.)
 *
 * History: born 2026-07-08 as an isolated phase-1/2 prototype (roadmap
 * #29/#30) measuring the per-node NPS ceiling for the GO/NO-GO gate --
 * full-eval C alpha-beta ~13.5M nodes/s vs the Python engine's ~90k =
 * ~150x, GO -- and the shipped core ever since the phase-3 root driver
 * landed (Old Engine/31 on; strongest engine in the repo, 29-1-0 vs v30
 * on arrival). */

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

/* FI-01: Zobrist randoms -- the position key is maintained INCREMENTALLY on
 * the Board through apply_move/make_null (O(1) XORs) instead of the old
 * 9-MIX full-state hash recomputed at every node. Seeded once (splitmix64,
 * fixed seed => reproducible keys across runs/machines). */
static uint64_t Z_PSQ[2][7][64];   /* [color][pt 1..6][sq] */
static uint64_t Z_EP[65];          /* [ep+1]; Z_EP[0] (no ep) = 0 */
static uint64_t Z_CR[64];          /* per castling-rights rook-home square */
static uint64_t Z_TURN;

static uint64_t splitmix64(uint64_t* x)
{
    uint64_t z = (*x += 0x9E3779B97F4A7C15ULL);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return z ^ (z >> 31);
}

/* --- step-5 eval masks (built once alongside PAWN_ATT; ports of
 * engine.py's _build_pawn_masks) ------------------------------------------ */
static uint64_t FILE_BB8[8], ADJ_FILES[8];
static uint64_t PASSED_MASK[2][64];    /* enemy pawns that stop/guard a passer */
static uint64_t SUPPORT_MASK[2][64];   /* own pawns adjacent, at-or-behind */
static uint64_t STOPATK_MASK[2][64];   /* enemy pawns attacking the stop square */
static int CENTER_MANH[64];            /* centre Manhattan distance, 0..6 */

static void cuckoo_build(void);        /* FI-29: defined with the repetition
                                        * machinery (needs the sliders below);
                                        * called once Z_PSQ/Z_TURN are set. */

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
    uint64_t seed = 0x50594749/*PYGI*/ + 0x4E;
    for (int c = 0; c < 2; c++)
        for (int pt = 1; pt <= 6; pt++)
            for (int sq = 0; sq < 64; sq++)
                Z_PSQ[c][pt][sq] = splitmix64(&seed);
    Z_EP[0] = 0;                        /* no-ep contributes nothing */
    for (int i = 1; i < 65; i++) Z_EP[i] = splitmix64(&seed);
    for (int i = 0; i < 64; i++) Z_CR[i] = splitmix64(&seed);
    Z_TURN = splitmix64(&seed);
    cuckoo_build();                     /* FI-29: reversible-move deltas */
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
 *   bits 22-23 : FI-02.3 SEE-verdict tags (ordering computes, qsearch
 *                 reuses; every key consumer masks to 15 bits)
 *   bits 24-31 : reserved
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
    uint64_t key;             /* FI-01: Zobrist, maintained by apply_move */
} Board;

/* FI-01: full-state key computation -- the ORACLE for the incremental
 * update (make_board entry points + the ZKEY differential); the search
 * itself never calls this per node. Raw ep convention (EP-01's FIDE
 * filter is applied as an O(1) fixup in board_key). */
static uint64_t key_from_scratch(const Board* b)
{
    uint64_t k = (b->turn == WHITE) ? Z_TURN : 0;
    const uint64_t* bbs[7] = {0, &b->pawns, &b->knights, &b->bishops,
                              &b->rooks, &b->queens, &b->kings};
    for (int c = 0; c < 2; c++)
        for (int pt = 1; pt <= 6; pt++)
            for (uint64_t t = *bbs[pt] & b->occ[c]; t; t &= t - 1)
                k ^= Z_PSQ[c][pt][__builtin_ctzll(t)];
    for (uint64_t t = b->castling; t; t &= t - 1)
        k ^= Z_CR[__builtin_ctzll(t)];
    k ^= Z_EP[b->ep + 1];
    return k;
}

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
    b.key = key_from_scratch(&b);      /* FI-01: once per entry from Python */
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
static inline int attacked(int sq, uint64_t occ, int us,
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
static inline int legal(const Board* b, int from, int to, int is_ep)
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
static inline int sq_attacked_by_them(const Board* b, int sq)
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
static inline int in_check(const Board* b)
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

    /* FI-02.2: the mover PT is already packed in every move word this
     * function ever sees (gen_* and move_from_key all pack it; the three
     * call sites pass their words through) -- the old 5-branch bitboard
     * probe re-derived it per child, millions of times per second. */
    int movpt = (mv >> MV_SHIFT_MOVER) & 7;     /* 1=P 2=N 3=B 4=R 5=Q 6=K */

    uint64_t capmask = tb;
    if (movpt == 1 && to == b->ep && !(b->occ[them] & tb))
        capmask = 1ULL << ((us == WHITE) ? to - 8 : to + 8);

    /* FI-01: incremental Zobrist -- mirror every state mutation below with
     * its XOR. The victim PT rides in the move word (bits 18-20, packed by
     * every generator); the ZKEY differential is the correctness gate. */
    uint64_t zkey = b->key ^ Z_TURN;
    int victim = (mv >> MV_SHIFT_VICTIM) & 7;
    if (victim)
        zkey ^= Z_PSQ[them][victim][(capmask == tb)
                                    ? to : __builtin_ctzll(capmask)];

    uint64_t ncap = ~capmask;
    b->pawns &= ncap; b->knights &= ncap; b->bishops &= ncap;
    b->rooks &= ncap; b->queens &= ncap;
    b->occ[them] &= ncap;

    uint64_t nfrom = ~fb;
    b->pawns &= nfrom; b->knights &= nfrom; b->bishops &= nfrom;
    b->rooks &= nfrom; b->queens &= nfrom; b->kings &= nfrom;

    int finalpt = promo ? promo : movpt;
    zkey ^= Z_PSQ[us][movpt][from] ^ Z_PSQ[us][finalpt][to];   /* FI-01 */
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
        zkey ^= Z_PSQ[us][4][rf] ^ Z_PSQ[us][4][rt];           /* FI-01 */
    }

    uint64_t cr = b->castling;
    if (movpt == 6)
        cr &= (us == WHITE) ? ~((1ULL << 0) | (1ULL << 7))
                            : ~((1ULL << 56) | (1ULL << 63));
    cr &= ~fb;
    cr &= ~capmask;
    for (uint64_t crx = b->castling ^ cr; crx; crx &= crx - 1)
        zkey ^= Z_CR[__builtin_ctzll(crx)];                    /* FI-01 */
    b->castling = cr;

    int new_ep = (movpt == 1 && (to - from == 16 || from - to == 16))
               ? (from + to) / 2 : -1;
    zkey ^= Z_EP[b->ep + 1] ^ Z_EP[new_ep + 1];                /* FI-01 */
    b->ep = new_ep;
    b->turn = them;
    b->key = zkey;
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
/* FB-41: classic-scale piece values, by PT. Consumers: qsearch delta
 * (g_score_hyg=0 pin path), draw_score margin, mop-up npm, simplify _npm.
 * Retuning this array moves contempt/draw-avoidance and mop-up thresholds
 * too. NOT the real eval scale (that's the Texel g_mg_val/g_eg_val);
 * DELTA_VAL is a third, deliberately separate scale -- see FB-33 note
 * there. engine.py's _draw_score/_npm mirrors hand-type the same numbers:
 * keep them in lockstep or the bit-exact eval oracle splits. */
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
    /* FB-17: build the taper against the SYNCED PHASE_MAX (sync order
     * guarantees set_mobility_params already ran) -- a retune below 24 no
     * longer desyncs the blend from engine.py's `// _pm`; above 24 is
     * clamped here AND at the read (the [25] table is the hard bound). */
    {
        extern int PHASE_MAX;
        int pm = (PHASE_MAX > 0 && PHASE_MAX <= 24) ? PHASE_MAX : 24;
        for (int ph = 0; ph <= 24; ph++) {
            int p = ph > pm ? pm : ph;
            for (int rel = 0; rel < 8; rel++)
                g_passed_taper[ph][rel] =
                    (passed_mg[rel] * p + passed_eg[rel] * (pm - p)) / pm;
        }
    }
    g_eval_ready = 1;
}

/* Doubled / isolated / backward / passed, White's perspective. */
static int pawn_structure(uint64_t wp, uint64_t bp, int phase)
{
    int s = 0;
    const int* taper = g_passed_taper[phase > 24 ? 24 : phase]; /* FB-17 */
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
/* CW-01 (CONFIRMED into v42, tenth campaign: +3.27 +/-6.8 null KEPT as
 * correctness; driver default ON): cannot-win clamp. If the side the eval favors has no
 * pawns, no rooks/queens, and at most a lone minor (or two knights), it
 * cannot force mate -- the position's true upper bound is a draw, so the
 * score clamps to 0. Fixes the GUI/practical blindness where the engine
 * shuffles at "+2.6" with a lone bishop vs pawns, actively AVOIDING the
 * capture that would reveal the draw (horizon-effect draw-avoidance).
 * Mirrored bit-exactly in engine.py's _evaluate_static (use_cantwin) --
 * the oracle differential covers both. 0 = tip minus CW-01 (v41's eval). */
static int g_cantwin = 0;
void set_cantwin(int v) { g_cantwin = v; }
static inline int cantwin_clamp(const Board* b, int s)
{
    if (!g_cantwin || s == 0) return s;
    uint64_t strong = (s > 0) ? b->occ[WHITE] : b->occ[BLACK];
    if ((b->pawns | b->rooks | b->queens) & strong) return s;
    int nb = __builtin_popcountll(b->bishops & strong);
    int nn = __builtin_popcountll(b->knights & strong);
    if (nb + nn <= 1 || (nb == 0 && nn == 2)) return 0;
    return s;
}

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
    /* FB-06: PHASE_MAX is a synced eval tunable (eval_c.c, set_mobility_
     * params) -- the hardcoded 24 here would silently desync on a retune. */
    extern int PHASE_MAX;
    if (phase > PHASE_MAX) phase = PHASE_MAX;
    int score = PHASE_MAX > 0
              ? (mg * phase + eg * (PHASE_MAX - phase)) / PHASE_MAX : mg;
    score += (b->turn == WHITE) ? g_tempo : -g_tempo;

    /* lone-loser strong mop-up shortcut (replaces ALL positional terms).
     * Kingless test positions skip it (Python would index [-1]; C won't). */
    uint64_t wk = b->kings & occ_w, bk = b->kings & occ_b;
    int lone_w = (occ_w & ~b->kings & ~b->pawns) == 0;
    int lone_b = (occ_b & ~b->kings & ~b->pawns) == 0;
    if (lone_w != lone_b && wk && bk) {
        /* FB-41: same-integer PIECE_VAL substitution (classic scale). */
        int npm_w = PIECE_VAL[PT_KNIGHT] * __builtin_popcountll(b->knights & occ_w)
                  + PIECE_VAL[PT_BISHOP] * __builtin_popcountll(b->bishops & occ_w)
                  + PIECE_VAL[PT_ROOK]   * __builtin_popcountll(b->rooks   & occ_w)
                  + PIECE_VAL[PT_QUEEN]  * __builtin_popcountll(b->queens  & occ_w);
        int npm_b = PIECE_VAL[PT_KNIGHT] * __builtin_popcountll(b->knights & occ_b)
                  + PIECE_VAL[PT_BISHOP] * __builtin_popcountll(b->bishops & occ_b)
                  + PIECE_VAL[PT_ROOK]   * __builtin_popcountll(b->rooks   & occ_b)
                  + PIECE_VAL[PT_QUEEN]  * __builtin_popcountll(b->queens  & occ_b);
        int adv = npm_w - npm_b;
        if ((adv < 0 ? -adv : adv) >= g_mopup_min) {
            int wks = __builtin_ctzll(wk), bks = __builtin_ctzll(bk);
            int loser = (adv > 0) ? bks : wks;
            int df = (wks & 7) - (bks & 7), dr = (wks >> 3) - (bks >> 3);
            int md = (df < 0 ? -df : df) + (dr < 0 ? -dr : dr);
            int bonus = g_mopup_scmd * CENTER_MANH[loser]
                      + g_mopup_sking * (14 - md);
            return cantwin_clamp(b, score + ((adv > 0) ? bonus : -bonus));
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
        /* FB-41: same-integer PIECE_VAL substitution (classic scale). */
        int mw = PIECE_VAL[PT_PAWN]   * __builtin_popcountll(b->pawns   & occ_w)
               + PIECE_VAL[PT_KNIGHT] * __builtin_popcountll(b->knights & occ_w)
               + PIECE_VAL[PT_BISHOP] * __builtin_popcountll(b->bishops & occ_w)
               + PIECE_VAL[PT_ROOK]   * __builtin_popcountll(b->rooks   & occ_w)
               + PIECE_VAL[PT_QUEEN]  * __builtin_popcountll(b->queens  & occ_w);
        int mb = PIECE_VAL[PT_PAWN]   * __builtin_popcountll(b->pawns   & occ_b)
               + PIECE_VAL[PT_KNIGHT] * __builtin_popcountll(b->knights & occ_b)
               + PIECE_VAL[PT_BISHOP] * __builtin_popcountll(b->bishops & occ_b)
               + PIECE_VAL[PT_ROOK]   * __builtin_popcountll(b->rooks   & occ_b)
               + PIECE_VAL[PT_QUEEN]  * __builtin_popcountll(b->queens  & occ_b);
        int diff = mw - mb;
        if ((diff < 0 ? -diff : diff) >= g_simp_thresh) {
            int pieces = __builtin_popcountll(b->knights | b->bishops
                                              | b->rooks | b->queens);
            score += (diff > 0 ? 1 : -1) * g_simp_weight * (14 - pieces);
        }
    }
    return cantwin_clamp(b, score);
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
static __thread uint32_t g_killers[CS_MAXPLY + 8][2];  /* FB-26 headroom */
static __thread uint32_t g_counter[4096];

/* Q-01: continuation history (v30 #1.6, deferred at phase-3 step 1 and
 * never landed). Quiet ordering adds two context scores on top of butterfly
 * history: g_cont1 keyed by the PREVIOUS move (the opponent move that led
 * here), g_cont2 by the move TWO back (our own previous move); both indexed
 * by (mover_pt<<6 | to) of predecessor and candidate -- the compact
 * piece-to form (448x448 int16 per table, 392 KiB __thread each) instead of
 * v30's sparse from-to dicts. g_ctx[ply] holds the (pt<<6|to) of the move
 * that ENTERED ply (0 = none: root, null-move children). Same gravity rule
 * and HIST_MAX as butterfly history; updated at quiet beta cutoffs with the
 * same malus sweep. Band check: |history + cont1 + cont2| <= 3*16384 --
 * still far inside the quiet band (< ORD_COUNTER 700k, > ORD_BADCAP).
 * Deviations from v30 (documented): root context starts empty (v30 seeds
 * the real previous game move); qsearch ordering reads no cont scores
 * (g_ctx is only maintained by negamax, and captures dominate there).
 * set_cont_hist(0) restores v36's search node-exactly.
 * A/B vs Old Engine/36 (2026-07-10, 10k @ 50+0.20, the first campaign of
 * that era): -0.87 +/-6.8, ptnml 374/1136/1955/1211/324, pair ratio 1.02 --
 * a dead NULL. The ordering vein paid for staged generation (P-23 +24.67)
 * but not for finer quiet scores at this depth; the two ~800KB tables'
 * cache pressure and the per-move clears buy nothing back. DORMANT
 * (default OFF = v36 node-exact); re-test only at a much longer TC. */
static int g_cont_hist = 0;
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
#define TT_BITS 21              /* default: 2^21 x 24B = 48 MB */
/* FI-10: the table size is runtime-settable (UCI Hash). TT_SIZE/TT_MASK
 * became globals; set_tt_bits reallocates. Default 21 bits = the compiled
 * size the whole ledger was measured at (node-identical when untouched). */
static size_t   g_tt_size = (size_t)1 << TT_BITS;
static uint64_t g_tt_mask = ((uint64_t)1 << TT_BITS) - 1;
#define TT_SIZE g_tt_size
#define TT_MASK g_tt_mask
/* FI-17/FI-26a (P-45 re-arm, ADOPTED 2026-07-12): prefetch the child's TT
 * line right after apply_move. P-45 measured null because computing the
 * child key ate the gain; FI-01 made c.key free -- re-benched at +4.9% NPS
 * median, 3/3 pairs positive. Node-identical (a prefetch is a hint; raw
 * key is fine, the EP-01 fixup diverges only on rare phantom-ep nodes).
 * Timed Elo measured as part of the FI-26a batch A/B. */
#define TT_PREFETCH(k) do { if (g_tt) __builtin_prefetch(&g_tt[(k) & TT_MASK], 0, 0); } while (0)
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
/* FI-03: the static eval cached in d2's spare high 16 bits. The eval is
 * deterministic per position (params fixed per process -- FB-04 guards
 * that), so a cached value is EXACT, never approximate: reusing it is
 * node-identical and skips the most expensive per-node call on TT hits.
 * TT_EVAL_NONE marks entries stored without one (in-check nodes, root). */
#define TT_EVAL(e)     ((int)(int16_t)(uint16_t)((e).d2 >> 48))
#define TT_EVAL_NONE   (-32768)

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

/* FI-10: resize the TT (UCI Hash). Frees + re-callocs; entries are lost by
 * design (a resize is a config event, like ucinewgame). NEVER call during a
 * search (the host guards; csearch has no internal lock for this). */
void set_tt_bits(int bits)
{
    if (bits < 16) bits = 16;
    if (bits > 28) bits = 28;                /* 28 = 6 GB of 24B entries */
    size_t n = (size_t)1 << bits;
    if (n == g_tt_size && g_tt) return;
    size_t old_size = g_tt_size;             /* FB-26: the comment promised */
    uint64_t old_mask = g_tt_mask;           /* "the OLD size" -- keep it   */
    free(g_tt);
    g_tt = (TTEntry*)calloc(n, sizeof(TTEntry));
    if (g_tt == NULL) {                      /* degrade like Q-13: retry at
                                              * the old size next move */
        g_tt_size = old_size;
        g_tt_mask = old_mask;
        return;
    }
    g_tt_size = n;
    g_tt_mask = (uint64_t)n - 1;
    g_gen = 0;
}

/* FI-13a: TT utilization in permille (UCI `hashfull`) -- samples 1000
 * evenly-spaced slots; an all-zero entry image means never written. */
int cs_hashfull(void)
{
    if (g_tt == NULL) return 0;
    int used = 0;
    for (int i = 0; i < 1000; i++) {
        const TTEntry* t = &g_tt[(uint64_t)i * (TT_SIZE - 1) / 999];  /* FB-32:
                                     * spans 0..TT_SIZE-1; the old TT_SIZE/1000
                                     * stride never sampled the table tail */
        if (t->key_x | t->d1 | t->d2) used++;
    }
    return used;
}

static inline void tt_store_raw(TTEntry* t, uint64_t key, int value,
                                uint32_t move, int depth, int flag, int ev)
{
    /* FB-26: the int16 eval pack -- unreachable in legal play, enforced.
     * NEVER touch the TT_EVAL_NONE sentinel (-32768): clamping it to
     * -32767 would turn "no cached eval" into a fake real one. */
    if (ev != TT_EVAL_NONE) {
        if (ev > 32767) ev = 32767;
        else if (ev < -32767) ev = -32767;
    }
    uint64_t d1 = (uint64_t)(uint32_t)value | ((uint64_t)move << 32);
    uint64_t d2 = (uint64_t)(uint16_t)depth
                | ((uint64_t)(uint16_t)flag << 16)
                | ((uint64_t)(uint16_t)g_gen << 32)
                | ((uint64_t)(uint16_t)(int16_t)ev << 48);   /* FI-03 */
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

/* EP-01: FIDE-exact ep in the position hash -- CONFIRMED into v40
 * (2026-07-11, +4.31 +/-6.8 @10k, a null KEPT as correctness; the driver
 * pushes EP_FILTER=True, this compiled default stays 0 as the ladder pin).
 * Raw-ep hashing set a key component after EVERY double push; per FIDE
 * (and python-chess's _transposition_key, which the match arbiter's
 * threefold claims use) the ep right is part of the position only if an ep
 * capture is actually LEGAL. A phantom ep therefore split one real
 * position across two keys: repetitions could be MISSED and TT sharing was
 * needlessly split (merging the entries even saves nodes). With the filter
 * on, ep hashes only when some own pawn can legally play the capture --
 * exactly has_legal_en_passant; since FI-01 an O(1) board_key fixup.
 * cs_board_key shares this path, so the driver's game-history keys stay
 * consistent with the search either way. set_ep_filter(0) = tip minus
 * EP-01 (= v39's hashing). */
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
    /* FI-01: O(1) -- the key lives on the Board (apply_move maintains it).
     * EP-01's FIDE filter is a fixup, not a re-hash: a phantom ep square
     * (no legal ep capture) is XORed back out, so set_ep_filter stays a
     * runtime toggle at zero steady-state cost (ep squares are rare). */
    if (g_ep_filter && b->ep >= 0 && !ep_grants_move(b))
        return b->key ^ Z_EP[b->ep + 1];     /* == swap to Z_EP[0] == 0 */
    return b->key;
}

/* FI-02.4: sc_out != NULL defers the sort -- scores are written there and
 * the caller draws moves lazily via pick_next (identical emission order:
 * strict-> max pick, shift-to-front keeps gen-order ties stable). Most
 * nodes cut on move 1-3 and never pay for sorting the tail. sc_out == NULL
 * keeps the classic sort (root, VERIFY reference). */
static void order_moves(const Board* b, uint32_t* mv, int n, int ply,
                        uint32_t counter_key, uint32_t tt_move, int use_cont,
                        int* sc_out)
{
    int color = b->turn, full = g_order_mode;
    uint32_t k0 = g_killers[ply][0], k1 = g_killers[ply][1];
    int sc_local[256];
    int* sc = sc_out ? sc_out : sc_local;
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
                /* FI-02.3: tag the verdict into the move word's reserved
                 * bits (22-23: 0 unknown, 1 SEE>=0, 2 SEE<0) -- the sort
                 * carries it, and qsearch's losing-capture skip reads the
                 * tag instead of recomputing the same SEE. Every consumer
                 * of these words masks to 15 bits before comparing/storing. */
                mv[i] = (m & ~(3u << 22)) | ((sv < 0 ? 2u : 1u) << 22);
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
    if (sc_out) return;             /* FI-02.4: caller picks lazily */
    for (int i = 1; i < n; i++) {   /* stable insertion sort, score desc */
        uint32_t xm = mv[i]; int xs = sc[i], j = i - 1;
        while (j >= 0 && sc[j] < xs) { mv[j+1]=mv[j]; sc[j+1]=sc[j]; j--; }
        mv[j+1] = xm; sc[j+1] = xs;
    }
}

/* FI-02.4: bring the best remaining move to slot i. Strict > picks the
 * FIRST max (gen-order tie rule) and the shift preserves the relative
 * order of everything passed over -- the emitted stream is exactly the
 * stable full sort's. */
static inline uint32_t pick_next(uint32_t* mv, int* sc, int i, int n)
{
    int bi = i;
    for (int j = i + 1; j < n; j++)
        if (sc[j] > sc[bi]) bi = j;
    if (bi != i) {
        uint32_t bm = mv[bi]; int bs = sc[bi];
        memmove(&mv[i + 1], &mv[i], (size_t)(bi - i) * sizeof(uint32_t));
        memmove(&sc[i + 1], &sc[i], (size_t)(bi - i) * sizeof(int));
        mv[i] = bm; sc[i] = bs;
    }
    return mv[i];
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
                /* (FI-26 note, 2026-07-12: a lazy-pick variant of this sort
                 * was tried -- stream-identical, ladder-verified -- but the
                 * paired bench could not separate it from noise and its
                 * worst case (all-nodes consuming the whole quiet list) is
                 * memmove-heavy. Parked for the full FI-26 batch; the
                 * staged path reaches here mostly at non-cut nodes, unlike
                 * the FI-02.4 paths where lazy picking paid.) */
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
/* FB-09: optional node budget (UCI `go nodes N`; 0 = unlimited). Checked in
 * the same rate-limited slot as the deadline; main thread only (helpers ride
 * g_hstop). Node-identical when 0. */
static uint64_t g_node_limit = 0;
void set_node_limit(uint64_t n) { g_node_limit = n; }

#define CS_TIME_CHECK() do { \
        if ((g_nodes & 4095) == 0) { \
            if (g_deadline && now_ns() >= g_deadline) g_abort = 1; \
            if (g_node_limit && !g_is_helper && g_nodes >= g_node_limit) \
                g_abort = 1; \
        } \
        if (CS_UNWINDING()) return 0; \
    } while (0)

/* Host-requested abort (UCI `stop`): same unwind path as the deadline. */
void cs_stop(void) { g_abort = 1; }

/* FI-13a: selective depth -- deepest ply touched since cs_search_begin
 * (main thread; extensions + qsearch included). For UCI `seldepth`. */
static __thread int g_seldepth = 0;
int cs_seldepth(void) { return g_seldepth; }

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
/* Sized past the qsearch recursion guard (CS_MAXPLY + 60): the CB-01
 * correctness batch writes qsearch keys here too (gated on g_score_hyg;
 * without hygiene only negamax plies are written, as before). */
static __thread uint64_t g_path[CS_MAXPLY + 62];
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
    /* FB-41: same-integer PIECE_VAL substitution (classic scale). */
    diff += PIECE_VAL[PT_PAWN]   * (__builtin_popcountll(b->pawns & mine)
                                  - __builtin_popcountll(b->pawns & theirs));
    diff += PIECE_VAL[PT_KNIGHT] * (__builtin_popcountll(b->knights & mine)
                                  - __builtin_popcountll(b->knights & theirs));
    diff += PIECE_VAL[PT_BISHOP] * (__builtin_popcountll(b->bishops & mine)
                                  - __builtin_popcountll(b->bishops & theirs));
    diff += PIECE_VAL[PT_ROOK]   * (__builtin_popcountll(b->rooks & mine)
                                  - __builtin_popcountll(b->rooks & theirs));
    diff += PIECE_VAL[PT_QUEEN]  * (__builtin_popcountll(b->queens & mine)
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

/* --- FI-29: cuckoo upcoming-repetition (van Kervinck / SF has_game_cycle) --
 * is_repetition only sees repetitions already ON the path; this detects that
 * the side to move can FORCE one with a single reversible move, so the node
 * scores the contempt draw a full search earlier (prunes lost shuffle
 * subtrees, banks half points from perpetuals one ply sooner).
 *
 * Table: one Zobrist delta per unordered reversible non-pawn move
 * (Z_PSQ[c][pt][s1] ^ Z_PSQ[c][pt][s2] ^ Z_TURN), cuckoo-hashed into 8192
 * slots by two hash functions (SF's exact scheme; 3668 entries, 45% load).
 * Probe: at odd distances k = 3,5,... within the reversible window, if
 * key ^ past_key(k) equals a tabled delta AND the mover really sits on one
 * endpoint with the path between clear, playing that move recreates the
 * past key -> upcoming repetition. hmc bounds k, and a null move passes
 * hmc = 0 down, so the window never spans a null (no key continuity there).
 *
 * Soundness envelope (matches Stockfish, gated by the CYCLE_VERIFY
 * differential): the claimed move may be pin-illegal -- SF accepts this,
 * the key still repeats, and making the "move" on a copy reproduces the
 * past key exactly, so the differential stays clean. Two classes SF
 * accepts that would break KEY-level exactness are excluded here: probes
 * from in-check nodes are skipped at the call site (a quiet shuffle move
 * rarely answers check), and a match whose move would strip castling
 * rights is rejected below (the real key would gain Z_CR terms and NOT
 * repeat). ep never interferes: an ep right implies a just-played double
 * push, i.e. hmc == 0, and the probe needs hmc >= 3. */
static uint64_t g_cuckoo[8192];
static uint16_t g_cuckoo_mv[8192];     /* from | to<<6 | pt<<12 (pt 2..6) */
static int g_cycle = 0;                /* 0 = off = v48 node-exact */
void set_cycle(int v) { g_cycle = v ? 1 : 0; }
#define CUCKOO_H1(k) ((int)((k) & 0x1FFF))
#define CUCKOO_H2(k) ((int)(((k) >> 16) & 0x1FFF))

#ifdef CYCLE_VERIFY                    /* differential gate: every true     */
static long g_cyc_hits = 0;            /* return is re-proven by making the */
static long g_cyc_bad  = 0;            /* claimed move -- see selftest      */
void cs_cycle_stats(long* hits, long* bad) { *hits = g_cyc_hits; *bad = g_cyc_bad; }
#endif

static void cuckoo_build(void)
{
    int count = 0;
    for (int c = 0; c < 2; c++)
        for (int pt = PT_KNIGHT; pt <= PT_KING; pt++)
            for (int s1 = 0; s1 < 64; s1++) {
                uint64_t att =
                    pt == PT_KNIGHT ? KNIGHT_ATT[s1] :
                    pt == PT_KING   ? KING_ATT[s1]   :
                    pt == PT_BISHOP ? bishop_attacks(s1, 0) :
                    pt == PT_ROOK   ? rook_attacks(s1, 0)   :
                    bishop_attacks(s1, 0) | rook_attacks(s1, 0);
                for (uint64_t t = att & ~((1ULL << (s1 + 1)) - 1); t; t &= t - 1) {
                    int s2 = __builtin_ctzll(t);
                    uint64_t key = Z_PSQ[c][pt][s1] ^ Z_PSQ[c][pt][s2] ^ Z_TURN;
                    uint16_t mv = (uint16_t)(s1 | (s2 << 6) | (pt << 12));
                    int j = CUCKOO_H1(key);
                    for (int kick = 0; ; kick++) {   /* SF's insertion loop */
                        uint64_t tk = g_cuckoo[j]; g_cuckoo[j] = key; key = tk;
                        uint16_t tm = g_cuckoo_mv[j]; g_cuckoo_mv[j] = mv; mv = tm;
                        if (key == 0) break;
                        j = (j == CUCKOO_H1(key)) ? CUCKOO_H2(key) : CUCKOO_H1(key);
                        if (kick > 8192) { g_cuckoo[0] = 0; return; }  /* cannot
                            happen at 45% load; poisoning slot 0 keeps a
                            broken build fail-safe (probe never matches 0) */
                    }
                    count++;
                }
            }
    (void)count;                       /* 3668, the SF-identical census */
}

/* Can the side to move force a repetition with one reversible move?
 * Same walk as is_repetition (path first, then game history), but at ODD
 * distances (those positions had the OTHER side to move -- one move of
 * ours away): a delta match means playing the tabled move reproduces the
 * past key exactly. Call with hmc >= 3 and not in check. */
static inline int upcoming_repetition(const Board* b, uint64_t key,
                                      int ply, int hmc)
{
    uint64_t occ = b->occ[0] | b->occ[1];
    uint64_t own = b->occ[b->turn];
    for (int k = 3; k <= hmc; k += 2) {
        uint64_t past;
        if (k <= ply)                        past = g_path[ply - k];
        else if (k - ply - 1 < g_nhist)      past = g_hist[k - ply - 1];
        else                                 break;
        uint64_t delta = key ^ past;
        int j = CUCKOO_H1(delta);
        if (g_cuckoo[j] != delta) {
            j = CUCKOO_H2(delta);
            if (g_cuckoo[j] != delta) continue;
        }
        int s1 = g_cuckoo_mv[j] & 63;
        int s2 = (g_cuckoo_mv[j] >> 6) & 63;
        int pt = g_cuckoo_mv[j] >> 12;
        uint64_t b1 = 1ULL << s1, b2 = 1ULL << s2;
        /* exactly one endpoint holds a piece -- ours, of the delta's type */
        uint64_t on = occ & (b1 | b2);
        if (!on || (on & (on - 1)) || !(on & own)) continue;
        int from = (on & b1) ? s1 : s2, to = (on & b1) ? s2 : s1;
        if (board_piece_type_at(b, from) != pt) continue;
        if (INBETWEEN_BITBOARDS[from][to] & occ & ~(1ULL << to)) continue;
        /* key-soundness: the move must not strip castling rights (the real
         * post-move key would differ from `past` by Z_CR terms) */
        uint64_t crh = b->castling & (b1 | b2);
        if (pt == PT_KING)
            crh |= b->castling & (b->turn == WHITE
                                  ? ((1ULL << 0) | (1ULL << 7))
                                  : ((1ULL << 56) | (1ULL << 63)));
        if (crh) continue;
#ifdef CYCLE_VERIFY
        {   /* differential: really make the claimed move; the child key
             * must equal the matched past key, byte for byte. apply_move
             * needs the mover PT packed (FI-02.2); victim 0 = quiet. */
            Board c2 = *b;
            apply_move(&c2, (uint32_t)(from | (to << 6)
                                       | (pt << MV_SHIFT_MOVER)));
            if (c2.key == past) g_cyc_hits++; else g_cyc_bad++;
        }
#endif
        return 1;
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
 * set_check_ext(0) = tip minus check extensions (node-exact vs v33 at
 * INTRODUCTION; later confirmed features stack on top -- the selftest
 * ladder is the executable regression authority, not this comment). */
static int g_check_ext = 1;
void set_check_ext(int v) { g_check_ext = v; }
#define CHECK_EXT_MAX 5
/* P-47: the budget itself is runtime-settable. 5 (the v30/CHECK_EXT_MAX
 * recipe) reproduces v36 node-exactly. Raise-to-8 REJECTED (A/B vs v36
 * 2026-07-10, 10k @ 50+0.20: -4.59 +/-6.8, pair ratio 0.96) -- deeper
 * check lines cost more than they find; extensions vein confirmed thin
 * (P-01 +6.8, P-43 +3.5 marginal, P-47 -4.6). Don't re-try at this TC. */
static int g_check_ext_budget = CHECK_EXT_MAX;
void set_check_ext_budget(int v)
{
    g_check_ext_budget = v < 0 ? 0 : (v > 32 ? 32 : v);
}

/* CB-01 (correctness batch, one master toggle -- final_improvements.md
 * FB-05/FB-07/FB-08 + FI-07): score-hygiene fixes that individually sit
 * far under the +/-6.8 resolution and together form one "score draws as
 * draws, keep proven bounds" feature. OFF (0) restores v37 node-exactly:
 *   (a) FB-05 delta pruning budgets the TEXEL value of the victim (queen
 *       1150) instead of the classic 900 the synced eval outgrew,
 *   (b) FB-07 qsearch in-check nodes detect repetition (perpetual-check
 *       lines used to score as eval, and P-44 persisted the misscore),
 *   (c) FB-08 qsearch detects insufficient-material dead draws,
 *   (d) null-move returns/stores its fail-soft bound (unproven mate
 *       scores clamped to beta),
 *   (e) the qsearch TT probe narrows alpha from a LOWER bound,
 *   (f) mate-distance pruning,
 *   (g) deep-qsearch ordering reads killer slot CS_MAXPLY-1, not the
 *       ROOT's killers (the old ply>=64 clamp went to slot 0). */
static int g_score_hyg = 0;
void set_score_hygiene(int v) { g_score_hyg = v ? 1 : 0; }
/* (a): max(MG,EG) of the synced Texel values, rounded up -- must COVER the
 * eval swing of capturing the piece. (FB-33: MVV-LVA never reads PIECE_VAL;
 * its only live consumer is the !g_score_hyg delta arm -- dead in the
 * shipped config, kept as pin-support for set_score_hygiene(0).) */
static const int DELTA_VAL[7] = {0, 100, 360, 360, 520, 1150, 0};

/* PV-01: triangular PV table -- the PV is collected DURING the search (each
 * PV node prepends its best move to its child's line when the score lands
 * inside the window) instead of being reconstructed from the TT afterwards,
 * so the emitted PV can no longer be truncated/spliced by TT eviction.
 * Node-exact: pure bookkeeping at is_pv alpha-raises, zero search decisions
 * read it. Sized for qsearch's recursion guard (CS_MAXPLY + 60).
 *
 * PV-02 (set_pv_exact, CONFIRMED into v37): the remaining truncation source
 * is the TT itself, which cuts PV nodes off via EXACT hits and
 * bound-narrowing before their line is walked. pv_exact skips the whole
 * TT-cutoff block at PV nodes (the standard strong-engine rule; the TT move
 * is still used for ordering), making the collected PV complete end-to-end.
 * A/B vs v36 (2026-07-10, 10k @ 50+0.20): +0.17 +/-6.8 -- a clean null, so
 * the exact PV is FREE; the driver defaults it ON (cengine.PV_EXACT).
 * set_pv_exact(0) restores v36's search. */
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
 * set_single_reply(0) = tip minus P-43 (node-exact vs v34 at introduction).
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
 * ago. Three uses, all gated: set_improving(0) = tip minus P-04 (was
 * node-exact vs v34 at introduction):
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
static __thread int g_seval[CS_MAXPLY + 8];            /* FB-26 headroom */

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

/* FI-24(a)+(b) (armed for the thirty-first 50+0.20-equivalent campaign, vs
 * Old Engine/51 -- the null-move refinement batch, two toggles one campaign
 * per the entry's pre-registration; both null-mechanism family):
 *  (a) g_null_nodouble -- forbid null-after-null: the parent's null is
 *      visible as the prev12==0xFFFFFFFF sentinel, and two stand-pats in a
 *      row prove nothing new while hiding zugzwang two plies deep.
 *  (b) g_null_evalr -- grow the null reduction R when the static eval is
 *      far above beta (R += (prune_eval-beta)/200, capped +2): deep nulls
 *      only at clearly-winning nodes, so the shallow-null population is
 *      untouched (the measured NULL_BASE 2->3 cliff -- +17.5% fixed-depth
 *      nodes from losing shallow null coverage -- cannot recur here).
 * Both 0 = off = v51 node-exact. NOT correctness-class: revert on null. */
static int g_null_nodouble = 0;
void set_null_nodouble(int v) { g_null_nodouble = v ? 1 : 0; }
static int g_null_evalr = 0;
void set_null_evalr(int v) { g_null_evalr = v ? 1 : 0; }
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
void set_lmr_div(int x100) { if (x100 < 25) x100 = 25;   /* FB-26: /0 UB */
                             g_lmr_div = x100 / 100.0; init_lmr(); }

/* null move: pass the turn, clear the (single-move) ep right. */
static inline void make_null(Board* b)
{
    b->key ^= Z_TURN ^ Z_EP[b->ep + 1];      /* FI-01: Z_EP[0] == 0 */
    b->turn ^= 1; b->ep = -1;
}

/* side has a knight/bishop/rook/queen (null-move zugzwang guard). */
static inline int has_non_pawn(const Board* b, int side)
{
    return (b->knights | b->bishops | b->rooks | b->queens) & b->occ[side] ? 1 : 0;
}

/* FI-15 NNUE (Phases 1-5 BUILT-DORMANT 2026-07-18): the entire NNUE side --
 * weight loader, KA8T feature extraction, T16 threat encoding, F49-31
 * per-thread ply-indexed accumulator stack, NEON+scalar quantized forward --
 * lives in NNUE/nnue.c, single-TU-included here (no build change, full
 * cross-inlining; snapshots' csearch.c predates the include and is
 * untouched). Everything is behind g_use_nnue (set_use_nnue, default 0 =
 * v50 byte-exact; the driver attr is cengine.USE_NNUE). Hybrid rules and
 * the frozen architecture: DESIGN_nnue.md "Phase 1 spec". */
#include "NNUE/nnue.c"

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
 * ply-relative mate encoding as negamax. set_qs_tt(0) = tip minus P-44's
 * search node-exactly.
 * CONFIRMED into v35 (2026-07-10): isolation A/B vs the P-22 base (both
 * sides equally fast) +8.06 +/-6.8 over 10k @45+0.1, CI clear of zero --
 * the persistent warm table across a game delivered what the flat
 * cold-ladder time-to-depth bench could not show. */
static int g_qs_tt = 1;
void set_qs_tt(int v) { g_qs_tt = v; }

/* FI-08 (Q-03, LIVE CANDIDATE): P-14 proved the warm cross-move table is
 * the engine's most valuable asset (+23.5); P-44's depth-0 qsearch stores
 * erode it a little every move, because a different-key OLD-GENERATION
 * entry is freely replaceable no matter how deep it is -- a depth-12
 * entry from the previous move's search can be evicted by a stand-pat
 * cutoff. Guard: old-gen entries are replaceable by a qsearch store only
 * up to this depth. -1 = off (v40's rule: old-gen always replaceable).
 * Cold-TT fixed-depth trees are UNAFFECTED either way (after a reset the
 * only old-gen entries are zeroed slots, depth 0). */
static int g_qs_evict_max = -1;
void set_qs_evict_max(int v) { g_qs_evict_max = v; }

/* CB-02 (LIVE CANDIDATE, ninth 50+0.20 campaign, A/B vs Old Engine/40
 * pending): correctness batch #4, keep-on-null class like CB-01 --
 *  (a) FB-22: the null-move TT store obeys the replacement policy instead
 *      of clobbering deeper entries (and keeps a same-key entry's move),
 *  (b) FI-27.1: qsearch applies the 50-move rule (completes CB-01's
 *      draw set; same !in_chk semantics as negamax),
 *  (c) FI-24c: deep null cutoffs (depth >= 10) are verified with a
 *      reduced NO-NULL search -- zugzwang/fortress insurance that
 *      has_non_pawn alone cannot provide (g_no_null suppresses null
 *      moves for the whole verification subtree, Heinz-style).
 * The driver half (FB-23, cengine.CB2) rides the same class attr:
 * root fail-high moves are adopted+promoted across aspiration calls.
 * 0 = v40 node-exact. */
static int g_cb2 = 0;
static __thread int g_no_null = 0;
void set_cb2(int v) { g_cb2 = v; }

/* NV-01 (LIVE CANDIDATE, eleventh 50+0.20 campaign): isolate CB-02(c).
 * The deep-null verification search is the only COSTLY component of the
 * CB-02 batch (~+47% d12 nodes, ~one ply of nodes-to-depth) and modern
 * top engines dropped verification years ago (has_non_pawn + TT cover
 * zugzwang well enough). The candidate is verification OFF (cengine
 * pushes NULL_VERIFY=False); 1 = v41/v42's verifying search exactly. */
static int g_null_verify = 1;
void set_null_verify(int v) { g_null_verify = v; }

/* FI-04 (LIVE CANDIDATE, twelfth 50+0.20 campaign): history-based LMR --
 * the one search-formula idea ALL FIVE v39+ audits proposed independently.
 * The quiet's own butterfly history (already maintained for ordering) nudges
 * its reduction: well-scoring quiets reduce one less, badly-scoring ones one
 * more. A move-QUALITY feature at fixed depth (P-23's paying family), not a
 * depth-chaser; the divisor is the runtime knob (adj = hist/div clamped to
 * +/-1; HIST_MAX=16384, div 8192 => adj != 0 only on strong signals).
 * 0 = off = v43 node-exact. */
static int g_lmr_hist = 0;
void set_lmr_hist(int v) { g_lmr_hist = v; }

/* FI-25 (armed for the fourteenth 50+0.20 A/B, vs Old Engine/44): use the
 * TT's SEARCH value as a pruning-eval sharpener. FI-03 reuses the cached
 * STATIC eval; the entry's search value is strictly better information
 * whenever its bound applies (LOWER above / UPPER below the static eval,
 * EXACT always) -- prunes both more accurately and less wrongly at the
 * same depth, Stockfish-family practice. Non-mate values only (mate scores
 * are ply-dependent); the sharpened value feeds RFP / null-move / frontier
 * futility ONLY -- static_eval itself stays raw for the FI-03 TT cache and
 * the P-04 eval stack (exactness invariants). 0 = off = v44 node-exact. */
static int g_tt_eval_sharpen = 0;
void set_tt_eval_sharpen(int v) { g_tt_eval_sharpen = v; }

/* FI-30 (armed for the twenty-second 50+0.20 A/B, vs Old Engine/47):
 * (a) qsearch TT-value stand-pat sharpener -- FI-25's exact rule applied at
 * the tree's most populous node type. When the qsearch TT probe hits but the
 * bound does not cut, the entry's SEARCH value replaces the static eval as
 * the stand-pat wherever the bound provably improves it (LOWER above /
 * UPPER below / EXACT always; non-mate values only). Feeds the stand-pat
 * beta cutoff, the best-init/alpha raise, and the delta-pruning base. The
 * FI-03 TT-eval cache keeps the RAW static eval (raw_stand -- the same
 * exactness split FI-25 uses for static_eval vs prune_eval).
 * (b) keep-move rider: a stand-pat cutoff stores move 0; the same-key
 * replace rule then deletes a stored best move for zero information gain --
 * FB-22's shipped keep-the-move rule (negamax null store) applied to
 * qs_tt_store. 0/0 = off = v47 node-exact. */
static int g_qs_tt_sharpen = 0;
void set_qs_tt_sharpen(int v) { g_qs_tt_sharpen = v; }
static int g_qs_keep_move = 0;
void set_qs_keep_move(int v) { g_qs_keep_move = v; }

/* FI-18 (armed for the fifteenth 50+0.20 A/B, vs Old Engine/45): SEE
 * pruning of losing captures at shallow depth. Bad captures are ordered
 * LAST (ORD_BADCAP band / staged stage 6) but still fully searched at
 * every depth; the standard prune skips them at non-PV, not-in-check,
 * non-check-giving, depth <= 3, late in the list. The SEE verdict is
 * already known -- the staged stream's stage-6 emissions ARE the
 * SEE-negative captures, and the array path's FI-02.3 tag (bits 22-23,
 * 2 = SEE < 0) was computed by order_moves -- so ZERO new SEE calls.
 * Failure mode is tactical misses: matetrack must stay clean.
 * 0 = off = v45 node-exact. */
static int g_see_prune = 0;
void set_see_prune(int v) { g_see_prune = v; }

/* FI-06 (armed for the sixteenth 50+0.20 A/B, vs Old Engine/45): root-move
 * ordering by prior-iteration subtree node counts + warm-TT seed for
 * iteration 1. Root-only bookkeeping, zero per-node cost: after a
 * completed iteration the MAIN thread records how many nodes each root
 * move's subtree ate; the next iteration (same root position) keeps the
 * PV/prev move first and orders the rest by those counts descending -- a
 * fail-low move that still ate a big tree is the likeliest refutation
 * candidate, so trying it earlier tightens alpha sooner and shrinks the
 * aspiration re-searches. When the driver has no prev_key yet (iteration
 * 1 of a fresh game move), the ordering seeds from the persistent TT's
 * stored best move (P-14's warm asset, one probe). Helpers never read or
 * write the tables (g_is_helper guard): no shared-state race, and their
 * deliberately-diverse ordering stays v45. 0 = off = v45 node-exact. */
static int g_root_order = 0;
static uint64_t g_ro_key = 0;              /* root position the data is for */
static int g_ro_n = 0;
static uint16_t g_ro_mv[256];              /* 15-bit move keys */
static uint64_t g_ro_cnt[256];             /* their subtree node counts */
void set_root_order(int v) { g_root_order = v; g_ro_key = 0; g_ro_n = 0; }

/* MultiPV support (host-level feature, abi 10): a root-move EXCLUSION list.
 * The driver finds line 1 normally, then re-searches with the better lines'
 * first moves excluded to get lines 2..k -- the warm TT makes those
 * re-searches cheap. Empty list (the default, and everything match play
 * ever uses) is a single `if (g_rx_n)` that never fires: node-exact, no
 * toggle needed. While exclusions are active the root TT store and the
 * FI-06 recorder are suppressed -- a 2nd-best result must never clobber
 * the root's real TT entry (P-14's warm asset / prev-iteration ordering).
 * Helpers see the same list (process-wide, set between searches): correct
 * for MultiPV, inert otherwise. */
static uint16_t g_rx[256];      /* FI-45: searchmoves inverts a whitelist,
                                 * so exclusions can approach the legal-move
                                 * count -- 16 was sized for MultiPV only */
static int g_rx_n = 0;
void root_exclude_clear(void) { g_rx_n = 0; }
void root_exclude_add(int key15)
{
    if (g_rx_n < 256) g_rx[g_rx_n++] = (uint16_t)(key15 & 0x7FFF);
}

/* FI-23 (armed for the twenty-first 50+0.20 A/B, vs Old Engine/47):
 * history-driven quiet pruning. LMP prunes by COUNT only; this adds the
 * signal sibling -- skip quiets the EXISTING butterfly history has
 * consistently punished (below -threshold), at the same shallow/non-PV/
 * not-in-check/non-check-giving nodes LMP and SEE-prune already gate.
 * Reuses g_history read-only, no new bookkeeping. 0 = off = v47 node-exact;
 * threshold is a magnitude on the +-HIST_MAX=16384 scale. Armed at 256, NOT
 * the spec's suggested HIST_MAX/2=8192 -- that measured as a dead gate (see
 * cengine.py's HIST_PRUNE comment for the engagement sweep); g_history is
 * zeroed every move by cs_search_begin, so within one move's search it
 * rarely swings past a few hundred. */
static int g_hist_prune = 0;
void set_hist_prune(int v) { g_hist_prune = v < 0 ? 0 : v; }  /* FB-31 */

/* FI-50/51/52 (qsearch-TT batch, armed for the twenty-fourth 50+0.20 A/B vs
 * Old Engine/49 -- the FI-30 lineage, three non-overlapping toggles ganged as
 * one campaign per the grouped-toggle precedent FI-30 set):
 *   FI-50 g_qs_beta_narrow -- narrow beta from a TT_UPPER qsearch hit, the
 *         CB-01(e) alpha-narrow's mirror (negamax has done both since 2597-98).
 *   FI-51 g_qs_ttm_exempt  -- the qsearch TT move is immune to the losing-SEE
 *         skip and delta pruning: a nonzero stored bm beat stand-pat at store
 *         time, so the search-proven refutation must not be statically tossed.
 *   FI-52 g_qs_chk_d1      -- in-check RESOLVED qsearch stores tagged depth 1
 *         (structurally the same node a depth-1 in-check negamax node searches),
 *         so negamax's TT_DEPTH>=depth gate can cut directly from them.
 * All three 0 = off = v49 node-exact. */
static int g_qs_beta_narrow = 0;
void set_qs_beta_narrow(int v) { g_qs_beta_narrow = v ? 1 : 0; }
static int g_qs_ttm_exempt = 0;
void set_qs_ttm_exempt(int v) { g_qs_ttm_exempt = v ? 1 : 0; }
static int g_qs_chk_d1 = 0;
void set_qs_chk_d1(int v) { g_qs_chk_d1 = v ? 1 : 0; }

/* FI-63 (armed for the thirty-second campaign, vs Old Engine/52): SF-style
 * quietCheckEvasions. In-check qsearch nodes are the last node population
 * with ZERO pruning -- they fan out through perpetual/spite-check lines
 * where the 3rd+ quiet evasion, ordered behind TT/killer-quality moves,
 * virtually never improves on the first two. After N fully-searched quiet
 * evasions, skip the rest; captures and promotion evasions are ALWAYS
 * searched. Mate guard: the cap never applies while every searched evasion
 * still loses to mate (best <= -MATE_THRESH), so a mate score can never be
 * concluded from a pruned move set. Known unsoundness (delta-pruning
 * class): a capped fail-low node stores TT_UPPER at the searched best,
 * which is a wrong-way bound if a skipped evasion was better -- paired
 * matetrack is the PRIMARY gate, not a formality. 0 = off = v52
 * node-exact (repo 0-means-off convention); armed value 2. */
static int g_qs_evasion_cap = 0;
void set_qs_evasion_cap(int v) { g_qs_evasion_cap = v < 0 ? 0 : v; }

/* P-33 REVISIT (armed for the thirty-second campaign, vs Old Engine/52):
 * singular extensions. The Python era rejected this at depth ~8 (null @d8,
 * negative @d6) -- but singular is the classic technique whose value scales
 * WITH depth, and the C core now searches ~19, so the old verdict is a
 * measurement of a different engine (the FI-49 lesson read in reverse:
 * there SF's rule needed SF's tree; here the tree finally resembles one).
 *
 * Mechanism (Stockfish-shaped, conservative): at a non-root node with a TT
 * move whose entry is deep enough (TT_DEPTH >= depth - 3) and is a
 * LOWER/EXACT non-mate bound, run a reduced zero-window verification
 * search with that move EXCLUDED, at a lowered beta (tt_val - margin*depth
 * /64). If every OTHER move fails below that bar, the TT move is singular
 * and gets +1 ply.
 *
 * Threading: g_excl[ply] carries the excluded move for the verification
 * search. Inside an excluded search the node MUST NOT (a) take a TT cutoff
 * -- the stored entry describes the un-excluded node and would instantly
 * return the very move being tested -- or (b) write the TT, which would
 * poison the real node's entry with an exclusion-relative bound. Both are
 * gated below. g_excl is per-thread and always restored, including on the
 * unwind path.
 *
 * 0 = off = v52 node-exact. NOT correctness-class: revert on null. */
/* FI-59 (killers/malus batch, armed for the thirty-second campaign vs Old
 * Engine/52): ply-2 killer reuse. Warm-start an untouched killer slot from
 * two plies up (same side to move) at first touch -- one bounded 8-byte
 * copy per node, no new table/stage/band, so the existing emission and
 * dedup machinery handles the inherited keys identically on BOTH the
 * staged and array paths (P-23 stream identity by construction). After a
 * null move ply-2 is the opponent's slot: ordering noise only, and
 * move_from_key re-validates legality at emission. The inherited write
 * persists for the rest of the search (later ply-2 updates do not
 * re-propagate) -- intended. 0 = off = v52 node-exact (no write occurs). */
static int g_killer_inherit = 0;
void set_killer_inherit(int v) { g_killer_inherit = v ? 1 : 0; }

/* FI-60 (same batch): quiet-history malus on ALL beta cutoffs. Today only a
 * QUIET cutter sweeps -depth*depth over the tried quiets; when a bad
 * capture or promotion cuts, the quiets that already failed keep their
 * history untouched. This extends the malus to those cutoffs (the cutter is
 * NOT in quiets[], so the bound is nq, not nq-1). No bonus to the cutter
 * (that is FI-05 capture history, separate), no killer/counter write --
 * those stay quiet-only. Ordering-only mutation: no TT/FI-03 exactness
 * exposure. Honest ceiling: staged ordering searches good captures at i=0
 * with nq==0, so this only fires on bad-cap/promo cutoffs AFTER quiets were
 * tried. 0 = off = v52 node-exact. */
static int g_quiet_malus_all = 0;
void set_quiet_malus_all(int v) { g_quiet_malus_all = v ? 1 : 0; }

static int g_singular = 0;
void set_singular(int v) { g_singular = v ? 1 : 0; }
static int g_se_min_depth = 8;      /* min depth to attempt the test */
static int g_se_margin    = 64;     /* beta drop = margin * depth / 64 */
static int g_se_budget    = 3;      /* max singular extensions per line --
                                     * INDEPENDENT of the P-01 chk budget:
                                     * sharing it starved the check
                                     * extensions that find mates */
void set_singular_params(int min_depth, int margin)
{
    g_se_min_depth = min_depth < 4 ? 4 : min_depth;
    g_se_margin    = margin   < 1 ? 1 : margin;
}
void set_singular_budget(int v) { g_se_budget = v < 0 ? 0 : v; }
static __thread uint32_t g_excl[CS_MAXPLY + 8];   /* 0 = no exclusion */

/* FI-48 (armed for the twenty-fifth 50+0.20 A/B, vs Old Engine/49):
 * flag-aware TT replacement. All three same-key store sites are flag-blind,
 * so an equal-depth bound-only store (qsearch stand-pat LOWERs, unproven
 * CB-02 null LOWERs, negamax UPPER/LOWER results) overwrites a fully
 * resolved EXACT entry of the same position -- FI-30(b) rescues only the
 * move, not the flag and value. Level 1: same-key shield -- keep the
 * incumbent when it is EXACT, at least as deep, and the incoming flag is a
 * bound. Level 2 rider (not armed): +2 effective-depth bonus for EXACT
 * incumbents in the cross-key current-gen replace test. Skipping a store
 * never changes a returned value, so 0 = off = v49 node-exact. NOT
 * correctness-class (SF's tuned policy is the opposite -- recency wins):
 * revert on null. */
static int g_tt_keep_exact = 0;
void set_tt_keep_exact(int v) { g_tt_keep_exact = v < 0 ? 0 : v; }

static inline int tt_exact_shield(TTEntry cur, int new_depth, int new_flag)
{   /* same-key path only */
    return g_tt_keep_exact && TT_FLAG(cur) == TT_EXACT
        && new_flag != TT_EXACT && TT_DEPTH(cur) >= new_depth;
}
static inline int tt_exact_bonus(TTEntry cur)
{   return (g_tt_keep_exact >= 2 && TT_FLAG(cur) == TT_EXACT) ? 2 : 0; }

/* FI-49 (armed for the twenty-fifth 50+0.20 A/B, vs Old Engine/49):
 * TT fail-high depth tightening, matching current Stockfish -- an
 * equal-depth TT_LOWER whose value would cut (v >= beta) needs one ply
 * more stored depth before the cutoff/narrowing block fires; fail-high
 * scores are unstable at equal depth. EXACT entries and narrowing-only
 * LOWER hits (v < beta) are untouched, PV nodes were already skipped
 * (PV-02). 0 = off = v49 node-exact. NOT correctness-class: revert on
 * null. */
static int g_tt_fh_tight = 0;
void set_tt_fh_tight(int v) { g_tt_fh_tight = v ? 1 : 0; }

/* FI-53 (BUILT-DORMANT, arm after the FI-49 verdict): rule50 TT staleness
 * guard. A decisive-but-non-mate TT value stored at a low halfmove clock is
 * stale near the 50-move horizon -- the win it promises may no longer be
 * convertible before the rule draw. At hmc >= 90 refuse the TT value
 * cutoff/narrowing (negamax block + qsearch bound-return block) for values
 * |v| >= 500cp below MATE_THRESH, falling through to real search. Mate
 * scores and quiet values still cut, so mate finds are never lost by
 * construction; TT-move ordering and the FI-03/FI-25/FI-30 field reads are
 * untouched. Pre-registered partial coverage: FI-25/FI-30 sharpening still
 * consumes stale values inside the window. 0 = off = v49 node-exact. */
static int g_tt_r50 = 0;
void set_tt_r50(int v) { g_tt_r50 = v ? 1 : 0; }
#define R50_TT_HMC   90                /* SF-classic window: hmc 90..99 */
#define R50_DECISIVE 500               /* decisive-but-non-mate filter */
static inline int tt_r50_stale(int hmc, int v)
{
    return g_tt_r50 && hmc >= R50_TT_HMC
        && v > -MATE_THRESH && v < MATE_THRESH        /* mates still cut */
        && (v >= R50_DECISIVE || v <= -R50_DECISIVE); /* quiet vals still cut */
}

/* FI-54 (BUILT-DORMANT, arm after the FI-49 verdict): depth-independent TT
 * mate handling -- a forced mate/stalemate is depth-invariant. Store side
 * (set_term_store): the three terminal returns that today skip the TT store
 * (negamax staged-exhaust, negamax n==0, qsearch evasion checkmate) write a
 * TT_EXACT entry at sentinel depth 200 (> any real draft, wins every
 * depth-preferred replace), mate values ply-encoded, stalemate 0,
 * TT_EVAL_NONE (never a fake FI-03 eval). Probe side (set_tt_mate_cut): a
 * second negamax cutoff arm fires even when TT_DEPTH(e) < depth iff the
 * ply-adjusted value is mate-range and the bound proves it; PV nodes
 * skipped (PV-01 mate lines). Rule draws are decided before the probe in
 * both searches, so a permanent entry cannot mask a draw at the probing
 * node; the residual GHI exposure (a propagated mate resting on a
 * repetition claimable from the current path) lives only in the probe arm
 * and matches Stockfish's accepted tradeoff. Both 0 = off = v49
 * node-exact. */
static int g_term_store = 0;
void set_term_store(int v) { g_term_store = v ? 1 : 0; }
static int g_tt_mate_cut = 0;
void set_tt_mate_cut(int v) { g_tt_mate_cut = v ? 1 : 0; }

/* FI-56 (BUILT-DORMANT, arm after the FI-53/54 verdict): root-move LMR.
 * Deliberately overturns the long-standing "no reductions or pruning at
 * root" stance for LATE roots only: at depth >= 3, quiet non-promotion
 * root moves at index >= 4 that neither respond to check nor give check
 * are scouted at depth-1-R with R = g_lmr[depth][i]/2 (capped depth-2).
 * A reduced scout that beats alpha is re-searched at full depth
 * zero-window before the existing full-window re-search -- the standard
 * three-step cascade negamax already uses. Move 0 (PV move) is never
 * reduced; FB-23 adoption is untouched (best_move updates only after the
 * cascade completes at full depth). Known side effects (pre-registered):
 * FI-06 subtree counts shrink under reduction (dormant, relative order
 * only) and out_second becomes a shallower upper bound. 0 = off = v49
 * node-exact. NOT correctness-class: 2k screen mandatory before the 10k
 * (design-stance reversal), revert on null. */
static int g_root_lmr = 0;
void set_root_lmr(int v) { g_root_lmr = v ? 1 : 0; }

/* FI-55 (armed for the twenty-eighth 50+0.20 A/B, vs Old Engine/51): IIR
 * trigger extension. P-03 reduces only when there is NO TT move; this also
 * reduces when the TT move is WEAK ordering evidence -- a TT_UPPER entry
 * stored shallower than the current depth (the move is just whatever the
 * fail-low search last tried; it carries no cutoff evidence, so ordering
 * ahead is nearly as blind as a TT miss). Current-SF trigger form
 * (!ttMove || bound == UPPER); persistently-failing-low nodes re-fire the
 * reduction each visit (intended, matches SF). Existing IIR_MIN_DEPTH and
 * !in_chk gates kept. The F1 depth-gap sub-variant (tt_depth <= depth-4
 * regardless of flag) is deliberately NOT built -- follow-up slot only if
 * this form ships. 0 = off = v51 node-exact. NOT correctness-class:
 * revert on null. */
static int g_iir_weak = 0;
void set_iir_weak(int v) { g_iir_weak = v ? 1 : 0; }

/* FI-64 (armed for the twenty-ninth 50+0.20 A/B, vs Old Engine/51): LMR on
 * SEE-losing captures. Badcaps are ordered dead last (staged stage 6 /
 * ORD_BADCAP) and almost never best, yet each gets a full-depth zero-window
 * scout today; sharing the g_lmr reduction table trims the widest useless
 * subtrees. Reduction, NOT pruning (unlike the closed FI-18 vein): a
 * reduced badcap that fails high is re-searched at full depth by the
 * existing PVS ladder, so no move is ever lost -- deep sacrifices are seen
 * one iteration later at worst. Killer/history/counter updates stay
 * quiet-gated; the FI-04 history nudge is quiet-gated in the same edit
 * (butterfly history is quiet-only -- wrong on capture fromto squares if
 * FI-04 is ever armed). depth==3 overlaps dormant FI-18 if ever re-armed.
 * 0 = off = v51 node-exact. NOT correctness-class: revert on null. */
static int g_lmr_badcap = 0;
void set_lmr_badcap(int v) { g_lmr_badcap = v ? 1 : 0; }

static void tt_store_terminal(TTEntry* t, uint64_t key, int val, int ply)
{
    if (!g_term_store || !g_use_tt || t == NULL || CS_UNWINDING()) return;
    int sv = val;
    if (sv >= MATE_THRESH) sv += ply;                /* node -> ply-relative */
    else if (sv <= -MATE_THRESH) sv -= ply;
    tt_store_raw(t, key, sv, 0, 200, TT_EXACT, TT_EVAL_NONE);
}

static inline void qs_tt_store(uint64_t key, int val, int ply, uint32_t move,
                               int flag, int ev, int depth)  /* FI-52: depth */
{
    TTEntry* t = &g_tt[key & TT_MASK];
    TTEntry cur = *t;
    uint64_t ck = cur.key_x ^ cur.d1 ^ cur.d2;
    int replace = (ck == key)
                ? (TT_DEPTH(cur) <= depth            /* was <= 0; == at depth 0 */
                   && !tt_exact_shield(cur, depth, flag))   /* FI-48 */
                : (TT_GEN(cur) != (int)(uint16_t)g_gen
                       ? (g_qs_evict_max < 0 || TT_DEPTH(cur) <= g_qs_evict_max)
                       : TT_DEPTH(cur) + tt_exact_bonus(cur) <= depth);
    if (!replace) return;
    int sv = val;
    if (sv >= MATE_THRESH) sv += ply;                /* node -> ply-relative */
    else if (sv <= -MATE_THRESH) sv -= ply;
    uint32_t mv = move & 0x7FFF;
    if (g_qs_keep_move && mv == 0 && ck == key)      /* FI-30(b): a move-0
                                                      * store must not delete
                                                      * a same-key entry's
                                                      * ordering asset */
        mv = TT_MOVE(cur) & 0x7FFF;
    tt_store_raw(t, key, sv, mv, depth, flag, ev);
}

/* FB-40: the FI-30(a) stand-pat sharpen rule, shared by both qsearch
 * branches (lazy P-46 path and the set_qs_lazy(0) ladder-pin path) -- the
 * two copies previously had to be edited in lockstep, and the non-lazy one
 * is exercised only in ladder node-exact runs, where a missed edit would
 * silently break value-identity instead of failing match play. */
static inline int qs_sharpen_stand(int stand, int qs_sh_flag, int qs_sh_val)
{
    /* FI-30(a): proven TT bound beats static eval for stand-pat. */
    if (g_qs_tt_sharpen && qs_sh_flag >= 0
            && qs_sh_val > -MATE_THRESH && qs_sh_val < MATE_THRESH) {
        if (qs_sh_flag == TT_EXACT
            || (qs_sh_flag == TT_LOWER && qs_sh_val > stand)
            || (qs_sh_flag == TT_UPPER && qs_sh_val < stand))
            return qs_sh_val;
    }
    return stand;
}

static int qsearch(Board* b, int alpha, int beta, int ply, int in_chk,
                   int hmc)
{
    g_nodes++;
    g_pv_len[ply] = 0;     /* PV-01: every exit path leaves a valid (empty)
                            * line -- a stale slot would splice wrong moves
                            * into the parent's PV. ply < PV_MAX: the guard
                            * below caps recursion at CS_MAXPLY + 60. */
    if (ply > g_seldepth) g_seldepth = ply;  /* FI-13a: UCI seldepth */
    CS_TIME_CHECK();
    if (in_chk < 0) in_chk = in_check(b);
    if (ply >= CS_MAXPLY + 60)                       /* hard recursion guard */
        return in_chk ? 0 : eval_full_stm(b);
    int is_pv = (beta - alpha) > 1;

    /* P-44: TT probe -- before movegen AND eval, so a hit costs nothing.
     * CB-01 (b)/(c): game-state draws are decided BEFORE the probe (same
     * order negamax uses -- a TT hit must never mask a draw-by-rule). */
    uint64_t key = 0;
    uint32_t tt_move = 0;
    int use_qtt = g_use_tt && g_qs_tt && g_tt != NULL;
    if (g_score_hyg) {
        if (!(b->pawns | b->rooks | b->queens) && insufficient_material(b))
            return draw_score(b);            /* (c) dead-drawn exchanges */
        if (g_cb2 && hmc >= 100 && !in_chk)  /* CB-02(b): 50-move rule --
                                              * same in-check-plays-on
                                              * semantics as negamax */
            return draw_score(b);
        key = board_key(b);                  /* (b) every qsearch node logs
                                              * its key so in-check nodes can
                                              * see even-distance ancestors */
        g_path[ply] = key;
        if (in_chk && hmc >= 4 && is_repetition(key, ply, hmc))
            return draw_score(b);            /* perpetual-check lines */
    }
    int tt_eval = TT_EVAL_NONE;      /* FI-03: cached static eval, if any */
    int qs_sh_flag = -1, qs_sh_val = 0;  /* FI-30(a): hit's flag+value */
    if (use_qtt) {
        if (!key) key = board_key(b);
        TTEntry e;
        if (tt_load(&g_tt[key & TT_MASK], key, &e)) {
            tt_eval = TT_EVAL(e);
            if (g_use_nnue && TT_DEPTH(e) != 0)
                tt_eval = TT_EVAL_NONE;  /* F49-B02: depth>=1 store = NN
                                          * origin -- an HCE stand-pat must
                                          * not consume the NN scale */
            int v = TT_VALUE(e);                     /* ply-relative -> node */
            if (v >= MATE_THRESH) v -= ply;
            else if (v <= -MATE_THRESH) v += ply;
            int fl = TT_FLAG(e);
            qs_sh_flag = fl; qs_sh_val = v;          /* FI-30(a) */
            if (!(g_pv_exact && is_pv)              /* PV-02: PV nodes walk on */
                    && !tt_r50_stale(hmc, v)) {     /* FI-53: stale decisive
                                                     * values fall through */
                if (fl == TT_EXACT) return v;
                if (fl == TT_LOWER && v >= beta) return v;
                if (fl == TT_UPPER && v <= alpha) return v;
                if (g_score_hyg && fl == TT_LOWER && v > alpha)
                    alpha = v;               /* CB-01 (e): proven lower bound
                                              * sharpens delta pruning below */
                else if (g_qs_beta_narrow && fl == TT_UPPER && v < beta)
                    beta = v;                /* FI-50: proven ceiling, mirror of
                                              * (e); v > alpha here (2372
                                              * returned otherwise) so the
                                              * window never collapses */
            }
            tt_move = TT_MOVE(e);
        }
    }
    int alpha_orig = alpha;      /* FB-26: captured AFTER the CB-01(e)
                                  * narrowing, like negamax -- the store
                                  * below must never label a value that only
                                  * beat the PRE-narrow alpha as EXACT */

    int color = b->turn, best, stand = 0, raw_stand = 0;
    uint32_t moves[256];
    int n;
    if (in_chk) {
        n = gen_legal(b, moves);                     /* full evasions */
        if (n == 0) {                                /* checkmate */
            if (use_qtt)                             /* FI-54: permanent fact */
                tt_store_terminal(&g_tt[key & TT_MASK], key,
                                  -CS_INF + ply, ply);
            return -CS_INF + ply;
        }
        best = -CS_INF;
    } else if (g_qs_lazy && g_qgen) {
        /* P-46: eval + stand-pat BEFORE generation -- a large share of
         * qsearch nodes exit right here, and their movegen was pure waste.
         * Stalemate semantics preserved exactly: before returning stand we
         * confirm a legal move exists (early-exit quiet scan first -- the
         * common instant hit -- then the noisy list for locked positions);
         * no legal move at all is still a 0 draw, never an eval.
         * VALUE-IDENTICAL to the v35 path at every node => node-identical. */
        stand = (tt_eval != TT_EVAL_NONE) ? tt_eval    /* FI-03: exact cache */
                                          : eval_full_stm(b);
        raw_stand = stand;               /* FI-30: the FI-03 store stays RAW */
        stand = qs_sharpen_stand(stand, qs_sh_flag, qs_sh_val);   /* FB-40 */
        if (stand >= beta) {                         /* fail-soft stand-pat */
            if (has_legal_quiet(b) || gen_noisy(b, moves) > 0) {
                if (use_qtt && !CS_UNWINDING())      /* P-44: cache the cutoff */
                    qs_tt_store(key, stand, ply, 0, TT_LOWER, raw_stand, 0);
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
        stand = (tt_eval != TT_EVAL_NONE) ? tt_eval    /* FI-03: exact cache */
                                          : eval_full_stm(b);
        raw_stand = stand;               /* FI-30: the FI-03 store stays RAW */
        stand = qs_sharpen_stand(stand, qs_sh_flag, qs_sh_val);   /* FB-40 */
        if (stand >= beta) {                         /* fail-soft stand-pat */
            if (use_qtt && !CS_UNWINDING())          /* P-44: cache the cutoff */
                qs_tt_store(key, stand, ply, 0, TT_LOWER, raw_stand, 0);
            return stand;
        }
        if (stand > alpha) alpha = stand;
        best = stand;
    }

    uint32_t bm = 0;                                 /* P-44: best move found */
    int quiet_evasions = 0;                          /* FI-63: cap counter */
    /* CB-01 (g): plies past the killer table read the LAST slot, not the
     * root's (the old clamp-to-0 ordered deep qsearch with root killers). */
    int msc[256];                    /* FI-02.4: lazy pick, no up-front sort */
    order_moves(b, moves, n,
                ply < CS_MAXPLY ? ply : (g_score_hyg ? CS_MAXPLY - 1 : 0),
                0, tt_move, 0, msc);
    for (int i = 0; i < n; i++) {
        uint32_t m = pick_next(moves, msc, i, n);
        int victim   = (m >> MV_SHIFT_VICTIM) & 7;
        int is_promo = (m >> 12) & 7;
        /* FI-51: the search-proven TT move dodges the qsearch skips below. */
        int is_ttm = g_qs_ttm_exempt && tt_move && (m & 0x7FFF) == tt_move;
        if (in_chk && g_qs_evasion_cap && !victim && !is_promo) {
            /* FI-63: SF quietCheckEvasions -- cap fully-searched quiet
             * evasions, never while the node still reads mated. */
            if (quiet_evasions >= g_qs_evasion_cap && best > -MATE_THRESH)
                continue;
            quiet_evasions++;
        }
        if (!in_chk) {
            if (!victim && !is_promo) continue;      /* quiets: not in qsearch */
            if (victim && !is_promo) {               /* pure capture */
                /* Q-02: SEE only when the mover outranks the victim -- with
                 * mover <= victim the worst case after the recapture is
                 * victim - mover >= 0, so SEE can never be negative and the
                 * skip below can never fire. Node-identical; the ordering's
                 * SEE (order_moves / Stager) uses the same gate. */
                int mover = (m >> MV_SHIFT_MOVER) & 7;
                if (mover > victim && !is_ttm) {       /* FI-51: exempt TT move */
                    /* FI-02.3: ordering already ran this exact SEE -- read
                     * its tag (bits 22-23); fall back only if untagged. */
                    int tag = (m >> 22) & 3;
                    int neg = (tag == 2);
                    if (tag == 0) {
                        int from = m & 63, to = (m >> 6) & 63;
                        neg = see(b->pawns, b->knights, b->bishops, b->rooks,
                                  b->queens, b->kings, b->occ[WHITE],
                                  b->occ[BLACK],
                                  color, from, to, (m & MV_BIT_EP) ? 1 : 0) < 0;
                    }
                    if (neg) continue;               /* skip losing captures */
                }
                /* CB-01 (a): the margin must cover the TEXEL value the
                 * synced eval actually awards (queen 1148), not the classic
                 * 900 -- else a saving queen recapture can be pruned. */
                if (!is_ttm                          /* FI-51: exempt TT move */
                    && stand + (g_score_hyg ? DELTA_VAL[victim]
                                            : PIECE_VAL[victim])
                             + g_delta_margin <= alpha)
                    continue;                        /* delta pruning */
            }
        }
        Board c = *b;
        apply_move(&c, m);
        TT_PREFETCH(c.key);                          /* FI-17 */
        int child_hmc = (victim || ((m >> MV_SHIFT_MOVER) & 7) == PT_PAWN
                         || is_promo) ? 0 : hmc + 1;
        int v = -qsearch(&c, -beta, -alpha, ply + 1, in_check(&c), child_hmc);
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
        qs_tt_store(key, best, ply, bm, flag,
                    in_chk ? TT_EVAL_NONE : raw_stand,   /* FI-03: RAW eval */
                    (g_qs_chk_d1 && in_chk) ? 1 : 0);    /* FI-52: depth-1 tag */
    }
    return best;
}

static int negamax(Board* b, int depth, int alpha, int beta, int ply,
                   uint32_t prev12, int in_chk, int hmc, int chk, int srb,
                   int seb)
{
    g_nodes++;
    g_pv_len[ply] = 0;     /* PV-01: see qsearch -- every exit path must
                            * leave a valid (empty) line. ply <= CS_MAXPLY
                            * here, well inside PV_MAX. */
    if (ply > g_seldepth) g_seldepth = ply;  /* FI-13a: UCI seldepth */
    CS_TIME_CHECK();
    if (in_chk < 0) in_chk = in_check(b);

    /* Hard ply bound (BUG-03): memory safety must not depend on the
     * Python-side depth cap -- search_bench takes an uncapped depth, and
     * future extensions may push ply past the root depth. g_killers is
     * sized [CS_MAXPLY] and g_path [CS_MAXPLY+8]; stop strictly below. */
    if (ply >= CS_MAXPLY)
        return in_chk ? 0 : (g_use_nnue ? nn_eval(b, ply) : eval_full_stm(b));

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
    /* FI-29: the side to move can FORCE a repetition with one reversible
     * move -- bound the node by the contempt draw one search earlier.
     * In-tree only (never the root), never in check (the shuffle move
     * would rarely be a legal evasion), alpha-raise not hard return (a
     * PV node keeps searching for better than the draw). */
    if (g_cycle && ply > 0 && !in_chk && hmc >= 3) {
        int d = draw_score(b);
        if (alpha < d && upcoming_repetition(b, key, ply, hmc)) {
            alpha = d;
            if (alpha >= beta) return alpha;
        }
    }

    /* CB-01 (f): mate-distance pruning -- no line from here can beat a mate
     * already forced at a shallower ply; two compares, prunes whole
     * subtrees in mate-bearing positions (and never changes the result).
     * NON-PV nodes only: the fastest-mate score lands EXACTLY on the
     * clamped beta, so at PV nodes the in-window pv_store condition
     * (v < beta) could never record the mating line -- first matetrack run
     * with the clamp everywhere: 470 Bad PVs, all fastest-mate lines. The
     * node savings live in the zero-window bulk anyway. */
    if (g_score_hyg && beta - alpha <= 1) {
        if (alpha < -CS_INF + ply)     alpha = -CS_INF + ply;
        if (beta  >  CS_INF - ply - 1) beta  =  CS_INF - ply - 1;
        if (alpha >= beta) return alpha;
    }

    if (depth <= 0)
        return g_qsearch ? qsearch(b, alpha, beta, ply, in_chk, hmc)
                         : eval_full_stm(b);

    /* P-33: the move excluded at THIS node by a singular verification
     * search (0 = normal search). Read once; the TT cutoff and the TT
     * store below are both suppressed while it is set. */
    uint32_t excluded = (ply < CS_MAXPLY + 8) ? g_excl[ply] : 0;

    /* --- TT probe -------------------------------------------------- */
    uint32_t tt_move = 0;
    int tt_eval = TT_EVAL_NONE;      /* FI-03: cached static eval, if any */
    int tt_sh_flag = -1, tt_sh_val = 0;          /* FI-25: hit's flag+value */
    int tt_depth = -1;               /* FI-55: hoisted for the IIR-weak
                                      * trigger (read-only -- must not
                                      * perturb the cutoff gate below) */
    TTEntry* tte = NULL;
    if (g_use_tt && g_tt) {         /* FB-13b: g_tt may be NULL (failed alloc) */
        tte = &g_tt[key & TT_MASK];
        TTEntry e;
        if (tt_load(tte, key, &e)) {
            tt_move = TT_MOVE(e);
            tt_eval = TT_EVAL(e);
            if (g_use_nnue && TT_DEPTH(e) < 1)
                tt_eval = TT_EVAL_NONE;  /* F49-B02: depth-0 store = HCE
                                          * origin -- negamax's NN static
                                          * eval must not consume it */
            tt_sh_flag = TT_FLAG(e);             /* FI-25: any-depth bound */
            tt_sh_val = TT_VALUE(e);
            tt_depth = TT_DEPTH(e);              /* FI-55 */
            /* PV-02: at PV nodes skip the whole cutoff/narrowing block (the
             * EXACT return AND the bound-narrowing both truncate the
             * collected PV); the TT move above still orders. */
            /* FI-49: fail-high tightening -- an equal-depth LOWER that would
             * cut (v >= beta) must be one ply deeper (SF-standard: fail-high
             * scores are unstable at equal depth). Non-mate only: tt_sh_val
             * is the RAW stored value, and the mate guard is exactly what
             * makes the beta comparison node-exact. Deliberate deviation
             * kept: an equal-depth EXACT with v > beta still returns (exact
             * scores carry no fail-high instability). fh_extra is 0 with
             * the toggle off = v49 byte-identical. */
            int fh_extra = (g_tt_fh_tight && TT_FLAG(e) == TT_LOWER
                            && tt_sh_val > -MATE_THRESH
                            && tt_sh_val < MATE_THRESH
                            && tt_sh_val >= beta) ? 1 : 0;
            if (TT_DEPTH(e) >= depth + fh_extra && !excluded  /* P-33 */
                    && !(g_pv_exact && (beta - alpha) > 1)) {
                int v = TT_VALUE(e);                /* ply-relative -> node */
                if (v >= MATE_THRESH) v -= ply;
                else if (v <= -MATE_THRESH) v += ply;
                if (!tt_r50_stale(hmc, v)) {        /* FI-53: stale decisive
                                                     * values near the 50-move
                                                     * horizon fall through */
                    if (TT_FLAG(e) == TT_EXACT) return v;
                    if (TT_FLAG(e) == TT_LOWER && v > alpha) alpha = v;
                    else if (TT_FLAG(e) == TT_UPPER && v < beta) beta = v;
                    if (alpha >= beta) return v;
                }
            } else if (g_tt_mate_cut && !excluded             /* P-33 */
                       && !(g_pv_exact && (beta - alpha) > 1)) {
                /* FI-54 probe arm: mate-range values cut regardless of
                 * stored depth -- a forced mate is depth-invariant. */
                int v = TT_VALUE(e);                /* ply-relative -> node */
                if (v >= MATE_THRESH) v -= ply;
                else if (v <= -MATE_THRESH) v += ply;
                if (v >= MATE_THRESH || v <= -MATE_THRESH) {
                    int fl = TT_FLAG(e);
                    if (fl == TT_EXACT) return v;
                    if (fl == TT_LOWER && v >= MATE_THRESH && v >= beta)
                        return v;
                    if (fl == TT_UPPER && v <= -MATE_THRESH && v <= alpha)
                        return v;
                }
            }
        }
    }
    int alpha_orig = alpha;                          /* AFTER the TT narrowing */
    int is_pv = (beta - alpha) > 1;

    /* P-03: IIR -- no TT move here, so ordering is blind; go shallower.
     * FI-55: an UPPER entry shallower than depth is weak evidence too
     * (SF trigger form: !ttMove || bound == UPPER).
     * (Not in check: reduced-depth evasion search is a tactical risk.) */
    {
        int tt_weak = g_iir_weak && tt_move
                      && tt_sh_flag == TT_UPPER && tt_depth < depth;
        if (g_iir && depth >= IIR_MIN_DEPTH && (!tt_move || tt_weak)
                && !in_chk)
            depth--;
    }

    /* static eval (for pruning); meaningless in check, unused at PV nodes
     * (P-04 additionally computes it at PV nodes to feed the eval stack). */
    int want_eval = !in_chk && (!is_pv || g_improving);
    int static_eval = want_eval
        ? (tt_eval != TT_EVAL_NONE ? tt_eval        /* FI-03: exact cache */
                                   : (g_use_nnue ? nn_eval(b, ply)   /* FI-15
                                       * hybrid: NN is negamax's static eval;
                                       * qsearch stand-pat stays HCE */
                                                 : eval_full_stm(b)))
        : 0;

    /* P-04: record this ply's eval and compare to our own two plies ago.
     * Every ancestor on the current path wrote its slot on the way down, so
     * ply-2 is always fresh; the root loop writes g_seval[0]. */
    int improving = 0;
    if (g_improving) {
        g_seval[ply] = in_chk ? SEVAL_NONE : static_eval;
        if (!in_chk && ply >= 2 && g_seval[ply - 2] != SEVAL_NONE)
            improving = static_eval > g_seval[ply - 2];
    }

    /* FI-25: sharpen the PRUNING eval with the TT's search value when its
     * bound provably improves the estimate (after the seval stack recorded
     * the raw value; the FI-03 stores below also keep static_eval raw). */
    int prune_eval = static_eval;
    if (g_tt_eval_sharpen && tt_sh_flag >= 0 && want_eval
            && tt_sh_val > -MATE_THRESH && tt_sh_val < MATE_THRESH) {
        if (tt_sh_flag == TT_EXACT
            || (tt_sh_flag == TT_LOWER && tt_sh_val > prune_eval)
            || (tt_sh_flag == TT_UPPER && tt_sh_val < prune_eval))
            prune_eval = tt_sh_val;
    }

    /* --- pre-move pruning (non-PV, not in check) ------------------- */
    if (g_prune && !is_pv && !in_chk && abs(beta) < MATE_THRESH) {
        /* reverse futility / static null-move (P-04: an improving node
         * prunes one ply deeper for the same eval; off/not-improving = v34) */
        if (depth <= g_rfp_depth && prune_eval - g_rfp_margin * (depth - improving) >= beta)
            return prune_eval;
        /* null-move pruning (hmc 0 below the null: repetition/50-move
         * cannot be tracked across a non-move, so disable them there) */
        if (depth >= 3 && prune_eval >= beta && has_non_pawn(b, b->turn)
            && !(g_null_nodouble && prev12 == 0xFFFFFFFF)   /* FI-24(a) */
            && !(g_cb2 && g_no_null)) {      /* CB-02(c): verify subtree */
            int R = g_null_base + depth / g_null_div;
            if (g_null_evalr) {              /* FI-24(b): eval-scaled R */
                int x = (prune_eval - beta) / 200;
                if (x > 2) x = 2;
                if (x > 0) R += x;
            }
            Board c = *b; make_null(&c);
            if (g_use_nnue)                  /* FI-15: null moves no pieces --
                                              * propagate the slot; nn_eval
                                              * swaps perspectives via turn */
                g_nn_acc[ply + 1] = g_nn_acc[ply];
            g_ctx[ply + 1] = 0;              /* Q-01: null = no context */
            int ns = -negamax(&c, depth - 1 - R, -beta, -beta + 1, ply + 1,
                              0xFFFFFFFF, 0, 0, chk, srb, seb);
            if (CS_UNWINDING()) return 0;            /* ns is garbage */
            if (ns >= beta) {
                int verified = 1;
                if (g_cb2 && g_null_verify && depth >= 10) {
                    /* CB-02(c): confirm the deep cutoff with a reduced
                     * no-null re-search at THIS node (same window). */
                    g_no_null = 1;
                    int vs = negamax(b, depth - 1 - R, beta - 1, beta, ply,
                                     prev12, in_chk, hmc, chk, srb, seb);
                    g_no_null = 0;
                    if (CS_UNWINDING()) return 0;
                    if (vs < beta) verified = 0;     /* zugzwang mis-cut */
                }
                if (verified) {
                    if (!g_score_hyg) return beta;   /* v37: fail-hard */
                    /* CB-01 (d): keep the fail-soft bound (tighter TT
                     * info); an unproven null-move MATE is never trusted
                     * -- clamp. */
                    if (ns >= MATE_THRESH) ns = beta;
                    if (g_use_tt && tte && ns < MATE_THRESH
                        && !CS_UNWINDING()) {
                        if (!g_cb2) {
                            tt_store_raw(tte, key, ns, 0, depth, TT_LOWER,
                                         static_eval);        /* FI-03 */
                        } else {
                            /* CB-02(a)/FB-22: obey the replacement policy;
                             * never clobber a DEEPER entry, and keep a
                             * same-key entry's move (ordering asset). */
                            TTEntry cur = *tte;
                            uint64_t ck = cur.key_x ^ cur.d1 ^ cur.d2;
                            if (ck == key) {
                                if (depth >= TT_DEPTH(cur)
                                    && !tt_exact_shield(cur, depth,
                                                        TT_LOWER))  /* FI-48 */
                                    tt_store_raw(tte, key, ns,
                                                 TT_MOVE(cur) & 0x7FFF,
                                                 depth, TT_LOWER,
                                                 static_eval);
                            } else if (TT_GEN(cur) != (int)(uint16_t)g_gen
                                       || depth >= TT_DEPTH(cur)
                                                   + tt_exact_bonus(cur)) {
                                tt_store_raw(tte, key, ns, 0, depth,
                                             TT_LOWER, static_eval);
                            }
                        }
                    }
                    return ns;
                }
            }
        }
    }

    /* FI-59: warm-start an untouched killer slot from two plies up, BEFORE
     * either ordering path snapshots g_killers[ply] -- so staged and array
     * see the same table (P-23 stream identity). */
    if (g_killer_inherit && ply >= 2 && ply < CS_MAXPLY
            && g_killers[ply][0] == 0) {
        g_killers[ply][0] = g_killers[ply - 2][0];
        g_killers[ply][1] = g_killers[ply - 2][1];
    }

    uint32_t counter_key = (prev12 != 0xFFFFFFFF) ? g_counter[prev12] : 0;

    /* P-23: staged ordering engages at not-in-check, full-ordering nodes
     * with P-43 off (single-reply needs the total move count up front);
     * everything else keeps the v35 generate-all path. */
    int staged = (g_staged == 1 && g_order_mode == 1 && !in_chk
                  && !g_single_reply);
    Stager st;
    uint32_t moves[256];
    int msc[256];                    /* FI-02.4: lazy-pick scores */
    int n = 0, sr_ext = 0;
    if (staged) {
        stager_init(&st, b, ply, counter_key, tt_move);
    } else {
        n = gen_legal(b, moves);
        if (n == 0) {                                /* ply-relative mate */
            int tv = in_chk ? -CS_INF + ply : 0;
            tt_store_terminal(tte, key, tv, ply);    /* FI-54 */
            return tv;
        }
        /* P-43: single-reply extension -- node-level, fires when this node
         * has exactly one legal move (spends from the srb budget). */
        sr_ext = (g_single_reply && n == 1 && srb > 0) ? 1 : 0;

        if (g_staged == 2 && g_order_mode == 1 && !in_chk && !g_single_reply) {
            /* VERIFY mode: the staged stream must equal order_moves' sorted
             * array move-for-move at every eligible node. */
            uint32_t ref[256];
            for (int i = 0; i < n; i++) ref[i] = moves[i];
            order_moves(b, ref, n, ply, counter_key, tt_move, 1, NULL);
            Stager vs;
            stager_init(&vs, b, ply, counter_key, tt_move);
            for (int i = 0; i < n; i++) {
                uint32_t sm = stager_next(&vs);
                /* FI-02.3 tags live in bits 22-23 of order_moves' output;
                 * the staged stream never sets them -- compare payload. */
                if ((sm & 0x3FFFFF) != (ref[i] & 0x3FFFFF)) {
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
        order_moves(b, moves, n, ply, counter_key, tt_move, 1, msc);
    }

    /* P-33: singular test -- is the TT move the ONLY playable move here?
     * Run once per node, before the loop, and remember the verdict for the
     * TT move's extension inside it. Skipped at the root (ply 0), inside
     * an exclusion search (no recursion), and when the entry is too
     * shallow / not a lower bound / mate-ranged. */
    int se_extend = 0;
    if (g_singular && tt_move && !excluded && ply > 0 && !in_chk
            && depth >= g_se_min_depth
            && tt_depth >= depth - 3
            && (tt_sh_flag == TT_LOWER || tt_sh_flag == TT_EXACT)
            && tt_sh_val > -MATE_THRESH && tt_sh_val < MATE_THRESH) {
        int sbeta = tt_sh_val - (g_se_margin * depth) / 64;
        int sdepth = (depth - 1) / 2;
        if (sdepth >= 1 && sbeta > -MATE_THRESH) {
            g_excl[ply] = tt_move;
            int sv = negamax(b, sdepth, sbeta - 1, sbeta, ply, prev12,
                             in_chk, hmc, chk, srb, seb);
            g_excl[ply] = 0;             /* ALWAYS restore, unwind included */
            if (CS_UNWINDING()) return 0;
            if (sv < sbeta) se_extend = 1;   /* nothing else reaches the bar */
        }
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
            m = pick_next(moves, msc, i, n);   /* FI-02.4 */
        }
        if (excluded && (m & 0x7FFF) == excluded) continue;   /* P-33 */
        if (i == 0) best_move = m;
        int victim = (m >> MV_SHIFT_VICTIM) & 7;
        int mover  = (m >> MV_SHIFT_MOVER) & 7;
        int fromto = (m & 63) << 6 | ((m >> 6) & 63);
        int quiet  = !victim && !((m >> 12) & 7);    /* not capture/promo */
        int child_hmc = (victim || mover == PT_PAWN) ? 0 : hmc + 1;

        if (quiet && best > -MATE_THRESH && nq >= lmp_lim)
            continue;                                /* late-move pruning */

        /* FI-18: SEE-losing capture? (staged: stage 6 emits exactly the
         * SEE-negative captures; array path: FI-02.3 tag 2 = SEE < 0) */
        int badcap = staged ? (st.stage == 6) : (((m >> 22) & 3) == 2);

        Board c = *b;
        apply_move(&c, m);
        TT_PREFETCH(c.key);                          /* FI-17 */
        g_ctx[ply + 1] = (uint16_t)((mover << 6) | ((m >> 6) & 63));  /* Q-01 */
        int gives_check = in_check(&c);

        if (g_see_prune && badcap && !is_pv && !in_chk && !gives_check
                && depth <= 3 && i >= 3 && best > -MATE_THRESH)
            continue;         /* FI-18: skip late losing captures near leaf */

        if (g_hist_prune && quiet && !is_pv && !in_chk && !gives_check
                && depth <= 3 && best > -MATE_THRESH
                && g_history[color][fromto] < -g_hist_prune)
            continue;         /* FI-23: skip history-punished quiets */

        if (g_prune && quiet && !is_pv && !in_chk && !gives_check && depth == 1
                && best > -MATE_THRESH
                && prune_eval + g_fut_margin
                   + ((g_improving && !improving) ? g_rfp_margin / 2 : 0) <= alpha)
            continue;    /* frontier futility (P-04: declining node cuts more) */

        if (quiet) {
            quiets_ck[nq] = (uint16_t)((mover << 6) | ((m >> 6) & 63));
            quiets[nq++] = fromto;
        }

        /* P-01 check extension (never combines with LMR: R needs !gives_check)
         * + P-43 single-reply (node-level; can stack, but n==1 => one line). */
        int ext = (g_check_ext && gives_check && chk > 0) ? 1 : 0;
        /* P-33: the singular TT move gets its extra ply from its OWN
         * budget (g_se_budget, threaded as seb) -- competing for the P-01
         * chk budget starved the check extensions that find mates
         * (measured: matetrack -34 found / -49 best). Independent budget
         * keeps stacking bounded without taxing P-01. */
        int se_ext = (se_extend && (m & 0x7FFF) == tt_move && seb > 0) ? 1 : 0;
        int nd = depth - 1 + ext + sr_ext + se_ext;
        int child_chk = chk - ext;
        int child_srb = srb - sr_ext;
        int child_seb = seb - se_ext;                /* P-33 budget */

        /* late-move reduction on quiet, late, non-checking moves */
        int R = 0;
        if (g_prune && depth >= 3 && i >= 3 && !in_chk && !gives_check
                && (quiet || (g_lmr_badcap && badcap))) {   /* FI-64 */
            R = g_lmr[depth < 64 ? depth : 63][i < 64 ? i : 63];
            if (is_pv && R) R--;
            if (g_improving && !improving) R++;      /* P-04: sharpen declining lines */
            if (g_lmr_hist && quiet) {               /* FI-04: history nudge --
                                                      * quiet-only butterfly
                                                      * table (FI-64 gate) */
                int adj = g_history[b->turn][((m & 63) << 6) | ((m >> 6) & 63)]
                        / g_lmr_hist;
                if (adj > 1) adj = 1; else if (adj < -1) adj = -1;
                R -= adj;
            }
            if (R > depth - 2) R = depth - 2;
            if (R < 0) R = 0;
        }

        if (g_use_nnue)      /* FI-15: child accumulator (after every prune
                              * gate above -- pruned moves never pay) */
            nn_push(ply, b, &c, m);

        uint32_t cp = (ply + 1 < CS_MAXPLY) ? (uint32_t)fromto : 0xFFFFFFFF;
        int v;
        if (i == 0) {
            v = -negamax(&c, nd, -beta, -alpha, ply + 1, cp, gives_check, child_hmc, child_chk, child_srb, child_seb);
        } else {                                     /* PVS scout (reduced) */
            v = -negamax(&c, nd - R, -alpha - 1, -alpha, ply + 1, cp, gives_check, child_hmc, child_chk, child_srb, child_seb);
            if (R && v > alpha)                      /* reduced scout beat alpha */
                v = -negamax(&c, nd, -alpha - 1, -alpha, ply + 1, cp, gives_check, child_hmc, child_chk, child_srb, child_seb);
            if (v > alpha && v < beta)               /* full-window PV re-search */
                v = -negamax(&c, nd, -beta, -alpha, ply + 1, cp, gives_check, child_hmc, child_chk, child_srb, child_seb);
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
            } else if (g_quiet_malus_all && nq > 0) {
                /* FI-60: a bad-capture/promo cutoff still punishes the
                 * quiets that were tried and failed. Cutter is NOT in
                 * quiets[] -> sweep the full nq. No bonus, no killer or
                 * counter write (those stay quiet-only). */
                int bonus = depth * depth;
                for (int q = 0; q < nq; q++)
                    hist_update(color, quiets[q], -bonus);
                if (g_cont_hist) {
                    int p1 = g_ctx[ply];
                    int p2 = (ply >= 1) ? g_ctx[ply - 1] : 0;
                    for (int q = 0; q < nq; q++) {
                        if (p1) cont_update(g_cont1[p1], quiets_ck[q], -bonus);
                        if (p2) cont_update(g_cont2[p2], quiets_ck[q], -bonus);
                    }
                }
            }
            break;
        }
    }

    /* P-23: on the staged path mate/stalemate is discovered by exhaustion --
     * no stage produced a single legal move. (best_move can only stay 0 with
     * zero moves streamed: the first streamed move is never skipped, since
     * LMP/futility both require best > -MATE_THRESH.) Return BEFORE the TT
     * store, exactly like the v35 n==0 path. */
    if (staged && best_move == 0) {
        int tv = in_chk ? -CS_INF + ply : 0;
        tt_store_terminal(tte, key, tv, ply);        /* FI-54 */
        return tv;
    }

    /* --- TT store: gen-aware depth-preferred, ply-relative mates ---- *
     * Same key: deeper-or-equal wins. Different key: an entry from an older
     * search generation is freely replaceable, a current-gen one only for
     * deeper-or-equal depth (the TT persists across ID iterations/moves).
     * Never store while unwinding (belt-and-braces: the loop returns before
     * reaching here, but a garbage store would poison EVERY later search
     * through the shared, persistent table). */
    if (g_use_tt && tte && !CS_UNWINDING()) {   /* FB-13b: tte NULL when TT-less */
        TTEntry cur = *tte;
        uint64_t cur_key = cur.key_x ^ cur.d1 ^ cur.d2;
        /* FI-48: flag hoisted above the replace test (the shield needs it). */
        int flag = (best <= alpha_orig) ? TT_UPPER
                 : (best >= beta)       ? TT_LOWER : TT_EXACT;
        int replace = (cur_key == key)
                    ? (TT_DEPTH(cur) <= depth
                       && !tt_exact_shield(cur, depth, flag))   /* FI-48 */
                    : (TT_GEN(cur) != (int)(uint16_t)g_gen
                       || TT_DEPTH(cur) + tt_exact_bonus(cur) <= depth);
        if (replace) {
            int sv = best;
            if (sv >= MATE_THRESH) sv += ply;
            else if (sv <= -MATE_THRESH) sv -= ply;
            /* 0x7FFF, not 0xFFFF: bit 15 is the mover PT's low bit
             * (MV_SHIFT_MOVER = 15). Storing it made the probe-side
             * `(m & 0x7FFF) == tt_move` never match for odd mover PTs
             * (pawn/bishop/queen) -- TT-move ordering was silently dead
             * for those movers. */
            tt_store_raw(tte, key, sv, best_move & 0x7FFF, depth, flag,
                         want_eval ? static_eval : TT_EVAL_NONE);  /* FI-03 */
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
    if (g_cont_hist) {                       /* Q-01: same per-move lifecycle
                                              * (dormant: skip the ~784KiB
                                              * clear, the tables are unread) */
        memset(g_cont1, 0, sizeof(g_cont1));
        memset(g_cont2, 0, sizeof(g_cont2));
    }
    memset(g_ctx, 0, sizeof(g_ctx));
    g_root_pv_len = 0;                   /* PV-01: fresh line per game move */
    g_seldepth = 0;                      /* FI-13a: per-move seldepth */
    g_helper_nodes = 0;                  /* Lazy-SMP helper node aggregate */
    if (g_tt == NULL && g_use_tt) {
        /* Q-13 + FB-13b: degrade, don't segfault -- and RETRY each move
         * instead of latching g_use_tt=0 forever (a transient failure would
         * have permanently disabled the TT). Consumers guard on g_tt. */
        g_tt = (TTEntry*)calloc(TT_SIZE, sizeof(TTEntry));
        if (g_tt == NULL)
            fprintf(stderr, "csearch: TT calloc(%zu) failed -- searching "
                    "without a transposition table this move\n",
                    (size_t)TT_SIZE * sizeof(TTEntry));
    }
    if (!g_lmr_ready) init_lmr();
    g_gen = (g_gen + 1) & 0x7FFF;        /* old entries become replaceable */
}

/* Root PVS body, shared by the main thread and the Lazy-SMP helpers: full
 * width inside [alpha, beta), no pruning; the only reduction is FI-56's
 * opt-in late-quiet-move LMR scout (g_root_lmr, default off = the
 * historical no-reductions root). Returns the best move's 15-bit key;
 * *out_done counts root moves fully searched (a stop mid-move leaves it
 * short). */
static uint32_t root_search(const Board* rb, int depth, int alpha, int beta,
                            uint32_t prev_key, int hmc,
                            int* out_score, int* out_done, int* out_second)
{
    Board b = *rb;
    uint64_t key = board_key(&b);
    g_path[0] = key;
    if (g_use_nnue)          /* FI-15: seed the per-thread accumulator stack
                              * (root_search is the shared entry for the main
                              * thread AND every SMP helper -- __thread) */
        nn_refresh(&b, 0);
    /* PV-01: g_root_pv is NOT zeroed here -- at short TCs the final ID
     * iteration almost always aborts mid-search, and zeroing per iteration
     * wiped the exact line the last COMPLETED iteration collected (the
     * driver then fell back to the TT walk for the very emit that matters:
     * matetrack Bad-PVs stayed ~59%). The table is zeroed per game move in
     * cs_search_begin; within a move the last in-window result wins, and
     * the driver's first_move==pv[0] check catches any staleness. */
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
    *out_second = -CS_INF;               /* FI-09(b): no 2nd move yet */
    if (n == 0) {                        /* mate/stalemate at the root */
        *out_score = in_check(&b) ? -CS_INF : 0;
        return 0;
    }
    /* FI-06(b): no previous best yet (iteration 1 of a fresh game move)
     * -> seed the ordering from the persistent TT's stored best move. */
    if (g_root_order && !g_is_helper && !prev_key && g_use_tt && g_tt) {
        TTEntry e;
        if (tt_load(&g_tt[key & TT_MASK], key, &e))
            prev_key = TT_MOVE(e);
    }
    order_moves(&b, moves, n, 0, 0, prev_key, 1, NULL);

    /* FI-06(a): same root position as the recorded iteration -> keep the
     * ordered-first move, stable-sort the rest by prior subtree node
     * counts descending (unknown moves count 0 and keep their v45
     * relative order behind the known ones). */
    if (g_root_order && !g_is_helper && g_ro_key == key && g_ro_n > 0) {
        uint64_t cnt[256];
        for (int i = 1; i < n; i++) {
            uint16_t mk = (uint16_t)(moves[i] & 0x7FFF);
            cnt[i] = 0;
            for (int j = 0; j < g_ro_n; j++)
                if (g_ro_mv[j] == mk) { cnt[i] = g_ro_cnt[j]; break; }
        }
        for (int i = 2; i < n; i++) {              /* stable insertion sort */
            uint32_t m = moves[i]; uint64_t c = cnt[i];
            int j = i - 1;
            while (j >= 1 && cnt[j] < c) {
                moves[j + 1] = moves[j]; cnt[j + 1] = cnt[j]; j--;
            }
            moves[j + 1] = m; cnt[j + 1] = c;
        }
    }

    int root_chk = in_check(&b);                   /* FI-56: LMR gate input */
    int best = -CS_INF, best2 = -CS_INF;           /* FI-09(b): best2 = 2nd-best
                                                    * root score (upper bound
                                                    * for the failing scouts) */
    uint32_t best_move = 0;                        /* first SEARCHED move below
                                                    * (== moves[0] unless the
                                                    * MultiPV list excludes it) */
    int record = g_root_order && !g_is_helper && !g_rx_n;  /* FI-06 bookkeeping */
    uint16_t l_mv[256]; uint64_t l_cnt[256];       /* FI-06: this iteration */
    for (int i = 0; i < n; i++) {
        uint32_t m = moves[i];
        if (g_rx_n) {                              /* MultiPV: skip excluded */
            int k15 = m & 0x7FFF, skip = 0;
            for (int j = 0; j < g_rx_n; j++)
                if (g_rx[j] == k15) { skip = 1; break; }
            if (skip) continue;
        }
        if (!best_move) best_move = m;             /* abort-fallback = first
                                                    * searched (= v46 exact
                                                    * when no exclusions) */
        uint64_t nodes0 = record ? g_nodes : 0;
        int victim = (m >> MV_SHIFT_VICTIM) & 7;
        int mover  = (m >> MV_SHIFT_MOVER) & 7;
        int child_hmc = (victim || mover == PT_PAWN) ? 0 : hmc + 1;
        Board c = b;
        apply_move(&c, m);
        TT_PREFETCH(c.key);                          /* FI-17 */
        if (g_use_nnue) nn_push(0, &b, &c, m);       /* FI-15: slot 0 -> 1 */
        int gc = in_check(&c);
        g_ctx[1] = (uint16_t)((((m >> MV_SHIFT_MOVER) & 7) << 6)
                              | ((m >> 6) & 63));    /* Q-01 child context */
        uint32_t cp = (uint32_t)((m & 63) << 6 | ((m >> 6) & 63));
        int v;
        if (i == 0) {
            v = -negamax(&c, depth - 1, -beta, -alpha, 1, cp, gc, child_hmc, g_check_ext_budget, SR_EXT_MAX, g_se_budget);
        } else {
            /* FI-56: reduced zero-window scout for late quiet root moves;
             * a scout that beats alpha verifies at full depth before the
             * full-window re-search (negamax's three-step cascade). */
            int R = 0;
            if (g_root_lmr && depth >= 3 && i >= 4
                    && !root_chk && !gc
                    && !victim && !((m >> 12) & 7)) {  /* quiet, no promo */
                R = g_lmr[depth < 64 ? depth : 63][i < 64 ? i : 63] / 2;
                if (R > depth - 2) R = depth - 2;
            }
            v = -negamax(&c, depth - 1 - R, -alpha - 1, -alpha, 1, cp, gc, child_hmc, g_check_ext_budget, SR_EXT_MAX, g_se_budget);
            if (R && v > alpha && !g_abort && !(g_is_helper && g_hstop))
                v = -negamax(&c, depth - 1, -alpha - 1, -alpha, 1, cp, gc, child_hmc, g_check_ext_budget, SR_EXT_MAX, g_se_budget);
            if (v > alpha && v < beta)
                v = -negamax(&c, depth - 1, -beta, -alpha, 1, cp, gc, child_hmc, g_check_ext_budget, SR_EXT_MAX, g_se_budget);
        }
        if (g_abort || (g_is_helper && g_hstop))
            break;                                   /* v is garbage */
        (*out_done)++;
        if (record) {                                /* FI-06: subtree size */
            l_mv[*out_done - 1] = (uint16_t)(m & 0x7FFF);
            l_cnt[*out_done - 1] = g_nodes - nodes0;
        }
        if (v > alpha && v < beta) {                 /* PV-01: root prepend */
            int cl = g_pv_len[1];
            g_root_pv[0] = m;
            memcpy(&g_root_pv[1], g_pv[1], (size_t)cl * sizeof(uint32_t));
            g_root_pv_len = cl + 1;
        }
        if (v > best) { best2 = best; best = v; best_move = m; }  /* FI-09(b) */
        else if (v > best2) best2 = v;
        if (v > alpha) alpha = v;
        if (alpha >= beta) break;                    /* aspiration fail-high */
    }

    /* FI-06: publish this iteration's counts for the next one. A partial
     * record (aspiration fail-high cuts the loop early) never overwrites a
     * fuller one for the same position -- the widest recent picture wins. */
    if (record && *out_done > 0
            && (g_ro_key != key || *out_done >= g_ro_n)) {
        g_ro_key = key;
        g_ro_n = *out_done;
        memcpy(g_ro_mv, l_mv, (size_t)*out_done * sizeof(uint16_t));
        memcpy(g_ro_cnt, l_cnt, (size_t)*out_done * sizeof(uint64_t));
    }

    /* Root TT store (feeds the next iteration's ordering + the PV walk).
     * Suppressed during a MultiPV exclusion search: a 2nd-best line's move
     * must never replace the root's true best in the persistent table. */
    if (g_use_tt && g_tt && !g_abort && !(g_is_helper && g_hstop)
            && *out_done > 0 && !g_rx_n) {
        int flag = (best <= alpha_orig) ? TT_UPPER
                 : (best >= beta)       ? TT_LOWER : TT_EXACT;
        tt_store_raw(&g_tt[key & TT_MASK], key, best,
                     best_move & 0x7FFF, depth, flag, TT_EVAL_NONE);
    }
    *out_score = best;
    *out_second = best2;                             /* FI-09(b) */
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
void set_threads(int n) { g_threads = (n < 1) ? 1 : (n > 256 ? 256 : n); }

typedef struct { Board b; int depth, hmc; uint32_t prev; } HelperArg;

static void* helper_entry(void* p)
{
    HelperArg* a = (HelperArg*)p;
    g_is_helper = 1;
    g_nodes = 0;
    int score, done, second;
    root_search(&a->b, a->depth, -CS_INF, CS_INF, a->prev, a->hmc,
                &score, &done, &second);
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
                        int* out_done, int* out_aborted, int* out_second)
{
    Board b = make_board(pawns, knights, bishops, rooks, queens, kings,
                         occ_w, occ_b, turn, ep, castling);

    /* Helpers only pay off once the tree is non-trivial. */
    pthread_t tids[256];
    HelperArg args[256];
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

    int score, done, second;
    uint32_t mv = root_search(&b, depth, alpha, beta, prev_key, hmc,
                              &score, &done, &second);

    if (nh) {
        g_hstop = 1;
        for (int i = 0; i < nh; i++) pthread_join(tids[i], NULL);
        g_hstop = 0;
    }
    *out_done = done;
    *out_aborted = g_abort ? 1 : 0;
    *out_nodes = g_nodes + __atomic_load_n(&g_helper_nodes, __ATOMIC_RELAXED);
    *out_score = score;
    *out_second = second;                    /* FI-09(b) */
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
    int done, aborted, second;
    return cs_search_root(pawns, knights, bishops, rooks, queens, kings,
                          occ_w, occ_b, turn, ep, castling,
                          depth, -CS_INF, CS_INF, 0, 0,
                          out_nodes, out_score, &done, &aborted, &second);
}

int csearch_abi(void) { return 25; }  /* 25 = FI-59/60 set_killer_inherit/set_quiet_malus_all;
                                       * 24 = P-33 set_singular/set_singular_params (singular extensions);
                                       * 23 = FI-63 set_qs_evasion_cap (quiet check-evasion cap);
                                       * 22 = FI-24ab set_null_nodouble/set_null_evalr;
                                       * 21 = FI-64 set_lmr_badcap (badcap LMR);
                                       * 20 = FI-55 set_iir_weak (IIR weak-evidence trigger);
                                       * 19 = FI-15 NNUE build-out (set_use_nnue/
                                       * nnue_load/nnue_ready/set_nnue_verify/
                                       * nnue_verify_stats + nnue_* oracles);
                                       * 18 = FI-56 set_root_lmr (root-move LMR);
                                       * 17 = FI-53/54 set_tt_r50 + set_term_store/set_tt_mate_cut;
                                       * 16 = FI-49 set_tt_fh_tight (fail-high depth tightening);
                                       * 15 = FI-48 set_tt_keep_exact (flag-aware TT replacement);
                                       * 14 = FI-50/51/52 qsearch-TT batch
                                       * (set_qs_beta_narrow/set_qs_ttm_exempt/set_qs_chk_d1);
                                       * 13 = FI-29 set_cycle (cuckoo upcoming-repetition);
                                       * 12 = FI-30 set_qs_tt_sharpen/set_qs_keep_move; 11 = cs_search_root out_second
                                       * (FI-09b easy-move 2nd-best score);
                                       * 10 = root_exclude_* (MultiPV);
                                       * 9 = set_tt_bits (FI-10 Hash);
                                       * 8 = set_score_hygiene (CB-01);
                                       * 7 = set_node_limit + cs_seldepth +
                                       * cs_hashfull (FB-09/FI-13);
                                       * 6 = cs_get_pv (PV-01) + set_pv_exact
                                       * + set_check_ext_budget; 5 = Lazy SMP
                                       * (set_threads) + cs_stop */
