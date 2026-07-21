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
CE_LADDER = {                     # depth -> (nodes, score)  [v52: FI-24(a)+(b) null refinements ON -- re-measured 2026-07-21; no-double-null + eval-scaled R reshape the deep tree (d10-d12 scores drift, d12 +59% nodes as suppressed nulls hand work back to real search). TT_BITS=23 (192 MB) unchanged]
    1: (95, 126), 2: (180, 126), 3: (378, 126), 4: (921, 122),
    5: (6111, 73), 6: (11724, 73), 7: (20425, 74), 8: (48469, 72),
    9: (81605, 75), 10: (162637, 53), 11: (290593, 53), 12: (717955, 58),
    13: (1225434, 75), 14: (1716693, 97),
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
    except Exception as ex:
        check("C core (cengine) searches", False,
              f"{type(ex).__name__}: {ex} -- rebuild csearch.so via ./setup.sh")

# --- 5h. cuci UCI host: protocol round-trip ------------------------------ #
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

# --- 5i. NNUE unit checks (FI-15, dormant build-out) --------------------- #
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
