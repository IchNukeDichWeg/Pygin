/* Micro-benchmark of the NNUE tail's first layer: 32 output rows x 528 inputs,
 * int8 x int8 -> int32. Standalone; touches nothing in the engine. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
#include <arm_neon.h>

#define IN2  528
#define D2   32
#define ITER 200000

static int8_t W[D2][IN2] __attribute__((aligned(16)));
static int8_t X[IN2]     __attribute__((aligned(16)));

/* v0 -- what nnue.c does today: one accumulator, serial dependency chain */
static inline int32_t dot_v0(const int8_t* w, const int8_t* x, int n){
    int32x4_t acc = vdupq_n_s32(0);
    for (int i = 0; i < n; i += 16)
        acc = vdotq_s32(acc, vld1q_s8(x+i), vld1q_s8(w+i));
    return vaddvq_s32(acc);
}
/* v1 -- four independent accumulators, chain broken */
static inline int32_t dot_v1(const int8_t* w, const int8_t* x, int n){
    int32x4_t a0=vdupq_n_s32(0),a1=a0,a2=a0,a3=a0;
    int i=0;
    for (; i+64<=n; i+=64){
        a0=vdotq_s32(a0, vld1q_s8(x+i),    vld1q_s8(w+i));
        a1=vdotq_s32(a1, vld1q_s8(x+i+16), vld1q_s8(w+i+16));
        a2=vdotq_s32(a2, vld1q_s8(x+i+32), vld1q_s8(w+i+32));
        a3=vdotq_s32(a3, vld1q_s8(x+i+48), vld1q_s8(w+i+48));
    }
    for (; i<n; i+=16) a0=vdotq_s32(a0, vld1q_s8(x+i), vld1q_s8(w+i));
    return vaddvq_s32(vaddq_s32(vaddq_s32(a0,a1),vaddq_s32(a2,a3)));
}
#if defined(__ARM_FEATURE_MATMUL_INT8)
/* v2 -- i8mm: SMMLA computes a 2x2 block. We have only ONE input vector, so
 * the second column is a duplicate and half of every result is discarded. */
static inline void dot_v2_pair(const int8_t* w0, const int8_t* w1,
                               const int8_t* x, int n, int32_t* o0, int32_t* o1){
    int32x4_t acc = vdupq_n_s32(0);
    for (int i = 0; i < n; i += 8){
        int8x16_t a = vcombine_s8(vld1_s8(w0+i), vld1_s8(w1+i));
        int8x8_t  xv = vld1_s8(x+i);
        int8x16_t b = vcombine_s8(xv, xv);
        acc = vmmlaq_s32(acc, a, b);
    }
    *o0 = vgetq_lane_s32(acc,0);
    *o1 = vgetq_lane_s32(acc,2);
}
#endif

/* v3 -- the NO-DEDICATED-DOT fallback: widen to int16, pairwise accumulate.
 * This is the branch nnue.c compiles on any target without a dot-product
 * instruction, and it approximates what an x86 box without VNNI has to do. */
static inline int32_t dot_v3(const int8_t* w, const int8_t* x, int n){
    int32x4_t a0=vdupq_n_s32(0),a1=a0;
    for (int i=0;i<n;i+=16){
        int8x16_t xv=vld1q_s8(x+i), wv=vld1q_s8(w+i);
        a0=vpadalq_s16(a0, vmull_s8(vget_low_s8(xv),  vget_low_s8(wv)));
        a1=vpadalq_s16(a1, vmull_s8(vget_high_s8(xv), vget_high_s8(wv)));
    }
    return vaddvq_s32(vaddq_s32(a0,a1));
}

static double now(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t);
    return t.tv_sec*1e9 + t.tv_nsec; }

int main(void){
    srandom(1);
    for (int r=0;r<D2;r++) for(int i=0;i<IN2;i++) W[r][i]=(int8_t)(random()%255-127);
    for (int i=0;i<IN2;i++) X[i]=(int8_t)(random()%127);
    volatile int32_t sink=0;
    const double macs = (double)D2*IN2;

    double t=now();
    for (int k=0;k<ITER;k++) for(int r=0;r<D2;r++) sink+=dot_v0(W[r],X,IN2);
    double d0=(now()-t)/ITER;

    t=now();
    for (int k=0;k<ITER;k++) for(int r=0;r<D2;r++) sink+=dot_v1(W[r],X,IN2);
    double d1=(now()-t)/ITER;

    printf("layer 32x528 = %.0f MACs\n\n", macs);
    printf("  v0 single-acc SDOT (today)   %7.1f ns   %5.1f MACs/ns\n", d0, macs/d0);
    printf("  v1 4-acc SDOT                %7.1f ns   %5.1f MACs/ns   %+.0f%%\n",
           d1, macs/d1, 100*(d0/d1-1));
#if defined(__ARM_FEATURE_MATMUL_INT8)
    int32_t o0,o1;
    t=now();
    for (int k=0;k<ITER;k++) for(int r=0;r<D2;r+=2){
        dot_v2_pair(W[r],W[r+1],X,IN2,&o0,&o1); sink+=o0+o1; }
    double d2=(now()-t)/ITER;
    printf("  v2 i8mm SMMLA pairs          %7.1f ns   %5.1f MACs/ns   %+.0f%%\n",
           d2, macs/d2, 100*(d0/d2-1));
#else
    printf("  v2 i8mm                       (not compiled in)\n");
#endif
    t=now();
    for (int k=0;k<ITER;k++) for(int r=0;r<D2;r++) sink+=dot_v3(W[r],X,IN2);
    double d3=(now()-t)/ITER;
    printf("  v3 no-dot fallback (x86-ish)  %7.1f ns   %5.1f MACs/ns   %+.0f%%\n",
           d3, macs/d3, 100*(d1/d3-1));
    printf("\n  a box without a dedicated int8 dot instruction pays %.1fx\n", d3/d1);
    (void)sink; return 0;
}
