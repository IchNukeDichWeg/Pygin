#!/usr/bin/env python3
"""uci/cuci_old.py -- cuci.py's UCI layer driving any FROZEN Old Engine snapshot.

    python3 uci/cuci_old.py 57        # speak UCI as v57

The generic form of uci/cuci57.py. Point it at a version number and it loads
"Old Engine/<N>/engine<N>.py" (falling back to engine.py) with importlib, then
hands cuci.py that module instead of the live cengine -- so every snapshot can
be driven by one UCI front end, which is what a cross-release suite needs.

ONE VERSION PER PROCESS. The snapshots carry their own .so files and the
loader hands back whichever image it saw first, so two versions in one process
silently run the same code. Each invocation is a fresh process; never import
two of these together.

C-ERA ONLY (v31+). Earlier snapshots predate the C search core and have no
csearch.so for cuci.py to drive.
"""

import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

if len(sys.argv) < 2 or not sys.argv[1].isdigit():
    raise SystemExit("usage: python3 uci/cuci_old.py <version-number>")
_N = sys.argv[1]
del sys.argv[1]                      # cuci.py must not see our argument

_DIR = os.path.join(_ROOT, "Old Engine", _N)
_CAND = [os.path.join(_DIR, f"engine{_N}.py"), os.path.join(_DIR, "engine.py")]
_SRC = next((p for p in _CAND if os.path.isfile(p)), None)
if _SRC is None:
    raise SystemExit(f"no engine module in {_DIR}")
if not os.path.isfile(os.path.join(_DIR, "csearch.so")):
    raise SystemExit(f"v{_N} predates the C core (no csearch.so) -- C era is v31+")

_spec = importlib.util.spec_from_file_location(f"engine{_N}", _SRC)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)       # loads this snapshot's own .so first

import cuci                                              # noqa: E402
cuci.cengine = _mod
cuci.main()
