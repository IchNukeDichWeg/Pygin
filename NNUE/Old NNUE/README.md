# Old NNUE — retired nets (flat, no subfolders)

Naming convention (mirrors Old Engine/):

- The LIVE net is `NNUE/nets/nnue_vN_<12 hex>.nnue`, e.g.
  `nnue_v1_52724f038139.nnue`. The suffix is the first 12 hex characters
  of the file's own sha256 — Stockfish's `nn-<12 hex>.nnue` convention.
  `vN` bumps once per bootstrap round / retrain on new data.
- `NNUE/train.py` applies the hash itself at export
  (`config.stamp_net_hash`) and prints the final path; nobody computes it
  by hand.
- A small fix on the same data (re-export, tweak, short retrain) bumps
  the minor: `nnue_v1.1_<hash>.nnue`. The content hash changes too — that
  is the point, a re-export is a different file and gets a different name.
- When a new net replaces the live one, `mv` the old file HERE — flat,
  no subfolders. Keep the filename as-is; the hash IS the provenance:

      mv NNUE/nets/nnue_v1_52724f038139.nnue "NNUE/Old NNUE/"

- `toy.nnue` is not a version: it is the pipeline-proof artifact
  (trained on the 100k smoke set), stays in NNUE/nets/, and is exempt
  from the hash because the selftest and smoke open it by fixed path.

Why the hash is in the name at all: a net is an opaque 3 MB blob, so two
different nets under one filename are indistinguishable by eye — and that
is precisely the mistake that silently invalidates an A/B (you believe you
screened v2; you screened v1). With the content hash in the name, a
mismatched net is a wrong FILENAME, which is impossible to miss.

Live nets in `NNUE/nets/` are TRACKED as of 2026-07-27, so a fresh clone on
a rented A/B box has the net without a manual copy. Nets RETIRED into this
folder are still ignored: keeping every superseded net in the working tree
would grow the repo without bound, and git history already holds them from
when they were live. Per-net provenance (dataset, epochs, val
loss, screen result) belongs in the training log / improvements.md entry
for that round.
