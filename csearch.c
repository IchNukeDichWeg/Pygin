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

#include <string.h>

static uint64_t g_nodes;
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
 * (from==to is illegal), so 0 doubles as the "empty" sentinel. */
static int      g_history[2][4096];
static uint32_t g_killers[CS_MAXPLY][2];
static uint32_t g_counter[4096];

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

typedef struct {
    uint64_t key;
    int32_t  value;
    uint32_t move;      /* 16-bit move key in low bits */
    int16_t  depth;
    int16_t  flag;
} TTEntry;

static TTEntry* g_tt = NULL;
static int g_use_tt = 1;
void set_use_tt(int v) { g_use_tt = v; }

static inline uint64_t board_key(const Board* b)
{
    uint64_t h = 0x9E3779B97F4A7C15ULL, x;
    #define MIX(v) x = (v); h ^= x; h *= 0xFF51AFD7ED558CCDULL; h ^= h >> 29;
    MIX(b->pawns) MIX(b->knights) MIX(b->bishops)
    MIX(b->rooks) MIX(b->queens) MIX(b->kings)
    MIX(b->occ[WHITE]) MIX(b->castling)
    MIX((uint64_t)b->turn | ((uint64_t)(b->ep + 1) << 8))
    #undef MIX
    return h;
}

static void order_moves(const Board* b, uint32_t* mv, int n, int ply,
                        uint32_t counter_key, uint32_t tt_move)
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
            else                          s = g_history[color][(from << 6) | to];
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

/* --- Phase-3 step 3: pruning ------------------------------------------ */
#include <math.h>
static int g_prune = 1;                 /* 0 = no pruning (verification) */
void set_prune(int v) { g_prune = v; }

#define RFP_MARGIN   80                 /* per ply, reverse-futility */
#define FUT_MARGIN  150                 /* frontier futility */
static const int LMP_COUNT[4] = {0, 6, 10, 14};   /* by depth 1..3 */

static int g_lmr[64][64];
static int g_lmr_ready = 0;
static void init_lmr(void)
{
    for (int d = 1; d < 64; d++)
        for (int m = 1; m < 64; m++)
            g_lmr[d][m] = (int)(0.75 + log((double)d) * log((double)m) / 2.0);
    g_lmr_ready = 1;
}

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
#define DELTA_MARGIN 200

static int qsearch(Board* b, int alpha, int beta, int ply, int in_chk)
{
    g_nodes++;
    if (in_chk < 0) in_chk = in_check(b);
    if (ply >= CS_MAXPLY + 60)                       /* hard recursion guard */
        return in_chk ? 0 : eval_full_stm(b);

    int color = b->turn, best, stand = 0;
    uint32_t moves[256];
    int n = gen_legal(b, moves);
    if (in_chk) {
        if (n == 0) return -CS_INF + ply;            /* checkmate */
        best = -CS_INF;
    } else {
        if (n == 0) return 0;                        /* stalemate: draw, not eval */
        stand = eval_full_stm(b);
        if (stand >= beta) return stand;             /* fail-soft stand-pat */
        if (stand > alpha) alpha = stand;
        best = stand;
    }

    order_moves(b, moves, n, ply < CS_MAXPLY ? ply : 0, 0, 0);
    for (int i = 0; i < n; i++) {
        uint32_t m = moves[i];
        int victim   = (m >> MV_SHIFT_VICTIM) & 7;
        int is_promo = (m >> 12) & 7;
        if (!in_chk) {
            if (!victim && !is_promo) continue;      /* quiets: not in qsearch */
            if (victim && !is_promo) {               /* pure capture */
                int from = m & 63, to = (m >> 6) & 63;
                int sv = see(b->pawns, b->knights, b->bishops, b->rooks,
                             b->queens, b->kings, b->occ[WHITE], b->occ[BLACK],
                             color, from, to, (m & MV_BIT_EP) ? 1 : 0);
                if (sv < 0) continue;                /* skip losing captures */
                if (stand + PIECE_VAL[victim] + DELTA_MARGIN <= alpha)
                    continue;                        /* delta pruning */
            }
        }
        Board c = *b;
        apply_move(&c, m);
        int v = -qsearch(&c, -beta, -alpha, ply + 1, in_check(&c));
        if (v > best) best = v;
        if (v > alpha) alpha = v;
        if (alpha >= beta) break;
    }
    return best;
}

static int negamax(Board* b, int depth, int alpha, int beta, int ply,
                   uint32_t prev12, int in_chk)
{
    g_nodes++;
    if (in_chk < 0) in_chk = in_check(b);
    if (depth <= 0)
        return g_qsearch ? qsearch(b, alpha, beta, ply, in_chk)
                         : eval_full_stm(b);

    /* --- TT probe -------------------------------------------------- */
    uint64_t key = 0;
    uint32_t tt_move = 0;
    TTEntry* tte = NULL;
    if (g_use_tt) {
        key = board_key(b);
        tte = &g_tt[key & TT_MASK];
        if (tte->key == key) {
            tt_move = tte->move;
            if (tte->depth >= depth) {
                int v = tte->value;                 /* ply-relative -> node */
                if (v >= MATE_THRESH) v -= ply;
                else if (v <= -MATE_THRESH) v += ply;
                if (tte->flag == TT_EXACT) return v;
                if (tte->flag == TT_LOWER && v > alpha) alpha = v;
                else if (tte->flag == TT_UPPER && v < beta) beta = v;
                if (alpha >= beta) return v;
            }
        }
    }
    int alpha_orig = alpha;                          /* AFTER the TT narrowing */
    int is_pv = (beta - alpha) > 1;

    /* static eval (for pruning); meaningless in check, unused at PV nodes. */
    int static_eval = (!in_chk && !is_pv) ? eval_full_stm(b) : 0;

    /* --- pre-move pruning (non-PV, not in check) ------------------- */
    if (g_prune && !is_pv && !in_chk && abs(beta) < MATE_THRESH) {
        /* reverse futility / static null-move */
        if (depth <= 6 && static_eval - RFP_MARGIN * depth >= beta)
            return static_eval;
        /* null-move pruning */
        if (depth >= 3 && static_eval >= beta && has_non_pawn(b, b->turn)) {
            int R = 2 + depth / 6;
            Board c = *b; make_null(&c);
            int ns = -negamax(&c, depth - 1 - R, -beta, -beta + 1, ply + 1,
                              0xFFFFFFFF, 0);
            if (ns >= beta) return beta;
        }
    }

    uint32_t moves[256];
    int n = gen_legal(b, moves);
    if (n == 0)
        return in_chk ? -CS_INF + ply : 0;           /* ply-relative mate */

    uint32_t counter_key = (prev12 != 0xFFFFFFFF) ? g_counter[prev12] : 0;
    order_moves(b, moves, n, ply, counter_key, tt_move);

    int color = b->turn, best = -CS_INF;
    uint32_t best_move = moves[0];
    uint32_t quiets[256]; int nq = 0;
    int lmp_lim = (g_prune && !is_pv && !in_chk && depth <= 3) ? LMP_COUNT[depth] : 999;
    for (int i = 0; i < n; i++) {
        uint32_t m = moves[i];
        int victim = (m >> MV_SHIFT_VICTIM) & 7;
        int fromto = (m & 63) << 6 | ((m >> 6) & 63);
        int quiet  = !victim && !((m >> 12) & 7);    /* not capture/promo */

        if (quiet && best > -MATE_THRESH && nq >= lmp_lim)
            continue;                                /* late-move pruning */

        Board c = *b;
        apply_move(&c, m);
        int gives_check = in_check(&c);

        if (g_prune && quiet && !is_pv && !in_chk && !gives_check && depth == 1
                && best > -MATE_THRESH && static_eval + FUT_MARGIN <= alpha)
            continue;                                /* frontier futility */

        if (quiet) quiets[nq++] = fromto;

        /* late-move reduction on quiet, late, non-checking moves */
        int R = 0;
        if (g_prune && depth >= 3 && i >= 3 && quiet && !in_chk && !gives_check) {
            R = g_lmr[depth < 64 ? depth : 63][i < 64 ? i : 63];
            if (is_pv && R) R--;
            if (R > depth - 2) R = depth - 2;
            if (R < 0) R = 0;
        }

        uint32_t cp = (ply + 1 < CS_MAXPLY) ? (uint32_t)fromto : 0xFFFFFFFF;
        int v;
        if (i == 0) {
            v = -negamax(&c, depth - 1, -beta, -alpha, ply + 1, cp, gives_check);
        } else {                                     /* PVS scout (reduced) */
            v = -negamax(&c, depth - 1 - R, -alpha - 1, -alpha, ply + 1, cp, gives_check);
            if (R && v > alpha)                      /* reduced scout beat alpha */
                v = -negamax(&c, depth - 1, -alpha - 1, -alpha, ply + 1, cp, gives_check);
            if (v > alpha && v < beta)               /* full-window PV re-search */
                v = -negamax(&c, depth - 1, -beta, -alpha, ply + 1, cp, gives_check);
        }

        if (v > best) { best = v; best_move = m; }
        if (v > alpha) alpha = v;
        if (alpha >= beta) {                         /* fail-hard cutoff */
            if (quiet) {
                int bonus = depth * depth;
                hist_update(color, fromto, bonus);
                for (int q = 0; q < nq - 1; q++)
                    hist_update(color, quiets[q], -bonus);
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

    /* --- TT store: depth-preferred, ply-relative mate encoding ----- */
    if (g_use_tt && (tte->key != key || tte->depth <= depth)) {
        int flag = (best <= alpha_orig) ? TT_UPPER
                 : (best >= beta)       ? TT_LOWER : TT_EXACT;
        int sv = best;
        if (sv >= MATE_THRESH) sv += ply;
        else if (sv <= -MATE_THRESH) sv -= ply;
        /* 0x7FFF, not 0xFFFF: bit 15 is the mover PT's low bit (MV_SHIFT_MOVER
         * = 15). Storing it made the probe-side `(m & 0x7FFF) == tt_move`
         * never match for odd mover PTs (pawn/bishop/queen) -- TT-move
         * ordering was silently dead for those movers. */
        tte->key = key; tte->value = sv; tte->move = best_move & 0x7FFF;
        tte->depth = (int16_t)depth; tte->flag = (int16_t)flag;
    }
    return best;
}

/* Exported: fixed-depth alpha-beta. Returns the best root move (low 16 bits);
 * node count via *out_nodes, root score via *out_score (for verification). */
uint32_t search_bench(uint64_t pawns, uint64_t knights, uint64_t bishops,
                      uint64_t rooks, uint64_t queens, uint64_t kings,
                      uint64_t occ_w, uint64_t occ_b,
                      int turn, int ep, uint64_t castling,
                      int depth, uint64_t* out_nodes, int* out_score)
{
    Board b = make_board(pawns, knights, bishops, rooks, queens, kings,
                         occ_w, occ_b, turn, ep, castling);
    g_nodes = 0;
    memset(g_history, 0, sizeof(g_history));
    memset(g_killers, 0, sizeof(g_killers));
    memset(g_counter, 0, sizeof(g_counter));
    if (g_tt == NULL) g_tt = (TTEntry*)calloc(TT_SIZE, sizeof(TTEntry));
    memset(g_tt, 0, TT_SIZE * sizeof(TTEntry));   /* fresh TT per search */
    if (!g_lmr_ready) init_lmr();
    uint32_t moves[256];
    int n = gen_legal(&b, moves);
    order_moves(&b, moves, n, 0, 0, 0);
    /* Root: full-width PVS, no reductions/pruning (root moves are few and
     * important). First move full window; rest scout + re-search. */
    int best = -CS_INF, alpha = -CS_INF, beta = CS_INF;
    uint32_t best_move = n ? moves[0] : 0;
    for (int i = 0; i < n; i++) {
        uint32_t m = moves[i];
        Board c = b;
        apply_move(&c, m);
        int gc = in_check(&c);
        uint32_t cp = (uint32_t)((m & 63) << 6 | ((m >> 6) & 63));
        int v;
        if (i == 0) {
            v = -negamax(&c, depth - 1, -beta, -alpha, 1, cp, gc);
        } else {
            v = -negamax(&c, depth - 1, -alpha - 1, -alpha, 1, cp, gc);
            if (v > alpha)
                v = -negamax(&c, depth - 1, -beta, -alpha, 1, cp, gc);
        }
        if (v > best) { best = v; best_move = m; }
        if (v > alpha) alpha = v;
    }
    *out_nodes = g_nodes;
    *out_score = best;
    return best_move & 0x7FFF;   /* 15-bit move key: from|to<<6|promo<<12 */
}

int csearch_abi(void) { return 2; }
