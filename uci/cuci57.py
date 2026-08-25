#!/usr/bin/env python3
"""uci/cuci57.py -- cuci.py's UCI layer driving the FROZEN v57 engine.

    python3 uci/cuci57.py

v57 is the last pure-HCE release. Plain `cuci.py` is the live engine, which
since v58 arms the NNUE net, so it cannot stand in for v57 in a match that
is specifically about where the hand-crafted eval finished.

Only ONE engine version may be loaded per process: both versions' shared
libraries are named csearch.so/eval_c.so/movegen.so, and dyld hands back the
first image it loaded for a given name, so a second version silently runs the
first one's code. That is why the v57 module is imported BEFORE cuci (whose
own `import cengine` would otherwise win the race), and why the check below
is worth its two seconds: v57's bench signature is 1,145,629 against v58's
1,074,820, so a mix-up is loud instead of silent.
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)              # this file lives in uci/, not the root
_V57 = os.path.join(_ROOT, "Old Engine", "57", "engine57.py")

if not os.path.isfile(_V57):
    raise SystemExit(f"missing the v57 snapshot: {_V57}")

_spec = importlib.util.spec_from_file_location("engine57", _V57)
_e57 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_e57)                 # loads Old Engine/57/*.so first

import cuci                                    # noqa: E402

cuci.cengine = _e57                            # every cengine.Engine() is v57's
cuci._bind_wdl_family(_e57.Engine)             # v57 is HCE, so the hce WDL pair

if __name__ == "__main__":
    if _e57.Engine.USE_NNUE:
        raise SystemExit("the v57 snapshot has USE_NNUE True -- not pure HCE")
    cuci.main()
