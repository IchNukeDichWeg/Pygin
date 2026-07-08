"""
wdl.py -- shared W/D/L prediction from the fitted model (wdl_model.json).

    from wdl import wdl_white
    w, d, l = wdl_white(score_white_cp, board)   # percentages, White's view

Same convention Stockfish's WDL display uses: the fitted logistic gives
P(win) for the side reporting `cp` at the current game phase, so
P(White win) = model(+cp), P(Black win) = model(-cp) and the draw is the
remainder (clamped at 0 and renormalised -- the low-phase fit can put the
two win probabilities slightly above 1 combined; see fit_wdl_model.py's
phase_clamp_min note). Always White's perspective, so the numbers never
flip with the side to move. Returns None when wdl_model.json is missing.

Mate-convention scores (|score| >= 999_000, engine.py's MATE_THRESHOLD)
short-circuit to 100/0/0. The model is loaded once per process; call
`reload()` after refitting to pick a new file up in a live host.
"""

import json
import math
import os

_DIR = os.path.dirname(os.path.abspath(__file__))
_MODEL_PATH = os.path.join(_DIR, "wdl_model.json")
_MATE_THRESHOLD = 999_000

_model = ["unloaded"]


def _load():
    if _model[0] == "unloaded":
        try:
            with open(_MODEL_PATH, encoding="utf-8") as fh:
                _model[0] = json.load(fh)
        except OSError:
            _model[0] = None
    return _model[0]


def reload():
    """Forget the cached model (e.g. after fit_wdl_model.py rewrote it)."""
    _model[0] = "unloaded"


def _phase(board):
    """engine.py's tapered-eval phase (PHASE_WEIGHTS/PHASE_MAX)."""
    return (board.knights.bit_count() + board.bishops.bit_count()
            + board.rooks.bit_count() * 2 + board.queens.bit_count() * 4)


def _p_win(cp, phase, m):
    x = max(m["phase_clamp_min"], min(m["phase_max"], phase)) / m["phase_max"]
    a = sum(c * x ** i for i, c in enumerate(m["as"]))
    b = sum(c * x ** i for i, c in enumerate(m["bs"]))
    return 1.0 / (1.0 + math.exp((a - cp) / b))


def wdl_white(score_white, board):
    """(win, draw, loss) percentages from White's perspective, or None.

    score_white : engine score in centipawns, White POV (engine.last_score)
    board       : chess.Board (only the piece counts are read, for phase)
    """
    m = _load()
    if m is None or score_white is None:
        return None
    if score_white >= _MATE_THRESHOLD:
        return (100.0, 0.0, 0.0)
    if score_white <= -_MATE_THRESHOLD:
        return (0.0, 0.0, 100.0)
    ph = _phase(board)
    w = _p_win(score_white, ph, m)
    l = _p_win(-score_white, ph, m)
    d = 1.0 - w - l
    if d < 0.0:                       # low-phase fit edge: renormalise W+L
        w, l, d = w / (w + l), l / (w + l), 0.0
    return (round(w * 100, 1), round(d * 100, 1), round(l * 100, 1))


def format_wdl(score_white, board):
    """'White 40.1% / Draw 50.9% / Black 9.0%' or '' when unavailable."""
    r = wdl_white(score_white, board)
    if r is None:
        return ""
    w, d, l = r
    return f"White {w}% / Draw {d}% / Black {l}%"


if __name__ == "__main__":           # ponytail: smallest self-check
    import chess
    r = wdl_white(0, chess.Board())
    assert r is None or (abs(sum(r) - 100.0) < 0.5 and r[0] < 60), r
    m = wdl_white(1_000_000 - 5, chess.Board())
    assert m is None or m == (100.0, 0.0, 0.0), m
    print("wdl self-check OK:", format_wdl(150, chess.Board()))
