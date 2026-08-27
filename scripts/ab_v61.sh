#!/bin/bash
# The v61 candidates against the shipped engine. Run: ./scripts/ab_v61.sh
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
run v61a_hash    NNUE/shims/engine_v61a_hash.py
run v61b_deadtag NNUE/shims/engine_v61b_deadtag.py
run v61c_both    NNUE/shims/engine_v61c_both.py
echo "### ALL V61 CANDIDATES DONE $(TZ=Europe/Zurich date '+%H:%M:%S')"
