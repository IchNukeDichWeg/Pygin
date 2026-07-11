#!/usr/bin/env python3
"""
selftest.py -- one-command install health check.

    python3 selftest.py            # ~5 seconds, exit 0 = everything OK

Verifies the things that silently go wrong on a fresh clone / new machine:
python-chess present, both C libraries compiled and ABI-matched (a missing
or stale .so would otherwise drop the engine into a ~2x-slower pure-Python
fallback), move generation correct (perft spot check), the Python search
reproducing the canonical reference position node-for-node, the timed path
working, and the C search core (cengine) running a fixed-depth ladder to
depth 12 with pinned per-depth node+score values plus a 2s throughput
(NPS) probe. Also reports which Old Engine snapshots are ready for A/B
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

# --- 5b. C search core: fixed-depth ladder to depth 12 ------------------- #
# The whole cengine chain (csearch.so + ABI + eval params synced from
# engine.py) exercised as a real search, one iterative-deepening run per
# depth from a quiet middlegame position. The TT is reset COLD before each
# depth so the fixed-depth node count is reproducible (the process-global C
# TT is kept warm in normal play, which makes counts history-dependent).
#
# CE_LADDER pins (nodes, score) per depth for the CONFIRMED C search. Both
# are deterministic (integer eval, single thread, no root randomness) and
# machine-independent. Re-measure the whole table on any confirmed
# C-SEARCH change (same contract as REF_NODES) -- print it and paste back.
# The best move is printed and legality-checked but NOT pinned: near-equal
# quiet developing moves flip between depths without being a regression.
# Skipped (not failed) if csearch.c is absent (pre-phase-3 checkouts).
CE_LADDER_FEN = "r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 3 3"
CE_LADDER = {                     # depth -> (nodes, score)  [v40: EP-01 ep hashing]
    1: (102, 126), 2: (189, 126), 3: (749, 126), 4: (1020, 122),
    5: (6155, 73), 6: (10554, 68), 7: (30388, 74), 8: (73481, 57),
    9: (129136, 67), 10: (217588, 53), 11: (359628, 53), 12: (562363, 69),
}
if os.path.exists("csearch.c"):
    try:
        import cengine  # noqa: E402
        # Outpost: NULL, OFF (A/B vs v37 2026-07-10: -0.90 +/-6.8) -- the
        # default already reproduces v37; belt-and-braces pin.
        cengine.Engine.USE_OUTPOST = False
        # CB-01 correctness batch: CONFIRMED into v38 (+1.36 null KEPT as
        # correctness), default ON -- part of the pinned reference search.
        ce = cengine.Engine()
        ce.use_book = False
        ce.use_tb = False
        ce.smp_workers = 1
        # P-43 single-reply extension is DORMANT (default OFF after the 20k
        # A/B read kept-marginal +3.5); the default already reproduces v34,
        # so this pin is belt-and-braces against a future default flip.
        try:
            ce._lib.set_single_reply(0)
        except AttributeError:
            pass                       # pre-P-43 csearch.so: no such toggle
        # P-04 improving-flag is DORMANT (default OFF after a dead-null 10k
        # A/B: +0.38); the default already reproduces v34, so this pin is
        # belt-and-braces against a future default flip.
        try:
            ce._lib.set_improving(0)
        except AttributeError:
            pass                       # pre-P-04 csearch.so: no such toggle
        # P-44 qsearch TT probe: CONFIRMED into v35 (+8.06 isolation A/B),
        # default ON -- part of the pinned reference search above.
        # P-23 staged ordering: CONFIRMED into v36 (+24.67 A/B vs v35),
        # default ON -- part of the pinned reference search above.
        # Q-01 continuation history is DORMANT (default OFF after a dead-null
        # 10k A/B vs v36: -0.87 +/-6.8, the first 50+0.20-era campaign); the
        # default already reproduces v36, so this pin is belt-and-braces
        # against a future default flip.
        try:
            ce._lib.set_cont_hist(0)
        except AttributeError:
            pass                       # pre-Q-01 csearch.so: no such toggle
        # EP-01 FIDE-exact ep hashing: CONFIRMED into v40 (+4.31 null KEPT as
        # correctness -- repetition detection now matches the FIDE arbiter),
        # default ON -- part of the pinned reference search above.
        # P-47 check-ext budget: raise-to-8 REJECTED (-4.59 +/-6.8 @10k);
        # 5 is the confirmed recipe and the default -- belt-and-braces pin.
        # PV-02 exact PV: CONFIRMED into v37 (+0.17 null = free correctness),
        # default ON -- part of the pinned reference search above.
        try:
            ce._lib.set_check_ext_budget(5)
        except AttributeError:
            pass                       # pre-P-47 csearch.so
        print("\nC core ladder (cold TT per depth):")
        ok_all, mv_final = True, None
        for d in range(1, 13):
            ce._lib.cs_tt_reset()          # cold TT => reproducible count
            mv_final = ce.get_best_move(chess.Board(CE_LADDER_FEN), d)
            n, sc = ce.nodes_searched, ce.last_score
            exp = CE_LADDER.get(d)
            match = exp is not None and (n, sc) == exp
            ok_all = ok_all and match and mv_final in chess.Board(CE_LADDER_FEN).legal_moves
            flag = "  " if match else "!!"
            exp_s = "" if match else f"  != expected {exp}"
            print(f"  {flag} d{d:2d}  {str(mv_final):6s} score={sc:6d} "
                  f"nodes={n:>7,}{exp_s}")
        check("C core ladder to depth 12 (nodes+score pinned)", ok_all,
              "search reached d12, all values match CE_LADDER"
              if ok_all else "a value changed -- confirmed C-search change? "
              "re-measure CE_LADDER; else regression")

        # --- 5c. NPS: 2s timed search, print throughput ------------------ #
        # Catches the two disasters a fixed-depth ladder can't: a slow/
        # unoptimized build and the pure-Python eval fallback. Absolute NPS
        # is machine-dependent, so the printed number is for eyeballing
        # "dramatically up or down"; the hard check is only the disaster
        # floor (this machine ~2.9M; a healthy build is well over 1M).
        ce.use_book = False
        t0 = time.perf_counter()
        ce.get_best_move_timed(chess.Board(), 2.0, max_depth=99)
        dt = time.perf_counter() - t0
        nps = ce.nodes_searched / dt if dt > 0 else 0
        print(f"\nC core 2s search (startpos): depth {ce.last_depth}, "
              f"{ce.nodes_searched:,} nodes in {dt:.2f}s = {nps:,.0f} nps")
        check("C core NPS above disaster floor", nps > 300_000,
              f"{nps:,.0f} nps (floor 300k; expected ~1M+; "
              "below floor = unoptimized build or Python eval fallback)")
    except Exception as ex:
        check("C core (cengine) searches", False,
              f"{type(ex).__name__}: {ex} -- rebuild csearch.so via ./setup.sh")

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
