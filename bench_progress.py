#!/usr/bin/env python3
"""bench_progress.py -- regenerate the README "Version progression" table.

    python3 bench_progress.py [seconds]     # default 5

Runs a single timed search from the STARTING POSITION for every
``Old Engine/N/engineN.py`` snapshot plus the live repo-root ``cengine.py``
(the next version in the lineage), one version per subprocess so the C
``.so`` libraries never share an address space (the cross-contamination
rule -- see memory/so-cross-contamination). Reports nodes/s and the depth
reached, best-of-REPS by depth.

Absolute NPS is hardware-dependent (this is an Apple-Silicon reading); the
TREND across versions is the signal, not the raw number. Depth reached in a
fixed budget mixes speed AND selectivity -- a version that prunes/extends
differently can reach a different nominal depth at the same NPS, so the
Elo column (real A/B results, not this bench) is the strength axis.
"""
import concurrent.futures, importlib.util, json, os, subprocess, sys, time

REPO = os.path.dirname(os.path.abspath(__file__))
SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
REPS = 5
JOBS = 8          # concurrent version subprocesses (1 thread each)

CHILD = r'''
import importlib.util, json, os, sys, time
os.chdir(%r)
import chess
V, SECONDS, REPS = int(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3])
snap = os.path.join("Old Engine", str(V), "engine%%d.py" %% V)
path = snap if os.path.isfile(snap) else "cengine.py"
try:
    spec = importlib.util.spec_from_file_location("_bp_%%d" %% V, path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    bd, bn, bnodes = 0, 0.0, 0
    for _ in range(REPS):
        e = mod.Engine()
        for a in ("use_book", "use_tb"):
            if hasattr(e, a): setattr(e, a, False)
        if hasattr(e, "smp_workers"): e.smp_workers = 1
        t0 = time.perf_counter()
        e.get_best_move_timed(chess.Board(), SECONDS, 60)
        dt = time.perf_counter() - t0
        nodes = getattr(e, "nodes_searched", 0) or 0
        depth = getattr(e, "last_depth", 0) or 0
        nps = 0.0 if dt <= 0 else nodes / dt
        if depth > bd or (depth == bd and nps > bn): bd, bn, bnodes = depth, nps, nodes
    print(json.dumps({"v": V, "nps": round(bn, 2), "depth": bd}))
except Exception as ex:
    print(json.dumps({"v": V, "error": repr(ex)[:160]}))
''' % REPO

def versions():
    vs = sorted(int(d) for d in os.listdir(os.path.join(REPO, "Old Engine"))
                if d.isdigit())
    return vs + [vs[-1] + 1]          # + the live cengine

if __name__ == "__main__":
    # JOBS versions at a time. Each child is single-threaded, so JOBS*1 must
    # stay under the core count or the versions contend and the NPS reading
    # sags -- uniformly, but there is no reason to pay it. The measurement is
    # comparative across versions, so every version must see the SAME load.
    def run(v):
        r = subprocess.run([sys.executable, "-c", CHILD, str(v),
                            str(SECONDS), str(REPS)],
                           capture_output=True, text=True)
        return (r.stdout.strip().splitlines() or ["{}"])[-1]

    with concurrent.futures.ThreadPoolExecutor(max_workers=JOBS) as ex:
        for line in ex.map(run, versions()):
            print(line, flush=True)
