/*
 * eval_c.c -- C implementation of _mobility_king_safety_bb.
 *
 * Compiled to eval_c.so and called from engine.py via ctypes.  If the .so
 * is absent or fails to load, engine.py falls back to the Python version
 * automatically -- behaviour is identical, only speed differs.
 *
 * Build (from the project directory):
 *     python3 eval_build.py
 *
 * Constants mirror the Engine class attributes in engine.py; call
 * set_mobility_params() once from Engine.__init__() to keep them in sync.
 */

#include <stdint.h>

/* ---------- tuning constants (overridden by set_mobility_params) ---------- */
static int MOB_N = 4, MOB_B = 3, MOB_R = 2, MOB_Q = 1;
static int PHASE_MAX = 24;
static int SHIELD_MG = 5,  SHIELD_EG = 2;
static int RING_MG   = 13, RING_EG   = 0;
static int OPEN_MG   = 28, OPEN_EG   = 2;

void set_mobility_params(int mob_n, int mob_b, int mob_r, int mob_q,
                         int phase_max,
                         int shield_mg, int shield_eg,
                         int ring_mg,   int ring_eg,
                         int open_mg,   int open_eg)
{
    MOB_N = mob_n; MOB_B = mob_b; MOB_R = mob_r; MOB_Q = mob_q;
    PHASE_MAX = phase_max;
    SHIELD_MG = shield_mg; SHIELD_EG = shield_eg;
    RING_MG   = ring_mg;   RING_EG   = ring_eg;
    OPEN_MG   = open_mg;   OPEN_EG   = open_eg;
}

/* ---------- file masks (same layout as python-chess: a1=bit0, h8=bit63) -- */
static const uint64_t FILE_A  = 0x0101010101010101ULL;
static const uint64_t FILE_H  = 0x8080808080808080ULL;
static const uint64_t FILE_BB[8] = {
    0x0101010101010101ULL,   /* A */
    0x0202020202020202ULL,   /* B */
    0x0404040404040404ULL,   /* C */
    0x0808080808080808ULL,   /* D */
    0x1010101010101010ULL,   /* E */
    0x2020202020202020ULL,   /* F */
    0x4040404040404040ULL,   /* G */
    0x8080808080808080ULL,   /* H */
};

/* ---------- precomputed attack tables ------------------------------------ */
static uint64_t KNIGHT_ATT[64];
static uint64_t KING_ATT[64];
static uint64_t PAWN_ATT_W[64];   /* white pawn attacks from sq */
static uint64_t PAWN_ATT_B[64];   /* black pawn attacks from sq */
static int      tables_ready = 0;

static void init_tables(void)
{
    int sq;
    if (tables_ready) return;
    for (sq = 0; sq < 64; sq++) {
        uint64_t b = (uint64_t)1 << sq;

        /* knight: 8 target squares, masked to avoid file wrap */
        KNIGHT_ATT[sq] =
              ((b << 17) & ~FILE_A)
            | ((b << 15) & ~FILE_H)
            | ((b << 10) & ~(FILE_A | (FILE_A << 1)))
            | ((b <<  6) & ~(FILE_H | (FILE_H >> 1)))
            | ((b >> 17) & ~FILE_H)
            | ((b >> 15) & ~FILE_A)
            | ((b >> 10) & ~(FILE_H | (FILE_H >> 1)))
            | ((b >>  6) & ~(FILE_A | (FILE_A << 1)));

        /* king: 8 adjacent squares */
        KING_ATT[sq] =
              (b << 8)
            | (b >> 8)
            | ((b << 1) & ~FILE_A)
            | ((b >> 1) & ~FILE_H)
            | ((b << 9) & ~FILE_A)
            | ((b >> 9) & ~FILE_H)
            | ((b << 7) & ~FILE_H)
            | ((b >> 7) & ~FILE_A);

        /* pawn attacks: white goes +9 (NE) and +7 (NW) */
        PAWN_ATT_W[sq] = ((b << 9) & ~FILE_A) | ((b << 7) & ~FILE_H);
        /* black goes -7 (SE) and -9 (SW) */
        PAWN_ATT_B[sq] = ((b >> 7) & ~FILE_A) | ((b >> 9) & ~FILE_H);
    }
    tables_ready = 1;
}

/* ---------- slider attack generation (iterative, O(max 7) per direction) - */
static uint64_t rook_attacks(int sq, uint64_t occ)
{
    uint64_t att = 0;
    int t;
    for (t = sq + 8; t < 64;       t += 8) { att |= (uint64_t)1 << t; if ((occ >> t) & 1) break; }
    for (t = sq - 8; t >= 0;       t -= 8) { att |= (uint64_t)1 << t; if ((occ >> t) & 1) break; }
    for (t = sq + 1; t < 64 && (t & 7) != 0; t++)  { att |= (uint64_t)1 << t; if ((occ >> t) & 1) break; }
    for (t = sq - 1; t >= 0 && (t & 7) != 7; t--)  { att |= (uint64_t)1 << t; if ((occ >> t) & 1) break; }
    return att;
}

static uint64_t bishop_attacks(int sq, uint64_t occ)
{
    uint64_t att = 0;
    int t;
    for (t = sq + 9; t < 64 && (t & 7) != 0; t += 9) { att |= (uint64_t)1 << t; if ((occ >> t) & 1) break; }
    for (t = sq + 7; t < 64 && (t & 7) != 7; t += 7) { att |= (uint64_t)1 << t; if ((occ >> t) & 1) break; }
    for (t = sq - 7; t >= 0 && (t & 7) != 0; t -= 7) { att |= (uint64_t)1 << t; if ((occ >> t) & 1) break; }
    for (t = sq - 9; t >= 0 && (t & 7) != 7; t -= 9) { att |= (uint64_t)1 << t; if ((occ >> t) & 1) break; }
    return att;
}

/* ---------- main function ------------------------------------------------ */
/*
 * Mirrors _mobility_king_safety_bb in engine.py exactly.
 * wksq / bksq: king square index 0-63, or -1 if the king is not on the board.
 * Returns the score from White's perspective (positive = White better).
 */
int mobility_king_safety(
    uint64_t occ_w, uint64_t occ_b,
    uint64_t knights, uint64_t bishops, uint64_t rooks, uint64_t queens,
    uint64_t wp, uint64_t bp,
    int wksq, int bksq,
    int phase)
{
    init_tables();  /* must run before KING_ATT is read below */

    uint64_t occ   = occ_w | occ_b;
    uint64_t wring = (wksq >= 0) ? KING_ATT[wksq] : 0ULL;
    uint64_t bring = (bksq >= 0) ? KING_ATT[bksq] : 0ULL;
    int score      = 0;
    int w_ring_att = 0;
    int b_ring_att = 0;
    uint64_t t;
    int sq;

    /* --- knights --- */
    for (t = knights & occ_w; t; t &= t-1) {
        sq = __builtin_ctzll(t);
        uint64_t a = KNIGHT_ATT[sq];
        score      += MOB_N * __builtin_popcountll(a & ~occ_w);
        b_ring_att += __builtin_popcountll(a & bring);
    }
    for (t = knights & occ_b; t; t &= t-1) {
        sq = __builtin_ctzll(t);
        uint64_t a = KNIGHT_ATT[sq];
        score      -= MOB_N * __builtin_popcountll(a & ~occ_b);
        w_ring_att += __builtin_popcountll(a & wring);
    }

    /* --- bishops --- */
    for (t = bishops & occ_w; t; t &= t-1) {
        sq = __builtin_ctzll(t);
        uint64_t a = bishop_attacks(sq, occ);
        score      += MOB_B * __builtin_popcountll(a & ~occ_w);
        b_ring_att += __builtin_popcountll(a & bring);
    }
    for (t = bishops & occ_b; t; t &= t-1) {
        sq = __builtin_ctzll(t);
        uint64_t a = bishop_attacks(sq, occ);
        score      -= MOB_B * __builtin_popcountll(a & ~occ_b);
        w_ring_att += __builtin_popcountll(a & wring);
    }

    /* --- rooks --- */
    for (t = rooks & occ_w; t; t &= t-1) {
        sq = __builtin_ctzll(t);
        uint64_t a = rook_attacks(sq, occ);
        score      += MOB_R * __builtin_popcountll(a & ~occ_w);
        b_ring_att += __builtin_popcountll(a & bring);
    }
    for (t = rooks & occ_b; t; t &= t-1) {
        sq = __builtin_ctzll(t);
        uint64_t a = rook_attacks(sq, occ);
        score      -= MOB_R * __builtin_popcountll(a & ~occ_b);
        w_ring_att += __builtin_popcountll(a & wring);
    }

    /* --- queens --- */
    for (t = queens & occ_w; t; t &= t-1) {
        sq = __builtin_ctzll(t);
        uint64_t a = rook_attacks(sq, occ) | bishop_attacks(sq, occ);
        score      += MOB_Q * __builtin_popcountll(a & ~occ_w);
        b_ring_att += __builtin_popcountll(a & bring);
    }
    for (t = queens & occ_b; t; t &= t-1) {
        sq = __builtin_ctzll(t);
        uint64_t a = rook_attacks(sq, occ) | bishop_attacks(sq, occ);
        score      -= MOB_Q * __builtin_popcountll(a & ~occ_b);
        w_ring_att += __builtin_popcountll(a & wring);
    }

    /* --- pawn and enemy-king attacks on king rings --- */
    if (wring) {
        for (t = bp; t; t &= t-1) {
            sq = __builtin_ctzll(t);
            w_ring_att += __builtin_popcountll(PAWN_ATT_B[sq] & wring);
        }
        if (bksq >= 0)
            w_ring_att += __builtin_popcountll(KING_ATT[bksq] & wring);
    }
    if (bring) {
        for (t = wp; t; t &= t-1) {
            sq = __builtin_ctzll(t);
            b_ring_att += __builtin_popcountll(PAWN_ATT_W[sq] & bring);
        }
        if (wksq >= 0)
            b_ring_att += __builtin_popcountll(KING_ATT[wksq] & bring);
    }

    /* --- king-safety terms (tapered) ------------------------------------ */
    {
        int pm         = PHASE_MAX;
        int shield_val = (SHIELD_MG * phase + SHIELD_EG * (pm - phase)) / pm;
        int ring_val   = (RING_MG   * phase + RING_EG   * (pm - phase)) / pm;
        int open_val   = (OPEN_MG   * phase + OPEN_EG   * (pm - phase)) / pm;

        if (wksq >= 0) {
            score += __builtin_popcountll(wring & occ_w) * shield_val;
            score -= w_ring_att * ring_val;
            if (!(wp & FILE_BB[wksq & 7])) score -= open_val;
        }
        if (bksq >= 0) {
            score -= __builtin_popcountll(bring & occ_b) * shield_val;
            score += b_ring_att * ring_val;
            if (!(bp & FILE_BB[bksq & 7])) score += open_val;
        }
    }

    return score;
}
