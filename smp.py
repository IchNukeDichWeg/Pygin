"""smp.py -- Lazy SMP orchestration (#13 Phase 3).

Spawns N worker PROCESSES (true parallelism -- no GIL contention) that all
search the same root position to the same wall-clock budget, sharing one
lock-free transposition table (shared_tt.SharedTT). Because the workers explore
slightly differently (different RNG seeds desynchronise the equal-score
tiebreaks and the order in which subtrees are filled), their TT entries help
each other find cutoffs, so the group collectively reaches a greater depth than
a single process would in the same time.

The result is taken from the worker that completed the deepest iteration (ties
broken by the score best for the SIDE TO MOVE -- see pick_best).
python-chess is kept; only the search is parallelised.

    from smp import search_smp
    move, info = search_smp(board, time_limit=2.0, n_workers=4)

Most code doesn't call this directly: set ``engine.smp_workers = N`` (>=2) and
``Engine.get_best_move_timed`` dispatches here automatically (N = 1, the
default, runs the normal single-threaded search). Keep
``smp_workers * parallel_games <= CPU cores`` to avoid oversubscription.

NOTE: this must be a real importable module -- macOS multiprocessing uses
'spawn', which re-imports the module in each child, so worker functions must be
module-level and all args picklable (we pass the FEN string, not the Board).
"""
import os
import random
import threading
import time
from multiprocessing import Process, Queue, shared_memory
from queue import Empty

import chess

import engine
from shared_tt import SharedTT, DEFAULT_SLOTS

# Wall-clock slack past the search budget before the host stops waiting for
# worker results (P-03: a crashed/wedged worker must never hang the game).
COLLECT_SLACK_S = 10.0


def pick_best(rows, stm_white):
    """Deepest-completed iteration wins; ties broken by the score best for the
    SIDE TO MOVE. Worker scores (row[2]) are White-POV, so they are negated
    when Black is the mover -- the old raw-score tiebreak systematically
    picked the move WORST for Black (P-02).

    U-10: on an EXACT (depth, stm-score) tie between workers proposing
    different moves, pick randomly among the tied rows -- `max()` alone
    returns the first by wall-clock arrival, mirroring the root's
    `random.choice(near)` tiebreak instead of a nondeterministic race."""
    key = lambda r: (r[1], r[2] if stm_white else -r[2])
    best = max(key(r) for r in rows)
    tied = [r for r in rows if key(r) == best]
    return random.choice(tied) if len(tied) > 1 else tied[0]


def _apply_cfg(e, cfg, applied=None):
    """P-11: replicate the host engine's configuration in a worker. Without
    this every worker searched with class defaults -- book ON, tuner/setoption
    eval overrides ignored -- silently diverging from the advertised config.

    ``applied`` is the previous job's config (persistent pool workers): keys
    that vanished since then are first restored to the worker's own
    construction defaults, so a host REVERTING an option really reverts it.
    Returns the new applied dict."""
    cfg = cfg or {}
    applied = applied or {}
    if cfg != applied:
        for k in applied:
            if k not in cfg:
                setattr(e, k, e._smp_defaults[k])
        for k, v in cfg.items():
            setattr(e, k, v)
        e._sync_c_params()          # attribute writes alone never reach C eval
        e._pawn_cache.clear()       # memoized under the old weights
    # Invariants AFTER the config so no dict can break them.
    e.use_tb = False                # workers must never fire root HTTP probes
    e.smp_workers = 1               # never re-dispatch SMP from a worker
    e.use_shared_tt = True
    return dict(cfg)


def _worker(wid, fen, time_limit, max_depth, tt_name, n_slots, seed, q, cfg):
    random.seed(seed)                        # diversify the equal-score tiebreaks
    tt = SharedTT(n_slots=n_slots, name=tt_name)
    try:
        e = engine.Engine()
        e._shared_tt = tt
        _apply_cfg(e, cfg)
        board = chess.Board(fen)
        move = e.get_best_move_timed(board, time_limit, max_depth)
        q.put((wid, e.last_depth, e.last_score, e.nodes,
               move.uci() if move is not None else None))
    except Exception:
        # P-03: an error row instead of silence -- a missing q.put would leave
        # the parent blocked forever.
        q.put((wid, -1, 0, 0, None))
    finally:
        tt.close()                  # P-13: release the view AND the mapping
                                    # (main still owns unlink)


def search_smp(board, time_limit, n_workers=4, max_depth=64,
               n_slots=DEFAULT_SLOTS, base_seed=1000, config=None):
    """Parallel root search. ``n_workers`` is the on/off knob:

        n_workers == 1  -> SMP OFF: a single normal in-process search (no spawn,
                           no shared TT, zero parallel overhead).
        n_workers >= 2  -> SMP ON:  that many worker processes share one
                           lock-free TT and the deepest result is returned.

    Returns (best_move, info); info is a list of per-worker
    (wid, depth, score, nodes, uci) rows."""
    # FORK-BOMB GUARD. Run a single in-process search (NO spawn) when SMP is off
    # (n_workers<=1) OR when we are anywhere inside an SMP run. macOS 'spawn'
    # re-imports __main__ in each child, so an unguarded host script that calls
    # this at module level would otherwise recurse exponentially and hang the
    # machine. CLAUDECHESS_SMP_CHILD is set below around the spawn and inherited
    # by every descendant from birth -- it is reliably present even during the
    # child's __main__ re-import. It catches "descendant of an SMP spawn"
    # precisely, WITHOUT blocking a legitimate match game-worker subprocess that
    # wants its own SMP (so SMP works under match.py).
    if n_workers <= 1 or os.environ.get("CLAUDECHESS_SMP_CHILD"):
        e = engine.Engine()
        e.use_tb = False
        e.smp_workers = 1                    # never re-dispatch
        move = e.get_best_move_timed(board, time_limit, max_depth)
        return move, [(0, e.last_depth, e.last_score, e.nodes,
                       move.uci() if move is not None else None)]

    tt = SharedTT(n_slots=n_slots, create=True)
    tt.clear()
    fen = board.fen()
    q = Queue()
    procs = []
    os.environ["CLAUDECHESS_SMP_CHILD"] = "1"   # children inherit -> can't re-spawn
    try:
        for i in range(n_workers):
            p = Process(target=_worker,
                        args=(i, fen, time_limit, max_depth, tt.name, n_slots,
                              base_seed + i, q, config))
            p.start()
            procs.append(p)
    finally:
        os.environ.pop("CLAUDECHESS_SMP_CHILD", None)   # parent itself stays able to spawn

    # P-03: bounded collection. A worker killed by the OS (segfault/OOM) puts
    # nothing on the queue; the old blocking `q.get()` per worker then hung the
    # search forever. Wait at most budget + slack, and stop early once every
    # still-alive worker has answered.
    deadline = time.monotonic() + (time_limit or 0.0) + COLLECT_SLACK_S
    results = []
    while len(results) < n_workers:
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            break
        try:
            results.append(q.get(timeout=min(1.0, timeout)))
        except Empty:
            # Y-01: one-shot workers EXIT after answering, so "alive" here
            # means "hasn't answered yet" -- stop waiting only when no
            # UNANSWERED worker is still alive. (SMPPool.search's similar-
            # looking check is intentionally different: pool workers stay
            # alive after answering, so alive <= answered means exactly
            # "every live worker has answered" there.)
            answered = {r[0] for r in results}
            if not any(p.is_alive() for i, p in enumerate(procs)
                       if i not in answered):
                break
    for p in procs:
        p.join(timeout=2)
        if p.is_alive():
            p.terminate()
    tt.close()
    tt.unlink()

    valid = [r for r in results if r[4] is not None]
    if not valid:
        return None, results
    best = pick_best(valid, board.turn == chess.WHITE)
    return chess.Move.from_uci(best[4]), sorted(results)


# ====================================================================== #
# Persistent worker pool (#13 -- for INTERACTIVE use, e.g. a GUI or uci.py).
#
# search_smp() above spawns a fresh pool every call: fine for headless batch,
# but ruinous interactively (it re-imports the host module per worker, per
# move). SMPPool spawns its workers ONCE; thereafter each search just hands the
# (already-running) workers a position over a queue -- no per-move spawning, no
# re-import. Create it on the MAIN thread at startup, attach it to an engine via
# ``engine._smp_pool``, and ``close()`` it on exit.
#
# Safe with the GUIs because their __main__ (main.py / experiment.py) is guarded,
# so a worker re-importing it does not relaunch the GUI; and CLAUDECHESS_SMP_CHILD
# is set around the (one-time) spawn so a worker can never itself spawn.
# ====================================================================== #
def _pool_worker(wid, in_q, out_q, tt_name, n_slots, seed, stop_name):
    random.seed(seed)
    tt = SharedTT(n_slots=n_slots, name=tt_name)
    stop_shm = shared_memory.SharedMemory(name=stop_name)
    e = engine.Engine()
    e.use_tb = False
    e.smp_workers = 1                        # never re-dispatch SMP from a worker
    e.use_shared_tt = True
    e._shared_tt = tt

    # P-01 stop support: the host's `stop` can't reach another process's
    # engine attributes, so a 1-byte shared-memory flag is POLLED by a tiny
    # thread that flips this worker's _abort (the same host-owned abort the
    # UCI stop fix uses in-process). Deliberately NOT an mp.Event: a worker
    # terminated mid-wait() leaves the Event's internal semaphore held, and
    # the parent's next set() then deadlocks -- a plain shared byte has no
    # locks to corrupt. The job loop re-arms _abort per job; the host zeroes
    # the flag before each broadcast.
    def _watch():
        while True:
            time.sleep(0.005)
            if stop_shm.buf[0]:
                e._abort = True
    threading.Thread(target=_watch, daemon=True).start()

    applied = {}
    try:
        while True:
            job = in_q.get()
            if job is None:                  # shutdown sentinel
                break
            seq, fen, time_limit, max_depth, cfg = job
            e._abort = False                 # host cleared the stop flag before broadcast
            try:
                applied = _apply_cfg(e, cfg, applied)
                move = e.get_best_move_timed(chess.Board(fen), time_limit, max_depth)
                out_q.put((seq, wid, e.last_depth, e.last_score, e.nodes,
                           move.uci() if move is not None else None))
            except Exception:
                # P-03: a failed search must neither kill the worker loop nor
                # leave the host waiting for a row that never comes.
                out_q.put((seq, wid, -1, 0, 0, None))
    finally:
        tt.close()                           # P-13: view + mapping (main unlinks)
        try:
            stop_shm.close()
        except Exception:
            pass


class SMPPool:
    """A persistent set of search worker processes sharing one lock-free TT.

        pool = SMPPool(4)            # spawns 4 workers ONCE (call on main thread)
        engine_obj._smp_pool = pool  # get_best_move_timed now routes through it
        ...
        pool.close()                 # on shutdown
    """

    def __init__(self, n_workers=4, n_slots=DEFAULT_SLOTS, base_seed=1000):
        # Same fork-bomb guard as search_smp: a worker re-importing an
        # unguarded host module must never build a nested pool.
        if os.environ.get("CLAUDECHESS_SMP_CHILD"):
            raise RuntimeError("SMPPool created inside an SMP worker -- the "
                               "host module is missing an "
                               "`if __name__ == '__main__':` guard")
        self.n_workers = n_workers
        self.tt = SharedTT(n_slots=n_slots, create=True)
        self.tt.clear()
        self._in_qs = [Queue() for _ in range(n_workers)]     # broadcast per worker
        self._out_q = Queue()
        # P-01: `stop` must reach worker processes. A 1-byte lock-free shared
        # flag, deliberately not an mp.Event -- see the note in _pool_worker.
        self._stop_shm = shared_memory.SharedMemory(create=True, size=1)
        self._stop_shm.buf[0] = 0
        self._seq = 0                # P-04: search id, echoed in every result
        self._last_cfg = None        # P-11: detect config changes (see search)
        self.procs = []
        # Children inherit this -> a worker re-importing a host that triggers SMP
        # can never spawn again (the fork-bomb guard, same as search_smp).
        os.environ["CLAUDECHESS_SMP_CHILD"] = "1"
        try:
            for i in range(n_workers):
                p = Process(target=_pool_worker,
                            args=(i, self._in_qs[i], self._out_q, self.tt.name,
                                  n_slots, base_seed + i, self._stop_shm.name),
                            daemon=True)
                p.start()
                self.procs.append(p)
        finally:
            os.environ.pop("CLAUDECHESS_SMP_CHILD", None)

    def search(self, board, time_limit, max_depth=64, config=None):
        """Broadcast the position to all workers; return (best_move, info) with
        the deepest-completed result (does NOT spawn -- workers already run).

        Robustness contract: results are tagged with a per-search sequence id
        and stale rows from an abandoned previous search are discarded (P-04);
        collection is bounded by budget + slack and stops waiting for workers
        that died (P-03). ``config`` replicates host overrides in the workers
        (P-11)."""
        self._seq += 1
        seq = self._seq
        self._stop_shm.buf[0] = 0             # re-arm stop for this search
        if config != self._last_cfg:
            # P-11 coherence: entries scored under the OLD eval params would
            # poison the new config's search -- drop them (host-side, before
            # any worker starts; zeroing is idempotent).
            self.tt.clear()
            self._last_cfg = config
        fen = board.fen()
        for q in self._in_qs:
            q.put((seq, fen, time_limit, max_depth, config))
        deadline = time.monotonic() + (time_limit or 0.0) + COLLECT_SLACK_S
        results = []
        while len(results) < self.n_workers:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                break                          # never hang past budget + slack
            try:
                row = self._out_q.get(timeout=min(1.0, timeout))
            except Empty:
                if sum(1 for p in self.procs if p.is_alive()) <= len(results):
                    break                      # the missing answers are dead
                continue
            if row[0] != seq:
                continue                       # stale result -- discard (P-04)
            results.append(row[1:])            # (wid, depth, score, nodes, uci)

        valid = [r for r in results if r[4] is not None]
        if not valid:
            return None, results
        best = pick_best(valid, board.turn == chess.WHITE)
        return chess.Move.from_uci(best[4]), results

    def request_stop(self):
        """Ask every worker to abort its current search (host `stop`). The
        pending ``search()`` call then returns quickly with the moves found so
        far. Re-armed automatically at the next ``search()``."""
        self._stop_shm.buf[0] = 1

    def clear_tt(self):
        """Zero the shared TT (host `ucinewgame` -- mirrors the dict-TT reset)."""
        self.tt.clear()

    def close(self):
        try:
            self._stop_shm.buf[0] = 1        # abort any in-flight searches
        except Exception:
            pass
        for q in self._in_qs:
            try:
                q.put(None)                  # ask each worker to exit its loop
            except Exception:
                pass
        for p in self.procs:
            p.join(timeout=2)
            if p.is_alive():
                p.terminate()
        try:
            self.tt.close()
            self.tt.unlink()
        except Exception:
            pass
        try:
            self._stop_shm.close()
            self._stop_shm.unlink()
        except Exception:
            pass
