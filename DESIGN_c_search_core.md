# Design: C search core (roadmap #29/#30)

**Status:** planning / phase 0. **Date:** 2026-07-08. **Baseline:** v30 (~90k NPS, ~2560 internal Elo).

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
   Deliberate v1 deviations (documented in cengine.py): no root random
   tiebreak, no repetition check at quiescence nodes, no SMP/TB.
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
| v35 | P-44 qsearch TT probe/store (`set_qs_tt`, default on; probe before movegen+eval so a hit skips the node, any stored depth cuts; stores at depth 0 — stand-pat cutoffs as LOWER + resolved nodes by bound — gen-aware so they never displace same-key negamax entries; TT move seeds qsearch ordering; ply-relative mates) | **Isolation A/B vs the P-22 base (engine_qtt_off): +8.06 ±6.8** (10k @ 45+0.1, 51.16%, ptnml 359/1123/1891/1181/446, norm +15.35, pair ratio 1.10) — CI clear of zero; the warm table across a game delivered what the flat cold-ladder time-to-depth couldn't show. Bundle with P-22 directly vs v34: ~+71.8 ±8.5 @ 7,061 games — parts compose (~+64 + ~+8) | **CONFIRMED** 2026-07-10, snapshotted Old Engine/35 (v35 = v34 + P-22 + P-44 ≈ +72, the biggest C-era step) |
| v36-dev | P-23 staged move ordering (`set_staged`, default on; TT-move played via reconstruct+validate `move_from_key` with zero generation, then lazily: captures (SEE-demoted), killer0/1, counter, quiets by history, bad captures — each class generated only when the search reaches it) | VERIFY mode proved stream == order_moves under identical state over ~1M nodes (incl. Kiwipete); **live trees deliberately diverge: quiet stages see FRESHER history than v35's entry snapshot (often fewer nodes, e.g. startpos d10 −19%)**; ~+10–20% NPS; tactics + mate + Kiwipete-d11 pass; `set_staged(0)` = v35 node-exact | **CONFIRMED** 2026-07-10: **+24.67 ±6.8** (10k @ 45+0.1, 53.55%, ptnml 295/998/1911/1295/501, pair ratio 1.39, norm +47.51) vs Old Engine/35 — snapshotted Old Engine/36 (v36 = v35 + P-23 [+P-46 rider]); closes the 45+0.10 era, 50+0.30 from v37 campaigns on |
| queued | Outpost re-test (`cengine.USE_OUTPOST`, default off; P-20a sync mechanism — flips the embedded engine's `use_outpost` before `_sync_c_params` pushes `set_outpost_params` into csearch's eval_c copy) | Bit-exact vs the oracle over 16k positions with both libs synced; Python-era solo verdict +0 ±10 @ depth 8, P-20a subsumption logic tempers expectations | QUEUED for an A/B slot after Q-01 (user request 2026-07-10) |
| queued | Simplify-at-500 re-test (`cengine.USE_SIMPLIFY` + `SIMPLIFY_THRESHOLD=500`, default off; `csearch_set_simplify` — v30's `use_simplify` ported into csearch's eval, classic-value material diff incl. pawns, weight×(14−pieces), NOT applied on the lone-loser path, matching Python) | Bit-exact vs the oracle over 28k positions (9.5k gate-active). v30's 200cp version A/B'd −14 (traded into drawn endings); the ≥500cp gate removes that mode. **Verdict harness: MATCH_ADJUDICATE=0 matches and/or odds-vs-SF conversion play — adjudicated A/Bs barely see it (WDL calls wins in the same cp band)** | QUEUED (user request 2026-07-10) |
| v37-dev | Q-01 continuation history (`set_cont_hist`, default on; quiet ordering += `cont1[prev-move][move]` + `cont2[move-2-back][move]`, piece-to keyed 448×448 int16 tables, per-ply context stack `g_ctx`, same gravity/malus as butterfly history at quiet cutoffs; root context empty + qsearch reads none — documented deviations from v30) | Toggle-off node-exact vs v36 (12-depth ladder); VERIFY mode with cont ON: staged stream == order_moves over ~506k nodes; tactics + mate intact; d10–12 nodes ±4–18% (tree reshapes, A/B decides) | **PENDING A/B vs Old Engine/36 — FIRST 50+0.30-ERA CAMPAIGN** 2026-07-10 |
| v36-dev | P-46 lazy qsearch generation (`set_qs_lazy`, default on; eval + stand-pat BEFORE movegen — the many stand-pat-cutoff nodes skip generation entirely; stalemate exactness kept via an early-exit any-legal-move check, quiet scan first then the noisy list) | **NODE-IDENTICAL on/off** (7 FENs × d6/d10 + ladder natively); speed: middlegame +0.3%, startpos +2.4%, **pawn endgame +27.5%** (eval dominates busy boards and is paid either way; gen is the big slice only on quiet boards) | **KEPT (speed-only, batched)** 2026-07-10 — ~+1–3% aggregate is unresolvable at ±6.8 alone; its Elo rides in the next NPS-batch A/B (with P-23 staged ordering) vs Old Engine/35 |
| — | P-45 TT prefetch (child's TT line prefetched after `apply_move`) | Node-identical; **NULL on BOTH architectures**: Apple Silicon −0.7% median time-to-depth, server x86 +0.6% — modern OoO cores hide the TT latency without help; the extra board_key per move isn't paid back | **NULL, REVERTED** 2026-07-10 (bench-gated, no A/B spent; don't re-try bare prefetch — only worth revisiting bundled with P-27 incremental hashing, where the child key comes free) |
| v35-dev | P-22 noisy-only qsearch generation (`set_qgen`, default on; `gen_noisy` emits gen_legal's exact noisy subset — captures / promos / ep — in the same relative order; stalemate still detected before stand-pat via an early-exit `has_legal_quiet` when the noisy list is empty; in-check path unchanged) | **NODE-IDENTICAL** verified over 8 FENs × d6/d10 (incl. promo-heavy + near-stalemate); **+31.9% NPS** on a 4-position mixed bench (4.03M→5.31M), **+55%** on the startpos 2s probe (2.7M→4.2M) | **KEPT (speed-only)** 2026-07-09 — provably same tree, so no fixed-depth A/B needed. **Timed-play Elo MEASURED 2026-07-10 (as the P-22+P-44 bundle vs Old Engine/34): ~+71.8 ±8.5 @ 7,061 games, stopped as decisive — right in the ~2–3 Elo/1% NPS band for +32%. The biggest single C-era gain (~3× P-14); P-44's share inside it pending the isolation A/B (cengine vs engine_qtt_off). LESSON: "node-identical" exempts only the fixed-depth gate, not the Elo ledger — speed changes get their own timed A/B before the next feature stacks on top** |

## Recommendation

The upside is the single largest remaining lever (the only path to *multiples*
of NPS, and NPS is what converts to Elo here), but it is a genuine
weeks-to-months commitment that forks the codebase into a C engine. Do it
**only** with the GO/NO-GO gate at the end of phase 2 respected — that gate is
cheap (board + eval + a toy alpha-beta) and answers "will the constant factors
actually deliver the 10×?" before committing to the expensive phase 3.

If that appetite isn't there, v30 is a strong place to stop (~2560 internal,
dead-even with full Stockfish at rook odds, +139 Elo in a week).
