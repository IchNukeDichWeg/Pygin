#!/bin/bash
# THE run that decides whether FI-115 ships. Run: ./scripts/ab_ltc_deadtag.sh
#
#   engine_v61b_deadtag (192 MiB, tag ON)  vs  engine_nnue_v12 (192 MiB, OFF)
#   50+0.5, seed 59, UHO_4060_v4, cores/2 workers
#
# WHY THIS AND NOT THE RUN THAT ALREADY PASSED. FI-115 was confirmed on
# 2026-08-28 at 10+0.1 with a 24 MiB table on both sides (LLR +2.957, 6,160
# pooled pairs). 24 MiB is not what the engine ships. The small table was
# chosen to maximise replacement pressure, because a victim rule can only pay
# where entries are being evicted -- so that accept came from the regime built
# to favour it, and the shim note predicts less at 192 MiB. It does not
# transfer on its own.
#
# WHY NOT 192 MiB AT 10+0.1. Because the table would never fill. The FI-116
# ramp measurement put a 192 MiB table at roughly a 22-bit working set by ply
# 40 at 0.15s/move -- barely half occupied, almost nothing evicted. A null
# there would mean "no pressure to relieve", not "the rule does not work",
# and we would have spent a campaign learning nothing.
#
# WHY 50+0.5 IS THE RIGHT ONE. The recorded hashfull curve (cengine.py) has
# 192 MiB at 833 permille by ply 8 at 1.4s/move and fully saturated from ply
# 16, and 50+0.5 is longer still -- so every store after the opening evicts
# something and the victim rule is doing real work. It is also the TC the
# ledger is denominated in, so a pass here extends the ledger and gives the
# Elo for release notes.
#
# SEPARATE INSTRUMENT, SEPARATE STATE FILE. sprt_deadtag192_tc50+0.5.json
# never pools with the 10+0.1 files -- different clock, different table size,
# different experiment. Do not point --sprt-resume at one of those.
#
# COST: a 50+0.5 game is ~5x a 10+0.1 game, so ~6-7 hours per 10,000-game
# tranche at 48 workers. Budget for two.
set -u
cd "$(dirname "$0")/.."
LOGDIR="${LOGDIR:-$PWD}"
CORES=$(nproc); WORKERS=$((CORES / 2)); [ "$WORKERS" -lt 1 ] && WORKERS=1
echo "workers: $WORKERS (cores $CORES / 2 -- one core per engine)"
echo "instrument: 50+0.5, 192 MiB both sides, seed 59 -- pools ONLY with"
echo "            other 50+0.5 runs at this density."

A=NNUE/shims/engine_v61b_deadtag.py
B=NNUE/shims/engine_nnue_v12.py
S="sprt_deadtag192_tc50+0.5.json"
MAX_TRANCHES=4          # 40,000 games; at ~6.5h each this is already 26h
STOP=0
trap 'STOP=1; echo ""; echo "### INTERRUPTED -- stopping after this tranche"' INT

field () { python3 -c "
import json, os
f = '$S'
print(json.load(open(f)).get('$1') if os.path.isfile(f) else '$2')"; }

for f in "$A" "$B"; do [ -f "$f" ] || { echo "missing $f"; exit 1; }; done
T=0
while [ "$T" -lt "$MAX_TRANCHES" ]; do
    D=$(field decision None)
    [ "$D" != "None" ] && { echo "### ALREADY DECIDED: $D"; break; }
    OFF=$(field next_offset 0)
    T=$((T + 1))
    echo ""
    echo "=== deadtag192 tranche $T/$MAX_TRANCHES  offset $OFF  $(TZ=Europe/Zurich date '+%H:%M:%S') ==="
    python3 match.py "$A" "$B" 5000 "$OFF" \
        --workers "$WORKERS" --tc 50+0.5 --seed 59 \
        --sprt --sprt-min-pairs 1500 --sprt-resume "$S" \
        2>&1 | tee -a "$LOGDIR/ab_deadtag192_ltc.log"
    echo "### tranche $T done: $(field pairs 0) pooled pairs, LLR $(field llr 0), decision $(field decision None)"
    D=$(field decision None)
    [ "$D" != "None" ] && { echo "### DECIDED: $D  $(TZ=Europe/Zurich date '+%H:%M:%S')"; break; }
    [ "$STOP" -eq 1 ] && { echo "### STOPPED BY USER"; break; }
done
echo ""
echo "### LTC DEADTAG DONE $(TZ=Europe/Zurich date '+%H:%M:%S')"
[ -f "$S" ] && python3 -c "
import json; d = json.load(open('$S'))
print(f\"  {d['pairs']:,} pairs  LLR {d['llr']:+.3f}  decision {d['decision']}\")"
