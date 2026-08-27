#!/bin/bash
# Mate-finding across releases: run matetrack on every C-era snapshot and
# write ONE clean TSV, so mating efficiency can be tracked version by version.
#
#   ./scripts/matetrack_history.sh                       # v31..v60, mates2000
#   ./scripts/matetrack_history.sh --versions "55 57 60" # a subset
#   ./scripts/matetrack_history.sh --epd matetrack.epd --time 500
#
# C-ERA ONLY (v31+): earlier snapshots predate the C search core, so cuci.py
# has no csearch.so to drive. v60 is the live tree, driven by cuci.py itself.
#
# Each version runs in its OWN process (uci/cuci_old.py): the snapshots carry
# their own .so files and the loader hands back whichever image it saw first,
# so two versions in one process would silently run the same code.
#
# NOT an Elo instrument. Mate-suite score has been positive on changes that
# measured negative -- it is a CORRECTNESS signal. A reproducible decline
# deserves weight; a single good number does not.
set -u
cd "$(dirname "$0")/.."
REPO="$(pwd)"
MT="${MT:-$REPO/../matetrack}"
PY="$MT/.venv/bin/python"
EPD="mates2000.epd"; TIME_MS=0.2; CONC=6      # TIME_MS is SECONDS (matecheck --time)
# v31+ -- the whole C era runs under uci/cuci_old.py's compatibility layer.
# CAVEAT: only v56-v60 have a RECORDED bench signature to check against, and
# all four match exactly. v31-v55 run but supply 17-74 defaulted attributes
# each (uci/cuci_old.py <N> --audit lists them), so their rows are indicative,
# not proof that the snapshot behaves as it shipped.
VERSIONS="$(ls "Old Engine" | grep -E '^[0-9]+$' | sort -n | awk '$1>=31')"   # default: C era; pass --versions "$(seq 1 60)" for everything
SYZYGY="$REPO/syzygy"
while [ $# -gt 0 ]; do
  case "$1" in
    --epd) EPD="$2"; shift 2 ;;
    --time) TIME_MS="$2"; shift 2 ;;
    --concurrency) CONC="$2"; shift 2 ;;
    --versions) VERSIONS="$2"; shift 2 ;;
    --no-syzygy) SYZYGY=""; shift ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done
[ -x "$PY" ] || { echo "no matetrack venv at $PY -- python3 -m venv .venv && .venv/bin/pip install tqdm chess"; exit 1; }
[ -f "$MT/$EPD" ] || { echo "no $EPD in $MT"; exit 1; }

OUT="$REPO/matetrack_history_$(date +%Y%m%d_%H%M%S).tsv"
printf 'version\ttotal\tfound\tbest\tfound_pct\tbest_pct\tepd\ttime_ms\tconcurrency\n' > "$OUT"
N=$(echo "$VERSIONS" | wc -w | tr -d ' '); i=0; t0=$(date +%s)
echo "matetrack across $N versions -- $EPD @ ${TIME_MS}s, concurrency $CONC"
echo "output: $OUT"
echo ""
for v in $VERSIONS; do
  i=$((i+1))
  # a tiny exec wrapper per version: matecheck's --engine wants a program
  W="$(mktemp -t pygin-v$v)"
  if [ "$v" = "60" ]; then
      # the live tree IS v60; there is no snapshot .so to load
      printf '#!/bin/sh\nexec python3 %s/cuci.py "$@"\n' "$REPO" > "$W"
  elif [ -f "Old Engine/$v/csearch.so" ]; then
      printf '#!/bin/sh\nexec python3 %s/uci/cuci_old.py %s "$@"\n' "$REPO" "$v" > "$W"
  else
      # pre-C era (v1-v30): the pure-Python wrapper. The old fallback sent
      # these to the LIVE cuci.py, which would have silently measured v60
      # under a v12 label -- the exact mislabel a history file must never
      # contain. ~100-200x slower than the C era, and their Bad-PV column
      # is meaningless (they expose no PV); found/best remain real.
      printf '#!/bin/sh\nexec python3 %s/uci/uci_old.py %s "$@"\n' "$REPO" "$v" > "$W"
  fi
  chmod +x "$W"
  res=$(cd "$MT" && "$PY" matecheck.py --engine "$W" --epdFile "$EPD" \
        --time "$TIME_MS" --concurrency "$CONC" \
        ${SYZYGY:+--syzygyPath "$SYZYGY"} 2>/dev/null)
  tot=$(echo "$res" | grep -oE 'Total FENs: *[0-9]+' | grep -oE '[0-9]+$')
  fnd=$(echo "$res" | grep -oE 'Found mates: *[0-9]+' | grep -oE '[0-9]+$')
  bst=$(echo "$res" | grep -oE 'Best mates: *[0-9]+' | grep -oE '[0-9]+$')
  rm -f "$W"
  if [ -n "${tot:-}" ] && [ "$tot" -gt 0 ]; then
      fp=$(awk -v a="$fnd" -v b="$tot" 'BEGIN{printf "%.2f", 100*a/b}')
      bp=$(awk -v a="$bst" -v b="$tot" 'BEGIN{printf "%.2f", 100*a/b}')
      printf 'v%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$v" "$tot" "$fnd" "$bst" "$fp" "$bp" "$EPD" "$TIME_MS" "$CONC" >> "$OUT"
  else
      printf 'v%s\tFAILED\t\t\t\t\t%s\t%s\t%s\n' "$v" "$EPD" "$TIME_MS" "$CONC" >> "$OUT"
      fp="--"; bp="--"; fnd="FAILED"; bst=""
  fi
  el=$(( $(date +%s) - t0 )); eta=$(( el * (N - i) / i ))
  printf '  [%2d/%2d] v%-3s found %-6s best %-6s  (%s%% / %s%%)  elapsed %dm%02ds  ETA %dm%02ds\n' \
    "$i" "$N" "$v" "$fnd" "$bst" "$fp" "$bp" $((el/60)) $((el%60)) $((eta/60)) $((eta%60))
done
echo ""
echo "done -> $OUT"
