#!/usr/bin/env python3
"""Compile movegen.c -> movegen.so (re-run after any edit to movegen.c).

    python3 movegen_build.py       # macOS (uses clang)
    # On Linux replace clang with gcc in the cmd below, then run:
    python3 movegen_build.py       # or: gcc -O2 -shared -fPIC -o movegen.so movegen.c Constants.c

Also links in Constants.c so the magic-bitboard tables (ROOK_ATTACKS,
BISHOP_ATTACKS, INBETWEEN_BITBOARDS, the magic numbers, masks, REL_BITS) are
visible to the slider-attack helpers in movegen.c (#2.1 / #2.2).
"""
import platform, subprocess, sys, os

here = os.path.dirname(os.path.abspath(__file__))
srcs = [os.path.join(here, 'movegen.c'), os.path.join(here, 'Constants.c')]
out = os.path.join(here, 'movegen.so')
# C-03/C-04: -O3 + host-core tuning (see eval_build.py's note).
# V-14d: ARM wants -mcpu=native, x86 wants -march=native.
_mach = platform.machine().lower()
_tune = '-mcpu=native' if _mach.startswith(('arm', 'aarch')) else '-march=native'
cmd = ['clang', '-O3', _tune, '-shared', '-fPIC', '-o', out] + srcs
print(' '.join(cmd))
r = subprocess.run(cmd)
if r.returncode == 0:
    print(f'Built {out}')
else:
    sys.exit(1)
