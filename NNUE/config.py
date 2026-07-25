"""NNUE/config.py -- the frozen Phase-1 constants (DESIGN_nnue.md is the
contract; change anything here and you are re-freezing the spec + bumping
the affected format version). Shared by gen_data.py, train.py, nnue_ref.py
and the verification harnesses; NNUE/nnue.c hard-codes the same values and
the loader cross-checks them against every .nnue header.
"""

import os

NNUE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(NNUE_DIR)

# --- feature set ----------------------------------------------------------
FEATURE_SET = 1          # 1 = KA8T (advanced, the shipped config); 0 = plain768
KING_BUCKETS = 8 if FEATURE_SET == 1 else 1
THREAT_DIM = 16 if FEATURE_SET == 1 else 0     # T16: 8 int8 per side
IN_DIM = KING_BUCKETS * 768

# QK_MAP[rank][file//2] over the oriented+mirrored own-king square
# (files a-d after the mirror). Must match NN_QK_MAP in nnue.c.
QK_MAP = [
    [0, 1], [2, 3], [4, 5], [6, 6], [7, 7], [7, 7], [7, 7], [7, 7],
]

# --- net shape ------------------------------------------------------------
HIDDEN = 256             # accumulator width H (per perspective)
D2 = 32                  # tail hidden 1
D3 = 32                  # tail hidden 2

# --- quantization (locked integer semantics) ------------------------------
QA = 127                 # activation scale: float 1.0 == int 127
QB = 64                  # tail weight scale
OUT_CP = 400             # float output unit in centipawns (model predicts cp/400)
ACT_MAX = QA * QB        # 8128: clamp bound before >> 6

# trainer weight clips (guarantee the C loader's int16-overflow bound)
FT_WEIGHT_CLIP = 2.0     # |w1| <= 2.0  -> int16 |w| <= 254
FT_BIAS_CLIP = 4.0       # |b1| <= 4.0  -> int16 |b| <= 508;  33*254+508 << 32767
TAIL_WEIGHT_CLIP = QA / QB   # 1.984375 -> int8 range

# --- file formats ---------------------------------------------------------
NNUE_MAGIC = b"PYGINNUE"
NNUE_VERSION = 1
DATA_MAGIC = b"PYGNDATA"
DATA_VERSION = 1
RECORD_SIZE = 88
THREAT_VER = 1           # bump when the T16 formulas change (forces regen)

# --- label rules (Phase 2) ------------------------------------------------
LABEL_NODES = 5000       # fixed node budget per labeling search
LABEL_TT_BITS = 16       # 2^16 x 24B = 1.5 MB TT for LABELING only (the
                         # engine's own default is 21 = 48 MB). Labels are
                         # made with a cold TT per move, so the table is
                         # memset once per move; at 5000 nodes a 48 MB wipe
                         # is ~500x the memory traffic of the search it
                         # serves. gen_data and verify_labels MUST agree on
                         # this -- it is part of what the hard gate replays.
LABEL_MAX_ABS_CP = 2000  # drop positions with |search score| above this
LABEL_MAX_HMC = 40       # drop rule-50-window shuffle states (F5-19 rider)
LABEL_MIN_RANDOM_PLIES = 6   # opening randomization band (uniform legal).
                             # 6, not 4: there are only perft(4)=197,281
                             # distinct 4-ply lines, so a 750k-game random
                             # slice collides ~17.5k times (~2% of games are
                             # a byte-identical replay of another -- the
                             # search is deterministic at fixed nodes, so an
                             # identical opening yields an identical game).
                             # perft(6)=119,060,324 makes that ~0.
LABEL_MAX_RANDOM_PLIES = 12
LABEL_MAX_PLIES = 300    # game cap -> adjudicated draw
LABEL_ADJ_CP = 1500      # early win adjudication: |score| >= this ...
LABEL_ADJ_STREAK = 6     # ... for this many consecutive plies


def engine_fingerprint(eng):
    """One-line build+config fingerprint of a constructed engine.

    gen_data prints it once at generation, verify_labels once at audit.
    The hmc==0 reproduction gate only holds when the two MATCH: a git pull,
    a rebuild, or a different toggle between generating and auditing makes
    the search a different function, and the gate then reports mismatches
    that look like corruption but are just version skew. Printing it turns
    that failure from mysterious into obvious.
    """
    import subprocess
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_DIR,
            capture_output=True, text=True, timeout=5).stdout.strip() or "?"
    except Exception:
        head = "?"
    return (f"engine fp: git={head} abi={eng._lib.csearch_abi()} "
            f"cycle={int(eng.CYCLE_DETECT)} root_lmr={int(eng.ROOT_LMR)} "
            f"nnue={int(eng.USE_NNUE)} tt={int(eng.TT_BITS)}")


# --- default paths --------------------------------------------------------
DATASETS_DIR = os.path.join(NNUE_DIR, "datasets")
NETS_DIR = os.path.join(NNUE_DIR, "nets")
CHECKPOINTS_DIR = os.path.join(NNUE_DIR, "checkpoints")
TOY_NET = os.path.join(NETS_DIR, "toy.nnue")

# --- net naming -----------------------------------------------------------
NET_HASH_CHARS = 12      # Stockfish's nn-<12 hex>.nnue convention


def stamp_net_hash(path):
    """Rename a just-written .nnue to `<stem>_<first 12 of sha256>.nnue`
    and return the new path. Ours reads `nnue_v1_52724f038139.nnue`.

    Why the content hash is IN the name (Stockfish's reason): a net is an
    opaque 3 MB blob, so two different nets under one filename are
    undetectable by eye, and that is exactly the mistake that silently
    invalidates an A/B -- you think you screened v2 and you screened v1.
    With the hash in the name, a mismatched net is a wrong FILENAME, which
    is impossible to miss. The version prefix stays because the hash alone
    says nothing about ORDER; `v2` before `v3` is the part a human reads.

    `toy.nnue` is exempt: it is the plumbing-test artifact, not a version,
    and selftest_nnue.py / selfplay_smoke.py open it by fixed path.
    Re-stamping is idempotent -- an existing hash suffix is replaced, not
    appended, so re-running the trainer on its own output is safe.
    """
    import hashlib
    import re
    stem = os.path.basename(path)
    if stem == os.path.basename(TOY_NET):
        return path
    with open(path, "rb") as fh:
        h = hashlib.sha256(fh.read()).hexdigest()[:NET_HASH_CHARS]
    stem = re.sub(r"\.nnue$", "", stem)
    stem = re.sub(r"_[0-9a-f]{%d}$" % NET_HASH_CHARS, "", stem)
    new = os.path.join(os.path.dirname(path), f"{stem}_{h}.nnue")
    os.replace(path, new)
    return new
