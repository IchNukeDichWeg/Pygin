#!/bin/bash
# Epoch sweep on the 105M 25,000-node corpus: 20 / 40 / 60, ONE seed.
#
#     ./scripts/train_epoch_sweep.sh [seed]        default: 1
#
# WHY ONE SEED. The trainer is bit-reproducible (gw6 == gw6r byte-identical),
# so the ~31 Elo spread between nets IS the seed. Holding it fixed makes the
# epoch count the only variable, which is the only way three runs can be
# compared to each other at all. The price: any single net's Elo vs v12 still
# carries this seed's luck, so this sweep says which SCHEDULE is best and
# cannot say whether the corpus beat v12. Those are different questions.
#
# WHY --epochs IS ITS OWN EXPERIMENT. The flag sets the cosine LENGTH as well
# as the run length, so a 60-epoch run is NOT a 20-epoch run continued. Each
# count has to be trained from scratch; there is no prefix to reuse.
#
# NO 8-EPOCH CONTROL, user's call 2026-08-29: the local 3-seed run produced
# 8-epoch nets on this same corpus, and a box-trained control was judged not
# worth the extra 1.3h. Cross-hardware, so not byte-comparable to these.
#
# THE READOUT IS AN A/B, NOT VAL. Val is 0-for-11 as an Elo predictor on this
# project -- the g1/g2/g3 nets sat in a 0.0001 val range while being 31 Elo
# apart. Do not pick a winner off the val column printed here.
set -u
cd "$(dirname "$0")/.."
SEED="${1:-1}"
D=NNUE/datasets/gen25k.pygdata
say() { echo "[$(TZ=Europe/Zurich date '+%H:%M:%S %Z')] $*"; }

[ -f "$D" ] || { echo "missing $D -- fetch the nnue-gen25k release first"; exit 1; }

# --cache-chunks needs ~0.8 GB per million records (MEASURED on the 5090 box,
# not the ~250-300 B/record the trainer's help estimates). Refuse rather than
# discover it by thrashing six hours in.
python3 - <<PY || exit 1
import os, sys
rec = os.path.getsize("$D") // 88
need = rec * 0.95 * 0.8 / 1e6
# cgroup v2, then v1, and ONLY then the host figure. Inside a container
# sysconf reports the HOST, which is the lie this gate exists to catch: the
# 2026-08-29 box claimed 503 GB via free with a 241.7 GB actual grant, and a
# smaller box would have sailed through the check it was supposed to fail.
lim, src = None, None
for path, tag in (("/sys/fs/cgroup/memory.max", "cgroup v2"),
                  ("/sys/fs/cgroup/memory/memory.limit_in_bytes", "cgroup v1")):
    try:
        v = open(path).read().strip()
        if v != "max":
            lim, src = int(v), tag
            break
    except Exception:
        pass
if lim is None or lim > 2**50:      # v1 prints a sentinel when uncapped
    lim = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    src = "host (NO cgroup limit found -- may be overstated)"
print(f"  limit source: {src}")
print(f"  records {rec:,}  cache needs ~{need:.0f} GB  limit {lim/2**30:.0f} GB")
if need > lim / 2**30 * 0.85:
    sys.exit("  REFUSING: --cache-chunks would not fit. Rent a bigger box or "
             "drop --cache-chunks (and expect ~13x slower epochs).")
print("  fits.")
PY

for n in 20 40 60; do
    say "=== $n epochs, seed $SEED ==="
    NNUE/venv/bin/python NNUE/train.py "$D" \
        --epochs "$n" --chunk 2000000 --cache-chunks --lr-schedule cosine \
        --device cuda --seed "$SEED" \
        --checkpoint-dir "NNUE/checkpoints/e${n}s${SEED}" \
        --out "NNUE/nets/nnue_e${n}s${SEED}.nnue" 2>&1 | tee "e${n}s${SEED}.log"
    grep -aq "^exported best" "e${n}s${SEED}.log" || { say "$n FAILED"; exit 1; }
done

say "=== sweep done ==="
for n in 20 40 60; do grep -ah "^exported best" "e${n}s${SEED}.log" | tail -1; done
echo ""
echo "Next: A/B each against v12. The val numbers above decide NOTHING."
