# NNUE — FI-15 build-out (Phases 1–7 complete; first net trained, unscreened)

The complete NNUE infrastructure for Pygin: data generation, PyTorch
trainer, quantized export, C inference (accumulator + NEON/scalar forward),
and hybrid integration behind one master toggle. **Everything is dormant by
default** — with `cengine.USE_NNUE = False` the search is byte-exact vs
the same build without any of this. The bench signature is NOT a fixed
number to quote: it re-baselines at every ship and every eval change, so
verify by COMPARING (run `bench` before and after an NNUE-side change —
it must not move), never against a number copied from a doc. Reference
points: the build-out was verified at 1,083,772/1,508,415 (v50 era,
2026-07-18) and again at 1,122,753 (v53, 2026-07-22).

The frozen architecture/format contract is **docs/DESIGN_nnue.md → "Phase 1 spec
(FROZEN)"**. Summary: KA8T feature set (8 king buckets, horizontal mirror,
12 planes, IN=6144/perspective; plain-768 = same code path at KB=1) + T16
threat encoding (16 int8 aggregate scalars from one attack-union pass),
net FT→2×256 → [512+16]→32→32→1, int16/int8 quantization QA=127/QB=64,
`.nnue` format v1, `.pygdata` training data format v1.

State (2026-07-26): the 82.4M-position dataset is generated and merged, the
first net is trained, and it passes every acceptance gate. What remains is
the 2k screen → 10k A/B, then bootstrap rounds. Round 1 is EXPECTED to lose:
the net costs ~58% of NPS, which the `--nodes` instrument charges honestly,
so it must find ~70–90 Elo of pure evaluation quality just to break even.

## Layout

| file | what |
|---|---|
| `nnue.c` | the entire C side, `#include`d by `csearch.c` (single TU — no build change). Loader, feature extraction, T16, F49-31 ply-indexed accumulator stack, NEON+scalar forward, verify mode, oracles |
| `config.py` | frozen constants shared by every tool (the C loader cross-checks them per net file) |
| `data_format.py` | `.pygdata` v1 writer/reader/merger (88-byte records, mmap-able) |
| `gen_data.py` | self-play labeling harness (F49-30 + F5-19 rules baked in) |
| `logs_to_pygdata.py` | converts match.py A/B battle logs into training data (same filters; per-side `--allow` version gate — pre-v49/CYCLE_DETECT sides only; tested: 20k-game log → 791,168 positions, 0 desyncs) |
| `verify_labels.py` | label audit: exact-reproduction gate + FI-29 shaping report |
| `nnue_ref.py` | numpy truth: feature extraction, `.nnue` I/O, EXACT quantized reference forward |
| `model.py`, `train.py` | PyTorch float model (QAT-style clipping) + trainer + quantized export |
| `verify_c.py` | Phase-4 gates: `forward`, `increment`, `nps`, `threatcost` |
| `selftest_nnue.py` | NNUE unit checks (spawned by `selftest.py`; exit 42 = skip when no net) |
| `selfplay_smoke.py` | 100-game stability smoke (`--net`, defaults to the toy net) |
| `verify.py` | runs every acceptance gate and prints one verdict — the command to use after training |
| `tools/` | browser inspector, its local build script, and the HCE-vs-NNUE comparator |
| `datasets/`, `nets/`, `checkpoints/`, `venv/` | local-only (gitignored) |

## Setup (one-time, training machine only)

The engine side needs nothing beyond `./setup.sh`. Training needs PyTorch
(system python here is 3.14; torch wants 3.12):

```
python3.12 -m venv NNUE/venv && NNUE/venv/bin/pip install torch numpy python-chess
```

## Commands (all from the repo root)

Generate data (any size). `--workers 0` means all cores but one and is the
only value worth passing — the boxes differ, and a hardcoded count is wrong
on the next one:

```
python3 NNUE/gen_data.py NNUE/datasets/run1.pygdata --positions 100000 --nodes 5000 --workers 0 --seed 42
```

Labeling runs a 1.5 MB TT (`LABEL_TT_BITS`), not the engine's 48 MB default:
a cold TT is reset before every labeling search, so the table is memset once
per move, and wiping 48 MB to serve a 5,000-node search is ~500x the memory
traffic of the search itself. That is shared-bus work, so it degrades as
worker count rises — it cost a 111-worker box a 26x slowdown before it was
found. `verify_labels.py` mirrors the constant; the fingerprint prints `tt=`
so a mismatch shows up as version skew rather than phantom corruption.

Two stop rules: `--positions N` collects exactly N positions (game count
varies); `--games N` (overrides it) plays exactly N games and keeps every
extracted position — measured yields ~65/game random, ~72/game book,
~20/game endgame. The Phase-6 recipe is games-targeted: 750k random +
50k UHO + 250k endgame ≈ ~57M positions.

Without `--book`, each game opens with a uniform-legal random walk of
`LABEL_MIN_RANDOM_PLIES..LABEL_MAX_RANDOM_PLIES` plies (6..12). The floor is
6 because the search is deterministic at fixed nodes, so two games that draw
the same opening are byte-identical duplicates — and there are only
perft(4)=197,281 distinct 4-ply lines, which at 750k games means ~17.5k
collisions (~2% of the slice). perft(6)=119,060,324 makes it ~0. Lowering
the floor re-introduces duplicate training rows; it does not affect the
`verify_labels.py` gate either way.

Opening/coverage modes (mixable into a multi-slice dataset via
`data_format.py merge` — the recommended Phase-6 recipe is random +
UHO-book + endgame slices):

```
--book data/UHO_Lichess_4852_v1.epd   # start games from random book lines
                                   # (O(1) memory: random-offset sampling)
--endgame [--eg-men 14]            # endgame harvest: early win adjudication
                                   # OFF (games reach real endgames), record
                                   # only positions with <= eg-men total men;
                                   # ply-cap games score-adjudicated so the
                                   # WDL label is not a fake draw
```

Audit the labels (hard gate: hmc==0 records reproduce exactly):

```
python3 NNUE/verify_labels.py NNUE/datasets/run1.pygdata --sample 200
```

Check a `.pygdata` is intact — header record count vs actual bytes, so a
truncated transfer is caught before it is trusted (a short file still parses
and still mmaps):

```
python3 NNUE/data_format.py check NNUE/datasets/*.pygdata
```

Train + export (writes `best.pt`, `loss_curve.csv`, and the quantized net):

```
NNUE/venv/bin/python NNUE/train.py NNUE/datasets/run1.pygdata --epochs 20 --chunk 2000000 --out NNUE/nets/nnue_v1.nnue
```

The holdout is GAME-granular: every 20th contiguous block of `--val-block`
(5000) records, not every 20th record. gen_data writes a game's positions
contiguously and the WDL label is per game, so a strided split put 3–4
positions of every game in val and the rest in train — measured 100.0% of
val games also present in train, which flatters val and drags the chosen
best-epoch later. Blocks cut that to 3.9%.

Other flags that exist because a multi-hour run needs them: `--checkpoint-dir`
(two runs sharing one directory overwrite each other's only artifact),
`--export-only` (recover a net from `best.pt` if a run died), and a per-chunk
heartbeat so an epoch is not silent for 15 minutes. Ctrl-C exports the best
epoch rather than discarding the run.

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
byte-exact vs the same build with it off; after any NNUE-side change run
`bench` and `selftest.py` and confirm the signature is UNCHANGED from
before the change (the absolute value moves with every engine ship).

## Accepting a trained net

One command runs every gate and prints a verdict; exit 0 means safe to arm.

```
python3 NNUE/verify.py --nnue NNUE/nets/nnue_v1_<hash>.nnue
```

| gate | what it proves | |
|---|---|---|
| `forward` | C forward == numpy reference | HARD |
| `increment` | accumulator == full refresh | HARD |
| `selftest` | oracle, mates, draws, fortress | HARD |
| `smoke` | 100 self-play games, no crash/leak | HARD |
| `nps` | net ON vs OFF throughput | report only |

`nps` deliberately cannot fail the run: the `--nodes` screen already charges
the net's speed cost by scaling each side's node budget, so it is a number to
know, not a bar to clear. `--quick` shrinks the samples 10x for a plumbing
check — not an acceptance run.

## Screening a net

`engine_nnue.py` (repo root) is a three-line subclass of `cengine.Engine`
with `USE_NNUE = True`, so arming a net never means editing `cengine.py` and
arming it for cuci, the selftest, the workbench and every other consumer at
once.

```
python3 match.py engine_nnue.py "Old Engine/55/engine55.py" 1000 --workers 0 --nodes 1750000
```

Watch the calibration lines: the HCE side should be granted proportionally
MORE nodes, because `--nodes` equalises TIME, not nodes. If both sides get
the same budget, the NNUE side did not arm.

A wrong `NNUE_FILE` raises from the loader rather than falling back to the
HCE — otherwise a typo would produce an HCE-vs-HCE run reading as "the net is
exactly as strong as the old engine", the most convincing wrong result
available.

## Inspecting a net — NNUE/tools/

| | |
|---|---|
| `nnue_inspector.html` | browser tool: per-piece values, T16 decomposition, legal moves, drag-and-drop editing. Verified bit-exact against the C engine on 40 exported positions, and its move generator against python-chess on 186 |
| `make_local.py` | wraps the inspector into a standalone document for opening off disk |
| `compare_eval.py` | HCE vs NNUE on the same positions, both numbers from the engine (`csearch_eval_white` / `nnue_eval_oracle`) |

## Net naming & retirement (mirrors Old Engine/)

Live net: **`NNUE/nets/nnue_vN_<12 hex>.nnue`** — e.g.
`nnue_v1_52724f038139.nnue`. The suffix is the first 12 hex characters of
the file's own sha256, Stockfish's `nn-<12 hex>.nnue` convention. A net is
an opaque 3 MB blob, so two different nets under one filename are
undetectable by eye — and that is exactly what silently invalidates an
A/B (you think you screened v2 and you screened v1). With the hash in the
name, a mismatched net is a wrong *filename*, which is impossible to miss.
`vN` stays because the hash says nothing about ORDER, and the ordering is
the part a human reads: vN bumps per bootstrap round / retrain on new
data, a small same-data fix bumps the minor (`nnue_v1.1_<hash>.nnue`).

`NNUE/train.py` applies the hash itself at export
(`config.stamp_net_hash`): pass `--out NNUE/nets/nnue_v1.nnue` and it
writes, hashes, renames, and prints the real name in its
`exported ... -> PATH` line. Use that printed name in `--net` flags and in
`cengine.NNUE_FILE`. Re-stamping is idempotent (an existing hash suffix is
replaced, not appended).

Retired nets move FLAT into `NNUE/Old NNUE/`
(`mv NNUE/nets/nnue_v1_<hash>.nnue "NNUE/Old NNUE/"`). `toy.nnue` is the
pipeline-proof artifact, not a version, and is **exempt** from the hash —
`selftest_nnue.py` and `selfplay_smoke.py` open it by fixed path. cengine's
`NNUE_FILE` default names the current live net (a placeholder until v1
exists); all `.nnue` files are gitignored (public repo) — only the Old
NNUE README is tracked.

## Generating real training data (Phase 6 — DONE, see below)

On a generation server (~50M positions, see docs/DESIGN_nnue.md for the
rationale; TC-free — the labeling budget is fixed NODES, so machine
speed changes wall clock only, never label quality; split across
servers with different --seed values and merge):

```
python3 NNUE/gen_data.py NNUE/datasets/random750k.pygdata --games 750000 --nodes 5000 --workers 95 --seed 1
```

Wall-clock: measured ~70 positions/s per worker locally (~1 s/game at
5,000 nodes/move) -> est. **~4-6 h** for the full three-slice mix on one
95-worker server (~2-3 h on two; generation games are ~70x shorter than
50+0.2 match games). Supplementary
source: existing A/B battle logs convert via `logs_to_pygdata.py` (deeper
50+0.2 labels; version-gate the sides to pre-v49 engines per F49-30, then
`data_format.py merge` the results with the self-play file).

Then: `verify_labels.py` on the result, train with `--epochs 20`-ish
(watch `loss_curve.csv`; val must fall and not diverge), export, run all
four `verify_c.py` gates + `selfplay_smoke.py`, and only then the 2k
screen per docs/DESIGN_nnue.md Phase 6.

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
