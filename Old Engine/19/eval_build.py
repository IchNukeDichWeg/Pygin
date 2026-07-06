#!/usr/bin/env python3
"""Compile eval_c.c -> eval_c.so (run once; re-run after any edit to eval_c.c).

Also links in Constants.c so the magic-bitboard tables (ROOK_ATTACKS,
BISHOP_ATTACKS, INBETWEEN_BITBOARDS, the magic numbers, masks, REL_BITS) are
visible to the slider-attack helpers in eval_c.c (#2.1 / #2.2).
"""
import subprocess, sys, os

here = os.path.dirname(os.path.abspath(__file__))
srcs = [os.path.join(here, 'eval_c.c'), os.path.join(here, 'Constants.c')]
out = os.path.join(here, 'eval_c.so')
cmd = ['clang', '-O2', '-shared', '-fPIC', '-o', out] + srcs
print(' '.join(cmd))
r = subprocess.run(cmd)
if r.returncode == 0:
    print(f'Built {out}')
else:
    sys.exit(1)
