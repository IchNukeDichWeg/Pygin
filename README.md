<div align="center">

# Pygin

**A from-scratch chess engine in Python + C.** The search is hand-written.
Since v58 the evaluation is an HCE/NNUE hybrid: the neural net scores positions
inside the main search, and the hand-crafted eval keeps quiescence. Nothing
calls out to another engine at runtime.<br/>
[`python-chess`](https://pypi.org/project/chess/) is used *only* for board
representation, move generation and legality.

![Strength](https://img.shields.io/badge/placement-~3164_CCRL_Blitz-58a6ff)
![Speed](https://img.shields.io/badge/speed-6.2M_nps-58a6ff)
![Versions](https://img.shields.io/badge/versions-66-8b949e)
![C--era_gains](https://img.shields.io/badge/C--era_gains-%2B474_Elo-f0883e)
![Source](https://img.shields.io/badge/source-MIT-green)
&nbsp;·&nbsp; Built with **[Claude Code](https://claude.com/claude-code)**

</div>

### At a glance

<table>
<tr><td><b>~3164</b></td><td>CCRL Blitz placement of v64, &plusmn;15</td><td><b>6.17M nps</b></td><td>the net costs ~28% of it</td></tr>
<tr><td><b>~+474 Elo</b></td><td>A/B-confirmed, v31&rarr;v66</td><td><b>~19 ply</b></td><td>from startpos in 5 s</td></tr>
<tr><td><b>+93.87 Elo</b></td><td>v58&rarr;v64, measured directly</td><td><b>1.21&times;</b></td><td>single-thread vs v31</td></tr>
<tr><td><b>v53+v54</b> eval lane</td><td>+37.52 &amp; +31.20, the two biggest</td><td><b>1 dependency</b></td><td><code>python-chess</code> only</td></tr>
</table>

<table>
<tr>
<td><img src="docs/elo_progression.svg?v=66" width="100%" alt="Cumulative A/B Elo across the C era, v31=0 climbing to +474 at v66"/></td>
<td><img src="docs/speed_progression.svg?v=66" width="100%" alt="Single-thread speed as a multiple of v31, peaking at 1.52x and ending at 1.21x"/></td>
</tr>
</table>

<table>
<tr>
<td><img src="docs/mate_progression.svg?v=66" width="100%" alt="Mate-finding on mates2000.epd across the C era, rising from 24.4% at v31 to a 51-53% plateau, then settling at 44.0% at v66"/></td>
</tr>
</table>

All three charts are self-play. The v30→v31 C rewrite (~34× faster) is off the left
edge, so v31 is the honest zero. Mate-finding is the one curve that does **not**
track Elo: it plateaus at ~51% by v39, then falls as the search gets more
selective and the net arms (v62 finds 39.9% of `mates2000.epd` at 0.25s, v51
found 52.1%), while every one of those releases measured POSITIVE in games.
Where a tactical suite and an A/B disagree, the A/B decides.

### Two engines, one eval

`cengine.py` + `csearch.c`, the C search core, is the shipped engine and about
50× the Python core: the whole per-node loop, NNUE included, runs in C, and
Python keeps only iterative deepening, time management and the book.
The net scores positions inside negamax while qsearch stand-pat stays on the
hand-crafted eval (an all-NN attempt measured -203 to -273 Elo).
`engine.py` is the reference Python engine and the single source of
*hand-crafted* eval truth, read by the C core at startup; the net's weights live
in `NNUE/nets/`.

### Measured strength

**~3164 ±15 on the CCRL Blitz scale**, from 960 games at 2'+1" on 2026-09-17
with **v64** against two opponents rated on that list (v65 and v66 are not
placed yet).

| Opponent | Games | Score | Result | Places us at |
|---|---|---|---|---|
| rated 2986 | 480 | 73.54% (274W/158D/48L) | **+177.59 ±24.3** | 3163.59 |
| rated 3254 | 480 | 37.29% (68W/222D/190L) | **-90.29 ±18.7** | 3163.71 |

With no early stop, the two placements, from opponents 268 points apart,
agree to **0.12 Elo**. This is a **placement on that
list's scale under our conditions, not a rating from it** (our openings, our
machine, opponents built from source); reproduce with `scripts/rating_ladder.py`.

The old ~2868 Elo was retracted 2026-09-05: the run's Elo cap never reached the
engine subprocess (T-14), and everything before 2026-08-13 came from a harness
that mis-scored repetitions in Stockfish's favour (fixed in `45dd989`). The bug was
near-symmetric between two Pygins, so it never inflated the A/B ledger.

**Odds ladder -- every rung was played against a CAPPED Stockfish** (`odds.py`
never applied `STOCKFISH_ELO`; UCI_Elo 2900 before 2026-08-07, 3000 after, fixed
under B-15), and the f2 series spans two opponents. The full-strength ladder is
still **unmeasured**.

| Rung | Result | Vintage |
|---|---|---|
| Pawn odds f2 | 81.00% / 1,000 games (704W/212D/84L, +251.89 ±35.2) | v59, 2026-08-13 |
| **Pawn odds h2** (active) | 56.75% / 200 games (75W/77D/48L, +47.19 ±38.0) | v62, 2026-09-02 |
| Knight, rook, queen odds | 100%, saturated | v53/v54 |
| vs the Python engine v30 | 195W / 5D / 0L over 200 games | v59, 2026-08-12 |

h2 answers the same question as f2 with 41% fewer games. Why it costs Stockfish
so much less than the other seven pawns is **not explained** (the open-file
hypothesis was rejected).

---

## Version progression

Each of the 66 versions is A/B-tested against the one before it. Regenerate the list and charts with `bench/bench_progress.py` and
`scripts/make_readme_charts.py`.

<details>
<summary><b>Every version in full</b> -- complete milestone + Elo list</summary>

- **v66** -- **a tempo term for the net, and a faster search.** E-05 gives the net the +15 cp tempo bonus the hand-written eval always had; the node-identical NPS audit adds **+11.29% ±0.29%** search-only NPS on x86, **+4.57% ±0.45%** on arm64. Also carried: Chess960. *(Each alone vs v65, **fixed** 10,000 games at 50+0.5 on x86. Tempo: **+25.62 ±4.2**, after a 10+0.1 screen ACCEPTed H1. NPS audit: **+26.32 ±4.1**. No early stop, so the ledger advances: +422 -> **~+474**, assuming the two add (v66 vs v65 as a whole is unmeasured). Bench 672,025 -> 593,598.)*
- **v65** -- **a new evaluation network.** Same architecture and search as v64; `NNUE_FILE` moves to `nnue_v15_4f85b4f247cd.nnue`, plus small fixes and a 4-row NEON dot kernel (4.5% fewer search cycles on arm64). *(**+61.91 ±4.3** over a **fixed** 9,976-game budget at 50+0.5 on x86. No early stop, so the ledger advances: +360 -> **+422**. 10+0.1 arm64 screen: +60.46 ±8.5, ACCEPT H1. Bench 785,901 -> 672,025.)*
- **v64** -- **a new net, and four settings that only pay together.** Four search parameters that each measured as nothing alone (`LMR_DIV` 170, `SEE_SCALED_K` 75, two clock settings) pass as one change; a profile-guided build adds **+4.7%** speed, node-identical. *(Bundle: **+6.19 ±4.1** over 11,896 games at 50+0.5, SPRT[0,4] LLR +2.969 **ACCEPT H1**; its first tranche ran a **full** 10,000 games at +5.94 ±4.4. Net, on top of the bundle: **+78.82 ±8.3** over 3,004 games, LLR +9.264 **ACCEPT H1**. Bench 1,123,567 -> 785,901.)*
- **v63** -- **losing captures near the leaves are no longer searched, and v62's rejected TT rule is gone.** FI-38 skips a SEE-losing capture (over 50cp per remaining ply) at depth 6 or less, off the PV, on v61's core again. *(HEAD vs v61: **+20.95 ±7.9** over 3,006 games, SPRT[0,4] LLR +3.049 **ACCEPT H1**. FI-38 K=50 on top: **+9.50 ±6.0** over 6,802 games, LLR +2.961 **ACCEPT H1**, after +9.34 ±5.3 on the 10+0.1 screen; K=75 and K=100 were NULL. All at 50+0.5 and **stopped early, so the ledger is not advanced**; a fixed-budget run vs v61 is owed. Bench 1,203,792 -> 1,123,567.)*
- **v62** -- **the aspiration window stopped fighting itself.** A score-scaled opening delta, a midpoint `beta` on fail-low and 1.5&times; growth end the low/high oscillation out to the 1920 fallback. v62 also shipped the already-rejected b05 FI-115 completion (ACCEPT H0, LLR -5.98), shared by both A/B arms and reverted in v63. *(**+18.40 ±7.6** at 50+0.5 over 3,439 games, SPRT[0,4] LLR +2.959 **ACCEPT H1**, and **+2.969 ACCEPT** on the 10+0.1 screen. Both **stopped early, so the magnitude is bound-biased and the ledger is not advanced on it**; a fixed-budget run is owed. Bench 1,140,099 -> 1,203,792.)*
- **v61** -- **dead entries in the transposition table are now evicted first.** An entry whose piece count exceeds the root's can never recur, so it goes first. Plus a two-cache-line TT prefetch, +1.09% NPS. *(**+15.89 ±4.2** over a **fixed** 10,000-game budget at 50+0.5, 192 MiB both sides. No early stop, so the ledger advances: +338 -> **+354**. A cold-TT bench cannot see the rule, so the signature stays 1,140,099.)*
- **v60** -- **the first net trained on real games, not generated ones.** Only `NNUE_FILE` moves, to `nnue_v12_bf86c4ced057.nnue`, learned from **24,825,823 positions harvested out of Pygin's own A/B match logs** with the game-result term off (`LAMBDA 1.0`), because **1,479/1,479** of those logs' repetition draws were phantom. *(GSPRT[0,4] LLR **+2.957 ACCEPT** at 793 pairs vs `Old Engine/59`, TIMED 50+0.5, 1,594 games -> +42.50 ±17.3, **stopped early, so the magnitude is bound-biased and the ledger is not advanced on it**. Against the v10 net it drew over a full 10,000 games.)*
- **v59** -- **lazy NNUE evaluation, and the first release measured as exactly the config that ships.** `LAZY_NNUE` skips the net's forward pass wherever a cheap bound already decides the node (bench 1,074,820 -> **1,214,534**). v58's +19.11 had been measured with it ON while v58 shipped it OFF. *(GSPRT[0,4] LLR **+2.950 ACCEPT** at 2,264 pooled pairs vs `Old Engine/58`, TIMED 50+0.5 on x86 -> **+13.84 ±6.4**, stopped early so the magnitude is bound-biased.)*
- **v58** -- **the first HCE/NNUE hybrid, and the first net that pays.** `USE_NNUE` arms `nnue_v4_6f910e35bb1e.nnue` inside negamax; the Elo is *net* of the roughly 30% NPS it costs. The only change from v3, which read **+0.52 ±6.8**, is the **learning-rate schedule**: cosine in place of flat took held-out val 0.074417 -> 0.066663. *(**+19.11 ±7.8** over 3,404 games TIMED 50+0.20 on x86, GSPRT[0,4] LLR **+2.950 ACCEPT** at 1,702 pairs. An arm64 confirmation is owed: v3 read +5.70 ±4.6 there. Bench 1,145,629 -> **1,074,820**.)*
- **v57** -- **the last pure-HCE release.** Host layer only and **node-identical** (bench 1,145,629), so no A/B was spent: a ponderhit honours the soft-stop (1.666 s → 0.686 s), the soft-stop knobs are on UCI, and `cuci.py` stops overwriting them with a hardcoded 0.55.
- **v56** -- **ProbCut**: a qsearch filter at `beta + 200` is confirmed by a reduced-depth search before anything is cut. Cuts **21.6% of nodes** at fixed depth (bench 1,461,732 → 1,145,629). *(**+11.44 ±6.9** over 5,924 games TIMED 50+0.20, GSPRT[0,4] LLR **+2.953 ACCEPT**; the `--nodes` campaign read **+4.11 ±4.2** over 21,806 games, so the fixed-node instrument reads CONSERVATIVE.)*
- **v55** -- **node-identical speed pair**: FI-11 pin-aware legality + FI-42 the (mg,eg,phase) accumulator on Board. Same moves (bench UNCHANGED at 1,461,732), found faster: **+8.3% NPS on x86, +13.5% on arm64**. *(**+9.66 ±8.2** over 6,874 games, TIMED 50+0.20, GSPRT[0,4] LLR +2.946 ACCEPT; **~1.16 Elo per 1% NPS**)*
- **v54** -- **PST retune** (736 piece-square entries fitted, 735 values moved; GSPRT[0,2] LLR +7.806, 11.7k games, second-largest release) *(**+31.20 ±5.6**)*
- **v53** -- **Texel eval retune** (44 scalars refitted on 4M own-self-play positions, game-result labels; LLR +9.918, 12k pooled games, largest single release) *(**+37.52 ±6.3**)*
- **v52** -- null-move refinements (no double null + eval-scaled R; 12k pooled games) *(+6.63 ±4.5)*
- **v51** -- root-move LMR (late quiet root scouts reduced; 9.3k pooled games) *(+11.12 ±5.3)*
- **v50** -- rule50 TT staleness guard + depth-independent TT mate handling (null kept as correctness) *(+1.60 ±6.8)*
- **v49** -- cuckoo upcoming-repetition (forcible draw scored one ply early; null kept as correctness) *(+0.97 ±6.8)*
- **v48** -- qsearch TT-quality batch (TT value sharpens stand-pat; 21.6k games) *(+4.73 ±3.2)*
- **v47** -- TT to 192 MB (diminishing) + MultiPV (node-exact off) *(+3.16 ±6.8)*
- **v46** -- transposition table doubled to 96 MB (borderline) *(+5.94 ±6.8)*
- **v45** -- TT search value sharpens the pruning eval (same NPS, smarter cuts) *(+13.52 ±6.8)*
- **v44** -- TT prefetch (node-identical, +5–6 % NPS) *(+13.31 ±6.8)*
- **v43** -- verified-null REMOVED (the insurance cost ~1 ply) *(+5.18 ±6.8)*
- **v42** -- cannot-win eval clamp (correctness) *(+3.27 ±6.8)*
- **v41** -- verified null + 50-move + TT-store policy (correctness) *(-2.88 ±6.8)*
- **v40** -- FIDE-exact en-passant hashing (correctness) *(+4.31 ±6.8)*
- **v39** -- incremental Zobrist + eval-in-TT + NPS batch *(+8.86 ±6.8)*
- **v38** -- score-hygiene batch (correctness) *(+1.36 ±6.8)*
- **v37** -- exact PV (correctness) *(+0.17 ±6.8)*
- **v36** -- staged move ordering *(+24.67 ±6.8)*
- **v35** -- noisy-only qsearch gen + qsearch TT *(≈ +72)*
- **v34** -- check extensions *(+6.81 ±6.8)*
- **v33** -- transposition table kept warm across moves *(+23.52 ±6.8)*
- **v32** -- internal iterative reduction *(+7.30 ±6.8)*
- **v31** -- **C search core** (whole per-node loop in C) *(29W/1D/0L gate ¹)*
- **v30** -- stability-scaled time (U-06); last Python *(+10.91 ±6.8)*
- **v29** -- soft-stop time management (P-35) *(+38.34 ±6.9)*
- **v28** -- node-identical speed batch (+4 %) *(+13.13 ±6.0)*
- **v27** -- node-identical speed batch (+12 %) *(+35.17 ±7.7)*
- **v26** -- node-identical speed batch *(+41.90 ±5.7)*
- **v25** -- 18-item bug block; Lazy-SMP production fixes *(+2.91 ±11.6)*
- **v24** -- TT-dispatch de-branching (± is the v21→v24 span) *(+11.75 ±6.8 ²)*
- **v23** -- Zobrist dispatch de-branching (code quality) *(≈ +0 est ²)*
- **v22** -- nine correctness bug fixes + six NPS wins *(≈ +8 est ²)*
- **v21** -- capture history, SEE capture pruning, LMR losing captures *(+16 ±10 ⁴)*
- **v20** -- rook-on-7th, mobility area, threats; one-call C eval *(+45 ±11 ⁴)*
- **v19** -- lock-free shared TT, multi-process SMP, packed move word *(≈ +5 est ⁵)*
- **v18** -- incremental Zobrist hashing (off by default; SMP infra) *(≈ +0 est ⁵)*
- **v17** -- **move generation ported to C** (`movegen.c`) *(+69 ±16 ³)*
- **v16** -- **evaluation ported to C** (`eval_c.c`) *((in ³))*
- **v15** -- LMR-divisor tune (tie); probcut tried & removed *(≈ +0 est ⁵)*
- **v14** -- Syzygy TB probe, internal iterative reduction, pawn hash *(≈ +8 est ⁵)*
- **v13** -- eval-weight retune *(≈ +4 est ⁵)*
- **v12** -- check-extension budgeting + max-extensions cap *(≈ +4 est ⁵)*
- **v11** -- incremental base eval (byte-identical) *(≈ +3 est ⁵)*
- **v10** -- TT refactor (two-tier + depth-preferred replacement) *(≈ +8 est ⁵)*
- **v9** -- late-move pruning, history malus, improving heuristic *(≈ +12 est ⁵)*
- **v8** -- quiescence stand-pat, trade-down simplify, PV extraction *(≈ +12 est ⁵)*
- **v7** -- pin evaluation *(≈ +4 est ⁵)*
- **v6** -- lone-king endgame eval fix *(≈ +8 est ⁵)*
- **v5** -- recapture extension *(≈ +3 est ⁵)*
- **v4** -- SEE move ordering + losing-capture pruning *(≈ +20 est ⁵)*
- **v3** -- endgame mop-up, contempt draws, counter-moves *(≈ +15 est ⁵)*
- **v2** -- search + eval build-out: PVS, futility, LMR, aspiration, pawn/mobility/king-safety eval, book *(≈ +120 est ⁵)*
- **v1** -- first working engine (naive negamax + material eval) *(--)*

</details>

<details>
<summary><b>Reading the table</b> (footnotes & caveats)</summary>

- **Elo Δ** is the A/B vs the previous version (C-era = 10,000 games). It is
  not summable down the whole column, because the TCs differ (Python era
  assorted ⁴, v32–36 at 45+0.10, v37–47 at 50+0.20, v48+ on `--nodes`).
- **`est` ⁵** is a feature-based estimate, not an A/B; the real anchor is ≈2442
  by v25.
- **Bundled A/Bs:** v16+v17 vs v15 = +69 ±16 ³; v22–24 vs v21 = +11.75 ±6.8 ².
  v31's ≈+215 ¹ was odds-derived on the pre-`45dd989` harness and is
  **withdrawn**; the v30→v31 gate (29W/1D/0L over 30 games) stands on its own.

</details>

### The biggest jumps

| Jump | NPS | What |
|---|---|---|
| **v15→v17** | 28.7k → 52.7k | eval, then movegen ported to C (byte-identical) |
| **v25→v28** | 49.1k → 69.0k | node-identical speed batches |
| **v30→v31** | 69.0k → **2.34M (~34×)** | whole per-node loop moves to C |
| **v34→v36** | 2.13M → 3.19M | noisy-only qsearch gen + staged ordering |
| **v43→v44** | 3.23M → 3.67M | TT prefetch: +13.31 Elo, ~2.7 Elo per 1% NPS |
| **v53** | -- | Texel eval retune: +37.52 Elo, biggest single release |
| **v54→v55** | 3.69× → **4.19×** | pin-aware legality + eval accumulator: +9.66 Elo, ~1.16 Elo per 1% NPS |
| **v55→v56** | **4.19×** | ProbCut: **+11.44 timed** (+4.11 on `--nodes`) at -21.6% nodes; S-06 pool +9.83% at 4 threads |
| **v56→v57** | **4.19×** | host layer only, node-identical. **Last pure-HCE release** |
| **v57→v58** | **~2.9×** | NNUE armed: **+19.11 timed** while GIVING BACK ~30% NPS to the net. First hybrid; the gain is the training schedule, not the architecture |

*Not visible as NPS:* v39→v40 (ep-key merge) and the v41→v43 verified-null
removal are nodes-to-depth gains at flat speed.

---

## Features

- **Search:** PVS alpha-beta, iterative deepening with **aspiration windows**,
  partial-iteration salvage, **quiescence** with delta pruning, check evasions and an exact
  lazy stand-pat, Internal Iterative Reduction, and **ProbCut** confirmed by a
  reduced-depth search.
- **Transposition table** (24-byte lockless entries, `Hash` MB): **dead-entry
  replacement (FI-115, ours)** evicts first any entry whose piece count exceeds
  the root's, since material is irreversible. Kept warm across moves with a
  rule-50 guard; depth-preferred replacement, depth-independent mate scores, a
  child prefetch, and **cuckoo** upcoming-repetition detection (Stockfish's scheme).
- **Selectivity:** null move (no double null, eval-scaled R), reverse futility,
  futility, **LMR** including root moves, LMP, SEE capture gating, and a
  cannot-win clamp.
- **Extensions and ordering:** check (per-line budget 5), single-reply and
  passed-pawn extensions; TT move, MVV-LVA + capture history, killers,
  counter-moves, history with quiet malus.
- **NNUE (armed):** `(6144 → 256)×2 → 16 → 32 → 1`, incremental perspective
  accumulators, int8/int16 quantized (QA 127, QB 64, output cp/400), a **lazy**
  forward pass, NEON+dotprod / AVX2 kernels. Scalar builds stay on the HCE (`NNUE_REQUIRE_SIMD`),
  where the ~3× slower tail makes the engine *worse*. Trained on positions
  labelled by search, with a cosine LR schedule -- the schedule alone was worth
  +19 Elo.
- **Hand-crafted eval** (the qsearch stand-pat, and the whole eval on non-SIMD
  hosts): tapered material and PSTs, pawn structure, king safety, mobility, rook
  files, bishop pair, threats, pins, tempo, endgame mop-up and cannot-win
  handling. 44 scalars Texel-fitted; the C port is **bit-exact** against the
  Python reference over 3M positions.
- **Internals:** magic-bitboard movegen matching python-chess's move order
  **byte-for-byte** (perft-verified over 1.49 billion nodes); **Lazy SMP** over
  pthreads on a lock-free shared TT (`Threads`).
- **Play:** local **Syzygy** 3-4-5 (WDL + DTZ) with online Lichess probing for
  6-7 men; Polyglot book (`Perfect2023.bin`); pondering; **MultiPV**; certified
  instant premoves; `wdl` info lines; **Chess960** (`UCI_Chess960`, see
  [Chess960](#chess960)).
- **Built, measured, and deliberately OFF:** singular extensions, outpost and
  king-shelter terms, subtree-count root ordering, history-driven quiet
  pruning, several qsearch-TT variants, a growing TT. Each measured null or
  negative and stays in the tree at its default.

## Setup

```bash
git clone https://github.com/IchNukeDichWeg/Pygin.git
cd Pygin
./setup.sh
```

`setup.sh` installs anything missing (Homebrew on macOS, apt/dnf/pacman/zypper
on Linux), builds the C libraries and, best-effort, the `Old Engine/` snapshots,
then self-tests. Needs Python 3.10+, a C compiler (`clang`/`gcc`),
`python-chess`, and Stockfish for strength/odds testing.

```bash
python3 selftest.py        # health check; exit 0 = OK, chainable
```

> **Isolated install:** `python3 -m venv .venv && source .venv/bin/activate`, then `./setup.sh`.
> **Windows:** build a Unix `.so`, so use [WSL](https://learn.microsoft.com/windows/wsl/install) (`wsl --install`) or Git Bash / MSYS2.
> **Rebuild C by hand:** `python3 scripts/eval_build.py && python3 scripts/movegen_build.py` (for `csearch.so`, re-run `./setup.sh`).

---

## Running a headless match

`match.py` plays engine-vs-engine with a live scoreboard + Elo, per-game logs
and PGN.

```bash
# the live engine vs the previous release: 100 positions (×2 colours)
python3 match.py cengine.py "Old Engine/65/engine65.py" 100 0 --workers 0
```

- Positional args are `engine1 engine2 NUM_POSITIONS OFFSET`; each position is
  played with both colours, so games = `NUM_POSITIONS × 2`.
- Flags: `--workers 0` = cores-1, `--adj on|off` adjudication, `--sf-elo N`,
  `--smp N`, `--book1/--book2 PATH`, `--start-pos True`, `--chess960`.
- Openings default to the bundled `UHO_4060_v4.epd`; point `FEN_FILE` at a
  larger set from the [Stockfish books repo](https://github.com/official-stockfish/books).

**vs Stockfish** (binary on `PATH`):

```bash
python3 match.py engine.py stockfish_engine.py 100 0 --sf-elo 2000   # 0 = full strength
```

**Material / time odds** are set in `odds.py`'s `CONFIG` block (default pawn
odds, `h2`) against **full-strength SF-18**, true of the code only since
2026-09-05. Each worker runs two engines, so `--workers 0` here means cores/2,
not cores-1, to keep the clock unstarved:

```bash
python3 odds.py --positions 500 --workers 0
```

---

## UCI options

`cuci.py` is the UCI engine (`setoption name <Option> value <x>`):

| Option | Type | Default | Range | Purpose |
|:-------|:-----|--------:|:------|:--------|
| `Threads` | spin | 1 | 1–512 | Lazy-SMP search threads. Above your **physical** core count they cost strength |
| `Hash` | spin | 192 | 2–24576 | TT size (MB), rounded **down** to a power of two (6144 / 12288 / 24576 near the top); resizing wipes it. **Raise it for long games or analysis** -- at 50+0.20 the default is full by move 16 |
| `MultiPV` | spin | 1 | 1–20 | PV lines reported. >1 is an analysis mode that bypasses the book |
| `Skill Level` | spin | 40 | 0–40 | Weaker play for human opponents. **40 = full strength**. **Not an Elo scale** -- see below |
| `OwnBook` | check | false | -- | Play from the bundled Polyglot book |
| `BookFile` | string |  | -- | Path to Polyglot `.bin` book (empty ⇒ bundled `Perfect2023.bin`) |
| `UseTB` | check | false | -- | Probe Syzygy at the root: **local `SyzygyPath` first**, then the online Lichess probe (needs network) |
| `Move Overhead` | spin | 40 | 0–5000 | Clock margin (ms) for GUI/network lag |
| `Premove` | check | false | -- | Emit certified instant-reply premoves |
| `UCI_ShowWDL` | check | true | -- | Emit `wdl` on info lines |
| `Clear Hash` | button | -- | -- | Wipe the transposition table without `ucinewgame` |
| `UCI_Chess960` | check | false | -- | Chess960 / Fischer random. See [Chess960](#chess960) |
| `Contempt` | spin | 50 | -100–100 | Draw score bias (cp) when ahead/behind |
| `SyzygyPath` | string |  | -- | Folder of local Syzygy `.rtbw`/`.rtbz` tables; the man-count is **detected from what is on disk** |
| `Ponder` | check | false | -- | Think on the opponent's clock; a ponderhit honours the soft-stop |
| `SoftStop` | spin | 55 | 1–100 | Base soft-stop fraction (% of the move budget) |
| `SoftStopStable` | spin | 40 | 1–100 | Fraction once the best move is stable |
| `SoftStopUnstable` | spin | 90 | 1–100 | Fraction while the best move is changing |
| `SoftStopStableIters` | spin | 3 | 1–20 | Iterations the best move must hold to count as stable |

**On `Skill Level` and the missing `UCI_Elo`.** The scheme is Stockfish's
(bias each of at least four root candidates by how far it trails the best) on a
0-40 scale: weakness `120 - level`, depth cap `1 + level // 2`. `UCI_Elo` and
`UCI_LimitStrength` are **deliberately not implemented**: no Elo curve has been fitted for this engine, while `Skill Level`
promises only a relative ordering. `scripts/calibrate_skill.py` measures what
each level is currently worth.

⚠️ **Never measure engine progress against a skill-limited opponent.** A limiter
injects errors at a fixed *rate*, so real differences compress: a version pair
worth +119 Elo read as ~22 against an Elo-limited one.

### Chess960

`setoption name UCI_Chess960 value true` switches the whole engine to
Fischer-random rules. FENs take X-FEN or Shredder-FEN rights, the book is
skipped, and castles are **king takes own rook** (`e1h1`, `b1a1`).
**Standard chess is node-identical**: the bench signature and the pinned d14
ladder are unchanged with it built in.

Correctness is gated: `testing/perft.py` runs the 10-position chessprogramming
Chess960 perft table to depth 5 on both C generators, `selftest.py` walks every
castle shape, NNUE verify mode checks each incremental push against a full
refresh, and development fuzzing compared 20,000 random back-rank positions
with 11,554 legal castles against python-chess, 0 mismatches.
`match.py --chess960` needs both engines to declare `SUPPORTS_CHESS960`.

**Does the net still pay in 960?** The net was trained on standard chess only,
so the shipped engine played itself with the net off, 5+0.05, 200 games per arm
on 2026-11-04:

| Openings | Score | Net over HCE |
|---|---|---|
| Chess960 start positions | 77.50% (132W/46D/22L) | **+214.85 ±47.1** |
| Standard (UHO) | 73.50% (125W/44D/31L) | **+177.22 ±39.8** |

The gap is well inside the error bars. This is relative strength only; no Chess960 strength has been
measured against an outside engine.

---

## Tooling

| Script | Purpose |
|---|---|
| `testing/perft.py` | Move-generator correctness gate, standard and Chess960 (`--deep` for the full 1.8 B-node suite). |
| `bench/profile_bench.py` | Real NPS + a per-function bottleneck breakdown (`--graph` for an HTML report). |
| `bench/nps_history_bench.py` | NPS / depth benchmark across the `Old Engine/` snapshots. |
| `bench/benchmark.py` | NPS / depth / nodes for the C core (`--type`, `--threads`, `--hash`, `--runs`). |
| `tuning/fit_wdl_model.py` | Fit the WDL models (`data/wdl_model.json` + `data/wdl_model_nnue.json`) from match logs; `--sync-only` copies them into `cuci.py`. |

---

## Project layout

```
engine.py              reference Python engine; source of every HCE scalar
cengine.py             root driver for the C search core -- THE SHIPPED ENGINE;
                       its class attributes are the live toggle set
csearch.c              the per-node search loop in C
eval_c.c / movegen.c   C evaluation and move generation
Constants.c/.h         magic-bitboard + attack tables
cuci.py                UCI host for the C search core (19 options)
match.py               headless match runner (SPRT, pentanomial)
battle_worker.py       per-game worker process used by match.py
stockfish_engine.py    UCI adapter for Stockfish
odds.py                material / time-odds match runner
selftest.py            the pre-commit gate (behavioural contracts)
setup.sh               builds the three .so files, then runs the selftest
Old Engine/<N>/        frozen version snapshots (engineN.py + its C sources)

NNUE/                  the net lane: config.py, gen_data.py (--syzygy,
                       --label-nnue), train.py, model.py, data_format.py,
                       verify_labels.py, label_depth_probe.py,
                       label_teacher_probe.py, nnue.c, nets/, datasets/,
                       shims/ (one file per A/B arm), campaigns/ (SPRT state)
uci/                   front ends for FROZEN snapshots (cuci_old.py, uci_old,
                       uci_legacy)
lib/                   time_manager, interruptible (Ctrl-C/SIGTERM salvage),
                       smp + shared_tt (the Python engine's Lazy SMP)
data/                  Perfect2023.bin book, UHO opening EPDs, fen.txt, and the
                       two fitted WDL models (hce + nnue, never pooled)
syzygy/                local Syzygy 3-4-5 tables (gitignored, ~939 MB;
                       scripts/fetch_syzygy.sh pulls and checksums them)
docs/                  design notes, OpenBench guide, progression SVGs, licences
tuning/                texel.py, fit_wdl_model.py and the eval-fitting tools
bench/                 NPS, depth and profiling harnesses
testing/               perft, SPRT, and the correctness gates selftest runs
scripts/               build, release, A/B campaign, export and fetch scripts
```

`Old Engine/<N>/` holds every historical version, each self-contained. See
its [README](Old%20Engine/README.md).

---

## Notes

- C `.so` files are platform-specific and not committed; `setup.sh` builds them.
- If a `.so` won't load, the engine falls back to pure Python (correct, slower);
  the self-test reports which path is active.

## License

- **Source:** MIT -- see [`LICENSE`](LICENSE).
- **Released binaries** bundle [`python-chess`](https://github.com/niklasf/python-chess)
  (GPL-3.0+), so the binary distribution is GPL-3.0 as a whole. Full text,
  source pointers and credits (Perfect2023 book -- Sedat Canbaz; UHO suites --
  Stefan Pohl) in [`THIRD_PARTY_LICENSES.md`](docs/THIRD_PARTY_LICENSES.md).
