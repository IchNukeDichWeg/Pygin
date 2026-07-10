"""
cengine.py -- Python root driver for the C search core (csearch.so).
====================================================================

A drop-in ``Engine`` for the project's battle/match harness, with the
ENTIRE per-node search loop in C (csearch.c): board, move ordering,
transposition table, pruning, quiescence and the full static eval
(bit-exact port of engine.py's ``_evaluate_static``, verified over 3M
positions). Born as phase-3 step 6 of the C-core plan; the shipped engine
since Old Engine/31. Defaults reproduce v37 (Old Engine/37) plus whatever
eval-toggle candidate is armed on the class attrs below (currently
USE_OUTPOST, A/B PENDING).

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
  * PV-02 exact PV (set_pv_exact; CONFIRMED into v37 2026-07-10,
    snapshotted Old Engine/37; set_pv_exact(0) = v36's search): skip TT
    cutoffs/narrowing at PV nodes so the collected PV is complete
    end-to-end -- the same matetrack FEN goes 1-move -> full 13-ply mate
    PV, Bad-PVs -> zero. Tree-changing (d12 ~-23% nodes) yet the A/B was a
    clean null (+0.17 +/-6.8 @10k 50+0.20, pair ratio 1.02): for a
    correctness feature, a null means FREE.

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
  * EP-01 FIDE-exact ep hashing (set_ep_filter; correctness-positive --
    see the deviations below -- but it changes every tree, so it waits for
    its own A/B slot at a campaign boundary; OFF = raw-ep hashing).

Deliberate deviations from v30 (documented, revisit if an A/B says so):
  * no root random tiebreak (deterministic best move),
  * no singular extensions / razoring (dormant or absent in v30 at match
    depths anyway),
  * repetition detection covers negamax nodes, not quiescence nodes,
  * the position hash mixes the RAW ep square (set after every double push),
    so a phantom ep splits one FIDE-identical position across two keys and
    repetition detection can MISS repetitions the arbiter would count --
    EP-01 above (hash ep only when a legal ep capture exists, =
    python-chess's _transposition_key) is the fix-in-waiting,
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

    # Outpost re-test (user request 2026-07-10; Python-era solo verdict was
    # +0 +/-10 at depth 8, P-20a's subsumption logic tempers expectations).
    # Same sync mechanism as USE_KING_SHELTER: flips the embedded engine's
    # use_outpost BEFORE _sync_c_params pushes set_outpost_params into
    # csearch's eval_c copy. False = v37 eval exactly. LIVE CANDIDATE =
    # True, A/B vs Old Engine/37 PENDING (fourth 50+0.20-era campaign) --
    # selftest pins the ladder to False meanwhile.
    USE_OUTPOST = True

    # Simplify-at-500 re-test (user request; v30's use_simplify A/B'd -14 at
    # threshold 200 -- traded into DRAWN endings; a decisive >=500cp gate
    # removes that failure mode). Pushed via csearch_set_simplify; threshold
    # 0 (off) = v36 eval exactly. CAVEAT: adjudicated matches barely see it
    # (WDL calls wins near this same band) -- its verdict harness is
    # MATCH_ADJUDICATE=0 matches and/or odds-vs-Stockfish conversion play.
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

        lib = ctypes.CDLL(os.path.join(_DIR, "csearch.so"))
        # BUG-04: must match the NEWEST abi whose exports this file calls
        # (set_node_limit / cs_seldepth / cs_hashfull are abi 7) --
        # bump together with csearch_abi.
        if lib.csearch_abi() < 7:
            raise RuntimeError("csearch.so too old -- rebuild via ./setup.sh")
        B = ctypes.c_uint64
        BOARD_ARGS = [B] * 8 + [ctypes.c_int] * 2 + [B]
        lib.cs_search_begin.argtypes = [ctypes.POINTER(B), ctypes.c_int,
                                        ctypes.c_double]
        lib.cs_search_root.argtypes = BOARD_ARGS + [ctypes.c_int] * 3 + \
            [ctypes.c_uint32, ctypes.c_int, ctypes.POINTER(B),
             ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
             ctypes.POINTER(ctypes.c_int)]
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
                            ("set_ep_filter", 0), ("set_cont_hist", 0)):
            getattr(lib, setter)(val)
        # FB-04: one process = one config. A second construction with a
        # DIFFERENT config would silently retarget the process-wide globals
        # under the first instance (the gui.py EvE bug class). Same config
        # is fine (match workers construct one engine per process).
        global _SYNCED_FINGERPRINT
        fp = (self.USE_KING_SHELTER, self.USE_OUTPOST, self.USE_SIMPLIFY,
              self.SIMPLIFY_THRESHOLD, self.CHECK_EXT_BUDGET, self.PV_EXACT)
        if _SYNCED_FINGERPRINT is not None and _SYNCED_FINGERPRINT != fp:
            raise RuntimeError(
                "cengine: two different Engine configs in one process -- "
                "csearch.so's eval params/toggles are process-wide; run the "
                "second config in its own process")
        _SYNCED_FINGERPRINT = fp
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
        self.smp_workers = min(64, max(1, int(os.environ.get(
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
        # P-35/U-06 knobs, same semantics as engine.py
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
        a stop aimed at a live search is untouched (the lock is held)."""
        if self._abort and not Engine._SEARCH_LOCK.locked():
            self._abort = False

    def get_best_move(self, board, depth):
        self._clear_stale_abort()
        return self._search(board, None, depth)

    def get_best_move_timed(self, board, time_limit, max_depth=245):
        # Default = MAX_DEPTH_CAP so the clock, not the cap, is the limit --
        # the old default of 10 silently capped ad-hoc timed searches (the C
        # core passes depth 10 in well under a second).
        self._clear_stale_abort()            # FB-10
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

        # Opening book (delegated; instant when it hits, like v30).
        if self.use_book:
            self._py.use_book = True
            book = self._py._book_move(board)
            if book is not None:
                record = {"depth": 0, "move": book.uci(), "score": 0,
                          "nodes": 0, "time_ms": 0, "book": True}
                self._emit(record)
                self._emit(dict(record, final=True), final=True)
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
                record = {"depth": 0, "move": tb_move.uci(),
                          "score": score_white, "nodes": 0, "time_ms": 0,
                          "tb": True, "wdl": wdl}
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
                time_limit = max(0.05, time_limit - setup)
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

        for depth in range(1, min(max_depth, self.MAX_DEPTH_CAP) + 1):
            if self._abort:
                break        # host stop() landed before/between C calls; the
                             # C-side g_abort covers stops DURING a cs_search_
                             # root call -- this covers the gaps around them
            key, score, nodes, done, aborted = self._root_aspiration(
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
        alpha = prev_score - delta
        beta = prev_score + delta
        while True:
            res = self._root(bargs, depth, alpha, beta, prev_key, hmc)
            if res[4]:                       # aborted: caller handles
                return res
            score = res[1]
            if score <= alpha:               # fail low: widen downward
                alpha = max(-CS_INF, score - delta)
            elif score >= beta:              # fail high: widen upward
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
        key = self._lib.cs_search_root(
            *bargs, depth, alpha, beta, prev_key, hmc,
            ctypes.byref(nodes), ctypes.byref(score),
            ctypes.byref(done), ctypes.byref(aborted))
        return key, score.value, nodes.value, done.value, aborted.value

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
