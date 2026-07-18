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
LABEL_MAX_ABS_CP = 2000  # drop positions with |search score| above this
LABEL_MAX_HMC = 40       # drop rule-50-window shuffle states (F5-19 rider)
LABEL_MIN_RANDOM_PLIES = 4   # opening randomization band (uniform legal)
LABEL_MAX_RANDOM_PLIES = 12
LABEL_MAX_PLIES = 300    # game cap -> adjudicated draw
LABEL_ADJ_CP = 1500      # early win adjudication: |score| >= this ...
LABEL_ADJ_STREAK = 6     # ... for this many consecutive plies

# --- default paths --------------------------------------------------------
DATASETS_DIR = os.path.join(NNUE_DIR, "datasets")
NETS_DIR = os.path.join(NNUE_DIR, "nets")
CHECKPOINTS_DIR = os.path.join(NNUE_DIR, "checkpoints")
TOY_NET = os.path.join(NETS_DIR, "toy.nnue")
