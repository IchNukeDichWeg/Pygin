/* NNUE/nnue.c -- FI-15 NNUE evaluation: weight loading, feature extraction,
 * per-thread ply-indexed accumulator stack (F49-31), threat encoding (T16),
 * and the quantized forward pass (NEON + scalar, bit-exact by construction:
 * both paths are pure int16/int32 arithmetic with identical semantics).
 *
 * BUILD MODEL: this file is #included by csearch.c as part of its single
 * translation unit (right before qsearch) -- no build-script change, full
 * cross-inlining, and the Old Engine snapshots (whose csearch.c predates the
 * include) are untouched. It relies on csearch.c definitions that precede
 * the include point: Board, PT_*, MV_*, WHITE/BLACK, KNIGHT_ATT/KING_ATT,
 * PAWN_ATT, bishop_attacks/rook_attacks, board_piece_type_at, cantwin_clamp,
 * CS_MAXPLY, FILE_A/FILE_H.
 *
 * ARCHITECTURE (DESIGN_nnue.md "Phase 1 spec (FROZEN)" is the contract):
 *   feature set KA8T (id 1): per perspective, oriented (rank-flip for
 *   Black) + horizontally mirrored (own king to files a-d) board, 8 king
 *   buckets (QK_MAP), 12 piece planes x 64 squares -> IN = 6144;
 *   plain-768 (id 0) is the SAME code path at KB=1/no-mirror/no-threats.
 *   T16 threat encoding: 8 aggregate int8 scalars per side, recomputed per
 *   eval from one attack-union pass (full attack planes rejected: not
 *   incrementally updatable and ~5k extra accumulate ops per eval).
 *   Net: FT IN->256 x2 persp (int16), tail [512+16]->32->32->1 (int8/int32,
 *   QA=127, QB=64, clamp-then-shift activations, trunc-division output,
 *   cp = out * 400 / 8128).
 *
 * ACCUMULATOR (F49-31): NOT on the copy-make Board -- a per-thread
 * ply-indexed stack g_nn_acc[CS_MAXPLY + 62] mirroring g_path. The negamax
 * and root child sites call nn_push (parent slot ply -> child slot ply+1,
 * one fused write); a null-move child copies the slot (no pieces moved);
 * qsearch never reads NOR writes the stack (Phase-5 hybrid: HCE stand-pat),
 * so the tree's most populous nodes pay nothing. King moves refresh the
 * mover's perspective from the child board (bucket/mirror may change); the
 * opponent perspective updates incrementally (kings are ordinary planes
 * there). Every g_use_nnue==0 path compiles to an untaken branch: toggle
 * off is byte-exact v50 (bench 1,083,772 armed / 1,508,415 ROOT_LMR-off).
 */

#define NN_H_MAX   256          /* compile-time ceiling; file's H <= this  */
#define NN_T_MAX   16
#define NN_D2_MAX  32
#define NN_D3_MAX  32
#define NN_IN_MAX  (8 * 768)
#define NN_QA      127
#define NN_QB      64
#define NN_ACT_MAX (NN_QA * NN_QB)      /* 8128: clamp bound before >> 6 */

static int g_use_nnue = 0;              /* master toggle; 0 = v50 byte-exact */

typedef struct {
    int loaded;
    int feature_set;                    /* 1 = KA8T, 0 = plain768 */
    int in_dim, h, tdim, kb, d2, d3;
    int in2, in2p;                      /* tail input dim, padded to 16 */
    int16_t* w1;                        /* [in_dim][h], row per feature */
    int16_t* b1;                        /* [h] */
    int8_t*  w2;                        /* [d2][in2p] (zero-padded rows) */
    int32_t* b2;                        /* [d2] */
    int8_t*  w3;                        /* [d3][d2p] */
    int32_t* b3;
    int8_t*  w4;                        /* [d3p] */
    int32_t  b4;
    int      d2p, d3p;                  /* padded strides */
} NNUENet;
static NNUENet g_net;                   /* process-wide, like the TT */

typedef struct {
    int16_t v[2][NN_H_MAX];             /* [WHITE/BLACK perspective] */
    uint8_t mirror[2], bucket[2];       /* per-perspective view state */
} NNAccum;
static __thread NNAccum g_nn_acc[CS_MAXPLY + 62];   /* mirrors g_path sizing */

/* verify mode (Phase-4 gate b): every nn_push re-derives the child slot by
 * full refresh and compares -- a mismatch is an accumulator desync. */
static int g_nnue_verify = 0;
static uint64_t g_nnv_pushes = 0, g_nnv_bad = 0;
void set_nnue_verify(int v) { g_nnue_verify = v ? 1 : 0; }
void nnue_verify_stats(uint64_t* pushes, uint64_t* bad)
{
    *pushes = g_nnv_pushes; *bad = g_nnv_bad;
}

/* ---------------- feature indexing (the KA8T contract) ------------------- */
/* QK_MAP[rank][file>>1] over the oriented+mirrored own-king square (file
 * a-d after the mirror). Castled-short king (g1 -> b1) = bucket 0. */
static const uint8_t NN_QK_MAP[8][2] = {
    {0, 1}, {2, 3}, {4, 5}, {6, 6}, {7, 7}, {7, 7}, {7, 7}, {7, 7}
};

static inline int nn_osq(int persp, int mirror, int sq)
{
    if (persp == BLACK) sq ^= 56;       /* rank flip */
    if (mirror) sq ^= 7;                /* file mirror */
    return sq;
}

/* plane 0..5 = perspective's own P..K, 6..11 = opponent's P..K */
static inline int nn_plane(int persp, int color, int pt)
{
    return (color == persp ? 0 : 6) + pt - 1;
}

static inline const int16_t* nn_col(int bucket, int plane, int osq)
{
    return g_net.w1
         + ((size_t)bucket * 768 + (size_t)plane * 64 + (size_t)osq)
           * (size_t)g_net.h;
}

/* view state (mirror flag + king bucket) of perspective P on board b.
 * Kingless side (test positions only): bucket 0, no mirror. */
static inline void nn_view(const Board* b, int persp, int* mirror, int* bucket)
{
    uint64_t kbb = b->kings & b->occ[persp];
    if (!kbb || g_net.feature_set == 0) { *mirror = 0; *bucket = 0; return; }
    int ksq = __builtin_ctzll(kbb);
    if (persp == BLACK) ksq ^= 56;
    *mirror = (ksq & 7) >= 4;
    if (*mirror) ksq ^= 7;
    *bucket = NN_QK_MAP[ksq >> 3][(ksq & 7) >> 1];
}

/* -------------- T16 threat encoding (shared C truth) --------------------- */
/* One attack-union pass per side; 8 scalars per side, already in activation
 * units (int8 0..127 == float 0..1). Formulas are frozen in DESIGN_nnue.md;
 * a change bumps the dataset header's threat_ver and forces regeneration. */
static void nn_attack_union(const Board* b, int side,
                            uint64_t* out_all, uint64_t* out_pawn)
{
    uint64_t occ = b->occ[0] | b->occ[1], own = b->occ[side];
    uint64_t p = b->pawns & own;
    uint64_t pa = (side == WHITE)
        ? (((p << 9) & ~FILE_A) | ((p << 7) & ~FILE_H))
        : (((p >> 7) & ~FILE_A) | ((p >> 9) & ~FILE_H));
    uint64_t all = pa;
    for (uint64_t t = b->knights & own; t; t &= t - 1)
        all |= KNIGHT_ATT[__builtin_ctzll(t)];
    for (uint64_t t = (b->bishops | b->queens) & own; t; t &= t - 1)
        all |= bishop_attacks(__builtin_ctzll(t), occ);
    for (uint64_t t = (b->rooks | b->queens) & own; t; t &= t - 1)
        all |= rook_attacks(__builtin_ctzll(t), occ);
    uint64_t k = b->kings & own;
    if (k) all |= KING_ATT[__builtin_ctzll(k)];
    *out_all = all; *out_pawn = pa;
}

#define NN_CENTER4 0x0000001818000000ULL          /* d4 e4 d5 e5 */
static inline uint8_t nn_t8(int v) { return (uint8_t)(v > 127 ? 127 : v); }

static void nn_threat_vec(const Board* b, uint8_t out_w[8], uint8_t out_b[8])
{
    uint64_t aw, pw, ab, pb;
    nn_attack_union(b, WHITE, &aw, &pw);
    nn_attack_union(b, BLACK, &ab, &pb);
    uint64_t occ_w = b->occ[WHITE], occ_b = b->occ[BLACK];
    uint64_t kw = b->kings & occ_w, kb = b->kings & occ_b;
    uint64_t ring_w = kw ? KING_ATT[__builtin_ctzll(kw)] : 0;
    uint64_t ring_b = kb ? KING_ATT[__builtin_ctzll(kb)] : 0;
    uint64_t mm_w = (b->knights | b->bishops | b->rooks | b->queens) & occ_w;
    uint64_t mm_b = (b->knights | b->bishops | b->rooks | b->queens) & occ_b;
    for (int s = 0; s < 2; s++) {
        uint8_t* o     = s ? out_w : out_b;     /* s=1 -> WHITE vec */
        uint64_t a_s   = s ? aw : ab,   a_o  = s ? ab : aw;
        uint64_t pa_s  = s ? pw : pb,   pa_o = s ? pb : pw;
        uint64_t own   = s ? occ_w : occ_b, opp = s ? occ_b : occ_w;
        uint64_t mm_s  = s ? mm_w : mm_b;
        uint64_t ring_o = s ? ring_b : ring_w;
        o[0] = nn_t8(2  * __builtin_popcountll(a_s));
        o[1] = nn_t8(16 * __builtin_popcountll(a_s & ring_o));
        o[2] = nn_t8(32 * __builtin_popcountll(mm_s & pa_o));
        o[3] = nn_t8(32 * __builtin_popcountll(own & a_o & ~a_s));
        o[4] = nn_t8(32 * __builtin_popcountll(opp & a_s & ~a_o));
        o[5] = nn_t8(16 * __builtin_popcountll(own & a_o));
        o[6] = nn_t8(16 * __builtin_popcountll(own & ~b->pawns & pa_s));
        o[7] = nn_t8(32 * __builtin_popcountll(a_s & NN_CENTER4));
    }
}

/* ------------------- accumulator refresh + push -------------------------- */
static void nn_acc_refresh_persp(const Board* b, NNAccum* a, int persp)
{
    int mirror, bucket;
    nn_view(b, persp, &mirror, &bucket);
    a->mirror[persp] = (uint8_t)mirror;
    a->bucket[persp] = (uint8_t)bucket;
    int16_t* acc = a->v[persp];
    const int h = g_net.h;
    memcpy(acc, g_net.b1, (size_t)h * sizeof(int16_t));
    const uint64_t* bbs[7] = {0, &b->pawns, &b->knights, &b->bishops,
                              &b->rooks, &b->queens, &b->kings};
    for (int c = 0; c < 2; c++)
        for (int pt = 1; pt <= 6; pt++) {
            int plane = nn_plane(persp, c, pt);
            for (uint64_t t = *bbs[pt] & b->occ[c]; t; t &= t - 1) {
                const int16_t* col = nn_col(bucket, plane,
                                            nn_osq(persp, mirror,
                                                   __builtin_ctzll(t)));
                for (int i = 0; i < h; i++) acc[i] += col[i];
            }
        }
}

static void nn_refresh(const Board* b, int ply)
{
    nn_acc_refresh_persp(b, &g_nn_acc[ply], WHITE);
    nn_acc_refresh_persp(b, &g_nn_acc[ply], BLACK);
}

/* fused copy+delta writes (the F49-31 point: one streaming write per child,
 * never a separate memcpy). Plain C -- clang auto-vectorizes these int16
 * loops; exactness is trivial (wrap-free by the loader's overflow bound). */
static void nn_apply2(int16_t* restrict d, const int16_t* restrict s,
                      const int16_t* restrict a1, const int16_t* restrict s1,
                      int h)
{
    for (int i = 0; i < h; i++) d[i] = (int16_t)(s[i] + a1[i] - s1[i]);
}
static void nn_apply3(int16_t* restrict d, const int16_t* restrict s,
                      const int16_t* restrict a1, const int16_t* restrict s1,
                      const int16_t* restrict s2, int h)
{
    for (int i = 0; i < h; i++)
        d[i] = (int16_t)(s[i] + a1[i] - s1[i] - s2[i]);
}
static void nn_apply4(int16_t* restrict d, const int16_t* restrict s,
                      const int16_t* restrict a1, const int16_t* restrict s1,
                      const int16_t* restrict a2, const int16_t* restrict s2,
                      int h)
{
    for (int i = 0; i < h; i++)
        d[i] = (int16_t)(s[i] + a1[i] - s1[i] + a2[i] - s2[i]);
}

/* parent slot ply + move word -> child slot ply+1. `parent`/`child` are the
 * boards before/after apply_move(m). Mover/victim PTs ride in the packed
 * move word (FI-02.2); ep and castling are derived exactly like apply_move.
 * King move => full refresh of the mover's perspective (bucket/mirror). */
static void nn_push(int ply, const Board* parent, const Board* child,
                    uint32_t m)
{
    const int from = m & 63, to = (m >> 6) & 63, promo = (m >> 12) & 7;
    const int mover = (m >> MV_SHIFT_MOVER) & 7;
    const int victim = (m >> MV_SHIFT_VICTIM) & 7;
    const int us = parent->turn, them = us ^ 1;
    const int finalpt = promo ? promo : mover;
    const int h = g_net.h;
    int capsq = to;
    if (m & MV_BIT_EP) capsq = (us == WHITE) ? to - 8 : to + 8;
    int castle = (mover == PT_KING) && (to - from == 2 || from - to == 2);
    int rf = 0, rt = 0;
    if (castle) {
        if (to > from) { rf = (us == WHITE) ? 7 : 63; rt = (us == WHITE) ? 5 : 61; }
        else           { rf = (us == WHITE) ? 0 : 56; rt = (us == WHITE) ? 3 : 59; }
    }
    NNAccum* pa = &g_nn_acc[ply];
    NNAccum* ca = &g_nn_acc[ply + 1];
    for (int persp = 0; persp < 2; persp++) {
        if (mover == PT_KING && persp == us) {
            /* own-king move: bucket/mirror can change -> full refresh */
            nn_acc_refresh_persp(child, ca, persp);
            continue;
        }
        int mir = pa->mirror[persp], bkt = pa->bucket[persp];
        ca->mirror[persp] = (uint8_t)mir;
        ca->bucket[persp] = (uint8_t)bkt;
        const int16_t* s1 = nn_col(bkt, nn_plane(persp, us, mover),
                                   nn_osq(persp, mir, from));
        const int16_t* a1 = nn_col(bkt, nn_plane(persp, us, finalpt),
                                   nn_osq(persp, mir, to));
        if (castle) {
            const int16_t* s2 = nn_col(bkt, nn_plane(persp, us, PT_ROOK),
                                       nn_osq(persp, mir, rf));
            const int16_t* a2 = nn_col(bkt, nn_plane(persp, us, PT_ROOK),
                                       nn_osq(persp, mir, rt));
            nn_apply4(ca->v[persp], pa->v[persp], a1, s1, a2, s2, h);
        } else if (victim) {
            const int16_t* s2 = nn_col(bkt, nn_plane(persp, them, victim),
                                       nn_osq(persp, mir, capsq));
            nn_apply3(ca->v[persp], pa->v[persp], a1, s1, s2, h);
        } else {
            nn_apply2(ca->v[persp], pa->v[persp], a1, s1, h);
        }
    }
    if (g_nnue_verify) {                 /* gate (b): incremental vs scratch */
        NNAccum ref;
        nn_acc_refresh_persp(child, &ref, WHITE);
        nn_acc_refresh_persp(child, &ref, BLACK);
        g_nnv_pushes++;
        if (memcmp(ref.v[0], ca->v[0], (size_t)h * sizeof(int16_t))
            || memcmp(ref.v[1], ca->v[1], (size_t)h * sizeof(int16_t))
            || ref.mirror[0] != ca->mirror[0] || ref.mirror[1] != ca->mirror[1]
            || ref.bucket[0] != ca->bucket[0] || ref.bucket[1] != ca->bucket[1])
            g_nnv_bad++;
    }
}

/* --------------------------- forward pass -------------------------------- */
/* int8 dot over a padded row (pads are zero on both sides). NEON and scalar
 * are both exact integer pipelines -- bit-identical by construction; the
 * Phase-4 gate additionally proves it empirically vs the numpy reference. */
#if defined(__ARM_NEON)
#include <arm_neon.h>
static inline int32_t nn_dot_row(const int8_t* w, const int8_t* x, int n)
{
    int32x4_t acc = vdupq_n_s32(0);
    for (int i = 0; i < n; i += 16) {
        int8x16_t xv = vld1q_s8(x + i);
        int8x16_t wv = vld1q_s8(w + i);
#if defined(__ARM_FEATURE_DOTPROD)
        acc = vdotq_s32(acc, xv, wv);
#else
        int16x8_t lo = vmull_s8(vget_low_s8(xv), vget_low_s8(wv));
        int16x8_t hi = vmull_s8(vget_high_s8(xv), vget_high_s8(wv));
        acc = vpadalq_s16(acc, lo);
        acc = vpadalq_s16(acc, hi);
#endif
    }
    return vaddvq_s32(acc);
}
#else
static inline int32_t nn_dot_row(const int8_t* w, const int8_t* x, int n)
{
    int32_t s = 0;
    for (int i = 0; i < n; i++) s += (int32_t)w[i] * (int32_t)x[i];
    return s;
}
#endif

/* clamp-then-shift activation: a' = min(max(v,0), 8128) >> 6 (exact, and
 * identical in the numpy reference -- see DESIGN_nnue.md quantization). */
static inline int8_t nn_act(int32_t v)
{
    if (v < 0) v = 0;
    if (v > NN_ACT_MAX) v = NN_ACT_MAX;
    return (int8_t)(v >> 6);
}

/* raw quantized net output for the side to move, in centipawns -- NO
 * post-network shaping (cantwin is applied by nn_eval, matching how
 * shaping wraps eval_white today; F5-19 keeps it outside the net). */
static int nn_forward(const Board* b, const NNAccum* a)
{
    const int h = g_net.h, tdim = g_net.tdim;
    const int us = b->turn, them = us ^ 1;
    int8_t x[2 * NN_H_MAX + NN_T_MAX + 16] __attribute__((aligned(16)));
    const int16_t* au = a->v[us];
    const int16_t* at = a->v[them];
    for (int i = 0; i < h; i++) {
        int v = au[i];
        x[i] = (int8_t)(v < 0 ? 0 : (v > NN_QA ? NN_QA : v));
        v = at[i];
        x[h + i] = (int8_t)(v < 0 ? 0 : (v > NN_QA ? NN_QA : v));
    }
    if (tdim) {
        uint8_t tw[8], tb[8];
        nn_threat_vec(b, tw, tb);
        const uint8_t* tus = (us == WHITE) ? tw : tb;
        const uint8_t* tth = (us == WHITE) ? tb : tw;
        for (int i = 0; i < 8; i++) {
            x[2 * h + i] = (int8_t)tus[i];
            x[2 * h + 8 + i] = (int8_t)tth[i];
        }
    }
    for (int i = 2 * h + tdim; i < g_net.in2p; i++) x[i] = 0;   /* pads */

    int8_t h1[NN_D2_MAX + 16] __attribute__((aligned(16)));
    for (int j = 0; j < g_net.d2; j++)
        h1[j] = nn_act(g_net.b2[j]
                       + nn_dot_row(g_net.w2 + (size_t)j * g_net.in2p, x,
                                    g_net.in2p));
    for (int j = g_net.d2; j < g_net.d2p; j++) h1[j] = 0;

    int8_t h2[NN_D3_MAX + 16] __attribute__((aligned(16)));
    for (int j = 0; j < g_net.d3; j++)
        h2[j] = nn_act(g_net.b3[j]
                       + nn_dot_row(g_net.w3 + (size_t)j * g_net.d2p, h1,
                                    g_net.d2p));
    for (int j = g_net.d3; j < g_net.d3p; j++) h2[j] = 0;

    int32_t out = g_net.b4 + nn_dot_row(g_net.w4, h2, g_net.d3p);
    return (int)((int64_t)out * 400 / NN_ACT_MAX);   /* trunc division */
}

/* the engine's NN static eval: stm-relative, with post-network shaping
 * (cantwin_clamp on the White-POV value, exactly as it wraps eval_white;
 * mop-up/simplify are HCE-internal terms and do NOT apply to the net). */
static int nn_eval(const Board* b, int ply)
{
    int v = nn_forward(b, &g_nn_acc[ply]);
    int w = (b->turn == WHITE) ? v : -v;
    w = cantwin_clamp(b, w);
    return (b->turn == WHITE) ? w : -w;
}

/* ----------------------------- loader ------------------------------------ */
static uint32_t nn_crc32(const uint8_t* p, size_t n)
{
    static uint32_t tab[256];
    static int ready = 0;
    if (!ready) {
        for (uint32_t i = 0; i < 256; i++) {
            uint32_t c = i;
            for (int k = 0; k < 8; k++)
                c = (c & 1) ? 0xEDB88320u ^ (c >> 1) : (c >> 1);
            tab[i] = c;
        }
        ready = 1;
    }
    uint32_t c = 0xFFFFFFFFu;
    for (size_t i = 0; i < n; i++) c = tab[(c ^ p[i]) & 0xFF] ^ (c >> 8);
    return c ^ 0xFFFFFFFFu;
}

static uint32_t nn_rd32(const uint8_t* p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8)
         | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static uint64_t nn_rd64(const uint8_t* p) {
    return (uint64_t)nn_rd32(p) | ((uint64_t)nn_rd32(p + 4) << 32);
}

/* Load a .nnue file (format v1, DESIGN_nnue.md). Returns 0 on success;
 * nonzero error codes are distinct for the host's error message. Loading is
 * a config event like set_tt_bits: never call during a search. */
int nnue_load(const char* path)
{
    FILE* f = fopen(path, "rb");
    if (!f) return 1;
    uint8_t hdr[64];
    if (fread(hdr, 1, 64, f) != 64) { fclose(f); return 2; }
    if (memcmp(hdr, "PYGINNUE", 8) != 0) { fclose(f); return 3; }
    if (nn_rd32(hdr + 8) != 1) { fclose(f); return 4; }      /* version */
    NNUENet n;
    memset(&n, 0, sizeof(n));
    n.feature_set = (int)nn_rd32(hdr + 12);
    n.in_dim = (int)nn_rd32(hdr + 16);
    n.h      = (int)nn_rd32(hdr + 20);
    n.tdim   = (int)nn_rd32(hdr + 24);
    n.kb     = (int)nn_rd32(hdr + 28);
    n.d2     = (int)nn_rd32(hdr + 32);
    n.d3     = (int)nn_rd32(hdr + 36);
    uint32_t qa = nn_rd32(hdr + 40), qb = nn_rd32(hdr + 44),
             ocp = nn_rd32(hdr + 48);
    uint32_t crc = nn_rd32(hdr + 52);
    uint64_t psz = nn_rd64(hdr + 56);
    if ((n.feature_set != 0 && n.feature_set != 1)
        || n.h < 8 || n.h > NN_H_MAX
        || n.tdim < 0 || n.tdim > NN_T_MAX
        || n.d2 < 1 || n.d2 > NN_D2_MAX || n.d3 < 1 || n.d3 > NN_D3_MAX
        || n.kb < 1 || n.kb > 8
        || n.in_dim != n.kb * 768
        || (n.feature_set == 0 && (n.kb != 1 || n.tdim != 0))
        || (n.feature_set == 1 && (n.kb != 8 || n.tdim != 16))
        || qa != NN_QA || qb != NN_QB || ocp != 400) {
        fclose(f); return 5;
    }
    n.in2 = 2 * n.h + n.tdim;
    n.in2p = (n.in2 + 15) & ~15;
    n.d2p = (n.d2 + 15) & ~15;
    n.d3p = (n.d3 + 15) & ~15;
    size_t sz_w1 = (size_t)n.in_dim * n.h * 2, sz_b1 = (size_t)n.h * 2;
    size_t sz_w2 = (size_t)n.d2 * n.in2, sz_b2 = (size_t)n.d2 * 4;
    size_t sz_w3 = (size_t)n.d3 * n.d2, sz_b3 = (size_t)n.d3 * 4;
    size_t sz_w4 = (size_t)n.d3, sz_b4 = 4;
    size_t want = sz_w1 + sz_b1 + sz_w2 + sz_b2 + sz_w3 + sz_b3 + sz_w4 + sz_b4;
    if (psz != want) { fclose(f); return 6; }
    uint8_t* buf = (uint8_t*)malloc(want);
    if (!buf) { fclose(f); return 7; }
    if (fread(buf, 1, want, f) != want) { free(buf); fclose(f); return 8; }
    fclose(f);
    if (nn_crc32(buf, want) != crc) { free(buf); return 9; }

    n.w1 = (int16_t*)malloc(sz_w1);
    n.b1 = (int16_t*)malloc(sz_b1);
    n.w2 = (int8_t*)calloc((size_t)n.d2 * n.in2p, 1);
    n.b2 = (int32_t*)malloc(sz_b2);
    n.w3 = (int8_t*)calloc((size_t)n.d3 * n.d2p, 1);
    n.b3 = (int32_t*)malloc(sz_b3);
    n.w4 = (int8_t*)calloc((size_t)n.d3p, 1);
    if (!n.w1 || !n.b1 || !n.w2 || !n.b2 || !n.w3 || !n.b3 || !n.w4) {
        free(buf); free(n.w1); free(n.b1); free(n.w2); free(n.b2);
        free(n.w3); free(n.b3); free(n.w4);
        return 7;
    }
    uint8_t* p = buf;
    memcpy(n.w1, p, sz_w1); p += sz_w1;
    memcpy(n.b1, p, sz_b1); p += sz_b1;
    for (int j = 0; j < n.d2; j++) {                 /* zero-padded rows */
        memcpy(n.w2 + (size_t)j * n.in2p, p, (size_t)n.in2); p += n.in2;
    }
    memcpy(n.b2, p, sz_b2); p += sz_b2;
    for (int j = 0; j < n.d3; j++) {
        memcpy(n.w3 + (size_t)j * n.d2p, p, (size_t)n.d2); p += n.d2;
    }
    memcpy(n.b3, p, sz_b3); p += sz_b3;
    memcpy(n.w4, p, sz_w4); p += sz_w4;
    memcpy(&n.b4, p, 4);
    free(buf);

    /* int16-accumulator overflow bound: 32 active features + bias must
     * never wrap (the trainer's weight clips guarantee it; verify). */
    int32_t maxw = 0, maxb = 0;
    for (size_t i = 0; i < (size_t)n.in_dim * n.h; i++) {
        int32_t v = n.w1[i] < 0 ? -n.w1[i] : n.w1[i];
        if (v > maxw) maxw = v;
    }
    for (int i = 0; i < n.h; i++) {
        int32_t v = n.b1[i] < 0 ? -n.b1[i] : n.b1[i];
        if (v > maxb) maxb = v;
    }
    if (33 * maxw + maxb > 32767) {
        free(n.w1); free(n.b1); free(n.w2); free(n.b2);
        free(n.w3); free(n.b3); free(n.w4);
        return 10;
    }

    /* swap in (free any previous net) */
    free(g_net.w1); free(g_net.b1); free(g_net.w2); free(g_net.b2);
    free(g_net.w3); free(g_net.b3); free(g_net.w4);
    n.loaded = 1;
    g_net = n;
    return 0;
}

int nnue_ready(void) { return g_net.loaded; }

/* master toggle. Refuses to arm without a loaded net (nn_eval would deref
 * NULL); cengine checks nnue_load's rc first and raises loudly, so the
 * silent-refuse path is belt-and-braces only. */
void set_use_nnue(int v) { g_use_nnue = (v && g_net.loaded) ? 1 : 0; }
int get_use_nnue(void) { return g_use_nnue; }

/* ------------------- oracles for the Python harnesses -------------------- */
/* Raw stm-POV net output (full refresh, no shaping) -- the Phase-4 gate (a)
 * compares this against NNUE/nnue_ref.py's quantized forward, EXACT. */
int nnue_eval_oracle(uint64_t pawns, uint64_t knights, uint64_t bishops,
                     uint64_t rooks, uint64_t queens, uint64_t kings,
                     uint64_t occ_w, uint64_t occ_b,
                     int turn, int ep, uint64_t castling)
{
    if (!g_net.loaded) return -32768;
    Board b = make_board(pawns, knights, bishops, rooks, queens, kings,
                         occ_w, occ_b, turn, ep, castling);
    NNAccum a;
    nn_acc_refresh_persp(&b, &a, WHITE);
    nn_acc_refresh_persp(&b, &a, BLACK);
    return nn_forward(&b, &a);
}

/* T16 bytes for the data pipeline (White vec then Black vec, 16 bytes) --
 * the SAME function the engine's forward pass calls, so trainer and
 * inference consume byte-identical threat inputs by construction. */
void nnue_threats(uint64_t pawns, uint64_t knights, uint64_t bishops,
                  uint64_t rooks, uint64_t queens, uint64_t kings,
                  uint64_t occ_w, uint64_t occ_b,
                  int turn, int ep, uint64_t castling, uint8_t* out16)
{
    Board b = make_board(pawns, knights, bishops, rooks, queens, kings,
                         occ_w, occ_b, turn, ep, castling);
    nn_threat_vec(&b, out16, out16 + 8);
}

/* Active feature indices of one perspective (debug/parity aid): fills
 * out[] (<= 32) and returns the count. Requires a loaded net (bucketing
 * depends on the feature set). */
int nnue_features_oracle(uint64_t pawns, uint64_t knights, uint64_t bishops,
                         uint64_t rooks, uint64_t queens, uint64_t kings,
                         uint64_t occ_w, uint64_t occ_b,
                         int turn, int ep, uint64_t castling,
                         int persp, int* out)
{
    if (!g_net.loaded) return -1;
    Board b = make_board(pawns, knights, bishops, rooks, queens, kings,
                         occ_w, occ_b, turn, ep, castling);
    int mirror, bucket, cnt = 0;
    nn_view(&b, persp, &mirror, &bucket);
    const uint64_t* bbs[7] = {0, &b.pawns, &b.knights, &b.bishops,
                              &b.rooks, &b.queens, &b.kings};
    for (int c = 0; c < 2; c++)
        for (int pt = 1; pt <= 6; pt++)
            for (uint64_t t = *bbs[pt] & b.occ[c]; t; t &= t - 1)
                out[cnt++] = bucket * 768 + nn_plane(persp, c, pt) * 64
                           + nn_osq(persp, mirror, __builtin_ctzll(t));
    return cnt;
}
