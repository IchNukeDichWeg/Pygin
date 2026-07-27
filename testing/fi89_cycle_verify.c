/* FI-89: the CYCLE_VERIFY differential, re-run with the STRICT arm on.
 *
 *   cc -O1 -g -I. -w -DCYCLE_VERIFY -o /tmp/fi89 \
 *      testing/fi89_cycle_verify.c eval_c.c Constants.c -lm -lpthread
 *   /tmp/fi89
 *
 * FI-29 shipped on this oracle: every upcoming_repetition claim is re-proven
 * by actually making the claimed move and comparing the child key to the
 * matched past key (13,272 claims / 0 mismatches over 1.1M nodes at the time).
 * FI-89 adds a second condition to that function, so the differential has to
 * be re-run with the new arm live -- a soundness gate is only current for the
 * code it was last run against.
 *
 * The CYCLE_VERIFY block sits BEFORE FI-89's `continue`, so claims that the
 * strict arm then discards are still counted and still verified. That is the
 * coverage we want: it proves the delta match is sound whether or not the new
 * rule keeps it.
 *
 * Position: a knight shuffle from the start position, replayed as GAME
 * HISTORY rather than as tree moves, because the pre-root history is exactly
 * the population FI-89 changes. After each full Nf3/Nf6/Ng1/Ng8 cycle both
 * sides are back to the starting arrangement with the halfmove clock still
 * running, so the root repeats a pre-root position and the side to move can
 * force another repetition in one reversible move.
 */
#include "csearch.c"

static void install_eval(void)
{
    static int mg[6 * 64], eg[6 * 64];
    static const int val[7] = {0, 100, 320, 330, 500, 900, 0};
    static const int ph[7]  = {0, 0, 1, 1, 2, 4, 0};
    static const int pm[8]  = {0, 5, 10, 20, 35, 60, 100, 0};
    static const int pe[8]  = {0, 10, 20, 35, 60, 100, 150, 0};
    for (int pt = 0; pt < 6; pt++)
        for (int sq = 0; sq < 64; sq++) {
            int f = sq & 7, r = sq >> 3;
            int c = (3 - (f < 4 ? 3 - f : f - 4)) + (3 - (r < 4 ? 3 - r : r - 4));
            mg[pt * 64 + sq] = c * 2;
            eg[pt * 64 + sq] = c;
        }
    csearch_set_eval(mg, eg, val, val, ph, 10, -12, -15, -8, pm, pe, 500, 8, 10);
}

/* start position, and the four knight hops of one cycle (from, to) */
#define G1 6
#define F3 21
#define G8 62
#define F6 45

static const uint64_t P  = 0x00FF00000000FF00ULL, B_ = 0x2400000000000024ULL,
                      R  = 0x8100000000000081ULL, Q  = 0x0800000000000008ULL,
                      K  = 0x1000000000000010ULL, CR = 0x8100000000000081ULL;

/* The nine positions of a two-cycle shuffle, as (knights, occW, occB, turn).
 * Every even index is White to move and repeats the start arrangement at
 * indices 0, 4 and 8 -- so index 8 is the THIRD occurrence and index 4 the
 * second, which is the distinction FI-89 is about. */
static void position(int i, uint64_t* n, uint64_t* w, uint64_t* b, int* turn)
{
    uint64_t kn = 0x4200000000000042ULL;
    uint64_t ow = 0x000000000000FFFFULL, ob = 0xFFFF000000000000ULL;
    const int wf[2] = {G1, F3}, bf[2] = {G8, F6};
    for (int s = 0; s < i; s++) {
        int white = (s % 2) == 0;
        int leg = (s / 2) % 2;                 /* 0 = out, 1 = back */
        int from = white ? wf[leg] : bf[leg];
        int to   = white ? wf[!leg] : bf[!leg];
        uint64_t m = (1ULL << from) | (1ULL << to);
        kn ^= m;
        if (white) ow ^= m; else ob ^= m;
    }
    *n = kn; *w = ow; *b = ob; *turn = (i % 2) == 0;
}

int main(void)
{
    install_eval();
    set_tt_bits(20);
    set_cycle(1);

    /* history keys, most recent first -- cs_search_begin's convention */
    uint64_t keys[9];
    for (int i = 0; i <= 8; i++) {
        uint64_t n, w, b; int turn;
        position(i, &n, &w, &b, &turn);
        keys[i] = cs_board_key(P, n, B_, R, Q, K, w, b, turn, -1, CR);
    }
    if (keys[0] != keys[4] || keys[4] != keys[8]) {
        printf("  SETUP BROKEN: the shuffle does not return to its own key\n");
        return 1;
    }

    int bad = 0;
    for (int strict = 0; strict <= 1; strict++) {
        set_rep_strict(strict);
        long hits = 0, badclaims = 0;
        uint64_t total_nodes = 0;
        /* root = index 8; history = 7..0, most recent first */
        for (int depth = 6; depth <= 12; depth++) {
            uint64_t hist[8];
            for (int j = 0; j < 8; j++) hist[j] = keys[7 - j];
            cs_tt_reset();
            cs_search_begin(hist, 8, 0.0);
            uint64_t n, w, b; int turn;
            position(8, &n, &w, &b, &turn);
            uint64_t nodes = 0;
            int score = 0, done = 0, aborted = 0, second = 0;
            cs_search_root(P, n, B_, R, Q, K, w, b, turn, -1, CR,
                           depth, -CS_INF, CS_INF, 0, 0,
                           &nodes, &score, &done, &aborted, &second);
            total_nodes += g_nodes;
        }
        cs_cycle_stats(&hits, &badclaims);
        printf("  rep_strict=%d  %8llu nodes   cuckoo claims %6ld   "
               "MISMATCHES %ld  %s\n", strict,
               (unsigned long long)total_nodes, hits, badclaims,
               badclaims ? "** UNSOUND **" : "sound");
        if (badclaims) bad++;
        g_cyc_hits = g_cyc_bad = 0;
    }
    printf("  %s\n", bad ? "FAIL -- a claimed cycle did not reproduce its key"
                         : "PASS -- every claimed cycle re-made its past key "
                           "exactly, both arms");
    return bad ? 1 : 0;
}
