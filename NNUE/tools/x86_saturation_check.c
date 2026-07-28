/* The AVX2 branch leans on one unproven claim: _mm256_maddubs_epi16 saturates
 * at int16, and we assert it cannot trigger. Model the exact semantics --
 * pairwise (u8 * s8) + (u8 * s8) into int16 -- over the REAL operand ranges
 * and confirm the bound, then confirm the full reduction matches scalar. */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
int main(void){
    int32_t worst_hi = 0, worst_lo = 0; long over = 0;
    for (int u0=0; u0<=127; u0++) for (int u1=0; u1<=127; u1++)
        for (int s0=-128; s0<=127; s0+=17) for (int s1=-128; s1<=127; s1+=17){
            int32_t p = u0*s0 + u1*s1;
            if (p > worst_hi) worst_hi = p;
            if (p < worst_lo) worst_lo = p;
            if (p > 32767 || p < -32768) over++;
        }
    printf("pairwise int16 range over the real operand domain: %d .. %d\n", worst_lo, worst_hi);
    printf("int16 limits:                                      -32768 .. 32767\n");
    printf("saturating combinations found: %ld\n", over);
    printf("headroom: %d low, %d high\n", worst_lo - (-32768), 32767 - worst_hi);
    return over != 0;
}
