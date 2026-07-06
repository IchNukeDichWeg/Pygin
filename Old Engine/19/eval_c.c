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
#include "Constants.h"   /* #2.1/#2.2: magic tables + INBETWEEN_BITBOARDS */

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

/* ---------- slider attacks: magic bitboards (#2.2) ------------------------ *
 * Same signature as the previous Dumb7Fill versions, so every call site in
 * mobility_king_safety just gets faster -- byte-identical attack sets,
 * verified against the iterative version on random occupancies before this
 * swap landed. Tables (ROOK_*, BISHOP_*) live in Constants.c.
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

/* ====================================================================== *
 * #2.5: positional_extras = bishop_pair + rook_files + mopup
 *
 * Folds three small Python helpers into one C call. Same semantics as
 * _bishop_pair_bb + _rook_files_bb + _mopup_bb in engine.py: returns the
 * combined delta from White's perspective in centipawns.
 *
 * `strong != 0` mirrors the lone-loser branch in _eval_positional_white:
 * skip bishop_pair / rook_files, run mopup with the heavier weights so a
 * winning K + Q (+ B/P) vs K never shuffles into a draw.
 *
 * Constants are kept in sync with the Engine tuner via set_positional_params,
 * called once from Engine.__init__ (just like set_mobility_params already
 * does). CENTER_MANHATTAN is a pure function of (file, rank) so it lives
 * statically here; the table is identical to engine.py's _center_manhattan.
 * ====================================================================== */
static int POS_ROOK_OPEN = 22, POS_ROOK_SEMI = 11;
static int POS_BP_MG = 30, POS_BP_EG = 50;
static int POS_MOPUP_MIN = 500;
static int POS_MOPUP_CMD = 8, POS_MOPUP_KING = 10;
static int POS_MOPUP_STRONG_CMD = 24, POS_MOPUP_STRONG_KING = 18;

static const int CENTER_EDGE[8] = {3, 2, 1, 0, 0, 1, 2, 3};
#define CENTER_MANHATTAN(sq) (CENTER_EDGE[(sq) & 7] + CENTER_EDGE[(sq) >> 3])

void set_positional_params(int rook_open, int rook_semi,
                           int bp_mg, int bp_eg,
                           int mopup_min,
                           int mopup_cmd, int mopup_king,
                           int mopup_str_cmd, int mopup_str_king)
{
    POS_ROOK_OPEN = rook_open; POS_ROOK_SEMI = rook_semi;
    POS_BP_MG = bp_mg; POS_BP_EG = bp_eg;
    POS_MOPUP_MIN = mopup_min;
    POS_MOPUP_CMD = mopup_cmd; POS_MOPUP_KING = mopup_king;
    POS_MOPUP_STRONG_CMD = mopup_str_cmd; POS_MOPUP_STRONG_KING = mopup_str_king;
}

int positional_extras(uint64_t knights, uint64_t bishops,
                      uint64_t rooks, uint64_t queens,
                      uint64_t occ_w, uint64_t occ_b,
                      uint64_t wp, uint64_t bp,
                      int wksq, int bksq,
                      int phase, int strong, int include_mopup)
{
    /* Lone-loser branch: only mopup with the heavy weights, sign by adv. */
    if (strong) {
        int npm_w = 320 * __builtin_popcountll(knights & occ_w)
                  + 330 * __builtin_popcountll(bishops & occ_w)
                  + 500 * __builtin_popcountll(rooks   & occ_w)
                  + 900 * __builtin_popcountll(queens  & occ_w);
        int npm_b = 320 * __builtin_popcountll(knights & occ_b)
                  + 330 * __builtin_popcountll(bishops & occ_b)
                  + 500 * __builtin_popcountll(rooks   & occ_b)
                  + 900 * __builtin_popcountll(queens  & occ_b);
        int adv = npm_w - npm_b;
        int aadv = adv < 0 ? -adv : adv;
        if (aadv < POS_MOPUP_MIN || wksq < 0 || bksq < 0) return 0;
        int loser = (adv > 0) ? bksq : wksq;
        int dfile = (wksq & 7) - (bksq & 7); if (dfile < 0) dfile = -dfile;
        int drank = (wksq >> 3) - (bksq >> 3); if (drank < 0) drank = -drank;
        int md = dfile + drank;
        int bonus = POS_MOPUP_STRONG_CMD * CENTER_MANHATTAN(loser)
                  + POS_MOPUP_STRONG_KING * (14 - md);
        return (adv > 0) ? bonus : -bonus;
    }

    int score = 0;

    /* rook_files: (semi-)open file bonus per rook. */
    uint64_t rw = rooks & occ_w;
    while (rw) {
        int sq = __builtin_ctzll(rw);
        rw &= rw - 1;
        uint64_t fmask = 0x0101010101010101ULL << (sq & 7);
        if (!(wp & fmask))
            score += (bp & fmask) ? POS_ROOK_SEMI : POS_ROOK_OPEN;
    }
    uint64_t rb = rooks & occ_b;
    while (rb) {
        int sq = __builtin_ctzll(rb);
        rb &= rb - 1;
        uint64_t fmask = 0x0101010101010101ULL << (sq & 7);
        if (!(bp & fmask))
            score -= (wp & fmask) ? POS_ROOK_SEMI : POS_ROOK_OPEN;
    }

    /* bishop_pair: phased blend, applied for either side at >= 2 bishops. */
    if (PHASE_MAX > 0) {
        int bpv = (POS_BP_MG * phase + POS_BP_EG * (PHASE_MAX - phase)) / PHASE_MAX;
        if (__builtin_popcountll(bishops & occ_w) >= 2) score += bpv;
        if (__builtin_popcountll(bishops & occ_b) >= 2) score -= bpv;
    }

    /* mopup: drive the losing side's king toward an edge when one side
     * has a decisive non-pawn material edge. Gated by include_mopup so
     * the midgame branch (high phase) can call positional_extras for the
     * bishop_pair + rook_files terms ALONE -- matching engine.py exactly. */
    if (include_mopup && wksq >= 0 && bksq >= 0) {
        int npm_w = 320 * __builtin_popcountll(knights & occ_w)
                  + 330 * __builtin_popcountll(bishops & occ_w)
                  + 500 * __builtin_popcountll(rooks   & occ_w)
                  + 900 * __builtin_popcountll(queens  & occ_w);
        int npm_b = 320 * __builtin_popcountll(knights & occ_b)
                  + 330 * __builtin_popcountll(bishops & occ_b)
                  + 500 * __builtin_popcountll(rooks   & occ_b)
                  + 900 * __builtin_popcountll(queens  & occ_b);
        int adv = npm_w - npm_b;
        int aadv = adv < 0 ? -adv : adv;
        if (aadv >= POS_MOPUP_MIN) {
            int loser = (adv > 0) ? bksq : wksq;
            int dfile = (wksq & 7) - (bksq & 7); if (dfile < 0) dfile = -dfile;
            int drank = (wksq >> 3) - (bksq >> 3); if (drank < 0) drank = -drank;
            int md = dfile + drank;
            int bonus = POS_MOPUP_CMD * CENTER_MANHATTAN(loser)
                      + POS_MOPUP_KING * (14 - md);
            score += (adv > 0) ? bonus : -bonus;
        }
    }

    return score;
}