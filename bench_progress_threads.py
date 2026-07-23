#!/usr/bin/env python3
"""bench_progress_threads.py -- like bench_progress.py, but at a chosen
smp_workers count, for the README's "NPS 4 Threads" column.

    python3 bench_progress_threads.py [threads] [seconds]   # default 4, 5

Same methodology as bench_progress.py (one version per subprocess, best-of-5
by depth), except ``smp_workers`` is set to the given thread count instead of
forced to 1. The actual work happens in bench_progress_threads_worker.py,
run as a real file (not -c/stdin): the multi-process SMP path used by
versions 25-30 relies on Python multiprocessing's 'spawn' start method,
which re-imports the __main__ script by PATH to bootstrap each worker --
a -c/stdin invocation has no such path and every worker fails at startup.

Versions without an ``smp_workers`` attribute (v1-18, pre any SMP) or whose
SMP predates v25's "Lazy-SMP production fixes" (v19-24 -- known fragile, see
that milestone's own description) report an error and get "--" in the
README; there is nothing reliably measurable there.
"""
import concurrent.futures, os, subprocess, sys

import interruptible

if "-h" in sys.argv or "--help" in sys.argv:
    print(__doc__.strip()); sys.exit(0)

REPO = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(REPO, "bench_progress_threads_worker.py")
THREADS = int(sys.argv[1]) if len(sys.argv) > 1 else 4
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
REPS = 5
JOBS = 2          # concurrent version subprocesses (THREADS each)

def versions():
    return sorted(int(d) for d in os.listdir(os.path.join(REPO, "Old Engine"))
                  if d.isdigit())

if __name__ == "__main__":
    # JOBS versions at a time, THREADS each -- keep JOBS*THREADS under the
    # core count so versions do not contend. Every version must see the same
    # load for the column to stay comparable.
    def run(v):
        r = subprocess.run([sys.executable, WORKER, REPO, str(v),
                            str(THREADS), str(SECONDS), str(REPS)],
                           capture_output=True, text=True)
        return (r.stdout.strip().splitlines() or ["{}"])[-1]

    with concurrent.futures.ThreadPoolExecutor(max_workers=JOBS) as ex:
        for line in ex.map(run, versions()):
            print(line, flush=True)
