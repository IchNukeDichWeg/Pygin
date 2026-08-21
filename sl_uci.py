#!/usr/bin/env python3
"""sl_uci.py -- a minimal UCI front-end for DeepMind's searchless_chess.

An external yardstick of a completely different shape: a 9M/136M/270M
transformer that plays by ONE forward pass per position, no tree at all
(arXiv 2402.04494). Useful here precisely because it shares nothing with
Pygin -- no search, no NNUE, no handcrafted eval -- so a match against it
probes different weaknesses than Stockfish does.

    python3 uci_match.py \
        --engine1 "python3 cuci.py" --name1 pygin \
        --engine2 "searchless_chess/venv/bin/python sl_uci.py" --name2 sl9M \
        --threads1 1 --hash1 128 --book off --positions 2 \
        --tc1 10+0.1 --depth2 1

--depth2 1 is what takes the searchless side OFF the clock. It ignores every
go limit by construction, but a forward pass still costs ~0.6s on this Mac's
CPU, so under a shared 10+0.1 clock it flags around move 14 and every game
ends TIME_FORFEIT instead of on the board. A fixed limit makes uci_match.py
skip clock accounting for that side; --tc1 then sets Pygin's strength alone.

The clone, its venv and the checkpoints are all gitignored (a checkpoint is
63 MB), so this file is the only tracked part. Recreating them:

    git clone https://github.com/google-deepmind/searchless_chess
    python3 -m venv searchless_chess/venv
    searchless_chess/venv/bin/pip install -r searchless_chess/requirements.txt
    searchless_chess/venv/bin/pip install grain apache-beam \
        jax==0.4.38 jaxlib==0.4.38 chex==0.1.87 orbax-checkpoint==0.6.4 \
        dm-haiku==0.0.13
    cd searchless_chess/checkpoints && ./download.sh   # or just the 9M dir

The pins are not cosmetic: on current jax, `jax.sharding.PositionalSharding`
is gone and the repo's transformer fails to import. 0.4.38 is the last
release that still has it, and chex/orbax/haiku are pinned to versions that
accept that jax.

SMOKE-TESTED 2026-08-21: 4 games vs Pygin at 10+0.1, all four CHECKMATE,
60-127 plies, 4-0 Pygin. No protocol errors, no illegal moves.
"""
import math
import os
import sys

MODEL = "9M"          # released checkpoints: 9M, 136M, 270M. Only the ones
                      # actually downloaded into checkpoints/ will load.

_ROOT = os.path.dirname(os.path.abspath(__file__))
_CLONE = os.path.join(_ROOT, "searchless_chess")
_SRC = os.path.join(_CLONE, "src")
sys.path.insert(0, _ROOT)              # so `searchless_chess.*` imports

if not os.path.isdir(_SRC):
    sys.exit(f"searchless_chess clone not found at {_CLONE} "
             f"-- see the setup recipe in this file's docstring")

import chess                                                      # noqa: E402
from searchless_chess.src.engines import engine as sl_engine      # noqa: E402


def _build():
    """Build the neural engine with cwd parked in src/.

    constants.py builds its checkpoint path from os.getcwd(), so the engine
    only loads when cwd is src/ -- without this it looks for ../checkpoints
    relative to whoever launched the process."""
    from searchless_chess.src.engines import constants
    prev = os.getcwd()
    os.chdir(_SRC)
    try:
        return constants.ENGINE_BUILDERS[MODEL]()
    finally:
        os.chdir(prev)


def _score(engine, board, move):
    """The model's own win probability for `move`, as centipawns.

    The net predicts a distribution over return buckets per legal move; the
    win probability is that distribution's inner product with the bucket
    values, which is exactly the quantity play() maximises. The logistic
    conversion is a courtesy for GUIs and for match.py's WDL adjudication --
    cp here is a transformed win probability, NOT a search score. Returns
    None rather than raising: a missing score costs an info field, while a
    raised exception would cost the game."""
    try:
        import numpy as np
        probs = np.exp(engine.analyse(board)["log_probs"])
        wins = np.inner(probs, engine._return_buckets_values)
        wp = float(wins[sl_engine.get_ordered_legal_moves(board).index(move)])
        wp = min(max(wp, 1e-4), 1 - 1e-4)
        return int(max(-2000, min(2000, -400 * math.log10(1 / wp - 1))))
    except Exception:
        return None


def main():
    engine = None                  # built lazily: `uci` has to answer instantly
    board = chess.Board()
    out = lambda s: (sys.stdout.write(s + "\n"), sys.stdout.flush())

    for line in sys.stdin:
        cmd = line.strip()
        if not cmd:
            continue
        if cmd == "uci":
            out(f"id name searchless_chess-{MODEL}")
            out("id author Google DeepMind")
            out("uciok")
        elif cmd == "isready":
            engine = engine or _build()
            out("readyok")
        elif cmd == "ucinewgame":
            board = chess.Board()
        elif cmd.startswith("position"):
            parts = cmd.split()
            if "startpos" in parts:
                board = chess.Board()
                i = parts.index("startpos") + 1
            else:                                   # position fen <6 fields>
                i = parts.index("fen") + 1
                board = chess.Board(" ".join(parts[i:i + 6]))
                i += 6
            if i < len(parts) and parts[i] == "moves":
                for mv in parts[i + 1:]:
                    board.push(chess.Move.from_uci(mv))
        elif cmd.startswith("go"):
            engine = engine or _build()
            move = engine.play(board)
            cp = _score(engine, board, move)
            score = "score cp 0" if cp is None else f"score cp {cp}"
            out(f"info depth 1 {score} nodes 1 pv {move.uci()}")
            out(f"bestmove {move.uci()}")
        elif cmd == "quit":
            return
        elif cmd.startswith("setoption"):
            pass                                    # no options to set


if __name__ == "__main__":
    main()
