"""
engine62.py -- FROZEN SNAPSHOT of v62 (2026-08-31).
===========================================================================

Snapshot of cengine.py at the v62 milestone. v62 ARMS FI-21/S10, THE
ASPIRATION-WINDOW REPAIR (ASPIRATION_FI21 True). The root window used to open
flat at 30cp regardless of position and widen 2x per fail, and a fail-low left
beta where it was -- so a position could oscillate low/high and pay the whole
ladder out to the 1920 fallback. Three riders replace that as ONE policy, and
they are not separable toggles: the opening delta scales with the previous
score (14 + prev^2/16384), a fail-low pulls beta to the midpoint before alpha
drops (Stockfish's order), and growth is 1.5x.

Driver-only. The C core is byte-identical to v61 and the CB-02/FB-23
provisional-move machinery is preserved verbatim.

vs Old Engine/61, same nnue_v12 net on both arms so the window policy is the
only variable. Screen 10+0.1: ACCEPT H1, LLR +2.969 over 6,687 games. Shipping
instrument 50+0.5: +18.40 +/- 7.6 over 3,439 games (52.65%, ptnml
62/361/735/450/109, nElo +28.25), SPRT[0,4] LLR +2.959 -> ACCEPT H1, STOPPED
EARLY. Both runs crossed a bound, so both carry the stopping bias that turned
FI-115's +33.83 into +19.11 at a fixed budget -- a fixed-budget 50+0.5 re-run
is owed before this is treated as settled. LEDGER STAYS AT ~+354: per the
v60 precedent a bound-stopped magnitude does not advance it.

Bench signature 1,203,792 (was 1,140,099): unlike FI-115 this one DOES move
the bench, because the aspiration window shapes the root search from the first
iteration and needs no warm TT to engage.

The only survivor of the 2026-08-31 campaign: eleven candidates, six
rejections, four nulls, one skipped.

NNUE_FILE is resolved against THIS FOLDER, so it climbs back to the repo
root ("../../NNUE/nets/...") rather than carrying a 3 MB copy of the net.
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

    # FI-85 x-ray slider mobility REMOVED 2026-07-24: SCREEN-KILLED
    # (-4.52 +/-15.3, do-not-retry) and its gating in all six slider loops
    # cost NPS while dormant. See eval_c.c.

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

    # FI-76 wrong-bishop clamp REMOVED 2026-07-24: SCREEN-NULL (+0.17
    # +/-15.3, ~9% pair engagement, 46 up / 44 down inside it) and it cost
    # per-node work at both eval return sites while gated off. See csearch.c
    # for the do-not-re-add condition.

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
    #
    # FI-105 (2026-07-29) ABANDONED PRE-SCREEN on the EBF gate, and found a
    # defect in the verdict above while doing it. **At the documented armed
    # divisor 8192 this is a DEAD GATE**: bench reads 1,461,732 -- byte
    # identical to baseline -- so the mechanism never fires there. Same trap
    # FI-23 recorded ("armed at 256, NOT the spec's 8192 -- that measured as a
    # dead gate"): cs_search_begin zeroes g_history every move, so within one
    # search it rarely passes a few hundred and hist/8192 clamps to 0. The
    # +2.15 +/-6.8 campaign therefore measured a mechanism at the very edge of
    # engagement, and the divisor was never tuned because the result fell
    # below the tune threshold -- circular.
    #
    # Re-armed at LIVE divisors on top of FI-103+FI-104, EBF vs baseline:
    #     pair alone        -7.49%
    #     + LMR_HIST 2048   -7.02%   (worse)
    #     + LMR_HIST  512   -3.32%   (much worse)
    # It claims better reductions and does not deliver them, so the one-sided
    # gate abandons it. This was R10's designated falsification point.
    #
    # SCREENED 2026-07-30 anyway, because the same gate produced a FALSE
    # NEGATIVE on FI-107 (abandoned, then shipped at +4.11). Armed as
    # CUTNODE_LMR + LMR_HIST 2048, without TTPV_LMR (+31.8% nodes on its own):
    # -1.51 +/- 12.4 over 3,000 games, LLR -0.404 of -2.944. CLOSED here --
    # a formal reject costs ~18,900 more games to prove a negative nobody
    # would ship. The gate was right about this one; it is only discredited
    # for constant-factor changes, which this is not.
    # Finer-quiet-signal vein now 0-for-4 at this TC.
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
    #
    # RE-EXAMINED 2026-07-30 and the verdict STANDS, but the note above lacked
    # the counter-argument, so here it is measured. Warm table across a real
    # game at 1.4s/move (the 50+0.20 operating point), hashfull in permille:
    #
    #             ply 8   ply 16   ply 24   ply 32   ply 40
    #   192 MB      833      974      995     1000     1000
    #   768 MB      329      564      721      813      886
    #
    # 192 MB is FULLY SATURATED from move 16 and stays full for the rest of
    # every game -- every store after that evicts something. A cold search
    # hides this completely (bench hashfull reads 17 permille), which is why
    # it had never been seen.
    #
    # It still does not justify a raise, for a reason that is about the
    # HARNESS rather than the engine: match.py runs TWO engine processes per
    # worker and each allocates its own table, so the default is multiplied by
    # 2N. At 111 workers, 768 MB is 170 GB and 384 MB is 85 GB; on a 16 GB
    # laptop 768 MB caps local runs at ~5 workers instead of ~20. Measuring a
    # ~+1.5 item would cost a 4x slower campaign. Hash is exposed over UCI up
    # to 20 GB -- serious long games set it there, which is the right place
    # for this knob to live.
    TT_BITS = 23
    # FI-115 CONFIRMED and ARMED 2026-08-29. Dead-entry TT replacement: an
    # entry whose stored piece count exceeds the root's is provably
    # unreachable (material is irreversible), so it is evicted first, and a
    # still-reachable old entry is depth-protected instead of clobbered on
    # age alone. +15.89 +/- 4.2 Elo at 50+0.5 with 192 MiB on BOTH sides --
    # the shipped table size on the release instrument, 10,000 games at a
    # FIXED budget so the number carries no stopping bias.
    TT_DEADTAG = True

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
    # banking the whole time budget -- no tree change, pure clock save.
    # FI-09 BUNDLE RESOLVED 2026-07-14 (twentieth 50+0.20 campaign vs Old
    # Engine/47, 10k games): +0.69 +/-6.8 (norm +1.49, SPRT LLR -0.314, no
    # decision within budget) -- dead-null, single-reply/easy-move roots are too
    # rare at this TC to move the needle. REVERTED to False (shipped v47 clock
    # behavior; the CE_LADDER never saw a single-reply root either way). Kept as
    # dormant infrastructure, not deleted -- do-not-retry at this TC.
    SINGLE_REPLY_INSTANT = False

    # FI-09(b): easy-move fast-out -- when the best root move leads the 2nd-best
    # by >= EASY_MARGIN_CP for EASY_ITERS consecutive iterations (depth >=
    # EASY_MIN_DEPTH), bank the clock by capping the soft-stop at EASY_FRAC.
    # Scales INTO the U-06 machinery (min with the stability frac), never a new
    # clock path. second-best = cs_search_root's out_second, an UPPER bound on
    # the true 2nd-best (failing scouts fail soft), so the test is conservative
    # -- it never over-claims dominance. NULL alongside FI-09(a) in the same
    # bundle A/B (see above) -- REVERTED to False 2026-07-14 (shipped v47 clock;
    # only affects TIMED search, the fixed-depth CE_LADDER is untouched).
    EASY_MOVE = False                        # FI-09 BUNDLE NULL, do-not-retry
    EASY_MARGIN_CP = 250
    EASY_ITERS = 3
    EASY_MIN_DEPTH = 8
    EASY_FRAC = 0.35

    # FI-23: history-driven quiet pruning -- LMP prunes by move-count only;
    # this adds the signal sibling, skipping quiets the EXISTING butterfly
    # history has consistently punished (same shallow/non-PV/not-in-check/
    # non-check-giving gate as LMP/FI-18). Reuses g_history read-only, no new
    # bookkeeping or ABI change. 0 = off = v47 node-exact; threshold is a
    # magnitude on the +-HIST_MAX=16384 scale. Armed at 256 after an
    # engagement sweep (8192 through 512 measured bit-identical to off --
    # cs_search_begin zeroes g_history every move, so one search's history
    # rarely swings a slot past a few hundred; see git history for the sweep).
    # FI-23 REJECTED 2026-07-16 (twenty-first 50+0.20 campaign vs Old
    # Engine/47): -5.23 +/-7.1 @9,243 games, pair ratio 0.92, norm -10.89,
    # SPRT[0,4] LLR -2.955 ACCEPT H0 (stopped early) -- a real negative, not
    # a null. With FI-18's -1.25 the shallow quiet/capture-prune vein is
    # 0-for-2 in the C era: within-search history is too thin a signal at
    # depth <= 3 to beat the ordering that already buried those moves.
    # REVERTED to 0 (dormant, do-not-retry at this TC); mechanism kept.
    HIST_PRUNE = 0

    # FI-30: (a) QS_TT_SHARPEN -- FI-25's rule applied at qsearch's
    # stand-pat: on a TT hit whose bound didn't cut, the entry's SEARCH
    # value replaces the static eval as the stand-pat wherever the bound
    # provably improves it (LOWER above / UPPER below / EXACT; non-mate);
    # the FI-03 TT-eval cache keeps the RAW eval (raw_stand split).
    # (b) QS_KEEP_MOVE -- a stand-pat (move-0) store keeps a same-key
    # entry's best move (FB-22's rule applied to qs_tt_store).
    # CONFIRMED 2026-07-16 => v48 (twenty-second campaign vs Old
    # Engine/47, four pooled tranches = 21,605 games @ 50+0.20):
    # +4.73 +/-3.19 (50.68%, ptnml 606/2518/4309/2714/655, pair ratio
    # 1.08, norm +9.70), pooled GSPRT[0,4] LLR +3.475 crossing the
    # +2.944 accept bound -- the C era's first sequential-test ACCEPT.
    # A premature revert at the 10k cap was walked back: the SPRT said
    # CONTINUE, and extensions ran until it decided (never cap a
    # sequential test at a fixed budget again).
    QS_TT_SHARPEN = True
    QS_KEEP_MOVE = True

    # FI-29: cuckoo upcoming-repetition (van Kervinck / SF has_game_cycle).
    # is_repetition only sees repetitions already ON the path; the cuckoo
    # table (8192 slots, one Zobrist delta per reversible non-pawn move)
    # detects that the side to move can FORCE one with a single move, so
    # the node scores the contempt draw a full search earlier -- pruning
    # lost shuffle subtrees and banking perpetual half-points sooner.
    # In-tree only, never in check, alpha-raise (not hard return); a match
    # that would strip castling rights is rejected (key-soundness beyond
    # SF's envelope). KEPT-ON-NULL => v49 2026-07-17 (twenty-third
    # campaign vs Old Engine/48, 10,000 games @ 50+0.20): +0.97 +/-6.8
    # (50.14%, pair ratio 1.02, norm +2.01, GSPRT[0,4] LLR -0.19) -- the
    # pre-registered correctness-class rule ships the null, the sixth of
    # its class (EP-01/CB-01/CB-02/PV-02/CW-01 precedent). Build gates:
    # CYCLE_VERIFY 13,272 claims / 0 mismatches over 1.1M nodes; paired
    # matetrack noise-flat (899/773 on vs 905/774 off); blocked-pawn
    # fortress d16 11,893 -> 4,310 nodes, score snaps to 0.
    CYCLE_DETECT = True

    # FI-50/51/52: the qsearch-TT batch -- FI-30's direct descendant, three
    # non-overlapping toggles ganged as one campaign (the grouped-toggle
    # precedent FI-30 set):
    #  (50) QS_BETA_NARROW -- narrow beta from a TT_UPPER qsearch hit (the
    #       CB-01(e) alpha-narrow's mirror; negamax has done both all along).
    #  (51) QS_TTM_EXEMPT  -- the qsearch TT move dodges the losing-SEE skip and
    #       delta pruning (a nonzero stored bm beat stand-pat at store time).
    #  (52) QS_CHK_D1      -- in-check RESOLVED qsearch stores tagged depth 1 so
    #       negamax's TT_DEPTH>=depth gate can cut directly from them.
    # NULL 2026-07-18 (twenty-fourth campaign vs Old Engine/49, 10,000 games
    # @ 50+0.20): -0.28 +/-6.8 (49.96%, ptnml 295/1177/2055/1187/286, pair
    # ratio 1.00, norm -0.57, GSPRT[0,4] LLR -0.797 no decision, trend flat)
    # -- a dead null, NOT correctness-class => all three REVERTED to False
    # (dormant, mechanisms kept). The never-cap doctrine wasn't triggered:
    # it protects tests trending toward a bound (FI-30's LLR climbed and
    # never reversed); this one sat flat-to-negative. Not split for
    # attribution (~$54 to price three likely-zeros against symmetric
    # ptnml); FI-50 alone stays a cheap solo re-run candidate (its entry
    # priced it keep-on-null-adjacent, unexercisable from a batch verdict).
    # Paired matetrack had PASSED pre-verdict (ON 907/778 vs OFF 900/773
    # found/best mates on mates2000 @0.5s -- noise-flat, no tactical or
    # PV-integrity regression; the mechanisms are safe, just not worth Elo).
    QS_BETA_NARROW = False
    QS_TTM_EXEMPT = False
    QS_CHK_D1 = False

    # FI-48: flag-aware TT replacement -- shield same-key EXACT entries from
    # equal-depth bound-only overwrites; level 2 adds a +2 cross-key
    # effective-depth bonus for EXACT incumbents. CLOSED AS DEAD GATE
    # 2026-07-18 pre-A/B (FI-23/P-33 doctrine: never spend a 10k on a config
    # that barely runs). Instrumented count under the full production config
    # (PV-02 on, warm TT): level 1 fired 26x over ~4M+ nodes of bench +
    # timed search (577k same-key store checks -> 26 qualified); level 2
    # added ~170 cross-key blocks per ~20M timed nodes -- ~0.001% either
    # way. STRUCTURAL cause, not tuning: a node that would overwrite an
    # equal/deeper same-key EXACT entry is cut off by that very entry at
    # its own probe before it can store (only PV-02-skipped PV nodes
    # escape), and cross-key pressure is starved because the 192MB table
    # does not saturate at 50+0.2 (same reason FI-08 nulled / FI-20 is
    # gated). Mechanism kept for a cheap re-measure if the TT shrinks, the
    # TC lengthens, or FI-20's hashfull gate ever shows saturation.
    TT_KEEP_EXACT = 0

    # FI-49: TT fail-high depth tightening (SF-standard) -- an equal-depth
    # TT_LOWER whose value would cut (v >= beta, non-mate) needs TT_DEPTH >=
    # depth+1 before the negamax cutoff/narrowing block fires. REJECTED
    # 2026-07-18 (twenty-fifth campaign vs Old Engine/49, 10,000 games @
    # 50+0.20, seed 42): -3.65 +/-6.8 (49.48%, ptnml 310/1234/2002/1159/295,
    # pair ratio 0.94, norm -7.44, GSPRT[0,4] LLR -2.403 -- 82% of the way
    # to the H0 bound at full budget). A reject-lean null, not flat: the
    # +28% fixed-depth node cost was not bought back, exactly as the paired
    # matetrack's ~-1.5% best-mate dip predicted (pre-registered
    # corroboration). The warm-cross-move-TT argument for SF's rule does
    # not transfer to Pygin at this TC. REVERTED to False (dormant,
    # do-not-retry at this TC); mechanism kept.
    TT_FH_TIGHT = False

    # FI-53 + FI-54: the store/probe-policy pair. KEPT-ON-NULL => v50
    # 2026-07-18 (twenty-sixth campaign vs Old Engine/49, 10,000 games @
    # 50+0.20, first on rotated SUBSET_SEED 50): +1.60 +/-6.8 (50.23%,
    # ptnml 278/1155/2106/1165/296, pair ratio 1.02, norm +3.33, GSPRT[0,4]
    # LLR +0.117 flat) -- the pre-registered correctness-class rule ships
    # the null, the seventh and eighth releases of the class (EP-01/CB-01/
    # CB-02/PV-02/CW-01/FI-29 precedent). Build gates had leaned positive:
    # paired matetrack ON 905/777 vs OFF 893/768 (the mate machinery
    # visibly helps mate-finding), KQvK@hmc95 correctly scored 0.
    #  FI-53 TT_R50      -- at hmc>=90 refuse TT cutoffs/narrowing for
    #       decisive-but-non-mate stored values (|v|>=500cp): the promised
    #       win may not be convertible before the rule draw. Mates and
    #       quiet values still cut (mate finds never lost by construction).
    #  FI-54 TERM_STORE  -- terminal mate/stalemate returns write a
    #       permanent TT_EXACT entry at sentinel depth 200 (a forced mate
    #       is depth-invariant); provably safe half.
    #  FI-54 TT_MATE_CUT -- negamax probe cuts on mate-range TT values
    #       regardless of stored depth (GHI exposure = SF's accepted
    #       tradeoff; if matetrack shows wrong mates, arm TERM_STORE alone).
    # All False = v49 node-exact.
    TT_R50 = True
    TERM_STORE = True
    TT_MATE_CUT = True

    # FI-56: root-move LMR -- late (i>=4) quiet
    # non-promotion root moves that neither respond to nor give check are
    # scouted at depth-1-R (R = g_lmr[d][i]/2, cap depth-2, depth>=3), with
    # a full-depth zero-window verify before the full-window re-search --
    # negamax's standard cascade, now at the root. Deliberately overturned
    # the "no reductions at root" design stance; 3/4-convergent,
    # SF-standard. CONFIRMED => v51 2026-07-18 (twenty-seventh campaign vs
    # Old Engine/50, seed 50): 2k screen +17.56 +/-15.3 (CI > 0), main
    # tranche (offset 1000) ACCEPT H1 at 7,343 games (+9.37 +/-8.0, LLR
    # +2.957 > +2.944, stopped early) -- the C era's SECOND SPRT accept;
    # pooled 9,343 games: +11.12 +/-5.3 (51.60%, ptnml 220/996/1988/1173/
    # 282, ratio 1.20, pooled LLR +4.549). The -28% fixed-depth node cut
    # converted to depth at fixed time (matetrack +28 mates presaged it).
    # False = v50 node-exact.
    ROOT_LMR = True

    # FI-55: IIR weak-evidence trigger -- P-03 reduces on a missing TT move;
    # this also reduces when the TT move exists but is weak ordering
    # evidence: a TT_UPPER entry stored shallower than the current depth
    # (the move is whatever the fail-low search last tried -- no cutoff
    # evidence, ordering nearly as blind as a miss). Current-SF trigger
    # form (!ttMove || bound == UPPER); IIR_MIN_DEPTH/!in_chk gates kept;
    # the F1 depth-gap sub-variant is NOT built and now stays that way.
    # SCREEN-KILLED 2026-07-19 (twenty-eighth candidate vs Old Engine/51,
    # seed 51, 2k screen): -9.04 +/-15.2 (48.70%, ptnml 63/250/408/234/45,
    # pair ratio 0.89, norm -18.90) -- a negative lean on a +0-2 prior
    # fails the screen gate; no 10k spent. CALIBRATION LESSON recorded:
    # the paired matetrack had read the STRONGEST result on the books
    # (+100 mates, 1049/877 vs 949/811) and the screen still leaned
    # negative -- matetrack magnitude does NOT predict Elo (it measures
    # tactics-finding; the re-firing reduction mis-ranks quiet positions).
    # REVERTED to False (dormant, do-not-retry at this TC); mechanism
    # kept, abi 20 stays.
    IIR_WEAK = False

    # FI-64: LMR on SEE-losing captures -- badcaps (ordered dead last,
    # almost never best) share the g_lmr reduction table instead of getting
    # full-depth zero-window scouts. Reduction NOT pruning: a reduced
    # badcap that fails high re-searches at full depth via the PVS ladder,
    # so no move is ever lost (deep sacs seen one iteration later at
    # worst) -- unlike the closed FI-18 pruning vein. The FI-04 history
    # nudge is quiet-gated in the same edit (butterfly history is
    # quiet-only). SCREEN-KILLED 2026-07-21 (twenty-ninth candidate vs Old
    # Engine/51, seed 51 -- the first screen on the nodes instrument,
    # 2k @ --nodes 2M NPS-calibrated on the cheap server): -10.95 +/-15.3
    # (48.43%, ptnml 40/281/427/206/46, pair ratio 0.79, norm -24.07).
    # The earlier GCloud TIMED screen had read +2.78 +/-15.2 -- the two
    # reads straddle null within joint noise; combined evidence
    # null-to-negative on a +0-2 prior = no 10k. The FI-18 diagnosis
    # ("these subtrees already fail low fast -- alpha-beta gets the skip
    # for free") stands as the likely story. REVERTED to False (dormant,
    # do-not-retry at this TC); mechanism kept, abi 21 stays; the FI-04
    # quiet-gate fix inside the widened block survives (latent-bug value).
    LMR_BADCAP = False

    # P-26 selectivity spins as VISIBLE class attrs (previously hardcoded in
    # the _sync_c_params push below; exposing them is the sweep's precondition
    # -- house rule: no hidden values, a candidate is an attr change here).
    # SWEEP POINT 1 ARMED 2026-07-21 (thirtieth campaign vs Old Engine/51,
    # the selectivity lane's zero-code opener): LMR_DIV 200 -> 170, i.e.
    # every LMR reduction scales by ~1.18 (R = 0.75 + ln(d)ln(m)/1.70).
    # Fixed-depth engagement is the depth-thesis direction: bench
    # 1,083,772 -> 965,336 (-10.9%; 185 read -4.9%, 150 -12.8% and
    # flattening -- 170 is the knee). Known exposure: over-reduction of
    # late quiets (tactical misses) -- paired matetrack is the gate, and
    # the interior-reduction 0-for-2 record (FI-55/64) is priced in: this
    # scales the CONFIRMED-good existing reduction structure rather than
    # adding new reduction sites. NULL_BASE 2->3 was measured and PARKED:
    # +17.5% fixed-depth nodes (R+1 pushes shallow nulls below the child-
    # depth floor, losing null pruning where the tree is widest) -- anti-
    # thesis, screen it only if the sweep exhausts better points.
    # (2, 6, 200) = v51 node-exact. NOT correctness-class: revert on null;
    # a kept point re-pins and the next point sweeps from there.
    # POINT 1 VERDICT: NULL 2026-07-21 (thirtieth campaign, split 2k screen
    # @ nodes 1.75M on two cheap servers, pooled): +0.69 (50.10%, ptnml
    # 56/230/412/258/44, ratio 1.06, LLR -0.063 dead flat) -- LMR
    # aggressiveness is measurably FLAT near the default at this
    # resolution; reverted to 200, sweep advances to the next lever.
    NULL_BASE = 2
    NULL_DIV = 6
    LMR_DIV = 200

    # FI-24(a)+(b): the null-move refinement batch, ARMED 2026-07-21 for
    # the thirty-first campaign vs Old Engine/51 (nodes@1.75M standard).
    # Two toggles, ONE campaign per the entry's pre-registration (same
    # null-mechanism family -- the FI-30 batching precedent, not the
    # FI-50/51/52 anti-pattern):
    #  (a) NULL_NODOUBLE -- no null-after-null (prev12 sentinel): two
    #      stand-pats in a row prove nothing and hide zugzwang 2 plies in.
    #  (b) NULL_EVALR -- R += (prune_eval-beta)/200 capped +2: deep nulls
    #      only at clearly-winning nodes; the shallow-null population is
    #      untouched, so the measured NULL_BASE cliff cannot recur.
    # Both False = v51 node-exact. CONFIRMED => v52 2026-07-21 (thirty-first
    # campaign vs Old Engine/51, nodes@1.75M NPS-calibrated, split across two
    # cheap servers): split 2k screen +15.82 pooled (LLR +1.403), then two
    # 5k tranche halves (+4.79 / +10.84); POOLED 12,000 games **+6.63 +/-4.5**
    # (50.95%, ptnml 257/1376/2490/1558/319, pair ratio 1.15, pooled
    # GSPRT[0,4] LLR **+4.533 ACCEPT**) -- the C era's THIRD SPRT accept and
    # the first campaign confirmed on the nodes instrument. Owes the
    # one-time timed cross-check (instrument validation, pre-registered).
    NULL_NODOUBLE = True
    NULL_EVALR = True

    # FI-63: SF-style quietCheckEvasions -- in-check qsearch nodes are the
    # last node population with ZERO pruning; after QS_EVASION_CAP
    # fully-searched quiet evasions the rest are skipped (captures and
    # promotion evasions ALWAYS searched), never while the node still reads
    # mated (best <= -MATE_THRESH), so a mate can't be concluded from a
    # pruned set. Known unsoundness, delta-pruning class: a capped fail-low
    # node stores TT_UPPER at the searched best -- a wrong-way bound if a
    # skipped evasion was better, which the TT-quality machinery (P-44/
    # FI-30 sharpening) then reads; paired matetrack is the PRIMARY gate.
    # CLOSED AS A DEAD GATE 2026-07-21, pre-A/B (FI-48 precedent, second
    # of its class): the feature has NO useful operating point. Cap sweep
    # at fixed depth -- 2: 1,163,657 nodes (+10.5%!), 3: +0.5%, 4: +0.3%,
    # 6: ~0 (vacuous). At the spec's armed value it ENGAGES but costs a
    # tenth of the tree (the named wrong-way TT_UPPER mis-bounding forcing
    # re-search upstream); at cap>=3 it barely fires at all, because
    # in-check qsearch nodes rarely hold more than two quiet evasions. And
    # the PRIMARY gate failed: paired matetrack ON 930/802 vs OFF 948/811
    # (-18 found, -9 best) -- the skipped-saving-evasion mode is real, not
    # theoretical. Against an entry priced +0-1 Elo, no screen is
    # warranted. Mechanism kept at 0 (v52 node-exact, abi 23); re-measure
    # only if qsearch evasion ordering changes materially.
    QS_EVASION_CAP = 0

    # P-33 REVISIT: singular extensions. Rejected in the PYTHON era (null
    # @depth 8, negative @depth 6) -- but that measured a depth-~8 engine,
    # and singular is the classic technique whose value scales WITH depth;
    # the C core now searches ~19. At a non-root node with a deep-enough
    # TT move (TT_DEPTH >= depth-3, LOWER/EXACT, non-mate), a reduced
    # zero-window search with that move EXCLUDED runs at a lowered beta;
    # if every other move fails below it, the TT move is singular and gets
    # +1 ply (spending from the P-01 check-extension budget, so stacking
    # stays bounded). Inside an exclusion search the node takes no TT
    # cutoff and writes no TT entry -- both would be exclusion-relative.
    # ARMED for the thirty-second campaign vs Old Engine/52 (seed 52,
    # nodes@1.75M) at the MEASURED point, not the textbook one. The
    # spec-default 8/64 costs +57% nodes on the d11 bench and +25% at d16
    # -- FI-49 died at +28%, so that config was never viable on a
    # fixed-node instrument. Swept at d16 (the d11 bench is BLIND above
    # min_depth 11 -- it reads 0% there because no such nodes exist, a
    # measurement artifact worth remembering):
    #   10/128 +25.0%, 12/128 +24.6%, 14/128 +13.4%, 14/192 -2.8%, 16 vacuous
    # 14/192 is the point where the extensions pay for their own
    # verification searches (fewer total nodes than baseline while still
    # firing -- it differs from the off-run, unlike the vacuous 16). In
    # game conditions (~1.75M nodes/move, depth ~17-19) nodes at depth
    # >= 14 are a real population. False = v52 node-exact. NOT
    # correctness-class: revert on null.
    # CLOSED 2026-07-21 pre-A/B, on TWO independent matetrack failures --
    # the Python-era verdict (null @d8, negative @d6) earns its second
    # confirmation, now at depth ~19 where the "singular scales with depth"
    # argument should have favored it. Run 1 (extension spending from the
    # P-01 chk budget): ON 905/754 vs OFF 939/803 = -34 found / -49 best.
    # Hypothesis: singular was starving the check extensions that find
    # mates. Fix built (SE_BUDGET, an INDEPENDENT per-line cap threaded
    # through negamax; cost -2.8% -> +1.8% nodes @d16). Run 2: ON 909/763
    # vs OFF 943/807 = -34 / -44 -- essentially IDENTICAL damage, so the
    # hypothesis was WRONG and the loss is intrinsic to extending the TT
    # move here: in mate-bearing lines the mating move often is NOT the TT
    # move, and the extra ply spent on the TT move is effort taken from it.
    # Parameter sweeps recorded for the graveyard (d16, 3 positions):
    # 10/128 +25.0%, 12/128 +24.6%, 14/128 +13.4%, 14/192 -2.8%, 16 vacuous;
    # the spec-default 8/64 costs +57% on the d11 bench (FI-49 died at
    # +28%). No screen spent. Mechanism kept in-tree at 0 (v52 node-exact,
    # abi 24); do-not-retry at this TC without a materially different
    # extension rule (e.g. extending on a non-TT-move criterion).
    SINGULAR = False
    SE_MIN_DEPTH = 14
    SE_MARGIN = 192
    SE_BUDGET = 3        # per-line cap, INDEPENDENT of CHECK_EXT_BUDGET

    # FI-59 + FI-60: the ORDERING/HISTORY batch -- the untried mechanism
    # family after extensions and reductions were exhausted (FI-55/63/64 and
    # P-33 all closed or reverted). Two toggles, ONE campaign: both mutate
    # ordering/history only, neither touches the TT or bounds, and each is
    # priced +0-2 alone (batching per the FI-24 same-family precedent, NOT
    # the FI-50/51/52 anti-pattern of ganging independent mechanisms).
    #  FI-59 KILLER_INHERIT -- warm-start an untouched killer slot from two
    #    plies up (same side to move); one 8-byte copy per first-touch node,
    #    no new stage/band, both ordering paths see the same table.
    #  FI-60 QUIET_MALUS_ALL -- a bad-capture/promo cutoff also sweeps the
    #    -depth*depth malus over the quiets already tried (cutter not in the
    #    list, so the bound is nq); no bonus, no killer/counter write.
    # ARMING DECISION (measured, not planned): FI-59 goes SOLO. Per-toggle
    # cost at d16 (3 positions) -- FI-59 -3.2% nodes (cheaper AND better
    # ordered: the warm killer slot earns its cutoffs), FI-60 **+27.3%**,
    # both +28.7%. FI-60's profile is the one that killed FI-49 (+28%,
    # rejected -3.65) and FI-63 (+10.5%, closed): the extra malus traffic
    # drives history more negative, reshaping LMR/pruning into a bushier
    # tree, on a +0-2 prior. Not worth a fixed-node campaign -- PARKED with
    # this data (mechanism kept at 0, re-measure only if the history
    # gravity/clamp changes). Arming FI-59 alone also keeps attribution
    # clean, per the FI-50/51/52 batch-null lesson.
    # FI-58 (mate killers) is deliberately NOT here either: its own entry
    # prices self-play Elo at ~0 and makes matetrack the accept gate, so it
    # belongs to a matetrack decision, not an Elo campaign.
    # FI-59 VERDICT: SCREEN-KILLED 2026-07-21 (thirty-second candidate vs
    # Old Engine/52, split 2k screen @nodes 1.75M, pooled): -5.21 (49.25%,
    # ptnml 56/242/419/242/41, ratio 0.95, LLR -0.657) -- negative lean on
    # a +0-2 prior, no tranche spent. The two reproducible matetrack
    # declines (-15/-14 found) called it correctly after all: stale killers
    # from two plies up displace the real move in forcing lines, and the
    # -3.2% node saving does not buy that back. LESSON REFINED: matetrack
    # magnitude still does not predict Elo (FI-55), but a REPRODUCIBLE
    # decline is worth heeding even outside a feature's named risk mode --
    # the ambiguity call here was wrong, and the screen was the right
    # instrument to settle it for $2. REVERTED to False (dormant,
    # do-not-retry at this TC); SF's own removal of ply-2 killers now has
    # a local confirmation.
    KILLER_INHERIT = False
    QUIET_MALUS_ALL = False

    # FI-12: keep the history table across game moves, halved, instead of
    # wiping it (P-17 from v24, never tried in the C era). Consecutive
    # positions share most of their quiet-move structure, so a decayed table
    # starts move ordering warm the way the TT already does -- and the
    # TT-warm family is 2-for-2 (P-14 +23.5, P-44 +8.1). Killers and
    # countermoves are deliberately NOT kept: they are ply-indexed, so a
    # shifted root makes them wrong rather than merely stale. Watch the P-23
    # stager, which reads live history -- a warm start shifts early-iteration
    # ordering. SCREEN-NULL 2026-07-22 (thirty-third campaign vs Old
    # Engine/53, nodes@1.75M): +1.74 +/-15 pooled over 2,000 games on two
    # servers (ptnml 50/227/429/251/43, ratio 1.06, LLR +0.055), both halves
    # reading +1.74 -- a clean flat, not a noisy one. No 10k spent.
    # The tree really did get 12.8% cheaper at fixed depth and it bought
    # NOTHING: warm history reaches the same moves sooner, it does not find
    # better ones. That closes the history-refinement vein at 0-for-4
    # (Q-01, FI-04, FI-23, FI-12). Dormant, do-not-retry.
    HIST_KEEP = False

    # Lazy-SMP worker count. Plain class default -- no environment
    # read; hosts set it on the instance (match.py --smp).
    SMP_WORKERS = 1

    # FI-67: TT-move-first stage in the lazy qsearch path -- search a
    # validated TT move BEFORE gen_noisy/order_moves; a beta cutoff skips
    # both entirely. BENCH-CLASS: gates replicate the move loop verbatim, so
    # ON is node-identical (any bench drift is a bug) and the ship gate is
    # measured NPS + unchanged signature, no A/B slot. The savings population
    # is only bound-mismatch TT hits -- the depth-ungated value cutoff already
    # returns pre-movegen on most cut-capable nodes -- hence the abandon
    # threshold: < ~0.3% NPS means leave it dormant. MEASURED 2026-07-23:
    # node-identity PASSED everywhere (bench 1,122,753 exact with ON, ladder
    # pins exact, 1.55M-node differential over 8 positions x 11 depths clean)
    # but the NPS median over 32 paired d13 ratios was -0.26% -- below the
    # abandon threshold, exactly the shadowing the estimate predicted. The
    # correct implementation buys nothing on this tree. DORMANT, do-not-ship;
    # revisit only if the qsearch TT cutoff ever grows a depth gate. False =
    # v53 byte-exact.
    QS_TTFIRST = False

    # FI-90: qsearch skips a capture whose SEE is negative, at a threshold of
    # exactly 0 -- Stockfish runs a NEGATIVE margin there, because a
    # marginally-losing capture at a horizon node can still be best (the SEE
    # chain prices one square and cannot see the follow-up). The entire
    # affected population is ONE motif: defended BxN at SEE -10 is the only
    # capture class in the open interval (-100, 0); every other losing class
    # sits at -170 or worse and deserves the skip. Not a value-table artifact
    # -- the engine's own Texel fit prices B over N by a LARGER margin (MG
    # 306/322, EG 342/356) than SEE's 320/330. 0 = v55 node-exact; armed
    # value 100. SCREENED NULL twice 2026-07-27: +4.00 +/-15.2 alone, and
    # -2.95 +/-15.2 batched with FB-48 (LLR +0.268 / -0.423, both flat).
    # Closed -- batching two +0-priced items rescued neither.
    QS_SEE_MARGIN = 0

    # FI-103 (R10 wave 1): cut-node aware LMR. A zero-window child, or any
    # non-first child of a PV node, is expected to FAIL HIGH -- reduce it one
    # ply more. Pygin had zero notion of cut nodes; LMR was indexed on two
    # dimensions with three small adjustments against Stockfish's ~eight
    # signals, which is R10's diagnosis for the lane's 2-for-9 record.
    # PRICED +0 ALONE: FI-104 (ttPv) is the counterweight. False = node-exact.
    # SCREENED 2026-07-27: +2.43 +/-15.2 alone (LLR +0.109) -- the
    # foundation is not harmful, but priced +0 and it reads +0.
    # CLOSED 2026-07-31 on the PRE-REGISTERED stopping rule. Reopened after
    # ProbCut showed a 2k screen is triage (this had been closed on +2.43
    # +/-15.2), and rerun TIMED because --nodes was suspected of
    # under-crediting node savings.
    #
    #   pooled  +2.84 +/- 4.6 over 22,000 games (11,000 pairs), nElo +4.22
    #   penta   557 / 2620 / 4515 / 2702 / 606
    #   LLR     +0.870 -> +0.961 -> +1.698 -> +1.391 -> +1.620 of +2.944
    #
    # The rule was: at 10,000 pairs the LLR must be >= +1.9 or stop. It was
    # +1.391. Stopped. Switching to [0,2] post hoc would not have rescued it
    # either -- that reads +1.174, LOWER, because the effect sits between the
    # bounds in both brackets.
    #
    # TWO THINGS BANKED. (1) The instrument hypothesis is FALSIFIED: --nodes
    # read +2.43 and the clock reads +2.84, so ProbCut's +4.11 -> +11.44 gap
    # was specific to ProbCut, NOT general to node-saving changes -- the
    # backlog's --nodes nulls do not need re-reading. (2) A ~+3 effect is not
    # resolvable at any budget we will pay: 22,000 games left the CI at
    # [-1.8, +7.4]. Pick the SPRT bracket to match the expected effect BEFORE
    # the campaign, never after seeing the data.
    #
    # Mechanism stays off. Not "rejected" -- consistently positive across five
    # tranches and never confirmable. Costs 10.5% nodes; if a future change
    # makes the tree cheaper, it is worth one more look.
    CUTNODE_LMR = False

    # FI-104 (R10 wave 1): ttPv -- a node that was ever on a PV reduces LESS,
    # permanently. The counterweight to FI-103, not an independent idea:
    # cut-node reduction prunes hard everywhere, and this is what stops it
    # pruning hard in lines that once mattered. Storage was free (bit 2 of the
    # TT's 16-bit flag word, which carries a 2-bit bound); TT_FLAG masks to the
    # bound at the macro so ~15 existing comparison sites needed no edit.
    # False = node-exact. SCREENED AS THE PAIR 2026-07-29: -0.69 +/-15.2.
    # EBF fell 7.49% (vs -0.76%/+1.85% for the singles -- a real, large
    # interaction) and it did NOT convert to Elo. R10's thesis holds in
    # tree shape and fails in strength.
    TTPV_LMR = False

    # FI-106 (R10): RAZORING -- the fail-LOW side, absent from Pygin entirely
    # (0 refs before 2026-07-29). RFP covers fail-high, frontier futility
    # covers single moves; nothing covered a node whose eval is so far BELOW
    # alpha that no quiet move plausibly rescues it. A verifying QSEARCH
    # confirms before anything is returned -- never a static score, because
    # static forward pruning is what sank FI-18 and FI-23. 0 = node-exact;
    # armed 400 (+250/ply, depth <= RAZOR_DEPTH).
    #
    # ABANDONED ON THE EBF GATE 2026-07-29, before any screen. It cuts nodes
    # hard at FIXED DEPTH and makes the tree grow FASTER per ply:
    #     off          bench 1,461,732   EBF 1.713
    #     margin 400   bench 1,193,315   EBF 1.778  (+3.77%)
    #     margin 600   bench 1,174,896   EBF 1.795  (+4.74%)
    # A one-time saving that worsens the growth rate is the opposite of what
    # razoring claims. Likely mechanism: early fail-low returns seed the TT
    # with shallow bounds, so deeper nodes get worse information and re-search
    # more. Mechanism kept at 0; do not re-arm without changing what it stores.
    RAZOR_MARGIN = 0
    RAZOR_DEPTH = 3

    # FI-107 (R10): ProbCut -- the fail-HIGH mirror of FI-106, and the one
    # big Stockfish node-saver Pygin has no equivalent of. A shallow search
    # at beta + PROBCUT_MARGIN that fails high is taken as evidence the full
    # search would too. Never static: a qsearch filters, a real reduced
    # search at depth - PROBCUT_RED confirms, and a deeper TT bound vetoes.
    # 0 = off = node-exact; armed 200 at depth >= 5, reduction 4.
    #
    # CONFIRMED 2026-07-30, SPRT ACCEPT: +4.11 Elo over 21,806 games
    # (10,903 pairs), GSPRT[0,4] LLR +2.971 -> ACCEPT H1. Fourth SPRT accept
    # in the program's history, and the first pruning MECHANISM to pay since
    # the search lane was declared exhausted.
    #
    # It was nearly lost twice: the EBF gate said ABANDON (+0.91%) -- a FALSE
    # NEGATIVE, because the gate reads the tree's SLOPE and this is a constant
    # factor (-21.6% nodes at fixed depth, flat growth rate) -- and I then read
    # the SPRT's LLR off the wrong engine and closed it as null. Both are
    # written up in the memory; the operative lesson for this file is that the
    # +15 screen gate is TRIAGE, not the ship threshold.
    # FI-109 (R10, the last item): CORRECTION HISTORY. Static eval is
    # systematically wrong in ways that REPEAT -- for a given pawn structure it
    # reads reliably high or low against what the search then proves. Track
    # that per (side, pawn key) as a depth-weighted EWMA and add it to
    # prune_eval, so RFP, razoring, futility and the null-move gate all get a
    # better number from ONE signal.
    #
    # DISPUTED and that is the point: P-42 measured this at -16.4 in the Python
    # engine at depth ~8. The counter is that FI-25 (+13.52) since proved the
    # prune_eval slot pays in the C core. Pre-registered as a 2k screen.
    #
    # It earns a build despite R10's abandonment rule (FI-105 read null, which
    # by that rule closes the section) because R10's items split by CATEGORY,
    # not by lane: missing-context-SIGNAL items went 0-for-3 (FI-103/104/105),
    # while ABSENT MECHANISMS went 1-for-2 and the winner was FI-107 at +11.44
    # -- shipped with both signal toggles OFF, so it never needed the
    # prerequisites R10 claimed. This is the last absent mechanism in the file.
    #
    # 0 = off = node-exact. The pawn key is computed from scratch inside the
    # gate rather than carried on Board: FI-31's incremental key would add 8
    # bytes to a struct copied at every node, an NPS cost paid even when this
    # is off. Dormant here costs nothing.
    # CLOSED PRE-SCREEN 2026-07-30, no server time spent. The dispute
    # resolves in P-42's favour (it measured -16.4 in the Python engine).
    #
    #   uncapped (+-512cp)  -25.5% nodes, tactics 289 -> 277  (-12)
    #   capped at 64cp      +4.9% nodes,  tactics 289 -> 284  (-5)
    #
    # The -25.5% was not the mechanism working, it was over-pruning driven by
    # an absurd clamp: a historical average was allowed to move prune_eval by
    # half a queen, and all four consumers prune on it at once. Bounded
    # sanely, the correction is BIDIRECTIONAL and costs 4.9% MORE nodes while
    # still losing 5 tactical positions -- worse than baseline on both axes,
    # so there is no path to positive Elo (cf. FI-49, rejected at -3.65 for a
    # node cost alone). Fifth item closed pre-A/B on measurement.
    #
    # WHY IT DOES NOT PAY HERE, which is the transferable part: correction
    # history corrects SYSTEMATIC eval bias. v53's Texel retune refitted 44
    # scalars against game results on Pygin's own self-play -- which is
    # precisely the procedure that removes systematic bias. There is little
    # left for the table to learn. Do not retry unless the eval changes
    # family (an NNUE net would be a different eval, and the argument resets).
    CORR_HIST = False
    CORR_CAP = 64          # max cp the correction may move prune_eval

    PROBCUT_MARGIN = 200
    PROBCUT_DEPTH = 5
    PROBCUT_RED = 4

    # FB-48: contempt is ON by default and every rule draw routes through
    # draw_score -- insufficient material, 50-move, repetition, FI-29's cycle
    # gate, all three qsearch draws. Stalemate does NOT: all five in-tree
    # sites return a literal 0. So with a piece's difference a repetition
    # scores -50 and is correctly avoided while a stalemate -- the same half
    # point -- scores 0. FI-54 makes it persistent (depth-200 TT_EXACT).
    # Ships as ONE edit across both searches; a negamax-only or qsearch-only
    # fix would create a new inconsistency (the FB-40 warning). The ROOT site
    # is deliberately excluded: there n == 0 means the game is over and the
    # value is only reported to the GUI. False = v55 node-exact.
    # SHIPPED 2026-07-27 on a keep-on-null screen: -1.39 +/-15.2 over 2,000
    # games @nodes 1.75M vs Old Engine/55, ptnml [2, 61, 881, 55, 1] -- 88% of
    # pairs in the middle bucket, i.e. almost nothing moved, exactly as a
    # 0.363%-of-nodes endgame-only population predicts. Bench 1,461,732 and
    # the full CE_LADDER are UNCHANGED with it on, so it re-pins nothing.
    SM_CONTEMPT = True

    # FI-89: repetition against the ROOT or the pre-root GAME HISTORY draws on
    # the FIRST match, while match.py's arbiter (python-chess
    # can_claim_threefold_repetition) needs the THIRD. True adopts SF's
    # Position::is_draw split -- in-tree matches (k < ply) still draw on the
    # first, matches at or before the root need a second occurrence further
    # back -- at BOTH sites: is_repetition AND FI-29's upcoming_repetition,
    # which has the identical hole and would otherwise keep a false contempt
    # floor at the parent. Engagement is narrow by construction (needs
    # hmc >= ply, i.e. shallow plies in high-halfmove-clock positions --
    # grindy endings and shuffle phases, not ordinary middlegame nodes).
    # False = v55 node-exact. SCREEN-KILLED 2026-07-27: -20.17 +/-15.3,
    # which took the correctness class from 8-for-8 to 8-for-9.
    REP_STRICT = False

    # FI-15 NNUE (Phases 1-5 BUILT-DORMANT 2026-07-18): hybrid NN eval --
    # nn_eval replaces the HCE as negamax's static eval, qsearch stand-pat
    # stays HCE (the old MLP project's -203/-273 lesson), the FI-03 TT eval
    # cache is depth-gated per F49-B02 so the two scales never cross.
    # Architecture: KA8T king-bucketed features + T16 threat vector,
    # 6144->2x256->528->32->32->1 quantized int16/int8 (DESIGN_nnue.md
    # "Phase 1 spec" is the frozen contract; NNUE/README.md has every
    # command). CONFIRMED and ARMED 2026-08-03 with the v4 net: engine_nnue_v4
    # vs Old Engine/57 on a clock 50s+0.20, x86, GSPRT[0,4] ACCEPT H1 at 1,702
    # pairs (3,404 games) -- LLR +2.950, +19.11 +/- 7.8 Elo, ptnml
    # 71/358/691/477/105, ratio 1.36. Ledger +305 -> +324, the second-largest
    # confirmed gain after the v53 Texel retune, and the FIRST net to pay.
    # What changed between v3 (+0.52 +/- 6.8 on the same x86 instrument, i.e.
    # nothing) and v4 was the TRAINING, not the net: same dataset, same
    # dimensions, a cosine LR schedule instead of a flat one took held-out val
    # 0.074417 -> 0.066663. Identical dims means identical NPS, so the whole
    # +19 is judgement, not speed. OWED: an arm64 confirmation -- v3 read
    # +5.70 +/- 4.6 there against +0.52 here, so the architecture spread is
    # real and only x86 has been measured for v4 (risk direction is
    # favourable, arm64 read HIGHER for v3).
    # Net naming convention: NNUE/nets/nnue_vN_<first 12 hex of the file's
    # own sha256>.nnue -- Stockfish's scheme (nn-<12 hex>.nnue), so a net
    # can never be silently swapped for a different net under the same
    # name; vN keeps the human-readable ORDER the hash cannot express
    # (minor bump vN.M for small same-data fixes). NNUE/train.py stamps
    # the hash on at export (config.stamp_net_hash); retired nets move
    # FLAT into "NNUE/Old NNUE/". toy.nnue is the pipeline-proof artifact,
    # not a version, and is exempt from the hash.
    # Only read when USE_NNUE is True (load fails loudly if the file does
    # not exist -- no silent HCE fallback).
    USE_NNUE = True
    NNUE_FILE = os.path.join("..", "..", "NNUE", "nets",
                             "nnue_v12_bf86c4ced057.nnue")   # v60
    # FI-104. The net's value is a RACE between its better judgement and the
    # nodes that judgement costs: v3 measured +5.70 +/- 4.6 while conceding
    # 30.6% of its nodes to a SIMD build. On a CPU with neither NEON nor AVX2
    # the tail falls back to the scalar loop -- ~3x slower again -- and the
    # deficit swallows the eval whole, so the net makes the engine WORSE. That
    # is a bad default to ship to a machine nobody here has measured. With
    # this True, a scalar build refuses to arm the net and plays the HCE.
    # Set it False to measure the scalar path deliberately (the only reason
    # to want it): a visible in-file line, not a hidden env switch.
    NNUE_REQUIRE_SIMD = True

    # FI-106 (CONFIRMED. Best measurement 2026-07-31: engine_nnue_lazy vs
    # Old Engine/56 -- the CURRENT engine, ProbCut armed on BOTH sides -- on a
    # clock 50s+0.20, arm64, FULL 2,000-game budget with NO early stop:
    # +19.30 +/- 15.3 Elo (52.78%), nElo +29.43, Ptnml 40/203/420/280/57,
    # pair ratio 1.39, 95% lower bound +4.00. Unbiased: the point estimate is
    # not conditioned on crossing a bound.
    # An earlier read of +33.83 was vs v55 with ProbCut off, and STOPPED EARLY
    # at the accept bound -- superseded, and its magnitude was inflated by the
    # stopping rule. x86 is weaker (+5.91 vs v55, 4,000 games), but ~2/3 of
    # that gap is the AVX2 tail running 27% off its own machine's pace, which
    # is fixable code rather than an architectural fact -- see NNUE/README.md.)
    # v59 (2026-08-10): ISOLATED and CONFIRMED -- same v4 net both sides,
    # this toggle the only difference, vs Old Engine/58: GSPRT[0,4] LLR
    # +2.950 ACCEPT at 2,264 pairs (ptnml 69/509/975/595/116, pooled score
    # 51.99% -> +13.84 +/- 6.4, stopped early so the magnitude is
    # bound-biased). The package numbers above priced NNUE+lazy together;
    # this is the toggle alone, and it pays. Flipped True as v59.
    LAZY_NNUE = True
    LAZY_NNUE_MARGIN = 200

    # FI-105. Which shared object to load. Only an instrumented build has any
    # business changing this (NNUE/tools/lazy_probe.py compiles one with
    # -DCS_LAZY_PROBE), and it MUST keep a distinct filename: dyld resolves by
    # name, so a second .so sharing this one's name returns the first image
    # already mapped and the process silently searches with the wrong engine.
    CSEARCH_SO = "csearch.so"

    # v30 time-management / aspiration constants (ports, same values)
    ASPIRATION_MIN_DEPTH = 4
    ASPIRATION_DELTA = 30                    # centipawns; C scores are cp too
    ASPIRATION_FI21 = True                   # FI-21/S10 window policy, v62.
                                             # OFF restores the v61 window
                                             # (flat delta, 2x growth, no
                                             # midpoint pull) for A/B only.
    SOFT_STOP_STABLE_FRAC = 0.40
    # How long a PV may get. The EXACT prefix -- the line the search actually
    # proved, from the C triangular table -- is always emitted in full, past
    # this and past any cap (a mate PV must reach the mate). This bounds only
    # the SPECULATIVE TT tail that continues the line after the proven part
    # runs out. It used to be the current DEPTH, which is why every info line
    # came out ~depth plies and looked truncated; raised to the cs_get_pv
    # buffer size on the user's call 2026-07-30 so analysis shows the whole
    # line. The tail still stops on an illegal move, a repetition, or a dry TT,
    # so this is a ceiling and not a target.
    PV_MAX_LEN = 128

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

    def _eval_fingerprint(self):
        """FB-55: sha1 of the exact eval payload csearch_set_eval receives.

        Mirrors the push below field for field -- if a field is added there
        and not here, the guard silently stops covering it, so keep the two
        in lockstep (the FB-41/FB-42 lesson)."""
        import hashlib
        eng = self._py
        order = [chess.PAWN, chess.KNIGHT, chess.BISHOP,
                 chess.ROOK, chess.QUEEN, chess.KING]
        parts = [
            [v for pt in order for v in eng.mg_tables[pt]],
            [v for pt in order for v in eng.eg_tables[pt]],
            [eng.MG_VALUES[pt] for pt in order],
            [eng.EG_VALUES[pt] for pt in order],
            [eng.PHASE_WEIGHTS[pt] for pt in order],
            [eng.TEMPO, eng.DOUBLED_PAWN, eng.ISOLATED_PAWN, eng.BACKWARD_PAWN],
            list(eng.PASSED_PAWN_MG), list(eng.PASSED_PAWN_EG),
            [eng.MOPUP_MIN_ADV, eng.MOPUP_STRONG_CMD_WEIGHT,
             eng.MOPUP_STRONG_KING_WEIGHT],
            [eng.DOUBLED_PAWN_EG, eng.ISOLATED_PAWN_EG, eng.BACKWARD_PAWN_EG],
            [eng.CONTEMPT, eng.DRAW_AVOID_MARGIN],
        ]
        blob = ";".join(",".join(str(int(v)) for v in p) for p in parts)
        return hashlib.sha1(blob.encode()).hexdigest()[:16]

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

        lib = ctypes.CDLL(os.path.join(_DIR, self.CSEARCH_SO))
        # BUG-04: must match the NEWEST abi whose exports this file calls
        # (abi 35 = FI-103/104/106/107 set_cutnode_lmr/ttpv_lmr/razor/probcut) -- bump with csearch_abi.
        if lib.csearch_abi() < 36:
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
                   "set_storm_params", "set_threats_params",
                   "set_xray_mob"):
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
              self.TT_BITS, self.TT_KEEP_WARM, self.HIST_PRUNE,
              self.QS_TT_SHARPEN, self.QS_KEEP_MOVE, self.CYCLE_DETECT,
              self.QS_BETA_NARROW, self.QS_TTM_EXEMPT, self.QS_CHK_D1,
              self.TT_KEEP_EXACT, self.TT_FH_TIGHT, self.TT_R50,
              self.TERM_STORE, self.TT_MATE_CUT, self.ROOT_LMR,
              self.USE_NNUE, self.NNUE_FILE,
              self.IIR_WEAK, self.LMR_BADCAP,
              self.NULL_BASE, self.NULL_DIV, self.LMR_DIV,
              self.NULL_NODOUBLE, self.NULL_EVALR, self.QS_EVASION_CAP,
              self.SINGULAR, self.SE_MIN_DEPTH, self.SE_MARGIN, self.SE_BUDGET,
              self.KILLER_INHERIT, self.QUIET_MALUS_ALL, self.HIST_KEEP,
            self.QS_TTFIRST, self.REP_STRICT, self.QS_SEE_MARGIN,
            self.CUTNODE_LMR, self.TTPV_LMR,
            self.RAZOR_MARGIN, self.RAZOR_DEPTH,
            self.PROBCUT_MARGIN, self.PROBCUT_DEPTH, self.PROBCUT_RED,
            self.CORR_HIST, self.CORR_CAP,
            self.SM_CONTEMPT,
              # FB-55: the guard covered 49 TOGGLES and not one EVAL VALUE --
              # a hole exactly where the .so-cross-contamination class bites.
              # csearch.so's eval params are process-wide too, so two engines
              # whose only difference is a retuned scalar (a texel candidate
              # vs the shipped eval -- the commonest same-process pairing in
              # this project) passed the guard and silently shared whichever
              # was constructed FIRST. The pushed vector is hashed, not
              # listed: it is ~1,500 numbers and must not bloat the tuple.
              self._eval_fingerprint())
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
        # FI-115: dead-entry tag for TT replacement. Default OFF -- with it
        # off the store path is byte-identical to before (the piece count is
        # stamped into spare bits either way; only the victim rule changes).
        if hasattr(lib, "set_tt_deadtag"):
            lib.set_tt_deadtag(1 if getattr(self, "TT_DEADTAG", False) else 0)
        # FI-116: growing TT. Default OFF -- on, the table is allocated at
        # TT_BITS but indexed with 20 bits (24 MiB) until it passes 75% full,
        # doubling the active window from there. Must run AFTER set_tt_bits,
        # which sets the allocation the window grows into.
        if hasattr(lib, "set_tt_grow"):
            lib.set_tt_grow(1 if getattr(self, "TT_GROW", False) else 0)
        lib.set_hist_prune(int(self.HIST_PRUNE))               # FI-23
        lib.set_qs_tt_sharpen(1 if self.QS_TT_SHARPEN else 0)  # FI-30(a)
        lib.set_qs_keep_move(1 if self.QS_KEEP_MOVE else 0)    # FI-30(b)
        lib.set_cycle(1 if self.CYCLE_DETECT else 0)           # FI-29
        lib.set_qs_beta_narrow(1 if self.QS_BETA_NARROW else 0)  # FI-50
        lib.set_qs_ttm_exempt(1 if self.QS_TTM_EXEMPT else 0)    # FI-51
        lib.set_qs_chk_d1(1 if self.QS_CHK_D1 else 0)            # FI-52
        lib.set_tt_keep_exact(int(self.TT_KEEP_EXACT))           # FI-48
        lib.set_tt_fh_tight(1 if self.TT_FH_TIGHT else 0)        # FI-49
        lib.set_tt_r50(1 if self.TT_R50 else 0)                  # FI-53
        lib.set_term_store(1 if self.TERM_STORE else 0)          # FI-54
        lib.set_tt_mate_cut(1 if self.TT_MATE_CUT else 0)        # FI-54
        lib.set_root_lmr(1 if self.ROOT_LMR else 0)              # FI-56
        lib.set_iir_weak(1 if self.IIR_WEAK else 0)              # FI-55
        lib.set_lmr_badcap(1 if self.LMR_BADCAP else 0)          # FI-64
        lib.set_lazy_nnue(1 if self.LAZY_NNUE else 0)            # FI-106
        lib.set_lazy_margin(int(self.LAZY_NNUE_MARGIN))          # FI-106
        # FI-15 NNUE (abi 19): load-then-arm. A load failure with USE_NNUE
        # on raises loudly -- a missing/corrupt net must never silently
        # fall back to HCE (the A/B would be mislabeled). set_use_nnue(0)
        # is pushed unconditionally per the FB-06 authority rule.
        lib.nnue_load.argtypes = [ctypes.c_char_p]
        # FI-104: SIMD gate, BEFORE the load so a scalar box never even opens
        # the file. self.USE_NNUE (not the class attr) is what everything
        # downstream reads -- battle_worker.describe_nnue asks the ENGINE, so
        # a disarmed run reports its net as off rather than claiming one it
        # is not playing with. Announced on stderr because a silent downgrade
        # is how an A/B ends up mislabeled.
        if self.USE_NNUE and self.NNUE_REQUIRE_SIMD and hasattr(
                lib, "nnue_kernel_name"):
            lib.nnue_kernel_name.restype = ctypes.c_char_p
            _kern = lib.nnue_kernel_name().decode()
            # The "SCALAR" prefix is the contract (NNUE/nnue.c); verify.py and
            # tools/profile_nnue.py print the same string.
            if _kern.startswith("SCALAR"):
                print(f"!! NNUE disabled: this build has no SIMD dot kernel "
                      f"({_kern}). The node cost would outweigh the eval. "
                      f"Rebuild with ./setup.sh on a NEON/AVX2 host, or set "
                      f"NNUE_REQUIRE_SIMD = False to measure it anyway.",
                      file=sys.stderr)
                self.USE_NNUE = False
        if self.USE_NNUE:
            _np = (self.NNUE_FILE if os.path.isabs(self.NNUE_FILE)
                   else os.path.join(_DIR, self.NNUE_FILE))
            _rc = lib.nnue_load(_np.encode())
            if _rc != 0:
                raise RuntimeError(
                    f"nnue_load({_np!r}) failed (rc={_rc}) -- USE_NNUE "
                    "needs a valid .nnue file (NNUE/README.md: train or "
                    "regenerate the toy net)")
        lib.set_use_nnue(1 if self.USE_NNUE else 0)              # FI-15
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
        lib.set_null_move(int(self.NULL_BASE), int(self.NULL_DIV))  # P-26 sweep
        lib.set_fut_margin(150)
        lib.set_delta_margin(200)
        lib.set_lmp(6, 10, 14)
        lib.set_lmr_div(int(self.LMR_DIV))                          # P-26 sweep
        lib.set_null_nodouble(1 if self.NULL_NODOUBLE else 0)       # FI-24a
        lib.set_null_evalr(1 if self.NULL_EVALR else 0)             # FI-24b
        lib.set_qs_evasion_cap(int(self.QS_EVASION_CAP))            # FI-63
        lib.set_singular(1 if self.SINGULAR else 0)                 # P-33
        lib.set_singular_params(int(self.SE_MIN_DEPTH),
                                int(self.SE_MARGIN))                # P-33
        lib.set_singular_budget(int(self.SE_BUDGET))                # P-33
        lib.set_killer_inherit(1 if self.KILLER_INHERIT else 0)     # FI-59
        lib.set_quiet_malus_all(1 if self.QUIET_MALUS_ALL else 0)   # FI-60
        lib.set_hist_keep(1 if self.HIST_KEEP else 0)               # FI-12
        lib.set_qs_ttfirst(1 if self.QS_TTFIRST else 0)             # FI-67
        lib.set_rep_strict(1 if self.REP_STRICT else 0)             # FI-89
        lib.set_qs_see_margin(int(self.QS_SEE_MARGIN))              # FI-90
        lib.set_cutnode_lmr(1 if self.CUTNODE_LMR else 0)           # FI-103
        lib.set_ttpv_lmr(1 if self.TTPV_LMR else 0)                 # FI-104
        lib.set_razor(int(self.RAZOR_MARGIN), int(self.RAZOR_DEPTH))  # FI-106
        lib.set_probcut(int(self.PROBCUT_MARGIN), int(self.PROBCUT_DEPTH),
                        int(self.PROBCUT_RED))               # FI-107
        lib.set_corr_hist(1 if self.CORR_HIST else 0,
                          int(self.CORR_CAP))                       # FI-109
        lib.set_sm_contempt(1 if self.SM_CONTEMPT else 0)           # FB-48
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
        # FI-86: the EG halves, AFTER csearch_set_eval (which defaults them
        # flat and is what builds the taper the first time).
        lib.csearch_set_pawn_eg(eng.DOUBLED_PAWN_EG, eng.ISOLATED_PAWN_EG,
                                eng.BACKWARD_PAWN_EG)
        # 3. contempt draw scoring.
        lib.csearch_set_draw(eng.CONTEMPT, eng.DRAW_AVOID_MARGIN)
        self.contempt = eng.CONTEMPT     # FB-34: fingerprint honesty (cuci
                                         # echoes this; setoption updates it)
        # 4. simplify-at-500 (threshold 0 = off = v36 eval exactly).
        lib.csearch_set_simplify(
            int(self.SIMPLIFY_THRESHOLD) if self.USE_SIMPLIFY else 0,
            int(eng.SIMPLIFY_WEIGHT))

        # --- host-visible state (battle_worker contract) ------------------ #
        # OFF by default (user call 2026-08-29). A book is an opening
        # PREFERENCE, not strength: a GUI or bot operator who wants one says
        # so, and every A/B harness here already sets it explicitly. Silently
        # playing book moves also makes the first plies of an analysis session
        # not the engine's own judgement, which is rarely what is wanted.
        self.use_book = False
        # Tablebase probe (delegated to the embedded engine, root-only), OFF
        # by default like v30. When on, it is additionally gated to
        # *difficult* positions: at ~2.5M nps the search converts clearly
        # won endings on its own faster than the network round-trip, so the
        # probe only fires when the previous search's verdict was NOT
        # already decisive (see TB_DIFFICULT_CP).
        self.use_tb = False
        self.TB_DIFFICULT_CP = 500           # |last score| >= this: skip probe
                                             # (ONLINE probe only -- a local
                                             # file probe is never gated)
        self.syzygy_path = None              # UCI SyzygyPath; forwarded to
                                             # the embedded engine on set
        self.pv_uci = True
        # Lazy SMP: helper search threads inside csearch.so (shared lockless
        # TT, per-thread everything else). Default 1 -- the SMP Elo gain is
        # not yet A/B-measured, so multi-threading is strictly opt-in (set
        # this attr, or the Threads option in cuci.py). CLAUDECHESS_SMP env
        # honored like engine.py.
        # FB-13c: clamp to the C-side ceiling (set_threads clamps at 256
        # silently -- the Python attr must not misrepresent the real count).
        self.smp_workers = min(512, max(1, int(self.SMP_WORKERS)))   # csearch CS_MAX_THREADS
        # FB-53: `go searchmoves ...` restricts the ROOT to a whitelist. The
        # host applies it as a C-level exclusion list, which the book and TB
        # probes below never see -- they run BEFORE the search and used to
        # return an unlisted move, breaking the promise the GUI was given.
        # None = unrestricted; a set of chess.Move = the permitted set.
        self.search_moves = None
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
        # FB-28: engine.py's _resolve_book latches _book_resolved on the
        # first probe; without this reset a BookFile setoption arriving
        # after any `go` silently keeps serving the OLD book (and <empty>
        # could never restore the bundled scan). Invalidate on assignment
        # so every host gets live switching.
        self._py._book_resolved = False
        self._py._book_reader = None

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
        self._lib.cs_set_root_pc(int(bin(board.occupied).count("1")))  # FI-115
        self._clear_stale_abort()
        self._go_pending = False             # FB-21: window closed
        return self._search(board, None, depth)

    def get_best_move_timed(self, board, time_limit, max_depth=245):
        self._lib.cs_set_root_pc(int(bin(board.occupied).count("1")))  # FI-115
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
            if (book is not None and self.search_moves is not None
                    and book not in self.search_moves):
                book = None          # FB-53: not in the permitted set -- fall
                                     # through to the restricted search
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
        # LOCAL first, and UNGATED. The difficulty gate below exists to avoid
        # paying a network round-trip for a position the search already wins
        # on its own -- a local file probe costs microseconds, so that trade
        # does not apply and skipping it would only lose exact play. <=5 men
        # is what the shipped .rtbw/.rtbz set covers; 6-7 still go to Lichess,
        # which hosts tables we do not carry, and keep the gate.
        tb = None
        # TB_LOCAL_MAX_PIECES is None until the first probe detects it from
        # disk (dddb68a made the pin lazy); comparing against None raised
        # TypeError and turned EVERY UseTB search into bestmove 0000. None
        # means "unknown yet" and must fall through to the probe, which does
        # its own detection and its own piece-count gate (engine.py:2476).
        if self.use_tb:
            _cap = self._py.TB_LOCAL_MAX_PIECES
            if _cap is None or board.occupied.bit_count() <= _cap:
                tb = self._py._tb_probe_local(board)
        if tb is None and self.use_tb and abs(prev_verdict) < self.TB_DIFFICULT_CP:
            self._py.use_tb = True
            tb_to = self._py.tb_timeout
            if time_limit is not None:
                tb_to = min(tb_to, max(0.0, time_limit * 0.5))
            tb = self._py._tb_probe(board, tb_to)

        # De-nested on purpose: this consumes whichever probe produced `tb`.
        # It used to sit INSIDE the online branch, so once the local probe
        # started answering first, the verdict was computed and then thrown
        # away -- the engine played its own search move and reported +448 in a
        # position the tablebase had just called drawn.
        if (tb is not None and self.search_moves is not None
                and tb[1] not in self.search_moves):
            tb = None                # FB-53: same rule as the book above
        if tb is not None:
            wdl, tb_move = tb                # move already verified legal
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
        # FB-29: node-limited searches force 1 thread HERE (the C budget
        # counts main-thread nodes only, and `go nodes` exists for
        # determinism) -- cuci's guard at go-parse time was dead code, this
        # push used to clobber it when the search started.
        self._lib.set_threads(1 if self.node_limit
                              else int(self.smp_workers))  # Lazy SMP
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
                self.last_pv = self._extract_pv(board, dmv,
                                                self.PV_MAX_LEN)
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
        self.last_pv = self._extract_pv(board, move, self.PV_MAX_LEN)
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
        # FI-21/S10, CONFIRMED into v62 2026-08-31. Three riders, measured
        # together as one change (they are one policy, not three toggles):
        # a score-scaled opening window instead of a flat 30cp, a fail-low
        # that pulls beta to the midpoint before dropping alpha, and 1.5x
        # growth instead of 2x. ACCEPT H1 at BOTH time controls -- 10+0.1
        # (LLR +2.969, 6,687 games) and the shipping 50+0.5 (LLR +2.959,
        # 3,439 games). Both stopped at a bound, so the magnitude is biased
        # upward and is NOT quoted here; a fixed-budget 50+0.5 run is owed.
        if self.ASPIRATION_FI21:
            delta = 14 + (prev_score * prev_score) // 16384
        else:
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
                if self.ASPIRATION_FI21:
                    # SF order: beta comes off the OLD bounds first, then
                    # alpha drops. Kills the fail-low/fail-high oscillation
                    # that otherwise pays the full ladder to the 1920 cap.
                    beta = (alpha + beta) // 2
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
            delta += delta // 2 if self.ASPIRATION_FI21 else delta
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
