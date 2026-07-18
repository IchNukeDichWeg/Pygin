"""NNUE/model.py -- the PyTorch float model (trainer side).

Mirrors the frozen Phase-1 architecture: shared feature transformer
(Embedding-sum == accumulator), CReLU clamps to [0,1], tail
[2H+T] -> D2 -> D3 -> 1, output in cp/OUT_CP units. clip_weights() applies
the quantization clips after every optimizer step (QAT-style: the float
model always stays inside the representable integer range, so the export
rounding is faithful -- naive post-training rounding of an unclipped model
reliably tanks net quality, per DESIGN_nnue.md Phase 3).
"""

import torch
import torch.nn as nn

from config import (IN_DIM, HIDDEN, THREAT_DIM, D2, D3,
                    FT_WEIGHT_CLIP, FT_BIAS_CLIP, TAIL_WEIGHT_CLIP)
from nnue_ref import PAD_IDX


class NNUEModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.ft = nn.Embedding(IN_DIM + 1, HIDDEN, padding_idx=PAD_IDX)
        nn.init.normal_(self.ft.weight, std=0.05)
        with torch.no_grad():
            self.ft.weight[PAD_IDX].zero_()
        self.ft_bias = nn.Parameter(torch.zeros(HIDDEN))
        self.l2 = nn.Linear(2 * HIDDEN + THREAT_DIM, D2)
        self.l3 = nn.Linear(D2, D3)
        self.out = nn.Linear(D3, 1)

    def forward(self, idx_us, idx_them, threat_us_them):
        """idx_*: [B, 32] int64 (PAD_IDX-padded); threat_us_them: [B, T]
        float in [0,1] (already stm-ordered). Returns [B] in cp/OUT_CP."""
        a_us = self.ft(idx_us).sum(dim=1) + self.ft_bias
        a_th = self.ft(idx_them).sum(dim=1) + self.ft_bias
        x = torch.cat([a_us.clamp(0, 1), a_th.clamp(0, 1)], dim=1)
        if THREAT_DIM:
            x = torch.cat([x, threat_us_them], dim=1)
        h = self.l2(x).clamp(0, 1)
        h = self.l3(h).clamp(0, 1)
        return self.out(h).squeeze(1)

    @torch.no_grad()
    def clip_weights(self):
        self.ft.weight.clamp_(-FT_WEIGHT_CLIP, FT_WEIGHT_CLIP)
        self.ft.weight[PAD_IDX].zero_()
        self.ft_bias.clamp_(-FT_BIAS_CLIP, FT_BIAS_CLIP)
        for lin in (self.l2, self.l3, self.out):
            lin.weight.clamp_(-TAIL_WEIGHT_CLIP, TAIL_WEIGHT_CLIP)
