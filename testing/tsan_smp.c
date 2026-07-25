/* FB-12: ThreadSanitizer driver for the Lazy-SMP path.
 *
 *     cc -fsanitize=thread -O1 -g -I. -o /tmp/tsan_smp testing/tsan_smp.c \
 *        Constants.c -lm -lpthread && /tmp/tsan_smp
 *
 * TSan needs the WHOLE binary instrumented, and the engine normally lives in
 * a .so loaded by an uninstrumented Python -- so this driver includes
 * csearch.c directly (it is already a single translation unit that pulls in
 * eval_c.c and NNUE/nnue.c) and drives the root search at Threads=4 from C.
 *
 * It is a RACE detector, not a correctness test: the ladder and the bench
 * signature cover behaviour. What matters here is the report count.
 *
 * TEETH, verified 2026-07-25 -- "0 reports" means nothing unless the harness
 * can see a race. Reverting ONE store in tt_store_raw from an atomic to a
 * plain `t->d1 = d1;` makes this driver report 160 data races. With FB-12's
 * atomics in place it reports none over ~1.1M nodes at Threads=4.
 */
#include "csearch.c"

/* Startpos, then a middlegame with real tactics so helpers actually diverge. */
static const struct { const char* name; uint64_t p, n, b, r, q, k, w, bl;
                      int turn, ep; uint64_t cr; } POS[] = {
    /* startpos */
    {"startpos",
     0x00FF00000000FF00ULL, 0x4200000000000042ULL, 0x2400000000000024ULL,
     0x8100000000000081ULL, 0x0800000000000008ULL, 0x1000000000000010ULL,
     0x000000000000FFFFULL, 0xFFFF000000000000ULL, 1, -1,
     0x8100000000000081ULL},
    /* r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - : the
     * nps13 position -- open lines, real captures, so qsearch actually runs */
    {"middlegame",
     0x00EF00101000EF00ULL, 0x4000040000200002ULL, 0x2400000200000004ULL,
     0x8100000000000081ULL, 0x0800000000000008ULL, 0x1000000000000010ULL,
     0x000000021020EF9FULL, 0xFDEF041000000000ULL, 1, -1,
     0x8100000000000081ULL},
};

/* Real-ish eval params. Without these every score is 0, qsearch stand-pats
 * immediately and the qsearch TT store -- one of the three sites FB-12
 * converted -- is never exercised. Material + a crude centre bonus is enough
 * to make the tree diverge and the helpers disagree. */
static void install_eval(void)
{
    static int mg_pst[6 * 64], eg_pst[6 * 64];
    static const int val[7]   = {0, 100, 320, 330, 500, 900, 0};
    static const int phase[7] = {0, 0, 1, 1, 2, 4, 0};
    static const int passed_mg[8] = {0, 5, 10, 20, 35, 60, 100, 0};
    static const int passed_eg[8] = {0, 10, 20, 35, 60, 100, 150, 0};
    for (int pt = 0; pt < 6; pt++)
        for (int sq = 0; sq < 64; sq++) {
            int f = sq & 7, r = sq >> 3;
            int centre = (3 - (f < 4 ? 3 - f : f - 4))
                       + (3 - (r < 4 ? 3 - r : r - 4));
            mg_pst[pt * 64 + sq] = centre * 2;
            eg_pst[pt * 64 + sq] = centre;
        }
    csearch_set_eval(mg_pst, eg_pst, val, val, phase,
                     10, -12, -15, -8, passed_mg, passed_eg, 500, 8, 10);
}

int main(void)
{
    /* A TT big enough to force real sharing between the threads. */
    install_eval();
    set_tt_bits(20);
    set_threads(4);

    for (int rep = 0; rep < 2; rep++) {
        for (unsigned i = 0; i < sizeof(POS) / sizeof(POS[0]); i++) {
            cs_search_begin(NULL, 0, 0.0);          /* no deadline */
            uint64_t nodes = 0;
            int score = 0, done = 0, aborted = 0, second = 0;
            uint32_t mv = cs_search_root(
                POS[i].p, POS[i].n, POS[i].b, POS[i].r, POS[i].q, POS[i].k,
                POS[i].w, POS[i].bl, POS[i].turn, POS[i].ep, POS[i].cr,
                /* depth */ 9, -CS_INF, CS_INF,
                /* prev_key */ 0, /* hmc */ 0,
                &nodes, &score, &done, &aborted, &second);
            printf("  %-10s rep %d: move %u  score %d  nodes %llu  "
                   "done %d aborted %d\n",
                   POS[i].name, rep, mv & 0x7FFF, score,
                   (unsigned long long)nodes, done, aborted);
            fflush(stdout);
        }
        /* Exercise the stop path too: it writes the shared abort flag while
         * helpers are polling it, which is the other race the flags carry. */
        cs_stop();
    }
    printf("driver done -- any TSan report above is a real finding\n");
    return 0;
}
