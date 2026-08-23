"""engine_nnue.py -- the A/B entry point for a trained NNUE net.

    python3 match.py engine_nnue.py "Old Engine/55/engine55.py" 1000 \
        --workers 0 --nodes 1750000

Exists so arming a net never means editing cengine.py. Flipping USE_NNUE
there would change EVERY other consumer of the engine at once -- cuci, the
selftest, the workbench, any campaign running on another box off the same
checkout -- and a screen is supposed to change one thing. match.py loads an
engine by PATH and instantiates module.Engine(), so a subclass is the whole
mechanism needed; battle_worker gives each side its own process, so the
one-process-one-config .so rule is respected by construction.

NNUE_FILE is resolved by cengine relative to the repo root, and the loader
fails loudly if the file is missing -- there is no silent fall back to the
HCE, so a typo here cannot quietly produce an HCE-vs-HCE null result that
looks like "the net is exactly as strong as the old engine".

Point NNUE_FILE at the net the trainer printed and NNUE/verify.py accepted.
Bump it per bootstrap round; the hash in the filename is what makes it
obvious which net a logged screen actually played.
"""

from cengine import Engine as _HceEngine


class Engine(_HceEngine):
    USE_NNUE = True
    # v3: d2=16 narrow tail, full 82.4M set, cosine LR, 20 epochs. Gives up
    # capacity (val 0.074417 vs v2a's 0.072613 on the same holdout) to buy
    # speed -- the in-search NPS ratio falls from 1.738 to ~1.29, i.e. the
    # deficit goes ~42.5% -> ~23%. Whether that trade pays is exactly what
    # the screen exists to answer; held-out MSE does not predict it.
    NNUE_FILE = "NNUE/nets/nnue_v3_d16_2880b51afe28.nnue"
