# Pygin

A from-scratch chess engine written in **Python + C**. The search and
evaluation are hand-written (no NNUE, no external engine); the
[`python-chess`](https://pypi.org/project/chess/) library is used **only** for
board representation, move generation and legality checking. The
performance-critical evaluation and move generation are ported to C and loaded
via `ctypes`, so the engine plays at a strong level despite a Python core.

**Strength:** roughly **2440–2450 Elo** single-threaded (measured level with
Stockfish 18 capped at UCI_Elo 2450 over 2,500 games). With a queen removed it
still beats full-strength Stockfish 18 convincingly.

---

## Features

- **Search:** negamax + alpha-beta with PVS, iterative deepening, aspiration
  windows, a transposition table, and quiescence search.
- **Pruning / selectivity:** null-move pruning, reverse-futility and futility
  pruning, late-move reductions (LMR) and late-move pruning (LMP), plus check /
  single-reply / passed-pawn extensions.
- **Move ordering:** TT move, MVV-LVA with capture history, killers,
  counter-moves, the history heuristic, and Static Exchange Evaluation (SEE).
- **Evaluation:** a tapered hand-crafted evaluation (material + piece-square
  tables, pawn structure, king safety, mobility, rook files, bishop pair,
  threats, endgame mop-up), ported to C (`eval_c.c`).
- **C move generator** (`movegen.c`) with magic bitboards, reproducing
  python-chess's move order so the search stays byte-identical.
- **Lazy SMP** multi-process search with a lock-free shared transposition
  table (`smp.py`, `shared_tt.py`).
- **Optional** Polyglot opening book and online Syzygy tablebase probing.

---

## Requirements

`setup.sh` checks for these and installs anything missing (via Homebrew on
macOS, or apt/dnf/pacman/zypper on Linux):

- **Python 3.10+**
- **A C compiler** — `clang` (macOS) or `gcc` (Linux)
- **`python-chess`** (the only third-party Python dependency)
- **Stockfish** — optional, only used for absolute-strength / odds testing
  (`stockfish_engine.py`, `odds.py`)

---

## Setup

```bash
git clone https://github.com/IchNukeDichWeg/Pygin.git
cd Pygin
./setup.sh
```

`setup.sh` installs any missing prerequisites (python3, a C compiler,
stockfish, `python-chess`), builds the C libraries (`eval_c.so`, `movegen.so`)
for your platform, best-effort builds the C libraries for the `Old Engine/`
snapshots (so you can play them head-to-head), and runs a quick self-test.

> If you prefer to keep things isolated, create a virtualenv first
> (`python3 -m venv .venv && source .venv/bin/activate`) and then run
> `./setup.sh`.
>
> **Windows:** the engine builds a Unix shared library, so run it under
> [WSL](https://learn.microsoft.com/windows/wsl/install) (`wsl --install`,
> then `./setup.sh` inside the Ubuntu shell). Git Bash / MSYS2 also works.

To rebuild the C libraries by hand at any time:

```bash
python3 eval_build.py
python3 movegen_build.py
```

---

## Running a headless match

`match.py` plays an engine-vs-engine match and prints a live scoreboard +
Elo estimate, writing a full per-game log and a PGN file.

```bash
# current engine vs a saved snapshot: 100 games, 4 parallel workers
python3 match.py engine.py "Old Engine/26/engine26.py" 100 0 --workers 4
```

Positional arguments are `engine1 engine2 NUM_GAMES OFFSET`. Match settings
(time control, adjudication, opening file, etc.) are edited at the top of
`match.py`.

**Opening book:** `match.py` defaults to `UHO_4060_v4.epd`, a set of balanced
openings included in the repo. A smaller `fen.txt` is also bundled as a
fallback. For a larger book (e.g. `UHO_Lichess_4852_v1.epd`, 174 MB) see the
[official Stockfish books repo](https://github.com/official-stockfish/books)
and point `FEN_FILE` at it in `match.py`.

### Play against Stockfish (optional)

With a `stockfish` binary on your `PATH`:

```bash
STOCKFISH_ELO=2000 python3 match.py engine.py stockfish_engine.py 100 0
# STOCKFISH_ELO=0  -> full strength (used for odds matches)
```

### Material / time odds

`odds.py` runs an odds match (e.g. give one side queen odds). Everything is
configured in the `CONFIG` block at the top of the file, then:

```bash
python3 odds.py
```

---

## Tooling

| Script | Purpose |
|---|---|
| `perft.py` | Move-generator correctness gate vs the published Perft results (`--deep` for the full 1.5 B-node suite). |
| `profile_bench.py` | Real NPS + a per-function bottleneck breakdown in one pass (`--graph` for an HTML report). |
| `nps_history_bench.py` | NPS / depth benchmark across the `Old Engine/` snapshots. |
| `fit_wdl_model.py` | Fit the win/draw/loss model from match logs (`wdl_model.json`). |

---

## Project layout

```
engine.py              the engine (search + evaluation orchestration)
eval_c.c / movegen.c   C evaluation and move generation (built to .so)
Constants.c/.h         magic-bitboard + attack tables (linked into both .so)
smp.py / shared_tt.py  Lazy-SMP multi-process search + lock-free shared TT
time_manager.py        time-control budget calculation
match.py               headless engine-vs-engine match runner
battle_worker.py       per-game worker process used by match.py
stockfish_engine.py    UCI adapter exposing Stockfish through the same API
odds.py                material / time-odds match runner
Old Engine/<N>/        frozen version snapshots (engineN.py + its C sources)
```

`Old Engine/<N>/` holds every historical version, each self-contained, so you
can reproduce the engine's progression and A/B any two versions against each
other.

---

## Notes

- The C `.so` files are **not** committed — they are platform-specific and
  built from source by `setup.sh`.
- If a `.so` fails to load, the engine falls back to a pure-Python evaluation
  and move generator (correct, but several times slower); `setup.sh`'s
  self-test reports which path is active.

## License

MIT — see `LICENSE` if present, otherwise released under the MIT terms.
