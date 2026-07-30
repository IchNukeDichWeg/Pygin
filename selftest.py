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
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

REF_FEN = "r3k2r/8/8/8/8/8/8/R2QK2R w KQkq - 0 1"
REF_DEPTH = 6
REF_MOVE = "h1h8"
REF_NODES = 2874          # update on confirmed search changes (see docstring)

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
      "rebuild: python3 scripts/eval_build.py" if not engine._USE_C_EVAL else
      f"ABI {engine._eval_lib.abi_version()}")
check("movegen.so loaded (C movegen active)", engine._USE_C_MOVEGEN,
      "rebuild: python3 scripts/movegen_build.py" if not engine._USE_C_MOVEGEN else "")

# --- 3. move generation correct (perft spot check via perft.py) --------- #
r = subprocess.run([sys.executable, os.path.join("testing", "perft.py")],
                   capture_output=True, text=True)
check("perft quick suite", r.returncode == 0,
      "run `python3 testing/perft.py` to see the failing position" if r.returncode else "all positions exact")

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
# FB-58: this header claimed coverage the suite did not have -- until
# 2026-07-26, calculate_move_time had NO assertion anywhere in the tree, while
# every timed campaign, every SPRT confirm and the whole PM-01 premove chain
# stand on it. The table below is the deliverable, not a formality.
from time_manager import calculate_move_time as _cmt              # noqa: E402
_tm_board = chess.Board()
_tm_bad = []
# (label, my_ms, opp_ms, inc_ms, movestogo)
_TM_CASES = [
    ("classical 40/90+30", 90 * 60_000, 90 * 60_000, 30_000, 40),
    ("campaign 50+0.20",   50_000,      50_000,      200,     None),
    ("bullet 60+0",        60_000,      60_000,      0,       None),
    ("increment-only 1+2", 1_000,       60_000,      2_000,   None),
    ("low-time panic",     300,         30_000,      0,       None),
    ("movestogo 5",        30_000,      30_000,      0,       5),
]
_tm_budgets = {}
for _lbl, _my, _opp, _inc, _mtg in _TM_CASES:
    _b = _cmt(_tm_board, _my, _opp, _inc, movestogo=_mtg)
    _tm_budgets[_lbl] = _b
    if _b <= 0:
        _tm_bad.append(f"{_lbl}: non-positive budget {_b}")
    # NEVER spend more than the clock we actually have.
    if _b > _my:
        _tm_bad.append(f"{_lbl}: budget {_b:.0f} > clock {_my}")
# Monotonic in remaining time: more clock must never mean less thinking.
_mono = [_cmt(_tm_board, ms, 60_000, 0) for ms in
         (1_000, 5_000, 20_000, 60_000, 300_000)]
if any(b < a - 1e-9 for a, b in zip(_mono, _mono[1:])):
    _tm_bad.append(f"not monotonic in remaining time: "
                   + " ".join(f"{v:.0f}" for v in _mono))
check("FB-58: calculate_move_time budgets are sane and monotonic",
      not _tm_bad,
      "; ".join(_tm_bad) if _tm_bad else
      f"50+0.20 -> {_tm_budgets['campaign 50+0.20']:.0f} ms, "
      f"panic(300ms) -> {_tm_budgets['low-time panic']:.0f} ms, "
      f"monotonic over 1s..300s")

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
CE_LADDER = {
    1: (94, 111),
    2: (212, 111),
    3: (405, 111),
    4: (1911, 97),
    5: (5013, 135),
    6: (11171, 107),
    7: (36146, 120),
    8: (82605, 77),
    9: (157544, 81),
    10: (233641, 85),
    11: (363615, 98),
    12: (585429, 86),
    13: (998801, 87),
    14: (1890460, 95),
}
if os.path.exists("csearch.c"):
    try:
        import cengine  # noqa: E402
        # Outpost: NULL, OFF (A/B vs v37 2026-07-10: -0.90 +/-6.8) -- the
        # default already reproduces v37; belt-and-braces pin.
        cengine.Engine.USE_OUTPOST = False
        # TT_BITS: CONFIRMED into v47 at 23 (192 MB, +3.16 +/-6.8 @10k vs Old
        # Engine/46 -- the 96->192 MB increment; monotonic-low-risk lever,
        # net-positive at full load, RAM free). Diminishing (+5.94 then
        # +3.16) so memory-scaling CLOSES here -- no 24 probe. The CE_LADDER
        # above is the 23-bit measurement; 23 is the shipped default so this
        # pin is belt-and-braces. MultiPV (abi 10) is node-exact off (empty
        # exclusion list), so it needs no pin.
        cengine.Engine.TT_BITS = 23
        # CB-01 correctness batch: CONFIRMED into v38 (+1.36 null KEPT as
        # correctness), default ON -- part of the pinned reference search.
        # CB-02 correctness batch #4: CONFIRMED into v41 (-2.88 null KEPT
        # as correctness -- 50-move in qsearch, verified null, null-store
        # policy, fail-high adoption), default ON -- part of the pinned
        # reference search above.
        # CW-01 cannot-win eval clamp: CONFIRMED into v42 (+3.27 null KEPT
        # as correctness -- the eval no longer favors sides that cannot
        # force mate), default ON. The ladder is UNCHANGED by it (the clamp
        # cannot fire from this FEN's trees -- both sides keep pawns), so
        # the v41 pins carry over verbatim.
        # NV-01 verification isolation: RESOLVED into v43 (+5.18 vs Old
        # Engine/42, removal direction -- two independent reads priced
        # CB-02's deep-null verification at ~3-5 Elo of nodes-to-depth
        # cost, so v43 DROPS it; NULL_VERIFY=False is the confirmed
        # default, True = v42's verifying search).
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
        # FI-08/Q-03 qsearch eviction guard: DORMANT (dead null +0.14
        # +/-6.8 @10k vs Old Engine/40, 2026-07-11; default -1 = off = v40
        # rule). The cold ladder never saw it either way (post-reset
        # old-gen entries are zeroed depth-0 slots).
        # FI-04 history-LMR is DORMANT (+2.15 null @10k vs Old Engine/43,
        # 2026-07-12; the finer-quiet-signal vein is 0-for-3); the default
        # already reproduces v43, so this pin is belt-and-braces.
        try:
            ce._lib.set_lmr_hist(0)
        except AttributeError:
            pass                       # pre-FI-04 csearch.so
        # FI-26a TT prefetch: CONFIRMED into v44 (+13.31 +/-6.8 @10k vs Old
        # Engine/43, 2026-07-12) -- NODE-IDENTICAL, the v43 pins carried
        # over verbatim.
        # FI-25 TT-value pruning-eval sharpener: CONFIRMED into v45 (+13.52
        # +/-6.8 @10k vs Old Engine/44, 2026-07-12), default ON -- part of
        # the pinned reference search above (ladder re-measured with it).
        # FI-18 SEE pruning of losing captures is DORMANT (-1.25 null @10k
        # vs Old Engine/45, 2026-07-13; not correctness => default False,
        # mechanism kept); the default already reproduces v45, so this pin
        # is belt-and-braces.
        try:
            ce._lib.set_see_prune(0)
        except AttributeError:
            pass                       # pre-FI-18 csearch.so
        # FI-23 history-driven quiet pruning is REJECTED (twenty-first
        # campaign vs Old Engine/47, 2026-07-16: -5.23 +/-7.1, SPRT ACCEPT
        # H0 -- a real negative; HIST_PRUNE=0, dormant, do-not-retry at this
        # TC; shallow-prune vein 0-for-2 with FI-18); the default already
        # reproduces v47, so this pin is belt-and-braces.
        try:
            ce._lib.set_hist_prune(0)
        except AttributeError:
            pass                       # pre-FI-23 csearch.so
        # FI-30 qsearch TT-quality batch: CONFIRMED into v48 2026-07-16
        # (+4.73 +/-3.19 pooled @21,605 games vs Old Engine/47, GSPRT[0,4]
        # LLR +3.475 ACCEPT -- the C era's first sequential-test accept).
        # ON is the shipped default; pinned 1 so the v48 ladder below
        # survives any future re-toggle experiment (load-bearing).
        try:
            ce._lib.set_qs_tt_sharpen(1)
            ce._lib.set_qs_keep_move(1)
        except AttributeError:
            pass                       # pre-FI-30 csearch.so
        # FI-29 cuckoo upcoming-repetition: KEPT-ON-NULL into v49
        # 2026-07-17 (+0.97 +/-6.8 @10k vs Old Engine/48, GSPRT LLR -0.19
        # -- the sixth correctness release of its class). ON is the
        # shipped default; pinned 1 so the v49 ladder below survives any
        # future re-toggle experiment (load-bearing).
        try:
            ce._lib.set_cycle(1)
        except AttributeError:
            pass                       # pre-FI-29 csearch.so
        # FI-50/51/52 qsearch-TT batch: NULL (twenty-fourth campaign vs Old
        # Engine/49, 2026-07-18: -0.28 +/-6.8 @10k, LLR -0.797 flat -- not
        # correctness-class => reverted to dormant, mechanisms kept). The
        # defaults already reproduce v49, so these pins are belt-and-braces.
        try:
            ce._lib.set_qs_beta_narrow(0)
            ce._lib.set_qs_ttm_exempt(0)
            ce._lib.set_qs_chk_d1(0)
        except AttributeError:
            pass                       # pre-FI-50/51/52 csearch.so
        # FI-48 flag-aware TT replacement: CLOSED AS DEAD GATE 2026-07-18
        # pre-A/B (instrumented engagement ~0.001% of nodes at both levels;
        # the probe-side EXACT cutoff structurally prevents the guarded
        # overwrites). Default is 0, so this pin is belt-and-braces.
        try:
            ce._lib.set_tt_keep_exact(0)
        except AttributeError:
            pass                       # pre-FI-48 csearch.so
        # FI-49 fail-high depth tightening: REJECTED (twenty-fifth campaign
        # vs Old Engine/49, 2026-07-18: -3.65 +/-6.8 @10k, LLR -2.403
        # reject-lean -- the +28% node cost never paid; dormant,
        # do-not-retry at this TC). Default False, so this pin is
        # belt-and-braces.
        try:
            ce._lib.set_tt_fh_tight(0)
        except AttributeError:
            pass                       # pre-FI-49 csearch.so
        # FI-53/FI-54 store/probe pair: KEPT-ON-NULL into v50 2026-07-18
        # (+1.60 +/-6.8 @10k vs Old Engine/49, GSPRT LLR +0.117 -- the
        # seventh+eighth correctness releases of the class). ON is the
        # shipped default; pinned 1 so the v50 ladder below survives any
        # future re-toggle experiment (load-bearing).
        try:
            ce._lib.set_tt_r50(1)
            ce._lib.set_term_store(1)
            ce._lib.set_tt_mate_cut(1)
        except AttributeError:
            pass                       # pre-FI-53/54 csearch.so
        # FI-56 root LMR: CONFIRMED into v51 2026-07-18 (pooled +11.12
        # +/-5.3 @9,343 games vs Old Engine/50, pooled GSPRT[0,4] LLR
        # +4.549 -- the C era's second SPRT accept). ON is the shipped
        # default; pinned 1 so the v51 ladder below survives any future
        # re-toggle experiment (load-bearing).
        try:
            ce._lib.set_root_lmr(1)
        except AttributeError:
            pass                       # pre-FI-56 csearch.so
        # FI-55 IIR weak-evidence trigger: SCREEN-KILLED 2026-07-19
        # (-9.04 +/-15.2 @2k vs Old Engine/51 -- negative lean on a +0-2
        # prior, no 10k spent; matetrack's +100-mate read did not predict
        # Elo). Default False, so this pin is belt-and-braces.
        try:
            ce._lib.set_iir_weak(0)
        except AttributeError:
            pass                       # pre-FI-55 csearch.so
        # FI-64 badcap LMR: SCREEN-KILLED 2026-07-21 (-10.95 +/-15.3 @2k
        # nodes@2M vs Old Engine/51; GCloud timed screen had read +2.78 --
        # combined null-to-negative, no 10k spent). Default False, so this
        # pin is belt-and-braces.
        try:
            ce._lib.set_lmr_badcap(0)
        except AttributeError:
            pass                       # pre-FI-64 csearch.so
        # P-26 sweep point 1 (NULL_BASE 2->3): ARMED (thirtieth campaign vs
        # Old Engine/51) but NOT yet confirmed -- v51 is (2,6,200), so the
        # ladder pins the v51 values here (LOAD-BEARING: cengine's class
        # defaults carry the armed point for match play). Re-pin CE_LADDER
        # only when a sweep point is CONFIRMED/kept.
        ce._lib.set_null_move(2, 6)
        ce._lib.set_lmr_div(200)
        # FI-24a/b null refinement batch: CONFIRMED into v52 2026-07-21
        # (pooled +6.63 +/-4.5 @12,000 games vs Old Engine/51, pooled
        # GSPRT[0,4] LLR +4.533 ACCEPT -- the third SPRT accept). ON is
        # the shipped default; pinned 1 so the v52 ladder below survives
        # any future re-toggle experiment (load-bearing).
        try:
            ce._lib.set_null_nodouble(1)
            ce._lib.set_null_evalr(1)
        except AttributeError:
            pass                       # pre-FI-24ab csearch.so
        # FI-12 history persistence: ARMED CANDIDATE 2026-07-22, not yet
        # confirmed. LOAD-BEARING -- cengine's class default carries the
        # armed value for match play, so the ladder must force it OFF or it
        # measures the candidate instead of v53. Re-pin CE_LADDER only if
        # FI-12 is CONFIRMED.
        try:
            ce._lib.set_hist_keep(0)
        except AttributeError:
            pass                       # pre-FI-12 csearch.so
        # FI-63 quiet check-evasion cap: CLOSED AS DEAD GATE 2026-07-21
        # pre-A/B (harmful at cap 2 -- +10.5% nodes + matetrack -18 mates;
        # vacuous at cap>=3). Default 0, so this pin is belt-and-braces.
        try:
            ce._lib.set_qs_evasion_cap(0)
        except AttributeError:
            pass                       # pre-FI-63 csearch.so
        # P-33 singular extensions: CLOSED 2026-07-21 pre-A/B on two
        # independent matetrack failures (-34 found mates both times, with
        # and without an independent extension budget). Default False, so
        # this pin is belt-and-braces.
        try:
            ce._lib.set_singular(0)
        except AttributeError:
            pass                       # pre-P-33 csearch.so
        # FI-59/FI-60 ordering-history batch: FI-59 SCREEN-KILLED
        # 2026-07-21 (-5.21 pooled @2k, no tranche spent); FI-60 parked
        # pre-arm (+27.3% nodes). Both default False -- belt-and-braces.
        try:
            ce._lib.set_killer_inherit(0)
            ce._lib.set_quiet_malus_all(0)
        except AttributeError:
            pass                       # pre-FI-59/60 csearch.so
        # FI-06 root-move ordering is DORMANT (+2.26 null @10k vs Old
        # Engine/45, 2026-07-13 -- positive lean but CI covers zero, not
        # correctness); the default already reproduces v45, so this pin is
        # belt-and-braces.
        try:
            ce._lib.set_root_order(0)
        except AttributeError:
            pass                       # pre-FI-06 csearch.so
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
        recompute = "--recompute-ladder" in sys.argv   # FI-45: paste-ready
        rows = []                                      # re-pin output
        for d in range(1, 15):
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
            rows.append((d, n, sc))
        if recompute:
            # FI-45: every confirm re-pins the ladder by hand -- print the
            # block paste-ready instead. NOTE: values reflect the CURRENT
            # pin set above; only paste after a CONFIRMED tree change.
            print("\n# paste into CE_LADDER (selftest.py):")
            print("CE_LADDER = {")
            for d, n, sc in rows:
                print(f"    {d}: ({n}, {sc}),")
            print("}")
        check("C core ladder to depth 14 (nodes+score pinned)", ok_all,
              "search reached d14, all values match CE_LADDER"
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

        # --- 5d. determinism: cold-TT double run must be bit-identical --- #
        # Catches uninitialized memory, stray thread state, and any hidden
        # nondeterminism the pinned ladder would only see as a one-off.
        runs = []
        for _ in range(2):
            ce._lib.cs_tt_reset()
            ce.get_best_move(chess.Board(CE_LADDER_FEN), 10)
            runs.append((ce.nodes_searched, ce.last_score))
        check("C core deterministic (cold-TT d10 double run)",
              runs[0] == runs[1], f"{runs[0]} vs {runs[1]}")

        # --- 5e. mate minisuite: mate scores AND full PVs end in mate ---- #
        # PV-02 guarantees the exact line; a truncated or illegal mate PV
        # here means PV extraction or the mate-score plumbing regressed.
        MATES = [  # (fen, depth, max plies to mate)
            ("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1", 4, 1),   # back rank
            ("r6k/5ppp/8/8/8/8/1R3PPP/1R4K1 w - - 0 1", 6, 3), # ladder M2
        ]
        mates_ok, mate_why = True, "scores mate and PV ends in checkmate"
        for fen, d, plies in MATES:
            ce._lib.cs_tt_reset()
            ce.get_best_move(chess.Board(fen), d)
            if ce.last_score < ce.MATE_THRESHOLD:
                mates_ok, mate_why = False, f"no mate score on {fen}"
                break
            bd = chess.Board(fen)
            for u in ce.last_pv.split()[:plies]:
                mvp = chess.Move.from_uci(u)
                if mvp not in bd.legal_moves:
                    mates_ok, mate_why = False, f"illegal PV move {u} on {fen}"
                    break
                bd.push(mvp)
            else:
                if not bd.is_checkmate():
                    mates_ok, mate_why = False, f"PV does not mate on {fen}"
            if not mates_ok:
                break
        check("C core mate minisuite (score + exact PV)", mates_ok, mate_why)

        # --- 5f. draw machinery: cycle bound + dead material ------------- #
        # Blocked pawn wall: only reversible shuffles exist; FI-29's cycle
        # bound must collapse it to an exact 0 (this is also the cheap
        # engagement canary for CYCLE_DETECT). KNvK: insufficient-material
        # rule answers inside the contempt draw band without a real search.
        ce._lib.cs_tt_reset()
        ce.get_best_move(chess.Board("k7/8/8/p1p1p1p1/P1P1P1P1/8/8/K7 w - - 0 1"), 16)
        check("draw machinery: blocked-wall fortress scores exactly 0",
              ce.last_score == 0, f"score {ce.last_score} (cycle bound engaged?)")
        ce._lib.cs_tt_reset()
        ce.get_best_move(chess.Board("8/8/8/4k3/8/2N5/8/4K3 w - - 0 1"), 8)
        check("draw machinery: KNvK inside the contempt draw band",
              abs(ce.last_score) <= 60 and ce.nodes_searched < 5000,
              f"score {ce.last_score}, {ce.nodes_searched} nodes")

        # --- 5g. SMP smoke: helper threads search without corruption ----- #
        # Lazy-SMP is opt-in for matches but load-bearing for GUI/analysis
        # use; a half-second 4-thread search catches crashes and garbage
        # moves (scores are nondeterministic under SMP -- only legality
        # and liveness are asserted).
        ce.smp_workers = 4
        mv_smp = ce.get_best_move_timed(chess.Board(), 0.5, max_depth=99)
        ce.smp_workers = 1
        check("C core SMP smoke (4 threads, 0.5s)",
              mv_smp is not None and mv_smp in chess.Board().legal_moves,
              f"depth {ce.last_depth}, move {mv_smp}")

        # --- 5f2. FB-56: the P-26 defaults agree across the three files -- #
        # csearch.c, cengine.py's class attributes and cuci.py each carried
        # their own copy. P-26's sweep put these on a PLATEAU, so a drift
        # would be invisible in play while meaning the A/B harness and the
        # UCI host ran different engines. The C table is the source of truth;
        # it is immutable (the setters move the globals, not the table), so
        # this reads the SHIPPED value even after construction pushed others.
        import ctypes as _ct                                # noqa: E402
        ce._lib.cs_p26_default.restype = _ct.c_int
        ce._lib.cs_p26_default.argtypes = [_ct.c_int]
        ce._lib.cs_p26_count.restype = _ct.c_int
        P26 = ["rfp_margin", "rfp_depth", "fut_margin", "delta_margin",
               "lmp1", "lmp2", "lmp3", "null_base", "null_div", "lmr_div_x100"]
        cdef = {P26[i]: ce._lib.cs_p26_default(i)
                for i in range(ce._lib.cs_p26_count())}
        import cuci as _cuci                                # noqa: E402
        p26_bad = []
        for name, pyval in (("null_base", cengine.Engine.NULL_BASE),
                            ("null_div", cengine.Engine.NULL_DIV),
                            ("lmr_div_x100", cengine.Engine.LMR_DIV)):
            if cdef.get(name) != pyval:
                p26_bad.append(f"{name}: C {cdef.get(name)} vs cengine {pyval}")
        check("FB-56: P-26 defaults agree (C is the source of truth)",
              not p26_bad and len(cdef) == 10,
              "; ".join(p26_bad) if p26_bad else
              f"{len(cdef)} defaults, null={cdef['null_base']}/{cdef['null_div']}"
              f" lmr_div={cdef['lmr_div_x100'] / 100:.2f}"
              f" rfp={cdef['rfp_margin']}/{cdef['rfp_depth']}")

        # --- 5g0. FB-55: the construction guard covers EVAL, not just toggles #
        # csearch.so's eval params are process-wide, so two engines differing
        # only in a retuned scalar (a texel candidate vs the shipped eval --
        # this project's commonest same-process pairing) used to pass the
        # guard and silently share whichever was built first.
        _fp_eval = ce._eval_fingerprint()
        check("FB-55: construction guard hashes the eval payload",
              isinstance(_fp_eval, str) and len(_fp_eval) == 16
              and _fp_eval == ce._eval_fingerprint(),
              f"eval fingerprint {_fp_eval}")

        # --- 5g1. FI-42: eval accumulator vs the from-scratch oracle ----- #
        # apply_move maintains (mg, eg, phase) incrementally on the same
        # squares FI-01's Zobrist update touches. cs_acc_walk applies REAL
        # moves over the whole tree and recomputes from scratch at every
        # node; the castling / promotion / en-passant trees are the ones
        # that matter (FI-01's own bug was a castling case).
        import ctypes as _ct                                # noqa: E402
        ce._lib.cs_acc_walk.restype = _ct.c_uint64
        ce._lib.cs_acc_walk.argtypes = [_ct.c_uint64] * 8 + [
            _ct.c_int, _ct.c_int, _ct.c_uint64, _ct.c_int]
        acc_bad, acc_depth = 0, 4
        for fen_acc in (
                chess.STARTING_FEN,
                "r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1",
                "rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8"):
            bd_acc = chess.Board(fen_acc)
            acc_bad += ce._lib.cs_acc_walk(
                bd_acc.pawns, bd_acc.knights, bd_acc.bishops, bd_acc.rooks,
                bd_acc.queens, bd_acc.kings,
                bd_acc.occupied_co[chess.WHITE], bd_acc.occupied_co[chess.BLACK],
                1 if bd_acc.turn == chess.WHITE else 0,
                bd_acc.ep_square if bd_acc.ep_square is not None else -1,
                bd_acc.clean_castling_rights(), acc_depth)
        check("FI-42: eval accumulator matches the oracle on every node",
              acc_bad == 0,
              f"{acc_bad} mismatches over 3 trees at d{acc_depth}")

        # --- 5g1b. FI-16: movegen split consistency ----------------------- #
        # csearch.c carries FIVE generators that must agree by construction
        # (gen_legal, gen_noisy, gen_captures, gen_quiets, has_legal_quiet),
        # plus movegen.c's independent copy next door. perft.py drives
        # movegen.so and therefore cannot see csearch.c's generators at all;
        # the ladder and bench catch a divergence only INDIRECTLY, as a node
        # count change. Two direct gates instead:
        #   (a) the four split invariants at every node of a real tree walk
        #   (b) csearch's gen_legal vs movegen.so's, move for move
        MGC_WHY = {1: "captures+quiets != gen_legal", 2: "noisy not a subset",
                   4: "has_legal_quiet disagrees", 8: "move_from_key broke",
                   16: "a split is OUT OF gen_legal's order (FB-57)",
                   32: "gen_noisy != {victim||promo} exactly (FB-57)"}
        ce._lib.cs_movegen_walk.restype = _ct.c_uint64
        ce._lib.cs_movegen_walk.argtypes = [_ct.c_uint64] * 8 + [
            _ct.c_int, _ct.c_int, _ct.c_uint64, _ct.c_int]
        MGC_FENS = (
            chess.STARTING_FEN,
            "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
            "r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1",
            "rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8",
        )
        mgc_bad, mgc_flags = 0, 0
        for fen_mg in MGC_FENS:
            bd = chess.Board(fen_mg)
            r_mg = ce._lib.cs_movegen_walk(
                bd.pawns, bd.knights, bd.bishops, bd.rooks, bd.queens, bd.kings,
                bd.occupied_co[chess.WHITE], bd.occupied_co[chess.BLACK],
                1 if bd.turn == chess.WHITE else 0,
                bd.ep_square if bd.ep_square is not None else -1,
                bd.clean_castling_rights(), 3)
            mgc_bad += r_mg & 0xFFFFFFFF
            mgc_flags |= r_mg >> 32
        check("FI-16+FB-57: five generators agree, in order (split invariants)",
              mgc_bad == 0,
              f"{mgc_bad} bad nodes: "
              + ", ".join(v for k, v in MGC_WHY.items() if mgc_flags & k))

        # (b) the two INDEPENDENT gen_legal implementations must emit the same
        # move set. This is what makes perft's 1.5B nodes cover csearch too:
        # perft only ever exercised movegen.so's copy.
        mg_path = os.path.join(HERE, "movegen.so")
        if os.path.exists(mg_path):
            mgso = _ct.CDLL(mg_path)
            mgso.generate_legal.restype = _ct.c_int
            mgso.generate_legal.argtypes = [_ct.c_uint64] * 8 + [
                _ct.c_int, _ct.c_int, _ct.c_uint64,
                _ct.POINTER(_ct.c_uint32 * 256)]
            ce._lib.cs_gen_legal_list.restype = _ct.c_int
            ce._lib.cs_gen_legal_list.argtypes = [_ct.c_uint64] * 8 + [
                _ct.c_int, _ct.c_int, _ct.c_uint64,
                _ct.POINTER(_ct.c_uint32 * 256)]
            buf_a, buf_b = (_ct.c_uint32 * 256)(), (_ct.c_uint32 * 256)()
            random.seed(16)
            bd = chess.Board()
            split_ok, split_n, split_fen = True, 0, ""
            for _ in range(1500):
                if bd.is_game_over() or bd.fullmove_number > 120:
                    bd = chess.Board()
                    continue
                if not bd.is_check():          # movegen.so refuses in check
                    args = (bd.pawns, bd.knights, bd.bishops, bd.rooks,
                            bd.queens, bd.kings,
                            bd.occupied_co[chess.WHITE],
                            bd.occupied_co[chess.BLACK],
                            1 if bd.turn == chess.WHITE else 0,
                            bd.ep_square if bd.ep_square is not None else -1,
                            bd.clean_castling_rights())
                    n_a = mgso.generate_legal(*args, _ct.byref(buf_a))
                    n_b = ce._lib.cs_gen_legal_list(*args, _ct.byref(buf_b))
                    if n_a >= 0:
                        keys_a = sorted(buf_a[i] & 0x7FFF for i in range(n_a))
                        keys_b = sorted(buf_b[i] for i in range(n_b))
                        split_n += 1
                        if keys_a != keys_b:
                            split_ok, split_fen = False, bd.fen()
                            break
                bd.push(random.choice(list(bd.legal_moves)))
            check("FI-16: csearch and movegen.so generate the same moves",
                  split_ok and split_n > 800,
                  f"{split_n} positions agree" if split_ok
                  else f"DIVERGED on {split_fen}")

        # --- 5g2. PM-01 certification honours its wall-clock contract ---- #
        # FB-45: PREMOVE_CAP_S only blocks NEW sub-searches, so the true
        # bound is CAP_S + one (now capped) sub-search. Assert the bound and
        # that every certified pair is legal -- a premove that is wrong or
        # late is worse than no premove at all.
        import cuci                                        # noqa: E402
        b0 = chess.Board()
        mv0 = ce.get_best_move_timed(b0, 1.0, max_depth=99)
        t_pm = time.perf_counter()
        chain = cuci.certify_premoves(ce, b0, mv0, threading.Event())
        dt_pm = time.perf_counter() - t_pm
        bound = cuci.PREMOVE_CAP_S + cuci.PREMOVE_SEARCH_CAP_S
        bb = b0.copy(); bb.push(mv0)
        chain_ok = True
        for r_pm, m_pm in chain:
            if r_pm not in bb.legal_moves:
                chain_ok = False; break
            bb.push(r_pm)
            if m_pm not in bb.legal_moves:
                chain_ok = False; break
            bb.push(m_pm)
        check("PM-01 certification inside its wall-clock bound (FB-45)",
              chain_ok and dt_pm <= bound + 0.1,
              f"{dt_pm * 1000:.0f} ms vs {bound * 1000:.0f} ms bound, "
              f"{len(chain)} pair(s){'' if chain_ok else ', ILLEGAL PAIR'}")
    except Exception as ex:
        check("C core (cengine) searches", False,
              f"{type(ex).__name__}: {ex} -- rebuild csearch.so via ./setup.sh")

# --- 5h. FI-87: has_legal_quiet's subsumption claim ---------------------- #
# csearch.c's has_legal_quiet (the qsearch stalemate scan that decides
# draw-vs-eval when gen_noisy is empty) deliberately skips castling and
# double pushes, on the asserted claim that both are SUBSUMED: castling legal
# => the K->f king step is legal, a legal double push => the single push is
# legal. The claim was only ever a comment. It is a property of chess, not of
# the C code, so python-chess is the oracle: if it ever goes false the scan
# starts calling live positions stalemate -- a silent scoring bug, not a crash.
#
# Corpus note (the coverage is deliberately lopsided, because the claim is):
#   * the DOUBLE-PUSH half is randomly stressed -- sparse endgame positions
#     reach "only a pawn push is quiet" often (negative control: excluding
#     single pushes instead makes this very check fire within 4k positions,
#     so the harness has teeth). Full-game playouts never get that tight.
#   * the CASTLING half is over-determined and cannot be stressed randomly:
#     O-O legal requires f1/g1 empty and f1 unattacked, so Kf1 AND Rf1/Rg1
#     are legal quiets too. Crafted positions pin it; the argument carries it.
random.seed(87)
pos_checked, subsumption_ok, bad_fen = 0, True, ""
CRAFTED = [
    "4k3/8/8/8/8/8/8/R3K2R w KQ - 0 1",         # castling available
    "r3k2r/8/8/8/8/8/8/4K3 b kq - 0 1",
    "4k3/8/8/8/8/8/4P3/4K3 w - - 0 1",          # double push available
    "7k/5K2/8/8/8/8/8/5R2 b - - 0 1",           # black nearly stalemated
    "k7/P7/K7/8/8/8/8/8 b - - 0 1",             # black IS stalemated
    "8/8/8/8/8/1r6/P7/K7 w - - 0 1",            # pinned pawn, king moves only
    "1r4k1/8/8/3P4/8/K7/2q5/8 w - - 0 1",       # only quiet IS the pawn push
]
boards = [chess.Board(f) for f in CRAFTED]
b_pl = chess.Board()
for _ in range(2000):                     # seeded playout sweep (broad)
    if b_pl.is_game_over() or b_pl.fullmove_number > 120:
        b_pl = chess.Board()
        continue
    boards.append(b_pl.copy())
    b_pl.push(random.choice(list(b_pl.legal_moves)))
while len(boards) < 6000:                 # sparse endgames (the tight ones)
    sb = chess.Board(None)
    sqs = random.sample(range(64), random.randint(3, 5))
    sb.set_piece_at(sqs[0], chess.Piece(chess.KING, chess.WHITE))
    sb.set_piece_at(sqs[1], chess.Piece(chess.KING, chess.BLACK))
    for s in sqs[2:]:
        pt = random.choice([chess.PAWN, chess.KNIGHT, chess.BISHOP,
                            chess.ROOK, chess.QUEEN])
        if pt == chess.PAWN and chess.square_rank(s) in (0, 7):
            pt = chess.QUEEN                   # no pawns on the back ranks
        sb.set_piece_at(s, chess.Piece(pt, random.choice([True, False])))
    sb.turn = random.choice([chess.WHITE, chess.BLACK])
    if sb.is_valid():
        boards.append(sb)
for bq in boards:
    if bq.is_check():                     # scan is only used out of check
        continue
    quiets = [m for m in bq.legal_moves if not bq.is_capture(m)]
    scanned = [m for m in quiets          # what has_legal_quiet actually looks at
               if not bq.is_castling(m)
               and not (bq.piece_type_at(m.from_square) == chess.PAWN
                        and abs(chess.square_rank(m.to_square)
                                - chess.square_rank(m.from_square)) == 2)]
    pos_checked += 1
    if bool(quiets) != bool(scanned):
        subsumption_ok, bad_fen = False, bq.fen()
        break
check("FI-87: castling/double-push subsumed by has_legal_quiet's scan",
      subsumption_ok and pos_checked > 4000,
      f"{pos_checked} positions" if subsumption_ok
      else f"COUNTEREXAMPLE: {bad_fen}")

# --- 5i. cuci UCI host: protocol round-trip ------------------------------ #
# The UCI host carries every external consumer (GUIs, Mephisto, matetrack,
# a future OpenBench); a broken handshake or a silent bestmove regression
# must fail here, not in the field.
if os.path.exists("cuci.py"):
    r = subprocess.run(
        [sys.executable, "cuci.py"],
        input="uci\nisready\nposition startpos moves e2e4\ngo depth 6\nquit\n",
        capture_output=True, text=True, timeout=120)
    out = r.stdout
    uci_ok = ("uciok" in out and "readyok" in out and "bestmove " in out)
    bm = next((l for l in out.splitlines() if l.startswith("bestmove ")), "")
    try:
        bmv = chess.Move.from_uci(bm.split()[1]) if bm else None
        bd = chess.Board(); bd.push_uci("e2e4")
        uci_ok = uci_ok and bmv in bd.legal_moves
    except Exception:
        uci_ok = False
    check("cuci UCI round-trip (uciok/readyok/legal bestmove)", uci_ok,
          bm if bm else "no bestmove line -- see `python3 cuci.py` by hand")

    # FB-44: real ponder round-trip, and the release watcher must leave the
    # NEXT search alone. Must be PACED (Popen, not run(input=...)): a batch
    # stdin hands the host `quit` while the search is still running, so every
    # bestmove comes back instantly and the check asserts nothing.
    # This is a regression guard, not a reproduction -- the window is
    # microseconds (the search sets `holding` on every exit path, so a
    # released watcher wakes while the host is still inside join()), which is
    # why the fix is by construction. A watcher that fires on the wrong search
    # -- or a swap_lock deadlock -- shows up here as a short/missing search.
    pp = subprocess.Popen([sys.executable, "cuci.py"], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True, bufsize=1)
    wd = threading.Timer(60, pp.kill); wd.start()   # readline() must not wedge

    def _send(s):
        pp.stdin.write(s + "\n"); pp.stdin.flush()

    def _bestmove():
        while True:
            ln = pp.stdout.readline()
            if not ln or ln.startswith("bestmove "):
                return ln.strip()

    _send("uci"); _send("setoption name Ponder value true")
    _send("setoption name OwnBook value false")   # a book hit is instant
    _send("position startpos moves e2e4")
    _send("go ponder wtime 300000 btime 300000")
    time.sleep(0.4)
    _send("ponderhit")
    time.sleep(0.1)
    _send("stop")
    bm_p = _bestmove()
    _send("position startpos moves e2e4 e7e5")
    t_pon = time.perf_counter()
    _send("go movetime 1200")
    bm_2 = _bestmove()
    dt_pon = time.perf_counter() - t_pon
    _send("quit"); pp.wait(timeout=20); wd.cancel()
    check("ponder round-trip + watcher leaves the next search alone (FB-44)",
          bm_p.startswith("bestmove ") and bm_2.startswith("bestmove ")
          and dt_pon >= 1.0,
          f"ponder -> {bm_p or 'NOTHING'}; next go took {dt_pon:.2f}s "
          f"of its 1.20s (short = truncated by a stale watcher)")

    # FB-46: Hash lands on a power-of-two ENTRY count, so 200 MB becomes 192.
    # The round-down must be announced and the fingerprint must show it (a
    # second `uci` reprints the fingerprint).
    r = subprocess.run(
        [sys.executable, "cuci.py"],
        input="uci\nsetoption name Hash value 200\nuci\nquit\n",
        capture_output=True, text=True, timeout=120)
    said = "info string Hash 200 MB rounded down to 192 MB" in r.stdout
    fps = [l for l in r.stdout.splitlines() if " hash_bits=" in l]
    check("cuci Hash round-down is reported (FB-46)",
          said and len(fps) == 2 and "hash_bits=23" in fps[1],
          "" if said else "no info string for Hash 200")

# --- 5j. FI-82: SPRT tranche pooling (harness, no games played) ---------- #
# The pooled statistic decides campaigns; the two ways to corrupt it are
# pooling the wrong experiment and pooling overlapping shards. Subprocess so a
# match.py import cannot disturb anything above.
_t_sprt = os.path.join("testing", "test_sprt_resume.py")
if os.path.exists(_t_sprt):
    try:
        r = subprocess.run([sys.executable, _t_sprt],
                           capture_output=True, text=True, timeout=180)
        check("SPRT resume: pooling, fingerprint + offset refusals (FI-82)",
              r.returncode == 0,
              "" if r.returncode == 0
              else (r.stdout + r.stderr).strip().splitlines()[-1][:90])
    except subprocess.TimeoutExpired:
        check("SPRT resume: pooling, fingerprint + offset refusals (FI-82)",
              False, "timeout")

# --- 5j2. FI-100 / FI-101 / FI-102: the measurement readers --------------- #
# Each decides whether a campaign's or a bench's numbers are trustworthy --
# engagement, whether the calibration held, and whether the work actually
# fell -- so their own parsers need a gate. Each ships a --selftest with a
# control it must fail on; run them the same way as 5j. FI-102's real
# acceptance controls need Linux perf and are NOT covered here; its --selftest
# gates the parser and the one-sided rule only, and says so.
for _script, _label in ((os.path.join("testing", "pair_identity.py"),
                         "pair identity: mirror pairs + ptnml (FI-100)"),
                        (os.path.join("testing", "nodes_calibration.py"),
                         "nodes calibration: bench vs operating (FI-101)"),
                        (os.path.join("bench", "instr_bench.py"),
                         "instr_bench: perf parse + one-sided rule (FI-102)"),
                        (os.path.join("bench", "ebf.py"),
                         "ebf: ln(nodes)~depth fit + refusal control (FI-108)")):
    if not os.path.exists(_script):
        continue
    try:
        r = subprocess.run([sys.executable, _script, "--selftest"],
                           capture_output=True, text=True, timeout=120)
        check(_label, r.returncode == 0,
              "" if r.returncode == 0
              else (r.stdout + r.stderr).strip().splitlines()[-1][:90])
    except subprocess.TimeoutExpired:
        check(_label, False, "timeout")

# --- 5j3. FI-89: repetition semantics vs the ARBITER ---------------------- #
# Cross-checked against python-chess's can_claim_threefold_repetition(), not
# against a reading of the rule. Runs with REP_STRICT both ways: the OFF column
# must keep disagreeing with the arbiter (or the test measures nothing) and the
# in-tree case must draw on the first match in BOTH modes. Subprocess -- it
# loads csearch.so through its own ctypes handle.
_t_rep = os.path.join("testing", "test_rep_strict.py")
if os.path.exists(_t_rep):
    try:
        r = subprocess.run([sys.executable, _t_rep],
                           capture_output=True, text=True, timeout=180)
        check("FI-89: repetition rule agrees with the arbiter (REP_STRICT)",
              r.returncode == 0,
              "" if r.returncode == 0
              else (r.stdout + r.stderr).strip().splitlines()[-1][:90])
    except subprocess.TimeoutExpired:
        check("FI-89: repetition rule agrees with the arbiter (REP_STRICT)",
              False, "timeout")

# --- 5j3b. cuci.py's WDL constants match data/wdl_model.json ------------- #
# cuci hardcodes the model because the PyInstaller binary ships without data/.
# That is the right call and it has now drifted from the json THREE times,
# each caught by eye a release or more later. The curves happened to agree to
# 0.2pp the last time; that was luck, not a guarantee. Compare the numbers.
try:
    import json as _json
    with open(os.path.join("data", "wdl_model.json"), encoding="utf-8") as _f:
        _wm = _json.load(_f)
    import cuci as _cu
    _same = (_cu._WDL_AS == _wm["as"] and _cu._WDL_BS == _wm["bs"]
             and _cu._WDL_PHASE_MAX == _wm["phase_max"]
             and _cu._WDL_PHASE_CLAMP_MIN == _wm["phase_clamp_min"])
    check("cuci WDL constants match data/wdl_model.json", _same,
          "" if _same else
          f"cuci as={_cu._WDL_AS[:2]}... json as={_wm['as'][:2]}... "
          f"-- run `python3 tuning/fit_wdl_model.py --sync-only`")
    # The NNUE pair is DORMANT (nothing reads it while USE_NNUE is False) but
    # is still pinned: an unread constant that has silently drifted is worse
    # than one that was never there, because the switch-on looks safe.
    _np = os.path.join("data", "wdl_model_nnue.json")
    if os.path.exists(_np):
        with open(_np, encoding="utf-8") as _f:
            _wn = _json.load(_f)
        _nsame = (_cu._WDL_AS_NNUE == _wn["as"] and _cu._WDL_BS_NNUE == _wn["bs"])
        check("cuci NNUE WDL constants match data/wdl_model_nnue.json (dormant)",
              _nsame, "" if _nsame else
              "-- run `python3 tuning/fit_wdl_model.py --sync-only`")
except FileNotFoundError:
    pass                                  # no json in this checkout: not a failure
except Exception as _ex:                  # noqa: BLE001
    check("cuci WDL constants match data/wdl_model.json", False, repr(_ex))

# --- 5j4. WDL adjudication corpus + per-family model ---------------------- #
# Both of these pin SILENT failures, which is exactly what a selftest is for.
# The family guard exists because NNUE logs were dropped from the WDL fit with
# no message (their base names carry no version number), and the per-family
# consumer because an NNUE arm was being adjudicated on hand-crafted-eval
# thresholds -- neither shows up as an error, only as a drifted draw rate.
# No --selftest arg: these run their own checks and exit non-zero on failure.
for _wdl_t, _wdl_label in (
        (os.path.join("testing", "test_wdl_family.py"),
         "WDL corpus is gated by eval FAMILY, not just era"),
        (os.path.join("testing", "test_wdl_nnue_model.py"),
         "WDL: --nnue fits its own model and match.py loads it per side")):
    if not os.path.exists(_wdl_t):
        continue
    try:
        r = subprocess.run([sys.executable, _wdl_t],
                           capture_output=True, text=True, timeout=180)
        check(_wdl_label, r.returncode == 0,
              "" if r.returncode == 0
              else (r.stdout + r.stderr).strip().splitlines()[-1][:90])
    except subprocess.TimeoutExpired:
        check(_wdl_label, False, "timeout")

# --- 5k. NNUE unit checks (FI-15, dormant build-out) --------------------- #
# Runs in a SUBPROCESS: cengine's FB-04 one-process-one-config rule forbids
# a second, differently-configured Engine in this process. Exit 42 = no net
# file on disk = SKIP (the build is dormant until a net is trained); the
# pinned ladder above is never touched (USE_NNUE stays False here).
if os.path.exists(os.path.join("NNUE", "selftest_nnue.py")):
    try:
        r = subprocess.run(
            [sys.executable, os.path.join("NNUE", "selftest_nnue.py")],
            capture_output=True, text=True, timeout=600)
        if r.returncode == 42:
            print("\n  skip  NNUE checks (no net file -- dormant FI-15 build)")
        else:
            tail = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
            check("NNUE unit checks (toy net, subprocess)",
                  r.returncode == 0, tail)
    except subprocess.TimeoutExpired:
        check("NNUE unit checks (toy net, subprocess)", False, "timeout")

# --- 6. optional pieces: report, don't fail ------------------------------ #
print("\noptional:")
print(f"  {'ok  ' if os.path.exists('data/wdl_model.json') else 'none'}  data/wdl_model.json "
      "(match.py adjudication; tuning/fit_wdl_model.py writes it)")
for book in ("data/UHO_4060_v4.epd", "data/fen.txt"):
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
