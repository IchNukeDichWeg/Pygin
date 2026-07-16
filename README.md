# Pygin

A from-scratch chess engine written in **Python + C**. The search and
evaluation are hand-written (no NNUE, no external engine); the
[`python-chess`](https://pypi.org/project/chess/) library is used **only** for
board representation, move generation and legality checking.

> **Built with the help of [Claude Code](https://claude.com/claude-code).**
> Pygin is developed through human–AI collaboration: the search core, the
> C port, the evaluation, and the whole regimen of A/B-tested improvements
> were designed, implemented, and tuned with Claude Code.

The engine exists in two forms. `engine.py` is the reference implementation —
a full Python engine whose evaluation and move generation are ported to C
(`eval_c.c`, `movegen.c`). The current strongest engine is the **C search
core** (`cengine.py` + `csearch.c`): the *entire* per-node search loop — board,
ordering, transposition table, pruning, quiescence and a bit-exact port of the
evaluation — runs in C, with Python keeping only the root layer (iterative
deepening, time management, opening book). It reaches ~4.3M nodes/s, roughly
50× the Python core, and searches several plies deeper at the same time
control. `engine.py` remains the single source of truth for evaluation: the C
core syncs every eval parameter from it at startup.

**Strength:** the Python engine (`engine.py`) measures around **2440–2450 Elo**
single-threaded (level with Stockfish 18 capped at UCI_Elo 2450 over 2,500
games). The **C search core** is far stronger and still climbing: it beat the
Python engine **29–1–0** on arrival, and the C-era ledger has since added
**≈ +194 Elo** of A/B-confirmed gains (v31 → v48: IIR, TT persistence, check
extensions, qsearch-TT, noisy-only + staged move generation, an
incremental-Zobrist NPS batch, a TT prefetch, a TT-value pruning sharpener,
a qsearch TT-quality batch,
a doubled transposition table, and five correctness releases — exact PV,
score hygiene, FIDE-exact en-passant hashing, verified-null/50-move/TT-policy
batch, cannot-win eval clamp).
Against **full-strength** Stockfish 18 (`odds.py`) it scores **100%** (100/100
games) at queen odds and **93.25%** (400 games) at rook odds — both
saturated, i.e. no longer sensitive enough to show further progress — and
**76.75%** (400 games) at knight odds, the current external yardstick since
queen/rook stopped discriminating. All three were measured on the v31 C-core
baseline (odds percentages are hardware/environment-dependent — compare only
runs from the same machine); the C-era ledger has added **≈ +189 Elo** since
v31, so the true current gap is wider than these numbers show.

### Version progression

Speed (nodes/s) and search depth reached from the **starting position** in a
uniform **5 s** budget (book off, best of N), for every version, plus the A/B
Elo gain over the immediately preceding version where one was measured.
Regenerate the single-thread column with `python3 bench_progress.py`, the
4-thread column with `python3 bench_progress_threads.py 4`.

| Ver | NPS Single Thread | NPS 4 Threads ⁶ | Depth | Elo Δ vs prev | Milestone |
| --: | ----------------: | --------------: | ----: | :------------ | :-------- |
|   1 |              17 k |               — |     4 | —             | first working engine (naive negamax + material eval) |
|   2 |              31 k |               — |     7 | ≈ +120 est ⁵  | search + eval build-out: PVS, futility, LMR, aspiration, pawn/mobility/king-safety eval, book |
|   3 |              31 k |               — |     7 | ≈ +15 est ⁵   | endgame mop-up, contempt draws, counter-moves |
|   4 |              33 k |               — |     8 | ≈ +20 est ⁵   | SEE move ordering + losing-capture pruning |
|   5 |              31 k |               — |     8 | ≈ +3 est ⁵    | recapture extension |
|   6 |              31 k |               — |     9 | ≈ +8 est ⁵    | lone-king endgame eval fix |
|   7 |              32 k |               — |     8 | ≈ +4 est ⁵    | pin evaluation |
|   8 |              35 k |               — |     9 | ≈ +12 est ⁵   | quiescence stand-pat, trade-down simplify, PV extraction |
|   9 |              27 k |               — |    10 | ≈ +12 est ⁵   | late-move pruning, history malus, improving heuristic |
|  10 |              28 k |               — |    10 | ≈ +8 est ⁵    | TT refactor (two-tier + depth-preferred replacement) |
|  11 |              30 k |               — |    10 | ≈ +3 est ⁵    | incremental base eval (byte-identical) |
|  12 |              30 k |               — |    10 | ≈ +4 est ⁵    | check-extension budgeting + max-extensions cap |
|  13 |              31 k |               — |    10 | ≈ +4 est ⁵    | eval-weight retune |
|  14 |              35 k |               — |    10 | ≈ +8 est ⁵    | Syzygy TB probe, internal iterative reduction, pawn hash |
|  15 |              35 k |               — |    10 | ≈ +0 est ⁵    | LMR-divisor tune (tie); probcut tried & removed |
|  16 |              44 k |               — |    11 | (in ³)        | **evaluation ported to C** (`eval_c.c`) |
|  17 |              58 k |               — |    10 | +69 ±16 ³     | **move generation ported to C** (`movegen.c`) |
|  18 |              57 k |               — |    10 | ≈ +0 est ⁵    | incremental Zobrist hashing (off by default; SMP infra) |
|  19 |              57 k |               — |    12 | ≈ +5 est ⁵    | lock-free shared TT, multi-process SMP, packed move word |
|  20 |              61 k |               — |    12 | +45 ±11 ⁴     | rook-on-7th, mobility area, threats; one-call C eval |
|  21 |              62 k |               — |    13 | +16 ±10 ⁴     | capture history, SEE capture pruning, LMR losing captures |
|  22 |              61 k |               — |    12 | ≈ +8 est ²    | nine correctness bug fixes + six NPS wins |
|  23 |              59 k |               — |    13 | ≈ +0 est ²    | Zobrist dispatch de-branching (code quality) |
|  24 |              59 k |               — |    13 | +11.75 ±6.8 ² | TT-dispatch de-branching (± is the v21→v24 span) |
|  25 |              60 k |           227 k |    13 | +2.91 ±11.6   | 18-item bug block; Lazy-SMP production fixes |
|  26 |              72 k |           223 k |    13 | +41.90 ±5.7   | node-identical speed batch |
|  27 |              85 k |           226 k |    13 | +35.17 ±7.7   | node-identical speed batch (+12 %) |
|  28 |              88 k |           238 k |    13 | +13.13 ±6.0   | node-identical speed batch (+4 %) |
|  29 |              89 k |           167 k |    13 | +38.34 ±6.9   | soft-stop time management (P-35) |
|  30 |              88 k |           193 k |    12 | +10.91 ±6.8   | stability-scaled time (U-06); last Python |
|  31 |             2.7 M |          11.3 M |    17 | ≈ +215 ¹      | **C search core** (whole per-node loop in C) |
|  32 |             2.7 M |          10.9 M |    18 | +7.30 ±6.8    | internal iterative reduction |
|  33 |             2.5 M |          10.9 M |    21 | +23.52 ±6.8   | transposition table kept warm across moves |
|  34 |             2.5 M |          10.5 M |    21 | +6.81 ±6.8    | check extensions |
|  35 |             3.6 M |          14.0 M |    20 | ≈ +72         | noisy-only qsearch gen + qsearch TT |
|  36 |             4.0 M |          16.9 M |    22 | +24.67 ±6.8   | staged move ordering |
|  37 |             4.2 M |          16.6 M |    19 | +0.17 ±6.8    | exact PV (correctness) |
|  38 |             4.1 M |          16.1 M |    18 | +1.36 ±6.8    | score-hygiene batch (correctness) |
|  39 |             4.4 M |          18.1 M |    18 | +8.86 ±6.8    | incremental Zobrist + eval-in-TT + NPS batch |
|  40 |             4.3 M |          16.0 M |    18 | +4.31 ±6.8    | FIDE-exact en-passant hashing (correctness) |
|  41 |             4.3 M |          16.4 M |    17 | −2.88 ±6.8    | verified null + 50-move + TT-store policy (correctness) |
|  42 |             4.3 M |          18.6 M |    18 | +3.27 ±6.8    | cannot-win eval clamp (correctness) |
|  43 |             4.0 M |          16.2 M |    18 | +5.18 ±6.8    | verified-null REMOVED (the insurance cost ~1 ply; isolation A/B) |
|  44 |             4.3 M |          16.1 M |    18 | +13.31 ±6.8   | TT prefetch (node-identical, +5–6 % NPS) |
|  45 |             4.3 M |          16.9 M |    18 | +13.52 ±6.8   | TT search value sharpens the pruning eval (same NPS, smarter cuts) |
|  46 |             4.3 M |          15.3 M |    18 | +5.94 ±6.8    | transposition table doubled to 96 MB (borderline; less TT thrash per game) |
|  47 |             4.3 M |          14.7 M |    18 | +3.16 ±6.8    | TT to 192 MB (diminishing) + MultiPV (node-exact off) |
|  48 |             4.3 M |          14.7 M |    18 | +4.73 ±3.2    | qsearch TT-quality batch (TT value sharpens stand-pat; first SPRT accept, 21.6k games) |

**Table notes**

- **Elo Δ** — each figure is an A/B vs the previous version (C-era = 10,000
  games). Cumulative **≈ +189** over v31. **Not summable across the whole
  column:** TCs differ (early Python-era at assorted fast TCs ⁴, v32–36 at
  45+0.10, v37–47 at 50+0.20), so only same-TC spans are comparable.
- **`est`** ⁵ — a feature-based estimate, not an A/B; shown so every row
  carries a figure. Never summed into a rating. The real anchor is ≈2442 by
  v25 (vs SF-2450).
- **Bundled A/Bs** — v16+v17 vs v15 = **+69 ±16** ³; v22–24 vs v21 =
  **+11.75 ±6.8** ²; v31 arrival = **29–1–0** smoke match, ≈+215 is
  odds-derived, not an A/B ¹.
- **NPS 4 Threads** ⁶ — "—" for v1–24 (no reliable SMP: none before v19,
  fragile multi-process before v25). v25–30 = old multi-process SMP; v31+ =
  the C core's pthread Lazy-SMP, so the ~200k→millions jump at v30→v31 is
  partly that methodology change, not pure speedup.
- **Measurement** — absolute NPS is hardware-dependent (Apple Silicon); the
  C-era block (v31+) was re-measured in one uniform session so its rows are
  mutually comparable. Regenerate: `bench_progress.py` / `bench_progress_threads.py 4`.

**The load-bearing NPS jumps**

- **v15→v17, 35k → 58k** — eval then movegen ported to C (`eval_c.c`, `movegen.c`), byte-identical play.
- **v25→v28, 60k → 88k** — node-identical speed batches (hoisted invariants, fewer allocations).
- **v30→v31, 88k → 2.7M (~30×)** — the whole per-node loop moves to C: no interpreter cost, no per-node ctypes crossing. The single largest jump.
- **v34→v36, 2.5M → 4.0M** — noisy-only qsearch generation + staged (lazy) move ordering.
- **v38→v39, 4.1M → 4.4M** — incremental Zobrist hashing + eval cached in spare TT bits.
- **v43→v44, 4.0M → 4.3M** — TT prefetch (the v39 child key hides the probe's cache miss); +13.31 Elo, the biggest NPS win of the C era (~2.7 Elo per 1% NPS).

Some wins don't show as NPS: v39→v40 (ep-key merge) and the v41→v43
verified-null removal are **nodes-to-depth** gains at flat speed.

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
  evaluation (verified over 3M positions) — at ~4.3M nodes/s.
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

## UCI options

`cuci.py` is the UCI engine (`setoption name <Option> value <x>`). Standard
GUI options:

| Option | Type | Default | Range | Purpose |
|:-------|:-----|--------:|:------|:--------|
| `Threads` | spin | 1 | 1–256 | Lazy-SMP search threads |
| `Hash` | spin | 192 | 2–6144 | Transposition-table size (MB); resize wipes the table |
| `MultiPV` | spin | 1 | 1–5 | PV lines reported |
| `OwnBook` | check | true | — | Use opening book |
| `BookFile` | string |  | — | Path to Polyglot `.bin` book (empty ⇒ bundled `Perfect2023.bin`) |
| `UseTB` | check | false | — | Probe the online Lichess Syzygy tablebase at the root (needs network; no local path) |
| `Move Overhead` | spin | 40 | 0–5000 | Clock margin (ms) for GUI/network lag |
| `Premove` | check | false | — | Emit certified instant-reply premoves (opt-in) |
| `UCI_ShowWDL` | check | true | — | Emit `wdl` on info lines (opt-out for strict arenas) |
| `Clear Hash` | button | — | — | Wipe the transposition table without `ucinewgame` |
| `Contempt` | spin | 50 | −100–100 | Draw score bias (cp) when ahead/behind |

Search-tuning knobs (P-26) — defaults are the shipped values; exposed for
automated tuning (chess-tuning-tools), most users should leave them alone:

| Option | Type | Default | Range | Purpose |
|:-------|:-----|--------:|:------|:--------|
| `RFPMargin` | spin | 80 | 20–300 | Reverse-futility margin (cp) |
| `RFPDepth` | spin | 6 | 2–12 | Reverse-futility max depth |
| `FutMargin` | spin | 150 | 40–400 | Futility-pruning margin (cp) |
| `DeltaMargin` | spin | 200 | 50–500 | Quiescence delta-pruning margin (cp) |
| `LMPScale` | spin | 100 | 40–250 | Late-move-pruning count scale (%) |
| `LMRDiv` | spin | 200 | 120–350 | Late-move-reduction divisor (×100) |
| `NullBase` | spin | 2 | 1–4 | Null-move base reduction |
| `NullDiv` | spin | 6 | 3–12 | Null-move depth divisor |
| `AspDelta` | spin | 30 | 10–120 | Aspiration window delta (cp) |
| `SoftStable` | spin | 40 | 20–70 | Soft-stop time fraction, stable best move (%) |
| `SoftUnstable` | spin | 80 | 50–130 | Soft-stop time fraction, best move flips (%) |

---

## Tooling

| Script | Purpose |
|---|---|
| `perft.py` | Move-generator correctness gate vs the published Perft results (`--deep` for the full 1.5 B-node suite). |
| `profile_bench.py` | Real NPS + a per-function bottleneck breakdown in one pass (`--graph` for an HTML report). |
| `nps_history_bench.py` | NPS / depth benchmark across the `Old Engine/` snapshots. |
| `benchmark.py` | NPS / depth / nodes benchmark for the C search core (`--type`, `--threads`, `--hash`, averaged over `--runs`). |
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
