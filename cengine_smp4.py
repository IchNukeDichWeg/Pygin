"""SMP Elo A/B helper (local campaign artifact -- delete after the verdict).

The SAME v47 engine as cengine.py, but forced to 4 Lazy-SMP threads. Play it
vs cengine.py (1 thread) to measure the pthreads speedup's Elo -- the
mandatory re-measurement after the SMP TT-poison fix (the old d18-vs-d15
number predates it). Both sides are v47 + identical toggles; ONLY smp_workers
differs, so the match isolates the SMP gain.

CORE BUDGET: 4-thread engine needs 4 free cores. On a 224-core box use
~--workers 40 (leaves the 4-thread side real parallelism); higher oversubscribes
and understates SMP. Add --sprt (the effect is large -> early-stop).
"""
import os
os.environ["CLAUDECHESS_SMP"] = "4"    # read by cengine.Engine.__init__
import cengine

Engine = cengine.Engine
