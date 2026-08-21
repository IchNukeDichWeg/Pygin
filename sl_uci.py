#!/usr/bin/env python3
"""sl_uci.py -- a minimal UCI front-end for DeepMind's searchless_chess.

An external yardstick of a completely different shape: a transformer that
plays by ONE forward pass per position, no tree at all (arXiv 2402.04494).
Useful here precisely because it shares nothing with Pygin -- no search, no
NNUE, no handcrafted eval -- so a match against it probes different
weaknesses than Stockfish does.

    python3 uci_match.py \
        --engine1 "python3 cuci.py" --name1 pygin \
        --engine2 "searchless_chess/venv/bin/python sl_uci.py" --name2 sl9M \
        --option2 Model=9M \
        --threads1 1 --hash1 128 --book off --positions 2 \
        --tc1 10+0.1 --depth2 1

--depth2 1 is what takes the searchless side OFF the clock, and it is not
optional. The net ignores every go limit by construction, but a forward
pass still costs real time: measured on this Mac's CPU over 10 moves from
one 30-legal-move position, 9M has a median move of 1.37s (0.71-2.00) after
a 3.2s build. Cost scales with the legal move count, because action-value
batches one sequence per legal move, and with model size. Under a shared
10+0.1 clock the 9M flags around move 14 and every game ends TIME_FORFEIT
instead of on the board. A fixed limit makes uci_match.py skip clock
accounting for that side; --tc1 then sets Pygin's strength alone.

MODELS. `setoption name Model value X` picks the checkpoint, so one command
per model with no file edits (uci_match.py spells it --option2 Model=X).
Three load: 9M, 136M and 270M, all action-value -- P(win | state, action),
the paper's headline line, same Lichess corpus at three sizes. 270M is the
one the paper rates ~2895 Lichess blitz.

The release also publishes 9M_state_value and 9M_behavioral_cloning, and
NEITHER can be loaded by the released code. Their checkpoints are Flax:
every key looks like ('params', 'Dense_0', 'kernel'). The repo's
transformer.py is Haiku, whose keys look like ('embed', 'embeddings'), and
there is no Flax module anywhere in src/. So those two were saved from an
implementation that was never published, and loading them would mean
writing a Flax transformer and reverse-engineering its config from the
checkpoint. Not a path bug and not worth fixing -- they are the paper's
weaker ablations. Downloading them is a waste of 62 MB; download.sh pulls
them anyway.

The clone, its venv and the checkpoints (~2.9 GB for all five) are
gitignored, so this file is the only tracked part. Recreating them:

    git clone https://github.com/google-deepmind/searchless_chess
    python3 -m venv searchless_chess/venv
    searchless_chess/venv/bin/pip install -r searchless_chess/requirements.txt
    searchless_chess/venv/bin/pip install grain apache-beam \
        jax==0.4.38 jaxlib==0.4.38 chex==0.1.87 orbax-checkpoint==0.6.4 \
        dm-haiku==0.0.13
    cd searchless_chess/checkpoints && ./download.sh

The pins are not cosmetic: on current jax, `jax.sharding.PositionalSharding`
is gone and the repo's transformer fails to import. 0.4.38 is the last
release that still has it, and chex/orbax/haiku are pinned to versions that
accept that jax. Note download.sh unzips and rm's the archive in one run --
re-running it after a partial download deletes what it just failed to get.

SMOKE-TESTED 2026-08-21 (9M): 6 games vs Pygin at 10+0.1, all six CHECKMATE,
60-127 plies, 6-0 Pygin. No protocol errors, no illegal moves.
"""
import math
import os
import sys

DEFAULT_MODEL = "9M"          # overridden by `setoption name Model value X`

# Arch and policy per checkpoint. Upstream's ENGINE_BUILDERS only covers the
# three action-value nets, so the two 9M ablations are built here from the
# same parts -- they share 9M's dimensions and differ only in policy head.
MODELS = {
    "9M":   ("action_value",  8,  256, 8),
    "136M": ("action_value",  8, 1024, 8),
    "270M": ("action_value", 16, 1024, 8),
}
CHECKPOINT_STEP = 6_400_000

_ROOT = os.path.dirname(os.path.abspath(__file__))
_CLONE = os.path.join(_ROOT, "searchless_chess")
_SRC = os.path.join(_CLONE, "src")
sys.path.insert(0, _ROOT)              # so `searchless_chess.*` imports

if not os.path.isdir(_SRC):
    sys.exit(f"searchless_chess clone not found at {_CLONE} "
             f"-- see the setup recipe in this file's docstring")

import chess                                                      # noqa: E402
import numpy as np                                                # noqa: E402
from searchless_chess.src.engines import engine as sl_engine      # noqa: E402


def _build(model):
    """Build the neural engine with cwd parked in src/.

    constants.py builds its checkpoint path from os.getcwd(), so the engine
    only loads when cwd is src/ -- without this it looks for ../checkpoints
    relative to whoever launched the process."""
    import jax.random as jrandom
    import orbax.checkpoint as ocp
    from searchless_chess.src import tokenizer, transformer, utils
    from searchless_chess.src.engines import neural_engines

    policy, num_layers, embedding_dim, num_heads = MODELS[model]
    num_return_buckets = 128
    output_size = (utils.NUM_ACTIONS if policy == "behavioral_cloning"
                   else num_return_buckets)
    predictor = transformer.build_transformer_predictor(
        config=transformer.TransformerConfig(
            vocab_size=utils.NUM_ACTIONS,
            output_size=output_size,
            pos_encodings=transformer.PositionalEncodings.LEARNED,
            max_sequence_length=tokenizer.SEQUENCE_LENGTH + 2,
            num_heads=num_heads,
            num_layers=num_layers,
            embedding_dim=embedding_dim,
            apply_post_ln=True,
            apply_qk_layernorm=False,
            use_causal_mask=False,
        ))
    init = predictor.initial_params(rng=jrandom.PRNGKey(1),
                                    targets=np.ones((1, 1), dtype=np.uint32))
    # Two on-disk layouts ship in the same release. The three action-value
    # zips unpack to <model>/<step>/params, which is what upstream's
    # training_utils.load_parameters expects; the two 9M ablation zips
    # unpack the params directory's CONTENTS straight into <model>/, with no
    # step layer, so that loader raises FileNotFoundError on them. Restoring
    # through orbax directly covers both -- it is the same call
    # load_parameters ends in, just without the step lookup.
    path = os.path.join(_CLONE, "checkpoints", model,
                        str(CHECKPOINT_STEP), "params")
    params = ocp.Checkpointer(ocp.PyTreeCheckpointHandler()).restore(
        path, restore_args=ocp.checkpoint_utils.construct_restore_args(init))
    _, bucket_values = utils.get_uniform_buckets_edges_values(num_return_buckets)
    return neural_engines.ENGINE_FROM_POLICY[policy](
        return_buckets_values=bucket_values,
        predict_fn=neural_engines.wrap_predict_fn(
            predictor=predictor, params=params, batch_size=1))


def _score(engine, board, move, policy):
    """The model's own win probability for `move`, as centipawns.

    The value nets predict a distribution over return buckets; the win
    probability is that distribution's inner product with the bucket values,
    which is exactly the quantity play() maximises. Action-value keys it
    per-move under 'log_probs'; state-value evaluates the SUCCESSORS instead
    and keys them under 'next_log_probs' (already flipped to the mover's
    point of view upstream).

    Only action_value is reachable today (see the docstring on why the
    state-value checkpoint will not load), but the state-value key is kept
    because it costs one conditional and is the difference between working
    and silently scoring the wrong move if that net ever becomes loadable.

    uci_match.py does not adjudicate on score -- MAX_PLIES is its only
    adjudicated result -- so this is for the log, not for correctness.
    Returns None rather than raising: a missing score costs an info field,
    while a raised exception would cost the game."""
    try:
        res = engine.analyse(board)
        key = "log_probs" if policy == "action_value" else "next_log_probs"
        wins = np.inner(np.exp(res[key]), engine._return_buckets_values)
        wp = float(wins[sl_engine.get_ordered_legal_moves(board).index(move)])
        wp = min(max(wp, 1e-4), 1 - 1e-4)
        return int(max(-2000, min(2000, -400 * math.log10(1 / wp - 1))))
    except Exception:
        return None


def main():
    model = DEFAULT_MODEL
    engine = None                  # built lazily: `uci` has to answer instantly
    board = chess.Board()
    out = lambda s: (sys.stdout.write(s + "\n"), sys.stdout.flush())

    def ready():
        nonlocal engine
        if engine is None:
            engine = _build(model)
        return engine

    for line in sys.stdin:
        cmd = line.strip()
        if not cmd:
            continue
        if cmd == "uci":
            out(f"id name searchless_chess-{model}")
            out("id author Google DeepMind")
            out(f"option name Model type combo default {DEFAULT_MODEL} "
                + " ".join(f"var {m}" for m in MODELS))
            out("uciok")
        elif cmd.startswith("setoption"):
            parts = cmd.split()
            if "name" in parts and "value" in parts:
                name = " ".join(parts[parts.index("name") + 1:
                                      parts.index("value")])
                value = " ".join(parts[parts.index("value") + 1:])
                if name.lower() == "model":
                    if value not in MODELS:
                        out(f"info string unknown Model {value}, keeping {model}")
                    elif value != model:
                        model, engine = value, None   # rebuilt on next isready
        elif cmd == "isready":
            ready()
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
            eng = ready()
            move = eng.play(board)
            cp = _score(eng, board, move, MODELS[model][0])
            score = "" if cp is None else f"score cp {cp} "
            out(f"info depth 1 {score}nodes 1 pv {move.uci()}")
            out(f"bestmove {move.uci()}")
        elif cmd == "quit":
            return


if __name__ == "__main__":
    main()
