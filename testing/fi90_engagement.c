/* FI-90: the PER-NODE engagement dead-gate.
 *
 *   cc -O1 -g -I. -w -DQS_MARGIN_COUNT -o /tmp/fi90 \
 *      testing/fi90_engagement.c eval_c.c Constants.c -lm -lpthread
 *   /tmp/fi90
 *
 * The house closes items pre-A/B when the mechanism barely fires -- FI-48,
 * FI-63 and P-33 went that way, saving three slots and ~$54. FI-89 shows the
 * other failure mode: its engagement was counted by replaying GAME POSITIONS
 * and read 0.733%, so the change was called narrow; the screen came back at
 * 99.90% engagement and -20.17 Elo, because the rule ran at every NODE.
 *
 * This counts inside a real search, at the site where the decision is made:
 *   seen = capture admissions the SEE gate judged (mover > victim)
 *   flip = admissions the margin moved from SKIP to ADMIT (SEE in [-margin, 0))
 *
 * MEASURED 2026-07-27, margin 100 vs 0:
 *   3,602,371 judged, 190,936 FLIPPED -- 5.30% of judged, 1.86% of all nodes.
 * Genuine engagement, orders of magnitude above the ~0.001% that closed FI-48.
 *
 * **DO NOT QUOTE THIS DRIVER'S NODE COUNT AS A NODE COST.** install_eval()
 * below is a toy PST, and tree SHAPE depends on the eval: this driver reads
 * +42.7% nodes at margin 100 while the real synced eval reads **-17.1%**
 * (bench 1,211,981 vs 1,461,732). Opposite sign. The FLIP count is
 * eval-independent -- it turns on SEE_VALUES, which are real here -- so that
 * is what this driver is for, and only that. Take node cost from the bench.
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

/* startpos and a middlegame with both bishops and knights in contact -- the
 * BxN-defended motif needs pieces that can actually meet. */
static const struct { const char* name; uint64_t p, n, b, r, q, k, w, bl;
                      int turn, ep; uint64_t cr; } POS[] = {
    {"startpos",
     0x00FF00000000FF00ULL, 0x4200000000000042ULL, 0x2400000000000024ULL,
     0x8100000000000081ULL, 0x0800000000000008ULL, 0x1000000000000010ULL,
     0x000000000000FFFFULL, 0xFFFF000000000000ULL, 1, -1,
     0x8100000000000081ULL},
    {"middlegame",
     0x00EF00101000EF00ULL, 0x4000040000200002ULL, 0x2400000200000004ULL,
     0x8100000000000081ULL, 0x0800000000000008ULL, 0x1000000000000010ULL,
     0x000000021020EF9FULL, 0xFDEF041000000000ULL, 1, -1,
     0x8100000000000081ULL},
};

int main(void)
{
    install_eval();
    set_tt_bits(22);
    for (int margin = 0; margin <= 100; margin += 100) {
        set_qs_see_margin(margin);
        cs_qsm_reset();
        uint64_t total = 0;
        for (unsigned i = 0; i < sizeof(POS) / sizeof(POS[0]); i++)
            for (int d = 8; d <= 13; d++) {
                cs_tt_reset();
                cs_search_begin(NULL, 0, 0.0);
                uint64_t nodes = 0;
                int score = 0, done = 0, aborted = 0, second = 0;
                cs_search_root(POS[i].p, POS[i].n, POS[i].b, POS[i].r, POS[i].q,
                               POS[i].k, POS[i].w, POS[i].bl, POS[i].turn,
                               POS[i].ep, POS[i].cr, d, -CS_INF, CS_INF, 0, 0,
                               &nodes, &score, &done, &aborted, &second);
                total += g_nodes;
            }
        long seen = 0, flip = 0;
        cs_qsm_stats(&seen, &flip);
        printf("  margin %3d cp: %10llu nodes   SEE-judged admissions %9ld   "
               "FLIPPED %8ld  (%.4f%% of judged, %.5f%% of nodes)\n",
               margin, (unsigned long long)total, seen, flip,
               seen ? flip * 100.0 / seen : 0.0,
               total ? flip * 100.0 / (double)total : 0.0);
    }
    return 0;
}
