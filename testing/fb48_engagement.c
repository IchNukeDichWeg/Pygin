/* FB-48: the PER-NODE stalemate engagement dead-gate.
 *
 *   cc -O1 -g -I. -w -DSM_COUNT -o /tmp/fb48 \
 *      testing/fb48_engagement.c eval_c.c Constants.c -lm -lpthread
 *   /tmp/fb48
 *
 * The entry says up front that the population is thin. The d11x6 bench agrees
 * loudly: with the toggle ARMED the signature is 1,461,732 -- byte-identical
 * to the toggle off. Zero engagement across six middlegame positions.
 *
 * But stalemate is an ENDGAME motif, and the bench has no endgames. So this
 * drives positions where stalemate is actually reachable: bare-king races,
 * K+P vs K, and the blocked-wall fortress the selftest already uses.
 *
 *   fires   = in-tree stalemate returns (not in check, no legal move)
 *   nonzero = those where draw_score(b) != 0 -- the ONLY ones the fix changes.
 *             A stalemate at material parity scores 0 either way, so `fires`
 *             overstates engagement and `nonzero` is the real number.
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

/* Endgames, where stalemate lives. Material is DELIBERATELY lopsided in most
 * of them -- draw_score only differs from 0 when there is a material gap, so
 * a balanced set would report `fires` without `nonzero` and read as no
 * engagement for the wrong reason. */
static const struct { const char* name; uint64_t p, n, b, r, q, k, w, bl;
                      int turn, ep; uint64_t cr; } POS[] = {
    /* K+P vs K, white a-pawn on a7, kings a8/c7 -- the classic stalemate trap */
    {"KPvK a7",   1ULL << 48, 0, 0, 0, 0, (1ULL << 50) | (1ULL << 56),
     (1ULL << 48) | (1ULL << 50), 1ULL << 56, 1, -1, 0},
    /* K+Q vs K, the other classic: a careless queen move stalemates */
    {"KQvK",      0, 0, 0, 0, 1ULL << 27, (1ULL << 28) | (1ULL << 63),
     (1ULL << 27) | (1ULL << 28), 1ULL << 63, 1, -1, 0},
    /* K+R vs K */
    {"KRvK",      0, 0, 0, 1ULL << 24, 0, (1ULL << 26) | (1ULL << 63),
     (1ULL << 24) | (1ULL << 26), 1ULL << 63, 1, -1, 0},
    /* rook + pawns vs rook, a real lopsided ending */
    {"R+2P vs R", (1ULL << 33) | (1ULL << 41), 0, 0,
     (1ULL << 8) | (1ULL << 63), 0, (1ULL << 4) | (1ULL << 60),
     (1ULL << 4) | (1ULL << 8) | (1ULL << 33) | (1ULL << 41),
     (1ULL << 60) | (1ULL << 63), 1, -1, 0},
};

int main(void)
{
    install_eval();
    set_tt_bits(22);
    set_sm_contempt(1);                   /* count with the rule ARMED */
    cs_sm_reset();
    uint64_t total = 0;
    for (unsigned i = 0; i < sizeof(POS) / sizeof(POS[0]); i++)
        for (int d = 10; d <= 18; d++) {
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
    long fires = 0, nonzero = 0;
    cs_sm_stats(&fires, &nonzero);
    printf("  %llu nodes over 4 endgames x d10-18\n",
           (unsigned long long)total);
    printf("  in-tree stalemate returns : %ld  (%.5f%% of nodes)\n",
           fires, total ? fires * 100.0 / (double)total : 0.0);
    printf("  ...where draw_score != 0  : %ld  (%.5f%% of nodes)  <- the ONLY\n"
           "                                                        ones FB-48\n"
           "                                                        changes\n",
           nonzero, total ? nonzero * 100.0 / (double)total : 0.0);
    return 0;
}
