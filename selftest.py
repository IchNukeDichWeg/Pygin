#!/usr/bin/env python3
"""
selftest.py -- one-command install health check.

    python3 selftest.py            # ~5 seconds, exit 0 = everything OK

Verifies the things that silently go wrong on a fresh clone / new machine:
python-chess present, both C libraries compiled and ABI-matched (a missing
or stale .so would otherwise drop the engine into a ~2x-slower pure-Python
fallback), move generation correct (perft spot check), the search
reproducing the canonical reference position node-for-node, and the timed
path working. Also reports which Old Engine snapshots are ready for A/B
matches (missing snapshot .so files are built by ./setup.sh, not an error).

Exit code 0 = all checks pass, 1 = something failed (chainable:
``python3 selftest.py && python3 match.py ...``).

REF_NODES pins the reference search's exact node count. It is stable across
speed-only versions but changes when the SEARCH intentionally changes --
update it together with any confirmed search-behaviour change (the engine.py
docstring's version history records the current reference).
"""

import importlib.util
import os
import random
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

REF_FEN = "r3k2r/8/8/8/8/8/8/R2QK2R w KQkq - 0 1"
REF_DEPTH = 6
REF_MOVE = "h1h8"
REF_NODES = 3495          # update on confirmed search changes (see docstring)

# The reference node count pins the CONFIRMED (latest vN) search. A default-ON
# search feature that is still under A/B legitimately changes the tree, so a
# strict reference would false-FAIL a routine install check mid-experiment.
# Disable such toggles here so the reference tracks the confirmed baseline.
# When a feature's A/B confirms it into a version: remove it from this tuple
# AND re-measure REF_NODES with it on. (Time-policy toggles never appear here
# -- they don't change fixed-depth node counts.)
BASELINE_OFF = ()   # (none pending; P-42 was here, A/B'd -16.4 and reverted)

_failed = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        _failed.append(label)
    return ok


print("== Pygin selftest ==\n")

# --- 1. Python + dependencies ------------------------------------------ #
print(f"python {sys.version.split()[0]}")
try:
    import chess
    check("python-chess importable", True, f"v{chess.__version__}")
except ImportError:
    check("python-chess importable", False, "pip install -r requirements.txt")
    print("\n== FAILED (cannot continue without python-chess) ==")
    sys.exit(1)

# --- 2. engine import: .so loading + ABI handshake ---------------------- #
# engine.py performs the ABI handshake itself at import; a missing/stale
# eval_c.so leaves _USE_C_EVAL False (loud stderr warning, Python fallback).
import engine  # noqa: E402

check("eval_c.so loaded (C eval active)", engine._USE_C_EVAL,
      "rebuild: python3 eval_build.py" if not engine._USE_C_EVAL else
      f"ABI {engine._eval_lib.abi_version()}")
check("movegen.so loaded (C movegen active)", engine._USE_C_MOVEGEN,
      "rebuild: python3 movegen_build.py" if not engine._USE_C_MOVEGEN else "")

# --- 3. move generation correct (perft spot check via perft.py) --------- #
r = subprocess.run([sys.executable, "perft.py"], capture_output=True, text=True)
check("perft quick suite", r.returncode == 0,
      "run `python3 perft.py` to see the failing position" if r.returncode else "all positions exact")

# --- 4. reference search: node-exact + correct move --------------------- #
random.seed(42)
e = engine.Engine()
e.use_book = False
e.use_tb = False
e.smp_workers = 1
for _tog in BASELINE_OFF:          # pin the reference to the confirmed search
    setattr(e, _tog, False)
mv = e.get_best_move(chess.Board(REF_FEN), REF_DEPTH)
check("reference search move", str(mv) == REF_MOVE, f"{mv} (expected {REF_MOVE})")
check("reference search node-exact", e.nodes_searched == REF_NODES,
      f"{e.nodes_searched:,} nodes (expected {REF_NODES:,}; see REF_NODES note)")

# --- 5. timed path (time_manager + soft-stop machinery) ------------------ #
random.seed(42)
e2 = engine.Engine()
e2.use_book = False
e2.use_tb = False
e2.smp_workers = 1
t0 = time.perf_counter()
mv2 = e2.get_best_move_timed(chess.Board(), 1.0, max_depth=30)
dt = time.perf_counter() - t0
check("timed search returns in budget", mv2 is not None and dt < 2.0,
      f"depth {e2.last_depth} in {dt:.2f}s")

# --- 6. optional pieces: report, don't fail ------------------------------ #
print("\noptional:")
print(f"  {'ok  ' if os.path.exists('wdl_model.json') else 'none'}  wdl_model.json "
      "(match.py adjudication; fit_wdl_model.py writes it)")
for book in ("UHO_4060_v4.epd", "fen.txt"):
    print(f"  {'ok  ' if os.path.exists(book) else 'none'}  {book}")

# --- 7. Old Engine snapshots ready for A/B? ------------------------------ #
snaps_missing = []
if os.path.isdir("Old Engine"):
    for d in sorted(os.listdir("Old Engine"), key=lambda s: (len(s), s)):
        sdir = os.path.join("Old Engine", d)
        if not os.path.isdir(sdir):
            continue
        has_c = os.path.exists(os.path.join(sdir, "eval_c.c"))
        has_so = os.path.exists(os.path.join(sdir, "eval_c.so"))
        if has_c and not has_so:
            snaps_missing.append(d)
    if snaps_missing:
        print(f"  note  Old Engine snapshots without built .so: "
              f"{', '.join(snaps_missing)} -- run ./setup.sh before using them "
              "as A/B baselines (their engines would fall back to slow Python eval)")
    else:
        print("  ok    all Old Engine snapshots with C sources have built .so")

# --- verdict ------------------------------------------------------------- #
if _failed:
    print(f"\n== FAILED: {len(_failed)} check(s): {', '.join(_failed)} ==")
    sys.exit(1)
print("\n== ALL CHECKS PASSED ==")
sys.exit(0)
