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
        # rook files + bishop pair: independent of mobility despite being
        # folded into its loop in eval_c.c, so they port without the magic
        # bitboard attack generation the rest of that pass needs.
        "rook_open": e.ROOK_OPEN_FILE,
        "rook_semi": e.ROOK_SEMIOPEN_FILE,
        "bishop_pair_mg": e.BISHOP_PAIR_MG,
        "bishop_pair_eg": e.BISHOP_PAIR_EG,
        # mop-up: material advantage + king distances, no attack generation.
        # PIECE_VALUES is the mop-up/simplify scale, NOT the tapered
        # MG/EG_VALUES above -- they are different numbers and csearch.c keeps
        # its own twin (PIECE_VAL), so all three retune in lockstep.
        "piece_values": [int(v) for v in e.PIECE_VALUES],
        "mopup_cmd_weight_normal": e.MOPUP_CMD_WEIGHT,
        "mopup_king_weight_normal": e.MOPUP_KING_WEIGHT,
        "center_manhattan": list(e._center_manhattan),
        # mobility + threats (engine.py _mobility_bb). Attack sets are ray
        # walks in JS rather than magic bitboards: magics are a speed trick,
        # and a walk produces the identical set at a scale the browser will
        # never notice.
        "mob_knight": e.MOBILITY_WEIGHT[chess.KNIGHT],
        "mob_bishop": e.MOBILITY_WEIGHT[chess.BISHOP],
        "mob_rook": e.MOBILITY_WEIGHT[chess.ROOK],
        "mob_queen": e.MOBILITY_WEIGHT[chess.QUEEN],
        "use_mobility_area": bool(e.use_mobility_area),
        "use_threats": bool(e.use_threats),
        "threat_pawn": e.THREAT_PAWN,
        "threat_minor": e.THREAT_MINOR,
        # king safety: shield / ring attackers / open file / shelter. All
        # tapered, and the shelter pair tapers to ZERO at the endgame rather
        # than to an EG constant -- mirroring C.
        "king_shield_mg": e.KING_SHIELD_MG, "king_shield_eg": e.KING_SHIELD_EG,
        "king_ring_mg": e.KING_RING_ATTACK_MG, "king_ring_eg": e.KING_RING_ATTACK_EG,
        "king_open_mg": e.KING_OPEN_FILE_MG, "king_open_eg": e.KING_OPEN_FILE_EG,
        "shelter_close": e.SHELTER_CLOSE, "shelter_far": e.SHELTER_FAR,
        "use_king_shelter": bool(e.use_king_shelter),
    }
    # Pawn-structure masks, as decimal strings (they are 64-bit, and JSON
    # numbers are doubles). Exported rather than re-derived in JS: the passed
    # / support / stop-attack masks encode rules that are easy to get subtly
    # wrong by hand, and a wrong mask produces a plausible score rather than
    # an obvious failure. _passed_taper is already blended per phase.
    hexes = lambda seq: [str(int(x)) for x in seq]
    out["file_bb"] = hexes(e._file_bb)
    out["adj_files_bb"] = hexes(e._adj_files_bb)
    for name, attr in (("passed_mask", "_passed_mask"),
                       ("support_mask", "_support_mask"),
                       ("stop_atk_mask", "_stop_atk_mask")):
        m = getattr(e, attr)
        out[name] = {"white": hexes(m[chess.WHITE]), "black": hexes(m[chess.BLACK])}
    out["passed_taper"] = [list(row) for row in e._passed_taper]
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

    # Inject into the inspector too. The page is opened over file://, where a
    # fetch of a sibling JSON is blocked by CORS, so the parameters have to be
    # embedded. Between markers so this stays regenerable rather than becoming
    # the hand-maintained copy the whole exporter exists to avoid.
    inject(out)


def inject(params):
    page = os.path.join(_HERE, "nnue_inspector.html")
    if not os.path.isfile(page):
        return
    src = open(page, encoding="utf-8").read()
    a, b = "/*BEGIN_HCE_PARAMS*/", "/*END_HCE_PARAMS*/"
    i, j = src.find(a), src.find(b)
    if i < 0 or j < 0:
        print("note: inspector has no HCE_PARAMS markers, not injected")
        return
    block = f"{a}\nconst HCE_P={json.dumps(params, separators=(',', ':'))};\n"
    out = src[:i] + block + src[j:]
    if out != src:
        open(page, "w", encoding="utf-8").write(out)
    print(f"injected into {os.path.relpath(page, _ROOT)} "
          f"({len(block):,} bytes between the markers)")


if __name__ == "__main__":
    main()
