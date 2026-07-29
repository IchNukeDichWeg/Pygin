#!/usr/bin/env python3
"""test_wdl_family.py -- the eval-FAMILY guard in tuning/fit_wdl_model.py.

The bug this pins: engine_nnue.py / engine_nnue_lazy.py have no version
number in their base name, so the old ^engine(\\d+)$ gate dropped every NNUE
log from the WDL corpus in silence. The fix must (a) accept those logs,
(b) extract only the selected family's side, so NNUE cp and hand-crafted cp
never land in one fit, and (c) say so out loud when it drops something.

    python3 testing/test_wdl_family.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tuning"))

import fit_wdl_model as F

NNUE_LOG = "engine_nnue_vs_engine55_2026-07-28_13-15-20_10556.txt"
HCE_LOG = "cengine_vs_engine55_2026-07-28_09-00-00_1.txt"
OLD_LOG = "cengine_vs_engine52_2026-07-01_09-00-00_1.txt"

# --- family classification ------------------------------------------- #
assert F.eval_family("engine_nnue") == "nnue"
assert F.eval_family("engine_nnue_lazy") == "nnue"
assert F.eval_family("engine55") == "hce"
assert F.eval_family("cengine") == "hce"
assert F.eval_family("stockfish_engine") is None
assert F.eval_family("engine_phalanx") is None      # Python-era arm

# --- the pairing is near-equal in STRENGTH regardless of family ------- #
assert F.near_equal_pair("engine_nnue", "engine55", NNUE_LOG)
assert F.near_equal_pair("cengine", "engine55", HCE_LOG)
assert not F.near_equal_pair("engine53", "engine55", HCE_LOG)   # 2 eras apart
assert not F.near_equal_pair("cengine", "engine52", OLD_LOG)    # pre-v53

# --- but only the selected family's side is ever extracted ------------ #
try:
    F.EVAL_FAMILY = "hce"
    assert not F._side_usable("engine_nnue", NNUE_LOG)
    assert F._side_usable("engine55", NNUE_LOG)        # HCE side still counts

    F.EVAL_FAMILY = "nnue"
    assert F._side_usable("engine_nnue", NNUE_LOG)
    assert F._side_usable("engine_nnue_lazy", NNUE_LOG)
    assert not F._side_usable("engine55", NNUE_LOG)
    assert not F._side_usable("engine_nnue", OLD_LOG)  # pre-net date gate

    F.EVAL_FAMILY = None                               # texel.py's widening
    assert F._side_usable("engine_nnue", NNUE_LOG)
    assert F._side_usable("engine55", NNUE_LOG)
finally:
    F.EVAL_FAMILY = "hce"

# --- an unrecognised arm in a current-era log is LOUD, not silent ----- #
F.UNRECOGNISED_BASES.clear()
F._note_skip("engine_brandnew", "engine55", "engine_brandnew_vs_engine55_"
             "2026-07-29_09-00-00_1.txt")
assert F.UNRECOGNISED_BASES == {"engine_brandnew": 1}, F.UNRECOGNISED_BASES
F.UNRECOGNISED_BASES.clear()
F._note_skip("engine_phalanx", "engine14", OLD_LOG)    # Python era: not news
assert not F.UNRECOGNISED_BASES, F.UNRECOGNISED_BASES

print("test_wdl_family: ok")
