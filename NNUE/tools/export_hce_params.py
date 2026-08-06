#!/usr/bin/env python3
"""export_hce_params.py -- dump the LIVE hand-crafted eval parameters to JSON.

    python3 NNUE/tools/export_hce_params.py

Writes NNUE/tools/hce_params.json for nnue_inspector.html's HCE panel.

The point is that the browser never carries a hand-copied table. engine.py is
the single source of eval truth (cengine pushes these very values into
csearch.so at construction), so a retune that moves 44 scalars moves this file
too and the page stops agreeing with a stale copy. Re-run it after any eval
change, exactly like the selftest pins.

The field list mirrors cengine.py's csearch_set_eval call site, so if the C
core gains a parameter this file is the second place to add it and the
mismatch will show up in the browser as a disagreement with the engine.
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "lib")]

import chess                                             # noqa: E402
import engine as eng_mod                                 # noqa: E402

ORDER = [chess.PAWN, chess.KNIGHT, chess.BISHOP,
         chess.ROOK, chess.QUEEN, chess.KING]
NAMES = ["pawn", "knight", "bishop", "rook", "queen", "king"]


def main():
    e = eng_mod.Engine()
    out = {
        "_source": "engine.py via export_hce_params.py -- do not hand-edit",
        "order": NAMES,
        # tapered material + piece-square tables, White's perspective
        "mg_tables": {n: list(e.mg_tables[pt]) for n, pt in zip(NAMES, ORDER)},
        "eg_tables": {n: list(e.eg_tables[pt]) for n, pt in zip(NAMES, ORDER)},
        "mg_values": {n: e.MG_VALUES[pt] for n, pt in zip(NAMES, ORDER)},
        "eg_values": {n: e.EG_VALUES[pt] for n, pt in zip(NAMES, ORDER)},
        "phase_weights": {n: e.PHASE_WEIGHTS[pt] for n, pt in zip(NAMES, ORDER)},
        "tempo": e.TEMPO,
        # pawn structure, both taper halves
        "doubled_mg": e.DOUBLED_PAWN, "doubled_eg": e.DOUBLED_PAWN_EG,
        "isolated_mg": e.ISOLATED_PAWN, "isolated_eg": e.ISOLATED_PAWN_EG,
        "backward_mg": e.BACKWARD_PAWN, "backward_eg": e.BACKWARD_PAWN_EG,
        "passed_mg": list(e.PASSED_PAWN_MG),
        "passed_eg": list(e.PASSED_PAWN_EG),
        # endgame mop-up
        "mopup_min_adv": e.MOPUP_MIN_ADV,
        "mopup_cmd_weight": e.MOPUP_STRONG_CMD_WEIGHT,
        "mopup_king_weight": e.MOPUP_STRONG_KING_WEIGHT,
        # phase taper
        "phase_max": getattr(e, "PHASE_MAX", None),
    }
    # The eval fingerprint cengine already computes over this same payload.
    # Carrying it lets the page say WHICH eval it is reproducing, and lets a
    # future check assert the JSON was regenerated after a retune.
    try:
        ce_mod = __import__("cengine")
        out["eval_fingerprint"] = ce_mod.Engine()._eval_fingerprint()
    except Exception as ex:                    # cengine needs the built .so
        out["eval_fingerprint"] = None
        print(f"note: no eval fingerprint ({type(ex).__name__}) -- "
              f"run ./setup.sh if you want it stamped")

    path = os.path.join(_HERE, "hce_params.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, sort_keys=True)
    n = sum(len(v) for v in out["mg_tables"].values()) * 2
    print(f"wrote {os.path.relpath(path, _ROOT)}: {n} table entries, "
          f"{len(out)} fields, fingerprint {out['eval_fingerprint']}")


if __name__ == "__main__":
    main()
