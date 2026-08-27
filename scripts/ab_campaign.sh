#!/bin/bash
# Four A/Bs, one box, one instrument. Run: ./scripts/ab_campaign.sh
#
# INSTRUMENT, fixed: seed 59, 10+0.1, UHO_4060_v4, offset 0, cores/2 workers
# -- byte-identical to the s1-s4/m1-m4/g1-g3 screens, so the net results POOL
# with them. Worker density is part of the instrument, not a perf knob: a game
# runs TWO engines, so cores/2 is one core each, and NPS at a fixed clock IS
# playing strength (48 workers 2.28M nps vs 111 workers 1.02M).
#
# 5,000 positions = 10,000 games each. Our budget resolves down to ~9 Elo;
# below that a run cannot reach a bound. --sprt-min-pairs 1500 stops a clear
# loser near 3,000 games. NOTE: a bound-stopped run is a VERDICT ONLY -- its
# Elo is biased away from zero by construction and is not a quotable effect
# size. Only a full-budget run gives a number worth writing down.
set -u
cd "$(dirname "$0")/.."
LOGDIR="${LOGDIR:-$PWD}"
CORES=$(nproc); WORKERS=$((CORES / 2)); [ "$WORKERS" -lt 1 ] && WORKERS=1
echo "workers: $WORKERS (cores $CORES / 2 -- one core per engine)"
echo "NOTE: density is part of the instrument; these pool only with campaigns"
echo "      run at the SAME density. Sanity-check that before trusting them."
echo ""

run () {   # run <label> <engineA> <engineB>
    L=$1; A=$2; B=$3
    for f in "$A" "$B"; do [ -f "$f" ] || { echo "missing $f"; exit 1; }; done
    echo "=== $L  $(TZ=Europe/Zurich date '+%H:%M:%S') ==="
    python3 match.py "$A" "$B" 5000 0 \
        --workers "$WORKERS" --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500 \
        --sprt-resume "sprt_${L}_tc10+0.1.json" \
        2>&1 | tee "$LOGDIR/ab_$L.log"
    echo "### $L DONE $(TZ=Europe/Zurich date '+%H:%M:%S')"
}

S=NNUE/shims
# 1-2: the new nets vs the shipped net. gw6r is omitted ON PURPOSE -- it is
# byte-identical to gw6 (same sha256), so testing gw6 tests both.
run gw5_vs_v12  $S/engine_nnue_gw5.py  $S/engine_nnue_v12.py
run gw6_vs_v12  $S/engine_nnue_gw6.py  $S/engine_nnue_v12.py
# 3: TT size. Same net both sides; only TT_BITS differs.
run hash_small  $S/engine_hash_small.py $S/engine_hash_default.py
# 4: FI-115 dead-entry tag. Same net, same table size; only the victim rule.
run deadtag     $S/engine_deadtag_on.py $S/engine_deadtag_off.py

echo "### ALL FOUR A/B DONE $(TZ=Europe/Zurich date '+%H:%M:%S')"
