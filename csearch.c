/* csearch.c -- ISOLATED C search-core prototype (roadmap #29/#30, phase 1-2).
 * Board layer extracted verbatim from movegen.c (static, perft-verified);
 * material + full mobility/king-safety eval + fixed-depth alpha-beta appended
 * below to measure the real per-node NPS ceiling for the GO/NO-GO gate.
 * Does NOT touch the shipped movegen.so/eval_c.so.
 *
 * Build (links eval_c.c for the mobility/king-safety term + Constants.c):
 *   clang -O3 -march=native -shared -fPIC -w -I. \
 *         -o csearch.so csearch.c eval_c.c Constants.c
 *
 * GATE RESULT (2026-07-08): full-eval C alpha-beta ~13.5M nodes/s vs the
 * Python engine's ~90k = ~150x. GO for phase 3 (full C search core). */

#include <stdint.h>
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

static int eval_full_stm(const Board* b)
{
    int mat = eval_material_stm(b);
    uint64_t wp = b->pawns & b->occ[WHITE], bp = b->pawns & b->occ[BLACK];
    int white_pos = mobility_king_safety(b->occ[WHITE], b->occ[BLACK],
        b->knights, b->bishops, b->rooks, b->queens, wp, bp, b->kings,
        game_phase(b));
    return mat + ((b->turn == WHITE) ? white_pos : -white_pos);
}

static uint64_t g_nodes;
#define CS_INF 30000

/* MVV-LVA-ish: sort captures (higher victim first) before quiets, in place. */
static void order_moves(uint32_t* mv, int n)
{
    /* insertion sort by victim PT (bits 18-20) descending -- n is small */
    for (int i = 1; i < n; i++) {
        uint32_t x = mv[i];
        int xv = (x >> MV_SHIFT_VICTIM) & 7;
        int j = i - 1;
        while (j >= 0 && (((mv[j] >> MV_SHIFT_VICTIM) & 7) < xv)) {
            mv[j + 1] = mv[j]; j--;
        }
        mv[j + 1] = x;
    }
}

static int negamax(Board* b, int depth, int alpha, int beta)
{
    g_nodes++;
    if (depth == 0) return eval_full_stm(b);

    uint32_t moves[256];
    int n = gen_legal(b, moves);
    if (n == 0)                       /* mate or stalemate */
        return in_check(b) ? -CS_INF + (100 - depth) : 0;

    order_moves(moves, n);
    int best = -CS_INF;
    for (int i = 0; i < n; i++) {
        Board c = *b;
        apply_move(&c, moves[i]);
        int v = -negamax(&c, depth - 1, -beta, -alpha);
        if (v > best) best = v;
        if (v > alpha) alpha = v;
        if (alpha >= beta) break;     /* fail-hard cutoff */
    }
    return best;
}

/* Exported: run a fixed-depth alpha-beta from the given position, return the
 * best root move in the low 16 bits and the node count via *out_nodes. */
uint32_t search_bench(uint64_t pawns, uint64_t knights, uint64_t bishops,
                      uint64_t rooks, uint64_t queens, uint64_t kings,
                      uint64_t occ_w, uint64_t occ_b,
                      int turn, int ep, uint64_t castling,
                      int depth, uint64_t* out_nodes)
{
    Board b = make_board(pawns, knights, bishops, rooks, queens, kings,
                         occ_w, occ_b, turn, ep, castling);
    g_nodes = 0;
    uint32_t moves[256];
    int n = gen_legal(&b, moves);
    order_moves(moves, n);
    int best = -CS_INF, alpha = -CS_INF, beta = CS_INF;
    uint32_t best_move = n ? moves[0] : 0;
    for (int i = 0; i < n; i++) {
        Board c = b;
        apply_move(&c, moves[i]);
        int v = -negamax(&c, depth - 1, -beta, -alpha);
        if (v > best) { best = v; best_move = moves[i]; }
        if (v > alpha) alpha = v;
    }
    *out_nodes = g_nodes;
    return best_move & 0xFFFF;
}

int csearch_abi(void) { return 1; }
