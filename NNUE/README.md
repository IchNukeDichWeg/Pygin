# NNUE — FI-15 build-out (Phases 1–5 complete, dormant)

The complete NNUE infrastructure for Pygin: data generation, PyTorch
trainer, quantized export, C inference (accumulator + NEON/scalar forward),
and hybrid integration behind one master toggle. **Everything is dormant by
default** — `cengine.USE_NNUE = False` is byte-exact v50+armed-defaults
(bench `1,083,772` armed / `1,508,415` with ROOT_LMR off).

The frozen architecture/format contract is **DESIGN_nnue.md → "Phase 1 spec
(FROZEN)"**. Summary: KA8T feature set (8 king buckets, horizontal mirror,
12 planes, IN=6144/perspective; plain-768 = same code path at KB=1) + T16
threat encoding (16 int8 aggregate scalars from one attack-union pass),
net FT→2×256 → [512+16]→32→32→1, int16/int8 quantization QA=127/QB=64,
`.nnue` format v1, `.pygdata` training data format v1.

What remains (Phases 6–8): generate the real ~50M-position dataset, train,
2k screen → 10k A/B per net, bootstrap iterations, ship.

## Layout

| file | what |
|---|---|
| `nnue.c` | the entire C side, `#include`d by `csearch.c` (single TU — no build change). Loader, feature extraction, T16, F49-31 ply-indexed accumulator stack, NEON+scalar forward, verify mode, oracles |
| `config.py` | frozen constants shared by every tool (the C loader cross-checks them per net file) |
| `data_format.py` | `.pygdata` v1 writer/reader/merger (88-byte records, mmap-able) |
| `gen_data.py` | self-play labeling harness (F49-30 + F5-19 rules baked in) |
| `verify_labels.py` | label audit: exact-reproduction gate + FI-29 shaping report |
| `nnue_ref.py` | numpy truth: feature extraction, `.nnue` I/O, EXACT quantized reference forward |
| `model.py`, `train.py` | PyTorch float model (QAT-style clipping) + trainer + quantized export |
| `verify_c.py` | Phase-4 gates: `forward`, `increment`, `nps`, `threatcost` |
| `selftest_nnue.py` | NNUE unit checks (spawned by `selftest.py`; exit 42 = skip when no net) |
| `selfplay_smoke.py` | 100-game stability smoke with the toy net |
| `datasets/`, `nets/`, `checkpoints/`, `venv/` | local-only (gitignored) |

## Setup (one-time, training machine only)

The engine side needs nothing beyond `./setup.sh`. Training needs PyTorch
(system python here is 3.14; torch wants 3.12):

```
python3.12 -m venv NNUE/venv && NNUE/venv/bin/pip install torch numpy python-chess
```

## Commands (all from the repo root)

Generate data (any size; `--workers 223` on the match server):

```
python3 NNUE/gen_data.py NNUE/datasets/run1.pygdata --positions 100000 --nodes 5000 --workers 8 --seed 42
```

Audit the labels (hard gate: hmc==0 records reproduce exactly):

```
python3 NNUE/verify_labels.py NNUE/datasets/run1.pygdata --sample 200
```

Train + export (writes `checkpoints/best.pt`, `checkpoints/loss_curve.csv`,
and the quantized net):

```
NNUE/venv/bin/python NNUE/train.py NNUE/datasets/run1.pygdata --epochs 30 --out NNUE/nets/toy.nnue
```

Verify the C side (run after ANY nnue.c / trainer / format change):

```
python3 NNUE/verify_c.py forward --positions 100000        # C == numpy ref, exact
python3 NNUE/verify_c.py increment --pushes 1000000 --net NNUE/nets/toy.nnue
python3 NNUE/verify_c.py nps --net NNUE/nets/toy.nnue      # on/off throughput
python3 NNUE/verify_c.py threatcost                        # T16 recompute cost
```

Stability smoke + unit checks:

```
python3 NNUE/selfplay_smoke.py --games 100 --nodes 3000
python3 NNUE/selftest_nnue.py          # also auto-run by selftest.py
```

Enable in play (visible class attrs, no env vars — the house rule):

```python
import cengine
cengine.Engine.USE_NNUE = True                      # master toggle (abi 19)
cengine.Engine.NNUE_FILE = "NNUE/nets/toy.nnue"     # default already this
```

`cuci.py`'s fingerprint echoes `use_nnue=` for PGN forensics. Toggle OFF is
byte-exact v50; run `bench` (1,083,772) + `selftest.py` after any change.

## Generating real training data (Phase 6, the next step)

On the 223-worker server (~50M positions, see DESIGN_nnue.md for the
rationale; TC-free — the labeling budget is fixed NODES, immune to load):

```
nohup python3 NNUE/gen_data.py NNUE/datasets/main50m.pygdata --positions 50000000 --nodes 5000 --workers 223 --seed 1 > gen50m.log 2>&1 &
tail -f gen50m.log
```

Then: `verify_labels.py` on the result, train with `--epochs 20`-ish
(watch `loss_curve.csv`; val must fall and not diverge), export, run all
four `verify_c.py` gates + `selfplay_smoke.py`, and only then the 2k
screen per DESIGN_nnue.md Phase 6.

## Measured numbers (2026-07-18, this Mac, toy net)

- Phase-2 pipeline: 100k positions / ~1,500 games in ~3 min (8 workers,
  5k nodes/move). Scores symmetric (mean +7.5 cp, σ 495); F5-19 audit: 0
  shaped positions in 5,000 sampled.
- Label audit: 62/62 hmc==0 labels reproduced exactly; FI-29 would have
  draw-flattened 32/200 labels (the F49-30 population, measured).
- Phase-3 trainer: val MSE 0.996 → 0.054 in 30 epochs (~0.5 s/epoch);
  float-vs-int MAE 17.4 cp (three layers of QA/QB rounding noise).
- Phase-4 gates: forward — 100,000 random positions, C vs numpy reference,
  **0 mismatches** (+ 0 feature-set mismatches); increment — **1,021,688
  pushes, 0 mismatches** (ordinary/captures/castling/promotions/ep);
  NPS — off 6.05M → on 3.76M = **−37.8%** (design doc expected 40–60%);
  threatcost — ~1.7 µs/call through ctypes (upper bound; in-search cost is
  inside the NPS delta).
- Phase-5 smoke: NNUE selftest all-pass (oracle exact, accumulator exact,
  mates found, fortress 0 at d16, KNvK draw); 100-game self-play smoke:
  no crash, legal play, sane scores, no RSS growth, TT intact.
