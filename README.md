# Pygin

A from-scratch chess engine written in **Python + C**. The search and
evaluation are hand-written (no NNUE, no external engine); the
[`python-chess`](https://pypi.org/project/chess/) library is used **only** for
board representation, move generation and legality checking.

The engine exists in two forms. `engine.py` is the reference implementation —
a full Python engine whose evaluation and move generation are ported to C
(`eval_c.c`, `movegen.c`). The current strongest engine is the **C search
core** (`cengine.py` + `csearch.c`): the *entire* per-node search loop — board,
ordering, transposition table, pruning, quiescence and a bit-exact port of the
evaluation — runs in C, with Python keeping only the root layer (iterative
deepening, time management, opening book). It reaches ~3.5M+ nodes/s, roughly
40× the Python core, and searches several plies deeper at the same time
control. `engine.py` remains the single source of truth for evaluation: the C
core syncs every eval parameter from it at startup.

**Strength:** the Python engine (`engine.py`) measures around **2440–2450 Elo**
single-threaded (level with Stockfish 18 capped at UCI_Elo 2450 over 2,500
games). The **C search core** is far stronger and still climbing: it beat the
Python engine **29–1–0** on arrival, and the C-era ledger has since added
**≈ +135 Elo** of A/B-confirmed gains (v31 → v38: IIR, TT persistence, check
extensions, qsearch-TT, noisy-only + staged move generation, plus exact-PV and
score-hygiene correctness releases). Against
**full-strength** Stockfish 18 it scores **~93%** at rook odds and roughly
**~70%** at knight odds (knight-odds percentages are hardware/environment-
dependent — compare only runs from the same machine).

### Version progression

Speed (nodes/s) and search depth reached from the **starting position** in a
fixed single-threaded budget, for every version, plus the A/B Elo gain over
the immediately preceding version where one was measured. Regenerate with
`python3 bench_progress.py`.

| Ver | NPS (startpos) | Depth | Elo Δ vs prev | Milestone |
|----:|---------------:|------:|:--------------|:----------|
| 1  | 16 k | 4  | — | first working engine |
| 2  | 31 k | 7  | — | |
| 3  | 30 k | 7  | — | |
| 4  | 32 k | 7  | — | |
| 5  | 31 k | 8  | — | |
| 6  | 31 k | 8  | — | |
| 7  | 32 k | 8  | — | |
| 8  | 35 k | 8  | — | |
| 9  | 26 k | 9  | — | late-move pruning |
| 10 | 10 k | 8  | — | |
| 11 |  8 k | 8  | — | |
| 12 |  8 k | 8  | — | |
| 13 |  7 k | 7  | — | |
| 14 | 32 k | 9  | — | |
| 15 | 32 k | 10 | — | |
| 16 | 41 k | 10 | — | C evaluation (`eval_c.c`) |
| 17 | 55 k | 11 | — | C move generator (`movegen.c`) |
| 18 | 57 k | 10 | — | |
| 19 | 55 k | 11 | — | Zobrist / shared TT |
| 20 | 60 k | 12 | — | |
| 21 | 60 k | 12 | — | |
| 22 | 59 k | 12 | — | |
| 23 | 63 k | 12 | — | |
| 24 | 59 k | 12 | +11.75 ±6.8 ² | (measured over the v21→v24 span) |
| 25 | 60 k | 12 | — | |
| 26 | 68 k | 12 | — | |
| 27 | 84 k | 12 | — | |
| 28 | 86 k | 13 | — | NPS-optimisation era |
| 29 | 85 k | 12 | — | soft-stop time management |
| 30 | 84 k | 12 | — | last pure-Python version |
| 31 | 2.7 M | 17 | ≈ +215 ¹ | **C search core** (whole per-node loop in C) |
| 32 | 2.7 M | 18 | +7.30 ±6.8 | internal iterative reduction |
| 33 | 2.6 M | 21 | +23.52 ±6.8 | transposition table kept warm across moves |
| 34 | 2.7 M | 21 | +6.81 ±6.8 | check extensions |
| 35 | 3.6 M | 21 | ≈ +72 | noisy-only qsearch gen + qsearch TT |
| 36 | 3.9 M | 22 | +24.67 ±6.8 | staged move ordering |
| 37 | 3.9 M | 19 | +0.17 ±6.8 | exact PV (correctness) |
| 38 | 4.1 M | 18 | +1.36 ±6.8 | score-hygiene batch (correctness) |
| 39-dev | 4.5 M | 18 | *A/B pending* | incremental Zobrist + NPS batch |

¹ v31 is the C-core arrival: **29–1–0** vs v30 in a smoke match; the ≈ +215
is an external / odds-derived estimate, **not** a same-time-control A/B.
² The Python era has no per-version A/B; the one measured span is
v21 → v24 = **+11.75 ±6.8** over 10,000 games.

The C-era Elo figures are 10,000-game A/B matches vs the immediately previous
version (cumulative **≈ +135** over v31). **Time control differs by era**
(45 s + 0.10 for v32–v36, 50 s + 0.20 for v37–v39), so cross-era Elo is not
one currency. **NPS is the clean speed axis; depth reached in a fixed budget
also reflects selectivity** — v37/v38 search more nodes per ply (exact PV
re-searches PV nodes, the correctness batch adds quiescence draw checks), so
their depth dips even as raw NPS keeps climbing. Absolute NPS is
hardware-dependent (an Apple-Silicon reading); the trend is the signal.

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
- **C search core** (`csearch.c`, driven by `cengine.py`): the whole per-node
  loop in C — board, staged move ordering, array TT (kept warm across moves,
  probed in quiescence), pruning, quiescence and a bit-exact port of the
  evaluation (verified over 3M positions) — at ~3.5M+ nodes/s.
  `cuci.py` exposes it as a UCI engine.
- **Lazy SMP:** the C core uses pthreads with a lock-free shared TT (opt-in
  via the UCI `Threads` option); the Python engine has a multi-process
  variant (`smp.py`, `shared_tt.py`).
- **Optional** Polyglot opening book (`Perfect2023.bin` bundled) and online
  Syzygy tablebase probing.

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

To check the installation health at any time (C libraries loaded with the
right ABI, move generation exact, the Python search reproducing the reference
position node-for-node, the C search core running a fixed-depth ladder to
depth 12 with a throughput/NPS probe, snapshots ready for A/B matches):

```bash
python3 selftest.py        # a few seconds; exit 0 = everything OK, chainable
```

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

The C search core's library (`csearch.so`) has no separate build script —
re-run `./setup.sh` to rebuild it (it recompiles only what changed).

---

## Running a headless match

`match.py` plays an engine-vs-engine match and prints a live scoreboard +
Elo estimate, writing a full per-game log and a PGN file.

```bash
# C search core vs a saved snapshot: 100 games, 4 parallel workers
python3 match.py cengine.py "Old Engine/34/engine34.py" 100 0 --workers 4
```

Positional arguments are `engine1 engine2 NUM_GAMES OFFSET`. `NUM_GAMES` is a
count of *positions*; each is played twice (once per colour), so the total is
`NUM_GAMES × 2`. Match settings (time control, adjudication, etc.) are edited
at the top of `match.py`. Useful flags: `--book1 / --book2 PATH` give each
engine its own Polyglot book (for book testing), and `--start-pos True` plays
every game from the standard start position instead of the opening file.

**Starting positions:** `match.py` defaults to `UHO_4060_v4.epd`, a set of
balanced openings included in the repo (`fen.txt` is a smaller bundled
fallback). For a larger set (e.g. `UHO_Lichess_4852_v1.epd`, 174 MB) see the
[official Stockfish books repo](https://github.com/official-stockfish/books)
and point `FEN_FILE` at it in `match.py`. These seed the games; an engine's own
in-play opening book (`Perfect2023.bin`, bundled) is separate.

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
| `cbench.py` | NPS benchmark for the C search core. |
| `cuci.py` | UCI host for the C search core (`Threads` / `OwnBook` / `UseTB` options). |
| `fit_wdl_model.py` | Fit the win/draw/loss model from match logs (`wdl_model.json`; `wdl.py` reads it). |

---

## Project layout

```
engine.py              the reference Python engine (search + eval orchestration)
cengine.py             root driver for the C search core (the strongest engine)
csearch.c              the whole per-node search loop in C (built to .so)
eval_c.c / movegen.c   C evaluation and move generation (built to .so)
Constants.c/.h         magic-bitboard + attack tables (linked into the .so files)
cuci.py                UCI host for the C search core
smp.py / shared_tt.py  Lazy-SMP multi-process search + lock-free shared TT (Python engine)
time_manager.py        time-control budget calculation
wdl.py                 win/draw/loss model reader (adjudication, GUI eval bars)
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

MIT — see [`LICENSE`](LICENSE).
