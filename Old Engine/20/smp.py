"""smp.py -- Lazy SMP orchestration (#13 Phase 3).

Spawns N worker PROCESSES (true parallelism -- no GIL contention) that all
search the same root position to the same wall-clock budget, sharing one
lock-free transposition table (shared_tt.SharedTT). Because the workers explore
slightly differently (different RNG seeds desynchronise the equal-score
tiebreaks and the order in which subtrees are filled), their TT entries help
each other find cutoffs, so the group collectively reaches a greater depth than
a single process would in the same time.

The result is taken from the worker that completed the deepest iteration (ties
broken by score). python-chess is kept; only the search is parallelised.

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
from multiprocessing import Process, Queue

import chess

import engine
from shared_tt import SharedTT, DEFAULT_SLOTS


def _worker(wid, fen, time_limit, max_depth, tt_name, n_slots, seed, q):
    random.seed(seed)                        # diversify the equal-score tiebreaks
    tt = SharedTT(n_slots=n_slots, name=tt_name)
    try:
        e = engine.Engine()
        e.use_tb = False
        e.smp_workers = 1                    # never re-dispatch SMP from a worker
        e.use_shared_tt = True
        e._shared_tt = tt
        board = chess.Board(fen)
        move = e.get_best_move_timed(board, time_limit, max_depth)
        q.put((wid, e.last_depth, e.last_score, e.nodes,
               move.uci() if move is not None else None))
    finally:
        tt.arr = None                        # release the buffer view; main unlinks


def search_smp(board, time_limit, n_workers=4, max_depth=64,
               n_slots=DEFAULT_SLOTS, base_seed=1000):
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
                              base_seed + i, q))
            p.start()
            procs.append(p)
    finally:
        os.environ.pop("CLAUDECHESS_SMP_CHILD", None)   # parent itself stays able to spawn
    results = [q.get() for _ in procs]
    for p in procs:
        p.join()
    tt.close()
    tt.unlink()

    valid = [r for r in results if r[4] is not None]
    if not valid:
        return None, results
    # deepest-completed iteration wins; ties -> higher score
    best = max(valid, key=lambda r: (r[1], r[2]))
    return chess.Move.from_uci(best[4]), sorted(results)


# ====================================================================== #
# Persistent worker pool (#13 -- for INTERACTIVE use, e.g. a GUI).
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
def _pool_worker(wid, in_q, out_q, tt_name, n_slots, seed):
    random.seed(seed)
    tt = SharedTT(n_slots=n_slots, name=tt_name)
    e = engine.Engine()
    e.use_tb = False
    e.smp_workers = 1                        # never re-dispatch SMP from a worker
    e.use_shared_tt = True
    e._shared_tt = tt
    try:
        while True:
            job = in_q.get()
            if job is None:                  # shutdown sentinel
                break
            fen, time_limit, max_depth = job
            move = e.get_best_move_timed(chess.Board(fen), time_limit, max_depth)
            out_q.put((wid, e.last_depth, e.last_score, e.nodes,
                       move.uci() if move is not None else None))
    finally:
        tt.arr = None


class SMPPool:
    """A persistent set of search worker processes sharing one lock-free TT.

        pool = SMPPool(4)            # spawns 4 workers ONCE (call on main thread)
        engine_obj._smp_pool = pool  # get_best_move_timed now routes through it
        ...
        pool.close()                 # on shutdown
    """

    def __init__(self, n_workers=4, n_slots=DEFAULT_SLOTS, base_seed=1000):
        self.n_workers = n_workers
        self.tt = SharedTT(n_slots=n_slots, create=True)
        self.tt.clear()
        self._in_qs = [Queue() for _ in range(n_workers)]     # broadcast per worker
        self._out_q = Queue()
        self.procs = []
        # Children inherit this -> a worker re-importing a host that triggers SMP
        # can never spawn again (the fork-bomb guard, same as search_smp).
        os.environ["CLAUDECHESS_SMP_CHILD"] = "1"
        try:
            for i in range(n_workers):
                p = Process(target=_pool_worker,
                            args=(i, self._in_qs[i], self._out_q, self.tt.name,
                                  n_slots, base_seed + i),
                            daemon=True)
                p.start()
                self.procs.append(p)
        finally:
            os.environ.pop("CLAUDECHESS_SMP_CHILD", None)

    def search(self, board, time_limit, max_depth=64):
        """Broadcast the position to all workers; return (best_move, info) with
        the deepest-completed result (does NOT spawn -- workers already run)."""
        fen = board.fen()
        for q in self._in_qs:
            q.put((fen, time_limit, max_depth))
        results = [self._out_q.get() for _ in self.procs]
        valid = [r for r in results if r[4] is not None]
        if not valid:
            return None, results
        best = max(valid, key=lambda r: (r[1], r[2]))
        return chess.Move.from_uci(best[4]), results

    def close(self):
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
