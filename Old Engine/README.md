# Old Engine — frozen version snapshots

Every confirmed version of the engine, frozen at the moment it shipped. These
are the **A/B baselines**: a campaign always measures the live tree against
the previous version's snapshot, so these files must never be edited. Fixing
a bug here would silently change what every past result meant.

One directory per version, `Old Engine/<N>/`, each self-contained.

## Running one

Snapshots are `.py` files with an `Engine` class, the same interface the live
engine exposes, so any harness takes them by path:

```bash
python3 match.py cengine.py "Old Engine/53/engine53.py" 500 0 --workers 0 --nodes 1750000
```

The C sources are frozen alongside, and `./setup.sh` compiles each directory's
own `.so` files (step 7, best effort). **The `.so` files are gitignored and
`-mcpu=native`** — they are built per machine and cannot be copied between
them. A fresh clone therefore needs `./setup.sh` before any snapshot will run.

## Layout by era

| Versions | Contents |
| :-- | :-- |
| v1–v15 | `engineN.py` alone — pure Python, no C |
| v16–v19 | `+ eval_c.c/.so` — evaluation ported to C |
| v20–v24 | `+ movegen`, `Constants`, `fastboard`, `smp`, `shared_tt` — the Cython-era experiments |
| v25–v30 | `+ smp.py`, `shared_tt.py`, `time_manager.py`, `uci.py` — last of the Python search |
| v31+ | `engineN.py` (the C driver) `+ engine_eval.py` `+ csearch/eval_c/movegen` sources — the C search core |

## How a C-era snapshot is wired

From v31 the shipped engine is a Python *driver* over a C search core, so a
snapshot is two Python files, not one:

- **`engineN.py`** — the frozen `cengine.py`. Its `_load_pyengine()` loads the
  sibling `engine_eval.py` **by explicit path under a unique module name**,
  never the live repo-root `engine.py`.
- **`engine_eval.py`** — the frozen `engine.py`, which is the eval-parameter
  oracle: the driver reads every eval constant from it and pushes them into
  `csearch.so` at construction.

That indirection is what keeps a snapshot frozen. It became load-bearing at
**v53**, whose entire content is 44 changed eval constants — a snapshot that
imported the live `engine.py` would have tracked every later retune instead of
staying at v53.

## Two rules

1. **Never edit a snapshot.** Its numbers are cited in `DESIGN_c_search_core.md`,
   the README ledger, and every GitHub release.
2. **Verify a new snapshot is node-exact** against the live tree's freshly
   re-pinned `CE_LADDER`, in a *fresh process* — `csearch.so`'s eval globals are
   process-wide and every snapshot's `.so` shares a basename, so two versions in
   one process silently share whichever parameters were pushed last.

Note that `REF_NODES` is the **Python** engine's node count and is not the right
oracle for a C-era snapshot; the ladder is.

Per-version detail — what changed, and the A/B that confirmed it — lives in the
version table in the top-level `README.md` and the ledger in
`DESIGN_c_search_core.md`.
