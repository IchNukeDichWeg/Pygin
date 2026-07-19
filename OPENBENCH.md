# Running Pygin on OpenBench

[OpenBench](https://github.com/AndyGrant/OpenBench) is a distributed
SPRT-testing framework (Fishtest-style): a web server hands out paired-game
workloads, workers clone the engine at a given commit, build it, verify the
`bench` node signature, and play games via cutechess. This repo is
OpenBench-compliant as of v51:

- **`Makefile`** (repo root): `make EXE=<name> CC=<cc>` produces a single
  self-contained UCI binary at `./<name>` (PyInstaller onefile bundling
  cuci.py + the C libraries + the opening book) — the single-file artifact
  the OpenBench client caches and moves around.
- **CLI bench**: `./<name> bench` prints `<nodes> nodes <nps> nps` and
  exits — the signature check every worker runs. Node count is
  deterministic per machine class (v51 on the dev Mac: `1083772`).

## Worker prerequisites

Each worker machine needs, besides the usual OpenBench client deps:

```
pip3 install python-chess pyinstaller requests
```

plus a C compiler and a `cutechess-cli` binary (the client downloads one on
common platforms; otherwise build it and point the client at it).

## Server setup (once, e.g. on the rented box)

```
git clone https://github.com/AndyGrant/OpenBench.git
cd OpenBench && pip3 install -r requirements.txt
python3 manage.py migrate && python3 manage.py createsuperuser
python3 manage.py runserver 0.0.0.0:8000
```

Then register Pygin as an engine in the OpenBench config (the config
format moves between OpenBench versions — follow the repo's wiki page
"Adding New Engines"; the values that matter are below):

| Field | Value |
|---|---|
| Source / repo | `https://github.com/IchNukeDichWeg/Pygin` |
| Build path | `.` (the Makefile is at the repo root) |
| Bench | measured **on the worker fleet**: `make EXE=pygin && ./pygin bench` |
| Base branch | `main` |

## Starting a worker

```
python3 Client/Client.py -U <user> -P <pass> -S http://<server>:8000 -T <threads>
```

## Pygin-specific caveats

1. **Homogeneous workers only.** The build uses `-march=native` /
   `-mcpu=native`, and float rounding in the LMR log table drifts the node
   signature by a handful of nodes across CPU microarchitectures (the same
   benign drift the selftest ladder shows between the dev Mac and the A/B
   server). A mixed fleet would fail cross-worker bench verification.
   Register the bench value measured on the fleet's machine class, not the
   dev Mac's.
2. **Set `OwnBook=false` on both engines** in every test's option fields.
   OpenBench supplies opening diversity via its own book; Pygin's bundled
   Polyglot book would fight it.
3. **Onefile cold start.** The binary self-extracts per launch (~0.3–1 s on
   Linux tmpfs, several seconds on macOS). cutechess keeps engines alive
   across a match's games, so this is a once-per-match cost, but don't set
   aggressive engine-startup timeouts.
4. **Build time.** `make` runs PyInstaller (~30–60 s) on top of the C
   compiles. Normal for OpenBench (it builds once per test per worker),
   just slower than a pure-C engine's build.
5. The in-repo A/B harness (`match.py` + GSPRT, the numbered-campaign
   ledger) remains the source of truth for the version ledger; OpenBench
   is an additional/parallel instrument. Keep the SUBSET_SEED-era
   discipline for match.py campaigns regardless of what runs on OpenBench.
