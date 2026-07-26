/* FI-96: the SMP orchestration oracle, and the teeth for the 4-thread gate.
 *
 *   # the oracle -- helpers search but publish nothing:
 *   cc -O1 -g -I. -w -DFI96_HELPER_MUTE -o /tmp/fi96_mute \
 *      testing/fi96_orchestration.c eval_c.c Constants.c -lm -lpthread
 *   /tmp/fi96_mute            -> 4T main search MUST equal 1T, node for node
 *
 *   # the control -- the shipped configuration:
 *   cc -O1 -g -I. -w -o /tmp/fi96_plain \
 *      testing/fi96_orchestration.c eval_c.c Constants.c -lm -lpthread
 *   /tmp/fi96_plain           -> they MUST differ (helpers feed the TT)
 *
 * WHY THIS EXISTS. The tree ships real concurrency (FB-12's atomics, FI-47's
 * pool) and the 4-thread selftest check passes -- but nothing ever showed it
 * CAN fail, which is the standard FI-16 set and FI-87 followed. TSan covers
 * races; it says nothing about ORCHESTRATION: a dropped signal, a skipped
 * join, a helper whose generation counter is stale. Those produce a
 * well-synchronised wrong answer.
 *
 * The invariant: with helper stores suppressed, the only channel between
 * helpers and the main thread is closed, so the main thread's search must be
 * a node-for-node replay of the single-threaded one. Any divergence means
 * state leaked by some path other than the TT.
 *
 * WHAT THIS ORACLE DOES **NOT** SEE -- measured 2026-07-26, not assumed.
 * Three deliberate breaks were applied and only ONE reddens it:
 *
 *   corrupt one helper store   -> 6/6 pairs diverge   CAUGHT
 *   skip the pool join         -> 0/6 diverge         MISSED (TSan: 1 report)
 *   drop the per-move policy   -> 0/6 diverge         MISSED
 *
 * That is by construction: muting removes exactly the channel the other two
 * act through. A skipped join is a LIFETIME bug (helpers outliving the stack
 * frame that holds their args) -- ThreadSanitizer's job, and it does flag it.
 * A dropped per-move table policy changes helper ORDERING QUALITY, which is
 * invisible to the main thread's node count and only shows up as time-to-depth
 * (FI-95's instrument).
 *
 * So the SMP suite is three instruments with disjoint coverage:
 *   TSan            races and lifetime
 *   this oracle     state leaking outside the TT
 *   nps13 --threads helper quality
 * Anyone extending this should know which of the three their change belongs to.
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

/* startpos and the nps13 middlegame (bitboards from python-chess) */
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

/* One search from a COLD table; returns the MAIN thread's node count. */
static uint64_t run(int idx, int depth, int threads)
{
    set_threads(threads);
    cs_tt_reset();
    cs_search_begin(NULL, 0, 0.0);
    uint64_t nodes = 0;
    int score = 0, done = 0, aborted = 0, second = 0;
    cs_search_root(POS[idx].p, POS[idx].n, POS[idx].b, POS[idx].r, POS[idx].q,
                   POS[idx].k, POS[idx].w, POS[idx].bl, POS[idx].turn,
                   POS[idx].ep, POS[idx].cr, depth, -CS_INF, CS_INF, 0, 0,
                   &nodes, &score, &done, &aborted, &second);
    return g_nodes;          /* MAIN thread only -- helpers aggregate apart */
}

int main(void)
{
    install_eval();
    set_tt_bits(20);
#ifdef FI96_HELPER_MUTE
    const char* mode = "MUTED helpers (oracle)";
    const int   want_equal = 1;
#else
    const char* mode = "shipped config (control)";
    const int   want_equal = 0;
#endif
    printf("  %s\n", mode);
    int bad = 0;
    for (unsigned i = 0; i < sizeof(POS) / sizeof(POS[0]); i++) {
        for (int d = 8; d <= 10; d++) {
            uint64_t one = run(i, d, 1);
            uint64_t four = run(i, d, 4);
            int equal = (one == four);
            printf("    %-10s d%-2d  1T %-10llu  4T %-10llu  %s\n",
                   POS[i].name, d, (unsigned long long)one,
                   (unsigned long long)four, equal ? "identical" : "differ");
            if (equal != want_equal) bad++;
        }
    }
    if (want_equal)
        printf("  %s: %d of 6 pairs diverged -- orchestration leaked state\n",
               bad ? "FAIL" : "PASS", bad);
    else
        printf("  %s: %d of 6 pairs were identical -- helpers are NOT feeding\n"
               "        the TT, so the oracle above would prove nothing\n",
               bad ? "FAIL" : "PASS", bad);
    return bad ? 1 : 0;
}
