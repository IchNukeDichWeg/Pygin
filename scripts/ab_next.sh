#!/bin/bash
# Open candidates against the shipped engine (v60). Run: ./scripts/ab_next.sh
#
# These are CANDIDATES, not a release. Nothing has confirmed since v60: the
# two gw nets were rejected and the static-hash arm measured null, so a "v61"
# today would contain FI-113 alone (+1.09% NPS = ~+1 Elo) and is not worth
# cutting. A version number gets assigned when something here confirms.
#
# 10+0.1 ONLY. 50+0.5 is a separate decision and a separate campaign -- these
# same shim files serve it unchanged when that time comes.
#
# Instrument: seed 59, UHO_4060_v4, offset 0, cores/2 workers -- identical to
# every screen since s1, so results pool. Budget resolves ~+9 Elo and up.
# A bound-stopped run is a VERDICT ONLY; its Elo is biased away from zero.
#
# ORDER MATTERS: A and B are single-variable and run first. C changes two
# things at once and is only interpretable once A and B have reported.
set -u
cd "$(dirname "$0")/.."
LOGDIR="${LOGDIR:-$PWD}"
CORES=$(nproc); WORKERS=$((CORES / 2)); [ "$WORKERS" -lt 1 ] && WORKERS=1
echo "workers: $WORKERS (cores $CORES / 2 -- one core per engine)"
BASE=NNUE/shims/engine_nnue_v12.py
run () {
    L=$1; A=$2
    [ -f "$A" ] && [ -f "$BASE" ] || { echo "missing engine file"; exit 1; }
    echo "=== $L  $(TZ=Europe/Zurich date '+%H:%M:%S') ==="
    python3 match.py "$A" "$BASE" 5000 0 \
        --workers "$WORKERS" --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500 \
        --sprt-resume "sprt_${L}_tc10+0.1.json" \
        2>&1 | tee "$LOGDIR/ab_$L.log"
    echo "### $L DONE $(TZ=Europe/Zurich date '+%H:%M:%S')"
}
# v61a_hash IS the hash_small experiment and it is ALREADY ANSWERED: static
# 24 MiB vs 192 MiB measured NULL over the full 10,000 games @10+0.1 on
# 2026-08-28. Re-running it would buy nothing, so it is dropped, and v61c
# (small hash + deadtag) collapses to v61b at a smaller table -- also dropped.
# What replaces them is the item that null does NOT cover: a table that GROWS.
# deadtag_192 is NOT tonight's deadtag run: tonight tests it at 24 MiB, this
# tests it at the shipped 192 MiB.
run grow         NNUE/shims/engine_grow_on.py
run deadtag_192  NNUE/shims/engine_v61b_deadtag.py
echo "### ALL CANDIDATES DONE $(TZ=Europe/Zurich date '+%H:%M:%S')"
