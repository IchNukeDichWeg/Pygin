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
/* FI-86: the four mobility weights are TAPERED (MG/EG twins blended on the
 * game phase, exactly like SHIELD/RING/OPEN below). Both halves equal is
 * arithmetically identical to the old flat weight -- (v*p + v*(pm-p))/pm ==
 * v exactly in integers -- which is the byte-identity argument for the
 * first build. MOB_* below are the blended values, recomputed once per
 * mobility_king_safety call rather than per piece. */
static int MOB_N_MG = 4, MOB_B_MG = 3, MOB_R_MG = 2, MOB_Q_MG = 1;
static int MOB_N_EG = 4, MOB_B_EG = 3, MOB_R_EG = 2, MOB_Q_EG = 1;
int PHASE_MAX = 24;    /* FB-06: non-static -- csearch.c's taper reads it */
static int SHIELD_MG = 5,  SHIELD_EG = 2;
static int RING_MG   = 13, RING_EG   = 0;
static int OPEN_MG   = 28, OPEN_EG   = 2;

/* #2.5: rook_files + bishop_pair + mopup constants (overridden by
 * set_positional_params). Read by mobility_king_safety's #2.5b inlined
 * rook_files + bishop_pair pass and its folded low-phase mop-up. */
static int POS_ROOK_OPEN = 22, POS_ROOK_SEMI = 11;
static int POS_BP_MG = 30, POS_BP_EG = 50;
static int POS_MOPUP_MIN = 500;
static int POS_MOPUP_CMD = 8, POS_MOPUP_KING = 10;
/* FI-27: POS_MOPUP_STRONG_* deleted -- they were write-only (the live
 * strong mop-up lives in csearch.c's g_mopup_scmd/sking); the stale 24/18
 * initializers were a trap for a future "wire up symmetry" refactor. The
 * setter keeps its arity for ABI stability and ignores the two args. */

/* #3.x: rook on 7th rank tuning (overridden by set_rook_on_7th_params).
 * Phased blend; 0/0 disables. Bonus applies per rook on the side's 7th
 * (rank index 6 for white, 1 for black) when the enemy king is on its
 * back rank OR an enemy pawn still sits on its 7th -- both classic
 * conditions for the rook-on-7th being more than cosmetic. */
static int R7_MG = 18, R7_EG = 32;

/* #3.x: mobility-area toggle. When non-zero, every piece's mobility
 * count subtracts squares attacked by an enemy pawn (those squares
 * aren't really mobile -- a knight stepping there is just lost). 0 =
 * legacy behaviour (mobility counts every empty / enemy square the
 * piece sees). Mobility weights are NOT retuned here so the absolute
 * eval shrinks slightly; tuning the weights to compensate is a
 * follow-up the engine_feature_workflow can A/B independently. */
static int MOB_AREA_ON = 1;

/* FI-85: battery-transparent (x-ray) slider mobility. When on, a slider's
 * MOBILITY popcount is computed with own same-ray sliders removed from the
 * occupancy, so a doubled rook or a queen-behind-bishop counts its real
 * influence instead of seeing its own front piece as a wall. Ring-attack and
 * threat sets keep TRUE occupancy (a battery does not attack THROUGH for
 * check purposes). 0 = v54 byte-exact (ma == a, no extra attack calls). */
static int XRAY_MOB = 0;
void set_xray_mob(int on) { XRAY_MOB = on ? 1 : 0; }

/* #3.x: threats. Two coarse classes (cheap, big signal):
 *   THREAT_PAWN   -- per enemy non-pawn piece attacked by one of our pawns
 *   THREAT_MINOR  -- per enemy major piece attacked by one of our minors
 * The minor-side accumulator (w_minor_atk / b_minor_atk) is OR'd inside
 * the existing knight + bishop loops, so the per-call cost is two OR's
 * per piece + a handful of popcounts at the end. 0 disables. */
static int THREAT_PAWN  = 35;
static int THREAT_MINOR = 25;

/* Outpost bonus: knight/bishop on a square supported by a friendly pawn
 * and unreachable by any enemy pawn (no enemy pawn on adjacent files ahead).
 * Tapered MG/EG per piece type; 0/0 disables the whole block. */
static int OUTPOST_ON   = 0;
static int OUTPOST_N_MG = 20, OUTPOST_N_EG = 10;
static int OUTPOST_B_MG = 10, OUTPOST_B_EG =  5;

/* Space bonus: safe central squares (c-f files, ranks 2-4 for white /
 * ranks 5-7 for black) not attacked by an enemy pawn. Tapered by phase
 * so it fades toward zero in the endgame. 0 disables. */
static int SPACE_ON = 0;
static int SPACE_MG = 4;

/* Phalanx / connected-pawn bonus: reward pawns that are either side-by-side
 * on the same rank (phalanx) or defended by a friendly pawn from behind
 * (supported). Both are verifiable within 1-2 moves so the bonus is
 * reliable at shallow depth. Tapered MG/EG; 0 disables. */
static int PHALANX_ON = 0;
static int PHALANX_MG = 10;
static int PHALANX_EG = 5;

/* Pawn storm: bonus for friendly pawns advanced toward the enemy king.
 * Counts pawns on the three files centred on the enemy king's file that
 * have crossed the midline (ranks 5-7 for white, ranks 2-4 for black).
 * Pure middlegame term (EG fades to 0) since pawn advances near the enemy
 * king are only dangerous while pieces are on the board. 0 disables. */
static int STORM_ON = 0;
static int STORM_MG = 12;
static int STORM_EG = 0;

/* King shelter: per-file, per-distance pawn shield assessment.
 * When ON, replaces the flat `popcount(king_ring & own_pieces) * SHIELD_MG`
 * shield with a more accurate per-file check: for each of the 3 files
 * around the king (king_file-1 .. king_file+1), find the closest own pawn
 * strictly in front of the king and award SHELTER_CLOSE if it is 1 rank
 * away, or SHELTER_FAR if it is 2 ranks away. Phased down to 0 in the EG
 * (same as RING_EG = 0) because the king should be active there.
 * 0 disables (keeps the legacy flat-ring shield). */
static int SHELTER_ON    = 0;
static int SHELTER_CLOSE = 8;   /* cp per pawn 1 rank in front of king  */
static int SHELTER_FAR   = 4;   /* cp per pawn 2 ranks in front of king */

static const int CENTER_EDGE[8] = {3, 2, 1, 0, 0, 1, 2, 3};
#define CENTER_MANHATTAN(sq) (CENTER_EDGE[(sq) & 7] + CENTER_EDGE[(sq) >> 3])

void set_mobility_params(int mob_n, int mob_b, int mob_r, int mob_q,
                         int phase_max,
                         int shield_mg, int shield_eg,
                         int ring_mg,   int ring_eg,
                         int open_mg,   int open_eg)
{
    /* Legacy flat entry (abi <= 4): both halves take the same value, which
     * reproduces the pre-FI-86 behaviour exactly. set_mobility_eg overrides
     * the EG half afterwards; engine.py's sync calls both, in that order. */
    MOB_N_MG = MOB_N_EG = mob_n; MOB_B_MG = MOB_B_EG = mob_b;
    MOB_R_MG = MOB_R_EG = mob_r; MOB_Q_MG = MOB_Q_EG = mob_q;
    PHASE_MAX = phase_max;
    SHIELD_MG = shield_mg; SHIELD_EG = shield_eg;
    RING_MG   = ring_mg;   RING_EG   = ring_eg;
    OPEN_MG   = open_mg;   OPEN_EG   = open_eg;
}

/* FI-86: the EG half of the four mobility weights. Separate setter rather
 * than widening set_mobility_params, so a host that never calls it keeps
 * the flat behaviour (EG == MG) instead of silently zeroing the endgame. */
void set_mobility_eg(int mob_n_eg, int mob_b_eg, int mob_r_eg, int mob_q_eg)
{
    MOB_N_EG = mob_n_eg; MOB_B_EG = mob_b_eg;
    MOB_R_EG = mob_r_eg; MOB_Q_EG = mob_q_eg;
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
/* W-15: knight/king tables are bit-identical to Constants.c's
 * KNIGHT_ATTACKS/KING_ATTACKS (verified entry-by-entry), so alias those
 * const tables instead of rebuilding them at load. The PAWN tables stay
 * runtime-built -- Constants' pawn tables use the opposite "attacked-by"
 * convention (see init_tables note). */
#define KNIGHT_ATT KNIGHT_ATTACKS
#define KING_ATT   KING_ATTACKS
static uint64_t PAWN_ATT_W[64];   /* white pawn attacks from sq */
static uint64_t PAWN_ATT_B[64];   /* black pawn attacks from sq */
static int      tables_ready = 0;

/* C-06: runs once at .so load (constructor) so the exported functions no
 * longer pay an init_tables() call + tables_ready branch per invocation.
 * NOTE: Constants.c's WHITE/BLACK_PAWN_ATTACKS use the OPPOSITE convention
 * (squares whose pawns attack sq, not attacks-from-sq -- verified entry by
 * entry), so these runtime-built tables intentionally stay. */
__attribute__((constructor))
static void init_tables(void)
{
    int sq;
    if (tables_ready) return;
    for (sq = 0; sq < 64; sq++) {
        uint64_t b = (uint64_t)1 << sq;
        /* W-15: knight/king now aliased to Constants.c's const tables; only
         * the pawn tables are runtime-built (opposite convention there). */
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

/* W-14: single source of the non-pawn-material sum (was inlined in the
 * low-phase mop-up fold below; mirrors engine.py's _npm). */
static inline int npm_side(uint64_t occ, uint64_t knights, uint64_t bishops,
                           uint64_t rooks, uint64_t queens)
{
    return 320 * __builtin_popcountll(knights & occ)
         + 330 * __builtin_popcountll(bishops & occ)
         + 500 * __builtin_popcountll(rooks   & occ)
         + 900 * __builtin_popcountll(queens  & occ);
}

static inline uint64_t bishop_attacks(int sq, uint64_t occ)
{
    occ &= BISHOP_MASKS[sq];
    occ *= BISHOP_MAGIC_NUMBERS[sq];
    occ >>= 64 - BISHOP_REL_BITS[sq];
    return BISHOP_ATTACKS[sq][occ];
}

/* ---------- king shelter helper ------------------------------------------ */
/*
 * Per-file, per-distance pawn shield for one side's king.
 * Checks files kf-1, kf, kf+1; for each finds the closest own pawn strictly
 * in front of the king and applies sc (dist==1) or sf (dist==2).
 * is_white: 1 for white (pawns advance up), 0 for black (pawns advance down).
 * Returns a raw score already weighted by sc/sf (caller adds, not multiplies).
 */
static int compute_shelter(int ksq, uint64_t own_pawns, int is_white,
                           int sc, int sf)
{
    int score = 0;
    int kf    = ksq & 7;
    int kr    = ksq >> 3;
    int df;
    for (df = -1; df <= 1; df++) {
        int f = kf + df;
        if ((unsigned)f > 7u) continue;
        uint64_t fmask = FILE_BB[f];
        uint64_t ahead;
        if (is_white) {
            /* strictly above king rank: ranks kr+1 .. 7 */
            uint64_t below_incl = (kr < 7)
                ? (((uint64_t)1 << ((kr + 1) * 8)) - 1)
                : ~(uint64_t)0;
            ahead = own_pawns & fmask & ~below_incl;
            if (!ahead) continue;
            int psq  = __builtin_ctzll(ahead);   /* lowest = closest rank */
            int dist = (psq >> 3) - kr;
            if      (dist == 1) score += sc;
            else if (dist == 2) score += sf;
        } else {
            /* strictly below king rank: ranks 0 .. kr-1 */
            uint64_t above_incl = (kr > 0)
                ? ~(((uint64_t)1 << (kr * 8)) - 1)
                : ~(uint64_t)0;
            ahead = own_pawns & fmask & ~above_incl;
            if (!ahead) continue;
            int psq  = 63 - __builtin_clzll(ahead);  /* highest = closest rank */
            int dist = kr - (psq >> 3);
            if      (dist == 1) score += sc;
            else if (dist == 2) score += sf;
        }
    }
    return score;
}

/* ---------- main function ------------------------------------------------ */
/*
 * Mirrors _mobility_king_safety_bb in engine.py exactly.
 * kings: both kings' bitboard; the per-side square (0-63, or -1 if that king
 * is off the board) is derived below via kings & occ_w / occ_b.
 * Returns the score from White's perspective (positive = White better).
 */
int mobility_king_safety(
    uint64_t occ_w, uint64_t occ_b,
    uint64_t knights, uint64_t bishops, uint64_t rooks, uint64_t queens,
    uint64_t wp, uint64_t bp,
    uint64_t kings,
    int phase)
{
    /* U-04: derive king squares in C from the kings bitboard instead of
     * taking them as two int args -- drops two board.king() calls per eval
     * node on the Python side. -1 == that king is off the board. */
    int wksq = (kings & occ_w) ? __builtin_ctzll(kings & occ_w) : -1;
    int bksq = (kings & occ_b) ? __builtin_ctzll(kings & occ_b) : -1;

    uint64_t occ   = occ_w | occ_b;
    uint64_t wring = (wksq >= 0) ? KING_ATT[wksq] : 0ULL;
    uint64_t bring = (bksq >= 0) ? KING_ATT[bksq] : 0ULL;
    int score      = 0;

    /* FI-86: blend the four mobility weights ONCE per call, not per piece.
     * Identical to the old flat constants while MG == EG (integer exact).
     * Clamp phase the same way the callers do -- a phase above PHASE_MAX
     * would otherwise extrapolate past the MG end of the taper. */
    const int mob_pm = PHASE_MAX > 0 ? PHASE_MAX : 1;
    const int mob_ph = phase < 0 ? 0 : (phase > mob_pm ? mob_pm : phase);
    const int MOB_N = (MOB_N_MG * mob_ph + MOB_N_EG * (mob_pm - mob_ph)) / mob_pm;
    const int MOB_B = (MOB_B_MG * mob_ph + MOB_B_EG * (mob_pm - mob_ph)) / mob_pm;
    const int MOB_R = (MOB_R_MG * mob_ph + MOB_R_EG * (mob_pm - mob_ph)) / mob_pm;
    const int MOB_Q = (MOB_Q_MG * mob_ph + MOB_Q_EG * (mob_pm - mob_ph)) / mob_pm;
    int w_ring_att = 0;
    int b_ring_att = 0;
    uint64_t t;
    int sq;

    /* #3.x: per-side pawn attack sets (4 bulk shifts) -- used by BOTH
     * mobility-area and the threats block below, so compute unconditionally.
     * w_safe / b_safe pick whether mobility excludes enemy-pawn-attacked
     * squares (MOB_AREA_ON) or just enemy-pawn-blocked-by-own-occ. */
    uint64_t patk_w = ((wp << 9) & ~FILE_A) | ((wp << 7) & ~FILE_H);
    uint64_t patk_b = ((bp >> 7) & ~FILE_A) | ((bp >> 9) & ~FILE_H);
    uint64_t w_safe = MOB_AREA_ON ? (~occ_w & ~patk_b) : ~occ_w;
    uint64_t b_safe = MOB_AREA_ON ? (~occ_b & ~patk_w) : ~occ_b;
    /* FI-85: own same-ray slider masks (diag = bishops+queens, orth =
     * rooks+queens). Removing all own diagonal sliders is equivalent to
     * removing same-ray ones for the popcount -- off-ray pieces are not
     * blockers. */
    uint64_t wbat = (bishops | queens) & occ_w, bbat = (bishops | queens) & occ_b;
    uint64_t wrat = (rooks   | queens) & occ_w, brat = (rooks   | queens) & occ_b;
    /* #3.x: per-side minor-piece attack accumulator, OR'd inside the
     * knight + bishop mobility loops. Zero-init even when threats are
     * off so the threats block at the bottom can branch on a single int
     * without touching uninitialised storage. */
    uint64_t w_minor_atk = 0, b_minor_atk = 0;

    /* --- knights --- */
    for (t = knights & occ_w; t; t &= t-1) {
        sq = __builtin_ctzll(t);
        uint64_t a = KNIGHT_ATT[sq];
        score       += MOB_N * __builtin_popcountll(a & w_safe);
        b_ring_att  += __builtin_popcountll(a & bring);
        w_minor_atk |= a;                                /* #3.x */
        if (OUTPOST_ON) {
            int f = sq & 7, r = sq >> 3;
            uint64_t adj = 0;
            if (f > 0) adj |= FILE_BB[f-1];
            if (f < 7) adj |= FILE_BB[f+1];
            adj &= (r < 7) ? (~0ULL << ((r+1)*8)) : 0ULL;
            if (r >= 4 && (patk_w >> sq & 1) && !(bp & adj)) {
                int v = PHASE_MAX > 0 ? (OUTPOST_N_MG * phase + OUTPOST_N_EG * (PHASE_MAX-phase)) / PHASE_MAX : 0;
                score += v;
            }
        }
    }
    for (t = knights & occ_b; t; t &= t-1) {
        sq = __builtin_ctzll(t);
        uint64_t a = KNIGHT_ATT[sq];
        score       -= MOB_N * __builtin_popcountll(a & b_safe);
        w_ring_att  += __builtin_popcountll(a & wring);
        b_minor_atk |= a;                                /* #3.x */
        if (OUTPOST_ON) {
            int f = sq & 7, r = sq >> 3;
            uint64_t adj = 0;
            if (f > 0) adj |= FILE_BB[f-1];
            if (f < 7) adj |= FILE_BB[f+1];
            adj &= (r > 0) ? (~0ULL >> (64 - r*8)) : 0ULL;
            if (r <= 3 && (patk_b >> sq & 1) && !(wp & adj)) {
                int v = PHASE_MAX > 0 ? (OUTPOST_N_MG * phase + OUTPOST_N_EG * (PHASE_MAX-phase)) / PHASE_MAX : 0;
                score -= v;
            }
        }
    }

    /* --- bishops --- */
    for (t = bishops & occ_w; t; t &= t-1) {
        sq = __builtin_ctzll(t);
        uint64_t a = bishop_attacks(sq, occ);
        uint64_t ma = XRAY_MOB ? bishop_attacks(sq, occ & ~wbat) : a;  /* FI-85 */
        score       += MOB_B * __builtin_popcountll(ma & w_safe);
        b_ring_att  += __builtin_popcountll(a & bring);
        w_minor_atk |= a;                                /* #3.x */
        if (OUTPOST_ON) {
            int f = sq & 7, r = sq >> 3;
            uint64_t adj = 0;
            if (f > 0) adj |= FILE_BB[f-1];
            if (f < 7) adj |= FILE_BB[f+1];
            adj &= (r < 7) ? (~0ULL << ((r+1)*8)) : 0ULL;
            if (r >= 4 && (patk_w >> sq & 1) && !(bp & adj)) {
                int v = PHASE_MAX > 0 ? (OUTPOST_B_MG * phase + OUTPOST_B_EG * (PHASE_MAX-phase)) / PHASE_MAX : 0;
                score += v;
            }
        }
    }
    for (t = bishops & occ_b; t; t &= t-1) {
        sq = __builtin_ctzll(t);
        uint64_t a = bishop_attacks(sq, occ);
        uint64_t ma = XRAY_MOB ? bishop_attacks(sq, occ & ~bbat) : a;  /* FI-85 */
        score       -= MOB_B * __builtin_popcountll(ma & b_safe);
        w_ring_att  += __builtin_popcountll(a & wring);
        b_minor_atk |= a;                                /* #3.x */
        if (OUTPOST_ON) {
            int f = sq & 7, r = sq >> 3;
            uint64_t adj = 0;
            if (f > 0) adj |= FILE_BB[f-1];
            if (f < 7) adj |= FILE_BB[f+1];
            adj &= (r > 0) ? (~0ULL >> (64 - r*8)) : 0ULL;
            if (r <= 3 && (patk_b >> sq & 1) && !(wp & adj)) {
                int v = PHASE_MAX > 0 ? (OUTPOST_B_MG * phase + OUTPOST_B_EG * (PHASE_MAX-phase)) / PHASE_MAX : 0;
                score -= v;
            }
        }
    }

    /* --- rooks (W-10: open-file bonus fused into the same ctz loop that
     * already scans rooks&occ; integer adds are order-independent, so the
     * running score is identical to the old separate rook-files pass) --- */
    for (t = rooks & occ_w; t; t &= t-1) {
        sq = __builtin_ctzll(t);
        uint64_t a = rook_attacks(sq, occ);
        uint64_t ma = XRAY_MOB ? rook_attacks(sq, occ & ~wrat) : a;    /* FI-85 */
        score      += MOB_R * __builtin_popcountll(ma & w_safe);
        b_ring_att += __builtin_popcountll(a & bring);
        uint64_t fmask = 0x0101010101010101ULL << (sq & 7);
        if (!(wp & fmask))
            score += (bp & fmask) ? POS_ROOK_SEMI : POS_ROOK_OPEN;
    }
    for (t = rooks & occ_b; t; t &= t-1) {
        sq = __builtin_ctzll(t);
        uint64_t a = rook_attacks(sq, occ);
        uint64_t ma = XRAY_MOB ? rook_attacks(sq, occ & ~brat) : a;    /* FI-85 */
        score      -= MOB_R * __builtin_popcountll(ma & b_safe);
        w_ring_att += __builtin_popcountll(a & wring);
        uint64_t fmask = 0x0101010101010101ULL << (sq & 7);
        if (!(bp & fmask))
            score -= (wp & fmask) ? POS_ROOK_SEMI : POS_ROOK_OPEN;
    }

    /* --- queens --- */
    for (t = queens & occ_w; t; t &= t-1) {
        sq = __builtin_ctzll(t);
        uint64_t a = rook_attacks(sq, occ) | bishop_attacks(sq, occ);
        uint64_t ma = XRAY_MOB ? (rook_attacks(sq, occ & ~wrat)
                               |  bishop_attacks(sq, occ & ~wbat)) : a;  /* FI-85 */
        score      += MOB_Q * __builtin_popcountll(ma & w_safe);
        b_ring_att += __builtin_popcountll(a & bring);
    }
    for (t = queens & occ_b; t; t &= t-1) {
        sq = __builtin_ctzll(t);
        uint64_t a = rook_attacks(sq, occ) | bishop_attacks(sq, occ);
        uint64_t ma = XRAY_MOB ? (rook_attacks(sq, occ & ~brat)
                               |  bishop_attacks(sq, occ & ~bbat)) : a;  /* FI-85 */
        score      -= MOB_Q * __builtin_popcountll(ma & b_safe);
        w_ring_att += __builtin_popcountll(a & wring);
    }

    /* --- pawn and enemy-king attacks on king rings --- *
     * C-07: counted per shift DIRECTION in bulk instead of per pawn. Each
     * direction maps pawns to targets injectively, so attack INCIDENCES are
     * preserved exactly (a ring square attacked by two pawns still counts
     * twice) -- bit-identical to the old per-pawn PAWN_ATT loops. */
    if (wring) {
        w_ring_att += __builtin_popcountll(((bp >> 7) & ~FILE_A) & wring)
                    + __builtin_popcountll(((bp >> 9) & ~FILE_H) & wring);
        if (bksq >= 0)
            w_ring_att += __builtin_popcountll(KING_ATT[bksq] & wring);
    }
    if (bring) {
        b_ring_att += __builtin_popcountll(((wp << 9) & ~FILE_A) & bring)
                    + __builtin_popcountll(((wp << 7) & ~FILE_H) & bring);
        if (wksq >= 0)
            b_ring_att += __builtin_popcountll(KING_ATT[wksq] & bring);
    }

    /* --- king-safety terms (tapered) ------------------------------------ */
    {
        int pm         = PHASE_MAX;
        /* pm > 0 guards match the sc/sf lines below: a config with
         * phase_max = 0 must degrade to 0, not SIGFPE. */
        int shield_val = pm > 0 ? (SHIELD_MG * phase + SHIELD_EG * (pm - phase)) / pm : 0;
        int ring_val   = pm > 0 ? (RING_MG   * phase + RING_EG   * (pm - phase)) / pm : 0;
        int open_val   = pm > 0 ? (OPEN_MG   * phase + OPEN_EG   * (pm - phase)) / pm : 0;
        /* Shelter taper: full in MG, 0 in EG (king should be active there) */
        int sc = (pm > 0) ? (SHELTER_CLOSE * phase) / pm : 0;
        int sf = (pm > 0) ? (SHELTER_FAR   * phase) / pm : 0;

        if (wksq >= 0) {
            if (SHELTER_ON)
                score += compute_shelter(wksq, wp, 1, sc, sf);
            else
                score += __builtin_popcountll(wring & occ_w) * shield_val;
            score -= w_ring_att * ring_val;
            if (!(wp & FILE_BB[wksq & 7])) score -= open_val;
        }
        if (bksq >= 0) {
            if (SHELTER_ON)
                score -= compute_shelter(bksq, bp, 0, sc, sf);
            else
                score -= __builtin_popcountll(bring & occ_b) * shield_val;
            score += b_ring_att * ring_val;
            if (!(bp & FILE_BB[bksq & 7])) score += open_val;
        }
    }

    /* --- #2.5b: rook_files + bishop_pair folded in ---------------------- *
     * mobility_king_safety runs at EVERY phase since the roadmap-#1 fix
     * (originally only the Python dispatcher's high-phase branch reached
     * it). The Python flow always added _rook_files_bb + _bishop_pair_bb on
     * top of the C call, so we fold them in here unconditionally and have
     * the Python side skip those two calls. Removes a second / third Python
     * function call per eval at no extra ctypes round-trip.
     */
    {
        /* W-10: rook (semi-)open files now scored inside the rook mobility
         * loops above -- the separate pass here was a second scan of the
         * same rook sets. */
        /* bishop pair, phased blend. */
        if (PHASE_MAX > 0) {
            int bpv = (POS_BP_MG * phase + POS_BP_EG * (PHASE_MAX - phase)) / PHASE_MAX;
            if (__builtin_popcountll(bishops & occ_w) >= 2) score += bpv;
            if (__builtin_popcountll(bishops & occ_b) >= 2) score -= bpv;
        }
        /* #3.x: rook on 7th. Skip the whole block if R7_MG=R7_EG=0
         * (disabled via toggle), so the cost is one branch when off. */
        if ((R7_MG | R7_EG) && PHASE_MAX > 0) {
            int r7v = (R7_MG * phase + R7_EG * (PHASE_MAX - phase)) / PHASE_MAX;
            uint64_t RANK_7_BB = 0x00FF000000000000ULL;
            uint64_t RANK_2_BB = 0x000000000000FF00ULL;
            uint64_t RANK_8_BB = 0xFF00000000000000ULL;
            uint64_t RANK_1_BB = 0x00000000000000FFULL;
            /* White rook on rank 7 is "real" if black king sits on rank 8
             * (cornered) OR a black pawn still sits on rank 7 (target). */
            uint64_t w7 = rooks & occ_w & RANK_7_BB;
            if (w7 && ((bksq >= 0 && (1ULL << bksq) & RANK_8_BB) || (bp & RANK_7_BB)))
                score += r7v * __builtin_popcountll(w7);
            uint64_t b7 = rooks & occ_b & RANK_2_BB;
            if (b7 && ((wksq >= 0 && (1ULL << wksq) & RANK_1_BB) || (wp & RANK_2_BB)))
                score -= r7v * __builtin_popcountll(b7);
        }
        /* #3.x: threats. Pawn -> any enemy non-pawn, minor -> enemy major.
         * Cheap: just a couple of AND + popcount per class. Zero when both
         * weights are disabled. */
        if (THREAT_PAWN | THREAT_MINOR) {
            uint64_t b_non_pawn = occ_b & ~bp;
            uint64_t w_non_pawn = occ_w & ~wp;
            uint64_t b_major    = (rooks | queens) & occ_b;
            uint64_t w_major    = (rooks | queens) & occ_w;
            if (THREAT_PAWN) {
                score += THREAT_PAWN * __builtin_popcountll(patk_w & b_non_pawn);
                score -= THREAT_PAWN * __builtin_popcountll(patk_b & w_non_pawn);
            }
            if (THREAT_MINOR) {
                score += THREAT_MINOR * __builtin_popcountll(w_minor_atk & b_major);
                score -= THREAT_MINOR * __builtin_popcountll(b_minor_atk & w_major);
            }
        }
        /* Space: safe central squares (c-f files, ranks 2-4 for white /
         * ranks 5-7 for black) not attacked by an enemy pawn. Tapered by
         * phase so it fades in the endgame (no pieces left to occupy space).
         * patk_w/patk_b already computed above unconditionally. */
        if (SPACE_ON && PHASE_MAX > 0) {
            static const uint64_t CENTER_FILES = 0x3C3C3C3C3C3C3C3CULL;
            static const uint64_t SPACE_W = 0x00000000FFFFFF00ULL; /* ranks 2-4 (bits 8-31) */
            static const uint64_t SPACE_B = 0x00FFFFFF00000000ULL; /* ranks 5-7 (bits 32-55) */
            int space_val = (SPACE_MG * phase) / PHASE_MAX;
            int w_sp = __builtin_popcountll(CENTER_FILES & SPACE_W & ~patk_b & ~wp);
            int b_sp = __builtin_popcountll(CENTER_FILES & SPACE_B & ~patk_w & ~bp);
            score += space_val * (w_sp - b_sp);
        }
        if (PHALANX_ON && PHASE_MAX > 0) {
            uint64_t phalanx_w  = wp & ((wp & ~FILE_H) << 1 | (wp & ~FILE_A) >> 1);
            uint64_t phalanx_b  = bp & ((bp & ~FILE_H) << 1 | (bp & ~FILE_A) >> 1);
            uint64_t supported_w = wp & patk_w;
            uint64_t supported_b = bp & patk_b;
            int w_conn = __builtin_popcountll(phalanx_w | supported_w);
            int b_conn = __builtin_popcountll(phalanx_b | supported_b);
            int conn_val = (PHALANX_MG * phase + PHALANX_EG * (PHASE_MAX - phase)) / PHASE_MAX;
            score += conn_val * (w_conn - b_conn);
        }
        if (STORM_ON && PHASE_MAX > 0) {
            int storm_val = (STORM_MG * phase + STORM_EG * (PHASE_MAX - phase)) / PHASE_MAX;
            /* Ranks 5-7 for white (bits 32-55): white pawns past the midline. */
            static const uint64_t RANKS_5_7 = 0x00FFFFFF00000000ULL;
            /* Ranks 2-4 for black (bits 8-31): black pawns past the midline. */
            static const uint64_t RANKS_2_4 = 0x00000000FFFFFF00ULL;
            if (bksq >= 0) {
                int kf = bksq & 7;
                uint64_t sf = FILE_BB[kf];
                if (kf > 0) sf |= FILE_BB[kf-1];
                if (kf < 7) sf |= FILE_BB[kf+1];
                score += storm_val * __builtin_popcountll(wp & sf & RANKS_5_7);
            }
            if (wksq >= 0) {
                int kf = wksq & 7;
                uint64_t sf = FILE_BB[kf];
                if (kf > 0) sf |= FILE_BB[kf-1];
                if (kf < 7) sf |= FILE_BB[kf+1];
                score -= storm_val * __builtin_popcountll(bp & sf & RANKS_2_4);
            }
        }
    }

    /* --- C-18: low-phase mop-up folded in (ABI 2) ----------------------- *
     * Active at phase <= 6 -- mirroring engine.py's old dispatch, whose
     * Python side now skips its separate _mopup_bb call whenever this C
     * eval ran. */
    if (phase <= 6 && wksq >= 0 && bksq >= 0) {
        int npm_w = npm_side(occ_w, knights, bishops, rooks, queens);  /* W-14 */
        int npm_b = npm_side(occ_b, knights, bishops, rooks, queens);
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

/* ====================================================================== *
 * #2.5: eval-constant setters (rook_files + bishop_pair + mopup + extras)
 *
 * Kept in sync with the Engine tuner via the set_*_params calls below, each
 * invoked once from Engine.__init__ (just like set_mobility_params). The
 * rook_files + bishop_pair + mopup values are consumed by mobility_king_
 * safety's #2.5b inlined pass and its folded low-phase mop-up; they live at
 * file scope because that function is defined earlier in this TU and reads
 * them directly. CENTER_MANHATTAN is a pure function of (file, rank), stored
 * statically here identical to engine.py's _center_manhattan.
 * ====================================================================== */

/* #3.x: rook-on-7th setter. Called once from Engine.__init__ to keep the
 * C-side weights in sync with the Python tuner. Pass 0/0 to disable. */
void set_rook_on_7th_params(int mg, int eg)
{
    R7_MG = mg; R7_EG = eg;
}

/* #3.x: mobility-area setter (1 = on, 0 = legacy). */
void set_mobility_area(int on)
{
    MOB_AREA_ON = on ? 1 : 0;
}

/* #3.x: threats setter. Pass 0/0 to disable both classes. */
void set_threats_params(int pawn, int minor)
{
    THREAT_PAWN = pawn; THREAT_MINOR = minor;
}

void set_outpost_params(int on, int n_mg, int n_eg, int b_mg, int b_eg)
{
    OUTPOST_ON = on ? 1 : 0;
    OUTPOST_N_MG = n_mg; OUTPOST_N_EG = n_eg;
    OUTPOST_B_MG = b_mg; OUTPOST_B_EG = b_eg;
}

void set_space_params(int on, int space_mg)
{
    SPACE_ON = on ? 1 : 0;
    SPACE_MG = space_mg;
}

void set_phalanx_params(int on, int mg, int eg)
{
    PHALANX_ON = on ? 1 : 0;
    PHALANX_MG = mg;
    PHALANX_EG = eg;
}

void set_storm_params(int on, int mg, int eg)
{
    STORM_ON = on ? 1 : 0;
    STORM_MG = mg;
    STORM_EG = eg;
}

void set_shelter_params(int on, int close_mg, int far_mg)
{
    SHELTER_ON    = on ? 1 : 0;
    SHELTER_CLOSE = close_mg;
    SHELTER_FAR   = far_mg;
}

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
    (void)mopup_str_cmd; (void)mopup_str_king;   /* FI-27: see above */
}

/* ====================================================================== *
 * Static Exchange Evaluation (SEE) -- roadmap item #15.
 *
 * Mirrors engine.py's _see / _see_attackers / _least_valuable_attacker
 * exactly. PIECE_VALUES there is a fixed class constant, never tuned by
 * setoption or the WDL tuner (confirmed by grep before porting), so it's
 * hardcoded here rather than threaded through as a param like the tunable
 * eval weights elsewhere in this file.
 *
 * piece-type numbering matches python-chess: 1=PAWN 2=KNIGHT 3=BISHOP
 * 4=ROOK 5=QUEEN 6=KING (0 reserved for "none" / not-a-piece).
 * ====================================================================== */
static const int SEE_VALUES[7] = {0, 100, 320, 330, 500, 900, 20000};

/* Every piece (either colour) in `occupied` that attacks `square`. Mirrors
 * _see_attackers: the piece-type bitboards (pawns/knights/.../queens) are
 * NOT shrunk as the exchange proceeds -- only `occupied` shrinks, and the
 * single trailing `& occupied` is what removes a piece from consideration
 * once it's been "captured" in the simulation (including x-ray attackers
 * behind it becoming visible via the slider re-scan against the smaller
 * occupancy). occ_w/occ_b are likewise the ORIGINAL colour masks. */
static inline uint64_t see_attackers(
    uint64_t pawns, uint64_t knights, uint64_t bishops, uint64_t rooks,
    uint64_t queens, uint64_t kings, uint64_t occ_w, uint64_t occ_b,
    int square, uint64_t occupied)
{
    uint64_t bishops_queens = bishops | queens;
    uint64_t rooks_queens   = rooks | queens;
    /* V-08: skip the magic lookup when there's no such slider (mirror C-09).
     * diag/line are only ever ANDed with bishops_queens/rooks_queens, which
     * are 0 here, so the result is byte-identical. */
    uint64_t diag = bishops_queens ? bishop_attacks(square, occupied) : 0ULL;
    uint64_t line = rooks_queens   ? rook_attacks(square, occupied)   : 0ULL;
    uint64_t attackers =
          (KNIGHT_ATT[square] & knights)
        | (KING_ATT[square]   & kings)
        | (PAWN_ATT_B[square] & pawns & occ_w)   /* white pawns attacking `square` */
        | (PAWN_ATT_W[square] & pawns & occ_b)   /* black pawns attacking `square` */
        | (diag & bishops_queens)
        | (line & rooks_queens);
    return attackers & occupied;
}

/* (square, value) of the cheapest piece in `attackers` (caller has already
 * masked to a single colour). Mirrors _least_valuable_attacker's cheapest-
 * first piece-type order. Returns square -1 / value 0 if attackers is empty
 * (mirrors the (None, 0) Python return; callers here never call with an
 * empty attackers set, matching the Python control flow, but kept safe). */
static inline int see_lva(uint64_t attackers, uint64_t pawns, uint64_t knights,
                          uint64_t bishops, uint64_t rooks, uint64_t queens,
                          uint64_t kings, int *out_value)
{
    uint64_t subset;
    if ((subset = attackers & pawns))   { *out_value = SEE_VALUES[1]; return __builtin_ctzll(subset); }
    if ((subset = attackers & knights)) { *out_value = SEE_VALUES[2]; return __builtin_ctzll(subset); }
    if ((subset = attackers & bishops)) { *out_value = SEE_VALUES[3]; return __builtin_ctzll(subset); }
    if ((subset = attackers & rooks))   { *out_value = SEE_VALUES[4]; return __builtin_ctzll(subset); }
    if ((subset = attackers & queens))  { *out_value = SEE_VALUES[5]; return __builtin_ctzll(subset); }
    if ((subset = attackers & kings))   { *out_value = SEE_VALUES[6]; return __builtin_ctzll(subset); }
    *out_value = 0;
    return -1;
}

/* Net material (cp) won by capturing from_sq->to_sq if both sides keep
 * recapturing with their least-valuable attacker, each free to stop at
 * their best point. Mirrors _see exactly, including the fold-back loop.
 * turn: 1 if White is making the initial capture, 0 if Black.
 * is_ep: 1 if this is an en-passant capture (to_sq is the empty square
 * behind the actual captured pawn). Returns 0 if from_sq/to_sq don't
 * actually hold an attacker/victim (mirrors the Python "not a capture"
 * early-outs). */
int see(uint64_t pawns, uint64_t knights, uint64_t bishops, uint64_t rooks,
       uint64_t queens, uint64_t kings, uint64_t occ_w, uint64_t occ_b,
       int turn, int from_sq, int to_sq, int is_ep)
{

    uint64_t occupied = occ_w | occ_b;
    uint64_t tobit = 1ULL << to_sq;
    uint64_t frombit = 1ULL << from_sq;
    int target_value, attacker_value;
    int ep_sq = -1;

    if (is_ep) {
        target_value = SEE_VALUES[1];
        ep_sq = to_sq + (turn ? -8 : 8);
    } else {
        int victim_pt = 0;
        if      (pawns   & tobit) victim_pt = 1;
        else if (knights & tobit) victim_pt = 2;
        else if (bishops & tobit) victim_pt = 3;
        else if (rooks   & tobit) victim_pt = 4;
        else if (queens  & tobit) victim_pt = 5;
        else if (kings   & tobit) victim_pt = 6;
        if (victim_pt == 0) return 0;              /* not a capture */
        target_value = SEE_VALUES[victim_pt];
    }

    int attacker_pt = 0;
    if      (pawns   & frombit) attacker_pt = 1;
    else if (knights & frombit) attacker_pt = 2;
    else if (bishops & frombit) attacker_pt = 3;
    else if (rooks   & frombit) attacker_pt = 4;
    else if (queens  & frombit) attacker_pt = 5;
    else if (kings   & frombit) attacker_pt = 6;
    if (attacker_pt == 0) return 0;
    attacker_value = SEE_VALUES[attacker_pt];

    occupied &= ~frombit;
    if (ep_sq >= 0) occupied &= ~(1ULL << ep_sq);

    int side = !turn;    /* side to recapture next: the non-mover */
    uint64_t attackers = see_attackers(pawns, knights, bishops, rooks, queens,
                                       kings, occ_w, occ_b, to_sq, occupied);

    int gain[32];
    gain[0] = target_value;
    int d = 0;
    while (1) {
        d++;
        gain[d] = attacker_value - gain[d - 1];
        uint64_t side_occ = side ? occ_w : occ_b;
        uint64_t side_attackers = attackers & side_occ & occupied;
        if (!side_attackers) break;
        int lva_value;
        int lva_sq = see_lva(side_attackers, pawns, knights, bishops, rooks,
                             queens, kings, &lva_value);
        occupied &= ~(1ULL << lva_sq);
        attackers = see_attackers(pawns, knights, bishops, rooks, queens,
                                  kings, occ_w, occ_b, to_sq, occupied);
        attacker_value = lva_value;
        side = !side;
        if (d >= 31) break;
    }

    while (d > 1) {
        d--;
        int neg = -gain[d - 1];
        gain[d - 1] = -(neg > gain[d] ? neg : gain[d]);
    }
    return gain[0];
}

/* ---------- exported: ABI handshake --------------------------------------- *
 * Bump together with _EVAL_C_ABI in engine.py's load block whenever an
 * exported signature or the semantics of an existing export change, so a
 * stale-but-loadable .so is rejected at load instead of silently
 * mis-evaluating. */
int abi_version(void) { return 5; }   /* 2: C-18 folded mopup into
                                       *    mobility_king_safety at phase <= 6
                                       * 3: U-04 mobility_king_safety takes a
                                       *    kings bitboard, not wksq/bksq ints
                                       * 4: FI-85 set_xray_mob
                                       * 5: FI-86 set_mobility_eg */