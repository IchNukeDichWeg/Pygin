/*
 * movegen.c -- C legal move generator for ClaudeChess (#9).
 *
 * Replaces list(board.legal_moves) in the hot path. python-chess is kept for
 * push/pop, board state and game logic; ONLY move listing is replaced.
 *
 * The board's bitboards (already plain Python ints) are passed in; the C side
 * generates fully-legal moves and returns them packed as uint32:
 *     from | (to << 6) | (promo << 12)
 * where promo is the piece type (2=N,3=B,4=R,5=Q) or 0 for non-promotions --
 * exactly chess.Move(from, to, promo or None).
 *
 * ORDER: gen_legal() emits moves in python-chess's exact
 * generate_pseudo_legal_moves order (minus illegal), so after the engine's
 * stable sort the equal-score tie-break is identical to board.legal_moves.
 * That natural order matters for LMR/LMP (a prior reordering cost ~20 Elo),
 * so the swap must be byte-identical, not merely set-equal.
 *
 * IN CHECK: python-chess uses a different order (_generate_evasions). Rather
 * than replicate that brittle path, the exported generate_legal() returns -1
 * when the side to move is in check, and the Python glue falls back to
 * board.legal_moves for those (minority) nodes. perft() does NOT use that
 * guard -- it generates fully in every node (set-correct regardless of order).
 *
 * Colour/turn convention matches python-chess: WHITE=1, BLACK=0.
 *
 * Build:  python3 movegen_build.py   (clang -O2 -shared -fPIC)
 */

#include <stdint.h>

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
static uint64_t KNIGHT_ATT[64];
static uint64_t KING_ATT[64];
static uint64_t PAWN_ATT[2][64];   /* [WHITE]/[BLACK] */
static int tables_ready = 0;

static void init_tables(void)
{
    if (tables_ready) return;
    for (int sq = 0; sq < 64; sq++) {
        uint64_t b = 1ULL << sq;
        KNIGHT_ATT[sq] =
              ((b << 17) & ~FILE_A)
            | ((b << 15) & ~FILE_H)
            | ((b << 10) & ~(FILE_A | (FILE_A << 1)))
            | ((b <<  6) & ~(FILE_H | (FILE_H >> 1)))
            | ((b >> 17) & ~FILE_H)
            | ((b >> 15) & ~FILE_A)
            | ((b >> 10) & ~(FILE_H | (FILE_H >> 1)))
            | ((b >>  6) & ~(FILE_A | (FILE_A << 1)));
        KING_ATT[sq] =
              (b << 8) | (b >> 8)
            | ((b << 1) & ~FILE_A) | ((b >> 1) & ~FILE_H)
            | ((b << 9) & ~FILE_A) | ((b >> 9) & ~FILE_H)
            | ((b << 7) & ~FILE_H) | ((b >> 7) & ~FILE_A);
        PAWN_ATT[WHITE][sq] = ((b << 9) & ~FILE_A) | ((b << 7) & ~FILE_H);
        PAWN_ATT[BLACK][sq] = ((b >> 7) & ~FILE_A) | ((b >> 9) & ~FILE_H);
    }
    tables_ready = 1;
}

/* ---------- iterative slider attacks (Dumb7Fill, blocker included) -------- */
static uint64_t rook_attacks(int sq, uint64_t occ)
{
    uint64_t att = 0; int t;
    for (t = sq + 8; t < 64;               t += 8) { att |= 1ULL << t; if ((occ >> t) & 1) break; }
    for (t = sq - 8; t >= 0;               t -= 8) { att |= 1ULL << t; if ((occ >> t) & 1) break; }
    for (t = sq + 1; t < 64 && (t & 7);    t += 1) { att |= 1ULL << t; if ((occ >> t) & 1) break; }
    for (t = sq - 1; t >= 0 && (t & 7)!=7; t -= 1) { att |= 1ULL << t; if ((occ >> t) & 1) break; }
    return att;
}

static uint64_t bishop_attacks(int sq, uint64_t occ)
{
    uint64_t att = 0; int t;
    for (t = sq + 9; t < 64 && (t & 7)!=0; t += 9) { att |= 1ULL << t; if ((occ >> t) & 1) break; }
    for (t = sq + 7; t < 64 && (t & 7)!=7; t += 7) { att |= 1ULL << t; if ((occ >> t) & 1) break; }
    for (t = sq - 7; t >= 0 && (t & 7)!=0; t -= 7) { att |= 1ULL << t; if ((occ >> t) & 1) break; }
    for (t = sq - 9; t >= 0 && (t & 7)!=7; t -= 9) { att |= 1ULL << t; if ((occ >> t) & 1) break; }
    return att;
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

/* Is `sq` attacked by `them`, given occupancy `occ`?  `us` = colour of the
 * piece on sq (for the pawn-attack table lookup).  p/n/bq/rq/k are THEM's
 * pawns / knights / bishops+queens / rooks+queens / king bitboards. */
static int attacked(int sq, uint64_t occ, int us,
                    uint64_t p, uint64_t n, uint64_t bq, uint64_t rq, uint64_t k)
{
    if (KNIGHT_ATT[sq] & n)            return 1;
    if (KING_ATT[sq]   & k)            return 1;
    if (PAWN_ATT[us][sq] & p)          return 1;
    if (bishop_attacks(sq, occ) & bq)  return 1;
    if (rook_attacks(sq, occ)   & rq)  return 1;
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
    int ksq = (fb & b->kings) ? to : __builtin_ctzll(b->kings & b->occ[us]);
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
    int ksq = __builtin_ctzll(b->kings & b->occ[us]);
    return sq_attacked_by_them(b, ksq);
}

/* ---------- legal move generation (python-chess pseudo-legal order) ------- */
/* Emits in exactly generate_pseudo_legal_moves order, minus illegal moves.
 * Correct SET in every position; correct ORDER when not in check. */
static int gen_legal(const Board* b, uint32_t* out)
{
    init_tables();
    int us = b->turn, them = us ^ 1, cnt = 0;
    uint64_t own = b->occ[us], enemy = b->occ[them], occ = own | enemy;
    uint64_t empty = ~occ;
    uint64_t t, a;
    int from, to;

    /* 1. non-pawn piece moves: all of (N|B|R|Q|K), descending from-square;
     *    for each, targets descending. (King's normal moves included here.) */
    uint64_t nonpawns = (b->knights | b->bishops | b->rooks | b->queens | b->kings) & own;
    for (t = nonpawns; t; t &= ~(1ULL << from)) {
        from = 63 - __builtin_clzll(t);
        uint64_t fb = 1ULL << from, att;
        if      (b->knights & fb) att = KNIGHT_ATT[from];
        else if (b->kings   & fb) att = KING_ATT[from];
        else if (b->bishops & fb) att = bishop_attacks(from, occ);
        else if (b->rooks   & fb) att = rook_attacks(from, occ);
        else                      att = rook_attacks(from, occ) | bishop_attacks(from, occ);
        for (a = att & ~own; a; a &= ~(1ULL << to)) {
            to = 63 - __builtin_clzll(a);
            if (legal(b, from, to, 0)) out[cnt++] = from | (to << 6);
        }
    }

    /* 2. castling: descending rook-square => king side before queen side. */
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
                out[cnt++] = e | (g << 6);
        }
        if (b->castling & (1ULL << qs_rook)) {
            int d = e - 1, c = e - 2, n2 = e - 3;
            if (!(occ & ((1ULL << d) | (1ULL << c) | (1ULL << n2)))
                && !sq_attacked_by_them(b, e)
                && !sq_attacked_by_them(b, d)
                && !sq_attacked_by_them(b, c))
                out[cnt++] = e | (c << 6);
        }
    }

    uint64_t pawns = b->pawns & own;

    /* 3. pawn captures (+ capture-promotions Q,R,B,N), descending from/to. */
    for (t = pawns; t; t &= ~(1ULL << from)) {
        from = 63 - __builtin_clzll(t);
        for (a = PAWN_ATT[us][from] & enemy; a; a &= ~(1ULL << to)) {
            to = 63 - __builtin_clzll(a);
            int promo = (us == WHITE) ? (to >= 56) : (to < 8);
            if (promo) {
                if (legal(b, from, to, 0)) {
                    out[cnt++] = from | (to << 6) | (5 << 12);
                    out[cnt++] = from | (to << 6) | (4 << 12);
                    out[cnt++] = from | (to << 6) | (3 << 12);
                    out[cnt++] = from | (to << 6) | (2 << 12);
                }
            } else if (legal(b, from, to, 0)) {
                out[cnt++] = from | (to << 6);
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
                out[cnt++] = from | (to << 6) | (5 << 12);
                out[cnt++] = from | (to << 6) | (4 << 12);
                out[cnt++] = from | (to << 6) | (3 << 12);
                out[cnt++] = from | (to << 6) | (2 << 12);
            }
        } else if (legal(b, from, to, 0)) {
            out[cnt++] = from | (to << 6);
        }
    }

    /* 5. double pawn pushes, descending to-square. */
    uint64_t dbl = (us == WHITE) ? ((single << 8) & empty & RANK_4)
                                 : ((single >> 8) & empty & RANK_5);
    for (a = dbl; a; a &= ~(1ULL << to)) {
        to = 63 - __builtin_clzll(a);
        from = (us == WHITE) ? to - 16 : to + 16;
        if (legal(b, from, to, 0)) out[cnt++] = from | (to << 6);
    }

    /* 6. en passant, descending capturer-square. */
    if (b->ep >= 0 && !((1ULL << b->ep) & occ)) {
        for (t = pawns & PAWN_ATT[them][b->ep]; t; t &= ~(1ULL << from)) {
            from = 63 - __builtin_clzll(t);
            if (legal(b, from, b->ep, 1)) out[cnt++] = from | (b->ep << 6);
        }
    }
    return cnt;
}

/* ---------- exported: generate_legal ------------------------------------- */
/* Returns move count, or -1 if the side to move is in check (caller should
 * fall back to python-chess to preserve the evasion move order). */
int generate_legal(uint64_t pawns, uint64_t knights, uint64_t bishops,
                   uint64_t rooks, uint64_t queens, uint64_t kings,
                   uint64_t occ_w, uint64_t occ_b,
                   int turn, int ep, uint64_t castling,
                   uint32_t* out)
{
    Board b = make_board(pawns, knights, bishops, rooks, queens, kings,
                         occ_w, occ_b, turn, ep, castling);
    if (in_check(&b)) return -1;
    return gen_legal(&b, out);
}

/* ---------- exported: generate_captures (quiescence) --------------------- */
/* Captures + promotions only, in the exact order _capture_moves builds them:
 *   1. non-pawn captures (incl. king), descending from/to
 *   2. pawn captures + capture-promotions (Q,R,B,N), descending from/to
 *   3. en passant, descending capturer
 *   4. non-capturing promotion pushes (Q,R,B,N), descending to
 * Only ever called when NOT in check (quiescence delegates check evasions to
 * order_moves), so no evasion-order handling is needed. */
int generate_captures(uint64_t pawns, uint64_t knights, uint64_t bishops,
                      uint64_t rooks, uint64_t queens, uint64_t kings,
                      uint64_t occ_w, uint64_t occ_b,
                      int turn, int ep, uint64_t castling,
                      uint32_t* out)
{
    Board bb = make_board(pawns, knights, bishops, rooks, queens, kings,
                          occ_w, occ_b, turn, ep, castling);
    const Board* b = &bb;
    init_tables();
    int us = b->turn, them = us ^ 1, cnt = 0;
    uint64_t own = b->occ[us], enemy = b->occ[them], occ = own | enemy, empty = ~occ;
    uint64_t t, a;
    int from, to;

    /* 1. non-pawn captures (incl. king), descending from/to */
    uint64_t nonpawns = (b->knights | b->bishops | b->rooks | b->queens | b->kings) & own;
    for (t = nonpawns; t; t &= ~(1ULL << from)) {
        from = 63 - __builtin_clzll(t);
        uint64_t fb = 1ULL << from, att;
        if      (b->knights & fb) att = KNIGHT_ATT[from];
        else if (b->kings   & fb) att = KING_ATT[from];
        else if (b->bishops & fb) att = bishop_attacks(from, occ);
        else if (b->rooks   & fb) att = rook_attacks(from, occ);
        else                      att = rook_attacks(from, occ) | bishop_attacks(from, occ);
        for (a = att & enemy; a; a &= ~(1ULL << to)) {
            to = 63 - __builtin_clzll(a);
            if (legal(b, from, to, 0)) out[cnt++] = from | (to << 6);
        }
    }

    uint64_t pawns_own = b->pawns & own;

    /* 2. pawn captures + capture-promotions (Q,R,B,N), descending from/to */
    for (t = pawns_own; t; t &= ~(1ULL << from)) {
        from = 63 - __builtin_clzll(t);
        for (a = PAWN_ATT[us][from] & enemy; a; a &= ~(1ULL << to)) {
            to = 63 - __builtin_clzll(a);
            int promo = (us == WHITE) ? (to >= 56) : (to < 8);
            if (promo) {
                if (legal(b, from, to, 0)) {
                    out[cnt++] = from | (to << 6) | (5 << 12);
                    out[cnt++] = from | (to << 6) | (4 << 12);
                    out[cnt++] = from | (to << 6) | (3 << 12);
                    out[cnt++] = from | (to << 6) | (2 << 12);
                }
            } else if (legal(b, from, to, 0)) {
                out[cnt++] = from | (to << 6);
            }
        }
    }

    /* 3. en passant, descending capturer */
    if (b->ep >= 0 && !((1ULL << b->ep) & occ)) {
        for (t = pawns_own & PAWN_ATT[them][b->ep]; t; t &= ~(1ULL << from)) {
            from = 63 - __builtin_clzll(t);
            if (legal(b, from, b->ep, 1)) out[cnt++] = from | (b->ep << 6);
        }
    }

    /* 4. non-capturing promotion pushes (Q,R,B,N), descending to */
    uint64_t single = (us == WHITE) ? ((pawns_own << 8) & empty) : ((pawns_own >> 8) & empty);
    uint64_t promo_push = single & ((us == WHITE) ? RANK_8 : RANK_1);
    for (a = promo_push; a; a &= ~(1ULL << to)) {
        to = 63 - __builtin_clzll(a);
        from = (us == WHITE) ? to - 8 : to + 8;
        if (legal(b, from, to, 0)) {
            out[cnt++] = from | (to << 6) | (5 << 12);
            out[cnt++] = from | (to << 6) | (4 << 12);
            out[cnt++] = from | (to << 6) | (3 << 12);
            out[cnt++] = from | (to << 6) | (2 << 12);
        }
    }
    return cnt;
}

/* ---------- copy-make (perft self-test only) ----------------------------- */
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

static uint64_t perft_rec(Board* b, int depth)
{
    uint32_t moves[256];
    int n = gen_legal(b, moves);
    if (depth <= 1) return (uint64_t)n;
    uint64_t nodes = 0;
    for (int i = 0; i < n; i++) {
        Board c = *b;
        apply_move(&c, moves[i]);
        nodes += perft_rec(&c, depth - 1);
    }
    return nodes;
}

/* ---------- exported: perft ---------------------------------------------- */
uint64_t perft(uint64_t pawns, uint64_t knights, uint64_t bishops,
               uint64_t rooks, uint64_t queens, uint64_t kings,
               uint64_t occ_w, uint64_t occ_b,
               int turn, int ep, uint64_t castling, int depth)
{
    Board b = make_board(pawns, knights, bishops, rooks, queens, kings,
                         occ_w, occ_b, turn, ep, castling);
    if (depth <= 0) return 1;
    return perft_rec(&b, depth);
}
