#!/bin/bash
# Open candidates against the shipped engine (v60). Run: ./scripts/ab_next.sh
# Run ./scripts/box_setup.sh FIRST -- it restores the deadtag state this
# script resumes from, and a missing state file silently restarts that
# experiment from zero instead of continuing it.
#
# These are CANDIDATES, not a release. Nothing has confirmed since v60: the
# two gw nets were rejected and the static-hash arm measured null, so a "v61"
# today would contain FI-113 alone (+1.09% NPS = ~+1 Elo) and is not worth
# cutting. A version number gets assigned when something here confirms.
#
# 10+0.1 ONLY. 50+0.5 is a separate decision and a separate campaign -- these
# same shim files serve it unchanged when that time comes.
#
# Instrument: seed 59, UHO_4060_v4, cores/2 workers -- identical to every
# screen since s1, so results pool. Budget resolves ~+9 Elo and up. A
# bound-stopped run is a VERDICT ONLY; its Elo is biased away from zero.
set -u
cd "$(dirname "$0")/.."
LOGDIR="${LOGDIR:-$PWD}"
CORES=$(nproc); WORKERS=$((CORES / 2)); [ "$WORKERS" -lt 1 ] && WORKERS=1
echo "workers: $WORKERS (cores $CORES / 2 -- one core per engine)"
echo "NOTE: density is part of the instrument; these pool only with campaigns"
echo "      run at the SAME density."
V12=NNUE/shims/engine_nnue_v12.py

run () {   # run <label> <engineA> <engineB> <offset>
    L=$1; A=$2; B=$3; OFF=$4
    for f in "$A" "$B"; do [ -f "$f" ] || { echo "missing $f"; exit 1; }; done
    echo "=== $L  offset $OFF  $(TZ=Europe/Zurich date '+%H:%M:%S') ==="
    python3 match.py "$A" "$B" 5000 "$OFF" \
        --workers "$WORKERS" --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500 \
        --sprt-resume "sprt_${L}_tc10+0.1.json" \
        2>&1 | tee "$LOGDIR/ab_$L.log"
    echo "### $L DONE $(TZ=Europe/Zurich date '+%H:%M:%S')"
}

# 1. FIRST, because it is the only run that can resolve something tonight.
# The 2026-08-28 campaign left deadtag at LLR +2.402 of the +2.944 it needs,
# still climbing when the 10,000-game budget ran out. Doctrine: never cap a
# sequential test still trending toward a bound. This CONTINUES that test --
# offset 5000 because the state file refuses to reuse spent openings, and
# --sprt-resume pools onto the 5,000 pairs already paid for. Deleting
# sprt_deadtag_tc10+0.1.json here would silently throw those away.
run deadtag NNUE/shims/engine_deadtag_on.py NNUE/shims/engine_deadtag_off.py 5000

# 2. FI-116, the growing TT: starts at 24 MiB and widens at 75% full. The
# static 24-vs-192 null does NOT cover this -- a narrow window costs nothing
# while the table is underfilled, and at 10+0.1 the ramp lasts the whole game.
run grow NNUE/shims/engine_grow_on.py "$V12" 0

# 3. Deadtag at the SHIPPED 192 MiB rather than the 24 MiB run 1 uses.
# Only worth its slot if run 1 confirms; a rejected toggle needs no sequel.
run deadtag_192 NNUE/shims/engine_v61b_deadtag.py "$V12" 0

echo "### ALL CANDIDATES DONE $(TZ=Europe/Zurich date '+%H:%M:%S')"
