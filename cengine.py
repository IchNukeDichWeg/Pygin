"""
cengine.py -- Python root driver for the C search core (csearch.so).
====================================================================

A drop-in ``Engine`` for the project's battle/match harness, with the
ENTIRE per-node search loop in C (csearch.c): board, move ordering,
transposition table, pruning, quiescence and the full static eval
(bit-exact port of engine.py's ``_evaluate_static``, verified over 3M
positions). Born as phase-3 step 6 of the C-core plan; the shipped engine
since Old Engine/31. Its defaults ARE v42 -- v41 + CW-01 cannot-win eval
clamp (+3.27 +/-6.8 vs Old Engine/41, a null KEPT as correctness: the
eval no longer favors sides that cannot force mate; snapshotted Old
Engine/42). v43 = v42 MINUS CB-02's deep-null verification: NV-01
measured the removal at +5.18 +/-6.8 vs Old Engine/42 (pair ratio 1.08),
converging with CB-02's own -2.88 lean -- the insurance cost ~3-5 Elo of
nodes-to-depth and is DROPPED (modern-engine practice); snapshotted Old
Engine/43; FI-04 history-LMR read +2.15 null and is DORMANT -- the
finer-quiet-signal vein is 0-for-3). v44 = v43 + FI-26a, the unconditional
TT prefetch after apply_move (node-identical, +4.9% NPS): the timed A/B
priced it at +13.31 +/-6.8 vs Old Engine/43 @10k 50+0.20 (51.91%, pair
ratio 1.25, norm +27.85) -- P-45's null INVERTED by FI-01's free child
key, the biggest single NPS win of the C era in Elo terms; snapshotted
Old Engine/44 (a staged-quiet lazy pick was tried alongside and PARKED,
bench noise). v45 = v44 + FI-25, the TT-value pruning-eval sharpener:
+13.52 +/-6.8 vs Old Engine/44 @10k 50+0.20 (51.94%, pair ratio 1.22,
norm +28.34) -- sonnet5's top new idea confirmed at full value, back to
back with v44's +13.31; snapshotted Old Engine/45. FI-18 SEE pruning of
losing captures read -1.25 null and FI-06 root-move ordering read +2.26
null (both DORMANT, mechanisms kept) vs Old Engine/45. v46 = v45 with the
TT doubled to 22 bits (96 MB): +5.94 +/-6.8 vs Old Engine/45 @10k 50+0.20
(50.85%, pair ratio 1.10, norm +12.33) -- a borderline-positive (CI just
touches zero) shipped on the monotonic-low-risk rationale, motivated by a
hashfull capture showing a single deep search fills half the 48 MB table;
snapshotted Old Engine/46. v47 = v46 with the TT at 23 bits (192 MB):
+3.16 +/-6.8 vs Old Engine/46 @10k 50+0.20 (50.46%, norm +6.54) -- the
96->192 MB increment, net-positive at full load (same monotonic-low-risk
ship); the diminishing +5.94->+3.16 CLOSES memory-scaling (no 24 probe).
v47 also carries MultiPV (UCI spin 1..5, node-exact off). Snapshotted Old
Engine/47. Armed candidate: (none pinned -- memory vein done; next is a
search/NPS feature, see final_improvements.md queue).

Python keeps only what needs game/host state -- exactly the phase-3 plan:
  * the iterative-deepening loop with v30's aspiration windows,
  * v30's P-35/U-06 soft-stop time management (stability-scaled),
  * v30's partial-iteration rule (an aborted depth's result is used iff at
    least the first root move finished),
  * the opening-book probe (delegated to an embedded engine.Engine, which is
    also the single source of truth for every eval table/parameter synced
    into the C core at construction),
  * TT retention policy (the fixed-size C TT PERSISTS across game moves --
    P-14, CONFIRMED +23.52 into v33; TT_KEEP_WARM=False restores v30's
    wipe-after-irreversible-move rule, which only ever existed for the
    Python engine's unbounded dict TT) and the game-history keys for
    repetition detection.

API (battle_worker.py contract):
    Engine().get_best_move(board, depth)                     -> Move | None
    Engine().get_best_move_timed(board, seconds, max_depth)  -> Move | None
    attributes: nodes_searched / last_score (White POV) / last_depth /
    last_pv, constants MATE_SCORE / MATE_THRESHOLD, settable use_book /
    pv_uci.

Search-feature ledger -- each entry names its csearch.c setter and the
baseline its non-default setting restores node-exactly (the ladder pin).
Eval-side toggles (USE_KING_SHELTER / USE_OUTPOST / USE_SIMPLIFY) live on
the class attrs below with their own verdicts.

ON by default (A/B-confirmed, or free by construction):
  * P-01 check extensions (set_check_ext; +6.81 +/-6.8 vs v33 ->
    snapshotted Old Engine/34; OFF = v33 node-exact). P-47 made the
    per-line budget runtime-settable (set_check_ext_budget; 5 = v36
    node-exact); raise-to-8 REJECTED 2026-07-10 (-4.59 +/-6.8 @10k
    50+0.20) -- the extensions vein is thin (P-01 +6.8, P-43 +3.5
    marginal, P-47 -4.6), do not re-try at this TC.
  * P-22 noisy-only qsearch generation (set_qgen; NODE-IDENTICAL by
    construction -- same noisy subset, same order, stalemate semantics
    preserved, verified over 8 FENs x 2 depths -- so it needs no ladder
    pin; +32% NPS mixed bench / +55% startpos. Timed Elo measured
    2026-07-10 as the P-22+P-44 bundle vs v34: ~+71.8 +/-8.5 @7k -- the
    NPS converts at the classic ~2-3 Elo/1%).
  * P-44 qsearch TT probe/store (set_qs_tt; isolation A/B vs the P-22 base
    +8.06 +/-6.8 @10k, CI clear of zero -> CONFIRMED into v35, snapshotted
    Old Engine/35; OFF = v34 node-exact): the node-majority qsearch probes
    the warm TT before movegen/eval and stores depth-0 entries that never
    displace negamax entries -- the persistent warm table across a game
    delivered what the flat cold-ladder time-to-depth bench could not show.
  * P-46 lazy qsearch generation (set_qs_lazy; node-identical, ~+1-3% NPS):
    eval + stand-pat run BEFORE movegen, so stand-pat exits never pay for
    generation.
  * P-23 staged move ordering (set_staged; +24.67 +/-6.8 @10k vs v35 ->
    CONFIRMED into v36, snapshotted Old Engine/36; set_staged(0) = v35
    node-exact): TT-move/captures/killers/counter/quiets/bad-captures
    generated lazily per stage -- ~+10-20% NPS AND a deliberate tree
    change (later stages score quiets with FRESHER history than v35's
    node-entry snapshot); stream equality under identical state proven by
    verify mode over ~1M nodes.
  * PV-01 triangular PV (cs_get_pv; NODE-EXACT, pure bookkeeping): the PV
    is collected during the search instead of TT-walked afterwards;
    _extract_pv emits the exact prefix in full, splicing the old TT walk
    only past any truncation. Necessary but NOT sufficient alone: with the
    warm TT, PV nodes hit exact entries almost immediately (check
    extensions inflate stored depths along mate lines), so the exact
    prefix was often 1 move and matetrack Bad-PVs stayed ~60%.
  * FI-02/FI-03 NPS batch (2026-07-11, NODE-IDENTICAL -- ladder passes
    bit-exactly, eval-cache differential clean over 15.9M nodes): mover PT
    read from the move word in apply_move (was a 5-branch bitboard probe);
    ordering's SEE verdict tagged into move-word bits 22-23 and reused by
    qsearch's losing-capture skip (every consumer masks to 15 bits);
    lazy pick_next ordering on the non-staged paths (stable shift-to-front,
    emission order == the full sort's; most nodes cut by move 3 and never
    sort the tail); static eval cached in the TT entry's spare 16 bits
    (deterministic per position => EXACT, reused on TT hits in negamax AND
    qsearch stand-pat -- the eval call is the most expensive per-node op).
    Paired alternating bench vs v38: +3.94% median, 9/9 pairs positive.
    Confirmed into v39 as the Phase-2 batch with FI-01 (+8.86 +/-6.8 vs Old
    Engine/38). (-flto was probed and read null on Apple Silicon, not adopted.)
  * FI-01 incremental Zobrist (2026-07-11, Phase-2 train part 2): the
    position key lives ON the Board and is XOR-maintained through
    apply_move/make_null (splitmix64 randoms, fixed seed) instead of the
    old 9-MIX full-state hash recomputed at every node; make_board computes
    it once per Python entry (key_from_scratch = the oracle). EP-01's FIDE
    filter became an O(1) fixup in board_key (phantom ep XORed back out),
    so set_ep_filter stays a runtime toggle at zero steady-state cost.
    ZKEY differential clean over 52.4M nodes (castling/ep/promo trees);
    d1-5 ladder bit-exact vs v38, deeper counts drift (different key
    values -> different TT index-collision patterns -- NOT a logic change);
    matetrack 896/767, zero Bad PVs. Paired bench: full Phase-2 train
    +8.92% NPS median vs v38, 9/9 pairs positive (Zobrist's own share
    ~+4.8% on top of part 1's +3.94%). A/B vs Old Engine/38: +8.86 +/-6.8
    @10k 50+0.20 (pair ratio 1.15, norm +18.89) -- CONFIRMED into v39.
  * PV-02 exact PV (set_pv_exact; CONFIRMED into v37 2026-07-10,
    snapshotted Old Engine/37; set_pv_exact(0) = v36's search): skip TT
    cutoffs/narrowing at PV nodes so the collected PV is complete
    end-to-end -- the same matetrack FEN goes 1-move -> full 13-ply mate
    PV, Bad-PVs -> zero. Tree-changing (d12 ~-23% nodes) yet the A/B was a
    clean null (+0.17 +/-6.8 @10k 50+0.20, pair ratio 1.02): for a
    correctness feature, a null means FREE.

  * CB-01 correctness batch (set_score_hygiene; CONFIRMED into v38
    2026-07-10, snapshotted Old Engine/38; set_score_hygiene(0) = v37
    node-exact): seven sub-resolution "score draws as draws, keep proven
    bounds" fixes -- Texel-consistent delta-pruning values, qsearch
    in-check repetition + insufficient-material detection (both draws
    decided BEFORE the qsearch TT probe, repetition sees qsearch plies via
    g_path logging), null-move fail-soft return + TT LOWER store (unproven
    mates clamped to beta), qsearch TT lower-bound alpha narrowing,
    mate-distance pruning (NON-PV nodes only: at a PV node the fastest-mate
    score lands exactly on the clamped beta and starves PV-01's in-window
    store -- matetrack caught it, 470 Bad PVs), deep-qsearch killers read
    slot 63 not the root's. A/B vs v37: +1.36 +/-6.8 @10k 50+0.20 (pair
    ratio 1.02) -- a clean null KEPT as correctness (PV-02 precedent);
    matetrack @0.5s 692/600 -> 868/751, ZERO Bad PVs (MDP ~+25% found).
  * EP-01 FIDE-exact ep hashing (set_ep_filter / EP_FILTER class attr;
    CONFIRMED into v40 2026-07-11, snapshotted Old Engine/40;
    EP_FILTER=False = v39 node-exact): the position key counts an
    en-passant square only when a legal ep capture actually exists
    (= python-chess's _transposition_key), so repetition detection agrees
    with the FIDE arbiter -- a phantom ep after a double push no longer
    splits one FIDE-identical position across two keys, missing
    repetitions in either direction. Since FI-01 it is an O(1) fixup in
    board_key that only runs when an ep square is set: near-zero cost,
    and merging the phantom-ep TT entries even saves nodes (d12 ladder
    713,014 -> 562,363). A/B vs Old Engine/39: +4.31 +/-6.8 @10k 50+0.20
    (50.62%, ptnml 227/1203/2064/1231/275, pair ratio 1.05, norm +9.14)
    -- a null KEPT as correctness (PV-02/CB-01 precedent).
  * CB-02 correctness batch #4 (set_cb2 + the CB2 driver logic; CONFIRMED
    into v41 2026-07-11, snapshotted Old Engine/41; CB2=False = v40
    node-exact): null-move TT store obeys the replacement policy (deeper
    entries and their moves survive), qsearch 50-move rule, verified deep
    null cutoffs (depth >= 10, g_no_null suppresses nulls in the
    verification subtree), root fail-high adoption/promotion across
    aspiration calls. A/B vs Old Engine/40: -2.88 +/-6.8 @10k 50+0.20
    (49.59%, ptnml 287/1198/2086/1169/260, pair ratio 0.96, norm -6.04)
    -- a null KEPT as correctness, the fourth of its class.
  * CW-01 cannot-win eval clamp (set_cantwin / CANTWIN class attr,
    mirrored into the embedded engine's use_cantwin; CONFIRMED into v42
    2026-07-11, snapshotted Old Engine/42; CANTWIN=False = v41 eval
    exactly): the eval clamps to 0 when the favored side has no pawns, no
    rooks/queens, and at most a lone minor (or two knights) -- it cannot
    force mate, so the true upper bound is a draw. A/B vs Old Engine/41:
    +3.27 +/-6.8 @10k 50+0.20 (50.47%, ptnml 257/1115/2159/1215/254, pair
    ratio 1.07, norm +6.98) -- a null KEPT as correctness, the fifth of
    its class.
  * FI-26a TT prefetch (unconditional TT_PREFETCH(c.key) after apply_move
    at the three child-recursion sites; CONFIRMED into v44 2026-07-12,
    snapshotted Old Engine/44; node-identical, no toggle -- deleting the
    macro line restores v43): FI-01's incremental child key made the
    prefetch address free, inverting P-45's original null. +4.9% NPS
    (median, 3/3 warmup-discarded pairs); A/B vs Old Engine/43: +13.31
    +/-6.8 @10k 50+0.20 (51.91%, ptnml 250/1050/2073/1321/306, pair ratio
    1.25, norm +27.85) -- the biggest single NPS win of the C era.
  * FI-25 TT-value pruning-eval sharpener (set_tt_eval_sharpen /
    TT_EVAL_SHARPEN class attr; CONFIRMED into v45 2026-07-12, snapshotted
    Old Engine/45; False = v44 node-exact): the TT hit's SEARCH value
    replaces the raw static eval in RFP / null-move / frontier futility
    whenever its bound provably improves the estimate (LOWER above / UPPER
    below / EXACT always; non-mate values, any entry depth); static_eval
    stays RAW for the FI-03 cache and the P-04 stack. A/B vs Old
    Engine/44: +13.52 +/-6.8 @10k 50+0.20 (51.94%, ptnml
    225/1100/2056/1299/320, pair ratio 1.22, norm +28.34).

DORMANT (default OFF, mechanism kept for longer-TC re-tests):
  * P-43 single-reply / forced-move extension (set_single_reply; +3.5
    +/-4.8 over 20k pooled games vs v34 -- positive-leaning on every
    signal but sub-significant, kept-marginal by user call; OFF = v34
    node-exact).
  * P-04 "improving" heuristic (set_improving; +0.38 +/-6.8 @10k vs v34 --
    a dead null despite -56% nodes and +1 ply: at this TC the deeper tree
    saw nothing new. v30's recipe: eval stack vs ply-2 feeding RFP depth /
    frontier-futility margin / LMR+1; OFF = v34 node-exact).
  * Q-01 continuation history (set_cont_hist; -0.87 +/-6.8 @10k 50+0.20 vs
    v36, 2026-07-10 -- a dead NULL: the 1-ply/2-ply continuation scores
    (v30's #1.6, piece-to keyed int16 tables) bought nothing at this depth
    and their ~1.6MB of tables cost cache; OFF = v36 node-exact).
  * (EP-01 graduated from this list to ON-by-default: CONFIRMED into v40,
    see the ledger above.)
  * FI-08 qsearch depth-0 eviction guard (set_qs_evict_max; +0.14 +/-6.8
    @10k vs Old Engine/40 -- dead null, not correctness, so unlike
    PV-02/CB-01/EP-01 it reverted: -1 = off = v40 rule, mechanism kept).
  * (CW-01 graduated from this list to ON-by-default: CONFIRMED into
    v42, see the ledger above.)

Deliberate deviations from v30 (documented, revisit if an A/B says so):
  * no root random tiebreak (deterministic best move),
  * no singular extensions / razoring (dormant or absent in v30 at match
    depths anyway),
  * repetition detection covers negamax nodes; quiescence only its
    in-check nodes (CB-01, path-logged keys),
  * (the raw-ep-hash deviation was FIXED by EP-01 in v40: the key now
    counts an ep square only when a legal ep capture exists,)
  * Lazy SMP exists in-process (csearch pthreads + lockless shared TT) but
    is strictly OPT-IN (smp_workers / UCI Threads; default 1, Elo
    unmeasured); tablebase probe exists but defaults off (use_tb=False,
    v30 match).
"""

import ctypes
import os
import sys
import threading
import time

import chess

_DIR = os.path.dirname(os.path.abspath(__file__))

CS_INF = 30000
CS_MATE_THRESH = CS_INF - 1000


def _load_pyengine():
    """Import the sibling engine.py (param source + book probe)."""
    if _DIR not in sys.path:
        sys.path.insert(0, _DIR)
    import engine as pyengine
    return pyengine


# FB-04: csearch.so's eval params + toggles + TT are PROCESS-WIDE. Two Engine
# instances with different configs in one process silently share them (the
# second construction re-syncs the globals under the first). Refuse instead.
_SYNCED_FINGERPRINT = None


class Engine:
    MATE_SCORE = 1_000_000
    MATE_THRESHOLD = MATE_SCORE - 1_000

    # P-20a king shelter: REJECTED at C-core depth (A/B vs v32, 2026-07-08:
    # 10k games @ 45+0.1, 49.38% = -4.27 +/-6.8, norm -7.98). The depth-8
    # signal (+10 +/-10 on the old engine) did not survive depth 14 --
    # deep search sees king attacks concretely, subsuming the static term.
    # False reproduces the v32 eval exactly (node-verified). Do not re-try
    # at this TC; the mechanism stays for future eval-toggle A/Bs.
    USE_KING_SHELTER = False

    # Outpost re-test: NULL, OFF (A/B vs Old Engine/37 2026-07-10, fourth
    # 50+0.20 campaign: -0.90 +/-6.8 @10k, 49.87%, ptnml 289/1230/1982/
    # 1216/283, pair ratio 0.99 -- the Python-era +0 +/-10 depth-8 signal
    # stayed a null at depth ~14, exactly P-20a's subsumption logic; unlike
    # a correctness null this buys nothing and costs eval work, so OFF).
    # C-era eval add-ons now 0-for-2 (shelter -4.27, outpost -0.90): no new
    # static-eval term without a 2k-game screen first. Same sync mechanism
    # as USE_KING_SHELTER; False = v37 eval exactly.
    USE_OUTPOST = False

    # CB-01 correctness batch (LIVE CANDIDATE, fifth 50+0.20-era campaign,
    # A/B vs Old Engine/37 PENDING; selftest pins the ladder to off).
    # One master toggle over seven sub-+/-6.8 "score draws as draws, keep
    # proven bounds" fixes -- csearch.c set_score_hygiene: (a) delta pruning
    # budgets Texel piece values (queen 1150 vs classic 900), (b) qsearch
    # in-check repetition detection (perpetuals scored as eval before, and
    # P-44 persisted the misscore into the warm TT), (c) qsearch
    # insufficient-material draws, (d) null-move fail-soft return + TT
    # LOWER store (unproven mates clamped), (e) qsearch TT lower-bound
    # alpha narrowing, (f) mate-distance pruning, (g) deep-qsearch killers
    # read slot 63, not the root's. KEEP-ON-NULL (PV-02 precedent:
    # correctness nulls are free); False = v37 node-exact.
    SCORE_HYGIENE = True

    # EP-01 FIDE-exact ep hashing: CONFIRMED into v40 (seventh 50+0.20-era
    # campaign, A/B vs Old Engine/39 2026-07-11: +4.31 +/-6.8 @10k, 50.62%,
    # pair ratio 1.05 -- a null KEPT as correctness, PV-02/CB-01 precedent).
    # The position key counts an en-passant square only when a legal ep
    # capture actually exists (= python-chess's _transposition_key), so
    # repetition detection agrees with the FIDE arbiter. Since FI-01 the
    # filter is an O(1) fixup in board_key that only runs when an ep square
    # is set -- near-zero cost. False = v39 node-exact.
    EP_FILTER = True

    # FI-08 / Q-03 qsearch depth-0 eviction guard: DORMANT (eighth 50+0.20
    # campaign, A/B vs Old Engine/40 2026-07-11: +0.14 +/-6.8 @10k, 50.02%,
    # pair ratio 1.01 -- a dead NULL; not a correctness fix, so the
    # Q-01/P-04 rule applies: default OFF, mechanism kept). Verdict also
    # prices the warm-TT-protection vein: at 48 MB / 50+0.20 the table is
    # not saturation-bound, deprioritizing FI-20 (gen-touch/2-slot bucket).
    # >= 0 = replace old-gen entries only up to that depth; -1 = v40 rule.
    QS_EVICT_MAX = -1

    # CB-02 correctness batch #4: CONFIRMED into v41 (ninth 50+0.20-era
    # campaign, A/B vs Old Engine/40 2026-07-11: -2.88 +/-6.8 @10k, 49.59%,
    # pair ratio 0.96 -- a null KEPT as correctness, the fourth of its
    # class after PV-02/CB-01/EP-01). C side (set_cb2): (a) FB-22 null-move
    # TT store obeys the replacement policy (never clobbers deeper entries,
    # keeps a same-key entry's move); (b) FI-27.1 qsearch 50-move rule;
    # (c) FI-24c deep null cutoffs (depth >= 10) verified with a reduced
    # no-null re-search (zugzwang insurance). Driver side (this attr):
    # FB-23 root fail-high moves adopted/promoted across aspiration calls
    # (v30's _partial_root_move rule). False = v40 node-exact.
    CB2 = True

    # CW-01 cannot-win clamp: CONFIRMED into v42 (tenth 50+0.20-era
    # campaign, A/B vs Old Engine/41 2026-07-11: +3.27 +/-6.8 @10k, 50.47%,
    # pair ratio 1.07 -- a null KEPT as correctness, the fifth of its
    # class). Eval clamps to 0 when the side it favors has no pawns and
    # cannot force mate (lone minor / two knights) -- no more shuffling at
    # "+2.6" to dodge a drawing capture (user-reported position goes
    # +2.92/shuffles -> 0.00/plays Kxc4). Bit-exact twin of engine.py's
    # use_cantwin (mirrored below: GUI eval bar and search always agree);
    # oracle differential clean over 389 positions. False = v41 eval.
    CANTWIN = True

    # NV-01 verification isolation: RESOLVED into v43 (eleventh 50+0.20
    # campaign, A/B vs Old Engine/42 2026-07-11: +5.18 +/-6.8 @10k for the
    # REMOVAL, 50.74%, pair ratio 1.08, norm +10.82). Converging evidence
    # (CB-02's own -2.88 lean + a recovered ply of nodes-to-depth) priced
    # CB-02(c)'s zugzwang insurance at ~3-5 Elo -- v43 drops it, matching
    # modern practice (Stockfish-family runs unverified null; has_non_pawn
    # + the TT cover zugzwang). True = v42's verifying search.
    NULL_VERIFY = False

    # FI-04 history-based LMR: DORMANT (twelfth 50+0.20-era campaign, A/B
    # vs Old Engine/43 2026-07-12: +2.15 +/-6.8 @10k, 50.31%, pair ratio
    # 1.05 -- a null below the pre-registered +3 tune threshold, so no
    # divisor tune; not correctness => the Q-01/P-04 rule: default 0,
    # mechanism kept). The finer-quiet-signal vein is now 0-for-3 at this
    # TC (Q-01 -0.87, P-42 -16.4, FI-04 +2.15) -- even the v39+ wave's
    # 5/5-consensus form doesn't pay; do not re-try without a longer TC.
    # divisor > 0 enables (adj = hist/div clamped +/-1); 0 = v43 exact.
    LMR_HIST = 0

    # FI-25 TT-value pruning-eval sharpener: ARMED (fourteenth 50+0.20-era
    # campaign, A/B vs Old Engine/44 PENDING -- sonnet5's top new idea).
    # FI-03 reuses the cached STATIC eval; the TT entry's SEARCH value is
    # strictly better information whenever its bound applies (LOWER above /
    # UPPER below the static eval, EXACT always), so it replaces the raw
    # eval in RFP / null-move / frontier futility -- prunes both more
    # accurately and less wrongly at the same depth, Stockfish-family
    # practice. Non-mate values only; the FI-03 TT cache and the P-04 eval
    # stack keep the RAW static eval (exactness invariants). False = v44
    # node-exact. CONFIRMED into v45 (fourteenth 50+0.20-era campaign, A/B
    # vs Old Engine/44 2026-07-12: +13.52 +/-6.8 @10k, 51.94%, pair ratio
    # 1.22 -- confirmed at full value, back to back with v44's +13.31).
    TT_EVAL_SHARPEN = True

    # FI-18 SEE pruning of losing captures: DORMANT (fifteenth 50+0.20-era
    # campaign, A/B vs Old Engine/45 2026-07-13: -1.25 +/-6.8 @10k, 49.82%,
    # pair ratio 0.98 -- a dead null with a negative lean; not correctness
    # => the Q-01/P-04 rule: default False, mechanism kept). Even the
    # standard-everywhere shallow losing-capture prune doesn't pay at this
    # TC -- bad captures are already ordered last, so alpha-beta was
    # getting most of the skip for free. matetrack stayed clean (913/783),
    # the Elo just wasn't there. False = v45 node-exact.
    SEE_PRUNE = False

    # FI-06 root-move ordering: DORMANT (sixteenth 50+0.20-era campaign, A/B
    # vs Old Engine/45 2026-07-13: +2.26 +/-6.8 @10k, 50.32%, pair ratio
    # 1.02 -- a positive lean landing in the predicted +0-4 band but the CI
    # covers zero; not correctness => the Q-01/P-04 rule: default False,
    # mechanism kept). Same magnitude/verdict as FI-04's +2.15: a free-ish
    # ordering tweak that can't clear the noise floor isn't banked. Three
    # root-only refinements (subtree-node-count ordering + warm-TT
    # iteration-1 seed, main thread only). False = v45 node-exact.
    ROOT_ORDER = False

    # FI-10: TT size in bits (2^bits x 24-byte entries; 21 = 48 MB, 22 =
    # 96 MB, 23 = 192 MB). CONFIRMED into v46 at 22 (seventeenth 50+0.20-era
    # campaign, A/B vs Old Engine/45 2026-07-13: +5.94 +/-6.8 @10k, 50.85%,
    # pair ratio 1.10, norm +12.33 -- a BORDERLINE-positive, CI just touches
    # zero, shipped on the monotonic-low-risk rationale: a bigger table
    # cannot worsen decision quality at fixed nodes and its only downside
    # (DRAM bandwidth) was exercised at the full 223-worker load = net +).
    # Motivated by the user's hashfull capture (a single deep search fills
    # ~half the 48 MB table). CONFIRMED into v47 at 23 (192 MB, eighteenth
    # campaign, A/B vs Old Engine/46 2026-07-13: +3.16 +/-6.8 @10k, 50.46%,
    # pair ratio 1.03, norm +6.54 -- the 96->192 MB increment; net-positive
    # at full load = bandwidth hasn't bitten, so same monotonic-low-risk
    # ship as v46). MEMORY-SCALING CLOSES HERE: +5.94 then +3.16 is halving
    # each doubling, so 24 (384 MB) would gain ~+1.5 = sub-noise; not worth
    # a campaign (RAM would still fit at ~85 GB). The UCI Hash option (cuci)
    # maps MB onto this; a resize wipes the table. 22 = v46 exact.
    TT_BITS = 23

    # Simplify-at-500 (v30's use_simplify ported: material-diff bonus past a
    # >=500cp gate; v30's 200cp version A/B'd -14, traded into drawn endings).
    # DROPPED FROM THE QUEUE 2026-07-13 -- not on the final_improvements plan
    # (it survives only as one cheap screen inside FI-14, low-prio). Kept as a
    # dormant off-by-default toggle: threshold 0 (off) = v36 eval exactly,
    # node-exact, so it costs nothing to leave. Pushed via csearch_set_simplify.
    USE_SIMPLIFY = False
    SIMPLIFY_THRESHOLD = 500

    # P-14 (CONFIRMED v33, +23.52 +/-6.8 vs v32): KEEP the C TT across
    # irreversible root moves. v30's wipe-on-capture/pawn-move rule existed
    # because its dict TT grew unbounded and dead entries wasted memory; the
    # C table is fixed-size with generation-aware replacement and
    # full-key-checked probes, and repetition/50-move draws are decided
    # BEFORE the TT probe -- so the wipe only discarded still-reachable
    # entries (the whole subtree behind the irreversible move) on a very
    # frequent event. False = v32's exact behavior.
    TT_KEEP_WARM = True

    # P-47: per-line check-extension budget (v30's MAX_CHECK_EXT recipe).
    # 5 = v36 node-exact. Raise-to-8 REJECTED 2026-07-10: -4.59 +/-6.8 @10k
    # vs v36 (49.34%, pair ratio 0.96, norm -9.09) -- deeper check lines
    # cost more than they find at this TC; extensions vein confirmed thin
    # (P-01 +6.8, P-43 +3.5 marginal, P-47 -4.6). Do not re-try at this TC.
    CHECK_EXT_BUDGET = 5

    # PV-02 (CONFIRMED into v37, 2026-07-10): skip TT cutoffs/narrowing at
    # PV nodes so the triangular PV (PV-01, always on) is complete
    # end-to-end -- the standard strong-engine rule; the TT move still
    # orders. A/B vs Old Engine/36 @ 50+0.20 10k: +0.17 +/-6.8 (pair ratio
    # 1.02) -- a clean null, i.e. the exact PV is FREE; kept ON as a
    # correctness feature (it fixed matetrack's ~60% Bad-PV rate).
    # False restores v36's search.
    PV_EXACT = True

    # FI-09(a): a forced move (exactly one legal reply) is played instantly,
    # banking the whole time budget -- no tree change, pure clock save. Armed
    # together with FI-09(b) (easy-move fast-out) as one bundle A/B; False =
    # shipped v47 clock behavior (the CE_LADDER never sees a single-reply root).
    SINGLE_REPLY_INSTANT = False

    # FI-09(b): easy-move fast-out -- when the best root move leads the 2nd-best
    # by >= EASY_MARGIN_CP for EASY_ITERS consecutive iterations (depth >=
    # EASY_MIN_DEPTH), bank the clock by capping the soft-stop at EASY_FRAC.
    # Scales INTO the U-06 machinery (min with the stability frac), never a new
    # clock path. second-best = cs_search_root's out_second, an UPPER bound on
    # the true 2nd-best (failing scouts fail soft), so the test is conservative
    # -- it never over-claims dominance. False = shipped v47 clock (only affects
    # TIMED search; the fixed-depth CE_LADDER is untouched).
    EASY_MOVE = False
    EASY_MARGIN_CP = 250
    EASY_ITERS = 3
    EASY_MIN_DEPTH = 8
    EASY_FRAC = 0.35

    # v30 time-management / aspiration constants (ports, same values)
    ASPIRATION_MIN_DEPTH = 4
    ASPIRATION_DELTA = 30                    # centipawns; C scores are cp too
    SOFT_STOP_STABLE_FRAC = 0.40
    SOFT_STOP_UNSTABLE_FRAC = 0.80
    SOFT_STOP_STABLE_ITERS = 2
    MAX_DEPTH_CAP = 245                       # ID-loop ceiling only. The REAL
                                             # depth limit is the C core's
                                             # CS_MAXPLY=64: negamax returns the
                                             # eval once ply>=64 (arrays g_killers
                                             # /g_seval[64], g_path[64+8]), so the
                                             # engine cannot search past ~64 ply
                                             # no matter this value. At 45+0.1 the
                                             # soft-stop ends near depth ~22, so
                                             # this cap is never reached in play;
                                             # a fixed-depth call >64 just repeats
                                             # identical iterations (safe, the ply
                                             # guard prevents overflow -- P-01
                                             # check exts +<=5 ply graze it, eval
                                             # cut). To truly search deeper, raise
                                             # CS_MAXPLY in csearch.c + resize the
                                             # arrays; this Python constant alone
                                             # does nothing for depth.

    def __init__(self):
        self._pymod = _load_pyengine()
        # The param sync below re-runs _sync_c_params, which early-returns
        # when engine.py fell back to pure-Python eval -- csearch.so would
        # then silently keep eval_c.c's COMPILED-IN defaults (which differ
        # from the engine's tuned values). Refuse to construct instead.
        if not self._pymod._USE_C_EVAL:
            raise RuntimeError(
                "engine.py loaded without eval_c.so (pure-Python fallback) "
                "-- cengine's eval-param sync would be skipped. Rebuild via "
                "./setup.sh; if this happens inside a benchmark/match worker "
                "that mixes engine versions in one process, isolate versions "
                "per process (fresh worker per cell).")
        self._py = self._pymod.Engine()      # book + the eval-param oracle
        # Eval toggles under A/B (see class attrs above): applied to the
        # embedded engine BEFORE _sync_c_params pushes them into csearch.so.
        self._py.use_king_shelter = bool(self.USE_KING_SHELTER)
        self._py.use_outpost = bool(self.USE_OUTPOST)
        self._py.use_cantwin = bool(self.CANTWIN)          # CW-01 mirror
        # FI-27: mirror simplify too -- flipping USE_SIMPLIFY for its queued
        # re-test must not split the GUI eval bar (evaluate_position -> _py)
        # from the C search's eval. And use_pin_eval is the ONE
        # _evaluate_static input with no C port: a Python-era experiment
        # flipping it would silently desync the oracle -- refuse loudly.
        self._py.use_simplify = bool(self.USE_SIMPLIFY)
        self._py.SIMPLIFY_THRESHOLD = int(self.SIMPLIFY_THRESHOLD)
        assert not self._py.use_pin_eval, \
            "use_pin_eval has no C port; the eval oracle would desync"

        lib = ctypes.CDLL(os.path.join(_DIR, "csearch.so"))
        # BUG-04: must match the NEWEST abi whose exports this file calls
        # (cs_search_root's out_second / FI-09b is abi 11) -- bump together
        # with csearch_abi.
        if lib.csearch_abi() < 11:
            raise RuntimeError("csearch.so too old -- rebuild via ./setup.sh")
        # FI-27: csearch.so links its OWN eval_c.c -- a shortcut rebuild that
        # touched eval_c without relinking csearch would silently drift the
        # param interface. Same gate engine.py applies to eval_c.so.
        if lib.abi_version() != self._pymod._EVAL_C_ABI:
            raise RuntimeError("csearch.so embeds a stale eval_c ABI -- "
                               "rebuild via ./setup.sh")
        # FI-27: the eval-setter argtypes were declared on eval_c.so's CDLL
        # only; ctypes argtypes live per-handle, so the PRODUCTION path ran
        # untyped. Copy engine.py's declarations onto this handle -- a future
        # signature change now fails loudly here too.
        for _n in ("set_mobility_params", "set_positional_params",
                   "set_mobility_area", "set_outpost_params",
                   "set_phalanx_params", "set_rook_on_7th_params",
                   "set_shelter_params", "set_space_params",
                   "set_storm_params", "set_threats_params"):
            try:
                getattr(lib, _n).argtypes = \
                    getattr(self._pymod._eval_lib, _n).argtypes
            except AttributeError:
                pass
        # FB-04 + FB-18: one process = one config. Checked BEFORE any
        # lib.set_* / eval-sync call -- a rejected second construction must
        # never have already retargeted the process-wide globals under the
        # first instance (the gui.py EvE bug class); the tuple includes
        # TT_BITS (a construction-time free+calloc of the SHARED table) and
        # TT_KEEP_WARM (per-move wipe policy on it).
        global _SYNCED_FINGERPRINT
        fp = (self.USE_KING_SHELTER, self.USE_OUTPOST, self.USE_SIMPLIFY,
              self.SIMPLIFY_THRESHOLD, self.CHECK_EXT_BUDGET, self.PV_EXACT,
              self.SCORE_HYGIENE, self.EP_FILTER, self.QS_EVICT_MAX,
              self.CB2, self.CANTWIN, self.NULL_VERIFY, self.LMR_HIST,
              self.TT_EVAL_SHARPEN, self.SEE_PRUNE, self.ROOT_ORDER,
              self.TT_BITS, self.TT_KEEP_WARM)
        if _SYNCED_FINGERPRINT is not None and _SYNCED_FINGERPRINT != fp:
            raise RuntimeError(
                "cengine: two different Engine configs in one process -- "
                "csearch.so's eval params/toggles are process-wide; run the "
                "second config in its own process")
        _SYNCED_FINGERPRINT = fp
        B = ctypes.c_uint64
        BOARD_ARGS = [B] * 8 + [ctypes.c_int] * 2 + [B]
        lib.cs_search_begin.argtypes = [ctypes.POINTER(B), ctypes.c_int,
                                        ctypes.c_double]
        lib.cs_search_root.argtypes = BOARD_ARGS + [ctypes.c_int] * 3 + \
            [ctypes.c_uint32, ctypes.c_int, ctypes.POINTER(B),
             ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
             ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        lib.cs_search_root.restype = ctypes.c_uint32
        lib.cs_board_key.argtypes = BOARD_ARGS
        lib.cs_board_key.restype = B
        lib.cs_tt_probe_move.argtypes = BOARD_ARGS
        lib.cs_tt_probe_move.restype = ctypes.c_uint32
        lib.cs_get_pv.argtypes = [ctypes.POINTER(ctypes.c_uint32),
                                  ctypes.c_int]
        lib.cs_get_pv.restype = ctypes.c_int
        self._lib = lib
        lib.set_check_ext_budget(int(self.CHECK_EXT_BUDGET))   # P-47
        lib.set_pv_exact(1 if self.PV_EXACT else 0)            # PV-02
        lib.set_score_hygiene(1 if self.SCORE_HYGIENE else 0)  # CB-01
        lib.set_ep_filter(1 if self.EP_FILTER else 0)          # EP-01
        lib.set_qs_evict_max(int(self.QS_EVICT_MAX))           # FI-08/Q-03
        lib.set_cb2(1 if self.CB2 else 0)                      # CB-02
        lib.set_cantwin(1 if self.CANTWIN else 0)              # CW-01
        lib.set_lmr_hist(int(self.LMR_HIST))                   # FI-04
        lib.set_tt_eval_sharpen(1 if self.TT_EVAL_SHARPEN else 0)  # FI-25
        lib.set_see_prune(1 if self.SEE_PRUNE else 0)          # FI-18
        lib.set_root_order(1 if self.ROOT_ORDER else 0)        # FI-06
        lib.set_null_verify(1 if self.NULL_VERIFY else 0)      # NV-01
        lib.set_tt_bits(int(self.TT_BITS))                     # FI-10 (Hash)
        # FB-06: cengine is AUTHORITATIVE over every behavioral C toggle --
        # a stale .so or drifted compiled-in default must not silently change
        # the search. Values = the confirmed ledger state (all defaults, so
        # this is node-identical; the selftest ladder is the drift detector).
        for setter, val in (("set_use_tt", 1), ("set_prune", 1),
                            ("set_qsearch", 1), ("set_order_mode", 1),
                            ("set_iir", 1), ("set_check_ext", 1),
                            ("set_qgen", 1), ("set_qs_tt", 1),
                            ("set_qs_lazy", 1), ("set_staged", 1),
                            ("set_single_reply", 0), ("set_improving", 0),
                            ("set_cont_hist", 0)):
            getattr(lib, setter)(val)
        # FB-19: the six P-26 selectivity knobs, pushed authoritatively too
        # (values = the compiled defaults, so this is a node-identical no-op
        # TODAY; a drifted default or stale .so now fails the ladder instead
        # of silently changing every non-UCI campaign).
        lib.set_rfp(80, 6)
        lib.set_null_move(2, 6)
        lib.set_fut_margin(150)
        lib.set_delta_margin(200)
        lib.set_lmp(6, 10, 14)
        lib.set_lmr_div(200)
        # FB-04: entries scored under a PREVIOUS construction's eval params
        # would poison this one (the table is process-global and persistent).
        # First construction: the table is empty, reset is a no-op.
        lib.cs_tt_reset()

        # --- sync every eval parameter from the live engine.py instance --- #
        # 1. mobility/king-safety & friends: csearch.so links its OWN copy of
        #    eval_c.c's globals (whose compiled-in defaults DIFFER from the
        #    engine's values), so re-run _sync_c_params against this library.
        orig = self._pymod._eval_lib
        self._pymod._eval_lib = lib
        try:
            self._py._sync_c_params()
        finally:
            self._pymod._eval_lib = orig
        # 2. base/pawn/mop-up tables for the C static eval.
        eng = self._py
        IA = lambda seq: (ctypes.c_int * len(seq))(*seq)
        order = [chess.PAWN, chess.KNIGHT, chess.BISHOP, chess.ROOK,
                 chess.QUEEN, chess.KING]
        lib.csearch_set_eval(
            IA([v for pt in order for v in eng.mg_tables[pt]]),
            IA([v for pt in order for v in eng.eg_tables[pt]]),
            IA([0] + [eng.MG_VALUES[pt] for pt in order]),
            IA([0] + [eng.EG_VALUES[pt] for pt in order]),
            IA([0] + [eng.PHASE_WEIGHTS[pt] for pt in order]),
            eng.TEMPO, eng.DOUBLED_PAWN, eng.ISOLATED_PAWN, eng.BACKWARD_PAWN,
            IA(eng.PASSED_PAWN_MG), IA(eng.PASSED_PAWN_EG),
            eng.MOPUP_MIN_ADV, eng.MOPUP_STRONG_CMD_WEIGHT,
            eng.MOPUP_STRONG_KING_WEIGHT,
        )
        # 3. contempt draw scoring.
        lib.csearch_set_draw(eng.CONTEMPT, eng.DRAW_AVOID_MARGIN)
        # 4. simplify-at-500 (threshold 0 = off = v36 eval exactly).
        lib.csearch_set_simplify(
            int(self.SIMPLIFY_THRESHOLD) if self.USE_SIMPLIFY else 0,
            int(eng.SIMPLIFY_WEIGHT))

        # --- host-visible state (battle_worker contract) ------------------ #
        self.use_book = True
        # Tablebase probe (delegated to the embedded engine, root-only), OFF
        # by default like v30. When on, it is additionally gated to
        # *difficult* positions: at ~2.5M nps the search converts clearly
        # won endings on its own faster than the network round-trip, so the
        # probe only fires when the previous search's verdict was NOT
        # already decisive (see TB_DIFFICULT_CP).
        self.use_tb = False
        self.TB_DIFFICULT_CP = 500           # |last score| >= this: skip probe
        self.pv_uci = True
        # Lazy SMP: helper search threads inside csearch.so (shared lockless
        # TT, per-thread everything else). Default 1 -- the SMP Elo gain is
        # not yet A/B-measured, so multi-threading is strictly opt-in (set
        # this attr, or the Threads option in cuci.py). CLAUDECHESS_SMP env
        # honored like engine.py.
        # FB-13c: clamp to the C-side ceiling (set_threads clamps at 64
        # silently -- the Python attr must not misrepresent the real count).
        self.smp_workers = min(256, max(1, int(os.environ.get(
            "CLAUDECHESS_SMP", "1"))))
        # FB-09: optional node budget (UCI `go nodes N`); None = unlimited.
        self.node_limit = None
        self.nodes_searched = 0
        self.last_score = 0                  # White POV, v30 mate convention
        self.last_depth = 0
        self.last_pv = ""
        # Host-owned abort flag (engine.py's P-05 ownership rule): set by
        # stop(), NEVER cleared by the engine itself -- the host clears it
        # before starting the next search (cuci.py's `go`, experiment.py's
        # _maybe_start_engine). This closes the stop-vs-go race that
        # cs_stop() alone cannot: a stop landing before the search thread
        # reaches cs_search_begin was ERASED there (begin resets the C
        # g_abort), so a `go infinite` + quick `stop` searched to the depth
        # cap and hung the UCI host in search_thread.join().
        self._abort = False
        # v30 live-stats surface (experiment.py's heartbeat reads BOTH of
        # these mid-search): .nodes updates per completed ID depth, and
        # .start_time is the search's perf_counter start.
        self.nodes = 0
        self.start_time = 0.0
        # GUI contract (experiment.py / WebChess): per-completed-depth and
        # final info callbacks, same record dicts v30 emits.
        self.on_depth = None
        self.on_final = None
        self.search_log = []
        # P-35/U-06 knobs, same semantics as engine.py.
        # TIME-POLICY TUNE RESOLVED 2026-07-13 (nineteenth 50+0.20 campaign vs
        # Old Engine/47): base soft-stop 0.60 read +1.29 +/-6.8 (norm +2.79,
        # SPRT LLR -0.009 dead-null) -> REVERTED to 0.55 (v47). Base-frac tuning
        # is exhausted (P-35 +38 -> U-06 +11 -> X-09 null -> 0.60 null); the
        # remaining time-policy idea is U-06 refinement (score-drop panic /
        # second-move gap = FI-22 stage 3), not the base fraction.
        self.soft_stop_frac = 0.55
        self.use_stability_time = True
        # (reentrancy lock is CLASS-level -- see _SEARCH_LOCK below)

    # ------------------------------------------------------------------ #
    # GUI helpers (experiment.py / WebChess use these beyond battle API)
    # ------------------------------------------------------------------ #
    def evaluate_position(self, board):
        """Terminal-aware static eval, White's perspective -- delegated to
        the embedded Python engine (bit-exact the same evaluation)."""
        return self._py.evaluate_position(board)

    @property
    def book_path(self):
        """Book probing is delegated to the embedded engine, so the book
        override (WebChess 'book file' picker) must reach IT, not us."""
        return self._py.book_path

    @book_path.setter
    def book_path(self, value):
        self._py.book_path = value

    def _emit(self, record, final=False):
        self.search_log.append(record)
        cb = self.on_final if final else self.on_depth
        if cb is not None:
            cb(record)

    # ------------------------------------------------------------------ #
    # ctypes marshaling helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _bargs(board):
        ep = board.ep_square if board.ep_square is not None else -1
        return (board.pawns, board.knights, board.bishops, board.rooks,
                board.queens, board.kings,
                board.occupied_co[chess.WHITE], board.occupied_co[chess.BLACK],
                1 if board.turn else 0, ep, board.clean_castling_rights())

    @staticmethod
    def _key_to_move(key):
        """15-bit C move key -> chess.Move (promo PT ids match python-chess)."""
        if not key:
            return None
        promo = (key >> 12) & 7
        return chess.Move(key & 63, (key >> 6) & 63, promotion=promo or None)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def _clear_stale_abort(self):
        """FB-10: `_abort` is host-owned (the host clears it before its next
        `go`) -- but a DIRECT API caller who did stop() after a finished
        search would otherwise get an instant garbage move from the next
        call. A set flag with NO search running is by definition stale;
        a stop aimed at a live search is untouched (the lock is held).

        FB-21: while a host-issued `go` is IN FLIGHT (`_go_pending`, set by
        cuci before thread.start()), a set flag is NOT stale -- it is a
        stop that raced the search thread's startup (buffered stdin
        delivers `go\nstop` before the child runs a bytecode). Erasing it
        here made `go infinite` + quick `stop` search to the depth cap
        with the host wedged in join(). The pending window closes below,
        once the flag has been sampled by the starting search."""
        if getattr(self, "_go_pending", False):
            return
        if self._abort and not Engine._SEARCH_LOCK.locked():
            self._abort = False

    def get_best_move(self, board, depth):
        self._clear_stale_abort()
        self._go_pending = False             # FB-21: window closed
        return self._search(board, None, depth)

    def get_best_move_timed(self, board, time_limit, max_depth=245):
        # Default = MAX_DEPTH_CAP so the clock, not the cap, is the limit --
        # the old default of 10 silently capped ad-hoc timed searches (the C
        # core passes depth 10 in well under a second).
        self._clear_stale_abort()            # FB-10
        self._go_pending = False             # FB-21: window closed
        return self._search(board, time_limit, max_depth)

    def stop(self):
        """Host-requested abort (UCI `stop`): the search unwinds and the
        driver returns the best move found so far.

        Two signals, covering both sides of the race with the search start:
        `_abort` survives cs_search_begin (which clears the C-side g_abort),
        so a stop that lands BEFORE the search thread arms the C search still
        aborts at the ID loop's next depth check instead of being lost. The
        host clears `_abort` before its next search (see __init__)."""
        self._abort = True
        self._lib.cs_stop()

    # ------------------------------------------------------------------ #
    # Iterative deepening driver (port of v30's get_best_move_timed loop)
    # ------------------------------------------------------------------ #
    # PROCESS-wide, not per-instance: csearch.so's search state (deadline,
    # abort flag, game-history keys, TT generation) is per-PROCESS, so the
    # serialization must be too. A per-instance lock let gui.py's
    # Engine-vs-Engine mode (TWO Engine instances, one csearch.so) race:
    # instance B's cs_search_begin cleared the shared abort flag while
    # instance A was still unwinding its deadline abort, so A's root loop
    # accepted a garbage-scored move as best and PLAYED it (the observed
    # "[19] c6d8 ... [Final] h6e6" queen blunder).
    _SEARCH_LOCK = threading.Lock()

    def _search(self, board, time_limit, max_depth):
        """Serialized search entry: LAST CALLER WINS. If any Engine in this
        process starts a search while one is running (host bugs observed in
        both experiment.py and gui.py EvE), abort the in-flight search and
        take over once it fully unwinds."""
        if not Engine._SEARCH_LOCK.acquire(blocking=False):
            self._lib.cs_stop()              # old search unwinds within ms
            Engine._SEARCH_LOCK.acquire()    # serialized takeover
        try:
            return self._search_impl(board, time_limit, max_depth)
        finally:
            Engine._SEARCH_LOCK.release()

    def _search_impl(self, board, time_limit, max_depth):
        t0 = time.perf_counter()
        prev_verdict = self.last_score       # previous MOVE's score (TB gate)
        self.nodes_searched = 0
        self.nodes = 0
        self.start_time = t0                 # heartbeat NPS reads this live
        self.last_score = 0
        self.last_depth = 0
        self.last_pv = ""
        self.search_log = []

        legal = list(board.legal_moves)
        if not legal:
            return None

        # FI-09(a): forced move -> play it instantly, bank the whole budget.
        # A single legal reply cannot be improved by search, book, or TB, so
        # short-circuit them all. last_score carries the prior verdict forward
        # (like the book path) so the TB difficulty gate stays armed.
        if self.SINGLE_REPLY_INSTANT and len(legal) == 1:
            only = legal[0]
            self.nodes_searched = self.nodes = 0
            self.last_depth = 0
            self.last_score = prev_verdict
            self.last_pv = only.uci()
            record = {"depth": 0, "move": only.uci(), "score": 0, "nodes": 0,
                      "time_ms": 0, "pv": only.uci()}
            self._emit(record)
            self._emit(dict(record, final=True), final=True)
            return only

        # Opening book (delegated; instant when it hits, like v30).
        if self.use_book:
            self._py.use_book = True
            book = self._py._book_move(board)
            if book is not None:
                # UCI hosts surface the move via the info pv (a bare depth-0
                # line was indistinguishable from a no-move result); depth
                # stays 0 + "book": True as the machine-readable marker.
                self.last_pv = book.uci()
                record = {"depth": 0, "move": book.uci(), "score": 0,
                          "nodes": 0, "time_ms": 0, "book": True,
                          "pv": book.uci()}
                self._emit(record)
                self._emit(dict(record, final=True), final=True)
                # FI-27: keep the TB difficulty gate armed across book moves
                # (last_score=0 would force a probe on the first post-book
                # move regardless of how decisive the game already is).
                self.last_score = prev_verdict
                return book

        # Tablebase probe (root-only, delegated to the embedded engine which
        # already skips trivial wins / insufficient material / too many
        # pieces). cengine adds the DIFFICULTY gate: if the previous move's
        # search verdict was already decisive, the search converts on its
        # own faster than the network round-trip -- skip the probe.
        if self.use_tb and abs(prev_verdict) < self.TB_DIFFICULT_CP:
            self._py.use_tb = True
            tb_to = self._py.tb_timeout
            if time_limit is not None:
                tb_to = min(tb_to, max(0.0, time_limit * 0.5))
            tb = self._py._tb_probe(board, tb_to)
            if tb is not None:
                wdl, tb_move = tb            # move already verified legal
                score_white = ((wdl if board.turn == chess.WHITE else -wdl)
                               * self._py.TB_SCORE_UNIT)
                self.last_score = score_white
                self.last_pv = tb_move.uci()
                record = {"depth": 0, "move": tb_move.uci(),
                          "score": score_white, "nodes": 0, "time_ms": 0,
                          "tb": True, "wdl": wdl, "pv": tb_move.uci()}
                self._emit(record)
                self._emit(dict(record, final=True), final=True)
                return tb_move

        # TT retention: v30's rule wiped on every irreversible root move
        # (halfmove_clock == 0); P-14 keeps the table warm instead (see the
        # class attr). With the toggle off this is v32's exact behavior.
        if board.halfmove_clock == 0 and not self.TT_KEEP_WARM:
            self._lib.cs_tt_reset()

        # Game-history keys for repetition detection: positions BEFORE the
        # root, most recent first, only as far as the halfmove clock reaches.
        hist = []
        h = board.copy()
        for _ in range(min(board.halfmove_clock, len(h.move_stack))):
            h.pop()
            hist.append(self._lib.cs_board_key(*self._bargs(h)))
        arr = (ctypes.c_uint64 * max(1, len(hist)))(*hist)
        self._lib.set_threads(int(self.smp_workers))     # Lazy SMP
        # FB-09: node budget (0 = unlimited); node-identical when unset.
        self._lib.set_node_limit(
            ctypes.c_uint64(int(self.node_limit) if self.node_limit else 0))
        # FB-11: book/TB/history setup time comes OUT of the budget -- the C
        # deadline armed below must not extend the move past the allocation
        # (a 2s TB stall on a 3s budget used to spend 5s). Sub-5ms setup
        # (the normal path) is left alone: bit-identical clock behavior.
        if time_limit is not None:
            setup = time.perf_counter() - t0
            if setup > 0.005:
                # FB-24: floor never EXCEEDS the original budget (a 20ms
                # zeitnot allocation must not become 50ms)...
                time_limit = max(min(time_limit, 0.05), time_limit - setup)
                # ...and elapsed restarts here so the ID loop's soft-stop
                # doesn't subtract the setup a SECOND time (v30 never
                # mutated the budget; the port double-counted).
                t0 = time.perf_counter()
        self._lib.cs_search_begin(arr, len(hist),
                                  float(time_limit) if time_limit else 0.0)

        bargs = self._bargs(board)
        hmc = board.halfmove_clock
        best_key = 0
        prev_score = None
        reached_depth = 0
        nodes = 0
        # U-06 stability tracking (port)
        stab_prev = None
        stab_iters = 0
        stab_changed = False
        easy_iters = 0                       # FI-09(b): easy-move streak

        for depth in range(1, min(max_depth, self.MAX_DEPTH_CAP) + 1):
            if self._abort:
                break        # host stop() landed before/between C calls; the
                             # C-side g_abort covers stops DURING a cs_search_
                             # root call -- this covers the gaps around them
            key, score, nodes, done, aborted, second = self._root_aspiration(
                bargs, depth, best_key, prev_score, hmc)
            if aborted:
                # v30 partial-iteration rule: the PV move is searched first,
                # so >= 1 completed root move means the partial result is
                # same-or-better than the previous depth's move.
                if done >= 1 and key:
                    best_key = key
                break

            # completed iteration
            if stab_prev is not None:
                if key == stab_prev:
                    stab_iters += 1
                    stab_changed = False
                else:
                    stab_iters = 0
                    stab_changed = True
            stab_prev = key
            # FI-09(b): easy-move streak -- best clearly ahead of the field
            if (self.EASY_MOVE and depth >= self.EASY_MIN_DEPTH
                    and second > -CS_INF
                    and score - second >= self.EASY_MARGIN_CP):
                easy_iters += 1
            else:
                easy_iters = 0
            best_key = key
            prev_score = score
            reached_depth = depth
            self.nodes = nodes               # live-stats heartbeat surface

            # live search info (GUI contract), v30's record shape
            if self.on_depth is not None or self.on_final is not None:
                dmv = self._key_to_move(key)
                self.last_pv = self._extract_pv(board, dmv, depth)
                self._emit({
                    "depth": depth,
                    "move": dmv.uci() if dmv else "----",
                    "score": self._white_v30(score, board.turn),
                    "nodes": nodes,
                    "time_ms": int((time.perf_counter() - t0) * 1000),
                    "pv": self.last_pv,
                })

            if abs(score) > CS_MATE_THRESH:
                break                        # forced mate found
            if time_limit is not None:
                elapsed = time.perf_counter() - t0
                soft = self.soft_stop_frac
                if soft is not None and self.use_stability_time:
                    if stab_changed:
                        soft = self.SOFT_STOP_UNSTABLE_FRAC
                    elif stab_iters >= self.SOFT_STOP_STABLE_ITERS:
                        soft = self.SOFT_STOP_STABLE_FRAC
                # FI-09(b): a dominant move banks even more of the clock
                if (soft is not None and self.EASY_MOVE
                        and easy_iters >= self.EASY_ITERS):
                    soft = min(soft, self.EASY_FRAC)
                if elapsed >= time_limit or (
                        soft is not None and elapsed >= soft * time_limit):
                    break

        move = self._key_to_move(best_key)
        if move is None or move not in board.legal_moves:
            move = legal[0]                  # safety net; must never trigger

        # --- stats in v30 conventions (battle_worker reads these) -------- #
        self.nodes_searched = nodes
        self.nodes = nodes
        self.last_depth = reached_depth
        self.last_score = self._white_v30(
            prev_score if prev_score is not None else 0, board.turn)
        self.last_pv = self._extract_pv(board, move, max(reached_depth, 1))
        self._emit({
            "depth": reached_depth,
            "move": move.uci() if move is not None else "----",
            "score": self.last_score,
            "nodes": nodes,
            "time_ms": int((time.perf_counter() - t0) * 1000),
            "pv": self.last_pv,
            "final": True,
        }, final=True)
        return move

    def _white_v30(self, score_c, turn):
        """CS_INF-relative stm score -> White-POV score in v30's MATE_SCORE
        convention (what battle_worker/GUIs expect)."""
        s = score_c
        if abs(s) > CS_MATE_THRESH:
            plies = CS_INF - abs(s)
            s = (1 if s > 0 else -1) * (self.MATE_SCORE - plies)
        return s if turn == chess.WHITE else -s

    def _root_aspiration(self, bargs, depth, prev_key, prev_score, hmc):
        """v30's aspiration wrapper: narrow window around the previous score,
        geometric widening on fail, full-window fallback."""
        if (depth < self.ASPIRATION_MIN_DEPTH or prev_score is None
                or abs(prev_score) >= CS_MATE_THRESH):
            return self._root(bargs, depth, -CS_INF, CS_INF, prev_key, hmc)
        delta = self.ASPIRATION_DELTA
        alpha = max(-CS_INF, prev_score - delta)   # FB-26: well-formed even
        beta = min(CS_INF, prev_score + delta)     # at near-mate prev_score
        provisional = 0                      # CB-02/FB-23: best PROVEN move
        while True:
            res = self._root(bargs, depth, alpha, beta, prev_key, hmc)
            if res[4]:                       # aborted: caller handles
                if self.CB2 and res[3] == 0 and provisional:
                    # FB-23a: the re-search died before finishing its first
                    # move -- play the move a completed call PROVED >= beta
                    # this depth, not the previous iteration's refuted one.
                    return (provisional, res[1], res[2], 1, True, res[5])
                return res
            score = res[1]
            if score <= alpha:               # fail low: widen downward
                alpha = max(-CS_INF, score - delta)
            elif score >= beta:              # fail high: widen upward
                if self.CB2 and res[0]:
                    # FB-23b: adopt the proven-better move as this depth's
                    # provisional best and order it FIRST in the re-search
                    # (v30's _partial_root_move rule, finally ported).
                    provisional = res[0]
                    prev_key = res[0]
                beta = min(CS_INF, score + delta)
            else:
                return res
            delta *= 2
            if delta >= 2 * self.ASPIRATION_DELTA * 32:
                return self._root(bargs, depth, -CS_INF, CS_INF, prev_key, hmc)

    def _root(self, bargs, depth, alpha, beta, prev_key, hmc):
        nodes = ctypes.c_uint64(0)
        score = ctypes.c_int(0)
        done = ctypes.c_int(0)
        aborted = ctypes.c_int(0)
        second = ctypes.c_int(0)             # FI-09(b): 2nd-best root score
        key = self._lib.cs_search_root(
            *bargs, depth, alpha, beta, prev_key, hmc,
            ctypes.byref(nodes), ctypes.byref(score),
            ctypes.byref(done), ctypes.byref(aborted), ctypes.byref(second))
        return (key, score.value, nodes.value, done.value, aborted.value,
                second.value)

    def _extract_pv(self, board, first_move, max_len):
        """PV-01: the exact line the search actually proved (the C triangular
        table, cs_get_pv), extended past any truncation by the old TT walk
        (legality-checked, stops on repetition). The exact prefix is emitted
        in full even beyond max_len (a mate PV must reach the mate); only the
        speculative TT tail respects the cap. Falls back to the pure TT walk
        (v30's _extract_pv) when the C PV is empty or disagrees with the
        chosen move (fail-low final iteration, partial abort)."""
        if first_move is None:
            return ""
        buf = (ctypes.c_uint32 * 128)()
        n = self._lib.cs_get_pv(buf, 128)
        if n == 0 or self._key_to_move(buf[0]) != first_move:
            n = 0                            # fallback: pure TT walk
        b = board.copy(stack=False)
        out = []
        seen = set()
        i = 0
        mv = self._key_to_move(buf[0]) if n else first_move
        while mv is not None:
            if i >= n and len(out) >= max_len:
                break                        # cap applies to the TT tail only
            if mv not in b.legal_moves:
                break
            try:
                out.append(mv.uci() if self.pv_uci else b.san(mv))
            except Exception:
                break
            b.push(mv)
            k = b._transposition_key()
            if i >= n and k in seen:
                break                        # TT walk may cycle; the exact
            seen.add(k)                      # prefix is finite by construction
            i += 1
            if i < n:
                mv = self._key_to_move(buf[i])
            else:
                mv = self._key_to_move(
                    self._lib.cs_tt_probe_move(*self._bargs(b)))
        return " ".join(out)
