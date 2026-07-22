# Old NNUE — retired nets (flat, no subfolders)

Naming convention (mirrors Old Engine/):

- The LIVE net is `NNUE/nets/nnue_net_vN.nnue` (v1, v2, ... — one bump
  per bootstrap round / retrain on new data).
- A small fix on the same data (re-export, tweak, short retrain) bumps
  the minor: `nnue_net_v1.1.nnue`.
- When a new net replaces the live one, `mv` the old file HERE — flat,
  no subfolders. Keep the filename as-is so the history stays readable:

      mv NNUE/nets/nnue_net_v1.nnue "NNUE/Old NNUE/"

- `toy.nnue` is not a version: it is the pipeline-proof artifact
  (trained on the 100k smoke set) and stays in NNUE/nets/.

The `.nnue` files themselves are gitignored everywhere (public repo, no
binaries) — this folder tracks only this README; the weight files live
on the machines that use them. Per-net provenance (dataset, epochs, val
loss, screen result) belongs in the training log / improvements.md entry
for that round.
