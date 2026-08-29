#!/bin/bash
# Put a REAL number on v60 + v61 together. Run: ./scripts/ab_price_v59_to_v61.sh
#
#   engine_v61b_deadtag (= v61: v12 net, 192 MiB, TT_DEADTAG on)
#     vs  Old Engine/59/engine59.py  (= v59: v4 net, no deadtag)
#   50+0.5, FIXED 5,000-game budget, no SPRT
#
# WHY THIS RUN EXISTS. v60's A/B stopped at a bound after 1,594 games and read
# +42.50 +/- 17.3. A sequential test stops the instant it crosses, so it is
# always caught on a favourable swing and its magnitude is biased upward by
# construction -- this repo watched +33.83 become +19.11 under a fixed budget.
# So v60 shipped with its own notes saying the ledger is NOT advanced on it,
# and it has carried 0.00 ever since. That is honest but it is an IOU, and the
# v12 net could not be separated from the v10 net over a FULL 10,000 games
# (50.54%, LLR +1.235), which is direct evidence +42.50 is inflated.
#
# Measuring v61 against v59 prices BOTH releases in one run: whatever comes
# back is v60 + v61 combined, and v61's own +15.89 +/- 4.2 is already known,
# so the remainder is v60's real contribution.
#
# 5,000 GAMES, NOT 10,000 -- user's call 2026-08-29, and it is sound HERE
# specifically. The budget question is always "is the error bar smaller than
# the effect": 5,000 games gives about +/-6.2 Elo against 10,000's +/-4.4, and
# the expected effect is the whole v59->v61 gap (v61 alone is +15.89, plus
# whatever v60 is worth). An effect that size clears +/-6.2 comfortably. Do NOT
# read this as a new default -- FI-115 needed 10,000 because a single feature
# worth ~+15 against +/-6.2 would have been marginal, and hash_small's +2.29
# would be invisible at either budget.
#
# NO SPRT. The point is the magnitude, and a bound-stopped magnitude is what
# put v60 in this position. The LLR is recomputed post-hoc from the
# pentanomial, so the verdict comes free.
set -u
cd "$(dirname "$0")/.."
LOGDIR="${LOGDIR:-$PWD}"
POS="${1:-2500}"          # x2 colours = 5,000 games
OFF="${2:-0}"
CORES=$(nproc); WORKERS=$((CORES / 2)); [ "$WORKERS" -lt 1 ] && WORKERS=1

A=NNUE/shims/engine_v61b_deadtag.py
B="Old Engine/59/engine59.py"
for f in "$A" "$B"; do [ -f "$f" ] || { echo "missing $f"; exit 1; }; done

echo "### PREFLIGHT -- the candidate must really be v61"
python3 - "$A" <<'PY' || { echo "### REFUSING TO RUN"; exit 1; }
import sys, importlib.util as u
sp = u.spec_from_file_location("m", sys.argv[1]); m = u.module_from_spec(sp)
sp.loader.exec_module(m)
E = m.Engine
bits, tag = E.TT_BITS, bool(getattr(E, "TT_DEADTAG", False))
net = E.NNUE_FILE.split("/")[-1]
mib = (1 << bits) * 24 // 2**20
ok = bits == 23 and mib == 192 and tag and "v12" in net
print(f"  {'OK ' if ok else 'BAD'}  candidate: {net}  {mib} MiB  DEADTAG={tag}")
if not ok:
    sys.exit("  expected the v12 net, 192 MiB and TT_DEADTAG on -- that is v61")
print("  candidate is v61.  baseline is the frozen v59 snapshot.")
PY

echo ""
echo "### v59 -> v61, 50+0.5, FIXED $((POS * 2))-game budget (no early stop)"
echo "### start $(TZ=Europe/Zurich date '+%H:%M:%S %Z')"
LOG="$LOGDIR/ab_v59_to_v61.log"
python3 match.py "$A" "$B" "$POS" "$OFF" \
    --workers "$WORKERS" --tc 50+0.5 --seed 61 2>&1 | tee "$LOG"

echo ""
echo "### RESULT  $(TZ=Europe/Zurich date '+%H:%M:%S %Z')"
python3 - "$LOG" <<'PY'
import re, sys
sys.path.insert(0, "testing")
import sprt
txt = open(sys.argv[1], errors="ignore").read()
elo = re.findall(r"Engine 1 score:.*=>\s*([-+0-9.]+) \+/- ([0-9.]+) Elo", txt)
pen = re.findall(r"^Ptnml:\s*([0-9,\s]+)$", txt, re.M)
if elo:
    e, m = float(elo[-1][0]), float(elo[-1][1])
    print(f"  v59 -> v61 (both releases): {e:+.2f} +/- {m:.1f} Elo")
    print(f"  v61 alone, already measured: +15.89 +/- 4.2")
    print(f"  => v60's share is about {e - 15.89:+.2f}, and THAT is the number "
          f"the ledger\n     has been carrying as 0.00 since 2026-08-23.")
    if e - m > 0:
        print("  The interval clears zero: bankable.")
    else:
        print("  The interval touches zero: still not bankable, needs more games.")
if pen:
    c = [int(x) for x in pen[-1].replace(",", " ").split()]
    r = sprt.evaluate(c, elo0=0.0, elo1=4.0, model="normalized")
    print(f"  Ptnml: {c}   post-hoc LLR {r['llr']:+.3f}")
PY
