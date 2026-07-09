"""
cengine.py -- Python root driver for the C search core (phase-3 step 6).
=========================================================================

A drop-in ``Engine`` for the project's battle/match harness, with the ENTIRE
per-node search loop in C (csearch.so): board, move ordering, transposition
table, pruning, quiescence and the full static eval (bit-exact port of
engine.py's ``_evaluate_static``, verified over 3M positions).

Python keeps only what needs game/host state -- exactly the phase-3 plan:
  * the iterative-deepening loop with v30's aspiration windows,
  * v30's P-35/U-06 soft-stop time management (stability-scaled),
  * v30's partial-iteration rule (an aborted depth's result is used iff at
    least the first root move finished),
  * the opening-book probe (delegated to an embedded engine.Engine, which is
    also the single source of truth for every eval table/parameter synced
    into the C core at construction),
  * TT retention policy (the C TT persists; cleared after irreversible root
    moves, v30's rule) and the game-history keys for repetition detection.

API (battle_worker.py contract):
    Engine().get_best_move(board, depth)                     -> Move | None
    Engine().get_best_move_timed(board, seconds, max_depth)  -> Move | None
    attributes: nodes_searched / last_score (White POV) / last_depth /
    last_pv, constants MATE_SCORE / MATE_THRESHOLD, settable use_book /
    pv_uci.

Deliberate v1 deviations from v30 (documented, revisit if the A/B says so):
  * no root random tiebreak (deterministic best move),
  * check extensions ARE on (P-01, csearch.c set_check_ext, A/B'd +6.81
    +/-6.8 vs v33 -> snapshotted Old Engine/34); single-reply / forced-move
    extension exists but is DORMANT (P-43, csearch.c set_single_reply,
    default OFF: A/B'd +3.5 +/-4.8 over 20k pooled games vs v34 --
    positive-leaning on every signal but sub-significant, kept-marginal by
    user call; default reproduces v34 node-exactly); the "improving"
    heuristic is ON (P-04, csearch.c set_improving, default on, A/B vs v34
    pending -- v30's exact recipe: eval stack vs ply-2, feeds RFP depth /
    frontier-futility margin / LMR+1; set_improving(0) restores v34
    node-exactly); no singular
    extensions / razoring (dormant or absent in v30 at match depths anyway),
  * repetition detection covers negamax nodes, not quiescence nodes,
  * no SMP, no tablebase probe (v30 default use_tb=False matches).
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

    # P-14 (CONFIRMED v33, +23.52 +/-6.8 vs v32): KEEP the C TT across
    # irreversible root moves. v30's wipe-on-capture/pawn-move rule existed
    # because its dict TT grew unbounded and dead entries wasted memory; the
    # C table is fixed-size with generation-aware replacement and
    # full-key-checked probes, and repetition/50-move draws are decided
    # BEFORE the TT probe -- so the wipe only discarded still-reachable
    # entries (the whole subtree behind the irreversible move) on a very
    # frequent event. False = v32's exact behavior.
    TT_KEEP_WARM = True

    # v30 time-management / aspiration constants (ports, same values)
    ASPIRATION_MIN_DEPTH = 4
    ASPIRATION_DELTA = 30                    # centipawns; C scores are cp too
    SOFT_STOP_STABLE_FRAC = 0.40
    SOFT_STOP_UNSTABLE_FRAC = 0.80
    SOFT_STOP_STABLE_ITERS = 2
    MAX_DEPTH_CAP = 60                       # C side: CS_MAXPLY 64; P-01 check
                                             # exts (+<=5 ply) may graze the
                                             # ply guard there, safe (eval cut)

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

        lib = ctypes.CDLL(os.path.join(_DIR, "csearch.so"))
        # BUG-04: must match the NEWEST abi whose exports this file calls
        # (set_threads / cs_stop are abi 5) -- bump together with csearch_abi.
        if lib.csearch_abi() < 5:
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
        self._lib = lib

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
        self.smp_workers = max(1, int(os.environ.get("CLAUDECHESS_SMP", "1")))
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
    def get_best_move(self, board, depth):
        return self._search(board, None, depth)

    def get_best_move_timed(self, board, time_limit, max_depth=10):
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
        """Walk best moves out of the C TT (legality-checked, stops on
        repetition) -- v30's _extract_pv against the C table."""
        if first_move is None:
            return ""
        b = board.copy(stack=False)
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
            k = b._transposition_key()
            if k in seen:
                break
            seen.add(k)
            mv = self._key_to_move(self._lib.cs_tt_probe_move(*self._bargs(b)))
        return " ".join(out)
