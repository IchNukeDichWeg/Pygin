"""SMP Elo A/B helper (local campaign artifact -- delete after the verdict).

The SAME v47 engine as cengine.py, but forced to 8 Lazy-SMP threads. Play it
vs a 1-thread v47 (Old Engine/47 or cengine.py) to extend the thread-count
curve past the +110.13 measured at 4 threads. Both sides are v47 + identical
toggles; ONLY smp_workers differs, so the match isolates the SMP gain.

CORE BUDGET: an 8-thread engine wants 8 free cores. On a 224-core box use
~--workers 24 (leaves the 8-thread side real parallelism); 223 workers here
would put ~1784 threads on 224 cores = 8x oversubscription and understate SMP.
Add --sprt (the effect is large -> early-stop).
"""
import os
os.environ["CLAUDECHESS_SMP"] = "8"    # read by cengine.Engine.__init__
import cengine

Engine = cengine.Engine
