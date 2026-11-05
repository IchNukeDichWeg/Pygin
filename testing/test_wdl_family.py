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

# Dated on/after NNUE_MIN_DATE, which tracks the CURRENT net rather than the
# first net ever trained (nnue_v12's 2026-08-22 on 2026-08-28, nnue_v15's
# 2026-10-14 on 2026-11-05). Built from the constant so the next bump does not
# have to touch this file. A net change is a cp-scale change, so a log from an
# older net is as unusable as a pre-net one -- STALE_NNUE_LOG (the v12 date,
# which this test accepted until 2026-11-05) pins that half of the contract.
_CUR = F.NNUE_MIN_DATE
NNUE_LOG = f"engine_nnue_vs_engine55_{_CUR}_13-15-20_10556.txt"
STALE_NNUE_LOG = "engine_nnue_vs_engine55_2026-08-22_13-15-20_10556.txt"
HCE_LOG = "cengine_vs_engine55_2026-07-28_09-00-00_1.txt"
OLD_LOG = "cengine_vs_engine52_2026-07-01_09-00-00_1.txt"

# --- family classification ------------------------------------------- #
assert F.eval_family("engine_nnue") == "nnue"
assert F.eval_family("engine_nnue_lazy") == "nnue"
assert F.eval_family("engine55") == "hce"
assert F.eval_family("cengine") == "hce"
# Contract changed 2026-08-10 (was: None). Stockfish is the third family:
# its cp scale gets its own model for match.py's per-side adjudication. It
# is extractable ONLY from allowlisted strength-matched logs -- in-era
# nowhere else, so a casual SF log still contributes nothing.
assert F.eval_family("stockfish_engine") == "sf"
assert not F._side_in_era("stockfish_engine", "cengine_vs_stockfish_engine_2026-08-08_00-00-00_1.txt")
_sf_listed = next(iter(F.NEAR_EQUAL_STOCKFISH_LOGS), None)
if _sf_listed:                      # allowlist may be empty (it is, 2026-08-10)
    assert F._side_in_era("stockfish_engine", _sf_listed + ".txt")
assert F.eval_family("engine_phalanx") is None      # Python-era arm
assert F.eval_family("engine58") == "nnue"          # snapshots by NUMBER
assert F.eval_family("engine57") == "hce"           # ...engine57 is hce forever

# --- the pairing is near-equal in STRENGTH regardless of family ------- #
assert F.near_equal_pair("engine_nnue", "engine55", NNUE_LOG)
assert F.near_equal_pair("cengine", "engine55", HCE_LOG)
# A net older than the shipped one is a different cp scale, so its NNUE side
# is out of era even though the pairing itself is near-equal in strength.
assert not F._side_in_era("engine_nnue", STALE_NNUE_LOG)
assert not F.near_equal_pair("engine_nnue", "engine55", STALE_NNUE_LOG)
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

    # Contract changed 2026-11-05 (was: only *nnue*-named arms were date-
    # gated). The net gate covers EVERY nnue-family side: a cengine or engine64
    # side from an older net is a different cp scale whatever its name says,
    # and the 2026-09-13 fit pooled v4/v12/v14 cp because of exactly this.
    _OLD_NET = "cengine_vs_engine64_2026-09-13_02-57-25_50002.txt"
    assert F.eval_family("cengine", _OLD_NET) == "nnue"
    assert not F._side_in_era("cengine", _OLD_NET)     # was True
    assert not F._side_in_era("engine64", _OLD_NET)    # was True
    _NEW = f"engine65_vs_engine64_{_CUR}_09-00-00_1.txt"
    assert F._side_in_era("engine65", _NEW)
    assert not F._side_in_era("engine64", _NEW)        # older net, newer log
    assert not F.near_equal_pair("engine65", "engine64", _NEW)
    _TEMPO = f"engine_tempo15_vs_cengine_{_CUR}_09-00-00_1.txt"
    assert F.near_equal_pair("engine_tempo15", "cengine", _TEMPO)
    assert F._side_usable("engine_tempo15", _TEMPO)
    assert F._side_usable("cengine", _TEMPO)

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
