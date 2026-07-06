"""
engine.py
=========

A self-contained chess engine. It uses the ``chess`` library only for the
board, move generation and legality checks -- never for evaluation or search,
which are entirely our own.


Search features
---------------
* **Negamax + alpha-beta** core with **Principal Variation Search (PVS)**.
* **Iterative deepening** reusing the previous iteration's PV move, killers,
  history scores and the transposition table. Partial-iteration results are
  preserved: if time runs out mid-depth, the best root move evaluated so far
  is used rather than always falling back to the last completed depth.
* **Aspiration windows** around the previous score for deeper iterations.
* **Transposition table** keyed by the board's internal position key
  (cheap and collision-safe -- see "Early correctness fixes" below) storing
  depth + bound (exact / lower / upper) + best move, with mate-score
  distance correction.
* **Quiescence search** with stand-pat, delta pruning and check evasions,
  plus a *lazy* stand-pat: the expensive positional eval terms are skipped
  when the cheap material+PST base already proves a >= beta cutoff (exact, so
  the search is unchanged).
* **Pruning / selectivity**: null-move pruning, reverse-futility (static
  null-move) pruning, futility pruning at frontier nodes, late-move
  reductions (LMR -- a log(depth)*log(move) reduction table) and late-move
  pruning (LMP -- dropping the quiet tail at shallow non-PV nodes once
  enough quiet moves have been searched without a cutoff).
* **Extensions**: check extension (post-push, draws on its own ``chk_budget``
  so a line full of captures cannot starve it), single-reply / forced-move
  extension, and passed-pawn push extension (5th rank or beyond). All three
  non-check extensions share a single ``ext_budget`` cap. A recapture extension
  also exists (``recapture_ext`` toggle) but is **off by default** -- the
  quiescence search already resolves exchanges at the leaves and extending again
  costs ≈35% more nodes for no measured gain.
* **Move ordering**: TT move, MVV-LVA + **capture history** (learned per
  ``(mover_pt, to_sq, victim_pt)`` triple, same gravity rule as quiet history,
  blended directly into the capture score so equal-MVV-LVA captures are ranked
  by past cutoff experience) captures, promotions, killer moves, the
  counter-move heuristic and the history heuristic (with a history malus that
  penalises quiet moves searched before the cutoff, keeping stale scores from
  dominating ordering), with **Static Exchange Evaluation (SEE)** demoting
  losing captures and pruning them in quiescence.
* **SEE-prune losing captures at frontier nodes**: at depth ≤ 2, non-PV,
  not-in-check, if the fast piece-type pre-filter flags a potentially losing
  capture (mover_pt > victim_pt) and the full SEE confirms SEE < -depth×100,
  the move is skipped entirely (the qsearch will resolve the same exchange at
  no extra tree cost).
* **LMR for losing captures**: captures where the mover's piece type exceeds
  the victim's (a fast proxy for "probably loses material") are reduced by 1
  at depth ≥ 3 once LMR_MIN_MOVE moves have been tried -- smaller reduction
  than quiets to stay conservative on tactics.
* **Static eval proxy in check**: in-check nodes no longer leave the improving
  heuristic blind. A TT-cached eval is used when available; otherwise alpha
  serves as a safe lower-bound proxy. This lets LMR's improving bias track
  through check-evasion sequences instead of treating every check node as
  "not improving".
* **Opening book**: optional Polyglot ``.bin`` book consulted before search,
  with uniformly-random move selection among all book entries for opening variety.
* **Endgame tablebase**: optional online Syzygy probe (the free Lichess
  tablebase API) at the root for positions with few enough pieces. A hit
  returns the provably-optimal move and skips the search entirely; it is never
  queried inside the search. To avoid paying network latency where it buys
  nothing, the probe is skipped for positions the search already nails faster
  than the API responds -- dead-drawn insufficient material and overwhelming
  pawnless mop-up wins (a lone king vs a major piece, e.g. KQK / KRK) -- so the
  tablebase is spent only on genuinely tricky endings (pawns, or a defending
  piece, where the win/draw verdict or technique can actually be wrong). Any
  network error / timeout / illegal response falls back to a normal search, and
  the network wait is bounded by the move's time budget so a miss cannot lose on
  time. Disabled with ``use_tb = False`` for fully offline / benchmark play.
* **Endgame / draws**: an endgame "mop-up" term that drives the weak king to
  the edge to convert won endings (KQK / KRK / KQ-vs-P), and contempt-scored
  repetition detection so a clearly winning side avoids draws while a losing
  side is happy to hold them.

Evaluation
----------
A tapered hand-crafted evaluation (HCE): middlegame and endgame scores blended
by game phase, returned in centipawns from White's view (``_evaluate_stm``
flips it to the side to move for negamax).

Terms:

* Material + piece-square tables + tempo bonus.
* Pawn structure: doubled / isolated / passed / backward pawns.
* King safety: pawn shield, open files, attacker count.
* Mobility, rook on open / semi-open file, bishop pair.

Speed tricks:

* The cheap base half (material + PST + phase + tempo) is kept *incrementally*:
  a per-move delta updates an accumulator on every make/unmake
  (``use_incremental_eval``), so it's never rescanned per node. The result is
  byte-for-byte identical to a from-scratch scan.
* The pawn-structure term depends only on the pawn bitboards and phase, so it
  is memoized in a pawn hash keyed on ``(white pawns, black pawns, phase)``.

Several further eval/search refinements (pin penalty, trade-down
simplification, recapture extension, alternative TT-replacement schemes,
quiescence SEE ordering) exist as off-by-default A/B toggles in ``__init__``;
see the per-flag verdicts there.

Early correctness fixes (pre-v15)
----------------------------------
The earliest version had several issues that ballooned the node count and the
per-node cost. Fixed and flagged inline with ``# FIX``:

* ``chess.polyglot.zobrist_hash(board)`` was called for the TT key at *every*
  node (rebuilds from scratch, ≈50k/s). Switched to
  ``board._transposition_key()`` (≈1.2M/s, ≈22x faster); Zobrist hashing is
  now only used for the book probe. (Unrelated to ``use_zobrist``, added
  later for Lazy SMP -- that's an *incremental* 64-bit hash for the shared
  TT, not a from-scratch rebuild, so it doesn't reintroduce this cost.)
* The root searched every move with a full, un-narrowed window (alpha never
  raised), disabling root pruning. Now uses PVS and raises alpha, while
  still supporting the random tiebreak.
* Move ordering called ``board.gives_check(move)`` for *every* legal move
  (one of python-chess's most expensive calls). Removed from ordering; check
  detection now happens once, cheaply, after the move is pushed.
* PVS, LMR, reverse-futility and futility pruning (claimed but not actually
  present in the original) are implemented, cutting the tree hard.

Version history
---------------
Each version is a saved snapshot in ``Old Engine/<N>/``; only the *changes* are
logged here. For what the current build does, see "Search features" and
"Evaluation" above. Aggregate NPS/Elo numbers live in "Cross-version
benchmark" below.

* **v1**: initial working engine -- negamax + alpha-beta, iterative deepening,
  an inline dict transposition table, quiescence search, null-move pruning,
  killer moves and a material + piece-square-table eval. This is the naive
  baseline the "Early correctness fixes" section above refers to.

* **v2**: the main search + eval build-out. Selectivity added -- PVS,
  reverse-futility (static null-move), futility pruning and LMR -- plus
  aspiration-window root search. New bitboard eval terms: pawn structure,
  mobility, king safety, bishop pair, rook files. History-heuristic updates
  and an optional Polyglot opening book (``use_book``).

* **v3**: endgame + draw handling -- the mop-up term (``_mopup_bb``) that drives
  the weak king to the edge, contempt-aware draw scoring (``_draw_score``) and
  the counter-move heuristic.

* **v4**: Static Exchange Evaluation (``_see``, ``use_see``) for move ordering
  and pruning losing captures.

* **v5**: recapture extension (``_recapture_at``).

* **v6**: endgame eval fix -- lone-loser detection so the king-safety terms are
  no longer dropped in lone-king endings.

* **v7**: pin evaluation (``_pin_penalty_bb``, ``use_pin_eval``).

* **v8**: eval refactor + quiescence stand-pat -- eval split into base /
  positional halves, mobility and king safety merged into one pass
  (``_mobility_king_safety_bb``), quiescence stand-pat (``_qs_stand_pat``),
  trade-down simplification (``use_simplify``) and PV extraction.

* **v9**: late-move pruning (``use_lmp``), the history malus
  (``use_history_malus``) and the "improving" heuristic. NPS drops ~14% by
  design (LMP skips near-leaf quiets) but the search reaches deeper per second.

* **v10**: transposition-table refactor -- probe/store split into ``_tt_get`` /
  ``_tt_store`` with two-tier and depth-preferred replacement variants
  (toggles), plus a quiescence-SEE ordering toggle.

* **v11**: incremental base eval (``use_incremental_eval``) -- material + PST +
  phase + tempo maintained by a per-move delta in ``_make`` / ``_unmake``
  instead of a per-node rescan (byte-identical to the from-scratch scan).

* **v12**: extension budgeting -- a separate check-extension budget and a
  ``MAX_EXTENSIONS`` cap so a capture-heavy line cannot starve the other
  extensions.

* **v13**: eval-weight retune -- bishop pair, rook files, tempo and the
  pawn-structure penalties reset from a tuning run.

* **v14**: online Lichess Syzygy tablebase (``use_tb`` / ``_tb_probe``,
  root-only, triviality-guarded so trivial mop-ups skip the ≈150-400ms round
  trip, network-safe), Internal Iterative Reduction (depth >= 4 with no TT
  move) and a pawn-structure hash keyed on ``(wp, bp, phase)`` (phase-tapered,
  so the naive ``(pawns, occ_white)`` key would be wrong).

* **v15**: pre-C-extension baseline. LMR divisor tuned 2.25 -> 2.0 (~12% more
  reductions; an overnight 5-variant sweep found every value a statistical
  tie -- 2.0 is just the noise-peak). Probcut was tried and removed here (+4
  +/-12 Elo at 1s/move, ≈0 at 500ms -- null at both TCs) -- see "Rejected /
  shelved experiments" below.

* **v16**: ``_mobility_king_safety_bb`` ported to C (``eval_c.c``, loaded via
  ``ctypes``; build ``python3 eval_build.py``). 0/10,000 positions differ
  from the Python path. **NPS 21,369 -> 27,507 (+28.7%)** at fixed depth.

* **v17**: legal + capture move generation ported to C (``movegen.c``; build
  ``python3 movegen_build.py``; toggle ``use_c_movegen``), reproducing
  python-chess's exact pseudo-legal move order so the search stays
  byte-identical (not just set-equal) after ``order_moves``'s stable sort --
  a prior *staged* movegen that reordered quiet ties lost ≈20 Elo, which is
  why order-matching was mandatory here. Perft-verified to depth 6 on the
  full standard suite. **NPS +24.8%** at fixed depth. **v16+v17 combined vs
  v15: +69 +/-16 Elo** (2000 games).

* **v18**: Lazy SMP groundwork -- incremental 64-bit Zobrist hashing
  (``use_zobrist``, maintained in ``_make``/``_unmake``, never rebuilt from
  scratch) so the dict TT's key can eventually live in shared memory (a
  plain tuple key can't, and a tuple's ``hash()`` is per-process-randomised
  anyway). Off by default -- zero overhead in normal play. Verified over 7M
  make/unmake checks (incremental == from-scratch).

* **v19**: Lazy SMP finished -- a lock-free shared-memory transposition table
  (``shared_tt.SharedTT``/``use_shared_tt``, Stockfish-style XOR'd 64-bit
  slots so a torn read is always detected as a miss, never a corrupt hit)
  and multi-process orchestration (``smp.py``, ``self.smp_workers``: N worker
  processes search the root to the same wall budget, diversified by RNG
  seed, deepest-completed result wins). N=4 reaches +1 ply deeper than N=1
  in the same wall time on most positions (≈70-85% efficiency) -- real but
  modest, and only pays off in time-limited (not fixed-depth) play. Also
  folds in the remaining eval/movegen infra: the INBETWEEN_BITBOARDS table,
  magic bitboards for slider attacks in ``eval_c.c``/``movegen.c``, and a
  packed move word (mover/victim piece type + en-passant flag) so the
  search loop skips several python-chess calls per move.

* **v20**: three new eval terms -- ``use_rook_on_7th`` (rook on the 7th vs an
  exposed enemy king/pawn), ``use_mobility_area`` (mobility excludes squares
  attacked by an enemy pawn), ``use_threats`` (bonus per enemy piece attacked
  by a cheaper one of ours) -- plus folding rook-files/bishop-pair into the
  existing mobility/king-safety C call to remove a second ctypes round trip.
  **A/B vs v19: +45 +/-11 Elo** (4000 games, 0.75+0.25 TC).

* **v21**: capture-history move ordering (``use_capt_history``), SEE-pruning
  of losing captures at depth <= 2 (``use_see_prune_captures``), LMR for
  losing captures, and an in-check static-eval proxy (``use_check_eval_proxy``)
  so the improving heuristic isn't blind through check evasions -- plus five
  small NPS wins (a bitboard ``has_non_pawn_material`` check, a longer
  time-check interval, a pre-allocated SEE gain array, a bitboard passed-
  pawn-push check, and direct killer slots). **A/B vs v20: +16 +/-10 Elo**
  (5000 games, 0.65+0.1 TC).

* **v22**: nine correctness bug fixes -- low-phase eval was dropping king
  safety and the post-v21 toggles entirely; the lone-loser mop-up shortcut
  returned 0 instead of falling through below its gate; a false
  ``TT_EXACT`` flag could be stored after an alpha-raise; the shared-TT
  mate-score clamp could overclaim a bound it hadn't proven; a stale
  ``alpha`` could pollute the TT's cached static eval; the null-move child
  mis-keyed 2-ply continuation history; the quiescence lazy-margin (400)
  was measured stale (raised to 700); mate delivered exactly at the
  75-move clock scored as a draw; and the four post-v21 toggles without a
  C implementation lacked a documented Python-fallback caveat -- plus six
  NPS wins (reusing raw capture tags in quiescence, interning ``Move``
  objects, int-packing history-table keys, reusing ordering-time SEE in
  the prune gate, porting SEE itself to C, and reordering the quiet-history
  lookup past the prune checks that might skip it). Not yet A/B'd for Elo.

* **v23**: fixed a Zobrist method-swap bug -- a permanently-off ``if`` guard
  in the hottest functions (``_make``/``_make_null``/``_unmake``) isn't free
  in CPython even when it never fires. Split into branch-free variants
  bound once per search instead of checked every node. No measurable NPS
  effect (within noise).

* **v24**: the same fix applied to the TT dispatch
  (``_tt_get``/``_tt_store``). +0.5-1.36% NPS depending on position (real,
  if small). Both v23 and v24 are kept for code quality regardless.

* **v25 (2026-07-04): the 18-item [BUG] block from improvements_v24.md.**
  Single-thread search is byte-identical to v24 (h1h8/3495 reference) apart
  from the intended fixes (zeitnot budgets, in-search TT cap).
  - setoption changes now reach the C eval (``_sync_c_params``, P-06).
  - UCI ``stop`` race closed via the host-owned ``_abort`` flag (P-05).
  - zeitnot time management: emergency budgets respect overhead, sub-250 ms
    budgets bind a 1024-node poll (P-08).
  - all search timing on monotonic ``perf_counter`` (P-09).
  - TT entry cap enforced inside long searches (P-15).
  - eval_c/movegen ``.so`` ABI handshake + loud fallback (P-12).
  - C-side division / ``ctzll(0)`` guards (C-01/C-02).
  - SMP cluster: pool created on the main thread so production runs
    multi-core with a live ``Threads`` option (P-01); stm-relative tie-break
    (P-02, the old one picked Black's *worst* tie); dead-worker timeouts +
    error rows (P-03); search-id-tagged results (P-04); pool carried across
    ucinewgame (P-07); host config replicated into workers (P-11); workers
    close their shm view (P-13); shared-TT PV walk fixed (X-01).
  - **A/B vs v24** (stopped early at 3,462/10k games, 45+0.15s clock):
    +2.91 +/-11.6, normalized +5.51 -- no regression; validated with a
    1,068-game interim (+3.8) and the SF-2450 absolute benchmark (≈2442).

* **v26 (2026-07-05, current ``engine.py``): the byte-identical NPS batch
  from improvements_v24.md.** Search is node-identical to v25 -- verified per
  item and end-to-end (8-position suite in all four zob x incremental
  configs, 40k+ accumulator round-trips, 56.9k SEE differential, 304-endgame
  oracle, perft). **Measured +18.5% time-to-depth vs v25** (interleaved
  best-of-3 on identical trees; the P-47 eval memo contributes +1.1%).
  **A/B vs v25 (2026-07-06): 10,000 games @ 45+0.15s clock (950 recovered
  from an interrupted first run + 9,050 resumed, disjoint openings):
  4329W/2541D/3130L = 55.99% -> +41.9 +/-5.7 Elo** (ptnml
  424/777/1860/1054/885, pair ratio 1.61, normalized ~+71.5) -- far above
  the +10-18 expected from speed alone; the zeitnot budgets, in-search TT
  cap and eval memo carry real Elo beyond time-to-depth.
  - 19 Python items: ctypes slice decode, qsearch castling-arg drop,
    ``_see_raw``, pre-bound C refs, insufficient-material pre-filter,
    ``PIECE_VALUES`` tuple, raw-tag promo/from-to reads,
    killer/countermove/history flat lists, ``_move_delta`` raw threading +
    ``_contrib`` table, gives_check pass-down, ``order_moves`` row return,
    inlined poll-mask gate, static-eval memo, make/unmake acc de-branch.
  - C group: ``-O3 -mcpu=native``, constructor table init, directional ring
    popcounts, ``attacked()`` slider guards, mopup folded into the C eval
    (=> eval ABI 2).
* **v27 (2026-07-06, current ``engine.py``): the risk-free Python NPS batch
  from improvements_v24.md merge #6.** Search is NODE-IDENTICAL to v26
  (every item gated on the 10-position suite = 148,775 nodes + the
  h1h8/3495 reference), so this is a pure speed refactor -- byte-identical
  play, faster. Items: U-01 (hoist remaining per-move ``self.`` loads + LMR
  clamp-once), W-08 (pm1/pm2 predecessor keys built once per node), U-02
  (passed-pawn-push gated on the raw tag), W-07 (thread the node's TT key
  into ``_evaluate_stm``), Y-03 (qsearch stand-pat shares the P-47 eval
  memo), P-24 (TT/killer/counter identity via 15-bit ints, not
  ``Move.__eq__``), Y-06 (continuation history as ``{pred: flat 8192-list}``
  instead of a 25-bit-keyed dict), W-09 (pawn cache keyed ``(wp, bp)`` with
  a per-call passer taper). W-12 (reuse the ordering history blend) was
  TRIED and REVERTED -- it changed node counts (history tables mutate during
  a node's own move loop), so it's a search-behaviour change for the A/B
  tier, not a free speedup. **Measured +12.0% NPS vs v26** (idle, interleaved
  3x3s over the 10-position suite: v26 78,242 -> v27 87,656 NPS; +0.2 ply avg
  depth, every position faster). Since search is node-identical, this speed
  should convert ~+12 Elo at a clock TC -- A/B vs v26 pending.
  Also folded in (node-identical, verified 2026-07-06 -- perft --deep ALL PASS
  1.49B nodes + the 148,775-node suite exact + h1h8/3495): Z-04 (`_capture_moves`
  returns rows), W-10 (rook open-file scored inside the rook mobility loops --
  one ctz pass), W-14 (one `_npm`/`npm_side` helper replaces the 6 duplicated
  material-value formulas across Python + C), W-15 (`eval_c.c`/`movegen.c` alias
  Constants.c's KNIGHT_ATTACKS/KING_ATTACKS instead of rebuilding them at load).
  Second node-identical batch (verified 2026-07-06, 148,775-node suite + h1h8/
  3495): V-05 (predecessor key computed once in `_negamax`), V-07 (`_capture_moves`
  carries `victim_value` on the row so quiescence doesn't re-decode it), V-08
  (`see_attackers` skips the slider magic lookup when no such slider exists).

Cross-version benchmark
-----------------------
Sweep (2026-07-02): 24 versions x 8 positions x 6 timed 5s runs (1152 searches).

* **NPS +79.2%** v1->v24.
* **Search depth +7.96 ply** (9.98 -> 17.94) -- the cleaner signal, since one
  position hits the depth cap on every version and dilutes the NPS aggregate.
* v9 (LMP) and v18->v19 (Zobrist/shared-TT) dip in NPS but gain depth --
  heavier, smarter search, not a slowdown.
* v16/v17 (C-eval / C-movegen) are the two biggest wins in both metrics.

A/B result (2026-07-04): v24 vs v21, 10,000 games @ ≈8.3 s/game -> **+11.75
+/-6.8 Elo** (51.69%; normalized +22.08; pentanomial 362/1120/1802/1250/466,
pair ratio 1.16). The whole v21->v24 batch is net-positive; NPS moved only
≈+2%, so the gain is mostly the bug fixes (low-phase king safety,
mate-at-clock-100, LAZY_MARGIN).

Strength (absolute, vs Stockfish)
----------------------------------
Latest (measured on v25; v26 AND v27 are search-identical so the figure
carries -- v26/v27 only made the same search faster,
2026-07-05): vs ``stockfish_engine.py`` at Stockfish Elo 2450,
2,500 games @ ≈7.2 s/game (match.py harness, engines single-threaded per its
default SMP override): 886W / 670D / 944L = 48.84% -> **-8.1 +/-13.6 Elo**
(normalized -13.7; pentanomial 172/242/451/242/143, pair ratio 0.93).
Point estimate **≈2442**, CI ≈[2428, 2456] -- statistically level with
Stockfish 2450; call the current single-thread strength **≈2440-2450**.

Earlier SMP benchmark: Stockfish skill ≈2400, engine running
``SMP_WORKERS = 4``: 400 games, 188W / 131D / 81L (63.4%) -> +95 +/-37 Elo
(4h25m, ≈40s/game), i.e. ≈2495 -- consistent with the single-thread figure
above plus the SMP-4 depth gain. This sits on top of v16/v17's
C-eval/C-movegen work (+69 Elo) and v19's optimization-reference work above.
An older 6000-game baseline (≈2341 vs Stockfish 2350) predates the C work
and ran single-threaded, so it's no longer comparable.

NB on PyPy: the old "≈1.5x faster" guidance is STALE. Once v16/v17 moved eval
and move-gen behind ctypes, PyPy's FFI cost erodes its edge -- a properly
warmed PyPy is only ≈+25% over CPython here (and *cold* PyPy is slower, so
short searches favour CPython). PyPy's JIT can't see through the ctypes/
python-chess boundary, which is also why a Cython build and a hand-written
bitboard board layer (both built and measured, never folded in -- see below)
couldn't beat it by much.

Rejected / shelved experiments
-------------------------------
* **Probcut** (v15): +4 +/-12 Elo at 1s/move, ≈0 at 500ms -- null at both
  tested TCs. Removed; design and bug history are in git.
* **Cython search core** (``engine_cy_build.py`` compiles engine.py
  unchanged): warmed PyPy (77.8k NPS) still beats it (71.3k), because the
  hot path is python-chess board ops that PyPy JITs and Cython (external,
  C-API speed) cannot. Kept only as an optional no-warmup CPython build;
  not folded into engine.py.
* **Own bitboard board layer** (``fastboard.py``, a drop-in ``FastBoard``
  for python-chess's Board): python-chess turned out to already be a tight
  pure-Python bitboard engine with O(1) magic attack tables, so the
  "python-chess is naive/object-heavy" premise was largely false. Only +9%
  CPython / ≈parity PyPy. Not integrated into the engine.
* **Outpost** (``use_outpost``): +0 +/-10 Elo (5000 games).
* **Space** (``use_space``): -9 +/-14 Elo (5000 games).
* **Phalanx / connected pawns** (``use_phalanx``): +3 +/-10 Elo (5000 games,
  local only).
* **Pawn storm** (``use_storm``): -5 +/-10 Elo (5000 games).
* **King shelter depth** (``use_king_shelter``): +10 +/-10 Elo solo (5000
  games) -- borderline, so tested combined instead of folded in alone.
* **Combined outpost + phalanx + shelter** (30,000 games vs the v21 base):
  **+5 +/-4 Elo FOR the base** -- i.e. ≈5 Elo weaker combined; individually-
  marginal features didn't compose additively. All five features above stay
  OFF; eval-tuning phase closed 2026-07-02.

Future Improvements (vs. Stockfish)
-----------------------------------
A review of the Stockfish architecture (as documented on the Chessprogramming Wiki)
highlights several advanced features missing from this engine that could yield
Elo or NPS gains. (Note: Outposts, Space, Phalanx, Pawn Storm, and King Shelter
were all tested and kept OFF -- see "Rejected / shelved experiments" above.)

1. Search Enhancements
   - Singular Extensions: Extending search depths when a single move is vastly
     better than all alternatives (not the same as the existing single-reply /
     forced-move extension, which only fires when there is literally one legal
     move; singular extension fires whenever one move's score is significantly
     above all siblings).  Expected +15-30 Elo; medium complexity.
   - Null Move Verification: A shallow verification search before returning a
     null-move cutoff at high depths (depth >= 10–12) to avoid incorrect prunes
     in zugzwang positions.  Low code cost; prevents rare but decisive errors
     in K+P endgames.
   - TT Prefetching: Using CPU instructions (e.g. __builtin_prefetch) to load
     the Transposition Table entry into the CPU cache before it is needed.
     Only relevant once the TT is a C array (the dict TT has no fixed layout
     to prefetch).
   Note: Move Count Based Pruning IS already implemented here as LMP
   (``use_lmp = True``, ``LMP_COUNT = {1:6, 2:10, 3:14}`` -- see above).

2. Evaluation Enhancements (Classical)
   - Material Imbalance Tables: Scoring how specific pieces coordinate (e.g., 
     Bishop pair + Knights vs. Rooks) rather than just summing piece values.
   - Material Hash Table: Caching material evaluations for a large speedup since 
     material changes rarely.

NNUE (why it isn't here, and the ultimate goal)
-----------------------------------------------
A real NNUE is **not** integrated, on purpose:

* Pure-Python inference needs an incremental accumulator plus ≈hundreds of
  multiply-accumulates per node. At the few-tens-of-thousands of nodes/sec this
  interpreter manages, that makes the engine *slower*, not stronger.
* Doing it properly would need a C extension (e.g. a Stockfish binding) --
  which defeats the "from scratch in Python" goal.
* Instead, the hand-crafted eval was expanded (backward pawns, per-piece
  mobility, king-zone attacker counts, rook files, bishop pair, tempo) as the
  practical substitute.

That said, a proper NNUE remains the biggest single upgrade available:
replacing all hand-crafted terms with learned weights could be worth
**+200-300 Elo**.
"""

import ctypes
import json
import math
import operator
import os
import random
import sys
import time
import urllib.parse
import urllib.request

import chess
import chess.polyglot

# --- #8: C evaluation module for mobility + king-safety ------------------- #
_EVAL_C_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'eval_c.so')
try:
    _eval_lib = ctypes.CDLL(_EVAL_C_PATH)
    _eval_lib.set_mobility_params.argtypes = [ctypes.c_int] * 11
    _eval_lib.set_mobility_params.restype = None
    _eval_lib.mobility_king_safety.argtypes = [
        ctypes.c_uint64, ctypes.c_uint64,   # occ_w, occ_b
        ctypes.c_uint64, ctypes.c_uint64,   # knights, bishops
        ctypes.c_uint64, ctypes.c_uint64,   # rooks, queens
        ctypes.c_uint64, ctypes.c_uint64,   # wp, bp
        ctypes.c_int, ctypes.c_int,         # wksq, bksq (-1 if absent)
        ctypes.c_int,                       # phase
    ]
    _eval_lib.mobility_king_safety.restype = ctypes.c_int
    # #2.5: positional_extras = bishop_pair + rook_files + mopup, in one call.
    # set_positional_params keeps the C-side constants in sync with the Python
    # tuner (called once from Engine.__init__, same pattern as the mobility
    # params). `strong != 0` mirrors the lone-loser branch in _eval_positional_white.
    _eval_lib.set_positional_params.argtypes = [ctypes.c_int] * 9
    _eval_lib.set_positional_params.restype = None
    # #3.x: rook on 7th rank. Phased (mg, eg) weights; 0/0 disables on the
    # C side (the toggle that gates the Python fallback also chooses what
    # to pass here).
    _eval_lib.set_rook_on_7th_params.argtypes = [ctypes.c_int, ctypes.c_int]
    _eval_lib.set_rook_on_7th_params.restype = None
    # #3.x: mobility-area toggle (1 = subtract enemy-pawn-attacked squares
    # from each piece's mobility count, 0 = legacy behaviour).
    _eval_lib.set_mobility_area.argtypes = [ctypes.c_int]
    _eval_lib.set_mobility_area.restype = None
    # #3.x: threats (pawn -> enemy non-pawn, minor -> enemy major). 0/0 off.
    _eval_lib.set_threats_params.argtypes = [ctypes.c_int, ctypes.c_int]
    _eval_lib.set_threats_params.restype = None
    # Outpost bonus (knight/bishop on pawn-supported, enemy-pawn-safe sq).
    _eval_lib.set_outpost_params.argtypes = [ctypes.c_int] * 5
    _eval_lib.set_outpost_params.restype = None
    # Space bonus (safe central squares c-f, ranks 2-4/5-7).
    _eval_lib.set_space_params.argtypes = [ctypes.c_int, ctypes.c_int]
    _eval_lib.set_space_params.restype = None
    # Phalanx / connected pawns.
    _eval_lib.set_phalanx_params.argtypes = [ctypes.c_int] * 3
    _eval_lib.set_phalanx_params.restype = None
    # Pawn storm toward enemy king.
    _eval_lib.set_storm_params.argtypes = [ctypes.c_int] * 3
    _eval_lib.set_storm_params.restype = None
    _eval_lib.set_shelter_params.argtypes = [ctypes.c_int] * 3
    _eval_lib.set_shelter_params.restype = None
    _eval_lib.positional_extras.argtypes = [
        ctypes.c_uint64, ctypes.c_uint64,   # knights, bishops
        ctypes.c_uint64, ctypes.c_uint64,   # rooks, queens
        ctypes.c_uint64, ctypes.c_uint64,   # occ_w, occ_b
        ctypes.c_uint64, ctypes.c_uint64,   # wp, bp
        ctypes.c_int, ctypes.c_int,         # wksq, bksq
        ctypes.c_int, ctypes.c_int,         # phase, strong
        ctypes.c_int,                       # include_mopup
    ]
    _eval_lib.positional_extras.restype = ctypes.c_int
    # Roadmap item #15: Static Exchange Evaluation, ported from _see/
    # _see_attackers/_least_valuable_attacker (engine.py) to eval_c.c.
    _eval_lib.see.argtypes = [
        ctypes.c_uint64, ctypes.c_uint64,   # pawns, knights
        ctypes.c_uint64, ctypes.c_uint64,   # bishops, rooks
        ctypes.c_uint64, ctypes.c_uint64,   # queens, kings
        ctypes.c_uint64, ctypes.c_uint64,   # occ_w, occ_b
        ctypes.c_int,                       # turn (1=white is the mover)
        ctypes.c_int, ctypes.c_int,         # from_sq, to_sq
        ctypes.c_int,                       # is_ep
    ]
    _eval_lib.see.restype = ctypes.c_int
    # P-12: ABI handshake. Bump together with abi_version() in eval_c.c on
    # any export-signature or semantics change -- a stale-but-loadable .so
    # must be rejected here, not trusted silently.
    _EVAL_C_ABI = 2      # 2: C-18 folded the low-phase mopup into the C eval
    _eval_lib.abi_version.restype = ctypes.c_int
    if _eval_lib.abi_version() != _EVAL_C_ABI:
        raise OSError(f"eval_c.so ABI {_eval_lib.abi_version()} != expected "
                      f"{_EVAL_C_ABI} (rebuild: python3 eval_build.py)")
    _USE_C_EVAL = True
except (OSError, AttributeError) as _e:
    # OSError: .so missing / unloadable / ABI mismatch. AttributeError: stale
    # build missing an expected symbol (abi_version included). Fall back to
    # Python -- loudly: the old silent fallback hid a ~2x slowdown.
    print(f"[engine] WARNING: eval_c.so unavailable ({_e}); pure-Python eval "
          "fallback is ~2x slower. Rebuild: python3 eval_build.py",
          file=sys.stderr)
    _USE_C_EVAL = False

# P-26: pre-bound C entry points for the two hottest boundary calls (eval and
# SEE) -- one LOAD_GLOBAL per call instead of LOAD_GLOBAL + LOAD_ATTR on the
# CDLL object.
_C_MKS = _eval_lib.mobility_king_safety if _USE_C_EVAL else None
_C_SEE = _eval_lib.see if _USE_C_EVAL else None


# --- #9: C legal move generator -------------------------------------------- #
# Replaces list(board.legal_moves) in order_moves. The C side emits moves in
# python-chess's exact generate_pseudo_legal_moves order (perft-verified to
# depth 6; ordered-equal to board.legal_moves on 76k non-check positions), so
# the swap is byte-identical -- same nodes/scores/move. When the side to move
# is in check, generate_legal returns -1 and we fall back to board.legal_moves
# (python-chess uses a different evasion order there; not worth replicating).
_MOVEGEN_C_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'movegen.so')
try:
    _mg_lib = ctypes.CDLL(_MOVEGEN_C_PATH)
    _mg_lib.generate_legal.argtypes = [
        ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64,
        ctypes.c_uint64, ctypes.c_uint64,   # pawns,knights,bishops,rooks,queens,kings
        ctypes.c_uint64, ctypes.c_uint64,   # occ_w, occ_b
        ctypes.c_int, ctypes.c_int,         # turn, ep (-1 if none)
        ctypes.c_uint64,                    # castling rights
        ctypes.POINTER(ctypes.c_uint32),    # out buffer
    ]
    _mg_lib.generate_legal.restype = ctypes.c_int
    _mg_lib.generate_captures.argtypes = _mg_lib.generate_legal.argtypes
    _mg_lib.generate_captures.restype = ctypes.c_int
    _MG_BUF = (ctypes.c_uint32 * 256)()
    # P-12: ABI handshake -- see the eval_c.so block above, same rules.
    _MOVEGEN_C_ABI = 1
    _mg_lib.abi_version.restype = ctypes.c_int
    if _mg_lib.abi_version() != _MOVEGEN_C_ABI:
        raise OSError(f"movegen.so ABI {_mg_lib.abi_version()} != expected "
                      f"{_MOVEGEN_C_ABI} (rebuild: python3 movegen_build.py)")
    _USE_C_MOVEGEN = True
except (OSError, AttributeError) as _e:
    print(f"[engine] WARNING: movegen.so unavailable ({_e}); python-chess "
          "movegen fallback is much slower. Rebuild: python3 movegen_build.py",
          file=sys.stderr)
    _USE_C_MOVEGEN = False


# Roadmap item #11: chess.Move is an immutable value object here (never
# mutated after construction -- push() only reads its attributes), and the
# same (from, to, promo) combination recurs constantly across the search
# tree, so every move decode below used to allocate a fresh, throwaway
# object for something that's really just a 15-bit key. Intern instead: a
# lazily-filled, direct-indexed table keyed by the move's identity bits
# (from|to<<6|promo<<12, i.e. raw & 0x7FFF -- the same low 15 bits
# MV_SHIFT_MOVER is built on top of). 2**15 slots covers every encodable
# (from, to, promo) triple, including many that are never legal in practice
# (e.g. same-square, off-board promo codes) and so simply stay None forever
# -- no upfront cost, only every DISTINCT move actually seen ever allocates.
_MOVE_CACHE = [None] * (1 << 15)


def _interned_move(key, _Move=chess.Move, _cache=_MOVE_CACHE):
    mv = _cache[key]
    if mv is None:
        mv = _Move(key & 63, (key >> 6) & 63, (key >> 12) & 7 or None)
        _cache[key] = mv
    return mv


def _c_legal_moves(board, _gen=(_mg_lib.generate_legal if _USE_C_MOVEGEN else None),
                   _buf=(_MG_BUF if _USE_C_MOVEGEN else None)):
    """Legal moves from the C generator as ``(moves, raws)`` parallel lists,
    or ``(None, None)`` if the side to move is in check (caller falls back
    to board.legal_moves; the in-check evasion order is left to python-chess).

    # #2.3: each raw int packs the move's mover-PT (bits 15-17), victim-PT
    (bits 18-20, 0 = quiet) and en-passant bit (21) on top of the from/to/
    promo low bits. Callers read those tags directly so the search loop no
    longer needs board.is_capture / board.piece_type_at / is_en_passant per
    move. The shared output buffer is fully decoded before return, so it's
    safe across recursive C calls."""
    n = _gen(board.pawns, board.knights, board.bishops, board.rooks,
             board.queens, board.kings,
             board.occupied_co[True], board.occupied_co[False],
             int(board.turn),
             board.ep_square if board.ep_square is not None else -1,
             board.clean_castling_rights(), _buf)
    if n < 0:
        return None, None
    raws = _buf[:n]        # P-18: one C-level slice, not n ctypes __getitem__s
    moves = [_interned_move(r & 0x7FFF) for r in raws]
    return moves, raws


def _c_capture_moves(board, _gen=(_mg_lib.generate_captures if _USE_C_MOVEGEN else None),
                     _buf=(_MG_BUF if _USE_C_MOVEGEN else None)):
    """Captures + promotions (in _capture_moves' exact order) from C, as
    ``(moves, raws)`` parallel lists. Only called when not in check.
    See _c_legal_moves for the raw-word layout (#2.3)."""
    n = _gen(board.pawns, board.knights, board.bishops, board.rooks,
             board.queens, board.kings,
             board.occupied_co[True], board.occupied_co[False],
             int(board.turn),
             board.ep_square if board.ep_square is not None else -1,
             # C-12: generate_captures never reads its castling argument
             # (captures/promotions only) -- passing 0 skips a python-chess
             # clean_castling_rights() call on every quiescence node.
             0, _buf)
    raws = _buf[:n]        # P-18: one C-level slice, not n ctypes __getitem__s
    moves = [_interned_move(r & 0x7FFF) for r in raws]
    return moves, raws


# #2.3: bit layout shared with movegen.c (MOVE_TAG). Module-level so callers
# can extract without importing constants from a function-local scope.
MV_SHIFT_MOVER = 15
MV_SHIFT_VICTIM = 18
MV_BIT_EP = 1 << 21
MV_MASK_PT = 7              # 3-bit piece-type field


# Roadmap item #12: the history/capt_history/cont_history tables were keyed
# by 3- or 5-element tuples, built and hashed twice per quiet move per node
# (once in order_moves, once again in _negamax's move loop for the LMP/LMR
# `hist` read). Packing each key into a single int lets CPython's small-int
# hash (identity, no work) replace a tuple's allocate-then-hash-every-element.
# Every read AND write site for a given table must use the SAME packer -- a
# width mismatch here would silently collide two different keys into the
# same slot, corrupting the table, not just slowing it down.
def _hist_key(color, frm, to):
    """(color, from, to) -> int. color:1 bit, frm/to: 6 bits each (0-63)."""
    return to | (frm << 6) | (color << 12)


def _capt_hist_key(mover_pt, to_sq, victim_pt):
    """(mover_pt, to_sq, victim_pt) -> int. pt fields: 3 bits (0-7), to: 6 bits."""
    return victim_pt | (to_sq << 3) | (mover_pt << 9)


def _cont_hist_key(pm_from, pm_to, color, frm, to):
    """LEGACY 25-bit packer -- unused since Y-06 split the tables into
    {pm_from|pm_to<<6: flat _hist_key-indexed list}. Kept for reference
    (improvements_v24_code.md cites the layout)."""
    return to | (frm << 6) | (color << 12) | (pm_to << 13) | (pm_from << 19)


class _TimeUp(Exception):
    """Raised inside the search to abort once the time budget is spent."""


# Transposition-table bound flags.
TT_EXACT = 0
TT_LOWER = 1   # fail-high: the true score is >= the stored value
TT_UPPER = 2   # fail-low:  the true score is <= the stored value


# Reused by order_moves' sort: a C-level itemgetter is roughly 2x faster than
# `lambda item: item[0]` because it skips Python frame setup per call.
_FIRST = operator.itemgetter(0)


# --- #13: incremental Zobrist hashing (prerequisite for a shared-memory TT) - #
# A fixed 64-bit position key, maintained move-by-move, so positions can be
# keyed in a lock-free shared array across SMP worker processes (the tuple
# `_transposition_key()` cannot live in shared memory, and per-node
# `polyglot.zobrist_hash` is far too slow). Tables are seeded deterministically
# so every worker process computes identical hashes. Position identity matches
# `_transposition_key`: pieces + side + clean castling rights + ep-IF-legal.
_ZOB_MASK = 0xFFFFFFFFFFFFFFFF
def _build_zobrist():
    rng = random.Random(0xC0FFEE13)          # fixed seed: identical across procs
    psq = [[[rng.getrandbits(64) for _ in range(64)] for _ in range(7)]
           for _ in range(2)]                # psq[color][piece_type 1..6][square]
    castle = [rng.getrandbits(64) for _ in range(4)]   # WK, WQ, BK, BQ
    ep = [rng.getrandbits(64) for _ in range(8)]       # by file
    side = rng.getrandbits(64)               # XOR when black to move
    return psq, castle, ep, side
_ZOB_PSQ, _ZOB_CASTLE, _ZOB_EP, _ZOB_SIDE = _build_zobrist()
# castling_rights bitboard squares -> castle-table index (standard chess)
_ZOB_CR_BITS = ((chess.H1, 0), (chess.A1, 1), (chess.H8, 2), (chess.A8, 3))


# --- #13: Lazy SMP worker count for HEADLESS use -------------------------- #
# This is the canonical place to switch SMP on/off for the engine -- edit the
# integer and every headless caller (match.py, lichess_bot, your own scripts)
# picks it up. The Elo benchmark in the module docstring was measured at
# SMP_WORKERS = 4. Semantics:
#     1   -> OFF (single-threaded search, no worker pool, no shared TT).
#     N>1 -> ON. A persistent pool of N worker processes shares a lock-free
#            shared-memory TT (see smp.py / shared_tt.py). Keep
#            N * parallel_games <= CPU cores or you oversubscribe.
# Per-run overrides, in priority order (highest wins):
#     1. ``Engine.smp_workers`` set programmatically AFTER construction
#        (e.g. ``match.py``'s ENGINE_SMP_OVERRIDE -- see below).
#     2. ``CLAUDECHESS_SMP`` env var (still honoured for shell one-offs).
#     3. This module constant.
# GUIs use a separate persistent pool via CLAUDECHESS_GUI_SMP; see gui.py.
SMP_WORKERS = 4


class Engine:
    """Negamax/PVS engine with TT, quiescence, ID, pruning and an opening book."""

    # ------------------------------------------------------------------ #
    # Material values (centipawns) -- used by MVV-LVA / delta pruning.
    # ------------------------------------------------------------------ #
    # X-07: tuple indexed by piece type (1-6; slot 0 unused) -- C-speed tuple
    # indexing in the per-move ordering/SEE/delta loops instead of dict
    # hashing. Not a UCI tunable (material tuning goes through MG_/EG_VALUES).
    PIECE_VALUES = (0, 100, 320, 330, 500, 900, 20000)


    # ------------------------------------------------------------------ #
    # Piece-square tables (PeSTO) -- separate middlegame / endgame tables
    # blended by game phase (tapered eval). Written from a8..h1 for WHITE;
    # white pieces look up table[square_mirror(sq)], black look up table[sq].
    # ------------------------------------------------------------------ #
    MG_PAWN_TABLE = [
            0,   0,   0,   0,   0,   0,  0,   0,
            98, 134,  61,  95,  68, 126, 34, -11,
            -6,   7,  26,  31,  65,  56, 25, -20,
            -14,  13,   6,  21,  23,  12, 17, -23,
            -27,  -2,  -5,  12,  17,   6, 10, -25,
            -26,  -4,  -4, -10,   3,   3, 33, -12,
            -35,  -1, -20, -23, -15,  24, 38, -22,
            0,   0,   0,   0,   0,   0,  0,   0,
    ]
    EG_PAWN_TABLE = [
            0,   0,   0,   0,   0,   0,   0,   0,
            178, 173, 158, 134, 147, 132, 165, 187,
            94, 100,  85,  67,  56,  53,  82,  84,
            32,  24,  13,   5,  -2,   4,  17,  17,
            13,   9,  -3,  -7,  -7,  -8,   3,  -1,
            4,   7,  -6,   1,   0,  -5,  -1,  -8,
            13,   8,   8,  10,  13,   0,   2,  -7,
            0,   0,   0,   0,   0,   0,   0,   0,
    ]
    MG_KNIGHT_TABLE = [
            -167, -89, -34, -49,  61, -97, -15, -107,
            -73, -41,  72,  36,  23,  62,   7,  -17,
            -47,  60,  37,  65,  84, 129,  73,   44,
            -9,  17,  19,  53,  37,  69,  18,   22,
            -13,   4,  16,  13,  28,  19,  21,   -8,
            -23,  -9,  12,  10,  19,  17,  25,  -16,
            -29, -53, -12,  -3,  -1,  18, -14,  -19,
            -105, -21, -58, -33, -17, -28, -19,  -23,
    ]
    EG_KNIGHT_TABLE = [
            -58, -38, -13, -28, -31, -27, -63, -99,
            -25,  -8, -25,  -2,  -9, -25, -24, -52,
            -24, -20,  10,   9,  -1,  -9, -19, -41,
            -17,   3,  22,  22,  22,  11,   8, -18,
            -18,  -6,  16,  25,  16,  17,   4, -18,
            -23,  -3,  -1,  15,  10,  -3, -20, -22,
            -42, -20, -10,  -5,  -2, -20, -23, -44,
            -29, -51, -23, -15, -22, -18, -50, -64,
    ]
    MG_BISHOP_TABLE = [
            -29,   4, -82, -37, -25, -42,   7,  -8,
            -26,  16, -18, -13,  30,  59,  18, -47,
            -16,  37,  43,  40,  35,  50,  37,  -2,
            -4,   5,  19,  50,  37,  37,   7,  -2,
            -6,  13,  13,  26,  34,  12,  10,   4,
            0,  15,  15,  15,  14,  27,  18,  10,
            4,  15,  16,   0,   7,  21,  33,   1,
            -33,  -3, -14, -21, -13, -12, -39, -21,
    ]
    EG_BISHOP_TABLE = [
            -14, -21, -11,  -8, -7,  -9, -17, -24,
            -8,  -4,   7, -12, -3, -13,  -4, -14,
            2,  -8,   0,  -1, -2,   6,   0,   4,
            -3,   9,  12,   9, 14,  10,   3,   2,
            -6,   3,  13,  19,  7,  10,  -3,  -9,
            -12,  -3,   8,  10, 13,   3,  -7, -15,
            -14, -18,  -7,  -1,  4,  -9, -15, -27,
            -23,  -9, -23,  -5, -9, -16,  -5, -17,
    ]
    MG_ROOK_TABLE = [
            32,  42,  32,  51, 63,  9,  31,  43,
            27,  32,  58,  62, 80, 67,  26,  44,
            -5,  19,  26,  36, 17, 45,  61,  16,
            -24, -11,   7,  26, 24, 35,  -8, -20,
            -36, -26, -12,  -1,  9, -7,   6, -23,
            -45, -25, -16, -17,  3,  0,  -5, -33,
            -44, -16, -20,  -9, -1, 11,  -6, -71,
            -19, -13,   1,  17, 16,  7, -37, -26,
    ]
    EG_ROOK_TABLE = [
           13, 10, 18, 15, 12,  12,   8,   5,
            11, 13, 13, 11, -3,   3,   8,   3,
            7,  7,  7,  5,  4,  -3,  -5,  -3,
            4,  3, 13,  1,  2,   1,  -1,   2,
            3,  5,  8,  4, -5,  -6,  -8, -11,
            -4,  0, -5, -1, -7, -12,  -8, -16,
            -6, -6,  0,  2, -9,  -9, -11,  -3,
            -9,  2,  3, -1, -5, -13,   4, -20,
    ]
    MG_QUEEN_TABLE = [
            -28,   0,  29,  12,  59,  44,  43,  45,
            -24, -39,  -5,   1, -16,  57,  28,  54,
            -13, -17,   7,   8,  29,  56,  47,  57,
            -27, -27, -16, -16,  -1,  17,  -2,   1,
            -9, -26,  -9, -10,  -2,  -4,   3,  -3,
            -14,   2, -11,  -2,  -5,   2,  14,   5,
            -35,  -8,  11,   2,   8,  15,  -3,   1,
            -1, -18,  -9,  10, -15, -25, -31, -50,
    ]
    EG_QUEEN_TABLE = [
            -9,  22,  22,  27,  27,  19,  10,  20,
            -17,  20,  32,  41,  58,  25,  30,   0,
            -20,   6,   9,  49,  47,  35,  19,   9,
            3,  22,  24,  45,  57,  40,  57,  36,
            -18,  28,  19,  47,  31,  34,  39,  23,
            -16, -27,  15,   6,   9,  17,  10,   5,
            -22, -23, -30, -16, -16, -23, -36, -32,
            -33, -28, -22, -43,  -5, -32, -20, -41,
    ]
    MG_KING_TABLE = [
            -65,  23,  16, -15, -56, -34,   2,  13,
            29,  -1, -20,  -7,  -8,  -4, -38, -29,
            -9,  24,   2, -16, -20,   6,  22, -22,
            -17, -20, -12, -27, -30, -25, -14, -36,
            -49,  -1, -27, -39, -46, -44, -33, -51,
            -14, -14, -22, -46, -44, -30, -15, -27,
            1,   7,  -8, -64, -43, -16,   9,   8,
            -15,  36,  12, -54,   8, -28,  24,  14,
    ]
    EG_KING_TABLE = [
            -74, -35, -18, -18, -11,  15,   4, -17,
            -12,  17,  14,  17,  17,  38,  23,  11,
            10,  17,  23,  15,  20,  45,  44,  13,
            -8,  22,  24,  27,  26,  33,  26,   3,
            -18,  -4,  21,  24,  27,  23,   9, -11,
            -19,  -3,  11,  21,  23,  16,   7,  -9,
            -27, -11,   4,  13,  14,   4,  -5, -17,
            -53, -34, -21, -11, -28, -14, -24, -43
    ]

    # Material values -- tuned on lichess-big3-resolved.book (7.15M WDL positions).
    # EG values are notably higher than MG for minor pieces, reflecting their
    # increased relative value as the board empties.
    MG_VALUES = {
        chess.PAWN: 89, chess.KNIGHT: 353, chess.BISHOP: 356,
        chess.ROOK: 489, chess.QUEEN: 1148, chess.KING: 0,
    }
    EG_VALUES = {
        chess.PAWN: 108, chess.KNIGHT: 335, chess.BISHOP: 328,
        chess.ROOK: 570, chess.QUEEN: 1020, chess.KING: 0,
    }
    # Game-phase contribution of each piece type. The maximum (full opening
    # material) is 24, used to blend the middlegame and endgame scores.
    PHASE_WEIGHTS = {
        chess.PAWN: 0, chess.KNIGHT: 1, chess.BISHOP: 1,
        chess.ROOK: 2, chess.QUEEN: 4, chess.KING: 0,
    }
    PHASE_MAX = 24

    # ------------------------------------------------------------------ #
    # Search bookkeeping / scoring constants
    # ------------------------------------------------------------------ #
    MATE_SCORE = 1_000_000
    MATE_THRESHOLD = MATE_SCORE - 1_000     # scores above this represent mate
    MAX_PLY = 100                            # hard recursion safety cap
    TT_MAX_ENTRIES = 1_500_000               # cap persisted TT memory (~few hundred MB)
    INF = 10_000_000                         # finite "infinity" for windows

    NULL_MOVE_R = 2                          # base null-move reduction
    LMR_MIN_MOVE = 3                         # first move index eligible for LMR
    MAX_EXTENSIONS = 5                      # non-check extension plies per line
    # Check extensions get their OWN budget so a line full of recaptures can't
    # starve them -- that was why long checking/mating sequences (especially in
    # the endgame) stopped being extended. Generous, but still capped (and
    # MAX_PLY caps total recursion) so perpetual-check trees can't explode.
    MAX_CHECK_EXT = 5

    ASPIRATION_MIN_DEPTH = 4                 # use aspiration from this depth
    ASPIRATION_DELTA = 30                    # initial half-window (centipawns)

    # Pruning margins (centipawns).
    FUTILITY_MARGIN = {1: 150, 2: 320}       # frontier futility by depth
    RFP_MARGIN = 90                          # reverse-futility margin per depth
    DELTA_MARGIN = 120                       # quiescence delta-pruning safety
    # Lazy-eval margin: a safe upper bound on the magnitude of the positional
    # terms (pawn structure / mobility / king safety / mop-up / pins / threats).
    # Bug fix (roadmap item #7): the old value (400, "measured max ~320") was
    # stale -- it predates use_threats/use_mobility_area being added and
    # turned on by default. Re-measured 2026-07-02 via 3800+ random-playout
    # positions plus hand-crafted stress cases (stacked passed pawns, forks,
    # exposed kings): observed max ~450 with those terms live, i.e. the old
    # margin was already being exceeded in practice, occasionally letting the
    # quiescence stand-pat return a wrong >= beta cutoff. 700 gives real
    # headroom (~1.5x the observed tail) rather than just clearing it.
    # The quiescence stand-pat skips the expensive terms when the cheap
    # material+PST base alone already proves a >= beta cutoff by this margin --
    # which is exact (assuming the bound holds), so the search stays
    # byte-for-byte unchanged; costs a little NPS (fewer lazy exits).
    LAZY_MARGIN = 700

    # Random tiebreak: among root moves within this many centipawns of the
    # best, one is chosen at random. Keeps equal positions from cycling
    # without ever preferring a measurably worse move.
    TIEBREAK_MARGIN = 5

    # Move-ordering score for a capture that SEE proves loses material. Placed
    # *below* the killer (800_000) and counter-move (780_000) bands so a losing
    # capture is tried after those quiet refutations, but still above the quiet
    # history scores. The (negative) SEE value is added so worse captures sort
    # lower among themselves.
    SEE_LOSING_CAPTURE = 700_000

    # ------------------------------------------------------------------ #
    # Evaluation weights (centipawns)
    # Tuned via coordinate descent on lichess-big3-resolved.book (7.15M WDL
    # positions, K=122).  Values below reflect a selective merge: material,
    # pawn-structure penalties, king-safety MG/EG split, and bishop-pair EG
    # were all taken from the tuner.  PASSED_PAWN_MG and MOBILITY_KNIGHT were
    # kept at hand-tuned values because the tuner collapsed them to
    # chess-senseless lows (WDL signal is weak for those terms in busy MG
    # positions).
    #
    # Bayesian game-based tuning (chess-tuning-tools, Jun 2026): ran ~500
    # iterations (100 games each, 5+0.05 TC) on 8 structural params.  GP
    # converged to an optimum of ~+12 Elo but CI straddled 0.  Direct match
    # (1000 games) showed candidate -8 ±22 Elo vs current values — no
    # improvement confirmed.  Current values retained.
    # ------------------------------------------------------------------ #
    # Bishop pair bonus: worth more in the endgame (fewer pieces to block diags).
    BISHOP_PAIR_MG = 32
    BISHOP_PAIR_EG = 55
    ROOK_OPEN_FILE = 17
    ROOK_SEMIOPEN_FILE = 11
    # #3.x: rook-on-7th. Tapered (mg, eg); bonus applies per rook on the
    # side's 7th rank when the enemy king sits on its back rank OR an
    # enemy pawn still sits on its 7th. EG > MG because the active rook on
    # 7th is most lethal once minor pieces are off and the back rank can't
    # easily be defended.
    ROOK_ON_7TH_MG = 18
    ROOK_ON_7TH_EG = 32
    # Tempo is high by convention -- WDL tuning consistently finds 15-20 optimal.
    TEMPO = 20

    DOUBLED_PAWN = 13
    ISOLATED_PAWN = 10
    BACKWARD_PAWN = 10
    # Penalty for a piece pinned (absolutely, to its own king): it cannot move
    # off the pin line, so its real mobility/usefulness is far below what the
    # raw mobility term credits it, and it is a standing tactical target.
    PIN_PENALTY = {
        chess.PAWN: 6, chess.KNIGHT: 14, chess.BISHOP: 14,
        chess.ROOK: 18, chess.QUEEN: 22, chess.KING: 0,
    }
    # Passed-pawn bonus indexed by the pawn's rank *from its own side* (0..7).
    # MG table is hand-tuned (tuner collapsed ranks 5-7 to ~20, which is wrong).
    # EG table is tuner output -- back-rank passers are heavily rewarded.
    PASSED_PAWN_MG = [0, 10, 17, 25, 40, 65, 105, 0]
    PASSED_PAWN_EG = [0, 1, 6, 31, 45, 45, 45, 0]

    # Per-piece mobility weight (centipawns per reachable square).
    # Knight kept at 4 -- tuner found 1, but WDL signal for knight mobility is
    # thin in the middlegame; 4 is consistent with other HCE engines.
    MOBILITY_WEIGHT = {
        chess.KNIGHT: 4, chess.BISHOP: 3, chess.ROOK: 2, chess.QUEEN: 1,
    }
    # King-safety MG/EG split: attack/shield matter in MG only; in EG the king
    # should be active, so EG penalties are near zero.
    KING_RING_ATTACK_MG = 13
    KING_RING_ATTACK_EG = 0
    KING_SHIELD_MG = 5
    KING_SHIELD_EG = 2
    KING_OPEN_FILE_MG = 28
    KING_OPEN_FILE_EG = 2

    # Endgame "mop-up": when one side has a decisive non-pawn material edge in
    # an endgame, drive the weak king toward a corner and march the strong king
    # in. Pure guidance (well under a pawn) so it never overrides real material;
    # it just breaks the eval ties that previously let KQK / KRK / KQK-vs-P
    # shuffle without making progress.
    MOPUP_MIN_ADV = 400          # min non-pawn material edge (cp) to engage
    MOPUP_CMD_WEIGHT = 12        # x weak-king centre-distance (0..6) -> push to edge
    MOPUP_KING_WEIGHT = 5        # x king closeness (0..~13) -> bring our king up
    # Strong weights for the bare-king mating eval: the king-driving gradient
    # must dominate the search (and the noise of extra winning material) so the
    # engine actually corners the king instead of shuffling. Kept well under a
    # minor piece so it never tempts sacrificing real material for "mop-up shape".
    MOPUP_STRONG_CMD_WEIGHT = 32
    MOPUP_STRONG_KING_WEIGHT = 16

    # "Trade down when ahead": once a side leads by a clear margin it should
    # exchange pieces (heading for a won endgame). SIMPLIFY_WEIGHT cp is handed
    # to the leader for every minor/major piece already off the board, but only
    # when the material lead is at least SIMPLIFY_THRESHOLD. Pure guidance (well
    # under a piece) so it never tempts a real material sacrifice.
    SIMPLIFY_THRESHOLD = 200       # min material lead (cp) to engage
    SIMPLIFY_WEIGHT = 10           # cp per piece already traded off

    # Contempt: avoid draws (repetitions / 50-move) while clearly winning and
    # accept them while clearly losing. Only applied when the side to move is
    # ahead / behind by at least DRAW_AVOID_MARGIN centipawns of material, so a
    # balanced position still scores a draw as 0.
    CONTEMPT = 50
    DRAW_AVOID_MARGIN = 200

    def __init__(self):
        # Middlegame / endgame piece-square tables keyed by piece type.
        self.mg_tables = {
            chess.PAWN: self.MG_PAWN_TABLE,
            chess.KNIGHT: self.MG_KNIGHT_TABLE,
            chess.BISHOP: self.MG_BISHOP_TABLE,
            chess.ROOK: self.MG_ROOK_TABLE,
            chess.QUEEN: self.MG_QUEEN_TABLE,
            chess.KING: self.MG_KING_TABLE,
        }
        self.eg_tables = {
            chess.PAWN: self.EG_PAWN_TABLE,
            chess.KNIGHT: self.EG_KNIGHT_TABLE,
            chess.BISHOP: self.EG_BISHOP_TABLE,
            chess.ROOK: self.EG_ROOK_TABLE,
            chess.QUEEN: self.EG_QUEEN_TABLE,
            chess.KING: self.EG_KING_TABLE,
        }

        # --- Precomputed pawn-evaluation masks (built once) ------------- #
        # These turn the per-pawn structure tests (isolated / backward /
        # passed) into O(1) bitboard intersections in the hot evaluation path.
        self._build_pawn_masks()

        # --- Search results, exposed to the UI after each move ---------- #
        self.nodes_searched = 0
        self.last_score = 0
        self.last_depth = 0
        self.last_pv = ""           # principal variation of the last search
        self.pv_uci = True         # PV format: False = SAN (Nf3), True = UCI (g1f3)

        # --- Internal search bookkeeping (reset every search) ----------- #
        self.nodes = 0
        self.tt = {}                # pos-key -> (depth, flag, value, best_move, static_eval, gen)
        # P-22: ply-indexed lists, not dicts -- ply is a small dense int, so
        # every probe/store is C-speed list indexing instead of hashing.
        self.killer_0 = [None] * (self.MAX_PLY + 2)   # ply -> most-recent killer
        self.killer_1 = [None] * (self.MAX_PLY + 2)   # ply -> second killer
        self._see_gain = [0] * 32   # pre-alloc; SEE is non-recursive so safe to reuse
        # X-05: _hist_key packs into 13 bits -> a flat 8192-slot list makes
        # every probe/store C-speed indexing with a free 0 default.
        self.history = [0] * 8192   # _hist_key(color, from, to) -> score
        # P-23: indexed by (prev_from | prev_to << 6) -- a 4096-slot list kills
        # both the per-node tuple allocation and the dict hashing.
        self.countermoves = [None] * 4096
        self.start_time = 0.0
        self.time_limit = None
        # Host-requested abort (uci.py `stop`). Set by the host thread, polled
        # in _check_time, and NEVER reset by the engine itself -- the host
        # clears it before starting the next search. That ownership rule is
        # what closes the stop-vs-go race: a time_limit poke would be
        # overwritten by a search that hasn't armed its clock yet, this flag
        # survives.
        self._abort = False
        # P-21: node mask for the inlined time-poll gate at the two
        # `self.nodes += 1` sites; kept in sync with the bound _check_time
        # variant at the clock-arm site in _search.
        self._poll_mask = 4095
        # Best root move seen so far in the current (possibly incomplete) ID
        # iteration. Updated after each fully-evaluated root move in _search_root
        # so that if _TimeUp fires mid-iteration we can still use the partial
        # result rather than falling back to the previous depth's move.
        self._partial_root_move = None

        # Incremental-eval accumulator (material + PST + phase, White's view, raw
        # uncapped phase). Maintained move-by-move via _make/_unmake instead of
        # the from-scratch scan loop in _eval_base_white. `_acc_valid` is True
        # only while a search is maintaining it; external evaluate_position calls
        # (acc not maintained) fall back to the scan loop. See use_incremental_eval.
        # #1.2: the accumulator is a length-3 *list* `[mg, eg, phase]` mutated
        # in place across _make / _unmake. The stack is a flat list of ints
        # (extended/popped 3 at a time) so a push/pop costs no tuple alloc and
        # only relies on CPython's overallocated list growth.
        self._acc = [0, 0, 0]
        self._acc_stack = []
        self._root_acc = [0, 0, 0]
        self._acc_valid = False

        # #1.3: per-ply static eval stack. `_eval_stack[ply]` holds the
        # static eval used at that ply (None when in check or PV-skipped).
        # The "improving" heuristic compares to ply-2: if the side to move
        # is statically better than it was two plies ago we treat the
        # position as on a positive trajectory -- prune more aggressively
        # (RFP, futility) and reduce more (LMR) when it's the opposite.
        self._eval_stack = [None] * (self.MAX_PLY + 2)

        # #1.6: continuation history. Two tables score quiet moves by how
        # well they followed the move played 1 / 2 plies earlier in the
        # line. Used both for move ordering (via order_moves) and as an
        # additive LMR/LMP bias on top of the per-(color, from, to)
        # `history` table. _move_stack[ply] holds the move PLAYED at that
        # ply so deeper plies can look back to ply-2 for cont_history_2.
        # Both dicts share `_update_cont_history`'s gravity rule, so scores
        # stay clamped to |HISTORY_MAX|.
        self.cont_history = {}    # Y-06: {pm_from|pm_to<<6: [0]*8192 flat list
        #                             indexed by _hist_key(color, frm, to)}
        self.cont_history_2 = {}  # same shape, keyed by the move from ply-2
        self._move_stack = [None] * (self.MAX_PLY + 2)

        # #13: incremental Zobrist (maintained only while use_zobrist is on, for
        # the SMP shared TT). Off by default -> zero overhead in normal play.
        self.use_zobrist = False
        self._zob = 0
        self._root_zob = 0
        self._zob_stack = []
        self._zob_valid = False

        # #13 Phase 2: lock-free shared-memory TT (set use_shared_tt + attach a
        # SharedTT to _shared_tt; implies use_zobrist). Off by default -> the
        # normal in-process dict TT is used.
        self.use_shared_tt = False
        self._shared_tt = None

        # #13 Phase 3: Lazy SMP worker count for HEADLESS use. 1 = OFF (normal
        # single-threaded search); >=2 = ON (get_best_move_timed spawns that many
        # workers sharing a lock-free TT, smp.search_smp). Reads CLAUDECHESS_SMP
        # env var first; falls back to the SMP_WORKERS module constant (currently
        # 4). So without the env var the engine defaults to 4 workers (ON). For a
        # GUI use a persistent smp.SMPPool via _smp_pool below, NOT this flag. Keep
        # smp_workers * parallel_games <= CPU cores to avoid oversubscription.
        try:
            self.smp_workers = max(1, int(os.environ.get("CLAUDECHESS_SMP", str(SMP_WORKERS))))
        except ValueError:
            self.smp_workers = SMP_WORKERS

        # #13: a persistent smp.SMPPool for INTERACTIVE use (GUIs). When set,
        # get_best_move_timed routes through it -- the workers already run, so it
        # never spawns at search time (safe from a GUI background thread, unlike
        # the per-move smp_workers path). Created once on the main thread at GUI
        # startup; None everywhere else (headless is unaffected).
        self._smp_pool = None

        # #3 Pawn-structure hash. _pawn_structure_bb is a *pure* function of
        # (wp, bp, phase), so its result is memoized on exactly that tuple --
        # the only correct key (the passed-pawn bonus is phase-tapered, so a
        # pawns-only key would be wrong). Pawn positions change in only a small
        # fraction of nodes, giving a high hit rate. The cache persists across
        # moves and searches (the function never depends on search state) and is
        # cleared wholesale once it exceeds PAWN_CACHE_MAX to bound memory.
        self._pawn_cache = {}
        self.PAWN_CACHE_MAX = 200_000
        # P-47: full-position static-eval memo (stm-relative), persists across
        # moves like the pawn cache; also serves quiescence stand-pat, which
        # the TT's cached-eval reuse never reached. Cleared wholesale at the
        # size cap and by _sync_c_params on any eval-param change.
        self._eval_memo = {}
        self.EVAL_MEMO_MAX = 200_000

        # Static Exchange Evaluation toggle. Used both at search time and by the
        # benchmark, which flips it off to measure the with/without-SEE delta on
        # the same code path.
        self.use_see = True

        # Late-move-reduction mode. When True, reductions are drawn from the
        # log(depth)*log(move_index) table below (more aggressive at deeper
        # plies / later moves, which buys search depth); when False the original
        # fixed 1/2-ply scheme is used. Benchmarked via bench_nps.py at ~-32%
        # nodes for the same fixed depth with no tactical regression, so it is
        # on by default; the flag stays for A/B comparisons.
        self.lmr_aggressive = True
        self._lmr_table = [[0] * 64 for _ in range(64)]
        for d in range(1, 64):
            for m in range(1, 64):
                self._lmr_table[d][m] = int(0.75 + math.log(d) * math.log(m) / 2.0)

        # Capture-sequence (recapture) extension mode -- searches an unresolved
        # exchange one ply deeper so the engine does not mis-judge a capture by
        # stopping in the middle of a trade:
        #   'off' - never extend recaptures
        #   'all' - extend every same-square recapture
        #   'see' - extend only sound recaptures (SEE >= 0), keeping the
        #           extension limited to genuine exchanges (the "limit it" form).
        # Benchmarked (bench_nps.py): the quiescence search already resolves
        # capture sequences at the leaves, so extending them again in the main
        # search cost ~35% more nodes (on top of LMR) for no tactical gain --
        # WAC solve-rate and fixed-depth tactic scores were identical for all
        # three modes. So it is 'off' by default; the flag stays for A/B / game
        # testing, where match results are the final arbiter. (~doubles nodes on
        # tactical positions when 'all' -> 'off' nearly halves the tree.)
        self.recapture_ext = 'off'

        # Penalise absolutely-pinned pieces in the evaluation (see
        # _pin_penalty_bb). A/B over 800 games: pin-ON scored 49.5% vs pin-OFF
        # (-3.5 Elo, within noise) while costing ~6% nps -- so it is OFF by
        # default. Flag kept for future A/B if the term is improved.
        self.use_pin_eval = False

        # "Trade down when ahead" eval term (see SIMPLIFY_*). A/B verdict: ON
        # scored 47.9% (-14 Elo) over 800 games @0.8s -> it HURTS (likely trades
        # into drawn endings), so OFF. (A noisy 100-game/0.15s run had said +;
        # the full match overturned it.) Flag kept if the term is reworked.
        self.use_simplify = False

        # #3.x: rook on 7th rank. Phased bonus per rook on the side's 7th
        # rank, gated by "is the rook actually doing something" -- enemy
        # king on its back rank OR enemy pawn still on its 7th. Default ON
        # behind a toggle; set False (and rebuild won't be needed) to A/B.
        # Disabling it also passes 0/0 to the C side so the inline rook-on-7th
        # block in mobility_king_safety becomes one branch.
        self.use_rook_on_7th = True

        # #3.x: mobility area. When True, every piece's mobility count
        # subtracts squares attacked by an enemy pawn -- a knight stepping
        # onto a pawn-attacked square isn't really mobile, it's lost. Same
        # mobility weights as before (no retune yet); subtle eval shift,
        # principled and standard. A/B via this toggle.
        self.use_mobility_area = True

        # #3.x: threats. Two coarse classes (cheap, big signal):
        #   pawn  -> any enemy non-pawn piece sitting on a square attacked
        #            by one of our pawns (the classic "pawn fork")
        #   minor -> enemy rook / queen attacked by one of our minors
        # Bonus is per threatened piece; tuning these is the natural next
        # offline A/B (defaults are conservative middle-ground values).
        # Disabling the toggle passes 0/0 to C so the whole threats block
        # collapses to one branch.
        self.use_threats = True
        self.THREAT_PAWN = 35       # cp per pawn-fork target
        self.THREAT_MINOR = 25      # cp per minor-attacks-major

        # Outpost: bonus for a knight or bishop on a square supported by a
        # friendly pawn and unreachable by any enemy pawn (no enemy pawn on
        # adjacent files ahead). Requires being on rank 5+ for white / rank 4-
        # for black. Tapered MG/EG per piece type. OFF by default; toggle ON to
        # A/B independently. Passed to C via set_outpost_params.
        # CAUTION (roadmap bug #9): C-ONLY -- no Python fallback exists. If
        # eval_c.so is ever missing/fails to load (_USE_C_EVAL False), turning
        # this on is a silent no-op instead of falling back with identical
        # behaviour like every other eval toggle. Currently moot since this
        # defaults off, but check _USE_C_EVAL before relying on it.
        self.use_outpost = False
        self.OUTPOST_N_MG = 35      # knight outpost middlegame bonus (cp)
        self.OUTPOST_N_EG = 15      # knight outpost endgame bonus (cp)
        self.OUTPOST_B_MG = 18      # bishop outpost middlegame bonus (cp)
        self.OUTPOST_B_EG = 8       # bishop outpost endgame bonus (cp)

        # Space: bonus for safe central squares (c-f files, ranks 2-4 for
        # white / ranks 5-7 for black) not attacked by an enemy pawn and not
        # occupied by a friendly pawn. Tapered by phase so it fades to zero
        # in the endgame. OFF by default. Passed to C via set_space_params.
        # CAUTION (roadmap bug #9): C-ONLY, no Python fallback -- see the
        # use_outpost note above, same caveat applies here.
        self.use_space = False
        self.SPACE_MG = 4           # cp per safe central square at full phase

        # Phalanx / connected pawns: bonus per pawn that is either side-by-side
        # with a friendly pawn on the same rank (phalanx) or defended by one
        # from behind (supported). Verifiable at shallow depth so reliable at
        # depth 7-9. OFF by default; toggle via engine_phalanx.py wrapper.
        # CAUTION (roadmap bug #9): C-ONLY, no Python fallback -- see the
        # use_outpost note above, same caveat applies here.
        self.use_phalanx = False
        self.PHALANX_MG = 10        # cp per connected pawn, middlegame
        self.PHALANX_EG = 5         # cp per connected pawn, endgame

        # Pawn storm: bonus for friendly pawns that have crossed the midline on
        # the three files centred on the enemy king's file (ranks 5-7 for white,
        # ranks 2-4 for black). Pure MG term (EG=0) — attacking pawn advances
        # only matter while pieces are on the board. OFF by default; toggle via
        # engine_storm.py wrapper for A/B. Passed to C via set_storm_params.
        # CAUTION (roadmap bug #9): C-ONLY, no Python fallback -- see the
        # use_outpost note above, same caveat applies here.
        self.use_storm = False
        self.STORM_MG = 12          # cp per storm pawn, middlegame
        self.STORM_EG = 0           # cp per storm pawn, endgame

        # King shelter depth: more granular pawn shield. Replaces the legacy
        # flat "popcount(king_ring & own_pieces) * KING_SHIELD_MG" with a
        # per-file, per-distance assessment for the three files around the king:
        #   SHELTER_CLOSE — cp bonus for a pawn 1 rank in front of the king
        #   SHELTER_FAR   — cp bonus for a pawn 2 ranks in front of the king
        # Pawns beyond rank+2 don't contribute. Tapered to 0 in the EG (king
        # should be active there). OFF by default; toggle via engine_shelter.py
        # wrapper for A/B. Passed to C via set_shelter_params.
        self.use_king_shelter = False
        self.SHELTER_CLOSE = 8      # cp per pawn 1 rank ahead
        self.SHELTER_FAR   = 4      # cp per pawn 2 ranks ahead

        # History malus ("history gravity"): on a quiet beta-cutoff, the move
        # that caused the cutoff is rewarded (depth^2, as before) AND every
        # quiet move that was searched *before* it at this node -- and so failed
        # to cut -- is penalised by the same magnitude. This stops moves that
        # repeatedly look good in ordering but never actually refute anything
        # from keeping a stale high history score, sharpening quiet-move
        # ordering over time. The reward/penalty are damped toward zero
        # ("gravity") so the score can't run away. Standard technique; kept
        # behind a flag for A/B (nodes / move-agreement / match Elo) per the
        # one-feature-at-a-time workflow.
        self.use_history_malus = True
        self.HISTORY_MAX = 1 << 14   # clamp |history| so a key can't dominate

        # Skip the (otherwise wasted) full static eval at PV nodes -- see the
        # note in _negamax. Behaviour-preserving, so it is a pure speedup; the
        # flag exists only so the benchmark can A/B the saved eval calls and
        # confirm the search is byte-for-byte identical with it on vs off.
        self.lazy_pv_eval = True

        # Late-move (move-count) pruning. At a shallow, non-PV, not-in-check
        # node, once this many QUIET moves have been searched without beating
        # alpha, stop searching the rest -- the ordering (TT/captures/killers/
        # counter/history, all tried first) makes it very unlikely a late quiet
        # move matters. Unlike lazy_pv_eval this is LOSSY: it can prune the rare
        # quiet move that was actually the only refutation (incl. quiet checks,
        # which are not filtered out here -- testing for check needs a push,
        # which defeats the point). So the table is conservative and the only
        # honest verdict is a self-play match (engine_battle.py), not node
        # counts. Captures/promotions are never pruned: they all sort ABOVE
        # every quiet in order_moves, so by the time a quiet is reached the rest
        # of the list is quiet too -- which is why a plain `break` is safe.
        # A/B verdict (200 games @0.75+0.5, LMP-ON vs an identical LMP-OFF
        # build): ON scored 53.5% = +24 +/- 49 Elo -- positive but inside the
        # noise band, so not significant on its own. The mechanism is clear and
        # measured, though: ON averaged depth 8.0 vs 7.4 (+0.6 ply, +1 median
        # ply) at the same clock, on ~16% fewer nodes/move (~14% lower NPS, as
        # cheap near-leaf quiets are exactly what gets pruned). Decisive games
        # ran 49-35 for ON. Kept ON: deeper search, no tactical regression
        # (identical WAC solve count in bench), no sign of harm over 200 games.
        self.use_lmp = True
        self.LMP_MAX_DEPTH = 3
        self.LMP_COUNT = {1: 6, 2: 10, 3: 14}   # quiets searched before pruning

        # Cache the side-to-move static eval in the TT entry and reuse it on a
        # hit instead of recomputing the full positional eval. The static eval
        # is a deterministic function of the position and the TT key is
        # collision-free, so the cached value is always exactly the value the
        # eval would return -- this is a pure speedup that cannot change the
        # search (same nodes/scores/move). It attacks the NPS cost that LMP's
        # extra interior nodes add, since every non-PV node that pruning needs
        # an eval for and finds in the TT now skips _evaluate_stm. The flag lets
        # the benchmark A/B the saved eval calls on identical code.
        # Measured (fixed depth, ON vs OFF): byte-for-byte identical search
        # (same move/score/node count), ~22% fewer _evaluate_stm calls, which
        # nets ~+3.5% NPS -- eval is only a fraction of per-node cost (movegen /
        # ordering / SEE / push-pop / the quiescence lazy-base eval are not
        # touched), so the call saving doesn't translate 1:1 to speed. Pure,
        # safe speedup with no behavioural change -> kept ON; no match needed.
        self.tt_cached_eval = True

        # In-check nodes skip the full static eval (meaningless there).  The
        # improving heuristic and LMR then treat every check node as
        # not-improving, which is overly conservative when the side is escaping
        # a check sequence.  When a TT eval is available use it as a proxy;
        # otherwise fall back to alpha (a safe lower bound).  Does NOT feed
        # any of the pruning gates (RFP/null/futility -- all gated on
        # not-in-check already); only affects the improving trajectory.
        self.use_check_eval_proxy = True

        # Capture history: (mover_pt, to_sq, victim_pt) -> score, same
        # gravity rule as quiet history.  Blended into MVV-LVA ordering so
        # equal-valued captures are ranked by past search experience.
        self.use_capt_history = True
        self.capt_history = [0] * 4096   # X-05: _capt_hist_key is 12 bits
        self.CAPT_HISTORY_MAX = 1 << 14

        # SEE-prune losing captures at frontier nodes.  At depth<=2, non-PV,
        # not-in-check: if SEE(capture) < -depth*100 the subtree is almost
        # certainly wasted work (the qsearch will resolve the same exchange).
        self.use_see_prune_captures = True

        # SEE-refined quiescence capture ordering (see _capture_moves). Pure
        # reordering -> value-preserving. A/B verdict: a STRICT no-op here --
        # value AND node count were identical (+0.0%) on every test position.
        # Quiescence already SEE-PRUNES losing captures inside the loop, so
        # demoting them in the ordering changes nothing about which nodes are
        # visited, while the sound captures keep their MVV-LVA order. So it only
        # adds wasted SEE calls during ordering -> OFF. (Reordering the *sound*
        # captures by SEE could move nodes, but that needs SEE for every capture
        # on the quiescence hot path -- near-certainly net-negative on time.)
        self.use_qsee_order = False

        # Depth-preferred TT replacement with per-search aging (see the store in
        # _negamax). Correctness-safe regardless (TT entries are always valid
        # bounds; keeping or dropping any never makes the search wrong). A/B
        # verdict (16-ply Ruy Lopez line, depth 7, persistent TT): depth-
        # preferred searched ~+6% MORE nodes and was slower than the original
        # always-replace. Always-replace keeps the freshest best_move/bound,
        # which orders the current search region better than a deeper-but-staler
        # entry -- and ordering quality dominates node count more than the odd
        # extra cutoff a deeper entry buys. So always-replace (OFF) wins. Flag +
        # `_tt_gen` kept for a future smarter scheme (e.g. a two-tier bucket:
        # one depth-preferred slot + one always-replace slot per key).
        self.use_tt_depth_replace = False
        self._tt_gen = 0

        # Two-tier ("two-deep") TT bucket: store TWO entries per position -- a
        # depth-preferred slot (keeps the deepest result, demoting the old deep
        # entry rather than discarding it) and an always-replace slot (keeps the
        # freshest). Aims for best-of-both over plain always-replace vs depth-
        # preferred. Correctness-safe (every stored entry is still a valid bound).
        # A/B verdict (4 diverse 16-ply lines, depth 7, persistent TT): a WASH --
        # ~1% fewer nodes and ~0.7% faster wall-time, but NPS a hair LOWER (the
        # extra probe/store work eats most of the node saving), all inside the
        # noise. Notably it does NOT regress like the single-slot depth-preferred
        # (use_tt_depth_replace) did. Given it also costs ~2x TT memory per key,
        # the marginal gain doesn't justify it -> OFF. Kept (with _tt_get/
        # _tt_store hiding the format) as the one TT scheme worth a self-play
        # match if that ~1% is ever worth chasing. NOTE: turning this ON ~doubles
        # TT memory; consider halving TT_MAX_ENTRIES to keep the cap honest.
        self.use_tt_two_tier = False

        # Incremental evaluation: maintain the (material + PST + phase) base
        # accumulator with a small per-move delta on each make/unmake (see
        # _make / _move_delta) instead of recomputing the full bitboard scan in
        # _eval_base_white at every node. Behaviour-preserving -- the cached
        # accumulator is byte-for-byte what the scan would produce -- so it is a
        # pure speedup (nodes/scores/move identical); the flag lets the benchmark
        # A/B the NPS gain. Only the cheap "base" half is incremental; the
        # positional terms (pawns/mobility/king-safety) are still recomputed.
        # Verified: _move_delta exact over 46k move/position pairs (all move types
        # incl. ep/castle/promo), search byte-identical on vs off (move/score/
        # nodes) across 37 positions; measured ~+10.5% NPS at fixed depth (nodes
        # identical). Runs under PyPy too (PyPy's edge is now only ~+25% post
        # #8/#9 -- see the Strength note) and also speeds up the CPython GUI
        # directly. Kept ON.
        self.use_incremental_eval = True

        # NOTE: staged (lazy) move generation was implemented and A/B'd here, and
        # REMOVED. It was ~+6% NPS but LOST a self-play match decisively (-27 and
        # -19 +/- 24 Elo over ~800 games each, two independent matchups): yielding
        # moves in stages reorders the ties among equal-scored quiets differently
        # from board.legal_moves, and that natural order is a better tiebreak for
        # the order-dependent prunes (LMR/LMP), so search quality dropped more
        # than the speed helped. Don't re-add it without also making the quiet
        # tiebreak match the eager order -- which defeats the point. order_moves
        # (eager, full sort) is the move source for the whole search.

        # #9: C legal move generator for order_moves. Byte-identical to
        # board.legal_moves (same eager order -> same LMR/LMP tiebreaks, unlike
        # the staged experiment above), so nodes/scores/move are unchanged; only
        # the per-node move-gen cost drops. In-check nodes fall back to
        # board.legal_moves. Set False to A/B the NPS gain (or if movegen.so is
        # absent the module flag is already False -> pure-python path).
        self.use_c_movegen = _USE_C_MOVEGEN

        # --- Real-time search logging ----------------------------------- #
        self.on_depth = None        # called after each completed ID iteration
        self.on_final = None        # called once the move is chosen
        self.search_log = []

        # --- Opening book (Polyglot .bin) ------------------------------- #
        # Drop a Polyglot book next to the .py files (or the working dir) under
        # one of these names; the first that opens is used. Free books include
        # Performance.bin / Titans.bin / gm2600.bin / baron30.bin / komodo.bin
        # (e.g. from https://github.com/niklasf/python-chess test data or the
        # many "polyglot book" mirrors). Set ``use_book = False`` to disable.
        self.use_book = True
        self._book_reader = None
        self._book_resolved = False
        self.book_path = None
        self._book_candidates = [
            "Elo2400.bin", "Performance.bin", "Titans.bin", "book.bin",
            "gm2600.bin", "baron30.bin", "komodo.bin",
        ]

        # --- #4 Endgame tablebase (Lichess Syzygy API) ------------------ #
        # At the ROOT ONLY, positions with few pieces are looked up in the free
        # Lichess Syzygy tablebase (https://tablebase.lichess.ovh), which hosts
        # provably-perfect WDL/DTZ data for <= 7 pieces. A hit returns the
        # optimal move and skips the search entirely. It is NEVER queried inside
        # the search (that would fire thousands of HTTP requests/sec). Any
        # network error, timeout, illegal or empty response falls back to a
        # normal search, so play is unaffected when offline. Set
        # ``use_tb = False`` for fully offline play / reproducible benchmarks.
        self.use_tb = False
        self.tb_max_pieces = 7              # Lichess hosts 7-man Syzygy
        self.tb_timeout = 1.0               # max seconds to wait on the network
        self.tb_url = "https://tablebase.lichess.ovh/standard?fen="
        self._tb_cache = {}                 # fen -> (wdl, Move) | None
        self.TB_CACHE_MAX = 50_000
        self.TB_SCORE_UNIT = 1000           # display cp per WDL step (for the UI)

        # --- #8: sync C eval constants to current class attributes --------- #
        self._sync_c_params()

        # P-11: snapshot construction-time values of the SMP-propagated
        # attributes; _smp_config diffs live values against this to build the
        # worker config (dicts copied so later in-place edits can't alias).
        self._smp_defaults = {
            k: (dict(v) if isinstance(v, dict) else v)
            for k in self.SMP_CONFIG_ATTRS
            for v in (getattr(self, k, None),)
        }

    # ================================================================== #
    # C-eval parameter sync (#8)
    # ================================================================== #
    def _sync_c_params(self):
        """Push the current eval attributes into the C library's globals.

        The C evaluation reads process-global statics; they are synced here
        once at construction, and any host that mutates eval attributes
        afterwards (uci.py's setoption path, a tuner's apply_params) must call
        this again -- without the re-sync those attribute writes silently
        never reach the live C eval. The globals are per-process, so two
        Engines with different eval params cannot coexist in one process
        (SMP workers each sync in their own process).
        """
        # X-04: (re)build the per-(color, piece, square) contribution table
        # for _move_delta -- one tuple load instead of five dict/PST lookups
        # per piece event. Built here so the one existing "eval params
        # changed" hook (init + uci setoption + tuners) also refreshes it;
        # needed on the pure-Python fallback path too, hence before the
        # early return.
        self._contrib = _ct = [[[None] * 64 for _ in range(7)] for _ in range(2)]
        for _pt in range(1, 7):
            for _sq in range(64):
                _ct[1][_pt][_sq] = self._piece_contrib(_pt, chess.WHITE, _sq)
                _ct[0][_pt][_sq] = self._piece_contrib(_pt, chess.BLACK, _sq)
        # P-47: memoized static evals were computed under the old params.
        self._eval_memo = {}
        if not _USE_C_EVAL:
            # W-04 (roadmap bug #9 guard): these four eval toggles are C-only
            # -- no Python fallback exists. Without eval_c.so they would be
            # silent no-ops and an A/B of them would test nothing; fail loud.
            _on = [a for a in ("use_outpost", "use_space", "use_phalanx",
                               "use_storm") if getattr(self, a, False)]
            if _on:
                print(f"[engine] WARNING: {', '.join(_on)} enabled but "
                      "eval_c.so is not loaded -- these terms are C-only "
                      "and will NOT be applied", file=sys.stderr)
            return
        _eval_lib.set_mobility_params(
            self.MOBILITY_WEIGHT[chess.KNIGHT],
            self.MOBILITY_WEIGHT[chess.BISHOP],
            self.MOBILITY_WEIGHT[chess.ROOK],
            self.MOBILITY_WEIGHT[chess.QUEEN],
            self.PHASE_MAX,
            self.KING_SHIELD_MG,     self.KING_SHIELD_EG,
            self.KING_RING_ATTACK_MG, self.KING_RING_ATTACK_EG,
            self.KING_OPEN_FILE_MG,  self.KING_OPEN_FILE_EG,
        )
        # #2.5: same sync for the new positional_extras call.
        _eval_lib.set_positional_params(
            self.ROOK_OPEN_FILE, self.ROOK_SEMIOPEN_FILE,
            self.BISHOP_PAIR_MG, self.BISHOP_PAIR_EG,
            self.MOPUP_MIN_ADV,
            self.MOPUP_CMD_WEIGHT, self.MOPUP_KING_WEIGHT,
            self.MOPUP_STRONG_CMD_WEIGHT, self.MOPUP_STRONG_KING_WEIGHT,
        )
        # #3.x: sync rook-on-7th weights (0/0 if the toggle is off).
        if self.use_rook_on_7th:
            _eval_lib.set_rook_on_7th_params(self.ROOK_ON_7TH_MG, self.ROOK_ON_7TH_EG)
        else:
            _eval_lib.set_rook_on_7th_params(0, 0)
        # #3.x: sync mobility-area toggle.
        _eval_lib.set_mobility_area(1 if self.use_mobility_area else 0)
        # #3.x: sync threats (0/0 if the toggle is off).
        if self.use_threats:
            _eval_lib.set_threats_params(self.THREAT_PAWN, self.THREAT_MINOR)
        else:
            _eval_lib.set_threats_params(0, 0)
        # Outpost: sync bonus weights (on=0 passes 0/0/0/0 to C).
        _eval_lib.set_outpost_params(
            1 if self.use_outpost else 0,
            self.OUTPOST_N_MG, self.OUTPOST_N_EG,
            self.OUTPOST_B_MG, self.OUTPOST_B_EG,
        )
        # Space: sync weight (on=0 disables).
        _eval_lib.set_space_params(
            1 if self.use_space else 0,
            self.SPACE_MG,
        )
        # Phalanx / connected pawns: sync weights (on=0 disables).
        _eval_lib.set_phalanx_params(
            1 if self.use_phalanx else 0,
            self.PHALANX_MG, self.PHALANX_EG,
        )
        # Pawn storm toward enemy king: sync weights (on=0 disables).
        _eval_lib.set_storm_params(
            1 if self.use_storm else 0,
            self.STORM_MG, self.STORM_EG,
        )
        # King shelter depth: per-file/per-distance pawn shield (on=0 disables).
        _eval_lib.set_shelter_params(
            1 if self.use_king_shelter else 0,
            self.SHELTER_CLOSE, self.SHELTER_FAR,
        )

    # P-11: attributes replicated into SMP worker processes. Curated: values
    # must be picklable and meaningful to re-apply on a fresh Engine (workers
    # re-force use_tb/smp_workers/use_shared_tt afterwards regardless). Names
    # missing on an older engine are skipped silently by the snapshot.
    SMP_CONFIG_ATTRS = (
        # host/protocol config
        "use_book", "TT_MAX_ENTRIES",
        # material tables (uci setoption MG_*/EG_*)
        "MG_VALUES", "EG_VALUES",
        # tunable eval scalars (uci TUNABLE_EVAL)
        "ROOK_OPEN_FILE", "ROOK_SEMIOPEN_FILE", "TEMPO",
        "DOUBLED_PAWN", "ISOLATED_PAWN", "BACKWARD_PAWN",
        "BISHOP_PAIR_MG", "BISHOP_PAIR_EG",
        "KING_RING_ATTACK_MG", "KING_RING_ATTACK_EG",
        "KING_SHIELD_MG", "KING_SHIELD_EG",
        "KING_OPEN_FILE_MG", "KING_OPEN_FILE_EG",
        # A/B toggles (so a toggle experiment run under SMP actually tests it)
        "use_outpost", "use_space", "use_phalanx", "use_storm",
        "use_king_shelter", "use_rook_on_7th", "use_mobility_area",
        "use_threats", "use_lmp", "tt_cached_eval", "use_check_eval_proxy",
        "use_capt_history", "use_see_prune_captures", "use_incremental_eval",
        "lazy_pv_eval", "use_history_malus", "use_see", "use_qsee_order",
        "use_tt_depth_replace", "use_tt_two_tier", "use_pin_eval",
        "use_simplify", "recapture_ext", "lmr_aggressive",
    )

    def _smp_config(self):
        """P-11: the instance's overrides to replicate in SMP workers --
        everything in SMP_CONFIG_ATTRS that differs from this engine's
        construction-time defaults. Workers apply these with setattr and then
        re-sync the C-eval globals (see smp._apply_cfg)."""
        cfg = {}
        for k, d in self._smp_defaults.items():
            v = getattr(self, k, None)
            if v != d:
                cfg[k] = v
        return cfg

    # ================================================================== #
    # Pawn-mask precomputation (one-time, used by the evaluation)
    # ================================================================== #
    def _build_pawn_masks(self):
        self._file_bb = [int(chess.BB_FILES[f]) for f in range(8)]
        self._adj_files_bb = [
            ((self._file_bb[f - 1] if f > 0 else 0)
             | (self._file_bb[f + 1] if f < 7 else 0))
            for f in range(8)
        ]
        # passed[color][sq]: enemy-pawn squares that would block/guard the pawn
        # (own file + adjacent files, all ranks strictly ahead of the pawn).
        # support[color][sq]: friendly-pawn squares on adjacent files at or
        # behind the pawn's rank (a pawn there can advance to defend it).
        # stop_atk[color][sq]: enemy-pawn squares that attack the pawn's stop
        # square (the square one rank ahead).
        self._passed_mask = {chess.WHITE: [0] * 64, chess.BLACK: [0] * 64}
        self._support_mask = {chess.WHITE: [0] * 64, chess.BLACK: [0] * 64}
        self._stop_atk_mask = {chess.WHITE: [0] * 64, chess.BLACK: [0] * 64}
        for sq in range(64):
            f = sq & 7
            r = sq >> 3
            for color in (chess.WHITE, chess.BLACK):
                ahead = range(r + 1, 8) if color == chess.WHITE else range(0, r)
                behind = range(0, r + 1) if color == chess.WHITE else range(r, 8)
                passed = support = stop = 0
                for nf in (f - 1, f, f + 1):
                    if not (0 <= nf < 8):
                        continue
                    for nr in ahead:
                        passed |= 1 << (nr * 8 + nf)
                for nf in (f - 1, f + 1):
                    if not (0 <= nf < 8):
                        continue
                    for nr in behind:
                        support |= 1 << (nr * 8 + nf)
                stop_r = r + 1 if color == chess.WHITE else r - 1
                if 0 <= stop_r < 8:
                    # An enemy pawn attacks 'stop' if it sits one rank further
                    # ahead on an adjacent file (it captures backwards onto stop).
                    atk_r = stop_r + 1 if color == chess.WHITE else stop_r - 1
                    if 0 <= atk_r < 8:
                        for nf in (f - 1, f + 1):
                            if 0 <= nf < 8:
                                stop |= 1 << (atk_r * 8 + nf)
                self._passed_mask[color][sq] = passed
                self._support_mask[color][sq] = support
                self._stop_atk_mask[color][sq] = stop

        # Centre Manhattan distance per square: 0 on the four centre squares,
        # up to 6 in the corners. The endgame mop-up term uses this to push the
        # weak king toward an edge / corner where it can be mated.
        _edge = [3, 2, 1, 0, 0, 1, 2, 3]
        self._center_manhattan = [
            _edge[sq & 7] + _edge[sq >> 3] for sq in range(64)
        ]

    # ================================================================== #
    # Opening book
    # ================================================================== #
    def _get_book_reader(self):
        """Lazily open the first available Polyglot book; cache the reader."""
        if self._book_resolved:
            return self._book_reader
        self._book_resolved = True
        search_dirs = [os.getcwd()]
        try:
            search_dirs.append(os.path.dirname(os.path.abspath(__file__)))
        except NameError:
            pass
        for directory in search_dirs:
            for name in self._book_candidates:
                path = os.path.join(directory, name)
                if os.path.isfile(path):
                    try:
                        self._book_reader = chess.polyglot.open_reader(path)
                        self.book_path = path
                        return self._book_reader
                    except Exception:
                        self._book_reader = None
        return self._book_reader

    def _book_move(self, board):
        """Return a legal Polyglot book move for ``board`` or ``None``.

        Picks randomly among the position's book entries, WEIGHTED by each
        entry's polyglot weight -- so the book's top move is by far the most
        likely pick (e.g. startpos e2e4 at weight 17949 vs f2f4 at 54 is
        ~330x more likely), but weaker sidelines still get an occasional look
        rather than never being played. Falls back to uniform choice in the
        (spec-legal but rare) case every candidate has weight 0.
        """
        if not self.use_book:
            return None
        reader = self._get_book_reader()
        if reader is None:
            return None
        try:
            entries = [e for e in reader.find_all(board)
                       if e.move in board.legal_moves]
        except Exception:
            return None      # read error
        if not entries:
            return None      # position not in book
        weights = [e.weight for e in entries]
        if not any(weights):
            return random.choice(entries).move
        return random.choices(entries, weights=weights, k=1)[0].move

    # ================================================================== #
    # #4 Endgame tablebase (Lichess Syzygy API)
    # ================================================================== #
    def _tb_probe(self, board, timeout):
        """Probe the Lichess Syzygy tablebase for the optimal move in ``board``.

        Returns ``(wdl, move)`` -- ``wdl`` in {2,1,0,-1,-2} from the side to
        move's perspective (2=win, 1=cursed win, 0=draw, -1=blessed loss,
        -2=loss), ``move`` the move to play -- or ``None`` on any miss: too many
        pieces, network error, timeout, or an empty / unparseable / illegal
        response. NEVER raises: every failure path returns None so the caller
        falls back to a normal search.

        The Lichess API returns ``moves`` already sorted best-first for the side
        to move, so ``moves[0]`` is the move to play. Trusting that ordering --
        rather than re-deriving a best move from raw WDL/DTZ -- is what makes
        cursed wins / blessed losses (50-move-rule edge cases) correct without
        any fragile tie-break logic of our own.
        """
        if not self.use_tb or timeout <= 0:
            return None
        if board.occupied.bit_count() > self.tb_max_pieces:
            return None
        # Don't spend a network round-trip on positions the search already
        # nails faster than the API responds: dead-drawn insufficient material
        # (the engine returns the draw instantly) and overwhelming pawnless
        # mop-up wins (lone king vs a major piece -- KQK / KRK etc., which the
        # mop-up term is purpose-built to convert). The tablebase is reserved
        # for genuinely tricky endings (pawns, or a defending piece) where the
        # HCE's win/draw verdict or technique could actually be wrong.
        if board.is_insufficient_material() or self._tb_trivial_win(board):
            return None

        fen = board.fen()
        if fen in self._tb_cache:
            return self._tb_cache[fen]

        try:
            url = self.tb_url + urllib.parse.quote(fen)
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None                      # offline / timeout / bad response

        moves = data.get("moves")
        if not moves:
            return None                      # no data for this position
        best_uci = moves[0].get("uci")
        if not best_uci:
            return None
        try:
            move = chess.Move.from_uci(best_uci)
        except Exception:
            return None
        if move not in board.legal_moves:    # paranoia: never play an illegal move
            return None

        wdl = data.get("wdl")
        if wdl is None:                      # WDL occasionally absent -> derive from category
            wdl = {"win": 2, "cursed-win": 1, "draw": 0,
                   "blessed-loss": -1, "loss": -2}.get(data.get("category"), 0)

        result = (wdl, move)
        if len(self._tb_cache) >= self.TB_CACHE_MAX:
            self._tb_cache.clear()
        self._tb_cache[fen] = result
        return result

    def _tb_trivial_win(self, board):
        """True if the position is an overwhelming, pawnless mop-up win that the
        search converts faster than the tablebase round-trip: one side is a bare
        lone king while the other holds a major piece (queen or rook).

        Any pawn makes it 'complex' (KP vs K, KQ vs KP, ... -- where the verdict
        can flip on precise play), so a pawn never counts as trivial. Minor-only
        wins are NOT skipped either: KBN vs K is pawnless but a hard mate that
        the generic center-distance mop-up can botch within the 50-move rule, so
        the tablebase still earns its latency there.
        """
        if board.pawns:
            return False                     # pawns present -> not trivial, probe it
        majors = board.queens | board.rooks
        if not majors:
            return False                     # only minors (KBN/KBB/KNN) -> probe
        w = board.occupied_co[chess.WHITE]
        b = board.occupied_co[chess.BLACK]
        # One side reduced to just its king, the other holding a major piece.
        return ((w.bit_count() == 1 and bool(b & majors))
                or (b.bit_count() == 1 and bool(w & majors)))

    # ================================================================== #
    # Evaluation (hand-crafted, tapered)
    # ================================================================== #
    def evaluate_position(self, board):
        """Public, terminal-aware static evaluation (White's perspective)."""
        if board.is_checkmate():
            return -self.MATE_SCORE if board.turn == chess.WHITE else self.MATE_SCORE
        if (board.is_stalemate()
                or board.is_insufficient_material()
                or board.is_seventyfive_moves()
                or board.is_fivefold_repetition()):
            return 0
        return self._evaluate_static(board)

    def _evaluate_stm(self, board, key=None):
        """Static evaluation relative to the side to move (for negamax).

        P-47: memoized by full position key (side to move included, so the
        stm-relative value is well-defined). Eval is a pure function of the
        position, so hits are exact -- byte-identical search, fewer evals.
        Unlike the TT's cached-eval reuse this also serves quiescence
        stand-pat and TT-miss nodes. W-07: callers that already computed the
        node's transposition key (negamax's repetition/TT key) pass it in."""
        if key is None:
            key = board._transposition_key()
        v = self._eval_memo.get(key)
        if v is None:
            white = self._evaluate_static(board)
            v = white if board.turn == chess.WHITE else -white
            if len(self._eval_memo) >= self.EVAL_MEMO_MAX:
                self._eval_memo.clear()
            self._eval_memo[key] = v
        return v

    def _evaluate_static(self, board):
        """Tapered material + PST plus positional terms, White's perspective.

        Split into a cheap base (material + PST + phase + tempo) and the
        expensive positional terms, so quiescence can evaluate lazily (see
        ``_qs_stand_pat``) without ever recomputing the base.
        """
        base, ctx = self._eval_base_white(board)
        return base + self._eval_positional_white(board, ctx)

    def _eval_base_white(self, board):
        """Cheap half: tapered material + PST + tempo (White's perspective).

        Single bitboard pass (no per-call ``piece_map``/``board.pieces``
        rebuilds). Returns ``(base_score, ctx)`` where ctx carries the bitboards
        and phase the positional half needs, so nothing is recomputed.
        """
        occ_w = board.occupied_co[chess.WHITE]
        occ_b = board.occupied_co[chess.BLACK]
        pawns = board.pawns
        knights = board.knights
        bishops = board.bishops
        rooks = board.rooks
        queens = board.queens
        kings = board.kings

        # mg / eg / (raw) phase: from the live accumulator while a search is
        # maintaining it, else the from-scratch bitboard scan.
        if self.use_incremental_eval and self._acc_valid:
            mg, eg, phase = self._acc
        else:
            mg, eg, phase = self._compute_acc(board)

        if phase > self.PHASE_MAX:
            phase = self.PHASE_MAX
        # Truncate toward zero (not floor) so the blend is exactly symmetric:
        # eval(pos) == -eval(mirror(pos)). Floor division would skew negatives.
        num = mg * phase + eg * (self.PHASE_MAX - phase)
        score = -((-num) // self.PHASE_MAX) if num < 0 else num // self.PHASE_MAX
        # Tempo (cheap, folded into the base so the lazy bound is exact).
        score += self.TEMPO if board.turn == chess.WHITE else -self.TEMPO
        ctx = (occ_w, occ_b, pawns, knights, bishops, rooks, queens, kings,
               pawns & occ_w, pawns & occ_b, phase)
        return score, ctx

    # ------------------------------------------------------------------ #
    # #13: Zobrist hashing (from-scratch + the small castling helper). The
    # incremental update lives in _make / _unmake. Verified incremental ==
    # from-scratch over random move sequences (incl. null/ep/castle/promo).
    # ------------------------------------------------------------------ #
    @staticmethod
    def _zob_castle(cr):
        z = 0
        for sq, idx in _ZOB_CR_BITS:
            if cr & (1 << sq):
                z ^= _ZOB_CASTLE[idx]
        return z

    def _compute_zobrist(self, board):
        z = 0
        psq = _ZOB_PSQ
        for pt, bb_all in ((chess.PAWN, board.pawns), (chess.KNIGHT, board.knights),
                           (chess.BISHOP, board.bishops), (chess.ROOK, board.rooks),
                           (chess.QUEEN, board.queens), (chess.KING, board.kings)):
            for sq in chess.scan_forward(bb_all & board.occupied_co[chess.WHITE]):
                z ^= psq[chess.WHITE][pt][sq]
            for sq in chess.scan_forward(bb_all & board.occupied_co[chess.BLACK]):
                z ^= psq[chess.BLACK][pt][sq]
        z ^= self._zob_castle(board.clean_castling_rights())
        if board.ep_square is not None and board.has_legal_en_passant():
            z ^= _ZOB_EP[board.ep_square & 7]
        if board.turn == chess.BLACK:
            z ^= _ZOB_SIDE
        return z

    # ------------------------------------------------------------------ #
    # Incremental-eval accumulator (material + PST + raw phase, White's view).
    # ------------------------------------------------------------------ #
    def _compute_acc(self, board):
        """From-scratch ``[mg, eg, phase]`` -- the bitboard scan. RAW phase (the
        PHASE_MAX cap is applied later in _eval_base_white, so deltas stay
        reversible). Returns a fresh list so the caller may mutate it in
        place without aliasing concerns."""
        occ_w = board.occupied_co[chess.WHITE]
        occ_b = board.occupied_co[chess.BLACK]
        mg = eg = phase = 0
        for pt, bb_all in ((chess.PAWN, board.pawns), (chess.KNIGHT, board.knights),
                           (chess.BISHOP, board.bishops), (chess.ROOK, board.rooks),
                           (chess.QUEEN, board.queens), (chess.KING, board.kings)):
            mgt = self.mg_tables[pt]
            egt = self.eg_tables[pt]
            mv = self.MG_VALUES[pt]
            ev = self.EG_VALUES[pt]
            pw = self.PHASE_WEIGHTS[pt]
            for sq in chess.scan_forward(bb_all & occ_w):
                idx = sq ^ 56            # == chess.square_mirror(sq)
                mg += mv + mgt[idx]
                eg += ev + egt[idx]
                phase += pw
            for sq in chess.scan_forward(bb_all & occ_b):
                mg -= mv + mgt[sq]
                eg -= ev + egt[sq]
                phase += pw
        return [mg, eg, phase]

    def _piece_contrib(self, pt, color, sq):
        """A single piece's (d_mg, d_eg, d_phase) contribution, White's view:
        material + PST signed by colour; phase always positive (it counts total
        material on the board, both sides)."""
        if color == chess.WHITE:
            idx = sq ^ 56
            return (self.MG_VALUES[pt] + self.mg_tables[pt][idx],
                    self.EG_VALUES[pt] + self.eg_tables[pt][idx],
                    self.PHASE_WEIGHTS[pt])
        return (-(self.MG_VALUES[pt] + self.mg_tables[pt][sq]),
                -(self.EG_VALUES[pt] + self.eg_tables[pt][sq]),
                self.PHASE_WEIGHTS[pt])

    def _move_delta(self, board, move, raw):
        """Mutate ``self._acc`` in place for playing ``move`` on ``board``
        (read BEFORE the push). Mirrors exactly what _compute_acc would return
        on the resulting position -- handles captures, en passant, promotions
        and castling. The caller is responsible for having snapshotted the
        old accumulator (see _make).

        X-03: from/to/mover/victim/ep/promo all come from the packed ``raw``
        tag every hot caller already holds, replacing four python-chess
        queries per make; castling isn't tagged but is exactly "the king
        moves two files". X-04: piece contributions come from the
        precomputed ``_contrib`` table (see _sync_c_params)."""
        acc = self._acc
        mg = acc[0]; eg = acc[1]; phase = acc[2]
        color = board.turn
        ctab = self._contrib[color]
        frm = raw & 63
        to = (raw >> 6) & 63
        mover_pt = (raw >> MV_SHIFT_MOVER) & MV_MASK_PT

        # Mover leaves `frm`.
        a, b, c = ctab[mover_pt][frm]
        mg -= a; eg -= b; phase -= c

        # Captured piece (if any) leaves the board. The ep-tagged victim is
        # always a pawn on the bypassed square; otherwise the victim type is
        # in the tag (0 = quiet).
        if raw & MV_BIT_EP:
            cap_sq = to + (-8 if color == chess.WHITE else 8)
            a, b, c = self._contrib[not color][chess.PAWN][cap_sq]
            mg -= a; eg -= b; phase -= c
        else:
            victim_pt = (raw >> MV_SHIFT_VICTIM) & MV_MASK_PT
            if victim_pt:
                a, b, c = self._contrib[not color][victim_pt][to]
                mg -= a; eg -= b; phase -= c

        # Mover (or the promoted piece) arrives on `to`.
        promo_pt = (raw >> 12) & 7
        a, b, c = ctab[promo_pt if promo_pt else mover_pt][to]
        mg += a; eg += b; phase += c

        # Castling: the rook moves too (king travels two files; no tag bit).
        if mover_pt == chess.KING and abs(to - frm) == 2:
            if to > frm:                                     # kingside
                r_from = chess.H1 if color == chess.WHITE else chess.H8
                r_to = chess.F1 if color == chess.WHITE else chess.F8
            else:
                r_from = chess.A1 if color == chess.WHITE else chess.A8
                r_to = chess.D1 if color == chess.WHITE else chess.D8
            a, b, c = ctab[chess.ROOK][r_from]
            mg -= a; eg -= b; phase -= c
            a, b, c = ctab[chess.ROOK][r_to]
            mg += a; eg += b; phase += c

        acc[0] = mg; acc[1] = eg; acc[2] = phase

    # ------------------------------------------------------------------ #
    # NPS roadmap item (2026-07-02): _make/_make_null/_unmake are the
    # hottest functions in the engine -- called at every single node -- and
    # used to carry an `if self._zob_valid:` check (1-2 per call) purely for
    # the Zobrist/shared-TT bookkeeping, even though that's off in the
    # overwhelming majority of searches (use_zobrist/use_shared_tt both
    # default False). Historical NPS data (v17->v18->v19, when this
    # machinery was added) showed a real ~3-5% cost from those "should be
    # free when off" checks -- Python doesn't make a permanently-False
    # branch actually free, only cheap. Fixed by splitting each into a
    # `_nozob` and `_zob` variant with NO conditional at all (the nozob
    # variant has no Zobrist code to skip; the zob variant has no check
    # because it's only ever bound while Zobrist is definitely wanted), and
    # binding self._make/_make_null/_unmake to the right pair once per
    # search (see _search, where _zob_valid itself is set/cleared) instead
    # of branching on every one of possibly millions of calls.
    # ------------------------------------------------------------------ #
    def _make_nozob(self, board, move, raw):
        """_make without Zobrist maintenance -- bound whenever a search has
        use_zobrist and use_shared_tt both off (the default; see _search).
        ``raw`` is the move's packed tag word (X-03). X-06: accumulator code
        is UNCONDITIONAL here -- this variant is only ever bound while
        use_incremental_eval is on and the search has the accumulator armed;
        the `_noacc` twin covers everything else."""
        self._acc_stack.extend(self._acc)   # flat 3-int snapshot
        self._move_delta(board, move, raw)  # mutates self._acc in place
        board.push(move)

    def _make_nozob_noacc(self, board, move, raw):
        """_make twin with NO accumulator maintenance (X-06): the class
        default (safe for ad-hoc pushes outside a search) and the bound
        variant when use_incremental_eval is off."""
        board.push(move)

    def _make_zob(self, board, move, raw):
        """_make WITH Zobrist maintenance -- bound only while a search has
        use_zobrist or use_shared_tt on. See _make_nozob for the common case."""
        self._acc_stack.extend(self._acc)
        self._move_delta(board, move, raw)
        self._zob_stack.append(self._zob)
        self._zob = self._zob_delta(board, move)   # piece + side; pre-push
        board.push(move)
        z = self._zob ^ self._zob_castle(board.clean_castling_rights())
        if board.ep_square is not None and board.has_legal_en_passant():
            z ^= _ZOB_EP[board.ep_square & 7]
        self._zob = z

    def _make_zob_noacc(self, board, move, raw):
        """_make_zob twin with NO accumulator maintenance (X-06)."""
        self._zob_stack.append(self._zob)
        self._zob = self._zob_delta(board, move)
        board.push(move)
        z = self._zob ^ self._zob_castle(board.clean_castling_rights())
        if board.ep_square is not None and board.has_legal_en_passant():
            z ^= _ZOB_EP[board.ep_square & 7]
        self._zob = z

    # Default binding so _make/_make_null/_unmake are always callable even
    # before the first _search() call rebinds them (e.g. ad-hoc scripts /
    # tests that push moves outside a real search); _search always rebinds
    # to the correct variants for use_zobrist/use_shared_tt AND
    # use_incremental_eval (X-06) before the tree walk starts. The defaults
    # are the accumulator-free twins, which are safe with no armed acc.
    _make = _make_nozob_noacc

    def _make_null_nozob(self, board):
        """_make_null without Zobrist maintenance (see _make_nozob).
        Snapshot the current values (no mutation happens for null, but the
        matching _unmake unconditionally pops 3 ints)."""
        self._acc_stack.extend(self._acc)
        board.push(chess.Move.null())

    def _make_null_nozob_noacc(self, board):
        """_make_null twin with NO accumulator maintenance (X-06)."""
        board.push(chess.Move.null())

    def _make_null_zob(self, board):
        """_make_null WITH Zobrist maintenance (see _make_zob)."""
        self._acc_stack.extend(self._acc)
        self._zob_stack.append(self._zob)
        z = self._zob ^ _ZOB_SIDE                  # flip side; ep cleared
        if board.ep_square is not None and board.has_legal_en_passant():
            z ^= _ZOB_EP[board.ep_square & 7]
        self._zob = z
        board.push(chess.Move.null())

    def _make_null_zob_noacc(self, board):
        """_make_null_zob twin with NO accumulator maintenance (X-06)."""
        self._zob_stack.append(self._zob)
        z = self._zob ^ _ZOB_SIDE                  # flip side; ep cleared
        if board.ep_square is not None and board.has_legal_en_passant():
            z ^= _ZOB_EP[board.ep_square & 7]
        self._zob = z
        board.push(chess.Move.null())

    _make_null = _make_null_nozob_noacc

    def _zob_delta(self, board, move):
        """Zobrist after `move`, EXCLUDING new castling/ep (added post-push by
        the caller). Reads board state BEFORE the push: removes the old
        castling/ep contribution, the mover (and any captured piece), adds the
        arriving piece (promotion-aware), the castled rook, and flips side."""
        z = self._zob
        us = board.turn
        z ^= self._zob_castle(board.clean_castling_rights())   # old castle out
        if board.ep_square is not None and board.has_legal_en_passant():
            z ^= _ZOB_EP[board.ep_square & 7]                  # old ep out
        frm = move.from_square
        to = move.to_square
        psq_us = _ZOB_PSQ[us]
        mover_pt = board.piece_type_at(frm)
        z ^= psq_us[mover_pt][frm]
        if board.is_en_passant(move):
            cap_sq = to - 8 if us == chess.WHITE else to + 8
            z ^= _ZOB_PSQ[not us][chess.PAWN][cap_sq]
        elif board.is_capture(move):
            z ^= _ZOB_PSQ[not us][board.piece_type_at(to)][to]
        z ^= psq_us[move.promotion if move.promotion else mover_pt][to]
        if board.is_castling(move):
            if board.is_kingside_castling(move):
                rf, rt = (chess.H1, chess.F1) if us == chess.WHITE else (chess.H8, chess.F8)
            else:
                rf, rt = (chess.A1, chess.D1) if us == chess.WHITE else (chess.A8, chess.D8)
            z ^= psq_us[chess.ROOK][rf] ^ psq_us[chess.ROOK][rt]
        z ^= _ZOB_SIDE
        return z

    def _unmake_nozob(self, board):
        """Pop the last move and restore the accumulator -- no Zobrist
        maintenance (see _make_nozob).

        The flat int stack is unpacked in reverse insert order (phase, eg, mg)
        directly into the mutable ``self._acc`` slots -- no list/tuple alloc.
        """
        board.pop()
        s = self._acc_stack
        acc = self._acc
        acc[2] = s.pop()
        acc[1] = s.pop()
        acc[0] = s.pop()

    def _unmake_nozob_noacc(self, board):
        """_unmake twin with NO accumulator maintenance (X-06)."""
        board.pop()

    def _unmake_zob(self, board):
        """_unmake WITH Zobrist maintenance (see _make_zob)."""
        board.pop()
        s = self._acc_stack
        acc = self._acc
        acc[2] = s.pop()
        acc[1] = s.pop()
        acc[0] = s.pop()
        self._zob = self._zob_stack.pop()

    def _unmake_zob_noacc(self, board):
        """_unmake_zob twin with NO accumulator maintenance (X-06)."""
        board.pop()
        self._zob = self._zob_stack.pop()

    _unmake = _unmake_nozob_noacc

    def _eval_positional_white(self, board, ctx):
        """Expensive half: pawn structure / mobility / king safety / mop-up /
        pins, summed (White's perspective). ``ctx`` comes from
        ``_eval_base_white`` so no bitboards are recomputed.

        # #2.5b: at high phase WITH the C eval available, the single
        ``mobility_king_safety`` ctypes call now also returns the
        rook_files + bishop_pair contributions (folded in-line on the C
        side). The two Python helpers are skipped in that branch so we
        don't double-count. Low phase / pure-Python fallback still calls
        them; the standalone ``positional_extras`` C helper (kept in
        eval_c.c) is unused but documents the prior trial fold.
        """
        (occ_w, occ_b, pawns, knights, bishops, rooks, queens, kings,
         wp, bp, phase) = ctx
        # "Mating" scenario: one side is down to a lone king (+ pawns).
        lone_loser = ((occ_w & ~kings & ~pawns) == 0) != ((occ_b & ~kings & ~pawns) == 0)
        if lone_loser:
            # Bug fix (roadmap item #2): only take the dedicated mop-up
            # shortcut when the non-pawn material edge actually clears
            # _mopup_bb's own MOPUP_MIN_ADV gate. Below that gate (e.g. a
            # lone knight, 320cp, vs a bare king+pawns), _mopup_bb returns 0
            # and this branch used to return that 0 for the WHOLE positional
            # eval -- silently dropping pawn structure (the dominant term in
            # exactly these endings), mobility, king safety, everything.
            # Duplicating the npm sum here (rather than checking if
            # _mopup_bb's result happens to be 0) avoids a false negative:
            # the bonus formula can legitimately compute to exactly 0 for
            # specific king squares even when the gate passes.
            npm_w = self._npm(0, knights, bishops, rooks, queens, occ_w)  # W-14
            npm_b = self._npm(0, knights, bishops, rooks, queens, occ_b)
            if abs(npm_w - npm_b) >= self.MOPUP_MIN_ADV:
                # Dedicated mating evaluation -- skip the noisy positional
                # terms and use a strong mop-up (see the long note this
                # replaced).
                return self._mopup_bb(occ_w, occ_b, knights, bishops,
                                      rooks, queens, kings, strong=True)
            # Gate didn't engage -- fall through to normal positional eval
            # below (which itself adds a weak mop-up at phase<=6, correctly
            # contributing 0 here since it shares the same gate).
        delta = self._pawn_structure_bb(wp, bp, phase)
        # #2.5b: rook_files + bishop_pair + rook_on_7th + threats + outpost/
        # space/phalanx/storm are all folded into the C mobility_king_safety
        # call UNCONDITIONALLY (eval_c.c assumes it's only ever called at high
        # phase, per its own comment) -- so the Python versions here must be
        # skipped whenever that C call is actually being made, at ANY phase,
        # not just phase>6. Bug fix (roadmap item #1): this used to read
        # `_USE_C_EVAL and phase > 6`, which meant at phase<=6 the C call was
        # never made at all (see below) yet these Python terms fired anyway --
        # that part was fine -- but it ALSO meant king-safety (shield/ring/
        # open-file) and the five post-v21 toggle terms were silently dropped
        # in every endgame, since nothing else ever computed them at low
        # phase. Fixed by calling _mobility_king_safety_bb unconditionally
        # below; high_phase_c now tracks "is the C call happening" rather than
        # "is phase high", so this guard still can't double-count.
        high_phase_c = _USE_C_EVAL
        if not high_phase_c:
            delta += self._rook_files_bb(rooks, occ_w, occ_b, wp, bp)
            delta += self._bishop_pair_bb(bishops, occ_w, occ_b, phase)
            # #3.x: rook-on-7th. With the C eval on, this is folded into
            # mobility_king_safety (and gated by R7_MG|R7_EG there); on the
            # Python-fallback path we add it explicitly here so the term
            # applies uniformly across paths.
            delta += self._rook_on_7th_bb(rooks, occ_w, occ_b, wp, bp, kings, phase)
        if self.use_pin_eval:
            delta += self._pin_penalty_bb(board, occ_w, occ_b)
        if self.use_simplify:
            delta += self._simplify_bb(occ_w, occ_b, pawns, knights,
                                       bishops, rooks, queens)
        # Combined pass: one attacks_mask per piece feeds BOTH mobility and
        # the king-ring attacker count (previously 16 separate, expensive
        # attackers_mask calls). Identical result, fewer board queries. Its
        # own internal MG/EG taper (via `phase`) already handles low phase
        # correctly -- calling it unconditionally (rather than only at
        # phase>6) is what restores king-safety/toggle terms in endgames.
        delta += self._mobility_king_safety_bb(
            board, occ_w, occ_b, knights, bishops, rooks, queens, wp, bp, phase)
        # C-18: with the C eval, the phase<=6 mop-up is folded into the
        # mobility_king_safety call itself (eval_c.c ABI 2); only the pure-
        # Python fallback still needs the separate helper.
        if phase <= 6 and not _USE_C_EVAL:
            delta += self._mopup_bb(occ_w, occ_b, knights, bishops,
                                    rooks, queens, kings)
        return delta

    def _qs_stand_pat(self, board, beta):
        """Stand-pat eval for quiescence, evaluated lazily: if the cheap base
        already proves stand_pat >= beta by LAZY_MARGIN (>= the max positional
        swing), skip the expensive positional terms. The cutoff is identical to
        the full eval, so the search is unchanged -- just faster."""
        base, ctx = self._eval_base_white(board)
        base_stm = base if board.turn == chess.WHITE else -base
        if base_stm - self.LAZY_MARGIN >= beta:
            return base_stm                          # full eval is also >= beta
        # Y-03: the P-47 memo caches exactly this stm-relative full eval --
        # consult/fill it so a leaf revisited anywhere in the tree skips the
        # positional recompute. The lazy exit above is a BOUND, never cached.
        key = board._transposition_key()
        v = self._eval_memo.get(key)
        if v is None:
            full = base + self._eval_positional_white(board, ctx)
            v = full if board.turn == chess.WHITE else -full
            if len(self._eval_memo) >= self.EVAL_MEMO_MAX:
                self._eval_memo.clear()
            self._eval_memo[key] = v
        return v

    def _is_endgame(self, board):
        phase = (((board.knights | board.bishops).bit_count()) * 1
                 + (board.rooks.bit_count()) * 2
                 + (board.queens.bit_count()) * 4)
        return phase <= 6

    # ------------------------------------------------------------------ #
    # Pawn structure: doubled, isolated, backward and passed pawns.
    # O(1) per pawn using masks precomputed in __init__.
    # ------------------------------------------------------------------ #
    def _pawn_structure_bb(self, wp, bp, phase):
        # #3 Pawn hash. W-09: only the passed-pawn bonus is phase-tapered, so
        # the cache is keyed on (wp, bp) alone -- it stores the phase-free
        # penalty sum plus the passer list, and the cheap taper is re-applied
        # per call. Piece trades with unchanged pawns now HIT instead of
        # recomputing the whole scan (the old key was (wp, bp, phase)).
        key = (wp, bp)
        cached = self._pawn_cache.get(key)
        if cached is None:
            base = 0
            passers = []
            for own, opp, sign, color in ((wp, bp, 1, chess.WHITE),
                                          (bp, wp, -1, chess.BLACK)):
                for f in range(8):
                    c = (own & self._file_bb[f]).bit_count()
                    if c > 1:                              # doubled
                        base -= sign * self.DOUBLED_PAWN * (c - 1)
                passed = self._passed_mask[color]
                support = self._support_mask[color]
                stopatk = self._stop_atk_mask[color]
                for sq in chess.scan_forward(own):
                    f = sq & 7
                    if not (own & self._adj_files_bb[f]):  # isolated
                        base -= sign * self.ISOLATED_PAWN
                    elif not (own & support[sq]) and (opp & stopatk[sq]):
                        base -= sign * self.BACKWARD_PAWN  # backward
                    if not (opp & passed[sq]):             # passed
                        r = sq >> 3
                        passers.append(
                            (sign, r if color == chess.WHITE else 7 - r))
            # #3 Memoize. Cap memory by dropping the whole cache when it grows
            # too large (cheaper and simpler than per-entry eviction; the cache
            # refills quickly and correctness is unaffected: pure function).
            if len(self._pawn_cache) >= self.PAWN_CACHE_MAX:
                self._pawn_cache.clear()
            self._pawn_cache[key] = cached = (base, passers)
        score, passers = cached
        pm = self.PHASE_MAX
        ppm = self.PASSED_PAWN_MG
        ppe = self.PASSED_PAWN_EG
        for sign, rel in passers:      # identical per-passer taper arithmetic
            score += sign * ((ppm[rel] * phase + ppe[rel] * (pm - phase)) // pm)
        return score

    def _is_passed_pawn(self, board, square, color):
        """Per-call passed-pawn test (used only by the rare push extension)."""
        opp = board.pawns & board.occupied_co[not color]
        return not (opp & self._passed_mask[color][square])

    # ------------------------------------------------------------------ #
    # Combined mobility + king safety, called at EVERY phase (roadmap bug #1
    # fix -- used to be high-phase-only, silently dropping king safety and
    # the five post-v21 toggle terms in every endgame). One attacks_mask per
    # sliding/knight piece serves both terms, so the king-ring attacker count
    # no longer needs 16 separate attackers_mask calls.
    #
    # CAUTION (roadmap bug #9): the C branch below also folds in rook_files/
    # bishop_pair/rook_on_7th/threats/outpost/space/phalanx/storm (eval_c.c's
    # mobility_king_safety), but the Python FALLBACK branch here only
    # implements mobility + king safety + threats + king shelter (_py_shelter)
    # -- outpost/space/phalanx/storm have no Python equivalent at all. If
    # eval_c.so is missing (_USE_C_EVAL False) and one of those four toggles
    # is on, the bonus is silently dropped instead of falling back with
    # identical behaviour. Dormant today since all four default off.
    # ------------------------------------------------------------------ #
    def _mobility_king_safety_bb(self, board, occ_w, occ_b,
                                 knights, bishops, rooks, queens, wp, bp, phase):
        if _USE_C_EVAL:
            wksq = board.king(chess.WHITE)
            bksq = board.king(chess.BLACK)
            return _C_MKS(
                occ_w, occ_b, knights, bishops, rooks, queens, wp, bp,
                wksq if wksq is not None else -1,
                bksq if bksq is not None else -1,
                phase,
            )
        am = board.attacks_mask
        wksq = board.king(chess.WHITE)
        bksq = board.king(chess.BLACK)
        wring = chess.BB_KING_ATTACKS[wksq] if wksq is not None else 0
        bring = chess.BB_KING_ATTACKS[bksq] if bksq is not None else 0

        score = 0
        w_ring_att = 0          # incidences of black pieces attacking White's ring
        b_ring_att = 0          # incidences of white pieces attacking Black's ring
        # #3.x: pawn attack sets used by mobility-area AND the threats block.
        FA = ~int(chess.BB_FILE_A) & ((1 << 64) - 1)
        FH = ~int(chess.BB_FILE_H) & ((1 << 64) - 1)
        patk_w = ((wp << 9) & FA) | ((wp << 7) & FH)
        patk_b = ((bp >> 7) & FA) | ((bp >> 9) & FH)
        if self.use_mobility_area:
            w_safe = ~occ_w & ~patk_b & ((1 << 64) - 1)
            b_safe = ~occ_b & ~patk_w & ((1 << 64) - 1)
        else:
            w_safe = ~occ_w & ((1 << 64) - 1)
            b_safe = ~occ_b & ((1 << 64) - 1)
        # #3.x: accumulate minor-piece attacks per color for the threats block.
        w_minor_atk = 0
        b_minor_atk = 0
        for pt, bb in ((chess.KNIGHT, knights), (chess.BISHOP, bishops),
                       (chess.ROOK, rooks), (chess.QUEEN, queens)):
            wt = self.MOBILITY_WEIGHT[pt]
            is_minor = pt in (chess.KNIGHT, chess.BISHOP)
            for sq in chess.scan_forward(bb & occ_w):
                a = am(sq)
                score += wt * (a & w_safe).bit_count()
                b_ring_att += (a & bring).bit_count()
                if is_minor:
                    w_minor_atk |= a
            for sq in chess.scan_forward(bb & occ_b):
                a = am(sq)
                score -= wt * (a & b_safe).bit_count()
                w_ring_att += (a & wring).bit_count()
                if is_minor:
                    b_minor_atk |= a
        # #3.x: threats. Pawn -> enemy non-pawn; minor -> enemy major.
        if self.use_threats:
            b_non_pawn = occ_b & ~bp
            w_non_pawn = occ_w & ~wp
            b_major = (rooks | queens) & occ_b
            w_major = (rooks | queens) & occ_w
            score += self.THREAT_PAWN  * (patk_w & b_non_pawn).bit_count()
            score -= self.THREAT_PAWN  * (patk_b & w_non_pawn).bit_count()
            score += self.THREAT_MINOR * (w_minor_atk & b_major).bit_count()
            score -= self.THREAT_MINOR * (b_minor_atk & w_major).bit_count()

        # Pawn and enemy-king ring incidences (not covered by the loop above).
        if wring:
            for sq in chess.scan_forward(bp):
                w_ring_att += (chess.BB_PAWN_ATTACKS[chess.BLACK][sq] & wring).bit_count()
            if bksq is not None:
                w_ring_att += (chess.BB_KING_ATTACKS[bksq] & wring).bit_count()
        if bring:
            for sq in chess.scan_forward(wp):
                b_ring_att += (chess.BB_PAWN_ATTACKS[chess.WHITE][sq] & bring).bit_count()
            if wksq is not None:
                b_ring_att += (chess.BB_KING_ATTACKS[wksq] & bring).bit_count()

        # King safety terms (shield / attacker penalty / open file), White POV.
        # Tapered: these terms matter more in the middlegame than the endgame.
        pm = self.PHASE_MAX
        shield_val   = (self.KING_SHIELD_MG   * phase + self.KING_SHIELD_EG   * (pm - phase)) // pm
        ring_val     = (self.KING_RING_ATTACK_MG * phase + self.KING_RING_ATTACK_EG * (pm - phase)) // pm
        open_val     = (self.KING_OPEN_FILE_MG * phase + self.KING_OPEN_FILE_EG * (pm - phase)) // pm
        # Shelter taper mirrors C: SHELTER_CLOSE/FAR at MG, 0 at EG.
        sc = (self.SHELTER_CLOSE * phase) // pm if pm else 0
        sf = (self.SHELTER_FAR   * phase) // pm if pm else 0
        if wksq is not None:
            if self.use_king_shelter:
                score += self._py_shelter(wksq, wp, True, sc, sf)
            else:
                score += (wring & occ_w).bit_count() * shield_val
            score -= w_ring_att * ring_val
            if not (wp & self._file_bb[wksq & 7]):
                score -= open_val
        if bksq is not None:
            if self.use_king_shelter:
                score -= self._py_shelter(bksq, bp, False, sc, sf)
            else:
                score -= (bring & occ_b).bit_count() * shield_val
            score += b_ring_att * ring_val
            if not (bp & self._file_bb[bksq & 7]):
                score += open_val
        return score

    def _py_shelter(self, ksq, own_pawns, is_white, sc, sf):
        """Python mirror of C compute_shelter: per-file, per-distance pawn bonus."""
        score = 0
        kf = ksq & 7
        kr = ksq >> 3
        for df in (-1, 0, 1):
            f = kf + df
            if not (0 <= f <= 7):
                continue
            fmask = self._file_bb[f]
            if is_white:
                # ranks strictly above kr
                below_incl = (1 << ((kr + 1) * 8)) - 1 if kr < 7 else (1 << 64) - 1
                ahead = own_pawns & fmask & ~below_incl & ((1 << 64) - 1)
                if not ahead:
                    continue
                psq = (ahead & -ahead).bit_length() - 1  # lowest set bit
                dist = (psq >> 3) - kr
            else:
                # ranks strictly below kr
                above_incl = ~((1 << (kr * 8)) - 1) & ((1 << 64) - 1) if kr > 0 else (1 << 64) - 1
                ahead = own_pawns & fmask & ~above_incl & ((1 << 64) - 1)
                if not ahead:
                    continue
                psq = ahead.bit_length() - 1  # highest set bit
                dist = kr - (psq >> 3)
            if dist == 1:
                score += sc
            elif dist == 2:
                score += sf
        return score

    # ------------------------------------------------------------------ #
    # Mobility: per-piece reachable-square count (own pieces excluded).
    # ------------------------------------------------------------------ #
    def _mobility_bb(self, board, occ_w, occ_b, knights, bishops, rooks, queens,
                     wp=0, bp=0):
        """Per-piece mobility (low-phase path). #3.x: when use_mobility_area
        is on, subtract enemy-pawn-attacked squares from each piece's
        count; when use_threats is on, also add pawn-threat and
        minor-on-major bonuses. ``wp``/``bp`` default to 0 only so old
        callers that didn't pass them don't break; the production
        dispatcher in _eval_positional_white always supplies them."""
        score = 0
        am = board.attacks_mask
        # Pawn attack sets used by mobility-area AND threats.
        FA = ~int(chess.BB_FILE_A) & ((1 << 64) - 1)
        FH = ~int(chess.BB_FILE_H) & ((1 << 64) - 1)
        patk_w = ((wp << 9) & FA) | ((wp << 7) & FH)
        patk_b = ((bp >> 7) & FA) | ((bp >> 9) & FH)
        if self.use_mobility_area:
            w_safe = ~occ_w & ~patk_b & ((1 << 64) - 1)
            b_safe = ~occ_b & ~patk_w & ((1 << 64) - 1)
        else:
            w_safe = ~occ_w & ((1 << 64) - 1)
            b_safe = ~occ_b & ((1 << 64) - 1)
        w_minor_atk = 0
        b_minor_atk = 0
        for pt, bb in ((chess.KNIGHT, knights), (chess.BISHOP, bishops),
                       (chess.ROOK, rooks), (chess.QUEEN, queens)):
            w = self.MOBILITY_WEIGHT[pt]
            is_minor = pt in (chess.KNIGHT, chess.BISHOP)
            for sq in chess.scan_forward(bb & occ_w):
                a = am(sq)
                score += w * (a & w_safe).bit_count()
                if is_minor:
                    w_minor_atk |= a
            for sq in chess.scan_forward(bb & occ_b):
                a = am(sq)
                score -= w * (a & b_safe).bit_count()
                if is_minor:
                    b_minor_atk |= a
        if self.use_threats:
            b_non_pawn = occ_b & ~bp
            w_non_pawn = occ_w & ~wp
            b_major = (rooks | queens) & occ_b
            w_major = (rooks | queens) & occ_w
            score += self.THREAT_PAWN  * (patk_w & b_non_pawn).bit_count()
            score -= self.THREAT_PAWN  * (patk_b & w_non_pawn).bit_count()
            score += self.THREAT_MINOR * (w_minor_atk & b_major).bit_count()
            score -= self.THREAT_MINOR * (b_minor_atk & w_major).bit_count()
        return score

    # ------------------------------------------------------------------ #
    # Rook on open / semi-open file, and the bishop-pair bonus.
    # ------------------------------------------------------------------ #
    def _rook_files_bb(self, rooks, occ_w, occ_b, wp, bp):
        score = 0
        for sq in chess.scan_forward(rooks & occ_w):
            fmask = self._file_bb[sq & 7]
            if not (wp & fmask):
                score += self.ROOK_OPEN_FILE if not (bp & fmask) else self.ROOK_SEMIOPEN_FILE
        for sq in chess.scan_forward(rooks & occ_b):
            fmask = self._file_bb[sq & 7]
            if not (bp & fmask):
                score -= self.ROOK_OPEN_FILE if not (wp & fmask) else self.ROOK_SEMIOPEN_FILE
        return score

    def _rook_on_7th_bb(self, rooks, occ_w, occ_b, wp, bp, kings, phase):
        """#3.x: per-rook bonus for sitting on the side's 7th rank, gated
        by enemy king on its back rank OR an enemy pawn still on its 7th.
        Phased blend (mg, eg). Returns 0 if the toggle is off, so callers
        don't need to gate themselves -- a hot-path branch hides the cost
        of an OFF toggle behind one ``and``."""
        if not self.use_rook_on_7th or self.PHASE_MAX <= 0:
            return 0
        r7v = (self.ROOK_ON_7TH_MG * phase
               + self.ROOK_ON_7TH_EG * (self.PHASE_MAX - phase)) // self.PHASE_MAX
        score = 0
        w7 = rooks & occ_w & int(chess.BB_RANK_7)
        if w7:
            bk = kings & occ_b
            if (bk & int(chess.BB_RANK_8)) or (bp & int(chess.BB_RANK_7)):
                score += r7v * w7.bit_count()
        b7 = rooks & occ_b & int(chess.BB_RANK_2)
        if b7:
            wk = kings & occ_w
            if (wk & int(chess.BB_RANK_1)) or (wp & int(chess.BB_RANK_2)):
                score -= r7v * b7.bit_count()
        return score

    def _pinned_for(self, board, color, own_occ):
        """Bitboard of ``color``'s pieces absolutely pinned to their own king.

        Same logic as python-chess's internal ``_slider_blockers`` but for an
        explicit colour (that method hard-codes the side to move), so it works
        for both kings in one static evaluation."""
        king = board.king(color)
        if king is None:
            return 0
        rq = board.rooks | board.queens
        bq = board.bishops | board.queens
        snipers = ((chess.BB_RANK_ATTACKS[king][0] & rq)
                   | (chess.BB_FILE_ATTACKS[king][0] & rq)
                   | (chess.BB_DIAG_ATTACKS[king][0] & bq)) & board.occupied_co[not color]
        occupied = board.occupied
        blockers = 0
        for sniper in chess.scan_forward(snipers):
            between = chess.between(king, sniper) & occupied
            if between and not (between & (between - 1)):   # exactly one piece between
                blockers |= between
        return blockers & own_occ

    def _pin_penalty_bb(self, board, occ_w, occ_b):
        """Penalty for absolutely-pinned pieces (pinned to their own king)."""
        score = 0
        for sq in chess.scan_forward(self._pinned_for(board, chess.WHITE, occ_w)):
            score -= self.PIN_PENALTY.get(board.piece_type_at(sq), 0)
        for sq in chess.scan_forward(self._pinned_for(board, chess.BLACK, occ_b)):
            score += self.PIN_PENALTY.get(board.piece_type_at(sq), 0)
        return score

    def _simplify_bb(self, occ_w, occ_b, pawns, knights, bishops, rooks, queens):
        """Encourage the materially-leading side to trade pieces (head for a won
        endgame): once the lead is decisive, reward the leader for every
        minor/major piece already off the board. White's perspective."""
        def mat(occ):
            return self._npm(pawns, knights, bishops, rooks, queens, occ)  # W-14
        diff = mat(occ_w) - mat(occ_b)
        if abs(diff) < self.SIMPLIFY_THRESHOLD:
            return 0
        pieces = (knights | bishops | rooks | queens).bit_count()   # 0..14
        sign = 1 if diff > 0 else -1
        return sign * self.SIMPLIFY_WEIGHT * (14 - pieces)

    def _bishop_pair_bb(self, bishops, occ_w, occ_b, phase):
        pm = self.PHASE_MAX
        bp = (self.BISHOP_PAIR_MG * phase + self.BISHOP_PAIR_EG * (pm - phase)) // pm
        score = 0
        if (bishops & occ_w).bit_count() >= 2:
            score += bp
        if (bishops & occ_b).bit_count() >= 2:
            score -= bp
        return score

    # ------------------------------------------------------------------ #
    # Endgame mop-up: drive the weak king to the edge, bring our king up.
    # Engaged only with a decisive non-pawn material edge, so it cannot
    # distort balanced or drawish endings.
    # ------------------------------------------------------------------ #
    def _mopup_bb(self, occ_w, occ_b, knights, bishops, rooks, queens, kings,
                  strong=False):
        npm_w = self._npm(0, knights, bishops, rooks, queens, occ_w)  # W-14
        npm_b = self._npm(0, knights, bishops, rooks, queens, occ_b)
        adv = npm_w - npm_b
        if abs(adv) < self.MOPUP_MIN_ADV:
            return 0
        wk = (kings & occ_w).bit_length() - 1      # white king square
        bk = (kings & occ_b).bit_length() - 1      # black king square
        loser = bk if adv > 0 else wk
        md = abs((wk & 7) - (bk & 7)) + abs((wk >> 3) - (bk >> 3))  # king Manhattan
        # ``strong`` (bare-king mating) cranks the weights so the king-driving
        # gradient dominates the search instead of being washed out by the noise
        # of the extra winning material -- which is what made the engine shuffle
        # a winning K+Q(+B/P) vs K into a draw.
        cmd_w = self.MOPUP_CMD_WEIGHT
        king_w = self.MOPUP_KING_WEIGHT
        if strong:
            cmd_w = self.MOPUP_STRONG_CMD_WEIGHT
            king_w = self.MOPUP_STRONG_KING_WEIGHT
        bonus = cmd_w * self._center_manhattan[loser] + king_w * (14 - md)
        return bonus if adv > 0 else -bonus

    # ------------------------------------------------------------------ #
    # Material / contempt helpers (used by the draw-avoidance logic).
    # ------------------------------------------------------------------ #
    def _npm(self, pawns, knights, bishops, rooks, queens, occ):
        """W-14: single source of the piece-value material sum used by the
        mop-up / simplify / material-diff paths (was inlined 4x, a silent
        desync hazard at the next material retune). Sourced from
        PIECE_VALUES so a retune propagates everywhere. Pass pawns=0 for the
        non-pawn-material (mop-up gate) callers. Value-identical to the old
        hard-coded 100/320/330/500/900."""
        PV = self.PIECE_VALUES
        return (PV[1] * (pawns & occ).bit_count()
                + PV[2] * (knights & occ).bit_count()
                + PV[3] * (bishops & occ).bit_count()
                + PV[4] * (rooks & occ).bit_count()
                + PV[5] * (queens & occ).bit_count())

    def _material_diff_stm(self, board):
        """Side-to-move material balance in centipawns (+ve = stm is ahead)."""
        occ_w = board.occupied_co[chess.WHITE]
        occ_b = board.occupied_co[chess.BLACK]

        def side(occ):
            return self._npm(board.pawns, board.knights, board.bishops,
                             board.rooks, board.queens, occ)

        diff = side(occ_w) - side(occ_b)
        return diff if board.turn == chess.WHITE else -diff

    def _draw_score(self, board):
        """Contempt-adjusted draw value from the side-to-move's perspective.

        Negative when the side to move is clearly ahead (so it steers away from
        repetitions / 50-move draws while winning) and positive when clearly
        behind (so it is happy to hold the draw). Zero when roughly balanced,
        matching a normal draw."""
        diff = self._material_diff_stm(board)
        if diff >= self.DRAW_AVOID_MARGIN:
            return -self.CONTEMPT
        if diff <= -self.DRAW_AVOID_MARGIN:
            return self.CONTEMPT
        return 0

    # ================================================================== #
    # Move ordering
    # ================================================================== #
    def order_moves(self, board, tt_move=None, ply=0, counter=None,
                    prev_move=None, pm1=0, pm2=0):
        """Legal moves ordered best-first: TT move, MVV-LVA captures /
        promotions, killers, then the history heuristic for quiet moves.
        Returns the sorted ``scored`` rows directly -- a list of
        ``(score, move, raw, see)`` tuples (P-19: the three parallel lists
        this used to build, plus the caller's re-zip, were pure per-node
        allocation overhead). ``raw`` is the packed uint32 tag (#2.3);
        ``see`` is an already-computed SEE value or None if this loop didn't
        compute one for that move (roadmap item #14).

        FIX: ``board.gives_check`` is intentionally *not* used here -- it is
        expensive and was a major source of the ordering cost. Check detection
        is done once, cheaply, after the move is pushed in the search.

        The sort key is the module-level ``_FIRST`` itemgetter rather than an
        inline lambda: itemgetter is implemented in C and avoids Python frame
        setup per comparison, which matters because this sort runs at every
        interior search node.

        # #1.6: when ``prev_move`` is supplied (the move played to reach this
        node) the per-quiet score is augmented with the 1-ply continuation
        history; if ``ply >= 2`` and ``_move_stack[ply-2]`` is set, the 2-ply
        continuation history is added too. Both bias quiet ordering toward
        moves that have repeatedly followed the same predecessor well.

        # #2.3: when raws come from the C generator the capture branch reads
        mover_pt / victim_pt / is_ep straight out of the move word -- no
        is_capture / is_en_passant / piece_type_at calls per move. The
        in-check / no-movegen fallback path synthesises raws from board
        queries so downstream consumers see one uniform shape.
        """
        k0 = self.killer_0[ply]                   # P-22: list indexing
        k1 = self.killer_1[ply]
        color = board.turn
        # P-24: identity via 15-bit ints -- `raw & 0x7FFF` carries exactly
        # from|to<<6|promo<<12, the same fields Move.__eq__ compares (drop is
        # always None in standard chess). -1 never matches any raw. Works
        # uniformly on the in-check fallback too: _synth_raws tags those.
        tt_key15 = (tt_move.from_square | (tt_move.to_square << 6)
                    | ((tt_move.promotion or 0) << 12)) if tt_move is not None else -1
        k0_key15 = (k0.from_square | (k0.to_square << 6)
                    | ((k0.promotion or 0) << 12)) if k0 is not None else -1
        k1_key15 = (k1.from_square | (k1.to_square << 6)
                    | ((k1.promotion or 0) << 12)) if k1 is not None else -1
        ct_key15 = (counter.from_square | (counter.to_square << 6)
                    | ((counter.promotion or 0) << 12)) if counter is not None else -1
        scored = []
        # #9 + #2.3: C move generator returns moves AND raws (parallel lists);
        # None => in check, fall back to python-chess and synthesise raws so
        # the search loop's tag reads keep working uniformly.
        if self.use_c_movegen:
            moves, raws = _c_legal_moves(board)
            if moves is None:
                moves = list(board.legal_moves)
                raws = self._synth_raws(board, moves)
        else:
            moves = list(board.legal_moves)
            raws = self._synth_raws(board, moves)
        # #1.6: pre-compute the predecessor coordinates once. `pm1` keys the
        # 1-ply continuation table; `pm2` (the move played at ply-2 -- i.e.
        # the same side's previous move) keys the 2-ply table.
        # W-08: sentinel 0 = "not supplied, compute here" (None is a VALID
        # value meaning "no predecessor in scope"). _negamax computes both
        # once and passes them in, so the identical block isn't run twice.
        if pm1 == 0:
            pm1 = (prev_move.from_square | (prev_move.to_square << 6)
                   ) if prev_move is not None else None  # Y-06: int pm key
        if pm2 == 0:
            pm2 = None
            if ply >= 2:
                pm2_move = self._move_stack[ply - 2]
                if pm2_move is not None:
                    pm2 = pm2_move.from_square | (pm2_move.to_square << 6)
        cont1 = self.cont_history
        cont2 = self.cont_history_2
        PV = self.PIECE_VALUES
        # U-01: hoist the two per-move flag reads next to the PV hoist.
        use_capt_hist = self.use_capt_history
        use_see = self.use_see
        for move, raw in zip(moves, raws):
            score = 0
            # Roadmap item #14: remember the SEE value when this loop computes
            # one, so the SEE-prune-captures gate in _negamax's move loop
            # (frontier nodes, depth<=2) can reuse it for the SAME move at the
            # SAME position instead of recomputing from scratch. None means
            # "not computed here" -- the gate's own condition doesn't exactly
            # match this loop's (value- vs piece-type-based mover>victim
            # pre-filter), so on a cache miss it still falls back to computing
            # SEE itself, exactly as before; this only removes the duplicate
            # call on the (overwhelmingly common) case where both agree.
            see = None
            rid = raw & 0x7FFF                    # P-24: 15-bit move identity
            if rid == tt_key15:
                score = 2_000_000
                scored.append((score, move, raw, see))
                continue
            victim_pt = (raw >> MV_SHIFT_VICTIM) & MV_MASK_PT
            if victim_pt != 0:                                   # capture (#2.3)
                mover_pt = (raw >> MV_SHIFT_MOVER) & MV_MASK_PT
                victim_value = PV[victim_pt]
                mover_value = PV[mover_pt]
                score = 1_000_000 + victim_value * 16 - mover_value   # MVV-LVA
                if use_capt_hist:
                    score += self.capt_history[
                        _capt_hist_key(mover_pt, move.to_square, victim_pt)]
                promo_pt = (raw >> 12) & 7        # X-08: promo from the tag bits
                if promo_pt:
                    score += PV[promo_pt]
                # SEE: demote captures that lose material below the killer /
                # counter-move band, so a losing capture is no longer tried
                # ahead of a quiet refutation. Only run SEE when the mover
                # outweighs the victim -- otherwise the exchange cannot lose
                # material (SEE >= 0) and the call would be wasted.
                elif use_see and mover_value > victim_value:
                    see = self._see_raw(board, raw)
                    if see < 0:
                        score = self.SEE_LOSING_CAPTURE + see
            elif raw & 0x7000:                    # X-08: promo bits 12-14
                score = 900_000 + PV[(raw >> 12) & 7]
            elif rid == k0_key15:
                score = 800_000
            elif rid == k1_key15:
                score = 799_999
            elif rid == ct_key15:
                score = 780_000             # counter-move heuristic (just below killers)
            else:
                frm = raw & 63                    # P-28: from/to from the tag
                to = (raw >> 6) & 63
                hkey = _hist_key(color, frm, to)  # Y-06: one key, three tables
                score = self.history[hkey]
                # Continuation history (#1.6): add 1-ply and 2-ply scores
                # for the same predecessor when one is in scope.
                if pm1 is not None:
                    arr = cont1.get(pm1)
                    if arr is not None:
                        score += arr[hkey]
                if pm2 is not None:
                    arr = cont2.get(pm2)
                    if arr is not None:
                        score += arr[hkey]
            scored.append((score, move, raw, see))
        scored.sort(key=_FIRST, reverse=True)
        return scored                     # P-19: rows, not three rebuilt lists

    def _synth_raws(self, board, moves):
        """Build the #2.3 raw uint32 for each move when we can't get them from
        the C generator -- in-check evasions (python-chess's special order),
        or the no-C-movegen fallback. Matches movegen.c's MOVE_TAG layout.

        Pays the per-move board.piece_type_at / is_en_passant cost the C path
        avoids, but this branch is rare (in-check nodes only) so the search
        loop downstream still wins.
        """
        out = []
        for mv in moves:
            frm = mv.from_square
            to = mv.to_square
            mover_pt = board.piece_type_at(frm) or 0
            ep = 1 if board.is_en_passant(mv) else 0
            if ep:
                victim_pt = chess.PAWN
            elif board.is_capture(mv):
                victim_pt = board.piece_type_at(to) or 0
            else:
                victim_pt = 0
            promo = mv.promotion or 0
            raw = (frm | (to << 6) | (promo << 12)
                   | (mover_pt << MV_SHIFT_MOVER)
                   | (victim_pt << MV_SHIFT_VICTIM)
                   | (MV_BIT_EP if ep else 0))
            out.append(raw)
        return out

    def _capture_moves(self, board):
        """Legal captures and promotions, ordered best-first (for quiescence).
        Returns ``(moves, raws)`` -- like ``order_moves`` / ``_c_capture_moves``
        -- so the caller can read victim_pt/mover_pt/is_ep straight from the
        already-computed tag instead of re-querying the board per move (roadmap
        item #10: this function computed those tags for scoring/ordering below
        and used to discard them before returning, forcing the quiescence loop
        -- ~50% of all nodes -- to re-derive the same information via
        ``board.is_en_passant`` + two ``piece_type_at`` calls per capture).

        Uses python-chess's dedicated capture generator plus the (rare)
        non-capturing promotions, instead of generating *all* legal moves and
        discarding the quiet ones -- a real saving on the quiescence hot path,
        where most legal moves are quiet.

        Ordering is MVV-LVA, refined by SEE when ``use_qsee_order`` is on: a
        capture whose mover outweighs its victim (so the exchange *might* lose
        material) is run through SEE and, if it loses, demoted below every sound
        capture. That is exactly where plain MVV-LVA misleads (e.g. QxP onto a
        defended pawn looks like a pawn win but is a queen loss). SEE is computed
        only for that questionable subset -- clearly-winning captures
        (victim >= mover, SEE >= 0) never pay for it -- so the cost is bounded.
        Pure reordering: the value quiescence returns is unchanged (the loop's
        delta/SEE prunes are exact), only the node count moves.
        """
        # #9 + #2.3: C capture generator returns (moves, raws). Only reached
        # when not in check, so no evasion handling. raws give us mover_pt /
        # victim_pt / is_ep without any board queries in the scoring loop.
        if self.use_c_movegen:
            moves, raws = _c_capture_moves(board)
        else:
            own = board.occupied_co[board.turn]
            promo_rank = chess.BB_RANK_8 if board.turn == chess.WHITE else chess.BB_RANK_1
            moves = list(board.generate_legal_captures())       # incl. e.p. + capture-promos
            for m in board.generate_legal_moves(board.pawns & own, promo_rank):
                if not board.is_capture(m):                      # non-capturing promotions
                    moves.append(m)
            raws = self._synth_raws(board, moves)

        scored = []
        PV = self.PIECE_VALUES
        for move, raw in zip(moves, raws):
            victim_pt = (raw >> MV_SHIFT_VICTIM) & MV_MASK_PT
            mover_pt = (raw >> MV_SHIFT_MOVER) & MV_MASK_PT
            victim_value = PV[victim_pt] if victim_pt else 0
            mover_value = PV[mover_pt] if mover_pt else 0
            promo_pt = (raw >> 12) & 7            # X-08: promo from the tag bits
            promo = PV[promo_pt] if promo_pt else 0
            score = victim_value * 16 - mover_value + promo      # MVV-LVA base
            # SEE refinement for questionable captures only (see docstring).
            if (self.use_qsee_order and victim_pt and not promo_pt
                    and self.use_see and mover_value > victim_value):
                see = self._see_raw(board, raw)
                if see < 0:                                      # losing -> sink it
                    score = self.SEE_LOSING_CAPTURE + see - 1_000_000
            scored.append((score, move, raw, victim_value))   # V-07: carry it
        scored.sort(key=_FIRST, reverse=True)
        return scored                     # Z-04: rows (+ V-07 victim_value)

    # ------------------------------------------------------------------ #
    # Static Exchange Evaluation (SEE)
    # ------------------------------------------------------------------ #
    def _see(self, board, move):
        """Net material (centipawns) won by the capture ``move`` -- see
        ``_see_py``'s docstring. Dispatches to the C port (roadmap item #15)
        when available; falls back to the pure-Python implementation
        otherwise. Behaviour is identical either way (verified over a large
        random position set including en passant and x-ray sequences)."""
        if _USE_C_EVAL:
            board_turn = board.turn
            return _C_SEE(
                board.pawns, board.knights, board.bishops, board.rooks,
                board.queens, board.kings,
                board.occupied_co[True], board.occupied_co[False],
                1 if board_turn else 0,
                move.from_square, move.to_square,
                1 if board.is_en_passant(move) else 0,
            )
        return self._see_py(board, move)

    def _see_raw(self, board, raw):
        """_see for callers holding the packed raw word (#2.3): from/to and
        the en-passant flag come straight from the tag bits, skipping the
        per-call ``board.is_en_passant`` (P-25). Identical result to ``_see``
        for the same move by construction -- the ep bit is set exactly when
        the C generator / _synth_raws tagged the move en passant (verified
        differentially at the #10 port and again for this change)."""
        if _USE_C_EVAL:
            return _C_SEE(
                board.pawns, board.knights, board.bishops, board.rooks,
                board.queens, board.kings,
                board.occupied_co[True], board.occupied_co[False],
                1 if board.turn else 0,
                raw & 63, (raw >> 6) & 63,
                1 if raw & MV_BIT_EP else 0,
            )
        return self._see_py(board, _interned_move(raw & 0x7FFF))

    def _see_py(self, board, move):
        """Net material (centipawns) won by the capture ``move`` if both sides
        keep recapturing on its target square with their least-valuable
        attacker.

        Positive => the capture wins material; negative => it loses material
        even under the optimal recapture sequence. Each side may stand pat at
        any point (the ``-max`` fold below), so a capture is never scored worse
        than its best stopping point. X-ray attackers behind a removed piece are
        picked up automatically because the attacker set is recomputed against
        the shrinking occupancy after every capture.
        """
        to_sq = move.to_square
        from_sq = move.from_square
        values = self.PIECE_VALUES

        if board.is_en_passant(move):
            target_value = values[chess.PAWN]
            ep_sq = to_sq + (-8 if board.turn == chess.WHITE else 8)
        else:
            # #2: piece_type_at -> bare int (no Piece alloc); None => not a capture.
            victim_pt = board.piece_type_at(to_sq)
            if victim_pt is None:
                return 0                       # not a capture
            target_value = values[victim_pt]
            ep_sq = None

        attacker_pt = board.piece_type_at(from_sq)
        if attacker_pt is None:
            return 0
        attacker_value = values[attacker_pt]

        # Occupancy with the initial attacker (and any e.p. victim) removed.
        occupied = board.occupied & ~chess.BB_SQUARES[from_sq]
        if ep_sq is not None:
            occupied &= ~chess.BB_SQUARES[ep_sq]

        side = not board.turn                  # side to recapture next
        attackers = self._see_attackers(board, to_sq, occupied)

        gain = self._see_gain
        gain[0] = target_value
        d = 0
        while True:
            d += 1
            gain[d] = attacker_value - gain[d - 1]
            side_attackers = attackers & board.occupied_co[side] & occupied
            if not side_attackers:
                break
            lva_sq, attacker_value = self._least_valuable_attacker(board, side_attackers)
            occupied &= ~chess.BB_SQUARES[lva_sq]
            attackers = self._see_attackers(board, to_sq, occupied)
            side = not side
            if d >= 31:                        # safety: never overflow gain[]
                break

        # Fold the gain list back to the root capture, honouring each side's
        # option to stop capturing rather than continue into a losing exchange.
        while d > 1:
            d -= 1
            gain[d - 1] = -max(-gain[d - 1], gain[d])
        return gain[0]

    def _see_attackers(self, board, square, occupied):
        """Bitboard of every piece (either colour) in ``occupied`` that attacks
        ``square``. Recomputed against a shrinking occupancy inside the SEE swap
        loop so x-ray (battery) attackers appear naturally as front pieces are
        removed."""
        bishops_queens = board.bishops | board.queens
        rooks_queens = board.rooks | board.queens
        diag = chess.BB_DIAG_ATTACKS[square][chess.BB_DIAG_MASKS[square] & occupied]
        rank = chess.BB_RANK_ATTACKS[square][chess.BB_RANK_MASKS[square] & occupied]
        file_ = chess.BB_FILE_ATTACKS[square][chess.BB_FILE_MASKS[square] & occupied]
        attackers = (
            (chess.BB_KNIGHT_ATTACKS[square] & board.knights)
            | (chess.BB_KING_ATTACKS[square] & board.kings)
            | (chess.BB_PAWN_ATTACKS[chess.BLACK][square]
               & board.pawns & board.occupied_co[chess.WHITE])
            | (chess.BB_PAWN_ATTACKS[chess.WHITE][square]
               & board.pawns & board.occupied_co[chess.BLACK])
            | (diag & bishops_queens)
            | ((rank | file_) & rooks_queens)
        )
        return attackers & occupied

    def _recapture_at(self, board, move, is_capture, last_cap_sq):
        """True if ``move`` recaptures on the square the parent just captured on
        *and* the configured ``recapture_ext`` mode admits it (see __init__)."""
        if not is_capture or last_cap_sq is None or move.to_square != last_cap_sq:
            return False
        mode = self.recapture_ext
        if mode == "off":
            return False
        if mode == "see":
            return self._see(board, move) >= 0
        return True                                   # "all"

    def _least_valuable_attacker(self, board, attackers):
        """(square, value) of the cheapest piece in ``attackers`` (the caller
        has already masked it down to a single colour)."""
        for piece_type, bb in (
            (chess.PAWN, board.pawns), (chess.KNIGHT, board.knights),
            (chess.BISHOP, board.bishops), (chess.ROOK, board.rooks),
            (chess.QUEEN, board.queens), (chess.KING, board.kings),
        ):
            subset = attackers & bb
            if subset:
                return chess.lsb(subset), self.PIECE_VALUES[piece_type]
        return None, 0

    def _store_killer(self, move, ply):
        if self.killer_0[ply] == move or self.killer_1[ply] == move:
            return
        self.killer_1[ply] = self.killer_0[ply]
        self.killer_0[ply] = move

    def _update_history(self, color, move, bonus):
        """Nudge the (color, from, to) history score by ``bonus`` (which may be
        negative for a malus), damped toward zero so it can never run away.

        Gravity: ``new = old + bonus - old*|bonus|/HISTORY_MAX``. Near zero this
        is just ``old + bonus``; as ``|old|`` approaches HISTORY_MAX the pull
        back toward zero cancels the bonus, bounding the score to that range."""
        key = _hist_key(color, move.from_square, move.to_square)
        old = self.history[key]
        self.history[key] = old + bonus - old * abs(bonus) // self.HISTORY_MAX

    def _update_capt_history(self, mover_pt, to_sq, victim_pt, bonus):
        """Gravity update for capture history. Key is (mover_pt, to_sq,
        victim_pt) so equal-MVV-LVA captures on the same square are ranked by
        which piece type historically caused cutoffs there."""
        key = _capt_hist_key(mover_pt, to_sq, victim_pt)
        old = self.capt_history[key]
        self.capt_history[key] = old + bonus - old * abs(bonus) // self.CAPT_HISTORY_MAX

    def _update_cont_history(self, table, pmk, hkey, bonus):
        """Same gravity rule as _update_history, applied to a continuation
        table. Y-06: ``table`` maps the predecessor key (pm_from|pm_to<<6) to
        a lazily-allocated 8192-slot flat list indexed by _hist_key -- same
        logical mapping as the old 25-bit dict, C-speed list indexing."""
        arr = table.get(pmk)
        if arr is None:
            arr = table[pmk] = [0] * 8192
        old = arr[hkey]
        arr[hkey] = old + bonus - old * abs(bonus) // self.HISTORY_MAX

    # ================================================================== #
    # Iterative-deepening driver
    # ================================================================== #
    def get_best_move(self, board, depth):
        """Best move via iterative deepening to a fixed ``depth`` (no clock)."""
        return self._search(board, max_depth=max(1, depth), time_limit=None)

    def get_best_move_timed(self, board, time_limit, max_depth=10):
        """Best move via iterative deepening bounded by ``time_limit`` seconds.

        # #13 Lazy SMP: when ``smp_workers > 1`` (or a pool was attached via
        ``_smp_pool``), the search runs across a PERSISTENT pool of worker
        processes sharing a lock-free TT. The pool is spawned ONCE (lazily, on
        first use) and reused for every later move -- no per-move spawning or
        __main__ re-import. The deepest-completed worker result is returned.

        Spawning is REFUSED (transparent fall back to the single-threaded
        search) when it would be unsafe or impossible:
          * CLAUDECHESS_SMP_CHILD set -- we are a descendant of an SMP spawn
            (fork-bomb guard; set only around a real spawn, inherited from birth,
            so it catches the recursive __main__ re-import case);
          * off the main thread -- e.g. a GUI background search;
          * inside a daemonic process -- multiprocessing forbids daemons from
            having children (this is what crashed under match.py until its
            EngineProcess was made non-daemon)."""
        # Lazily build the persistent pool the first time SMP is wanted here.
        if self._smp_pool is None and self.smp_workers > 1:
            import threading
            import multiprocessing
            if (os.environ.get("CLAUDECHESS_SMP_CHILD")
                    or threading.current_thread() is not threading.main_thread()
                    or multiprocessing.current_process().daemon):
                return self._search(board, max_depth=max_depth, time_limit=time_limit)
            from smp import SMPPool           # lazy: avoids an engine<->smp import cycle
            self._smp_pool = SMPPool(self.smp_workers)
        if self._smp_pool is not None:
            # Workers already run; this only hands them a position over a queue
            # (no spawn here), so it is safe even from a GUI background thread.
            # max_depth passes through unchanged: a caller's shallow cap must
            # mean the same thing under SMP as single-threaded.
            move, info = self._smp_pool.search(board, time_limit, max_depth,
                                               config=self._smp_config())
            valid = [r for r in info if r[4] is not None]
            if move is None or not valid:
                # P-03: pool degraded (dead workers / collection timeout) --
                # never hang or answer with a null move; search here instead.
                return self._search(board, max_depth=max_depth,
                                    time_limit=time_limit)
            from smp import pick_best         # lazy: engine<->smp import cycle
            best = pick_best(valid, board.turn == chess.WHITE)   # P-02: stm-relative tie-break
            self.last_depth, self.last_score = best[1], best[2]
            self.nodes = sum(r[3] for r in info)   # aggregate parallel work
            return move
        return self._search(board, max_depth=max_depth, time_limit=time_limit)

    def _search(self, board, max_depth, time_limit):
        # Reset per-move statistics and tables.
        self.nodes = 0
        # NPS: bind _tt_get/_tt_store to this instance's one active TT
        # strategy for the whole search (see _bind_tt_strategy) instead of
        # re-checking the same-every-time toggles on every node.
        self._bind_tt_strategy()
        # Transposition table persists ACROSS moves so the previous move's tree
        # (the opponent usually plays an expected reply) is reused -- a sizable
        # speedup in the middlegame/endgame. It is dropped only after an
        # irreversible move: halfmove_clock == 0 means the last move was a pawn
        # move or capture, so no earlier position can ever recur and those
        # entries are dead. A size cap bounds memory. Mate scores are stored
        # position-relative (see _tt_value_to/from), so they stay valid across
        # searches. (killers/history/countermoves are still reset each move.)
        if not self.use_shared_tt and (board.halfmove_clock == 0
                                       or len(self.tt) > self.TT_MAX_ENTRIES):
            self.tt = {}                       # shared TT: persistence/clearing is the orchestrator's job
        # New search generation: entries written by earlier moves now count as
        # "old" and become freely replaceable under depth-preferred replacement.
        self._tt_gen += 1
        self.killer_0 = [None] * (self.MAX_PLY + 2)   # P-22: see __init__
        self.killer_1 = [None] * (self.MAX_PLY + 2)
        self.history = [0] * 8192          # X-05: see __init__
        self.capt_history = [0] * 4096
        self.countermoves = [None] * 4096             # P-23: see __init__
        self.cont_history = {}
        self.cont_history_2 = {}
        # perf_counter, not time.time(): monotonic, so an NTP step mid-search
        # can't instantly abort the search or overrun the clock. Every elapsed
        # computation against start_time uses this same clock -- mixing the
        # two would be worse than either alone.
        self.start_time = time.perf_counter()
        self.time_limit = time_limit
        # Zeitnot: a sub-250 ms budget cannot be honored by the default
        # 4096-node time poll (~70 ms of nodes at baseline NPS) -- bind the
        # 8x finer variant for this search only. Method swap, not a per-node
        # branch (same rule as the _make/_tt binding).
        zeitnot = time_limit is not None and time_limit < 0.25
        self._check_time = (self._check_time_zeitnot if zeitnot
                            else self._check_time_std)
        # P-21: the mask gate is inlined at the two per-node call sites (one
        # AND+branch instead of a method call per node); the bound variant's
        # own mask check stays as a harmless backstop.
        self._poll_mask = 1023 if zeitnot else 4095
        self.search_log = []

        # Repetition tracking: count every position that has occurred from the
        # start of the game up to the root, so re-reaching any of them during
        # the search is detected as a repetition and scored as a draw (with
        # contempt -- see _draw_score). This is what lets a winning engine steer
        # away from threefold repetitions instead of shuffling into one.
        self._path = {}
        hist = board.copy()
        k = hist._transposition_key()
        self._path[k] = self._path.get(k, 0) + 1
        while hist.move_stack:
            hist.pop()
            k = hist._transposition_key()
            self._path[k] = self._path.get(k, 0) + 1

        root = board.copy()
        root_turn = root.turn
        legal = list(root.legal_moves)
        if not legal:
            self.nodes_searched = 0
            self.last_score = 0
            self.last_depth = 0
            return None

        # --- Opening book: play instantly if the position is in the book --- #
        book = self._book_move(root)
        if book is not None:
            self.nodes_searched = 0
            self.last_score = 0
            self.last_depth = 0
            record = {"depth": 0, "move": book.uci(), "score": 0,
                      "nodes": 0, "time_ms": 0, "book": True}
            self.search_log.append(record)
            if self.on_depth is not None:
                self.on_depth(record)
            final = dict(record)
            final["final"] = True
            if self.on_final is not None:
                self.on_final(final)
            return book

        # --- #4 Endgame tablebase: play the provably-optimal move instantly --
        # Bound the network wait so a tablebase MISS can never overrun the
        # move's time budget (a hit returns almost instantly anyway): wait at
        # most half the remaining clock when timed, else the full tb_timeout.
        tb_to = self.tb_timeout
        if time_limit is not None:
            elapsed = time.perf_counter() - self.start_time
            tb_to = min(tb_to, max(0.0, (time_limit - elapsed) * 0.5))
        tb = self._tb_probe(root, tb_to)
        if tb is not None:
            wdl, tb_move = tb                  # tb_move already verified legal in _tb_probe
            score_white = (wdl if root_turn == chess.WHITE else -wdl) * self.TB_SCORE_UNIT
            self.nodes_searched = 0
            self.last_score = score_white
            self.last_depth = 0
            record = {"depth": 0, "move": tb_move.uci(), "score": score_white,
                      "nodes": 0, "time_ms": 0, "tb": True, "wdl": wdl}
            self.search_log.append(record)
            if self.on_depth is not None:
                self.on_depth(record)
            final = dict(record)
            final["final"] = True
            if self.on_final is not None:
                self.on_final(final)
            return tb_move

        best_move = legal[0]
        best_score_white = 0
        reached_depth = 0
        pv_move = None
        prev_score = None

        # Seed the incremental-eval accumulator for the root and arm it; every
        # node from here maintains it via _make/_unmake. _search_root re-anchors
        # to _root_acc each iteration. Disarmed after the loop (below) so
        # external evaluate_position() calls fall back to the from-scratch scan.
        # _TimeUp is caught inside the loop, so the disarm always runs.
        self._root_acc = self._compute_acc(root)
        # Copy: _acc is mutated in place; aliasing it to _root_acc would
        # corrupt the root snapshot used to re-anchor each iteration.
        self._acc = self._root_acc[:]
        self._acc_stack = []
        self._acc_valid = True

        # #13: seed the incremental Zobrist for the root (SMP shared-TT key).
        # X-06: the acc-maintaining variants carry NO per-call check anymore;
        # pick acc/no-acc here, once per search, alongside the zob choice.
        inc = self.use_incremental_eval
        if self.use_zobrist or self.use_shared_tt:
            self._root_zob = self._compute_zobrist(root)
            self._zob = self._root_zob
            self._zob_stack = []
            self._zob_valid = True
            # NPS: bind the Zobrist-maintaining make/unmake variants for this
            # search only -- see the note above _make_nozob/_make_zob.
            self._make = self._make_zob if inc else self._make_zob_noacc
            self._make_null = (self._make_null_zob if inc
                               else self._make_null_zob_noacc)
            self._unmake = self._unmake_zob if inc else self._unmake_zob_noacc
        else:
            self._make = self._make_nozob if inc else self._make_nozob_noacc
            self._make_null = (self._make_null_nozob if inc
                               else self._make_null_nozob_noacc)
            self._unmake = (self._unmake_nozob if inc
                            else self._unmake_nozob_noacc)

        for depth in range(1, max_depth + 1):
            self._partial_root_move = None   # clear partial result for this depth
            try:
                score, move = self._search_root_aspiration(
                    root, depth, pv_move, prev_score)
            except _TimeUp:
                # Use the best root move found so far in the incomplete iteration
                # if any root moves were fully evaluated before time ran out.
                # Rationale: the PV move is always searched first, so either
                # (a) it's still best -> same move, no change, or (b) something
                # better was found -> take it. Only fall back to the previous
                # depth's move when no root move was completed at all this depth.
                if self._partial_root_move is not None:
                    best_move = self._partial_root_move
                break

            if move is not None:
                best_move = move
                pv_move = move
                reached_depth = depth
                prev_score = score
                best_score_white = score if root_turn == chess.WHITE else -score

                pv = self._extract_pv(root, best_move, depth)
                self.last_pv = pv
                record = {
                    "depth": depth,
                    "move": best_move.uci(),
                    "score": best_score_white,
                    "nodes": self.nodes,
                    "time_ms": int((time.perf_counter() - self.start_time) * 1000),
                    "pv": pv,
                }
                self.search_log.append(record)
                if self.on_depth is not None:
                    self.on_depth(record)

            if abs(score) > self.MATE_THRESHOLD:
                break       # forced mate found
            if time_limit is not None and (time.perf_counter() - self.start_time) >= time_limit:
                break

        # Disarm the accumulator: outside the search, evaluate_position() must
        # use the from-scratch scan (the live acc is only valid mid-search).
        self._acc_valid = False
        self._zob_valid = False
        # NPS: restore the branch-free no-Zobrist make/unmake as the default
        # (harmless no-op if they were never rebound this search).
        self._make = self._make_nozob_noacc       # X-06: acc is disarmed now
        self._make_null = self._make_null_nozob_noacc
        self._unmake = self._unmake_nozob_noacc

        self.nodes_searched = self.nodes
        self.last_score = best_score_white
        self.last_depth = reached_depth

        final_record = {
            "depth": reached_depth,
            "move": best_move.uci() if best_move is not None else "----",
            "score": best_score_white,
            "nodes": self.nodes,
            "time_ms": int((time.perf_counter() - self.start_time) * 1000),
            "pv": self.last_pv,
            "final": True,
        }
        if self.on_final is not None:
            self.on_final(final_record)
        return best_move

    def _extract_pv(self, board, first_move, max_len):
        """Reconstruct the principal variation (the line behind ``first_move``)
        by walking best-moves out of the transposition table, as a string like
        ``Nf3 Nc6 Bb5 a6`` (SAN) or ``g1f3 b8c6 ...`` (UCI, when ``pv_uci`` is
        set). Cheap (once per completed depth) and best-effort -- it stops when
        the TT has no entry, the move is stale, or a position repeats."""
        b = board.copy()
        out = []
        seen = set()
        mv = first_move
        while mv is not None and len(out) < max_len:
            if mv not in b.legal_moves:
                break
            try:
                out.append(mv.uci() if self.pv_uci else b.san(mv))
            except Exception:
                break
            b.push(mv)
            key = b._transposition_key()
            if key in seen:                  # repetition -> stop the walk
                break
            seen.add(key)
            if self.use_shared_tt and getattr(self, "_shared_tt", None) is not None:
                # X-01: _tt_get_shared keys on self._zob, which still holds the
                # ROOT hash during extraction -- probing it here would re-read
                # the root's slot every step. Walk with a per-position hash.
                data = self._shared_tt.get(self._compute_zobrist(b))
                raw15 = 0 if data is None else (data >> 32) & 0x7FFF
                mv = _interned_move(raw15) if raw15 else None
            else:
                entry = self._tt_get(key)
                mv = entry[3] if entry is not None else None
        return " ".join(out)

    # ------------------------------------------------------------------ #
    # Aspiration-window wrapper around the root search.
    # ------------------------------------------------------------------ #
    def _search_root_aspiration(self, board, depth, pv_move, prev_score):
        """Search the root with an aspiration window for deeper iterations.

        FIX: real aspiration windows (absent before). They narrow the window
        around the previous score and re-search only on fail-high/low, which
        is a net win once move ordering is good. We widen geometrically and
        fall back to a full window so we never loop forever.
        """
        if (depth < self.ASPIRATION_MIN_DEPTH or prev_score is None
                or abs(prev_score) >= self.MATE_THRESHOLD):
            return self._search_root(board, depth, pv_move, -self.INF, self.INF)

        delta = self.ASPIRATION_DELTA
        alpha = prev_score - delta
        beta = prev_score + delta
        while True:
            score, move = self._search_root(board, depth, pv_move, alpha, beta)
            if score <= alpha:                       # fail low: widen downward
                alpha = max(-self.INF, score - delta)
            elif score >= beta:                      # fail high: widen upward
                beta = min(self.INF, score + delta)
            else:
                return score, move
            delta *= 2
            if delta >= 2 * self.ASPIRATION_DELTA * 32:   # give up -> full window
                return self._search_root(board, depth, pv_move, -self.INF, self.INF)

    # ------------------------------------------------------------------ #
    # Root search: PVS over root moves with a random tiebreak among the
    # moves within TIEBREAK_MARGIN of the best.
    # ------------------------------------------------------------------ #
    def _search_root(self, board, depth, pv_move, alpha, beta):
        best_value = -self.INF
        best_move = None
        # (value, move, exact): `exact` is True only when `value` is a real
        # score, not a PVS scout upper bound. See the tiebreak note below.
        results = []
        a = alpha
        first = True

        # Each iteration / aspiration re-search starts from the root: re-anchor
        # the incremental accumulator so it can't drift across iterations.
        # Slice-copy so subsequent in-place mutations don't bleed into the
        # pristine root snapshot held by _root_acc.
        self._acc[:] = self._root_acc
        self._acc_stack = []
        if self._zob_valid:                    # #13: re-anchor the Zobrist too
            self._zob = self._root_zob
            self._zob_stack = []

        scored = self.order_moves(board, pv_move, 0)   # P-19: (score, move, raw, see) rows
        for _sc, move, raw, _see in scored:
            is_capture = ((raw >> MV_SHIFT_VICTIM) & MV_MASK_PT) != 0   # #2.3
            self._make(board, move, raw)
            child_last_cap = move.to_square if is_capture else None

            if first:
                value = -self._negamax(board, depth - 1, -beta, -a, 1,
                                       self.MAX_EXTENSIONS, child_last_cap)
                # Full-window search: exact only if it didn't fail low. A
                # fail-low first move returns an upper bound (<= alpha); if
                # we marked it `exact` it could leak into the random
                # tiebreak below and be played over a measurably better move.
                exact = value > alpha
            else:
                # PVS: scout with a null window, re-search on a fail-high.
                value = -self._negamax(board, depth - 1, -a - 1, -a, 1,
                                       self.MAX_EXTENSIONS, child_last_cap)
                if a < value < beta:
                    value = -self._negamax(board, depth - 1, -beta, -a, 1,
                                           self.MAX_EXTENSIONS, child_last_cap)
                    exact = True             # re-searched full window -> exact
                else:
                    # Scout fail-low: `value` is only an upper bound (<= a).
                    # The true score may be far lower, so it must NOT be
                    # trusted for the tiebreak.
                    exact = False
            self._unmake(board)

            results.append((value, move, exact))
            if value > best_value:
                best_value = value
                best_move = move
            self._partial_root_move = best_move   # track best seen so far this depth
            if value > a:
                a = value
            first = False
            if a >= beta:               # fail-high (aspiration re-search upstream)
                break

        # Random tiebreak among moves whose EXACT score is within the margin of
        # the best. Fail-low scout moves are excluded (their score is only an
        # upper bound), so a measurably worse move can never be chosen. This was
        # the bug behind weak picks like f2f3/h2h3: scout upper bounds landed
        # inside the margin and were wrongly treated as ties.
        if best_move is not None and best_value < self.MATE_THRESHOLD:
            near = [m for v, m, ex in results
                    if ex and v >= best_value - self.TIEBREAK_MARGIN]
            if len(near) > 1:
                best_move = random.choice(near)
        return best_value, best_move

    # ================================================================== #
    # Negamax with alpha-beta, TT, PVS, pruning and quiescence
    # ================================================================== #
    def _negamax(self, board, depth, alpha, beta, ply, ext_budget, last_cap_sq,
                 prev_move=None, chk_budget=None, in_check_hint=None):
        if chk_budget is None:
            chk_budget = self.MAX_CHECK_EXT
        self.nodes += 1
        if (self.nodes & self._poll_mask) == 0:   # P-21: inlined poll gate
            self._check_time()

        if ply >= self.MAX_PLY:
            return self._evaluate_stm(board)

        # Cheap draw detection. Bug fix (roadmap item #8): the halfmove_clock
        # check used to fire unconditionally, so a quiet move that delivers
        # checkmate WHILE also pushing the clock to 100 was scored as a draw
        # instead of a mate (FIDE: mate takes precedence over the 50-move
        # rule; python-chess's own is_seventyfive_moves() already excludes
        # mate the same way). `not board.is_check()` is a cheap pre-filter --
        # checkmate requires check, so this only falls through to the normal
        # (correct) checkmate/stalemate handling below in the rare case of an
        # in-check position at exactly the 50-move mark; insufficient-material
        # needs no such guard since it's a tautology (that side literally
        # cannot deliver mate with insufficient material). Also routed through
        # `_draw_score` (like the repetition check just below) instead of a
        # flat 0, for the same contempt-based draw avoidance/seeking.
        # P-27: insufficient material requires no pawn/rook/queen on the board
        # (exact pre-filter -- also covers the any-count same-color-bishops
        # case a popcount test would miss); one bitboard OR replaces the
        # python-chess call on ~all middlegame nodes.
        if ((not (board.pawns | board.rooks | board.queens)
                and board.is_insufficient_material())
                or (board.halfmove_clock >= 100 and not board.is_check())):
            return self._draw_score(board)

        # FIX: cheap internal position key instead of zobrist_hash (~22x faster).
        key = board._transposition_key()

        # Repetition: this exact position (same pieces, rights, side to move)
        # already occurred earlier on this line or in the game history. Treat
        # the first repetition as a draw -- contempt-scored so a winning side
        # avoids it and a losing side seeks it.
        if self._path.get(key):
            return self._draw_score(board)

        # --- Transposition-table probe --------------------------------- #
        tt_entry = self._tt_get(key)
        tt_move = None
        tt_eval = None                # cached static eval from a prior visit
        if tt_entry is not None:
            tt_depth, tt_flag, tt_value, tt_move, tt_eval, _tt_entry_gen = tt_entry
            if tt_depth >= depth:
                value = self._tt_value_from(tt_value, ply)
                if tt_flag == TT_EXACT:
                    return value
                if tt_flag == TT_LOWER and value > alpha:
                    alpha = value
                elif tt_flag == TT_UPPER and value < beta:
                    beta = value
                if alpha >= beta:
                    return value

        # Bug fix (roadmap item #3): snapshot alpha for the store-flag test
        # AFTER the TT probe's window narrowing above, not before. The move
        # loop below searches against the (possibly TT-raised) alpha, so a
        # fail-low result is only a valid upper bound relative to THIS alpha
        # -- using the pre-narrowing alpha here let a fail-low against a
        # raised alpha slip past `best_value <= alpha_orig` (since best_value
        # could sit between the two), landing in the TT_EXACT branch below and
        # storing a mere bound as if it were an exact score.
        alpha_orig = alpha

        # --- Leaf: quiescence search ----------------------------------- #
        if depth <= 0:
            return self._quiescence(board, alpha, beta, ply, in_check_hint)

        # X-02: the parent already computed this for us post-push
        # (gives_check); recompute only when entered without a hint (root
        # children and external callers).
        in_check = board.is_check() if in_check_hint is None else in_check_hint
        is_pv = (beta - alpha) > 1            # wide window => PV node

        # Static eval is needed for the pruning heuristics below; compute it
        # lazily. Skip it while in check (meaningless there) AND at PV nodes:
        # its only consumers -- reverse-futility, null-move and futility pruning
        # (plus the all-pruned fallback) -- are every one gated on `not is_pv`,
        # so at a PV node the full positional eval is computed and never read.
        # Skipping it there removes that wasted work and changes nothing about
        # the search (same nodes, scores and move). `lazy_pv_eval` lets the
        # benchmark A/B the eval-call count on identical code.
        static_eval = None
        if not in_check and not (self.lazy_pv_eval and is_pv):
            if self.tt_cached_eval and tt_eval is not None:
                static_eval = tt_eval                 # reuse: identical to recompute
            else:
                static_eval = self._evaluate_stm(board, key)   # W-07
        elif in_check and self.use_check_eval_proxy:
            # Use cached eval or alpha as a proxy so the improving heuristic
            # can track through check-evasion sequences.  Only feeds
            # _eval_stack / improving -- all pruning gates already require
            # not-in-check, so this never trips RFP / null / futility.
            static_eval = tt_eval if tt_eval is not None else alpha
        # Record for the "improving" heuristic (None at in-check / PV-skipped
        # plies is fine -- the comparison treats a missing reference as
        # not-improving, which is the conservative default).
        self._eval_stack[ply] = static_eval

        # `improving` is True when the side to move has a better static eval
        # than they did two plies ago (their own previous turn). We use it to:
        #   * shrink the effective RFP depth (more aggressive cut),
        #   * relax the futility margin's "useless" bar,
        #   * add +1 to LMR when NOT improving (sharpen reductions on a
        #     position that isn't getting any better).
        # Stays False at the first two plies and whenever either eval is None.
        improving = False
        if static_eval is not None and ply >= 2:
            prev = self._eval_stack[ply - 2]
            if prev is not None:
                improving = static_eval > prev

        # --- Reverse futility / static null-move pruning --------------- #
        # If we are already far above beta on the static score at a shallow
        # depth, assume the position holds and prune. Improving plies need
        # less margin (the trajectory backs the cut); use (depth - improving)
        # so an improving node prunes one ply deeper for the same eval.
        if (not is_pv and not in_check and depth <= 4
                and abs(beta) < self.MATE_THRESHOLD
                and static_eval - self.RFP_MARGIN * (depth - improving) >= beta):
            return static_eval

        # --- Razoring (#1.4) ------------------------------------------- #
        # The mirror of RFP at the bottom of the tree: if the static eval is
        # so far below alpha that one ply of search is unlikely to recover
        # it, skip the full-width search and verify with a qsearch (which
        # still sees tactics). A qsearch > alpha falls through to the normal
        # search so we never miss a real refutation. Margins reuse the
        # frontier futility table (150 / 320) plus one RFP_MARGIN of slack
        # to stay deliberately tighter than RFP -- razoring should only
        # fire on positions that are clearly losing on the board, not just
        # marginally behind.
        if (not is_pv and not in_check and depth in (1, 2)
                and abs(alpha) < self.MATE_THRESHOLD
                and static_eval + self.FUTILITY_MARGIN[depth]
                    + self.RFP_MARGIN <= alpha):
            q = self._quiescence(board, alpha, beta, ply, in_check)
            if q <= alpha:
                return q

        # --- Null-move pruning ----------------------------------------- #
        if (depth >= 3 and not in_check and not is_pv
                and static_eval >= beta
                and self._has_non_pawn_material(board, board.turn)
                and beta < self.MATE_THRESHOLD):
            r = self.NULL_MOVE_R + (depth // 6)
            self._make_null(board)
            # Bug fix (roadmap item #6): clear this node's _move_stack slot,
            # mirroring what the real-move path does at #1.6 above (just
            # with None instead of a move, since none was played). Without
            # this, _move_stack[ply] still holds whatever real move a
            # DIFFERENT, earlier-visited branch left there the last time
            # anything passed through this same ply -- so a grandchild
            # descending from this null-move line would read that stale,
            # unrelated move as its 2-ply-back continuation-history
            # predecessor instead of correctly seeing "no predecessor here".
            self._move_stack[ply] = None
            # X-02: the null child is provably not in check -- we weren't in
            # check (null gate above), and a position where the side that
            # just "moved" leaves its opponent's king capturable is illegal.
            null_score = -self._negamax(board, depth - 1 - r, -beta, -beta + 1,
                                        ply + 1, ext_budget, None, None,
                                        chk_budget, False)
            self._unmake(board)
            if null_score >= beta:
                return beta            # fail-hard; don't return false mates

        # --- Frontier futility pruning flag ---------------------------- #
        # When NOT improving we trust the static eval more aggressively, so
        # we widen the "useless" band by RFP_MARGIN // 2 -- a node already
        # declining cuts more quiets at the frontier. Improving nodes use
        # the original strict margin.
        if not is_pv and not in_check and depth in self.FUTILITY_MARGIN \
                and abs(alpha) < self.MATE_THRESHOLD:
            futility_margin = self.FUTILITY_MARGIN[depth]
            if not improving:
                futility_margin += self.RFP_MARGIN // 2
            futile = static_eval + futility_margin <= alpha
        else:
            futile = False

        # IIR: flying blind at full depth wastes nodes — reduce by 1 when we
        # have no TT move to guide ordering.
        if depth >= 4 and tt_move is None and not in_check:
            depth -= 1

        # Counter-move heuristic: the quiet move that last refuted this exact
        # predecessor move is tried early (it often refutes it again).
        # V-05: the predecessor key (from|to<<6) is the countermove index AND
        # the 1-ply continuation key pm1 -- build it once.
        pmk = (prev_move.from_square | (prev_move.to_square << 6)
               ) if prev_move is not None else None
        counter = self.countermoves[pmk] if pmk is not None else None   # P-23
        # #1.6: pre-compute the (from, to) tuples for the predecessors used
        # by the continuation-history lookups inside the move loop. None
        # means "no predecessor in scope" -- skip the lookup. W-08: computed
        # once here and passed into order_moves (which used to rebuild them).
        pm1 = pmk                                        # Y-06: int pm key
        pm2 = None
        if ply >= 2:
            pm2_move = self._move_stack[ply - 2]
            if pm2_move is not None:
                pm2 = pm2_move.from_square | (pm2_move.to_square << 6)
        scored = self.order_moves(board, tt_move, ply, counter, prev_move,
                                  pm1, pm2)   # P-19 rows / W-08 shared pms
        if not scored:
            return -self.MATE_SCORE + ply if in_check else 0

        # One-reply extension: in a forced line where only a single legal move
        # exists, search it one ply deeper (bounded by ext_budget). This keeps
        # the engine from cutting off forcing endgame mating sequences early.
        single_reply = len(scored) == 1

        best_value = -self.INF
        best_move = None
        move_index = 0
        # Quiet moves actually searched at this node, in order. On a quiet
        # beta-cutoff the last one is the refutation (it gets the history bonus)
        # and the earlier ones get the history malus (see use_history_malus).
        searched_quiets = []
        color = board.turn

        # Mark this position as on the current path so a deeper transposition
        # back to it is seen as a repetition. Restored after the loop.
        self._path[key] = self._path.get(key, 0) + 1
        # P-20: bind the node-invariant parts of the per-move prune gates and
        # table lookups once -- CPython pays a dict lookup per `self.X` per
        # move otherwise. alpha changes during the loop, so the MATE_THRESHOLD
        # window test stays inside (only the attribute load is hoisted).
        MATE_TH = self.MATE_THRESHOLD
        lmp_limit = (self.LMP_COUNT[depth]
                     if (self.use_lmp and not is_pv and not in_check
                         and depth <= self.LMP_MAX_DEPTH) else None)
        hist_floor = -(self.HISTORY_MAX >> 1)
        see_prune = (self.use_see_prune_captures and not is_pv
                     and not in_check and depth <= 2)
        hist_tab = self.history
        cont1_get = self.cont_history.get
        cont2_get = self.cont_history_2.get
        # U-01: hoist the rest of the loop's node-invariant self.* reads, and
        # clamp the LMR table's depth index once per node instead of per move.
        LMR_MIN = self.LMR_MIN_MOVE
        lmr_aggr = self.lmr_aggressive
        lmr_row = self._lmr_table[depth if depth < 64 else 63]
        hist_q = self.HISTORY_MAX >> 2
        use_capt_hist = self.use_capt_history
        use_hist_malus = self.use_history_malus

        for _sc, move, raw, ordering_see in scored:    # P-19: no re-zip
            # #2.3: tags read from the packed move word -- no board queries.
            is_capture = ((raw >> MV_SHIFT_VICTIM) & MV_MASK_PT) != 0
            is_quiet = not is_capture and not (raw & 0x7000)   # X-08: promo bits

            # Futility: at a frontier node, skip quiet moves that cannot raise
            # alpha (always keep at least the first move so we have a score).
            if futile and is_quiet and move_index > 0:
                move_index += 1
                continue

            # Late-move pruning: once enough quiet moves have been searched at a
            # shallow non-PV node without improving alpha, give up on the rest.
            # `searched_quiets` holds the quiets already searched (captures/
            # promos all sort first, so once we reach a quiet the remaining list
            # is all quiet -> a plain break drops exactly the late quiet tail).
            if (lmp_limit is not None and is_quiet
                    and abs(alpha) < MATE_TH
                    and len(searched_quiets) >= lmp_limit):
                break

            # Per-move history score reused by LMP-history and LMR below.
            # Cheap dict.get; only meaningful for quiets (captures/promos
            # don't pass through _update_history). #1.6: add 1-ply and 2-ply
            # continuation history so both LMP and LMR see the same combined
            # signal that move ordering used. Roadmap item #16: computed here
            # (after the two prune checks above that don't need it) rather
            # than right after is_quiet, so a move dropped by futility or
            # count-based LMP skips these 1-3 dict lookups entirely.
            # W-12 was tried here (reuse the ordering row's `_sc` as `hist`
            # for plain quiets) and FAILED the node-identity gate: history/
            # cont tables mutate DURING this node's own move loop (child
            # cutoffs write bonuses/maluses), so the fresh recompute reads
            # newer values than the ordering-time snapshot. Reusing `_sc` is
            # therefore a search-behavior change needing its own A/B, not a
            # free speedup -- do not "re-optimize" this without one.
            if is_quiet:
                frm = raw & 63                    # P-28: from/to from the tag
                to = (raw >> 6) & 63
                hkey = _hist_key(color, frm, to)
                hist = hist_tab[hkey]
                if pm1 is not None:               # Y-06: flat-array reads
                    arr = cont1_get(pm1)
                    if arr is not None:
                        hist += arr[hkey]
                if pm2 is not None:
                    arr = cont2_get(pm2)
                    if arr is not None:
                        hist += arr[hkey]
            else:
                hist = 0

            # History-gated LMP (#1.5): a quiet with strongly negative history
            # at this depth-shallow non-PV node is dropped early -- past
            # searches have repeatedly failed to refute with it, so we trust
            # the move-ordering verdict and skip it before paying for the
            # make/recurse/unmake. Bounded by the same depth gate as the
            # count-based LMP so it can never fire at deep / PV nodes.
            if (lmp_limit is not None and is_quiet
                    and abs(alpha) < MATE_TH
                    and move_index >= lmp_limit // 2
                    and hist < hist_floor):
                move_index += 1
                continue

            # SEE-prune losing captures at frontier nodes.  A capture with
            # clearly negative SEE at depth<=2 will be resolved (worse) by the
            # qsearch anyway; skip the full subtree.  Keep move_index==0 always
            # so we never return without a score.  mover > victim piece-type is
            # a fast pre-filter before calling the real SEE.
            if see_prune and is_capture and move_index > 0:
                mover_pt_raw = (raw >> MV_SHIFT_MOVER) & MV_MASK_PT
                victim_pt_raw = (raw >> MV_SHIFT_VICTIM) & MV_MASK_PT
                if mover_pt_raw > victim_pt_raw:
                    # Roadmap item #14: reuse the SEE order_moves already
                    # computed for this exact move/position when available
                    # (its own gate is value- not piece-type-based, so this
                    # can occasionally miss -- falls back to computing it here
                    # exactly as before on a miss, never a stale/wrong value).
                    see = ordering_see if ordering_see is not None else self._see_raw(board, raw)
                    if see < -depth * 100:
                        move_index += 1
                        continue

            # --- Extensions (single-reply / recapture / passed-pawn; check below) -- #
            # Non-check extensions draw on ext_budget; the check extension draws
            # on its own chk_budget so the two never starve each other.
            extension = 0
            is_check_ext = False
            if ext_budget > 0:
                if single_reply:
                    extension = 1
                elif (self._recapture_at(board, move, is_capture, last_cap_sq)):
                    extension = 1            # capture-sequence (recapture) extension
                elif (((raw >> MV_SHIFT_MOVER) & MV_MASK_PT) == 1
                        and self._is_passed_pawn_push(board, move)):
                    # U-02: tag says the mover is a pawn (pt 1) before paying
                    # the helper call -- its own first check is exactly this.
                    extension = 1

            self._make(board, move, raw)
            # #1.6: record so the child node can read predecessors at
            # ply-1 (this move) and ply-2 from `_move_stack`. Overwritten
            # on the next iteration; no cleanup needed on unmake.
            self._move_stack[ply] = move
            gives_check = board.is_check()
            if extension == 0 and gives_check and chk_budget > 0:
                extension = 1            # check extension (own budget, cheap post-push test)
                is_check_ext = True
            child_last_cap = move.to_square if is_capture else None
            new_depth = depth - 1 + extension
            child_ext = ext_budget - (0 if is_check_ext else extension)
            child_chk = chk_budget - (extension if is_check_ext else 0)

            # --- Late-move reductions for quiet, late, non-checking moves -- #
            reduction = 0
            if (depth >= 3 and move_index >= LMR_MIN
                    and extension == 0 and is_quiet
                    and not in_check and not gives_check):
                if lmr_aggr:
                    # log(depth)*log(move_index) reduction: small for early/
                    # shallow moves, growing for late moves at high depth.
                    reduction = lmr_row[move_index if move_index < 64 else 63]
                    if is_pv and reduction > 0:
                        reduction -= 1        # reduce less on PV nodes
                    # Sharpen reductions on non-improving lines: the position
                    # isn't trending up for us, so late quiets are even less
                    # likely to refute.
                    if not improving:
                        reduction += 1
                else:
                    reduction = 1
                    if move_index >= 6 and depth >= 6:
                        reduction = 2
                    if not improving:
                        reduction += 1
                    if is_pv and reduction > 0:
                        reduction -= 1        # reduce less on PV nodes
                # History-driven reduction bias (#1.5): a quiet with a strong
                # positive history (often refutes here) is reduced less; one
                # with negative history is reduced more. Bounded to [-2, +2]
                # so the LMR table still dominates and one outlier history
                # can't push the search past quiescence on its own.
                hist_shift = hist // hist_q
                if hist_shift > 2:
                    hist_shift = 2
                elif hist_shift < -2:
                    hist_shift = -2
                reduction -= hist_shift
                if reduction < 0:
                    reduction = 0
                # Never reduce a move into (or past) quiescence: keep at
                # least one ply of real search so the scout is meaningful.
                if reduction > new_depth - 1:
                    reduction = max(0, new_depth - 1)
            elif (depth >= 3 and move_index >= LMR_MIN
                    and extension == 0 and is_capture
                    and not in_check and not gives_check):
                # Losing captures (heavier piece takes lighter) already sorted
                # last among captures; reduce by 1 (smaller than quiet LMR).
                mover_pt_raw = (raw >> MV_SHIFT_MOVER) & MV_MASK_PT
                victim_pt_raw = (raw >> MV_SHIFT_VICTIM) & MV_MASK_PT
                if mover_pt_raw > victim_pt_raw:
                    reduction = 1
                    if reduction > new_depth - 1:
                        reduction = max(0, new_depth - 1)

            if move_index == 0:
                value = -self._negamax(board, new_depth, -beta, -alpha,
                                       ply + 1, child_ext, child_last_cap, move, child_chk, gives_check)
            else:
                # PVS scout (optionally reduced), then re-search if it surprises.
                value = -self._negamax(board, new_depth - reduction, -alpha - 1, -alpha,
                                       ply + 1, child_ext, child_last_cap, move, child_chk, gives_check)
                if reduction and value > alpha:
                    value = -self._negamax(board, new_depth, -alpha - 1, -alpha,
                                           ply + 1, child_ext, child_last_cap, move, child_chk, gives_check)
                if alpha < value < beta:
                    value = -self._negamax(board, new_depth, -beta, -alpha,
                                           ply + 1, child_ext, child_last_cap, move, child_chk, gives_check)
            self._unmake(board)
            if is_quiet:
                searched_quiets.append(move)

            if value > best_value:
                best_value = value
                best_move = move
            if value > alpha:
                alpha = value
            if alpha >= beta:
                bonus = depth * depth
                if is_capture:
                    if use_capt_hist:
                        mover_pt_raw = (raw >> MV_SHIFT_MOVER) & MV_MASK_PT
                        victim_pt_raw = (raw >> MV_SHIFT_VICTIM) & MV_MASK_PT
                        self._update_capt_history(mover_pt_raw, move.to_square,
                                                  victim_pt_raw, bonus)
                if is_quiet:
                    self._store_killer(move, ply)
                    self._update_history(color, move, bonus)         # reward refutation
                    # Malus: every quiet searched earlier here failed to cut.
                    if use_hist_malus:
                        for q in searched_quiets[:-1]:
                            self._update_history(color, q, -bonus)
                    if prev_move is not None:        # record refutation as counter-move
                        self.countermoves[prev_move.from_square
                                          | (prev_move.to_square << 6)] = move   # P-23
                    # #1.6: mirror the bonus / malus into the continuation
                    # tables for the same predecessors that ordering used.
                    # Skip when no predecessor is in scope (root / ply 1).
                    rfrm = raw & 63               # P-28: from/to from the tag
                    rto = (raw >> 6) & 63
                    hk_cut = _hist_key(color, rfrm, rto)   # Y-06
                    if pm1 is not None:
                        self._update_cont_history(self.cont_history,
                                                  pm1, hk_cut, bonus)
                        if use_hist_malus:
                            for q in searched_quiets[:-1]:
                                self._update_cont_history(self.cont_history, pm1,
                                    _hist_key(color, q.from_square, q.to_square),
                                    -bonus)
                    if pm2 is not None:
                        self._update_cont_history(self.cont_history_2,
                                                  pm2, hk_cut, bonus)
                        if use_hist_malus:
                            for q in searched_quiets[:-1]:
                                self._update_cont_history(self.cont_history_2, pm2,
                                    _hist_key(color, q.from_square, q.to_square),
                                    -bonus)
                break
            move_index += 1

        # Leaving this node: restore the repetition path counter.
        self._path[key] -= 1

        # Everything was futility-pruned except (possibly) nothing meaningful:
        # fall back to a static score so we never return -INF.
        if best_move is None:
            return (static_eval if static_eval is not None
                    else self._evaluate_stm(board, key))       # W-07

        # --- Store in the transposition table -------------------------- #
        if best_value <= alpha_orig:
            flag = TT_UPPER
        elif best_value >= beta:
            flag = TT_LOWER
        else:
            flag = TT_EXACT
        # Cache static_eval (None at PV-skipped nodes; a later visit that
        # needs it will recompute when the cached slot is None). Bug fix
        # (roadmap item #5): explicitly force None when in_check too, even
        # though the local `static_eval` variable holds a real value there
        # (the check-eval-proxy alpha, used by THIS node's improving
        # heuristic -- that use is intentional and unaffected by this fix).
        # That proxy is window-dependent (it's literally the search alpha,
        # not a position eval), so persisting it into the TT let a later
        # visit to the same in-check position -- under a different
        # alpha/beta window -- read it back via tt_eval as if it were a real
        # cached eval, polluting ITS improving heuristic with an arbitrary
        # number that has nothing to do with the position.
        self._tt_store(key, (depth, flag, self._tt_value_to(best_value, ply),
                             best_move, None if in_check else static_eval,
                             self._tt_gen))
        return best_value

    def _is_passed_pawn_push(self, board, move):
        """True if ``move`` advances a (5th-rank-or-beyond) passed pawn."""
        if not (board.pawns & chess.BB_SQUARES[move.from_square]):
            return False
        color = board.turn
        rank = chess.square_rank(move.to_square)
        if rank < 4 if color == chess.WHITE else rank > 3:
            return False
        return self._is_passed_pawn(board, move.to_square, color)

    # ------------------------------------------------------------------ #
    # Quiescence search: stand-pat + delta pruning over noisy moves.
    # ------------------------------------------------------------------ #
    def _quiescence(self, board, alpha, beta, ply, in_check_hint=None):
        self.nodes += 1
        if (self.nodes & self._poll_mask) == 0:   # P-21: inlined poll gate
            self._check_time()

        if ply >= self.MAX_PLY:
            return self._evaluate_stm(board)
        if (not (board.pawns | board.rooks | board.queens)
                and board.is_insufficient_material()):   # P-27: exact pre-filter
            return 0

        # X-02: use the parent's post-push gives_check when handed down
        # (negamax's depth<=0 forward and its razor probe); deeper qsearch
        # plies still compute -- their parents don't classify children.
        in_check = board.is_check() if in_check_hint is None else in_check_hint

        if in_check:
            # Must consider every evasion (else we could stand-pat out of mate).
            scored = self.order_moves(board, None, ply)   # P-19: rows
            if not scored:
                return -self.MATE_SCORE + ply        # checkmate
            best = -self.INF
            for _sc, move, raw, _see in scored:      # X-03: raw feeds _make
                self._make(board, move, raw)
                score = -self._quiescence(board, -beta, -alpha, ply + 1)
                self._unmake(board)
                if score > best:
                    best = score
                if score > alpha:
                    alpha = score
                if alpha >= beta:
                    return beta
            return best

        # Stand-pat: we can decline to capture and keep the static score.
        # Lazy: skips the expensive positional terms when the cheap base already
        # proves a >= beta cutoff (exact, so the search is unchanged).
        stand_pat = self._qs_stand_pat(board, beta)
        if stand_pat >= beta:
            return beta
        # Delta pruning: if even the biggest swing can't reach alpha, give up.
        # With a pawn on the 7th the biggest swing isn't a queen capture --
        # it's promote-with-capture, gaining (Queen - Pawn) on top of the
        # captured piece. Without this allowance we can prune a winning
        # promotion sequence and report a static score that's far too low.
        promo_rank = chess.BB_RANK_7 if board.turn == chess.WHITE else chess.BB_RANK_2
        promo_bonus = 0
        if board.pawns & board.occupied_co[board.turn] & promo_rank:
            promo_bonus = self.PIECE_VALUES[chess.QUEEN] - self.PIECE_VALUES[chess.PAWN]
        if stand_pat + self.PIECE_VALUES[chess.QUEEN] + promo_bonus + self.DELTA_MARGIN < alpha:
            return alpha
        if stand_pat > alpha:
            alpha = stand_pat

        for _sc, move, raw, victim_value in self._capture_moves(board):  # V-07
            # Per-move delta + SEE pruning on the captured material. Roadmap
            # item #10: mover_pt read straight from the raw tag; victim_value
            # is now carried on the row (V-07) so the loop doesn't re-derive
            # what _capture_moves already computed for MVV-LVA ordering
            # (quiescence is ~50% of all nodes). raw's victim_pt already reads
            # as PAWN for an en-passant capture (set at the tag's construction,
            # see _synth_raws / the C capture generator).
            if not (raw & 0x7000):                # X-08: promo bits 12-14
                if stand_pat + victim_value + self.DELTA_MARGIN < alpha:
                    continue
                # SEE pruning: drop captures that lose material outright. Only
                # worth checking when the mover outweighs the victim (else SEE
                # >= 0). This branch is never reached while in check -- the
                # in-check evasion search above returns before this loop.
                mover_pt = (raw >> MV_SHIFT_MOVER) & MV_MASK_PT
                mover_value = self.PIECE_VALUES[mover_pt] if mover_pt else 0
                if (self.use_see and mover_value > victim_value
                        and self._see_raw(board, raw) < 0):
                    continue
            self._make(board, move, raw)
            score = -self._quiescence(board, -beta, -alpha, ply + 1)
            self._unmake(board)
            if score >= beta:
                return beta
            if score > alpha:
                alpha = score
        return alpha

    # ================================================================== #
    # Helpers
    # ================================================================== #
    def _has_non_pawn_material(self, board, color):
        return bool((board.knights | board.bishops | board.rooks | board.queens)
                    & board.occupied_co[color])

    def _check_time_std(self):
        """Abort the search (via exception) once the time budget is used up
        or the host set ``_abort`` (see its note in __init__). Checked at the
        same node-mask cadence for both, so untimed (fixed-depth) searches are
        abortable too."""
        if (self.nodes & 4095) != 0:
            return
        if self._abort:
            raise _TimeUp()
        if (self.time_limit is not None
                and (time.perf_counter() - self.start_time) >= self.time_limit):
            raise _TimeUp()
        # Bound TT memory within a single long search too -- the between-search
        # cap can't help a `go infinite` / long-movetime search, which
        # otherwise grows the dict far past the advertised UCI Hash. Worst
        # case overshoot before the next poll: 4096 entries. (Zeitnot variant
        # skips this: a <250 ms search can't grow the TT meaningfully. Shared
        # TT is a fixed-size array; the orchestrator owns it.)
        if not self.use_shared_tt and len(self.tt) > self.TT_MAX_ENTRIES:
            self.tt.clear()

    def _check_time_zeitnot(self):
        """_check_time for sub-250 ms budgets: an 8x finer poll (every 1024
        nodes, ~18 ms at the ~58k-NPS baseline), because the default
        4096-node poll (~70 ms of nodes) cannot honor such a budget at all.
        Bound per search at the clock-arm site in _search -- method swap, not
        a per-node branch."""
        if (self.nodes & 1023) != 0:
            return
        if self._abort:
            raise _TimeUp()
        if (self.time_limit is not None
                and (time.perf_counter() - self.start_time) >= self.time_limit):
            raise _TimeUp()

    _check_time = _check_time_std

    # Mate scores are stored relative to the current ply so a mate found at a
    # different depth keeps a consistent distance when reused from the TT.
    def _tt_value_to(self, value, ply):
        if value > self.MATE_THRESHOLD:
            return value + ply
        if value < -self.MATE_THRESHOLD:
            return value - ply
        return value

    def _tt_value_from(self, value, ply):
        if value > self.MATE_THRESHOLD:
            return value - ply
        if value < -self.MATE_THRESHOLD:
            return value + ply
        return value

    # ------------------------------------------------------------------ #
    # Transposition-table access -- hides the one-slot vs two-tier format.
    # An entry is the 6-tuple (depth, flag, value, move, static_eval, gen).
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # NPS: _tt_get/_tt_store are called once per node (same frequency as
    # _make/_unmake) and used to check up to 3 mutually-exclusive TT-
    # strategy toggles (use_shared_tt / use_tt_two_tier / use_tt_depth_
    # replace) sequentially before reaching the real work, even though all
    # three default off. Same fix as _make/_make_null/_unmake: split into
    # one branch-free variant per strategy and bind self._tt_get/_tt_store
    # to the right pair once per search (see _search) instead of re-
    # checking the same-every-time toggles on every single node.
    # ------------------------------------------------------------------ #
    def _tt_get_default(self, key):
        """Plain always-replace / depth-replace TT (single entry per slot --
        both policies store one entry, only their write-time replacement
        rule differs, so retrieval is identical for both)."""
        return self.tt.get(key)

    def _tt_get_two_tier(self, key):
        """Best (deepest) of the (deep, fresh) pair stored per slot."""
        slot = self.tt.get(key)
        if slot is None:
            return None
        deep, fresh = slot
        if deep is None:
            return fresh
        if fresh is None:
            return deep
        return deep if deep[0] >= fresh[0] else fresh

    def _tt_get_shared(self, key):
        """#13: lock-free shared-memory TT (key is self._zob, not ``key``)."""
        data = self._shared_tt.get(self._zob)
        if data is None:
            return None
        mv = (data >> 32) & 0xFFFF
        move = None if mv == 0 else chess.Move(mv & 63, (mv >> 6) & 63,
                                               ((mv >> 12) & 7) or None)
        static_eval = None if (data & (1 << 10)) else (((data >> 48) & 0xFFFF) - 32768)
        return (data & 0xFF,                                   # depth
                (data >> 8) & 3,                              # flag
                ((data >> 16) & 0xFFFF) - 32768,              # value
                move,
                static_eval,
                0)                                            # gen (unused)

    # Default binding so _tt_get/_tt_store are callable before the first
    # _search() call rebinds them to the instance's actual configured
    # strategy (see _search); matches the _make/_unmake pattern above.
    _tt_get = _tt_get_default

    def _tt_store_default(self, key, entry):
        """Original always-replace scheme."""
        self.tt[key] = entry

    def _tt_store_depth_replace(self, key, entry):
        old = self.tt.get(key)
        if (old is None or old[5] != self._tt_gen
                or entry[0] >= old[0] or entry[1] == TT_EXACT):
            self.tt[key] = entry

    def _tt_store_two_tier(self, key, entry):
        slot = self.tt.get(key)
        if slot is None:
            self.tt[key] = (entry, None)
            return
        deep, _fresh = slot
        # New entry takes the depth slot when the slot is empty, holds a
        # leftover from an earlier search, or is at least as deep -- the
        # displaced deep entry drops into the always-replace slot. Otherwise
        # the new (shallower) entry just refreshes the always-replace slot.
        if deep is None or deep[5] != entry[5] or entry[0] >= deep[0]:
            self.tt[key] = (entry, deep)
        else:
            self.tt[key] = (deep, entry)

    def _tt_store_shared(self, key, entry):
        """#13: lock-free shared-memory TT (key is self._zob, not ``key``)."""
        depth, flag, value, move, static_eval, _gen = entry
        # Bug fix (roadmap item #4): the value field is 16 bits, so mate
        # scores (~+-1,000,000) clamp to +-32767 -- but clamping a value
        # can flip what the FLAG is allowed to claim:
        #   clamped positive (value>32767, stored as 32767): "true score
        #     is at least 32767" is always a true, weaker statement, so
        #     EXACT safely downgrades to LOWER, and an existing LOWER
        #     stays LOWER. But an existing UPPER ("true <= value") CANNOT
        #     be tightened to "true <= 32767" without proof -- skip the
        #     store rather than assert something unverified.
        #   clamped negative: the mirror image (EXACT/UPPER safe, LOWER
        #     must skip).
        # Un-clamped values (the overwhelming majority -- normal eval
        # scores are far under 32767) are unaffected either way.
        if value > 32767:
            if flag == TT_UPPER:
                return                      # can't safely represent, skip
            if flag == TT_EXACT:
                flag = TT_LOWER
        elif value < -32768:
            if flag == TT_LOWER:
                return                      # can't safely represent, skip
            if flag == TT_EXACT:
                flag = TT_UPPER
        if move is None:
            mv = 0
        else:
            mv = move.from_square | (move.to_square << 6) | ((move.promotion or 0) << 12)
        v = (value if -32768 <= value <= 32767 else (32767 if value > 0 else -32768)) + 32768
        if static_eval is None:            # bit 10 marks "no static eval"
            eflag, ev = (1 << 10), 0
        else:
            eflag = 0
            ev = (static_eval if -32768 <= static_eval <= 32767
                  else (32767 if static_eval > 0 else -32768)) + 32768
        data = ((depth & 0xFF) | ((flag & 3) << 8) | eflag
                | (v << 16) | (mv << 32) | (ev << 48))
        self._shared_tt.store(self._zob, data)

    _tt_store = _tt_store_default

    def _bind_tt_strategy(self):
        """Pick the one active TT strategy (mutually exclusive by
        construction -- see the priority order the original unified
        functions used: shared > two-tier > depth-replace > default) and
        bind self._tt_get/_tt_store directly to it. Called once per search
        (see _search) rather than every node."""
        if self.use_shared_tt:
            self._tt_get = self._tt_get_shared
            self._tt_store = self._tt_store_shared
        elif self.use_tt_two_tier:
            self._tt_get = self._tt_get_two_tier
            self._tt_store = self._tt_store_two_tier
        elif self.use_tt_depth_replace:
            self._tt_get = self._tt_get_default
            self._tt_store = self._tt_store_depth_replace
        else:
            self._tt_get = self._tt_get_default
            self._tt_store = self._tt_store_default
