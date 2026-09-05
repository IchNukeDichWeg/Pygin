<div align="center">

# Pygin

**A from-scratch chess engine in Python + C.** The search is hand-written.
Since v58 the evaluation is an HCE/NNUE hybrid: the neural net scores positions
inside the main search, and the hand-crafted eval keeps quiescence. There is no
external engine, and the net learned from Pygin's own self-play games only, with
no borrowed data and no borrowed weights.<br/>
[`python-chess`](https://pypi.org/project/chess/) is used *only* for board
representation, move generation and legality.

![Strength](https://img.shields.io/badge/strength-retracted-8b949e)
![Speed](https://img.shields.io/badge/speed-5.6M_nps-58a6ff)
![Versions](https://img.shields.io/badge/versions-62-8b949e)
![C--era_gains](https://img.shields.io/badge/C--era_gains-%2B354_Elo-f0883e)
![Source](https://img.shields.io/badge/source-MIT-green)
&nbsp;·&nbsp; Built with **[Claude Code](https://claude.com/claude-code)**

</div>

### At a glance

<table>
<tr><td><b>retracted</b></td><td>strength: see Measured strength</td><td><b>5.6M nps</b></td><td>the net costs ~30% of it</td></tr>
<tr><td><b>~+354 Elo</b></td><td>A/B-confirmed, v31&rarr;v62</td><td><b>~18 ply</b></td><td>from startpos in 5 s</td></tr>
<tr><td><b>+48.84 Elo</b></td><td>the NNUE era, v58&rarr;v62</td><td><b>1.11&times;</b></td><td>single-thread vs v31</td></tr>
<tr><td><b>v53+v54</b> eval lane</td><td>+37.52 &amp; +31.20, the two biggest</td><td><b>1 dependency</b></td><td><code>python-chess</code> only</td></tr>
</table>

<table>
<tr>
<td><img src="docs/elo_progression.svg?v=62" width="100%" alt="Cumulative A/B Elo across the C era, v31=0 climbing to +354 at v61"/></td>
<td><img src="docs/speed_progression.svg?v=62" width="100%" alt="Single-thread speed as a multiple of v31, peaking at 1.60x and ending at 1.11x once the net is armed"/></td>
</tr>
</table>

<table>
<tr>
<td><img src="docs/mate_progression.svg?v=62" width="100%" alt="Mate-finding on mates2000.epd across the C era, rising from 23.4% at v31 to a 51.6% plateau, then falling back to 40.4% at v62 as the net arms"/></td>
</tr>
</table>

All three charts are self-play. Every C-era version (v31 and up) is A/B-tested
against the one before it, and the gains stack to about **+354 Elo**.
Single-thread speed peaked at 1.60× and sits at 1.11× today: v58 hands about
30% of it to the net and still comes out +19.11 ahead. The v30→v31 C rewrite
(~34× faster) is off the left edge, so v31 is the honest zero.

Mate-finding is the one curve that does **not** track Elo, and it is left in
because of that. It climbs to a ~51% plateau by v39, then falls away as the
search gets more selective and the net arms — v62 finds 40.4% of
`mates2000.epd` at 0.25s where v49 found 51.6%. Forward pruning and a net that
values position over forced sequences both cost mate speed, and every one of
those releases still measured POSITIVE in games. A tactical-suite score is a
different instrument from an A/B, and where they disagree the A/B is the one
that decides.

The odds ladder against **full-strength** Stockfish is **unmeasured**, and
that is now true for two separate reasons. The pre-2026-08-13 rungs were
measured on a harness that erased Stockfish's won endgames, so those figures
are withdrawn rather than footnoted; see
[Measured strength](#measured-strength).

### Two engines, one eval

`cengine.py` and `csearch.c` are the C search core, and the strongest engine
here. The whole per-node loop runs in C: board, ordering, TT, pruning,
quiescence, and since v58 the NNUE forward pass. Python keeps only the root,
which means iterative deepening, time management and the book. That is about
50× the Python core.

The eval is a hybrid. The net scores positions inside negamax, while qsearch
stand-pat stays on the hand-crafted eval. The split is deliberate: an earlier
all-NN attempt measured -203 to -273 Elo. `NNUE_REQUIRE_SIMD` keeps the net off
CPUs with neither NEON nor AVX2, where the scalar tail costs more than the eval
is worth.

`engine.py` is the reference Python engine, and the single source of
*hand-crafted* eval truth: the C core reads every HCE parameter from it at
startup. The net's weights live in `NNUE/nets/` instead.

### Measured strength

**RETRACTED 2026-09-05. There is currently no strength figure for Pygin.**

The ~2868 Elo published here was derived from a run launched against an
explicit `--sf-elo 2900` cap. T-14 shows that override never survived the spawn
boundary: `EngineProcess._spawn` read the module default, so every game was
actually played at **UCI_Elo 3000**. The score below is real; the opponent it
is attributed to is not.

```
Score | 45.45% (454.5/1000)   <- real games
Games | N: 1000  W: 235  L: 326  D: 439
Penta | [30, 153, 212, 88, 17]   500 pairs
Conf  | 50+0.50, Threads=1, 4 workers, Stockfish 18 at UCI_Elo 3000
        (the run ASKED for 2900 and did not get it)
```

It is retracted rather than recomputed: a result against a 3000 cap does not
convert to a 2900 one by arithmetic, and the original was already an
extrapolation from a single cap rather than a two-cap bracket. A replacement
needs 2 x 1,000 games through the now-fixed harness. UCI_Elo is in any case
Stockfish's own limiter and not an external rating.

**This is the only strength figure on this page.** Everything previously
published here -- ~3010 at v58, ~2885 at v51, the whole odds ladder -- was
measured on a harness that accepted a threefold repetition that was merely
*available* rather than one that had occurred, and claimed the draw for
whichever side was about to convert. Against Stockfish that bias runs one way,
because Stockfish is the side with won endgames to grind. Fixed in `fc82cb7`.
Re-adjudicating the old games by evaluation predicted 46.0%; the re-run
measured 45.45%.

Those numbers have been removed rather than annotated. A retracted measurement
kept on the page with a footnote still gets quoted.

**Pawn odds (f2)** -- the published rung, and the one handicap Stockfish still
scores against: **81.00% over 1,000 games** (704W / 212D / 84L, +251.89
+/-35.2) at v59, 50+0.50, corrected harness, 2026-08-13, against Stockfish
**capped at UCI_Elo 3000** -- see the caveat below. The withdrawn pre-fix
figure was 90.30%; about nine points of it were the buggy termination erasing
Stockfish's conversions, not engine strength.

**Pawn odds (h2)** is the ACTIVE rung as of 2026-09-02, at **56.75% over 200
games** (75W / 77D / 48L, +47.19 +/-38.0) at v62. All eight white pawns were
measured that night: six of them cluster at 73.5-77.5% and are not separable
from each other, f2 leads at 80.50%, and h2 alone sits near 50%. The switch is
for resolution, not saturation -- an era gain of +35 Elo moves f2 by 3.0 score
points and h2 by 4.9, so h2 answers the same question with 41% fewer games.
Why h2 costs Stockfish so much less than the other seven is **not explained**;
the obvious candidate, that it hands SF an open file, was tested and rejected
(SF advances a rook up the vacated file in 76% of a2 games and 72% of h2 ones).
f2 and h2 numbers are different instruments and are never pooled.

> **The whole ladder was played against a CAPPED Stockfish.** `odds.py`
> resolved `STOCKFISH_ELO` into its own module globals and never applied it, so
> `stockfish_engine.py`'s own default sent `UCI_LimitStrength` on every run made
> after 2026-07-22: **UCI_Elo 2900** until 2026-08-07 and **3000** after. Fixed
> 2026-09-05 (B-15), verified by logging Stockfish's stdin. Consequences: no
> rung above is a full-strength figure; the f2 series spans two different
> opponents and does not pool with itself; a single night at one cap is still
> internally consistent, so the eight-pawn ranking and h2's separation stand.
> `UCI_LimitStrength` injects errors at a fixed rate, which compresses real
> gaps, so the ladder's sensitivity to an era gain is not what a full-strength
> reading would give. Every margin here is also the corrected trinomial one
> (T-22); the previously published ones were 1.3-1.7x too wide.

Against its own Python engine (v30, ~2440-2450), v59 scored **195W / 5D / 0L
over 200 games** (98.75%) at 50+0.50 on the corrected harness, 2026-08-12. No
rating is quoted; the gap is past what Elo can express. This replaces the
withdrawn 1,815-0-40, which was measured through the buggy termination.

**Standing, with their vintage:** knight, rook and queen odds are all
saturated at 100%, measured at v53/v54 under 45+0.15 (and, like every rung
above, against a capped Stockfish -- saturation is the one claim a cap cannot
weaken, since a stronger opponent could only score lower). Those runs
contained **zero draws**, and the bug could only act by producing a draw, so
the broken code path never executed in them -- the measurements are untouched
by the fix. They have not been re-measured at the current era; the engine has
only gotten stronger since, so saturation is asserted a fortiori, not
re-measured.

The internal A/B ledger is not affected in the same way. Between two Pygins the
bug is near-symmetric, so it inflated draws and compressed effect sizes toward
zero: those numbers read low, not high.

---

## Version progression

61 versions, each A/B-tested against the one before it. Speed is nodes/s,
depth is from startpos in 5 s (book off, best-of-N), and `Elo Δ` is the A/B
result against the previous version. Cumulatively that is ≈ +354 over v31.

The list below has the full per-version speed, depth and Elo, and the charts
above summarise it. Regenerate both with `bench/bench_progress.py` and
`scripts/make_readme_charts.py`.

<details>
<summary><b>Every version in full</b> -- complete milestone + Elo list</summary>

- **v62** -- **the aspiration window stopped fighting itself.** The root window opened flat at 30cp whatever the position and widened 2&times; per failure, and a fail-low left `beta` untouched -- so a position could oscillate low/high and pay the whole ladder out to the 1920 fallback. Three riders replace that as one policy: the opening delta scales with the previous score, a fail-low pulls `beta` to the midpoint before `alpha` drops, and growth is 1.5&times;. Driver-only, but the C core is **not** byte-identical to v61 as released: v62 also carried `b4339ae`+`a1a31cd`, the b05 FI-115 completion, which had already measured ACCEPT H0 (LLR -5.98) and was reverted after the release. Both arms of the A/B below shared that core, so the +18.40 measures the window honestly; v62-as-released vs v61-as-released was never measured. *(**+18.40 ±7.6** at 50+0.5 over 3,439 games, 52.65%, ptnml 62/361/735/450/109, nElo +28.25, SPRT[0,4] LLR +2.959 **ACCEPT H1** -- and **+2.969 ACCEPT** on the 10+0.1 screen before it. Both **stopped early, so the magnitude is bound-biased and the ledger is not advanced on it**; a fixed-budget 50+0.5 run is owed. Bench 1,140,099 -> 1,203,792 -- unlike v61 this one does move the signature, because the window shapes the root search from the first iteration.)*
- **v61** -- **dead entries in the transposition table are now evicted first.** Material is irreversible, so an entry whose stored piece count *exceeds* the current root's can never occur again -- garbage with certainty, not a guess. v61 stamps that count into 6 spare bits and takes those first, depth-protecting a still-reachable old entry instead of clobbering it on age. Nothing is swept; it is a better choice of victim, made at store time. Also carried: FI-113's two-cache-line TT prefetch, node-identical, +1.09% NPS. *(**+15.89 ±4.2** over a **fixed** 10,000-game budget at 50+0.5, 192 MiB both sides, 52.29%, ptnml 146/1044/2226/1375/209, ratio 1.33, nElo +25.77. No early stop, so the ledger advances: +338 -> **+354**. Measured at the shipped table size deliberately -- the 10+0.1 screen ran 24 MiB to force replacement pressure -- and a cold-TT bench cannot see this rule at all, so the signature stays 1,140,099.)*
- **v60** -- **the first net trained on real games, not generated ones.** The search is byte-identical to v59; only `NNUE_FILE` moves, to `nnue_v12_bf86c4ced057.nnue`. Every net before this learned from self-play positions manufactured for the purpose. v12 learned from **24,825,823 positions harvested out of Pygin's own A/B match logs**, labelled at depth 12-16 by the search those games actually ran. It is also the first trained with the game-result term off (`LAMBDA 1.0`) -- a correctness requirement, not a tuning choice: those logs predate the phantom-repetition fix and replay showed **1,479/1,479** of their repetition draws were phantom. *(GSPRT[0,4] LLR **+2.957 ACCEPT** at 793 pairs vs `Old Engine/59`, TIMED 50+0.5, 56.09% over 1,594 games -> +42.50 ±17.3 -- **stopped early, so the magnitude is bound-biased and the ledger is not advanced on it**. Against the v10 net it drew over a full 10,000 games.)*
- **v59** -- **lazy NNUE evaluation, and the first release measured as exactly the config that ships.** `LAZY_NNUE` is armed: the engine skips the net's forward pass wherever a cheap bound already decides the node, spending the saved time on more nodes (bench 1,074,820 -> **1,214,534**). The toggle was isolated for the first time -- same v4 net on both sides, nothing else different -- against `Old Engine/58` on the corrected harness. *(GSPRT[0,4] LLR **+2.950 ACCEPT** at 2,264 pooled pairs, TIMED 50+0.5 on x86, ptnml 69/509/975/595/116, pooled 51.99% -> **+13.84 ±6.4**, stopped early so the magnitude is bound-biased -- the verdict is the result. Historical note: v58's own +19.11 turned out to have been measured with this toggle ON while the release ran it OFF; v59 closes that gap by shipping what was measured.)*
- **v58** -- **the first HCE/NNUE hybrid, and the first net that pays.** `USE_NNUE` is armed on `nnue_v4_6f910e35bb1e.nnue`: the net replaces the hand-crafted eval inside negamax, while qsearch stand-pat stays HCE. Bench signature 1,145,629 -> **1,074,820**, and single-thread NPS drops roughly 30% to the SIMD tail, so the Elo below is measured *net* of that cost. The interesting part is what changed from v3, which read **+0.52 ±6.8** on this same instrument, i.e. nothing at all: not the dataset, not one dimension of the architecture, only the **learning-rate schedule**. Cosine in place of flat took held-out val 0.074417 -> 0.066663, and that is the whole gain; identical dimensions mean identical speed, so none of it is bought with nodes. `NNUE_REQUIRE_SIMD` keeps the net off scalar builds, where the ~3x slower tail would make the engine *worse*. *(**+19.11 ±7.8** over 3,404 games TIMED 50+0.20 on x86, GSPRT[0,4] LLR **+2.950 ACCEPT** at 1,702 pairs, ptnml 71/358/691/477/105 -- sixth SPRT accept, and the second-largest release after the v53 Texel retune. An arm64 confirmation is owed: v3 read +5.70 ±4.6 there against +0.52 here, so the architecture spread is real and only x86 has been measured for v4)*
- **v57** -- **the last pure-HCE release**; from here Pygin is an HCE/NNUE hybrid. Host layer only and **node-identical** to v56 (bench signature 1,145,629 unchanged, ladder node-exact), so no A/B slot was spent and the ledger is untouched. **Ponderhit now honours the soft-stop** -- a prediction hit used to spend the full fresh budget re-confirming an already-settled move; it now applies the same P-35/U-06 fractions the main search uses (1.666 s → 0.686 s, a ratio of 0.412 against the designed 0.40). The soft-stop neighbourhood is exposed over UCI (`SoftStop`, `SoftStopStable`, `SoftStopUnstable`, `SoftStopStableIters`) so it can be swept without a rebuild. And a latent bug is fixed: `cuci.py` restored a *hardcoded* 0.55 soft-stop fraction over whatever the engine set, which meant any future tuning would have worked in testing and been silently discarded in every real game.
- **v56** -- **ProbCut**: the fail-high half of forward pruning, which Pygin had no equivalent of. At a shallow non-PV node a qsearch filters each capture at `beta + 200` and a real reduced-depth search confirms before anything is cut, so nothing is ever pruned on a static score. Cuts **21.6% of nodes** at fixed depth (bench 1,461,732 → 1,145,629). *(**+11.44 ±6.9** over 5,924 games TIMED 50+0.20, GSPRT[0,4] LLR **+2.953 ACCEPT** -- the ledger's own instrument; the `--nodes` campaign that shipped it read **+4.11 ±4.2** over 21,806 games (LLR +2.971), so the fixed-node instrument reads CONSERVATIVE. Fifth SPRT accept, and the first pruning MECHANISM to pay since the search lane was declared exhausted: what was exhausted was the parameter space, not the mechanism space)*
- **v55** -- **node-identical speed pair**: FI-11 pin-aware legality + FI-42 the (mg,eg,phase) accumulator on Board. Bench signature UNCHANGED at 1,461,732, perft --deep clean, ladder node-exact -- the search plays the SAME moves, it just gets there faster: **+8.3% NPS on x86, +13.5% on arm64**. *(**+9.66 ±8.2** over 6,874 games, TIMED 50+0.20, GSPRT[0,4] LLR +2.946 ACCEPT -- measured on the clock because a fixed-node instrument reads zero for a node-identical change; **~1.16 Elo per 1% NPS**)*
- **v54** -- **PST retune** (736 piece-square entries fitted for the first time, texel.py --pst, 735 values moved; GSPRT[0,2] LLR +7.806, 11.7k games -- second-largest release) *(**+31.20 ±5.6**)*
- **v53** -- **Texel eval retune** (44 scalars refitted on 4M own-self-play positions, game-result labels; fourth SPRT accept, LLR +9.918, 12k pooled games -- largest single release) *(**+37.52 ±6.3**)*
- **v52** -- null-move refinements (no double null + eval-scaled R; third SPRT accept, 12k pooled games) *(+6.63 ±4.5)*
- **v51** -- root-move LMR (late quiet root scouts reduced; second SPRT accept, 9.3k pooled games) *(+11.12 ±5.3)*
- **v50** -- rule50 TT staleness guard + depth-independent TT mate handling (permanent terminal entries; null kept as correctness) *(+1.60 ±6.8)*
- **v49** -- cuckoo upcoming-repetition (forcible draw scored one ply early; null kept as correctness) *(+0.97 ±6.8)*
- **v48** -- qsearch TT-quality batch (TT value sharpens stand-pat; first SPRT accept, 21.6k games) *(+4.73 ±3.2)*
- **v47** -- TT to 192 MB (diminishing) + MultiPV (node-exact off) *(+3.16 ±6.8)*
- **v46** -- transposition table doubled to 96 MB (borderline; less TT thrash per game) *(+5.94 ±6.8)*
- **v45** -- TT search value sharpens the pruning eval (same NPS, smarter cuts) *(+13.52 ±6.8)*
- **v44** -- TT prefetch (node-identical, +5–6 % NPS) *(+13.31 ±6.8)*
- **v43** -- verified-null REMOVED (the insurance cost ~1 ply; isolation A/B) *(+5.18 ±6.8)*
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
  not summable across the whole column, because the TCs differ (Python era
  assorted ⁴, v32–36 at 45+0.10, v37–47 at 50+0.20, v48+ on `--nodes`).
- **`est` ⁵** is a feature-based estimate, not an A/B. The real anchor is ≈2442
  by v25.
- **Bundled A/Bs:** v16+v17 vs v15 = +69 ±16 ³; v22–24 vs v21 = +11.75 ±6.8 ².
  v31's ≈+215 ¹ was odds-derived and is **withdrawn** -- the odds ladder it came
  from was measured on the pre-`fc82cb7` harness. The v30→v31 gate (29W/1D/0L
  over 30 games) stands on its own: the jump was past what Elo could express.
- **NPS 4T** is "--" for v1–24 (no reliable SMP). v25–30 were multi-process,
  v31+ pthread Lazy-SMP, so the v30→v31 jump is partly methodology.

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
| **v56→v57** | **4.19×** | host layer only, node-identical: ponderhit soft-stop + the time-policy knobs over UCI. **Last pure-HCE release** |
| **v57→v58** | **~2.9×** | NNUE armed: **+19.11 timed** while GIVING BACK ~30% NPS to the net. First hybrid; the gain is the training schedule, not the architecture |

*Not visible as NPS:* v39→v40 (ep-key merge) and the v41→v43 verified-null
removal are nodes-to-depth gains at flat speed.

---

## Features

**Search**
- Negamax / alpha-beta with **PVS**; iterative deepening reusing the previous
  iteration's PV move, killers, history and TT; **aspiration windows**.
- Partial-iteration salvage: if time runs out mid-depth, the best root move
  evaluated so far is used rather than falling back to the last full depth.
- **Quiescence** with stand-pat, delta pruning, check evasions, and a lazy
  stand-pat that skips the expensive eval terms when the cheap base already
  proves a cutoff (exact -- the tree is unchanged).
- **Internal Iterative Reduction** at TT-less nodes; **ProbCut** on the
  fail-high side, where a qsearch filter is *confirmed* by a real reduced-depth
  search so nothing is ever cut on a static score.

**Transposition table** (24-byte entries, lockless XOR-folded, `Hash` MB)
- **Dead-entry replacement (FI-115, ours):** material is irreversible, so an
  entry whose piece count exceeds the root's can never recur -- evicted first,
  while a still-reachable old entry is depth-protected instead of clobbered on
  age. The count rides in 6 spare bits.
- **Kept warm across irreversible moves** rather than wiped, plus a rule-50
  guard so a stored score cannot outlive the draw counter that justified it.
- Depth-preferred replacement with an exact-bound bonus, terminal-node storing,
  depth-independent mate handling, and a two-cache-line prefetch of the child
  entry issued right after `apply_move`.
- **Cuckoo upcoming-repetition detection** (Stockfish's scheme): a cycle
  reachable in one move scores as a draw before the repetition physically happens.

**Selectivity**
- Null-move pruning with no-double-null and eval-scaled reduction; reverse
  futility (static null); futility pruning; **LMR** including *root* moves; LMP.
- SEE gating of captures at frontier nodes; a cannot-win clamp so a side without
  mating material is never scored as winning.

**Extensions** -- checks (per-line budget 5), single reply, passed-pawn pushes.

**Move ordering** -- TT move, MVV-LVA plus capture history, killers,
counter-moves, the history heuristic with quiet malus, and SEE for capture
sorting.

**Evaluation -- NNUE (armed)**
- `(6144 → 256)×2 → 16 → 32 → 1`, perspective accumulators updated
  **incrementally** through the game, int8/int16 quantized (QA 127, QB 64,
  output cp/400).
- **Lazy NNUE:** the forward pass is skipped wherever a cheap bound already
  decides the node -- the gain is in the nodes that buys.
- SIMD kernels (NEON+dotprod / AVX2) with a hard guard that refuses to arm the
  net on scalar builds, where the ~3× slower tail makes the engine *worse*.
- Trained on positions labelled by real search, blended cp + game result
  (`LAMBDA 0.75`), cosine LR schedule -- the schedule alone was worth +19 Elo.

**Evaluation -- hand-crafted (the qsearch stand-pat, and the whole eval on
non-SIMD hosts)**
- Tapered mg/eg by game phase: material, piece-square tables.
- Pawns: doubled, isolated, backward, passed.
- King safety: pawn shield, king-ring attacks, open/semi-open file penalties.
- Mobility, rook open and semi-open files, bishop pair, threats, pin penalty, tempo.
- Endgame: mop-up (centre-manhattan drive), simplification bias, contempt, and
  insufficient-material / cannot-win handling.
- 44 scalars Texel-fitted; the C port is verified **bit-exact** against the
  Python reference over 3M positions.

**Engine internals**
- Magic-bitboard movegen reproducing python-chess's move order **byte-for-byte**,
  perft-verified over 1.49 billion nodes.
- The entire per-node loop in C; Python keeps only clock, host and orchestration.
- **Lazy SMP** over pthreads on a lock-free shared TT (`Threads`); the Python
  engine has a separate multi-process variant.

**Endgame and play**
- Local **Syzygy** 3-4-5 probing (WDL + DTZ, so it converts rather than
  shuffling), with online Lichess probing for 6-7 men.
- Bundled Polyglot book (`Perfect2023.bin`); real UCI pondering with a
  soft-stop-aware ponderhit; **MultiPV** by root exclusion; certified instant
  premoves; `wdl` info lines from a WDL model fitted per eval family.

**Built, measured, and deliberately OFF** -- kept because the mechanism is
sound and the verdict is recorded: singular extensions, outpost and king-shelter
eval terms, SEE pruning of losing captures, root-move ordering by subtree count,
history-driven quiet pruning, several qsearch-TT variants, and a growing
transposition table. Each was A/B'd, measured null or negative, and left in the
tree at its default rather than deleted.

## Setup

```bash
git clone https://github.com/IchNukeDichWeg/Pygin.git
cd Pygin
./setup.sh
```

`setup.sh` installs anything missing (Homebrew on macOS, apt/dnf/pacman/zypper
on Linux), builds the C libraries, best-effort builds the `Old Engine/`
snapshots, and self-tests.

Needs Python 3.10+, a C compiler (`clang`/`gcc`), `python-chess` (the only
dependency), and Stockfish if you want strength/odds testing.

```bash
python3 selftest.py        # health check; exit 0 = OK, chainable
```

> **Isolated install:** `python3 -m venv .venv && source .venv/bin/activate`, then `./setup.sh`.
> **Windows:** build a Unix `.so`, so use [WSL](https://learn.microsoft.com/windows/wsl/install) (`wsl --install`) or Git Bash / MSYS2.
> **Rebuild C by hand:** `python3 scripts/eval_build.py && python3 scripts/movegen_build.py` (for `csearch.so`, re-run `./setup.sh`).

---

## Running a headless match

`match.py` plays engine-vs-engine, prints a live scoreboard + Elo, and writes a
per-game log and PGN.

```bash
# the live engine (a v63 candidate: v62's settings on v61's core) vs the
# previous release: 100 positions (×2 colours)
python3 match.py cengine.py "Old Engine/60/engine60.py" 100 0 --workers 0
```

- Positional args are `engine1 engine2 NUM_POSITIONS OFFSET`. Each position is
  played both colours, so games = `NUM_POSITIONS × 2`.
- Flags: `--workers 0` = cores-1, `--adj on|off` adjudication, `--sf-elo N`,
  `--smp N`, `--book1/--book2 PATH`, `--start-pos True`.
- Openings default to the bundled `UHO_4060_v4.epd`. Larger sets are in the
  [Stockfish books repo](https://github.com/official-stockfish/books); point
  `FEN_FILE` at one.

**vs Stockfish** (binary on `PATH`):

```bash
python3 match.py engine.py stockfish_engine.py 100 0 --sf-elo 2000   # 0 = full strength
```

**Material / time odds** are configured in `odds.py`'s `CONFIG` block (the
default is pawn odds, `h2`). The opponent is **full-strength SF-18** -- that is
what the ladder means, and a capped opponent measures something else that no
recorded rung can be compared to. This is true of the code only since
2026-09-05: before B-15 the setting never reached Stockfish and every recorded
rung was played capped. Each worker runs two engines, so `--workers 0`
here means cores/2, not cores-1 -- a real clock TC gets starved by
oversubscription, and a Stockfish opponent squeezed to 10k nps stops being the
yardstick the run is quoting:

```bash
python3 odds.py --positions 500 --workers 0
```

---

## UCI options

`cuci.py` is the UCI engine (`setoption name <Option> value <x>`). Standard
GUI options:

| Option | Type | Default | Range | Purpose |
|:-------|:-----|--------:|:------|:--------|
| `Threads` | spin | 1 | 1–512 | Lazy-SMP search threads. Above your **physical** core count they timeshare and cost strength |
| `Hash` | spin | 192 | 2–24576 | Transposition-table size (MB); resizing wipes it. Sizes are powers of two, so anything between rounds **down** (6144 / 12288 / 24576 near the top). **Raise it for long games or analysis** -- at 50+0.20 the default is full by move 16 |
| `MultiPV` | spin | 1 | 1–20 | PV lines reported. >1 is an analysis mode: it bypasses the book and is never active in match play |
| `Skill Level` | spin | 40 | 0–40 | Deliberately imperfect play for human opponents. **40 = full strength** and is byte-identical to the option being absent; lower levels cap the search depth and add a random bias that grows with how far a candidate trails the best move, so weak levels routinely pick a worse one. **Not an Elo scale** -- see below |
| `OwnBook` | check | false | -- | Play from the bundled Polyglot book. Off by default: a book is an opening preference, not strength |
| `BookFile` | string |  | -- | Path to Polyglot `.bin` book (empty ⇒ bundled `Perfect2023.bin`) |
| `UseTB` | check | false | -- | Probe Syzygy at the root. **Local `SyzygyPath` is always tried first**; the online Lichess probe is the opt-in fallback for sizes you do not have (needs network) |
| `Move Overhead` | spin | 40 | 0–5000 | Clock margin (ms) for GUI/network lag |
| `Premove` | check | false | -- | Emit certified instant-reply premoves (opt-in) |
| `UCI_ShowWDL` | check | true | -- | Emit `wdl` on info lines (opt-out for strict arenas) |
| `Clear Hash` | button | -- | -- | Wipe the transposition table without `ucinewgame` |
| `Contempt` | spin | 50 | -100–100 | Draw score bias (cp) when ahead/behind |
| `SyzygyPath` | string |  | -- | Folder of local Syzygy `.rtbw`/`.rtbz` tables. The man-count is **detected from what is on disk**, so 6- and 7-man sets are used locally if you have them; the online probe is only a fallback for what you do not |
| `Ponder` | check | false | -- | Think on the opponent's clock. A ponderhit honours the soft-stop rather than spending the full fresh budget |
| `SoftStop` | spin | 55 | 1–100 | Base soft-stop fraction (% of the move budget) before the search may stop between iterations |
| `SoftStopStable` | spin | 40 | 1–100 | Soft-stop fraction once the best move has held for `SoftStopStableIters` iterations |
| `SoftStopUnstable` | spin | 90 | 1–100 | Soft-stop fraction while the best move is still changing |
| `SoftStopStableIters` | spin | 3 | 1–20 | Iterations the best move must hold before "stable" applies |

**On `Skill Level` and the missing `UCI_Elo`.** The weakening scheme is
Stockfish's -- search at least four root candidates, then add a randomised
bias to each score that grows with how far it trails the best -- on a 0–40
scale instead of 0–20, which halves the step size without moving the
endpoints (weakness `120 - level`, depth cap `1 + level // 2`).

`UCI_Elo` and `UCI_LimitStrength` are **deliberately not implemented**, and
setting them returns an `info string` saying so rather than silently doing
nothing. `UCI_Elo` is a calibrated claim in Elo units, and no campaign has
fitted that curve for this engine; Stockfish's is fitted to Stockfish and
tops out around 700 Elo above this engine's own measured class, so adopting
it would assert a number nobody here has measured. `Skill Level` promises
only a relative ordering, which is a promise the code can keep.

That relativity is also why the scale needs no upkeep: a level is defined
against the engine it sits in, so a stronger release lifts every level with
it and 40 remains exactly full strength. An `UCI_Elo` mapping would instead
need re-fitting on every release. `scripts/calibrate_skill.py` measures what
each level is currently worth if you want the table.

⚠️ **Do not measure engine progress against a skill-limited engine.** A
limiter injects errors at a fixed *rate*, and exploiting them is nearly
fixed-yield, so real differences compress: a version pair measured at +119
Elo directly read as ~22 against an Elo-limited opponent.

---

## Tooling

| Script | Purpose |
|---|---|
| `testing/perft.py` | Move-generator correctness gate vs the published Perft results (`--deep` for the full 1.5 B-node suite). |
| `bench/profile_bench.py` | Real NPS + a per-function bottleneck breakdown in one pass (`--graph` for an HTML report). |
| `bench/nps_history_bench.py` | NPS / depth benchmark across the `Old Engine/` snapshots. |
| `bench/benchmark.py` | NPS / depth / nodes benchmark for the C search core (`--type`, `--threads`, `--hash`, averaged over `--runs`). |
| `cuci.py` | UCI host for the C search core (`Threads` / `OwnBook` / `UseTB` options). |
| `tuning/fit_wdl_model.py` | Fit the win/draw/loss model from match logs (`data/wdl_model.json` + `data/wdl_model_nnue.json`; `match.py` and `tuning/eval_bench.py` read them, `--sync-only` copies the coefficients into `cuci.py`). |

---

## Project layout

```
engine.py              the reference Python engine: the readable statement of
                       the search, and the source of every eval scalar the C
                       port is verified bit-exact against
cengine.py             root driver for the C search core -- THE SHIPPED ENGINE.
                       Its class attributes are the live toggle set
csearch.c              the whole per-node search loop in C (built to .so)
eval_c.c / movegen.c   C evaluation and move generation (built to .so)
Constants.c/.h         magic-bitboard + attack tables (linked into the .so files)
cuci.py                UCI host for the C search core (17 options)
match.py               headless engine-vs-engine match runner (SPRT, pentanomial)
battle_worker.py       per-game worker process used by match.py
stockfish_engine.py    UCI adapter exposing Stockfish through the same API
odds.py                material / time-odds match runner
selftest.py            the pre-commit gate: every behavioural contract, colour-coded
setup.sh               builds the three .so files, then runs the selftest
Old Engine/<N>/        frozen version snapshots (engineN.py + its C sources)

NNUE/                  the whole net lane -- config.py (labelling + arch
                       constants), gen_data.py (self-play generation, --syzygy,
                       --label-nnue), train.py, model.py, data_format.py,
                       verify_labels.py (label reproduction gate),
                       label_depth_probe.py + label_teacher_probe.py (is the
                       corpus deep enough / is the net a better teacher),
                       nnue.c, nets/, datasets/, shims/ (one file per A/B arm),
                       campaigns/ (SPRT state, committed the turn a run ends)
uci/                   front ends for FROZEN snapshots: cuci_old.py drives any
                       C-era engineN.py, uci_old/uci_legacy for the Python era
lib/                   shared support: time_manager (clock budgets),
                       interruptible (Ctrl-C/SIGTERM salvage), smp + shared_tt
                       (the PYTHON engine's multi-process Lazy SMP and its
                       lock-free shared TT)
data/                  Perfect2023.bin book, UHO opening EPDs, fen.txt, and the
                       two fitted WDL models (hce + nnue -- different cp scales,
                       never pooled)
syzygy/                local Syzygy 3-4-5 WDL/DTZ tables (gitignored, ~939 MB;
                       scripts/fetch_syzygy.sh pulls and checksums them)
docs/                  design notes, OpenBench guide, progression SVGs, licences
tuning/                texel.py, fit_wdl_model.py and the eval-fitting tools
bench/                 NPS / depth / profiling harnesses, incl. nps_history_bench
testing/               perft, SPRT, and the correctness gates selftest shells out
                       to (test_tt_deadtag, test_wdl_family, test_sprt_resume ...)
scripts/               build, release, A/B campaign, export and fetch scripts
```

`Old Engine/<N>/` holds every historical version, each self-contained. See
its [README](Old%20Engine/README.md).

---

## Notes

- C `.so` files are not committed. They are platform-specific and built by
  `setup.sh`.
- If a `.so` won't load, the engine falls back to pure Python (correct, slower);
  the self-test reports which path is active.

## License

- **Source:** MIT -- see [`LICENSE`](LICENSE).
- **Released binaries** bundle [`python-chess`](https://github.com/niklasf/python-chess)
  (GPL-3.0+), so the binary distribution is GPL-3.0 as a whole. Full text,
  source pointers and credits (Perfect2023 book -- Sedat Canbaz; UHO suites --
  Stefan Pohl) in [`THIRD_PARTY_LICENSES.md`](docs/THIRD_PARTY_LICENSES.md).

---

## Roadmap

Written 2026-09-05, after a 121-item audit of the post-revert tree
(`improvements_audit_v62.md`, gitignored). **97 items are open and every one of
them is named below**, so nothing is unscheduled by silence. Ids are the
audit's.

### Decisions taken, so they stop being re-argued

**The next A/B is SPRT at a 5,000-game cap, not a 10,000-game fixed budget.**
50+0.5, seed 62, **48 workers** (a 96-core box; timed runs are cores/2 because
a game runs two engines and NPS at a fixed clock *is* strength). The audit
asked for a fixed budget so the ledger could advance. That is the wrong trade
when the box bills by the hour: SPRT stops the moment a bound is crossed, and
if it crosses we have the verdict that decides whether v63 ships. If it runs
the full 5,000 without crossing, the point estimate at the cap is quotable and
the ledger advances anyway. So the cheap path can produce the expensive answer,
and never the reverse.

The one thing it cannot do is give a quotable magnitude *and* stop early. A
bound-stopped number stays a verdict, the ledger stays at ~+354, and this file
says so rather than quietly banking it.

**Stockfish 18 is pinned as the yardstick opponent.** Stockfish 19 shipped
2026-09-05 at up to +44 Elo over 18. It is a **different instrument**: no
figure measured against 18 pools with one measured against 19, and upgrading
the binary on any box silently re-bases every odds rung and every strength
figure. Until today nothing recorded which Stockfish played; the version now
sits in the campaign state file and in the instrument key, so a swap fires the
CONFIG-CHANGED diff. Move to SF19 only at a deliberate era boundary, and bridge
it by re-measuring the active rung against both.

### Phase 1 -- the number we do not have

Everything else is measured against a baseline we cannot currently name.

1. **X-01 / OPEN 1.** HEAD vs `Old Engine/61`, 50+0.5, seed 62, 48 workers,
   `--sprt` capped at 5,000 games. Answers "is the v63 candidate better than
   v61", prices the FI-21 aspiration window on a clean core, and is the v63
   release number. ~3.2 box-hours at the cap, less if it stops early.
2. **B-17.** Re-audit campaign provenance: seven tranches ran on an
   unverifiable core. One header commit adding the `.so` md5.
3. **D-18 (owner decision, not a task).** The ledger has no written provenance
   rule and roughly 77 of its ~354 Elo is bound-stopped magnitude. Strict policy
   reads about **+278**. Decide, then write the rule down.

### Phase 2 -- free NPS, and none of it measured where it counts

Zero game slots. Needs an **idle** box, not a match box. At the measured
~1.16 Elo per 1% NPS this is the largest available gain on the board.

4. **S-03** frontier `nn_push` skip: 31% of all pushes build an accumulator
   nothing reads. Node-identical, +1.5-2.2%.
5. **S-04** stack protector still on in the shipped `.so`: 15 guard sites
   including `negamax`, `qsearch`, `see`, `nn_tail`.
6. **S-08 + T-38** PGO: **+5.81% on arm64**, node-identical on all three
   oracles, unmeasured on x86 -- which is where every timed confirm runs.
   ~+6.7 Elo if it holds. **S-12**: do not batch LTO with it. **S-13**: IR-level
   PGO trains faster but is not separable here. **T-31**: PGO retires `.so` md5
   identity, so the snapshot oracle has to change with it.
7. **S-09** history/LMR tables are int32, 48 KiB against a 64 KiB L1d.
   **S-10** five dormant eval gates still inside `eval_white`'s loops, one
   commit each. **S-05 / E-01** SIMD and arm64 kernel coverage, same idle box.
8. Substrate readings, no change: **S-02** (the driver is not a speed problem),
   **S-07** (TT saturation, recorded under a retired TC), **S-15** (no pawn
   cache; ceiling ~1% of search time).
9. Larger and later: **S-06** in-check `gen_legal` full-legality-tests
   everything (~1 box-day of movegen work), **S-14** 24-byte TT entry into a
   lossless 16-byte layout, **S-01** the `TT_EVAL_NONE` wipe (not
   node-identical), **S-11** qsearch HCE stand-pat, **S-16** only if
   `TT_PCSTAMP` ever ships.

### Phase 3 -- the only search work that earned a slot

10. **X-14 / FI-38** depth-scaled SEE margins. Two slots, not one: the k=25
    selection argument is false in the arm config, so **k=25 and k=50 screen
    separately** at 10+0.1, ~1.3 box-hours each.
11. **X-06 + X-11 + D-22** first, not concurrently: 3 matetrack runs (~1
    box-hour) close OPEN 6. The mate decline is the FI-21 window (-0.96 pp), not
    the reverted core (+2.8 +/-3.1), and FI-38's gate depends on it.
12. **X-13** `SOFT_STOP_STABLE_FRAC` governs 26 of 30 moves and is the most
    interesting unexplored lever in the audit -- but ~30 box-hours with no sign
    prior. Only if OPEN 1 finishes early.
13. **X-17** `ASPIRATION_MATE_FLOOR`, 5 paired matetrack rounds, a kill filter
    that cannot confirm its own estimate. After Phase 3's matetrack work.
14. Closed pre-A/B on node oracles, recorded so they are not re-opened:
    **X-04**, **X-08** (FI-37 ETC), **X-09** (FI-34), **X-12**, **X-15**
    (FI-61), **B-16** (OPEN 5 stamping -- the fix makes its own oracle worse).
    **X-18** WITHDRAWS FI-106's closure (stale evidence, sign flips with depth).
    **X-19** FI-107's parameter neighbourhood is closed but its four mechanism
    variants were never built. **X-05** needs a SEE-aware variant first.
    **X-16** and **X-20** are NOT MEASURABLE as they stand. **X-07** is the
    52-toggle census, already run. **X-10** is this plan.

### Phase 4 -- correctness fixes that need measurement

Zero **slot** is not zero **risk**. These change the search tree and are owed a
screen, not a free commit: **B-03** (stager/order_moves SEE disagreement,
breaks P-23 stream identity), **B-07** (unclamped ProbCut mate), **X-02**
(P-04 futility sign inverted), **X-03** (improving stack mixes eval families).

Node-identical or interface-only, so they are free once written: **B-02**
(MultiPV collapses in a TB position), **B-05** (ProbCut child reads "parent was
a null move"), **B-08** (`_clear_stale_abort` erases a host stop), **B-09**
(`SyzygyPath` enables nothing), **B-10** (`_tb_probe_local` ignores the halfmove
clock), **B-12** (`go depth 0` returns a random move), **B-13** (MultiPV lines
in discovery order), **B-04** (`tt_r50_stale` exempts mate values).
**B-11** (FB-24 setup floor, 11.4x overrun) needs its trigger proven first.
**B-14** Syzygy-overridden labels carry no marker.

### Phase 5 -- checks and tooling, all free

15. Coverage pins, seconds of selftest each: **T-01** (fix the retained-TT
    pin's contract comment), **T-02** (no check plays a game to termination),
    **T-03** (the book path is never exercised), **T-06** (absolute movegen
    oracle via python-chess), **T-28** (nothing plumbs a CLI option to an engine
    child), **T-30** (three-oracle node-identity gate as a `setup.sh` acceptance
    step), **T-32** and **T-33** (matetrack is blind to every TT rule, and
    records one run per version with no spread). **T-04** lands opt-in, not as a
    selftest default. **T-05** the fixed-trajectory replay tool, needed before
    X-18 and X-19 can be quoted.
16. **T-21** `--dry-run`: start one worker, print both engines' EFFECTIVE
    options and net hash. This is the missing half of today's `--sf-elo` fix.
17. **T-18** the queue's INT trap cannot stop one candidate. Its `| tee`
    removal is **rejected as specified**: a plain redirect kills the live
    progress bar, and a silent terminal for hours is a worse failure than the
    one it fixes. Fix the trap, keep `tee`.
18. Smaller: **T-07** (AspDelta inert across its range), **T-08** (two spellings
    of the soft-stop fractions), **T-09** (Move Overhead / Hash / Contempt still
    hardcoded), **T-10** (MultiPV movetime sawtooth), **T-11** (`setup.sh`'s
    snapshot rebuild omits `NNUE/nnue.c`), **T-12**, **T-24** (one shim
    registry), **T-34** (the dot-kernel name reaches no log), **T-35**
    (cosmetic atexit traceback), **T-29** and **T-36** and **T-37** (the odds
    yardstick: honest sample size, the h2 castling signature, and time odds).

### Phase 6 -- documentation

**D-08** two tracked SVGs still publish the withdrawn 90.30% figure. **D-09**
the speed badge is on a retired NPS instrument. **D-10** the "biggest jumps"
table is on a third, older one. **D-11** version count and chart alt-text.
**D-12** the `--nodes` footnote is wrong for v48-v51 and v55 onward. **D-16**
and **D-21** shim docstrings describe baselines the revert changed, and the b05
figures look arm-swapped. **D-19** the dead-entry bullet describes an idealised
rule, not the qsearch-only one that ships. **D-20** three toggles document
"mechanism kept" over no-op C setters. **D-01** and **D-02** the net's NPS cost
is quoted four different ways and the quantization text is stale.

### Phase 7 -- the eval lane, and what Stockfish 19 says about it

**The NNUE corpus lane stays closed.** Volume, schedule and label depth are all
measured dead, fifteen consecutive nets have failed to beat v12, and held-out
validation is 0-for-14 as an Elo predictor. **T-13** removes the *reason* it was
last blocked -- nnue-labelled corpora now pass the `verify_labels` hard gate at
HEAD across five independent runs, and the accumulator explanation was wrong --
but a net-labelled corpus is still a corpus axis, and the measured teacher edge
is small. The gate being gone does not move the prior.

Stockfish 19's release notes touch this lane twice, and the honest reading is
*inspiration, not evidence*:

- **Quantization-Aware Training.** SF19 adds QAT. This project already has
  **E-05** (straight-through fake-quant of the tail weights,
  `NNUEModel.FAKE_QUANT`) scoped, with **E-03** showing the quantization MAE is
  almost entirely int8 tail-weight rounding -- 43 cp of it in the shipped v12 --
  and **E-02** correcting the premise the older estimate rested on. SF19 is
  outside evidence that the lane is real. Cost: 4 slots plus a GPU.
- **Rescoring with a strong external net.** SF19 rescored hundreds of billions
  of positions with a Leela net. That is the **external**-teacher arm, which is
  a different proposition from the self-teacher arm the audit closes: our net
  as teacher measures +0.0009 to +0.0060 rank correlation, while an external
  engine is a categorically larger delta. It is the one corpus variant with a
  real prior, and it is still a corpus experiment in a lane that is 0-for-15.
  Not scheduled; named so the option is not forgotten.
- **Pawn-pair features and retiring the small net** are architecture changes to
  SFNNv16 and do not transfer to a 6144-input king-bucketed net without a
  rebuild.

Also worth pricing before any SF19 move: **its strict position validation
terminates the process** on an invalid FEN or UCI command. Our openings come
from UHO and are legal, but the odds harness removes pieces from the start
position, and that path deserves a check before SF19 is ever the opponent.

Remaining eval items: **E-04** material-scaled output (one fixed-node screen),
**E-06** the verified endgame gap -- 50 of 51 R+minor vs R positions score +400
to +550 where the answer is a draw -- and **E-07** NNUE stand-pat in qsearch,
which fails three free oracle steps before it earns its ~30 box-hours.

### What this roadmap does not schedule

**T-24** and the seven-script half of **T-27** are each about a day of
mechanical edits across tools the earlier phases are actively using. Doing them
mid-stream churns the same files. Week two at the earliest.

