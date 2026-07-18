"""NNUE/nnue_ref.py -- the Python-side NNUE truth (numpy, no torch):

  * KA8T feature extraction from .pygdata record rows (must match
    nnue.c's nn_osq/nn_plane/nn_view/QK_MAP exactly -- the Phase-4 gate
    proves it empirically on random positions);
  * .nnue format v1 reader/writer (CRC32, header cross-checks);
  * the QUANTIZED reference forward pass: pure int32 numpy with the frozen
    integer semantics (clamp-then-shift activations, trunc-division
    output). nnue.c's scalar and NEON paths must match this EXACTLY.

Everything here is importable from both the system python (verification
harnesses) and the training venv (dataloader).
"""

import struct
import zlib

import numpy as np

from config import (FEATURE_SET, IN_DIM, HIDDEN, THREAT_DIM, KING_BUCKETS,
                    D2, D3, QA, QB, OUT_CP, ACT_MAX, QK_MAP,
                    NNUE_MAGIC, NNUE_VERSION,
                    FT_WEIGHT_CLIP, FT_BIAS_CLIP, TAIL_WEIGHT_CLIP)

PAD_IDX = IN_DIM                     # Embedding padding row (trainer side)

_QK = np.asarray(QK_MAP, dtype=np.int64)          # [rank][file//2]
_PT_BBS = ("pawns", "knights", "bishops", "rooks", "queens", "kings")


def _bits(bb):
    """uint64 array [B] -> bool array [B, 64] (bit i = square i)."""
    b = np.ascontiguousarray(bb, dtype="<u8")
    return np.unpackbits(b.view(np.uint8).reshape(-1, 8),
                         axis=1, bitorder="little").astype(bool)


def extract_features(recs):
    """Record rows -> (idx_w, idx_b) int64 [B, 32], padded with PAD_IDX.

    idx_w = active KA8T features of the WHITE perspective, idx_b BLACK.
    Mirrors nnue.c exactly: orientation sq^56 for Black, horizontal mirror
    ^7 when the oriented own-king file >= e, QK_MAP bucket, planes 0..5 own
    P..K / 6..11 opponent P..K. plain-768 config: no mirror, bucket 0.
    """
    recs = np.atleast_1d(recs)
    B = len(recs)
    union = np.zeros(B, dtype=np.uint64)
    for f in _PT_BBS:
        union |= recs[f]
    occ = {1: recs["occ_w"].astype(np.uint64),
           0: (union & ~recs["occ_w"]).astype(np.uint64)}

    out = {}
    sq = np.arange(64, dtype=np.int64)
    for persp in (1, 0):                                   # WHITE=1, BLACK=0
        kb = _bits(recs["kings"] & occ[persp])
        ksq = np.argmax(kb, axis=1)                        # kingless -> 0
        has_k = kb.any(axis=1)
        oks = np.where(persp == 1, ksq, ksq ^ 56)
        if FEATURE_SET == 1:
            mirror = np.where(has_k, (oks & 7) >= 4, False)
            oks = np.where(mirror, oks ^ 7, oks)
            bucket = np.where(has_k, _QK[oks >> 3, (oks & 7) >> 1], 0)
        else:
            mirror = np.zeros(B, dtype=bool)
            bucket = np.zeros(B, dtype=np.int64)

        xor_row = (0 if persp == 1 else 56) ^ np.where(mirror, 7, 0)  # [B]
        idx = np.full((B, 32), PAD_IDX, dtype=np.int64)
        fill = np.zeros(B, dtype=np.int64)
        for color in (1, 0):
            for pt_i, f in enumerate(_PT_BBS):
                plane = (0 if color == persp else 6) + pt_i
                rows, sqs = np.nonzero(_bits(recs[f] & occ[color]))
                if len(rows) == 0:
                    continue
                feats = (bucket[rows] * 768 + plane * 64
                         + (sqs ^ xor_row[rows]))
                # scatter into the next free slot per row
                for r, ft in zip(rows, feats):
                    idx[r, fill[r]] = ft
                    fill[r] += 1
        out[persp] = idx
    return out[1], out[0]


# --------------------------- .nnue I/O ------------------------------------
_HDR = struct.Struct("<8s12IQ")
assert _HDR.size == 64


class QuantNet:
    """Quantized tensors, exactly as stored on disk (unpadded rows)."""

    def __init__(self, w1, b1, w2, b2, w3, b3, w4, b4):
        self.w1 = np.asarray(w1, dtype=np.int16)     # [IN][H]
        self.b1 = np.asarray(b1, dtype=np.int16)     # [H]
        self.w2 = np.asarray(w2, dtype=np.int8)      # [D2][2H+T]
        self.b2 = np.asarray(b2, dtype=np.int32)
        self.w3 = np.asarray(w3, dtype=np.int8)      # [D3][D2]
        self.b3 = np.asarray(b3, dtype=np.int32)
        self.w4 = np.asarray(w4, dtype=np.int8)      # [D3]
        self.b4 = int(b4)

    @classmethod
    def from_float(cls, w1, b1, w2, b2, w3, b3, w4, b4):
        """Float tensors (trainer layout) -> quantized (with the frozen
        clips applied, so export == what training simulated)."""
        q = cls(
            np.round(np.clip(w1, -FT_WEIGHT_CLIP, FT_WEIGHT_CLIP) * QA),
            np.round(np.clip(b1, -FT_BIAS_CLIP, FT_BIAS_CLIP) * QA),
            np.round(np.clip(w2, -TAIL_WEIGHT_CLIP, TAIL_WEIGHT_CLIP) * QB),
            np.round(np.asarray(b2) * QA * QB),
            np.round(np.clip(w3, -TAIL_WEIGHT_CLIP, TAIL_WEIGHT_CLIP) * QB),
            np.round(np.asarray(b3) * QA * QB),
            np.round(np.clip(w4, -TAIL_WEIGHT_CLIP, TAIL_WEIGHT_CLIP) * QB),
            round(float(b4) * QA * QB),
        )
        maxw = int(np.abs(q.w1.astype(np.int32)).max())
        maxb = int(np.abs(q.b1.astype(np.int32)).max())
        assert 33 * maxw + maxb <= 32767, "FT int16 overflow bound violated"
        return q

    def save(self, path):
        payload = b"".join([
            self.w1.astype("<i2").tobytes(), self.b1.astype("<i2").tobytes(),
            self.w2.astype("i1").tobytes(), self.b2.astype("<i4").tobytes(),
            self.w3.astype("i1").tobytes(), self.b3.astype("<i4").tobytes(),
            self.w4.astype("i1").tobytes(),
            np.int32(self.b4).astype("<i4").tobytes(),
        ])
        hdr = _HDR.pack(NNUE_MAGIC, NNUE_VERSION, FEATURE_SET, IN_DIM,
                        HIDDEN, THREAT_DIM, KING_BUCKETS, D2, D3,
                        QA, QB, OUT_CP, zlib.crc32(payload), len(payload))
        with open(path, "wb") as f:
            f.write(hdr)
            f.write(payload)

    @classmethod
    def load(cls, path):
        with open(path, "rb") as f:
            (magic, ver, fs, ind, h, td, kb, d2, d3, qa, qb, ocp,
             crc, psz) = _HDR.unpack(f.read(64))
            payload = f.read()
        if magic != NNUE_MAGIC or ver != NNUE_VERSION:
            raise ValueError("bad magic/version")
        if (fs, ind, h, td, kb, d2, d3, qa, qb, ocp) != \
           (FEATURE_SET, IN_DIM, HIDDEN, THREAT_DIM, KING_BUCKETS,
                D2, D3, QA, QB, OUT_CP):
            raise ValueError("arch mismatch vs config.py")
        if len(payload) != psz or zlib.crc32(payload) != crc:
            raise ValueError("payload size/CRC mismatch")
        in2 = 2 * h + td
        o = 0
        def take(dt, n):
            nonlocal o
            a = np.frombuffer(payload, dtype=dt, count=n, offset=o)
            o += a.nbytes
            return a
        w1 = take("<i2", ind * h).reshape(ind, h)
        b1 = take("<i2", h)
        w2 = take("i1", d2 * in2).reshape(d2, in2)
        b2 = take("<i4", d2)
        w3 = take("i1", d3 * d2).reshape(d3, d2)
        b3 = take("<i4", d3)
        w4 = take("i1", d3)
        b4 = int(take("<i4", 1)[0])
        return cls(w1, b1, w2, b2, w3, b3, w4, b4)

    # ------------------ the quantized reference forward ------------------
    def forward(self, idx_w, idx_b, threat, stm):
        """EXACT integer forward, one position.

        idx_w/idx_b: active features per perspective (ints, no padding);
        threat: 16 uint8 (White vec then Black vec, dataset layout);
        stm: 1 = White to move. Returns stm-POV cp (int) -- must equal
        nnue.c's nn_forward / nnue_eval_oracle bit for bit.
        """
        w1 = self.w1.astype(np.int32)
        acc_w = self.b1.astype(np.int32) + w1[list(idx_w)].sum(axis=0)
        acc_b = self.b1.astype(np.int32) + w1[list(idx_b)].sum(axis=0)
        assert np.abs(acc_w).max() <= 32767 and np.abs(acc_b).max() <= 32767
        us, them = (acc_w, acc_b) if stm else (acc_b, acc_w)
        t = np.asarray(threat, dtype=np.int32)
        tus, tthem = (t[:8], t[8:]) if stm else (t[8:], t[:8])
        x = np.concatenate([np.clip(us, 0, QA), np.clip(them, 0, QA),
                            tus, tthem]) if THREAT_DIM else \
            np.concatenate([np.clip(us, 0, QA), np.clip(them, 0, QA)])

        def act(v):
            return np.minimum(np.maximum(v, 0), ACT_MAX) >> 6

        h1 = act(self.b2 + self.w2.astype(np.int32) @ x)
        h2 = act(self.b3 + self.w3.astype(np.int32) @ h1)
        out = int(self.b4 + self.w4.astype(np.int32) @ h2)
        q = abs(out) * OUT_CP // ACT_MAX           # trunc toward zero,
        return q if out >= 0 else -q               # matching C division


if __name__ == "__main__":            # ponytail: smallest runnable check
    rng = np.random.default_rng(0)
    q = QuantNet.from_float(
        rng.normal(0, 0.05, (IN_DIM, HIDDEN)),
        rng.normal(0, 0.1, HIDDEN),
        rng.normal(0, 0.1, (D2, 2 * HIDDEN + THREAT_DIM)),
        rng.normal(0, 0.1, D2),
        rng.normal(0, 0.1, (D3, D2)), rng.normal(0, 0.1, D3),
        rng.normal(0, 0.1, D3), 0.01)
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "t.nnue")
        q.save(p)
        q2 = QuantNet.load(p)
        assert (q.w1 == q2.w1).all() and q.b4 == q2.b4
        v1 = q.forward([3, 700, 4000], [5, 900, 5000],
                       list(range(16)), 1)
        v2 = q2.forward([3, 700, 4000], [5, 900, 5000],
                        list(range(16)), 1)
        assert v1 == v2
    print("nnue_ref self-check OK (roundtrip + forward", v1, ")")
