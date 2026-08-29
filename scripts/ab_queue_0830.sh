#!/bin/bash
# The 2026-08-30 queue: six search shims from the v60 audit plus the three
# v13 net seeds, each run at 10+0.1 until the SPRT DECIDES. Run
# ./scripts/box_setup.sh first -- it verifies all ten arms resolve their nets,
# and an arm that silently loads the wrong net produces a whole campaign of
# garbage that looks fine.
#
#   ./scripts/ab_queue_0830.sh
#
# ORDER IS PRIORITY, not convenience. P1 and P3 are the cheapest high-prior
# slots on the board; the nets answer the corpus question and cost three runs
# to say anything at all.
#
# P4 IS CONDITIONAL AND IS NOT MEASURED AGAINST THE BASELINE. It only runs if
# P3 accepted, and it runs against the P3 shim -- ttPv discount on top of
# cutnode LMR is the hypothesis, and batching the two would measure the pair
# while telling you nothing about which half paid.
#
# NOT HERE, DELIBERATELY: p2_improving (two confirmed defects would be
# measured as part of the recipe -- an inverted futility sign and mixed eval
# families in the improving stack) and s14_repstrict (its reset premise was
# disproven; the -20.17 screen-kill already measured that rule).
#
# 10+0.1 ONLY, and it decides INCLUSION, never magnitude. A bound-stopped run
# is a verdict: its Elo is biased away from zero by construction, which is how
# +33.83 became +19.11. Anything that accepts gets priced separately at
# 50+0.5 on a fixed budget, and that is the number that ships.
#
# Instrument: seed 59, UHO_4060_v4, cores/2 workers, identical to every screen
# since s1 so results pool. Density is part of the instrument.
#
# STATE CANNOT BE PUSHED FROM A RENTED BOX (no credentials). scp the
# sprt_*.json files back and commit them locally the turn a run ends -- a
# stale state file silently restarts an experiment from zero.
set -u
cd "$(dirname "$0")/.."
LOGDIR="${LOGDIR:-$PWD}"
CORES=$(nproc); WORKERS=$((CORES / 2)); [ "$WORKERS" -lt 1 ] && WORKERS=1
echo "workers: $WORKERS (cores $CORES / 2 -- one core per engine)"
echo "NOTE: density is part of the instrument; these pool only with campaigns"
echo "      run at the SAME density."

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

BASE=NNUE/shims/engine_v61b_deadtag.py

run_until_decision p1_corrhist NNUE/shims/engine_p1_corrhist.py "$BASE"
[ "$STOP" -eq 1 ] || run_until_decision p3_cutlmr NNUE/shims/engine_p3_cutlmr.py "$BASE"
[ "$STOP" -eq 1 ] || run_until_decision v13s1 NNUE/shims/engine_v13s1.py "$BASE"
[ "$STOP" -eq 1 ] || run_until_decision v13s2 NNUE/shims/engine_v13s2.py "$BASE"
[ "$STOP" -eq 1 ] || run_until_decision v13s3 NNUE/shims/engine_v13s3.py "$BASE"

# P4 only on a P3 accept, and against the P3 shim.
if [ "$STOP" -eq 0 ]; then
    P3D=$(state_field "sprt_p3_cutlmr_tc10+0.1.json" decision None)
    case "$P3D" in
        *H1*|*accept*|*ACCEPT*)
            echo ""; echo "### P3 accepted ($P3D) -- P4 is live, vs the P3 shim"
            run_until_decision p4_ttpvlmr NNUE/shims/engine_p4_ttpvlmr.py \
                NNUE/shims/engine_p3_cutlmr.py ;;
        *)  echo ""
            echo "### P4 SKIPPED: it is only meaningful on top of an accepted P3"
            echo "###   (P3 decision: $P3D)" ;;
    esac
fi

[ "$STOP" -eq 1 ] || run_until_decision s10_asp NNUE/shims/engine_s10_asp.py "$BASE"
[ "$STOP" -eq 1 ] || run_until_decision s11_falleval NNUE/shims/engine_s11_falleval.py "$BASE"
[ "$STOP" -eq 1 ] || run_until_decision s6_lmrhist NNUE/shims/engine_s6_lmrhist.py "$BASE"

echo ""
echo "### QUEUE DONE $(TZ=Europe/Zurich date '+%H:%M:%S')"
for L in p1_corrhist p3_cutlmr v13s1 v13s2 v13s3 p4_ttpvlmr s10_asp s11_falleval s6_lmrhist; do
    S="sprt_${L}_tc10+0.1.json"
    [ -f "$S" ] && python3 -c "
import json; d = json.load(open('$S'))
print(f\"  {'$L':14s} {d['pairs']:6,d} pairs  LLR {d['llr']:+.3f}  decision {d['decision']}\")"
done
echo ""
echo "Anything that ACCEPTED still needs its 50+0.5 fixed-budget price before"
echo "it can be quoted or added to the ledger. scp the sprt_*.json files back"
echo "and commit them before terminating the box."
