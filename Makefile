# OpenBench-compatible build -- see docs/OPENBENCH.md for the full setup.
#
#     make EXE=pygin-<branch>-<sha> [CC=clang] [PY=python3]
#
# Produces a single self-contained UCI binary at ./$(EXE): PyInstaller
# onefile bundling cuci.py + the three C .so libraries + Perfect2023.bin
# (the same recipe as ./scripts/build_exe.sh, parameterized for OpenBench's
# `make EXE=... CC=...` convention). The binary supports `./$(EXE) bench`
# (argv, prints "<nodes> nodes <nps> nps") as OpenBench workers require.
#
# Worker prerequisites: a C compiler, python3 with python-chess and
# pyinstaller installed (`pip3 install python-chess pyinstaller`).
#
# NOTE on -m*=native: node-count bench signatures are only comparable
# between IDENTICAL worker CPUs (float rounding in the LMR log table
# drifts a few nodes across microarchitectures -- the same benign drift
# the selftest ladder shows across machines). Run an OpenBench fleet on
# homogeneous workers and measure the registered bench value THERE, not
# on the dev machine.
# `CC ?= clang` would never fire: make predefines CC=cc (gcc on most Linux
# boxes), so a bare `make` silently built with gcc -- ~9% slower search than
# clang on an x86 Linux VM. Only replace make's own default; an explicit
# CC=... on the command line or in the environment still wins.
ifeq ($(origin CC),default)
CC = clang
endif
PY  ?= python3
EXE ?= pygin

ARCH := $(shell uname -m)
ifneq (,$(filter $(ARCH),arm64 aarch64))
TUNE := -mcpu=native
else
TUNE := -march=native
endif
CFLAGS := -O3 $(TUNE) -shared -fPIC -I. -w    # same flags as ./setup.sh

all: $(EXE)

# csearch.c single-TU-includes NNUE/nnue.c (FI-15), hence the dependency.
csearch.so: csearch.c eval_c.c Constants.c Constants.h NNUE/nnue.c
	$(CC) $(CFLAGS) -o $@ csearch.c eval_c.c Constants.c -lm -lpthread

eval_c.so: eval_c.c Constants.c Constants.h
	$(CC) $(CFLAGS) -o $@ eval_c.c Constants.c

movegen.so: movegen.c Constants.c Constants.h
	$(CC) $(CFLAGS) -o $@ movegen.c Constants.c

# The armed net, read from cengine.py exactly as scripts/build_exe.sh does:
# the binary dies at startup without it (USE_NNUE has no HCE fallback).
# Recursive `=` so `make clean` never runs the import.
NET = $(shell $(PY) -c 'import cengine; print(cengine.Engine.NNUE_FILE)')

# lib/: time_manager moved there on 2026-07-24 and cuci.py reaches it with a
# runtime sys.path insert PyInstaller cannot follow (build_exe.sh's --paths).
$(EXE): csearch.so eval_c.so movegen.so cuci.py cengine.py engine.py lib/time_manager.py
	$(PY) -m PyInstaller --onefile --name $(EXE) cuci.py \
	    --paths "$(CURDIR)/lib" --hidden-import time_manager \
	    --add-binary "$(CURDIR)/csearch.so:." \
	    --add-binary "$(CURDIR)/eval_c.so:." \
	    --add-binary "$(CURDIR)/movegen.so:." \
	    --add-data   "$(CURDIR)/data/Perfect2023.bin:." \
	    --add-data   "$(CURDIR)/$(NET):NNUE/nets" \
	    --hidden-import engine --hidden-import chess.polyglot \
	    --exclude-module pygame --exclude-module tkinter --exclude-module numpy \
	    --exclude-module PySide6 --exclude-module matplotlib --exclude-module flask \
	    --distpath . --workpath build --specpath build --log-level ERROR

clean:
	rm -rf build $(EXE)

.PHONY: all clean
