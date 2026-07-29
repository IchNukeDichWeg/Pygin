#!/usr/bin/env python3
"""FI-82: --sprt-resume tranche pooling.

    python3 testing/test_sprt_resume.py

Drives match.py's resume helpers directly (no games played, seconds to run).
The thing being protected is the POOLED STATISTIC: a campaign that runs to
decision spends tranches on different boxes and days, and the two ways to
corrupt it are pooling the wrong experiment (FI-30's tranche 4 ran on the
wrong checkout) and pooling overlapping shards (the same evidence counted
twice). Both must be refusals, not warnings.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import match                                                   # noqa: E402
import sprt                                                    # noqa: E402

_fails = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        _fails.append(label)


CFG = {"elo0": 0.0, "elo1": 4.0, "alpha": 0.05, "beta": 0.05,
       "model": "normalized"}
MODE_CFG = {"mode": "clock", "time_ms": None, "depth": None, "nodes": None,
            "tc_seconds": 50.0, "tc_increment": 0.2}

print("== FI-82 SPRT resume ==\n")

# --- 1. FI-30's published verdict, rebuilt from four tranches ------------- #
# The ledger records the campaign as four pooled tranches = 21,605 games,
# ptnml 606/2518/4309/2714/655, GSPRT[0,4] LLR +3.475 > +2.944 ACCEPT. Pooling
# is addition, so four arbitrary splits of that ptnml must reach the same LLR
# -- and no single tranche may reach it alone (that is the whole point of
# continuing a sequential test rather than restarting it).
FI30 = [606, 2518, 4309, 2714, 655]
tranches = [[151, 629, 1077, 678, 163],
            [151, 629, 1077, 678, 164],
            [152, 630, 1077, 679, 164],
            [152, 630, 1078, 679, 164]]
pooled = [sum(t[k] for t in tranches) for k in range(5)]
check("four tranches sum to FI-30's published ptnml", pooled == FI30,
      f"{pooled} vs {FI30}")

r = sprt.evaluate(pooled, CFG["elo0"], CFG["elo1"], CFG["model"],
                  CFG["alpha"], CFG["beta"])
check("pooled LLR reproduces FI-30's +3.475 ACCEPT",
      abs(r["llr"] - 3.475) < 0.001 and r["decision"] == "H1"
      and abs(r["upper"] - 2.9444) < 0.001,
      f"LLR {r['llr']:+.4f} vs bound {r['upper']:+.4f} -> {r['decision']}")

singles = [sprt.evaluate(t, CFG["elo0"], CFG["elo1"], CFG["model"],
                         CFG["alpha"], CFG["beta"]) for t in tranches]
check("no single tranche decides on its own",
      all(s["decision"] == "continue" for s in singles),
      "LLRs " + ", ".join(f"{s['llr']:+.2f}" for s in singles))

# --- 2. state round-trip: what a resumed run actually pools --------------- #
tmp = tempfile.mkdtemp(prefix="fi82_")
state = os.path.join(tmp, "ab.json")

acc = [0] * 5
for i, t in enumerate(tranches):
    acc = [acc[k] + t[k] for k in range(5)]
    match.sprt_resume_save(state, acc, (i + 1) * 5000,
                           {"decided": None, "llr": 1.0},
                           [{"engine1": "cengine", "engine2": "engine",
                             "mode": "nodes 1750000/move", "smp": 1,
                             "seed": 55, "fen_file": "UHO_4060_v4.epd",
                             "offset": i * 5000}])
    if i + 1 < len(tranches):
        base, nxt, runs, _note = match.sprt_resume_load(state, (i + 1) * 5000)
        check(f"tranche {i + 2} resumes with {sum(base):,} pooled pairs",
              base == acc and nxt == (i + 1) * 5000,
              f"penta {base}, next_offset {nxt}")
        check(f"tranche {i + 2} carries its provenance", len(runs) == 1,
              f"{len(runs)} run record(s)")

base, _nxt, _runs, _note = match.sprt_resume_load(state, 20000)
check("final pooled state equals the published ptnml", base == FI30,
      f"{base}")

# --- 3. WARN, never refuse ----------------------------------------------- #
# The fingerprint gate is GONE (2026-07-29, user's call). It refused to pool
# unless every config field AND the sha256 of both engine .py files matched --
# right for a certification suite, wrong for a research tool: a COMMENT added
# to cengine.py changed its hash and orphaned a campaign's prior games over a
# difference that cannot affect a single move. The contract is now: pool, and
# say what moved. Provenance is RECORDED so a pooled figure stays auditable.
base, nxt, runs, note = match.sprt_resume_load(state, 20000)
check("pools without any fingerprint argument", sum(base) == sum(FI30),
      f"{sum(base):,} pairs")
check("sprt_fingerprint is gone", not hasattr(match, "sprt_fingerprint"),
      "it still exists -- the gate was only half removed")

# An overlapping offset double-counts openings: same evidence, twice the
# apparent sample. It is the ONE thing worth shouting about, and it is still
# only a WARNING in the note -- the operator decides.
_b, _n, _r, note = match.sprt_resume_load(state, 19999)
check("WARNS on an overlapping offset", "OVERLAP WARNING" in note,
      note[:90])
check("names the offset to use instead", "20000" in note, note[:90])
_b, _n, _r, note_ok = match.sprt_resume_load(state, 20000)
check("silent at the exact next_offset", "OVERLAP" not in note_ok,
      note_ok[:90])

# A recorded decision is surfaced, not hidden: adding to a decided file is
# legitimate (more evidence) but must never look like a fresh test.
match.sprt_resume_save(state, FI30, 20000,
                       {"decided": "H1", "llr": 3.475}, [])
_b, _n, _r, note = match.sprt_resume_load(state, 20000)
check("surfaces an existing decision", "H1" in note, note[:90])

# --- 4. corrupt data is STILL fatal -------------------------------------- #
# Removing the config gate does not mean trusting the numbers. A bad penta
# silently poisons the statistic, which is not a choice an operator makes.
for penta, why in (([1, 2, -3, 4, 5], "negative"), ([1, 2, 3], "short")):
    with open(state, "w", encoding="utf-8") as fh:
        json.dump({"penta": penta, "next_offset": 0}, fh)
    try:
        match.sprt_resume_load(state, 0)
        check(f"refuses a corrupt penta ({why})", False, "pooled anyway!")
    except ValueError as ex:
        check(f"refuses a corrupt penta ({why})", "corrupt" in str(ex),
              str(ex)[:70])

# A file with no `runs` key at all (hand-written, or pre-dating provenance)
# must load, not crash -- the seed file for FI-107 was written by hand.
with open(state, "w", encoding="utf-8") as fh:
    json.dump({"penta": FI30, "next_offset": 20000}, fh)
base, nxt, runs, _note = match.sprt_resume_load(state, 20000)
check("loads a hand-written file with no provenance",
      base == FI30 and runs == [], f"penta {base}, runs {runs}")

for f in os.listdir(tmp):
    os.remove(os.path.join(tmp, f))
os.rmdir(tmp)

print()
if _fails:
    print(f"== FAILED: {len(_fails)} check(s) ==")
    sys.exit(1)
print("== ALL CHECKS PASSED ==")
