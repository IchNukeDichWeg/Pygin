#!/usr/bin/env python3
"""Compile eval_c.c -> eval_c.so (run once; re-run after any edit to eval_c.c).

    python3 eval_build.py          # macOS (uses clang)
    # On Linux replace clang with gcc in the cmd below, then run:
    python3 eval_build.py          # or: gcc -O2 -shared -fPIC -o eval_c.so eval_c.c Constants.c

Also links in Constants.c so the magic-bitboard tables (ROOK_ATTACKS,
BISHOP_ATTACKS, INBETWEEN_BITBOARDS, the magic numbers, masks, REL_BITS) are
visible to the slider-attack helpers in eval_c.c (#2.1 / #2.2).
"""
import subprocess, sys, os

here = os.path.dirname(os.path.abspath(__file__))
srcs = [os.path.join(here, 'eval_c.c'), os.path.join(here, 'Constants.c')]
out = os.path.join(here, 'eval_c.so')
# C-03/C-04: -O3 (more aggressive inlining/unrolling of the popcount-heavy
# eval loops) + -mcpu=native (schedule for this exact core; the .so is
# host-local by design). On x86 Linux use -march=native instead.
cmd = ['clang', '-O3', '-mcpu=native', '-shared', '-fPIC', '-o', out] + srcs
print(' '.join(cmd))
r = subprocess.run(cmd)
if r.returncode == 0:
    print(f'Built {out}')
else:
    sys.exit(1)
