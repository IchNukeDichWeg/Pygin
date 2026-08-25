#!/bin/bash
# A/B the three gen200m seeds against v12 -- the verdict the corpus is owed.
#
#     ./scripts/ab_gen200m.sh
#
# INSTRUMENT, fixed and deliberate: seed 59, 10+0.1, UHO_4060_v4, offset 0 --
# byte-identical to the s1-s4/m1-m4 seed screen, so these numbers POOL with
# it and "g median vs s median (-29)" is a like-for-like read. Changing any
# of it is a DEVIATION and must be said out loud, not buried here.
#
# --workers 48, NOT --workers 0. Worker density is part of the INSTRUMENT: the
# seed screen ran 48 parallel on a 96-core box, and measured worker density
# moves NPS hard (48 workers 2.28M nps vs 111 workers 1.02M nps), which moves
# playing strength at a fixed clock. Running g at 95 against an s screen taken
# at 48 would be two instruments, and they could not be pooled.
#
# 5,000 positions = 10,000 games per net (match.py's 3rd arg is POSITIONS,
# each played twice for colour balance), twice the seed screen's budget:
# the screen was a kill filter, this is a measurement.
#
# --sprt --sprt-min-pairs 1500 stops a clear loser after ~3,000 games instead
# of 10,000. NOTE: a run that stops at a bound is a VERDICT ONLY -- its Elo is
# biased away from zero by construction and must not be quoted as an effect
# size. Only a full-budget run gives a quotable number.
set -u
cd "$(dirname "$0")/.."
# logs next to the repo by default; override with LOGDIR=/somewhere
LOGDIR="${LOGDIR:-$PWD}"
BASE=NNUE/shims/engine_nnue_v12.py
[ -f "$BASE" ] || { echo "missing baseline $BASE"; exit 1; }
for n in g1 g2 g3; do
    SHIM="NNUE/shims/engine_nnue_$n.py"
    [ -f "$SHIM" ] || { echo "missing $SHIM"; exit 1; }
    NET=$(python3 -c "import sys;sys.path.insert(0,'NNUE/shims');import engine_nnue_$n as m;print(m.Engine.NNUE_FILE)")
    [ -f "$NET" ] || { echo "missing net $NET for $n"; exit 1; }
done
echo "all three shims and their nets present, and the baseline"
echo ""
for n in g1 g2 g3; do
    echo "=== $n vs v12  $(TZ=Europe/Zurich date '+%H:%M:%S') ==="
    python3 match.py "NNUE/shims/engine_nnue_$n.py" "$BASE" \
        5000 0 --workers 48 --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500 \
        --sprt-resume "sprt_engine_nnue_${n}_vs_engine_nnue_v12_tc10+0.1.json" \
        2>&1 | tee "$LOGDIR/ab_$n.log"
    echo "### $n DONE $(TZ=Europe/Zurich date '+%H:%M:%S')"
done
echo "### ALL THREE A/B DONE $(TZ=Europe/Zurich date '+%H:%M:%S')"
