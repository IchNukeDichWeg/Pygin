#!/bin/bash
# Run each open candidate at 10+0.1 until the SPRT DECIDES -- accept or
# reject, not "budget spent". Run ./scripts/box_setup.sh first; it restores
# the deadtag state this script resumes from, and a missing state file
# silently restarts that experiment from zero instead of continuing it.
#
#   ./scripts/ab_next.sh
#
# TWO candidates, both of them TT items:
#   deadtag  FI-115, continuing from LLR +2.402 of the +2.944 it needs
#   grow     FI-116, the table that starts at 24 MiB and widens at 75% full
# hash_small is NOT here and is not coming back: it measured null over the
# full 10,000 games, and a statically smaller table is just a smaller table.
# The growing one is the version of that idea worth measuring.
#
# 10+0.1 ONLY. Instrument: seed 59, UHO_4060_v4, cores/2 workers, identical
# to every screen since s1 so results pool. Each tranche is 5,000 positions
# = 10,000 games and starts at the offset the state file demands, so no
# opening is ever played twice across tranches.
#
# A bound-stopped run is a VERDICT ONLY -- its Elo is biased away from zero
# by construction and is not a quotable effect size. Re-run to a fixed
# budget if the magnitude ever needs writing down.
set -u
cd "$(dirname "$0")/.."
LOGDIR="${LOGDIR:-$PWD}"
CORES=$(sysctl -n hw.ncpu 2>/dev/null || nproc); WORKERS=$((CORES / 2)); [ "$WORKERS" -lt 1 ] && WORKERS=1
echo "workers: $WORKERS (cores $CORES / 2 -- one core per engine)"
echo "NOTE: density is part of the instrument; these pool only with campaigns"
echo "      run at the SAME density."

# Safety net, not a target. 12 tranches is 120,000 games per candidate, well
# past where a real effect declares itself; hitting it means the true value
# sits so close to the middle of [0, 4] that the walk will not commit, which
# is itself the answer. The book holds 241,670 positions = 48 tranches, so
# this cap binds first and openings never run out.
MAX_TRANCHES=12
STOP=0
trap 'STOP=1; echo ""; echo "### INTERRUPTED -- stopping after this tranche"' INT

state_field () { python3 -c "
import json, os, sys
f = '$1'
print(json.load(open(f)).get('$2') if os.path.isfile(f) else '$3')"; }

run_until_decision () {   # run_until_decision <label> <engineA> <engineB>
    L=$1; A=$2; B=$3
    S="sprt_${L}_tc10+0.1.json"
    for f in "$A" "$B"; do [ -f "$f" ] || { echo "missing $f"; exit 1; }; done
    T=0
    while [ "$T" -lt "$MAX_TRANCHES" ]; do
        D=$(state_field "$S" decision None)
        if [ "$D" != "None" ]; then
            echo "### $L ALREADY DECIDED: $D"; return
        fi
        OFF=$(state_field "$S" next_offset 0)
        T=$((T + 1))
        echo ""
        echo "=== $L tranche $T/$MAX_TRANCHES  offset $OFF  $(TZ=Europe/Zurich date '+%H:%M:%S') ==="
        # -a: tranches ACCUMULATE in one log. Truncating here would leave the
        # last tranche looking like the whole experiment.
        python3 match.py "$A" "$B" 5000 "$OFF" \
            --workers "$WORKERS" --tc 10+0.1 --seed 59 \
            --sprt --sprt-min-pairs 1500 --sprt-resume "$S" \
            2>&1 | tee -a "$LOGDIR/ab_$L.log"
        P=$(state_field "$S" pairs 0)
        D=$(state_field "$S" decision None)
        L_=$(state_field "$S" llr 0)
        echo "### $L tranche $T done: $P pooled pairs, LLR $L_, decision $D"
        if [ "$D" != "None" ]; then
            echo "### $L DECIDED: $D  $(TZ=Europe/Zurich date '+%H:%M:%S')"
            return
        fi
        [ "$STOP" -eq 1 ] && { echo "### $L STOPPED BY USER at $P pairs"; return; }
    done
    echo "### $L HIT THE $MAX_TRANCHES-TRANCHE CAP UNDECIDED -- that is the result:"
    echo "###   the effect sits too close to the middle of [0, 4] to commit."
}

# deadtag FIRST: it is already 82% of the way to a bound, so it is the run
# most likely to actually decide something. Its state file carries 5,000
# pairs; --sprt-resume pools onto them and the offset skips spent openings.
run_until_decision deadtag \
    NNUE/shims/engine_deadtag_on.py NNUE/shims/engine_deadtag_off.py
[ "$STOP" -eq 1 ] || run_until_decision grow \
    NNUE/shims/engine_grow_on.py NNUE/shims/engine_nnue_v12.py

echo ""
echo "### ALL CANDIDATES DONE $(TZ=Europe/Zurich date '+%H:%M:%S')"
for L in deadtag grow; do
    S="sprt_${L}_tc10+0.1.json"
    [ -f "$S" ] && python3 -c "
import json; d = json.load(open('$S'))
print(f\"  {'$L':10s} {d['pairs']:6,d} pairs  LLR {d['llr']:+.3f}  decision {d['decision']}\")"
done
