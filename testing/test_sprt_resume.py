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
fp = match.sprt_fingerprint(os.path.join(ROOT, "cengine.py"),
                            os.path.join(ROOT, "engine.py"),
                            CFG, MODE_CFG, match.FEN_FILE,
                            match.SUBSET_SEED, False)

acc = [0] * 5
for i, t in enumerate(tranches):
    acc = [acc[k] + t[k] for k in range(5)]
    match.sprt_resume_save(state, fp, acc, (i + 1) * 5000,
                           {"decided": None, "llr": 1.0})
    if i + 1 < len(tranches):
        base, nxt, _note = match.sprt_resume_load(state, fp, (i + 1) * 5000)
        check(f"tranche {i + 2} resumes with {sum(base):,} pooled pairs",
              base == acc and nxt == (i + 1) * 5000,
              f"penta {base}, next_offset {nxt}")

base, _nxt, _note = match.sprt_resume_load(state, fp, 20000)
check("final pooled state equals the published ptnml", base == FI30,
      f"{base}")

# --- 3. the two refusals ------------------------------------------------- #
# A fingerprint mismatch must REFUSE, not warn: this is the FI-30 tranche-4
# failure (games played against a reverted checkout, caught only by a reflog).
bad = dict(fp, e1="deadbeefdeadbeef")
try:
    match.sprt_resume_load(state, bad, 20000)
    check("refuses a different engine build", False, "pooled anyway!")
except ValueError as ex:
    check("refuses a different engine build", "e1" in str(ex), str(ex)[:70])

for field, value in (("seed", 999), ("fen_sha", "0000000000000000"),
                     ("tc", [10.0, 0.1]), ("cfg", dict(CFG, elo1=8.0))):
    try:
        match.sprt_resume_load(state, dict(fp, **{field: value}), 20000)
        check(f"refuses a changed {field}", False, "pooled anyway!")
    except ValueError as ex:
        check(f"refuses a changed {field}", field in str(ex), str(ex)[:70])

# An overlapping offset double-counts openings: same evidence, twice the
# apparent sample. The disjoint-shard guarantee is the reason offsets exist.
try:
    match.sprt_resume_load(state, fp, 19999)
    check("refuses an overlapping offset", False, "pooled anyway!")
except ValueError as ex:
    check("refuses an overlapping offset", "overlaps" in str(ex), str(ex)[:70])

base, _nxt, _note = match.sprt_resume_load(state, fp, 20000)
check("accepts the exact next_offset", sum(base) == sum(FI30))

# --- 4. fingerprint sensitivity ------------------------------------------ #
# Identity must NOT include the .so (every box builds its own -mcpu=native
# copy, and cross-box pooling is the point) but MUST include the FEN pool's
# content -- a same-named pool with different lines breaks disjointness.
fp2 = match.sprt_fingerprint(os.path.join(ROOT, "cengine.py"),
                             os.path.join(ROOT, "engine.py"),
                             CFG, MODE_CFG, match.FEN_FILE,
                             match.SUBSET_SEED, False)
check("fingerprint is stable across calls", fp == fp2)
check("fingerprint pins engine CONTENT, not path",
      fp["e1"] != fp["e2"] and len(fp["e1"]) == 16,
      f"e1={fp['e1']} e2={fp['e2']}")
check("fingerprint pins the FEN pool's content",
      fp["fen_sha"] is not None and fp["fen_file"] == os.path.basename(
          match._data_path(match.FEN_FILE)),
      f"{fp['fen_file']} @ {fp['fen_sha']}")

# A corrupt file must not be pooled as zeros.
with open(state, "w", encoding="utf-8") as fh:
    json.dump({"fingerprint": fp, "penta": [1, 2, -3, 4, 5],
               "next_offset": 0}, fh)
try:
    match.sprt_resume_load(state, fp, 0)
    check("refuses a corrupt penta", False, "pooled anyway!")
except ValueError as ex:
    check("refuses a corrupt penta", "corrupt" in str(ex), str(ex)[:70])

for f in os.listdir(tmp):
    os.remove(os.path.join(tmp, f))
os.rmdir(tmp)

print()
if _fails:
    print(f"== FAILED: {len(_fails)} check(s) ==")
    sys.exit(1)
print("== ALL CHECKS PASSED ==")
