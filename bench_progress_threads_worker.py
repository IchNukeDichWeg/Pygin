"""Internal one-shot worker for bench_progress_threads.py -- must be a real
file on disk (not -c/stdin) because the old engine19-30 multi-process SMP
path uses Python multiprocessing's 'spawn' start method, which re-imports
the __main__ script by PATH to bootstrap each worker; -c/heredoc invocations
have no such path and every worker fails at startup, silently degrading the
pool to a broken state instead of a clean single-thread fallback.
"""
import importlib.util, json, os, sys, time

if __name__ == "__main__":
    REPO, V, THREADS, SECONDS, REPS = (
        sys.argv[1], int(sys.argv[2]), int(sys.argv[3]),
        float(sys.argv[4]), int(sys.argv[5]))
    os.chdir(REPO)
    import chess
    snap = os.path.join("Old Engine", str(V), f"engine{V}.py")
    path = snap if os.path.isfile(snap) else "cengine.py"
    try:
        # Snapshot dirs are self-contained: a version with its OWN smp.py
        # (e.g. 19's older Process/Queue implementation, pre shared_memory)
        # must resolve `from smp import SMPPool` to that sibling file, not
        # the current root smp.py -- without this, versions lacking a local
        # smp.py silently import the wrong (incompatible) one via cwd/PATH
        # and every worker fails at startup, degrading to a phantom 0/0.
        sys.path.insert(0, os.path.dirname(os.path.abspath(path)))
        spec = importlib.util.spec_from_file_location(f"_bpt_{V}", path)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        e = mod.Engine()
        for a in ("use_book", "use_tb"):
            if hasattr(e, a): setattr(e, a, False)
        if not hasattr(e, "smp_workers"):
            raise AttributeError("no smp_workers on this version")
        e.smp_workers = THREADS
        bd, bn = 0, 0.0
        for _ in range(REPS):
            t0 = time.perf_counter()
            e.get_best_move_timed(chess.Board(), SECONDS, 60)
            dt = time.perf_counter() - t0
            nodes = getattr(e, "nodes_searched", 0) or getattr(e, "nodes", 0) or 0
            depth = getattr(e, "last_depth", 0) or 0
            nps = 0.0 if dt <= 0 else nodes / dt
            if depth > bd or (depth == bd and nps > bn): bd, bn = depth, nps
        print(json.dumps({"v": V, "nps": round(bn, 2), "depth": bd}))
    except Exception as ex:
        print(json.dumps({"v": V, "error": repr(ex)[:160]}))
