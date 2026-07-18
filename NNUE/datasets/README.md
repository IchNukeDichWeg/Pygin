# NNUE/datasets/ — training data lives here (gitignored)

Real training data goes in this directory as `.pygdata` files (format v1,
see `../data_format.py` and DESIGN_nnue.md). Nothing in here is tracked —
the repo is public and datasets are multi-GB.

Current contents (local only):
- `smoke100k.pygdata` — 100k-position pipeline-proof dataset (8.8 MB),
  generated 2026-07-18 by `python3 NNUE/gen_data.py`. Plumbing proof ONLY;
  never train a real net on it.

The real Phase-6 dataset (~50M positions) is generated on the match server:
see NNUE/README.md "Generating real training data" for the exact command.
