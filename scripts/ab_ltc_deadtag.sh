#!/bin/bash
# FI-115 at the SHIPPED table size, for a real Elo. One fixed-budget run.
#
#   ./scripts/ab_ltc_deadtag.sh [positions] [offset]      default: 2500 0
#
#   engine_v61b_deadtag (192 MiB, tag ON)  vs  engine_nnue_v12 (192 MiB, OFF)
#   50+0.5, seed 59, UHO_4060_v4, cores/2 workers
#
# SPLIT IT ACROSS BOXES. positions x 2 = games, and the offset picks which
# openings. Two boxes, disjoint offsets, pooled afterwards:
#     box A:  ./scripts/ab_ltc_deadtag.sh 2500 0
#     box B:  ./scripts/ab_ltc_deadtag.sh 2500 2500
# 10,000 games total lands in about 3.3h instead of 6.5h. Both boxes must run
# the SAME worker density (cores/2) or the halves are different instruments
# and must not be pooled.
#
# NO SPRT, ON PURPOSE. A sequential test stops the instant it crosses, so it
# is always caught at a favourable fluctuation and its magnitude is biased
# upward by construction -- that is how +33.83 became +19.11. A fixed budget
# has no such bias, and the LLR can still be computed from the pentanomial
# afterwards, which the tail of this script does. One run, both answers, half
# the compute of running a screen and then a measurement.
#
# WHY 50+0.5. User's call 2026-08-28, and it is the release instrument: this
# run decides whether FI-115 ships. Saturation is satisfied well before here
# -- the recorded hashfull curve reads 833 permille by ply 8 and full from
# ply 16 at 1.4s/move, so 50+0.20 would already have done -- but 50+0.5 is
# what the recent NNUE ledger runs used, and a release number should sit on
# the instrument the release is judged on. It costs ~1.4x more; that is the
# price of pooling with the runs it will be compared against.
#
# WHY NOT 10+0.1. The table never fills there: the FI-116 ramp work put a
# 192 MiB table at roughly a 22-bit working set by ply 40 at 0.15s/move, so
# almost nothing is evicted and a victim rule has nothing to improve. A null
# would mean "no pressure to relieve", not "the rule does not work".
#
# WHY 192 MiB IS ENFORCED BELOW. The 2026-08-28 accept was measured with
# 24 MiB on BOTH sides, which is not what the engine ships -- the small table
# was chosen to maximise replacement pressure, so that accept came from the
# regime built to favour it. The preflight refuses to start on the wrong pair.
#
# READING IT: 10,000 games gives about +/-4.4 Elo. A true +5 or better clears
# zero; if the point estimate lands at +2 or +3 the interval touches zero and
# the honest answer is "needs more games", not "confirmed".
set -u
cd "$(dirname "$0")/.."
LOGDIR="${LOGDIR:-$PWD}"
POS="${1:-2500}"
OFF="${2:-0}"
CORES=$(sysctl -n hw.ncpu 2>/dev/null || nproc); WORKERS=$((CORES / 2)); [ "$WORKERS" -lt 1 ] && WORKERS=1

A=NNUE/shims/engine_v61b_deadtag.py
B=NNUE/shims/engine_nnue_v12.py
for f in "$A" "$B"; do [ -f "$f" ] || { echo "missing $f"; exit 1; }; done

echo "### PREFLIGHT -- 192 MiB is checked, not assumed"
python3 - "$A" "$B" <<'PY' || { echo "### REFUSING TO RUN"; exit 1; }
import sys, importlib.util as u
tags = []
for p in sys.argv[1:]:
    sp = u.spec_from_file_location("m", p); m = u.module_from_spec(sp)
    sp.loader.exec_module(m)
    E = m.Engine
    bits = E.TT_BITS; mib = (1 << bits) * 24 // 2**20
    tag = bool(getattr(E, "TT_DEADTAG", False))
    grow = bool(getattr(E, "TT_GROW", False))
    ok = bits == 23 and mib == 192 and not grow
    print(f"  {'OK ' if ok else 'BAD'}  {p.split('/')[-1]:26s} "
          f"TT_BITS={bits} = {mib} MiB  DEADTAG={tag}  GROW={grow}")
    if not ok:
        sys.exit("  expected TT_BITS=23 (192 MiB) with TT_GROW off")
    tags.append(tag)
if tags != [True, False]:
    sys.exit(f"  expected tag ON in slot 1, OFF in slot 2 -- got {tags}")
print("  192 MiB both sides, tag is the only variable -- correct.")
PY

echo ""
echo "### FI-115 @ 192 MiB, 50+0.5, FIXED BUDGET (no early stop)"
echo "### $POS positions x 2 = $((POS * 2)) games from offset $OFF, $WORKERS workers"
echo "### start $(TZ=Europe/Zurich date '+%H:%M:%S %Z')"
LOG="$LOGDIR/ab_deadtag192_${OFF}.log"
python3 match.py "$A" "$B" "$POS" "$OFF" \
    --workers "$WORKERS" --tc 50+0.5 --seed 59 2>&1 | tee "$LOG"

echo ""
echo "### FI-115 RESULT  $(TZ=Europe/Zurich date '+%H:%M:%S %Z')"
python3 - "$LOG" <<'PY'
import re, sys
sys.path.insert(0, "testing")
import sprt
txt = open(sys.argv[1], errors="ignore").read()
elo = re.findall(r"Engine 1 score:.*=>\s*([-+0-9.]+ \+/- [0-9.]+ Elo)", txt)
pen = re.findall(r"^Ptnml:\s*([0-9,\s]+)$", txt, re.M)
if elo:
    print(f"  Elo (fixed budget, UNBIASED): {elo[-1]}")
if pen:
    c = [int(x) for x in pen[-1].replace(",", " ").split()]
    r = sprt.evaluate(c, elo0=0.0, elo1=4.0, model="normalized")
    print(f"  Ptnml: {c}")
    print(f"  LLR post-hoc [0,4] normalized: {r['llr']:+.3f}  "
          f"(bounds {r['lower']:+.3f} / {r['upper']:+.3f})")
print("  Pool with the other box's ptnml before deciding; each half alone is"
      "\n  ~+/-6 Elo and neither is the result on its own.")
PY
