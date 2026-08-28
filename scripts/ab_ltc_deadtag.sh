#!/bin/bash
# THE run that decides whether FI-115 ships, and produces a REAL Elo for it.
#   ./scripts/ab_ltc_deadtag.sh
#
#   engine_v61b_deadtag (192 MiB, tag ON)  vs  engine_nnue_v12 (192 MiB, OFF)
#   50+0.5, seed 59, UHO_4060_v4, cores/2 workers
#
# TWO PHASES, and the second is the point.
#   Phase 1  SPRT to a decision. Cheap, stops early, gives a VERDICT.
#   Phase 2  FIXED BUDGET, no early stop, fresh openings. Gives the NUMBER.
# Phase 2 only runs if phase 1 accepts, so a reject costs one phase instead
# of a day. A run that stops AT a bound is taken at a favourable fluctuation
# by construction: fine as a verdict, worthless as an effect size. Every Elo
# quoted for FI-115 must come from phase 2.
#
# WHY 192 MiB, ENFORCED BELOW RATHER THAN ASSUMED. FI-115 was confirmed on
# 2026-08-28 at 10+0.1 with 24 MiB on BOTH sides -- not what the engine
# ships. The small table was picked to maximise replacement pressure, since a
# victim rule can only pay where entries are evicted, so that accept came
# from the regime built to favour it. The preflight below refuses to start
# unless both arms really are 192 MiB, because that mistake already happened
# once and cost a campaign's worth of confidence.
#
# WHY NOT 192 MiB AT 10+0.1. The table would never fill: the FI-116 ramp
# measurement put a 192 MiB table at roughly a 22-bit working set by ply 40
# at 0.15s/move, so nothing is evicted and a null would mean "no pressure to
# relieve" rather than "the rule does not work".
#
# WHY 50+0.5. The recorded hashfull curve (cengine.py) has 192 MiB at 833
# permille by ply 8 at 1.4s/move and saturated from ply 16; 50+0.5 is longer
# still. Every store after the opening evicts something, so the victim rule
# is doing real work -- and it is the TC the ledger is denominated in.
#
# SEPARATE INSTRUMENT, SEPARATE STATE. These files never pool with the
# 10+0.1 runs: different clock, different table size, different experiment.
#
# COST: ~6-7h per 10,000-game tranche at 48 workers. Phase 1 is 1-2 tranches,
# phase 2 is 20,000 games in one run (~13h) for a +/-3.1 Elo margin.
set -u
cd "$(dirname "$0")/.."
LOGDIR="${LOGDIR:-$PWD}"
CORES=$(nproc); WORKERS=$((CORES / 2)); [ "$WORKERS" -lt 1 ] && WORKERS=1

A=NNUE/shims/engine_v61b_deadtag.py
B=NNUE/shims/engine_nnue_v12.py
S="sprt_deadtag192_tc50+0.5.json"
for f in "$A" "$B"; do [ -f "$f" ] || { echo "missing $f"; exit 1; }; done

# ---- HARD GATE: 192 MiB both sides, tag on exactly one -------------------
echo "### PREFLIGHT"
python3 - "$A" "$B" <<'PY' || { echo "### REFUSING TO RUN"; exit 1; }
import sys, importlib.util as u
want_bits, want_mib = 23, 192
tags = []
for p in sys.argv[1:]:
    sp = u.spec_from_file_location("m", p); m = u.module_from_spec(sp)
    sp.loader.exec_module(m)
    E = m.Engine
    bits = E.TT_BITS
    mib = (1 << bits) * 24 // 2**20
    tag = bool(getattr(E, "TT_DEADTAG", False))
    grow = bool(getattr(E, "TT_GROW", False))
    ok = bits == want_bits and mib == want_mib and not grow
    print(f"  {'OK ' if ok else 'BAD'}  {p.split('/')[-1]:26s} "
          f"TT_BITS={bits} = {mib} MiB  DEADTAG={tag}  GROW={grow}")
    if not ok:
        sys.exit(f"  expected TT_BITS={want_bits} ({want_mib} MiB), TT_GROW off")
    tags.append(tag)
if tags != [True, False]:
    sys.exit(f"  expected the tag ON in slot 1 and OFF in slot 2, got {tags}")
print("  192 MiB on both sides, tag is the only variable -- correct.")
PY

MAX_TRANCHES=4
STOP=0
trap 'STOP=1; echo ""; echo "### INTERRUPTED -- stopping after this tranche"' INT
field () { python3 -c "
import json, os
f = '$S'
print(json.load(open(f)).get('$1') if os.path.isfile(f) else '$2')"; }

# ---- PHASE 1: verdict ----------------------------------------------------
echo ""
echo "### PHASE 1 -- SPRT to a decision (verdict only, NOT an Elo)"
T=0
while [ "$T" -lt "$MAX_TRANCHES" ]; do
    D=$(field decision None)
    [ "$D" != "None" ] && { echo "### ALREADY DECIDED: $D"; break; }
    OFF=$(field next_offset 0); T=$((T + 1))
    echo ""
    echo "=== phase1 tranche $T/$MAX_TRANCHES  offset $OFF  $(TZ=Europe/Zurich date '+%H:%M:%S') ==="
    python3 match.py "$A" "$B" 5000 "$OFF" \
        --workers "$WORKERS" --tc 50+0.5 --seed 59 \
        --sprt --sprt-min-pairs 1500 --sprt-resume "$S" \
        2>&1 | tee -a "$LOGDIR/ab_deadtag192_phase1.log"
    echo "### tranche $T: $(field pairs 0) pairs, LLR $(field llr 0), decision $(field decision None)"
    [ "$(field decision None)" != "None" ] && break
    [ "$STOP" -eq 1 ] && break
done
D=$(field decision None)
echo ""
echo "### PHASE 1 VERDICT: $D  ($(field pairs 0) pairs, LLR $(field llr 0))"
if [ "$D" != "H1" ]; then
    echo "### Not an accept -- phase 2 skipped. Nothing here is quotable as Elo."
    exit 0
fi

# ---- PHASE 2: the number -------------------------------------------------
OFF=$(field next_offset 0)
echo ""
echo "### PHASE 2 -- FIXED BUDGET 20,000 games from offset $OFF, NO early stop."
echo "### This, and only this, is the Elo that may be quoted for FI-115."
python3 match.py "$A" "$B" 10000 "$OFF" \
    --workers "$WORKERS" --tc 50+0.5 --seed 59 \
    2>&1 | tee "$LOGDIR/ab_deadtag192_phase2.log"

echo ""
echo "### FI-115 FINAL, 192 MiB, 50+0.5, fixed budget $(TZ=Europe/Zurich date '+%H:%M:%S')"
python3 - "$LOGDIR/ab_deadtag192_phase2.log" <<'PY'
import re, sys
sys.path.insert(0, "testing")
import sprt
txt = open(sys.argv[1], errors="ignore").read()
elo = re.findall(r"Engine 1 score:.*=>\s*([-+0-9.]+ \+/- [0-9.]+ Elo)", txt)
pen = re.findall(r"^Ptnml:\s*([0-9,\s]+)$", txt, re.M)
if elo:
    print(f"  Elo (unbiased, fixed budget): {elo[-1]}")
if pen:
    c = [int(x) for x in pen[-1].replace(",", " ").split()]
    r = sprt.evaluate(c, elo0=0.0, elo1=4.0, model="normalized")
    llr = r["llr"] if isinstance(r, dict) else r
    print(f"  Ptnml: {c}")
    print(f"  LLR on the SAME data (post-hoc, [0,4] normalized): {llr:+.3f}")
print("  A fixed-budget Elo is unbiased; the phase-1 LLR is a verdict only.")
PY
