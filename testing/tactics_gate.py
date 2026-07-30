#!/usr/bin/env python3
"""Paired tactical gate for pruning changes -- CALIBRATED, not absolute.

    python3 testing/tactics_gate.py off 11
    python3 testing/tactics_gate.py on  11 --attr CORR_HIST
    python3 testing/tactics_gate.py on  11 --attr PROBCUT_MARGIN --value 200

WHY. R10 doctrine makes matetrack a PRIMARY gate for anything that prunes (it
killed P-33 twice and FI-63 once, saving three slots). No matetrack suite is
on disk; WAC + blindspots (340 positions) tests the same failure mode -- does
the extra pruning walk past a tactic.

READ IT AS A RATIO, NEVER AS A VETO. At fixed depth a node-cutting change
searches less, so it loses positions even when it is GOOD. Calibrated
2026-07-30 at depth 11 against a known winner:

    ProbCut (CONFIRMED +11.44)   -21.6% nodes   294 -> 289   -5   -0.23 / %
    FI-109 uncapped               -25.5% nodes   289 -> 277  -12   -0.47 / %
    FI-109 capped at 64cp         +4.9% nodes    289 -> 284   -5    n/a

So a shipped winner costs ~5 positions. Twice that per unit of pruning is the
warning sign. A change that costs nodes AND positions -- FI-109 capped -- has
no path to positive Elo and does not deserve a screen.

Separate processes per config: csearch.so's toggles are process-wide.
"""
import sys, os, re
sys.path.insert(0, os.getcwd())
import chess                                    # noqa: E402
import cengine                                  # noqa: E402

SUITES = ("testing/wac.epd", "testing/blindspots.epd")


def main():
    if len(sys.argv) < 3:
        print(__doc__.strip().split("\n\n")[1]); return 2
    arm = sys.argv[1] == "on"
    depth = int(sys.argv[2])
    attr = "CORR_HIST"
    value = True
    if "--attr" in sys.argv:
        attr = sys.argv[sys.argv.index("--attr") + 1]
    if "--value" in sys.argv:
        value = int(sys.argv[sys.argv.index("--value") + 1])
    setattr(cengine.Engine, attr, value if arm else
            (0 if isinstance(value, int) and not isinstance(value, bool) else False))
    e = cengine.Engine(); e.use_book = False
    solved = tot = 0
    for path in SUITES:
        if not os.path.exists(path):
            continue
        for line in open(path):
            line = line.strip()
            if " bm " not in line:
                continue
            fen = line.split(" bm ")[0].strip()
            bms = re.split(r"[;,]", line.split(" bm ")[1])[0].split()
            try:
                b = chess.Board(fen)
            except ValueError:
                continue
            e._lib.cs_tt_reset()
            mv = e.get_best_move(b, depth)
            if mv is None:
                continue
            tot += 1
            if {b.san(mv), mv.uci()} & set(bms):
                solved += 1
    print(f"{attr}={'on' if arm else 'off'} depth {depth}: {solved}/{tot} solved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
