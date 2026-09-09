#!/bin/bash
# OPEN 1 -- the number we do not have. Run:
#
#     ./scripts/ab_open1.sh                 preflights, then the campaign
#     ./scripts/ab_open1.sh --preflight-only  the three checks, then stop
#
# HEAD (the v63 candidate) vs Old Engine/61, 50+0.5, SPRT capped at 5,000
# games. Answers "is the v63 candidate better than v61", prices the FI-21
# aspiration window on a CLEAN core, and is the v63 release number.
#
# WHY THIS RUN EXISTS. v62 shipped carrying b4339ae+a1a31cd (the b05 FI-115
# completion), which had already measured ACCEPT H0 at LLR -5.98 against v61.
# It was reverted 2026-09-04, so HEAD is v62's SETTINGS on v61's CORE -- an
# engine that has never been measured against anything. The ledger has been
# frozen at ~+354 since v61 and every future A/B baselines against this tree.
#
# SPRT, NOT A FIXED BUDGET -- user's call 2026-09-09 and it is right here: the
# box bills by the hour, SPRT stops the moment a bound is crossed, and if it
# runs the full 5,000 the estimate carries no stopping bias and is quotable
# anyway. The cheap path can produce the expensive answer; the reverse is not
# true. What it CANNOT do is give a magnitude AND stop early: a bound-stopped
# number is a verdict, the ledger stays at ~+354, and a fixed-budget re-run is
# then owed before any Elo goes in the notes.
#
# TIMED instrument -> cores/2 workers. A game runs TWO engines, so cores/2 is
# one core each, and NPS at a fixed clock IS playing strength (48 workers hold
# 2.28M nps, 111 hold 1.02M). Density is part of the instrument: anything
# pooled with this run must match it.
set -u
cd "$(dirname "$0")/.."
PRE_ONLY=0
if [ "${1:-}" = "--preflight-only" ]; then PRE_ONLY=1; shift; fi
POS="${1:-2500}"                     # x2 colours = 5,000 games
OFF="${2:-0}"
CORES=$(sysctl -n hw.ncpu 2>/dev/null || nproc)
W=$((CORES / 2)); [ "$W" -lt 1 ] && W=1
A=cengine.py
B="Old Engine/61/engine61.py"
S=sprt_v63_cleancore.json
say(){ echo "[$(TZ=Europe/Zurich date '+%H:%M:%S %Z')] $*"; }

for f in "$A" "$B"; do [ -f "$f" ] || { echo "missing $f"; exit 1; }; done

say "=== PREFLIGHT A -- both arms must run the SAME core ==="
# HEAD's csearch.c is byte-identical to Old Engine/61's after the revert, so a
# box that built both correctly gets two identical hashes. A DIFFERENCE here
# means the revert did not land or a stale .so survived, and the run would
# measure the core instead of the window.
H1=$(md5sum csearch.so 2>/dev/null | cut -d' ' -f1 || md5 -q csearch.so)
H2=$(md5sum "Old Engine/61/csearch.so" 2>/dev/null | cut -d' ' -f1 || md5 -q "Old Engine/61/csearch.so")
echo "    HEAD          $H1"
echo "    Old Engine/61 $H2"
[ "$H1" = "$H2" ] || { echo "    REFUSING TO RUN: the two cores differ"; exit 1; }
echo "    OK -- identical, so the only variable is the aspiration window"

say "=== PREFLIGHT B -- the variable under test, and nothing else ==="
python3 - "$A" "$B" <<'PY' || { echo "    REFUSING TO RUN"; exit 1; }
import sys, importlib.util as u
def load(p):
    sp = u.spec_from_file_location("pf_" + p.replace("/", "_"), p)
    m = u.module_from_spec(sp); sp.loader.exec_module(m); return m.Engine
cand, base = load(sys.argv[1]), load(sys.argv[2])
ok = True
for name, E in (("HEAD", cand), ("v61", base)):
    bits, tag = E.TT_BITS, bool(getattr(E, "TT_DEADTAG", False))
    net = E.NNUE_FILE.split("/")[-1]
    good = bits == 23 and tag and "v12" in net
    ok = ok and good
    print(f"    {'OK ' if good else 'BAD'}  {name:4}  {net}  "
          f"TT_BITS={bits}  DEADTAG={tag}")
# The ONE thing that may differ. v61 predates the toggle, so it must not have it.
c = getattr(cand, "ASPIRATION_FI21", None)
b = getattr(base, "ASPIRATION_FI21", None)
print(f"    {'OK ' if (c is True and b is None) else 'BAD'}  "
      f"ASPIRATION_FI21  HEAD={c}  v61={b}")
sys.exit(0 if (ok and c is True and b is None) else 1)
PY

say "=== PREFLIGHT C -- what the engine children actually resolved ==="
python3 match.py "$A" "$B" "$POS" "$OFF" --workers "$W" --tc 50+0.5 \
    --seed 62 --sprt --sprt-min-pairs 1500 --sprt-resume "$S" --dry-run

if [ "$PRE_ONLY" -eq 1 ]; then
    say "=== PREFLIGHTS PASSED -- stopping here (--preflight-only) ==="
    exit 0
fi

say "=== OPEN 1: $((POS * 2)) games, 50+0.5, $W workers, SPRT [0,4] ==="
say "state: $S   (resume a killed run by re-running this script)"
python3 match.py "$A" "$B" "$POS" "$OFF" --workers "$W" --tc 50+0.5 \
    --seed 62 --sprt --sprt-min-pairs 1500 --sprt-resume "$S"
ST=$?

if [ "$ST" -ne 0 ]; then
    say "INTERRUPTED (exit $ST) -- the summary above is real, the campaign is not done"
    say "the STATE-LINE printed above is the recovery path if this box has no git credentials"
    exit "$ST"
fi
say "=== DONE ==="
say "commit $S before the box is destroyed, or copy the STATE-LINE off it"
