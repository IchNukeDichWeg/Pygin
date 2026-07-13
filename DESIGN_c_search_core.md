# Design: C search core (roadmap #29/#30)

**Status:** SHIPPED and iterating — all 4 phases done; live = v47-dev, last snapshot Old Engine/46 (C-era ledger ≈ +186 over v31; v44 = +13.31 TT prefetch, v45 = +13.52 TT-value pruning-eval sharpener, v46 = +5.94 TT doubled to 96 MB — borderline-positive; see the version table below). Armed candidate: TT_BITS=23 (192 MB) memory-scaling follow-up (v47-dev row below); FI-06 +2.26, FI-18 −1.25, FI-04 +2.15 all null (dormant). **Started:** 2026-07-08 (baseline v30, ~90k NPS). **A/B TC:** 45+0.10 through v36, 50+0.20 from v37 campaigns.

## The finding that reframes the project

`movegen.c` already contains a complete, perft-verified C board: an ~80-byte
`Board` struct, `apply_move` (copy-make), `gen_legal`, `in_check`, and a tight
all-C `perft_rec` loop. Measured throughput of that loop (gen + copy-make):

| Loop | Nodes/sec |
|------|-----------|
| C perft (Kiwipete d5/d6) | **~168,000,000** |
| Current engine search (v30) | ~90,000 |

**Make/unmake is therefore NOT the bottleneck** — it runs ~1,800× faster than
the search already. The ~90k ceiling is the *per-node Python work*: the eval
glue (accumulator + pawn structure + taper + the ctypes mobility call),
`order_moves` (MVV-LVA + history + SEE), the dict transposition table, the
pruning gates (RFP/null/razor/futility/LMR/LMP), and the ctypes boundary
crossings between all of them.

Consequence: **porting make/unmake alone would barely help** (confirmed
historically — `fastboard.py`, a faster pure-Python board layer, gave only +9%
CPython / ~0 PyPy). The win only materialises if the **entire per-node loop**
runs in C, so there is no Python interpreter cost and no ctypes crossing per
node. That is a "C search core", not a "make/unmake port".

## Upside / risk

- **Upside:** even reaching 1–2M NPS (a small fraction of the 168M board
  ceiling, throttled by eval/ordering/TT complexity) is a **10–20× NPS gain**
  → several extra plies of depth. That is worth well over +100 Elo (the
  ~3 Elo/1% rule is for small deltas and saturates over 10×, but the depth
  gain alone is large). Plausibly ~2560 → 2700+.
- **Cost:** weeks-to-months. It is effectively a second engine in C that must
  reproduce (or beat) the Python engine's play. The whole feature set —
  negamax, PVS, aspiration, TT, killers/history/continuation history, SEE
  ordering, LMR/LMP, null-move, RFP, razoring, futility, extensions, quiescence
  — has to be ported.
- **Risk:** high (large surface), but the project's proven perft + differential
  verification recipe de-risks each phase, and phases 1–2 are node-identical
  verifiable before any behaviour changes.

## Phased plan (each phase independently verifiable + GO/NO-GO gate)

**Phase 1 — C board layer the search can drive.**
Foundation mostly exists (`Board`, `apply_move`, `gen_legal`, `in_check`,
perft). Copy-make is the chosen model — an 80-byte struct copy is free in C and
avoids undo-stack bugs. Remaining work: the accessor/state API a search needs
(zobrist maintained on the struct, repetition detection, halfmove clock,
insufficient-material, piece lookups). **Gate:** perft exact on the full suite
(already passing) + differential vs python-chess over millions of positions.

**Phase 2 — C static eval (one entry point).**
Port the full static eval into one C function: material + PST + phase taper +
tempo + pawn structure + mobility + king safety (mobility/king-safety already
in `eval_c.c`; add the rest). **Gate:** differential vs the Python eval on
millions of positions, bit-exact (the Python eval is the oracle).
**GO/NO-GO #1:** with board + eval in C, prototype a fixed-depth C alpha-beta
(no TT, no move ordering beyond MVV-LVA) and measure NPS. If it is not already
≥ 5–10× the Python engine, the per-node constant factors are worse than this
analysis predicts — stop and reassess before phase 3.

**Phase 3 — C negamax core (the big phase).**
Port ordering (history/killers/continuation/SEE), a C array TT (fixed-size,
packed entries — `shared_tt.py`'s layout is the prototype), and every pruning
rule. Driven from Python only at the root; Python keeps time management, the
book/tablebase probe, and the UCI/GUI boundary. **Gate:** this is a NEW engine,
not byte-identical to v30 — verify by (a) same-or-better tactical suite,
(b) A/B vs v30 (must be strongly positive), (c) perft still exact.

**Phase 4 — integration. DONE (2026-07-08).** Time management landed with
step 6; the rest:
* **Lazy SMP (real pthreads — the GIL-free payoff):** `set_threads(N)` makes
  each `cs_search_root` spawn N-1 helper threads running the same root
  search (alternating depth/depth+1, full window), stopped when the main
  iteration completes. Shared state is the TT only — now lockless
  (XOR-folded keys, torn racy writes read as misses); everything else is
  `__thread`. Single-thread verified node-identical to the pre-SMP build;
  4 threads: **depth 18 vs 15 in the same 1 s budget** (10.9M aggregate
  nps). `cengine.smp_workers` defaults to **1** (the SMP Elo gain is not
  yet A/B-measured, so multi-threading is opt-in) and honours
  `CLAUDECHESS_SMP`.
* **UCI:** `cuci.py` — Threads/OwnBook/UseTB options, repetition-aware
  `position ... moves`, clock budgets via time_manager, streamed `info`
  lines, `stop`→`bestmove` via the C core's `cs_stop()` abort.
* **Tablebase probe:** delegated to the embedded engine.py (root-only,
  skips trivial wins), plus a cengine difficulty gate — no probe when the
  previous move's verdict was already decisive (±500 cp): at 2.5M nps the
  search converts clear wins faster than the network round-trip.
Remaining from the original phase-4 list: snapshot as the first C-era
version (`Old Engine/31`) — user's call, after the formal A/B/odds runs.

## Phase 1-2 prototype result (2026-07-08) — GO signal

`csearch.c` (isolated `csearch.so`, board layer extracted verbatim from
`movegen.c`, does not touch the shipped libraries): a **material-only**
fixed-depth alpha-beta with MVV-LVA ordering.

| | Nodes/sec |
|---|---|
| C material-only alpha-beta (startpos/Kiwipete/middlegame, d7) | **~20,000,000** |
| Python engine (v30) full search | ~90,000 |
| Ratio | **~224×** |

Legal, sensible root moves in every position. Material-only is the optimistic
ceiling, so the honest gate then linked `eval_c.c` and called the real
`mobility_king_safety` per leaf (the term that dominates per-node eval cost):

| | Nodes/sec | vs Python |
|---|---|---|
| C material-only alpha-beta | ~20,000,000 | ~224× |
| **C + full mobility/king-safety eval (honest)** | **~13,500,000** | **~150×** |
| Python engine (v30) | ~90,000 | 1× |

The expensive eval term cost only ~33% of the NPS (20M → 13.5M), not a cliff.
**GO/NO-GO gate CLEARED at ~150× with the honest eval.** Even after phase 3
adds the rest (history/killers/SEE ordering, C-array TT, quiescence, and all
the pruning — which adds per-node work but also *removes* nodes), a
conservative 3-6× slowdown from here still leaves ~25-50× the current engine.
**Decision: GO for phase 3.**

## Phase 3 breakdown (the big multi-session port)

Driven from Python at the root only; each sub-step verified before the next:
1. **Move ordering in C — DONE (2026-07-08).** history[color][from<<6|to],
   killers[ply], counter[prev] C arrays + SEE-demoted MVV-LVA captures, with
   gravity history + killer/counter updates on quiet beta-cutoffs. Verified
   **value-identical** to plain alpha-beta (`set_order_mode` 0 vs 1: root score
   matches on startpos/Kiwipete/middlegame) with node reductions of 47% / 4% /
   16%. NPS 13.5M → 11.4M (ordering overhead; the large ordering win is the
   TT move, which lands in step 2). Continuation history deferred (needs the
   move stack; fold in with step 3).
2. **C-array transposition table — DONE (2026-07-08).** Fixed 2^21 x 24-byte
   entries, depth-preferred replace, ply-relative mate encoding, O(1) mix-hash
   board key (full key stored + checked on probe → collisions rejected). TT
   move scored first in ordering (`ORD_TT`). Verified **value-identical** to
   the no-TT search (`set_use_tt` 0 vs 1: root score matches at depth 8) with
   node reductions **53% / 59% / 45%** (startpos / Kiwipete / middlegame). NPS
   11.4M → 6.9M (probe/store/hash + a conservative full clear per search; in
   real iterative-deepening the TT persists across iterations and moves, so
   this understates steady-state). ~76× the Python engine.
3. **Pruning — DONE (2026-07-08).** PVS + null-move + reverse-futility (static
   null) + LMR (log table) + LMP + frontier futility, with the `in_chk` hint
   threaded to children (parent's post-move `in_check` → child, and `0` to the
   null child). First lossy step, so verified differently: **98.7% node
   reduction** at depth 8 (`set_prune` 0 vs 1) and a **tactical suite intact**
   — free queen (fxg4 +899), back-rank mate (Rd8# found at depth 2 in pure
   alpha-beta, confirming mate detection), promotion (g8=Q), and correctly
   *declining* a rook-defended knight capture (down-a-knight −342). Move
   quality on quiet positions is still governed by the impoverished eval
   (material + mobility only — no PST/pawn-structure), which is step 5;
   the *search machinery* is correct. Razoring/extensions deferred (razoring
   wants qsearch = step 4; check extensions want a budget to avoid the
   fixed-depth infinite-extension trap). NPS 6.9M → 3.8M (extra static eval +
   per-move gives-check + PVS re-searches), but nodes/depth fell ~75×, so
   effective depth-in-fixed-time rose sharply — NPS alone understates pruning.
4. **Quiescence — DONE (2026-07-08).** Stand-pat (fail-soft), SEE-pruned
   losing captures, delta pruning, full evasions when in check (never
   stand-pat out of a mate), recursion guard. Verified: tactical suite still
   PASS (free queen, back-rank mate, queen promotion), and — the clearest
   sign the core now behaves like a real engine — quiescence **stabilises the
   leaf scores**: qs-off gave horizon-distorted negatives (startpos −10,
   Kiwipete −73, middlegame −42), qs-on gives sensible near-zero values
   (+1 / +7 / +22) and better moves (Nf3, captures) even under the impoverished
   eval. NPS 3.8M → 2.2M (~24× the Python engine).
5. **Full static eval in C — DONE (2026-07-08).** Complete port of
   `_evaluate_static`: tapered material+PST base (trunc-toward-zero blend,
   tempo), doubled/isolated/backward/passed pawn structure (V-06-style
   precomputed passer-taper table), the lone-loser strong mop-up shortcut,
   and eval_c.c's `mobility_king_safety` (linked in; csearch.so carries its
   own copy of those globals). Tables/params are NOT hard-coded: they arrive
   at init via `csearch_set_eval` + the same exported `set_*` calls
   engine.py's `_sync_c_params` makes (the harness literally re-runs
   `_sync_c_params` pointed at csearch.so), so engine.py stays the single
   source of truth and a retune cannot desync the C copy. Verified
   **bit-exact vs the Python `_evaluate_static` oracle over 3,000,000
   random positions — 0 mismatches** (random playouts + lone-king strips
   to force the mop-up path; 421,958 lone-loser positions hit).
   Search-level: tactical suite
   still PASS, and quiet-move quality is fixed — startpos now plays d2d4
   (+20, tempo-sized) instead of material-only nonsense; a quiet K+P
   endgame gets a sensible king move at ~6.8M nps. NPS **2.55M overall
   (~28x the Python engine)** — UP from step 4's 2.2M despite the heavier
   eval, because the step-4-review TT-move fix plus a real eval both
   improve ordering (fewer PVS re-searches). Pre-step-5 review also fixed
   two search bugs: the TT-move key was stored with a 16-bit mask (bit 15 =
   mover PT low bit, so TT-move ordering silently never fired for
   pawn/bishop/queen movers) and qsearch scored stalemate as static eval
   instead of 0.
6. **Root driver + time management — DONE (2026-07-08).** `cengine.py`: a
   drop-in `Engine` for match.py/battle_worker with the whole per-node loop
   in C. Python keeps exactly the root/game-state layer: v30's ID loop with
   aspiration windows (min-depth 4, delta 30, geometric widening), the
   P-35/U-06 stability-scaled soft-stop (same constants), v30's
   partial-iteration rule (aborted depth used iff >= 1 root move completed —
   the C root reports `out_done`/`out_aborted`), the book probe (delegated
   to an embedded engine.Engine, which is also the eval-param source), and
   v30's TT retention rule (C TT persists; `cs_tt_reset()` after an
   irreversible root move). C-side gaps closed: **TT persistence** via a
   generation field in the (still 24-byte) entry with gen-aware
   depth-preferred replacement; **time abort** via a monotonic deadline
   checked every 4096 nodes (~1.6 ms at 2.5M nps), aborts unwind without
   storing garbage; **repetition detection** via a path-key stack + game
   history keys fed from the root (`cs_board_key` export -- Python computes
   history keys with the search's own hash), scan step 2 within the
   halfmove-clock window; **50-move** via a halfmove clock threaded through
   the search (in-check at 100 plays on, v30's rule); **insufficient
   material** with v30's cheap `pawns|rooks|queens` pre-filter; all three
   contempt-scored via a `_draw_score` port (`csearch_set_draw`).
   Deliberate deviations (documented in cengine.py): no root random
   tiebreak. (The other v1 deviations retired: CB-01 added qsearch
   repetition/50-move/insufficient-material, Phase 4 added Lazy SMP and
   the gated TB probe, EP-01 fixed the raw-ep hashing.)
   Verified: KQK mate in 8 moves at 0.2s/move; winning side steps around
   threefold; 125-ply clean self-play game; budget honored (338 ms of a
   600 ms budget -- soft-stop banking); mate-score conventions map to v30's
   (MATE_SCORE 1M). **Depth 14 in 0.34 s on a middlegame position — v30
   reaches ~8 at the same TC.** selftest.py gained a C-core check;
   setup.sh builds csearch.so.

**Verification:** phases 1-2 and step 5 are differential/perft node-exact
against python-chess / the Python eval. The full core is a NEW engine (not
byte-identical to v30) — gate it by tactical-suite solve rate + **A/B vs v30
(must be strongly positive)** + perft still exact. Snapshot as the first C-era
version only after that A/B confirms.

## Final gate — A/B vs v30 (2026-07-08)

30-game smoke through the real match.py plumbing (45+0.1 clock, book off,
WDL adjudication, UHO openings):

| | W | D | L | Score | Elo (raw) |
|---|---|---|---|---|---|
| **cengine vs v30 (engine.py)** | **29** | 1 | **0** | **98.33%** | **+708 ±1677** |

Pentanomial 14 WW + 1 WD, zero lost/drawn pairs; 23/30 games ended by
adjudication (both engines' score reports agreeing — the driver's White-POV
cp convention verified in anger); zero errors in the log; ~8 s/game.
**Gate: strongly positive beyond any statistical doubt** (a 29-0-1 start
puts the 95% lower bound far above +300). At a 98% score this pairing is
outside Elo's measuring range — the meaningful next measurements are
external: the rook-odds-vs-full-Stockfish line (v30: dead even at 50.50%)
and stronger reference opponents. Perft: movegen unchanged, selftest exact.
Tactical suite: PASS (step 5).

**External re-date (2026-07-08): rook odds vs FULL Stockfish, 400 games @
45+0.15 — cengine 364W/18D/18L = 93.25%, +456 ±169 Elo** (345 wins by
checkmate; ~6.5 s/game). The line that v25→v30's +139 internal Elo could
not budge (48.00% → 50.50%, dead even twice) moved ~450 Elo in one step.
Rook odds is now saturated as a yardstick, like queen odds before it —
knight odds (Nb1) is the next external progress benchmark.

## C-era feature ledger (post-phase-4; adjacent A/Bs vs the previous snapshot)

| Version | Feature | A/B result | Verdict |
|---|---|---|---|
| v32 | P-03 Internal Iterative Reduction (`set_iir`) | **+7.30 ±6.8** (10k @ 45+0.1, 51.05%, ptnml 347/1155/1864/1209/425, norm +13.99) vs v31 | **CONFIRMED** 2026-07-08 |
| — | P-20a king shelter eval toggle | −4.27 ±6.8 (10k @ 45+0.1, 49.38%) vs v32 | REJECTED 2026-07-08, reverted (depth-8 signal subsumed by deep search) |
| v33 | P-14 TT kept warm across irreversible moves (`TT_KEEP_WARM`) | **+23.52 ±6.8** (10k @ 45+0.1, 53.38%, ptnml 319/1002/1898/1246/535, norm +44.49) vs v32 | **CONFIRMED** 2026-07-09 |
| v34 | P-01 check extensions (`set_check_ext`, +1 ply on checking moves, per-line budget 5 = v30's recipe) | **+6.81 ±6.8** (10k @ 45+0.1, 50.98%, ptnml 404/1087/1880/1167/462, norm +12.74, pair ratio 1.09) vs v33 — weakest confirmed gain, all secondary signals agree | **CONFIRMED** 2026-07-09, snapshotted Old Engine/34 |
| — | P-17 4-way set-associative TT (`set_tt_ways`) | −2.50 ±6.8 (10k @ 45+0.1, 49.64%, ptnml 428/1154/1900/1098/420, norm −4.71, pair ratio 0.96) vs v34 | REJECTED 2026-07-09, reverted (the ~15% deep-node savings did not convert to Elo — the direct-mapped table wasn't collision-bound enough in real games; don't re-try without a materially different eviction rule or table size) |
| — | P-43 single-reply / forced-move extension (`set_single_reply`; a node with one legal move gets +1 ply from its own budget=5, separate from the check budget) | +4.59 ±6.8 (10k) then +2.40 ±6.8 (10k, offset 5000) = **pooled +3.5 ±4.8 over 20k** (50.50%, ptnml 788/2265/3768/2316/863) vs v34 — positive on every secondary signal, sub-significant even at 20k | **KEPT-MARGINAL, DORMANT** 2026-07-09 (default OFF, user call; mechanism monotone-safe, re-test at longer TC someday; default reproduces v34 node-exactly). Recapture ext deliberately skipped — v30 found it redundant with qsearch+SEE, which the C core has |
| — | P-04 "improving" flag (`set_improving`; v30's exact recipe: per-thread eval stack, improving = own static eval > two plies ago; RFP margin ×(depth−improving), frontier futility +RFP/2 when declining, LMR+1 when declining) | **+0.38 ±6.8** (10k @ 45+0.1, 50.06%, ptnml 402/1174/1837/1185/402 — symmetric, pair ratio 1.01, norm +0.72) vs v34 — a dead NULL despite −56% nodes at d12 and +1 ply in the 2s probe: the deeper search saw nothing the shallower one didn't at this TC | **NULL, DORMANT** 2026-07-09 (default OFF; re-test only at a longer TC; don't buy more games — resolving ±0.4 needs ~3M) |
| infra | P-26 tuning infrastructure: selectivity constants runtime-settable (csearch.c `set_rfp/set_fut_margin/set_delta_margin/set_lmp/set_null_move/set_lmr_div`, defaults = shipped values, node-exact by ladder), 11 UCI options in cuci.py (RFPMargin/RFPDepth/FutMargin/DeltaMargin/LMPScale/LMRDiv/NullBase/NullDiv/AspDelta/SoftStable/SoftUnstable), `pygin-uci.sh` self-locating UCI wrapper, `tune_config.json`/`tune_smoke.json` for kiudee chess-tuning-tools (BO over 9 params, 1000 games/point @45+0.1, both engines = cuci, engine2 at defaults) | cutechess-cli loop validated locally (games to mate + adjudication via the wrapper); tuner schema keys verified against tune/local.py source; setters verified to change the search and restore defaults exactly | **INFRA** 2026-07-09 — tuned best-point must still pass the standard 10k match.py A/B vs v34 before shipping (winner's curse) |
| v35-dev | P-22 noisy-only qsearch generation (`set_qgen`, default on; `gen_noisy` emits gen_legal's exact noisy subset — captures / promos / ep — in the same relative order; stalemate still detected before stand-pat via an early-exit `has_legal_quiet` when the noisy list is empty; in-check path unchanged) | **NODE-IDENTICAL** verified over 8 FENs × d6/d10 (incl. promo-heavy + near-stalemate); **+31.9% NPS** on a 4-position mixed bench (4.03M→5.31M), **+55%** on the startpos 2s probe (2.7M→4.2M) | **KEPT (speed-only)** 2026-07-09 — provably same tree, so no fixed-depth A/B needed. **Timed-play Elo MEASURED 2026-07-10 (as the P-22+P-44 bundle vs Old Engine/34): ~+71.8 ±8.5 @ 7,061 games, stopped as decisive — right in the ~2–3 Elo/1% NPS band for +32%. The biggest single C-era gain (~3× P-14); P-44's share inside it pending the isolation A/B (cengine vs engine_qtt_off). LESSON: "node-identical" exempts only the fixed-depth gate, not the Elo ledger — speed changes get their own timed A/B before the next feature stacks on top** |
| v35 | P-44 qsearch TT probe/store (`set_qs_tt`, default on; probe before movegen+eval so a hit skips the node, any stored depth cuts; stores at depth 0 — stand-pat cutoffs as LOWER + resolved nodes by bound — gen-aware so they never displace same-key negamax entries; TT move seeds qsearch ordering; ply-relative mates) | **Isolation A/B vs the P-22 base (engine_qtt_off): +8.06 ±6.8** (10k @ 45+0.1, 51.16%, ptnml 359/1123/1891/1181/446, norm +15.35, pair ratio 1.10) — CI clear of zero; the warm table across a game delivered what the flat cold-ladder time-to-depth couldn't show. Bundle with P-22 directly vs v34: ~+71.8 ±8.5 @ 7,061 games — parts compose (~+64 + ~+8) | **CONFIRMED** 2026-07-10, snapshotted Old Engine/35 (v35 = v34 + P-22 + P-44 ≈ +72, the biggest C-era step) |
| — | P-45 TT prefetch (child's TT line prefetched after `apply_move`) | Node-identical; **NULL on BOTH architectures**: Apple Silicon −0.7% median time-to-depth, server x86 +0.6% — modern OoO cores hide the TT latency without help; the extra board_key per move isn't paid back | **NULL, REVERTED** 2026-07-10 (bench-gated, no A/B spent; don't re-try bare prefetch — only worth revisiting bundled with P-27 incremental hashing, where the child key comes free) |
| v36-dev | P-46 lazy qsearch generation (`set_qs_lazy`, default on; eval + stand-pat BEFORE movegen — the many stand-pat-cutoff nodes skip generation entirely; stalemate exactness kept via an early-exit any-legal-move check, quiet scan first then the noisy list) | **NODE-IDENTICAL on/off** (7 FENs × d6/d10 + ladder natively); speed: middlegame +0.3%, startpos +2.4%, **pawn endgame +27.5%** (eval dominates busy boards and is paid either way; gen is the big slice only on quiet boards) | **KEPT (speed-only, batched)** 2026-07-10 — ~+1–3% aggregate is unresolvable at ±6.8 alone; its Elo rides in the next NPS-batch A/B (with P-23 staged ordering) vs Old Engine/35 |
| v36-dev | P-23 staged move ordering (`set_staged`, default on; TT-move played via reconstruct+validate `move_from_key` with zero generation, then lazily: captures (SEE-demoted), killer0/1, counter, quiets by history, bad captures — each class generated only when the search reaches it) | VERIFY mode proved stream == order_moves under identical state over ~1M nodes (incl. Kiwipete); **live trees deliberately diverge: quiet stages see FRESHER history than v35's entry snapshot (often fewer nodes, e.g. startpos d10 −19%)**; ~+10–20% NPS; tactics + mate + Kiwipete-d11 pass; `set_staged(0)` = v35 node-exact | **CONFIRMED** 2026-07-10: **+24.67 ±6.8** (10k @ 45+0.1, 53.55%, ptnml 295/998/1911/1295/501, pair ratio 1.39, norm +47.51) vs Old Engine/35 — snapshotted Old Engine/36 (v36 = v35 + P-23 [+P-46 rider]); closes the 45+0.10 era, 50+0.20 from v37 campaigns on |
| — | Q-01 continuation history (`set_cont_hist`, default OFF; quiet ordering += `cont1[prev-move][move]` + `cont2[move-2-back][move]`, piece-to keyed 448×448 int16 tables, per-ply context stack `g_ctx`, same gravity/malus as butterfly history at quiet cutoffs; root context empty + qsearch reads none — documented deviations from v30) | Toggle-off node-exact vs v36 (12-depth ladder); VERIFY mode with cont ON: staged stream == order_moves over ~506k nodes; tactics + mate intact; d10–12 nodes ±4–18% (tree reshapes, A/B decides) | **NULL, DORMANT** 2026-07-10: **−0.87 ±6.8** (10k @ 50+0.20 — the first campaign of that era; 49.88%, ptnml 374/1136/1955/1211/324, pair ratio 1.02, norm −1.71) vs Old Engine/36 — finer quiet scores bought nothing at this depth and the ~1.6MB of tables cost cache (per-move clears now skipped when off). Default OFF = v36 node-exact; re-test only at a much longer TC |
| — | P-47 check-extension budget raise to 8 (`set_check_ext_budget`; 5 = v36 node-exact) | Sanity: 3/4 mate-in-6+ FENs diverged @d9, one flipped +734cp→mate-in-8 — the mechanism worked, the Elo didn't follow | **REJECTED** 2026-07-10: **−4.59 ±6.8** (10k @ 50+0.20, 49.34%, ptnml 378/1163/1986/1159/314, pair ratio 0.96, norm −9.09) vs Old Engine/36 — deeper check lines cost more than they find; extensions vein CLOSED at this TC (P-01 +6.8, P-43 +3.5 marginal, P-47 −4.6); reverted to 5 |
| shipped | PV-01 triangular PV (`cs_get_pv`; each PV node prepends its best move to the child's line on an in-window score — negamax, qsearch and the root all collect; the driver's `_extract_pv` emits the exact prefix in full, uncapped for mate lines, splicing the old TT walk only past a truncation, falling back to the pure walk on a fail-low/partial iteration) | **NODE-EXACT** (v36 ladder passes natively — zero search decisions read the tables); mate-in-4/5 spot check @1s: 5/6 full mate PVs. **Full matetrack re-run: Bad-PVs ~60% UNCHANGED — with the warm TT, PV nodes take exact cutoffs almost immediately (check extensions inflate stored depths along mate lines), so the exact prefix is often 1 move; a per-iteration root-PV wipe on aborted final iterations was also found and fixed (zeroed per game move now)** | **KEPT (reporting-only, necessary-but-not-sufficient)** 2026-07-10 — the truncation source is TT cutoffs at PV nodes; Bad-PVs drop only with PV-02 |
| v37 | PV-02 exact PV (`cengine.PV_EXACT = True` → `set_pv_exact`; PV nodes skip the TT cutoff/bound-narrowing block — the standard strong-engine rule; the TT move still orders) | Verified on a failing matetrack FEN: C PV goes 1 move → full 13-ply mate PV ending in checkmate; d12 ladder FEN −23% nodes (tree reshape) | **CONFIRMED (clean null = free correctness)** 2026-07-10: **+0.17 ±6.8** (10k @ 50+0.20, 50.02%, ptnml 347/1177/1922/1232/322, pair ratio 1.02, norm +0.34) vs Old Engine/36 — kept ON, snapshotted Old Engine/37 (v37 = v36 + PV-01 + PV-02, the exact-PV correctness release); CE_LADDER re-measured, matetrack re-baseline follows |
| — | Outpost re-test (`cengine.USE_OUTPOST`; P-20a sync mechanism) | Bit-exact vs the oracle over 16k positions; Python-era solo verdict +0 ±10 @ depth 8 | **NULL, OFF** 2026-07-10: **−0.90 ±6.8** (10k @ 50+0.20, 49.87%, ptnml 289/1230/1982/1216/283, pair ratio 0.99) vs Old Engine/37 — the depth-8 null stayed a null at depth ~14 (P-20a's subsumption logic); an eval null buys nothing and costs eval work, so OFF. C-era eval add-ons 0-for-2 ⇒ the 2k-game screen rule is now hard policy |
| v38 | CB-01 correctness batch (`cengine.SCORE_HYGIENE` → `set_score_hygiene`; 7 fixes: Texel delta values, qsearch in-check repetition + insufficient material, null fail-soft + TT LOWER store, qsearch TT alpha narrowing, mate-distance pruning [non-PV], deep-qsearch killer slot) | OFF = v37 node-exact (ladder-pinned); matetrack @0.5s 692/600 → **868/751, ZERO Bad PVs** (MDP ~+25% found); MDP@PV starved PV-01 (470 Bad PVs) → non-PV restriction | **CONFIRMED (null KEPT as correctness)** 2026-07-10: **+1.36 ±6.8** (10k @ 50+0.20, 50.20%, ptnml 257/1208/2043/1223/269, pair ratio 1.02, norm +2.85) vs Old Engine/37 — snapshotted Old Engine/38 |
| infra | Phase-0 hygiene batch (final_improvements.md FB-01/02/04/06/09/10/11/13 + FI-13a–d): ucinewgame-hold deadlock fix, search-thread exception guard (bestmove always emitted), Engine config-fingerprint guard + TT reset at construction, PHASE_MAX read from eval_c (taper no longer hardcodes 24), authoritative 14-toggle push from cengine, `go nodes` via C `set_node_limit` (abi 7), movetime-0 clamp, book/TB setup deducted from the budget, stale-`_abort` guard, multi-word setoption, calloc retry + NULL-guarded TT consumers, SMP clamp, pre-thread board snapshot, seldepth+hashfull info fields, Move Overhead option, OpenBench `bench` (signature 1,711,610 nodes @ d11×6), uci config fingerprint line | Ladder node-exact throughout (every change is default-identical — that WAS the drift test); paced cuci smoke: no deadlock, 3 gos → 3 bestmoves, node cap stops at ~53k/50k, bench reproducible | **INFRA** 2026-07-10 — zero tree change; liveness + measurement hygiene for every future campaign |
| v39 | FI-02/FI-03 NPS batch (Phase-2 train, part 1): apply_move mover-from-word, SEE verdict tagged in move-word bits 22-23 (ordering computes once, qsearch reuses), lazy pick_next on non-staged paths (stable shift-to-front), static eval cached in TT spare bits (exact by determinism, reused at negamax + qsearch stand-pat), static-inline hints | **NODE-IDENTICAL** (ladder bit-exact after every item); eval-cache differential clean over 15.9M nodes; paired alternating bench vs v38: **+3.94% median, 9/9 pairs positive**; −flto probed NULL on Apple Silicon (not adopted, P-45 lesson holds) | **CONFIRMED into v39** 2026-07-11 — shipped with FI-01 as the Phase-2 batch (+8.86 ±6.8 vs Old Engine/38) |
| v39 | FI-01 incremental Zobrist (Phase-2 train, part 2): position key XOR-maintained on the Board through apply_move/make_null (splitmix64, fixed seed); `key_from_scratch` = the oracle; EP-01's FIDE filter became an O(1) `board_key` fixup (toggle preserved) | ZKEY differential clean over **52.4M nodes**; d1–5 ladder bit-exact vs v38, deeper counts drift (key values → TT collisions, not logic); matetrack 896/767, zero Bad PVs; paired bench full train **+8.92% NPS median, 9/9** (Zobrist ~+4.8% on top of part 1) | **CONFIRMED into v39** 2026-07-11: **+8.86 ±6.8** (10k @ 50+0.20, 51.28%, ptnml 218/1158/2042/1315/267, pair ratio 1.15, norm +18.89) vs Old Engine/38 — snapshotted Old Engine/39; the +8.9% NPS converted at ~1 Elo/1% |
| v40 | EP-01 FIDE-exact ep hashing (`cengine.EP_FILTER = True` → `set_ep_filter`; the position key counts an ep square only when a legal ep capture exists, = python-chess's `_transposition_key` — repetition detection finally agrees with the arbiter, no phantom-ep key splits) | Since FI-01 an O(1) `board_key` fixup that runs only when an ep square is set (near-zero cost); OFF = v39 node-exact; the merged phantom-ep TT entries even save nodes (713,014 → 562,363 @d12); CE_LADDER re-pinned to v40, snapshot node-identical | **CONFIRMED into v40** 2026-07-11 (seventh 50+0.20 campaign): **+4.31 ±6.8** (10k @ 50+0.20, 50.62%, ptnml 227/1203/2064/1231/275, pair ratio 1.05, norm +9.14) vs Old Engine/39 — a null KEPT as correctness (PV-02/CB-01 precedent); snapshotted Old Engine/40 |
| — | FI-08/Q-03 qsearch depth-0 eviction guard (`set_qs_evict_max`; a P-44 stand-pat store may replace a prior-GENERATION entry only if its depth ≤ N; -1 = off) | Cold-TT ladder provably unaffected; warm probe @16-bit TT −8.3% nodes for the same depth | **DEAD NULL, DORMANT** 2026-07-11 (eighth 50+0.20 campaign vs Old Engine/40): **+0.14 ±6.8** @10k (50.02%, ptnml 245/1189/2115/1219/232, pair ratio 1.01, norm +0.30) — not a correctness fix, so the Q-01/P-04 rule applies: default `QS_EVICT_MAX = -1`, mechanism kept. Side-signal: the 48 MB table is not saturation-bound at this TC (deprioritizes FI-20) |
| v41 | CB-02 correctness batch #4 (`cengine.CB2 = True` → `set_cb2` + driver logic): (a) FB-22 null-move TT store obeys the replacement policy — never clobbers deeper entries, keeps a same-key entry's move; (b) FI-27.1 qsearch 50-move rule; (c) FI-24c deep null cutoffs (depth ≥ 10) verified with a reduced no-null re-search (`g_no_null`); (d) FB-23 root fail-high adoption/promotion across aspiration calls (v30's `_partial_root_move` rule) | OFF = v40 node-exact; shipped default diverges (562,363 → 828,672 @d12 — verification re-searches); CE_LADDER re-pinned to v41, snapshot node-identical (80,121@d8 / 828,672@d12); mate suite 4/4 both configs | **CONFIRMED into v41** 2026-07-11 (ninth 50+0.20 campaign): **−2.88 ±6.8** (10k @ 50+0.20, 49.59%, ptnml 287/1198/2086/1169/260, pair ratio 0.96, norm −6.04) vs Old Engine/40 — a null KEPT as correctness (fourth of its class); snapshotted Old Engine/41 |
| v42 | CW-01 cannot-win eval clamp (`cengine.CANTWIN = True` → `set_cantwin` + engine.py `use_cantwin` mirror): eval clamps to 0 when the favored side has no pawns, no rooks/queens, and at most a lone minor (or two knights) — cannot force mate ⇒ true upper bound is a draw; fixes user-reported horizon draw-dodging | OFF = v41 eval exactly; ladder untouched (clamp cannot fire while both sides keep pawns — verified); oracle differential clean ×389; reported position +2.92/shuffles → 0.00/plays Kxc4 | **CONFIRMED into v42** 2026-07-11 (tenth 50+0.20 campaign): **+3.27 ±6.8** (10k @ 50+0.20, 50.47%, ptnml 257/1115/2159/1215/254, pair ratio 1.07, norm +6.98) vs Old Engine/41 — a null KEPT as correctness (fifth of its class); snapshotted Old Engine/42 |
| v43 | NV-01 verification isolation (`cengine.NULL_VERIFY = False` → `set_null_verify`): v42 MINUS CB-02(c), the deep-null verification search | True = v42 node-exact; CE_LADDER re-pinned to v43 (d12 828,672 → 828,647); snapshot node-identical; verify-off recovers d18 at 5s startpos | **RESOLVED into v43** 2026-07-11 (eleventh 50+0.20 campaign): the REMOVAL measured **+5.18 ±6.8** (10k @ 50+0.20, 50.74%, ptnml 258/1151/2068/1230/293, pair ratio 1.08, norm +10.82) vs Old Engine/42 — converging with CB-02's −2.88 lean, the insurance priced at ~3-5 Elo of nodes-to-depth and DROPPED (modern practice); snapshotted Old Engine/43 |
| — | FI-04 history-based LMR (`set_lmr_hist`; quiet's butterfly history nudges its reduction ±1, div 8192 = strong signals only) | 0 = v43 node-exact; inert at ladder depths, fires at match depth ~17+ (div-512 probe proved the mechanism) | **NULL, DORMANT** 2026-07-12 (twelfth 50+0.20 campaign vs Old Engine/43): **+2.15 ±6.8** @10k (50.31%, ptnml 271/1160/2073/1228/268, pair ratio 1.05, norm +4.52) — below the pre-registered +3 tune threshold, no divisor tune; the finer-quiet-signal vein is 0-for-3 (Q-01 −0.87, P-42 −16.4, FI-04 +2.15) even for the wave's 5/5-consensus form |
| v44 | FI-26a TT prefetch (`TT_PREFETCH(c.key)` after apply_move in negamax/qsearch/root; unconditional — no toggle, deleting the macro line restores v43): P-45's null INVERTED by FI-01's free child key | NODE-IDENTICAL (a prefetch is a hint) — ladder passes UNCHANGED, no pin; paired bench +4.9% NPS median, 3/3 tight pairs (+4.7/+4.7/+5.8, warmup-discarded); a staged-quiet lazy pick was tried alongside, stream-identical and ladder-verified, but PARKED — the noisy-session bench couldn't separate it and its all-node worst case is memmove-heavy | **CONFIRMED +13.31 ±6.8** @10k 50+0.20 vs Old Engine/43 (51.91%, ptnml 250/1050/2073/1321/306, pair ratio 1.25, norm +27.85) — thirteenth campaign 2026-07-12; ~2.7 Elo/1% NPS, nearly 3× the ~+5 estimate and the biggest single NPS win of the C era in Elo terms. Snapshotted Old Engine/44 |
| v45 | FI-25 TT-value pruning-eval sharpener (`set_tt_eval_sharpen` / `TT_EVAL_SHARPEN` class attr; the TT hit's SEARCH value replaces the raw static eval in RFP/null-move/frontier-futility whenever its bound provably improves it — LOWER above / UPPER below / EXACT always; non-mate values, any entry depth). static_eval stays RAW for the FI-03 TT cache and the P-04 eval stack (exactness invariants) | toggle-off = v44 node-exact (was the ladder pin while armed); CE_LADDER re-measured with it ON (d12 828,647 → 767,017, −7% — nets toward more pruning); NPS unchanged (two integer compares) | **CONFIRMED +13.52 ±6.8** @10k 50+0.20 vs Old Engine/44 (51.94%, ptnml 225/1100/2056/1299/320, pair ratio 1.22, norm +28.34) — fourteenth campaign 2026-07-12, sonnet5's top new idea at full value, back to back with v44's +13.31; matetrack 913/783 (up from 896/767). Snapshotted Old Engine/45 |
| — | FI-18 SEE pruning of losing captures (`set_see_prune` / `SEE_PRUNE` class attr; skip SEE-negative captures at non-PV, not-in-check, non-check-giving nodes, depth ≤ 3, move index ≥ 3, best > −MATE_THRESH). The SEE verdict is FREE: the staged stream's stage-6 emissions ARE the losing captures, the array path reads the FI-02.3 tag (bits 22-23) — zero new SEE calls | toggle-off = v45 node-exact (ladder pin `set_see_prune(0)`); matetrack WITH it on: 913/783 — no tactical regression (this feature's failure mode) | **NULL, DORMANT** 2026-07-13 (fifteenth campaign vs Old Engine/45): **−1.25 ±6.8** @10k (49.82%, ptnml 288/1213/2025/1195/279, pair ratio 0.98, norm −2.59) — even the standard-everywhere prune doesn't pay here; bad captures were already ordered last, so alpha-beta got most of the skip for free. Not correctness ⇒ SEE_PRUNE=False, mechanism kept |
| — | FI-06 root-move ordering (`set_root_order` / `ROOT_ORDER` class attr): after each completed iteration the MAIN thread records every root move's subtree node count; the next iteration (same root key) keeps the PV/prev move first and stable-sorts the rest by count descending (a fail-low move that ate a big tree = the likeliest refutation). Iteration 1 of a fresh game move seeds ordering from the persistent TT's stored move (P-14's warm asset). Helpers guarded out (`g_is_helper`) — no shared-state race, their diverse ordering stays v45. Partial (fail-high-cut) records never overwrite fuller ones | toggle-off = v45 node-exact (ladder pin `set_root_order(0)`); ON: same best moves on the 4-FEN d9 probe, FEN2 −24% nodes; root-only bookkeeping, zero per-node cost | **NULL, DORMANT** 2026-07-13 (sixteenth campaign vs Old Engine/45): **+2.26 ±6.8** @10k (50.32%, ptnml 282/1189/2028/1184/317, pair ratio 1.02, norm +4.63) — a positive lean in the predicted +0-4 band but the CI covers zero; same magnitude/verdict as FI-04's +2.15, not correctness ⇒ ROOT_ORDER=False, mechanism kept |
| v46 | TT_BITS=22 (`TT_BITS` class attr / `set_tt_bits`; 96 MB table, up from v45's 48 MB): motivated by a live hashfull capture — a single deep search fills ~half the 48 MB table, the warm persistent TT then climbs to 950‰+ within a game | CE_LADDER re-measured at 22 bits (diverges from the 21-bit v45 ladder at d8+ — more slots, fewer index collisions); 21 = v45 node-exact | **BORDERLINE-POSITIVE +5.94 ±6.8** @10k 50+0.20 vs Old Engine/45 (50.85%, ptnml 264/1157/2014/1274/291, pair ratio 1.10, norm +12.33) — seventeenth campaign 2026-07-13 at full 223-worker load. CI just touches zero; SHIPPED on the monotonic-low-risk rule (a bigger table can't worsen decision quality at fixed nodes; the only downside, DRAM bandwidth, was exercised at full load = net +). Snapshotted Old Engine/46 |
| v47-dev | TT_BITS=23 (192 MB, doubling again): the memory-scaling follow-up — does the +5.94 compound, or does 223 engines striding a 4× footprint start losing to DRAM bandwidth? | 22 = v46 node-exact (ladder pin `TT_BITS=22`) | **PENDING A/B vs Old Engine/46** — eighteenth 50+0.20 campaign at full 223-worker load. RAM fine (223×192 MB ≈ 43 GB, server 184 GB free). If +, ship v47 and probe 24; if ~0, 22 was the sweet spot; if −, bandwidth ceiling found |

## Recommendation

The upside is the single largest remaining lever (the only path to *multiples*
of NPS, and NPS is what converts to Elo here), but it is a genuine
weeks-to-months commitment that forks the codebase into a C engine. Do it
**only** with the GO/NO-GO gate at the end of phase 2 respected — that gate is
cheap (board + eval + a toy alpha-beta) and answers "will the constant factors
actually deliver the 10×?" before committing to the expensive phase 3.

If that appetite isn't there, v30 is a strong place to stop (~2560 internal,
dead-even with full Stockfish at rook odds, +139 Elo in a week).
