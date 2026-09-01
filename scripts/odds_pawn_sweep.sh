#!/bin/bash
# Rank all eight white pawns as odds handicaps, empirically.
#
#     ./scripts/odds_pawn_sweep.sh [games]        default: 200
#
# WHY. The rung ordering in odds.py is CHESS REASONING, not measurement: f2
# is theoretically the biggest pawn to give (e1-h4 diagonal, castled king,
# f-file) and a2 the smallest (queenside rook pawn, king untouched). That
# theory has never been tested against this engine, and reasoning-from-
# mechanism has lost to measurement repeatedly on this project.
#
# ORDER IS DELIBERATE. f2 first (it extends the existing series and is the
# anchor), then a2 and h2 -- the two candidate rungs BELOW it. If the sweep is
# interrupted after three runs it has already answered the real question,
# "what comes after f2". The middle four are ranked last because they are the
# least useful and the least separable.
#
# WHAT 200 GAMES CAN AND CANNOT SAY. At 200 games a win rate carries roughly
# +/-6%. That resolves ENDPOINTS (f2 ~90% vs a2 maybe ~78% is far outside it)
# and does NOT resolve neighbours (c2 vs b2 differing by 2-3% is noise). For a
# publishable full ranking, re-run at 1000.
#
# Full-strength Stockfish, 50+0.50 from match.py, cores/2 workers -- the same
# instrument as the v58 pawn-odds point so f2 here extends that series.
set -u
cd "$(dirname "$0")/.."
G="${1:-200}"
W=$(( $(sysctl -n hw.ncpu 2>/dev/null || nproc) / 2 ))
OUT="odds_sweep_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUT"
say(){ echo "[$(TZ=Europe/Zurich date '+%H:%M:%S %Z')] $*" | tee -a "$OUT/sweep.log"; }

say "pawn-odds sweep: $G games x 8 squares, $W workers, full-strength SF"
say "order: f2 a2 h2 then the middle four -- endpoints first on purpose"

for sq in f2 a2 h2 g2 e2 d2 c2 b2; do
    L="$OUT/odds_$sq.log"
    say "=== $sq ($G games) ==="
    python3 odds.py --odds-squares "$sq" --num-games "$G" --workers "$W" 2>&1 | tee "$L"
    if grep -qa "interrupted -- writing summary" "$L"; then
        say "$sq was INTERRUPTED -- stopping the sweep here, not starting the next square"
        break
    fi
    grep -qa "Engine 1 score:" "$L" || { say "$sq produced no summary -- stopping"; break; }
    say "$sq done: $(grep -a 'Engine 1 score:' "$L" | tail -1)"
done

say "=== RANKING (highest win% = biggest handicap given to us) ==="
python3 - "$OUT" <<'PY' | tee -a "$OUT/sweep.log"
import glob, os, re, sys
d = sys.argv[1]
rows = []
for f in glob.glob(os.path.join(d, "odds_*.log")):
    sq = os.path.basename(f)[5:-4]
    m = re.findall(r"Engine 1 score:\s*[\d.]+/(\d+)\s*\(([\d.]+)%\)", open(f, errors="ignore").read())
    if m:
        n, pct = int(m[-1][0]), float(m[-1][1])
        # binomial-ish interval on the score rate, enough to see whether two
        # squares are actually separable at this sample size
        se = 100 * (0.25 / n) ** 0.5
        rows.append((pct, se, n, sq))
if not rows:
    print("  no completed squares yet"); raise SystemExit
rows.sort(reverse=True)
theory = {"f2":1,"g2":2,"e2":3,"d2":4,"h2":5,"c2":6,"b2":7,"a2":8}
full = len(rows) == 8
print(f"  {'sq':4} {'score%':>8} {'+/-':>6} {'games':>7}   rank ({len(rows)} run)   theory (of 8)")
for i,(pct,se,n,sq) in enumerate(rows, 1):
    t = theory.get(sq, "-")
    # only call a disagreement when the whole ladder ran; on a partial sweep
    # "2nd of 3" and "5th of 8" are not in conflict and flagging them is noise
    flag = "  <- DISAGREES" if (full and t != i) else ""
    print(f"  {sq:4} {pct:7.2f}% {se:5.2f}% {n:7,}        {i:2}            {t}{flag}")
print()
print("  Neighbours inside each other's +/- are NOT separable at this sample size.")
if not full:
    print(f"  PARTIAL: {len(rows)} of 8 squares ran, so the theory column is context, not a verdict.")
PY
say "logs in $OUT/"
