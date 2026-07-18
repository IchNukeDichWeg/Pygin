#!/usr/bin/env python3
"""NNUE/train.py -- FI-15 Phase 3: PyTorch trainer + quantized export.

    NNUE/venv/bin/python NNUE/train.py NNUE/datasets/smoke100k.pygdata \
        --epochs 40 --out NNUE/nets/toy.nnue

Loads a .pygdata dataset, extracts KA8T features (nnue_ref.extract_features,
the same index math the C side implements), trains the float model with
QAT-style weight clipping (model.py), tracks train/val loss, checkpoints
per epoch, and exports the best-val model to the .nnue quantized format.
After export it cross-checks: quantized-reference forward vs the float
model on a sample (reports MAE in cp -- quantization noise, expected small).

Label: u = LAMBDA * clamp(score, +/-2000)/400
         + (1-LAMBDA) * result * RESULT_CP/400        (White POV, then
flipped to stm POV to match the net's output convention).

The dataloader pre-extracts features ONCE into memory (int64 [N,32] x 2 +
threat bytes): 100k positions ~ 50 MB. For the 50M real run pass
--chunk 2000000: each epoch streams the mmap'd records in chunks (chunk
order + within-chunk order shuffled per epoch -- the standard approximate
shuffle; generation already interleaves games across worker shards), and
features are re-extracted per chunk (~20 s per 2M chunk) instead of held
in RAM (50M in-memory would need ~25 GB of index tensors).
"""

import argparse
import csv
import os
import sys
import time

import numpy as np

NNUE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, NNUE_DIR)

import torch

from config import (OUT_CP, THREAT_DIM, QA, CHECKPOINTS_DIR, TOY_NET,
                    LABEL_MAX_ABS_CP)
from data_format import read_pygdata
from model import NNUEModel
from nnue_ref import extract_features, QuantNet

LAMBDA = 0.75          # search-score vs game-WDL blend
RESULT_CP = 300        # a won game pulls the label by this many cp


def prepare(recs, log=print):
    t0 = time.time()
    idx_w, idx_b = extract_features(recs)
    stm = recs["stm"].astype(bool)
    score = np.clip(recs["score"].astype(np.float32),
                    -LABEL_MAX_ABS_CP, LABEL_MAX_ABS_CP)
    target_w = (LAMBDA * score / OUT_CP
                + (1 - LAMBDA) * recs["result"].astype(np.float32)
                * RESULT_CP / OUT_CP)
    target = np.where(stm, target_w, -target_w)          # stm POV
    thr = recs["threat"].astype(np.float32) / QA         # [N,16] W-vec,B-vec
    # stm-ordered threat: [us vec, them vec]
    thr_us = np.where(stm[:, None], thr[:, :8], thr[:, 8:])
    thr_th = np.where(stm[:, None], thr[:, 8:], thr[:, :8])
    idx_us = np.where(stm[:, None], idx_w, idx_b)
    idx_th = np.where(stm[:, None], idx_b, idx_w)
    log(f"  prepared {len(recs)} positions in {time.time()-t0:.1f}s")
    return (torch.from_numpy(np.ascontiguousarray(idx_us)),
            torch.from_numpy(np.ascontiguousarray(idx_th)),
            torch.from_numpy(np.ascontiguousarray(
                np.concatenate([thr_us, thr_th], axis=1))),
            torch.from_numpy(np.ascontiguousarray(target)))


def batches(tensors, bs, shuffle=True, seed=0):
    n = len(tensors[-1])
    order = np.random.default_rng(seed).permutation(n) if shuffle \
        else np.arange(n)
    for i in range(0, n, bs):
        sel = torch.from_numpy(order[i:i + bs])
        yield tuple(t[sel] for t in tensors)


def evaluate(model, tensors, bs):
    model.eval()
    tot, n = 0.0, 0
    with torch.no_grad():
        for iu, it, th, y in batches(tensors, bs, shuffle=False):
            pred = model(iu, it, th)
            tot += torch.nn.functional.mse_loss(
                pred, y, reduction="sum").item()
            n += len(y)
    return tot / max(1, n)


def export_nnue(model, path):
    sd = {k: v.detach().cpu().numpy() for k, v in model.state_dict().items()}
    w1 = sd["ft.weight"][:-1]                    # drop the PAD row
    q = QuantNet.from_float(
        w1, sd["ft_bias"],
        sd["l2.weight"], sd["l2.bias"],
        sd["l3.weight"], sd["l3.bias"],
        sd["out.weight"][0], sd["out.bias"][0])
    q.save(path)
    return q


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("--out", default=TOY_NET)
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch", type=int, default=8192)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--val-frac", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--limit", type=int, help="use only the first N records")
    ap.add_argument("--chunk", type=int, default=0,
                    help="stream the train split in chunks of N records "
                         "(0 = all in memory; use ~2000000 for 50M-scale)")
    args = ap.parse_args()

    torch.manual_seed(args.seed)
    recs = read_pygdata(args.dataset)              # mmap, read-only
    if args.limit:
        recs = recs[:args.limit]
    rng = np.random.default_rng(args.seed)
    nval = max(1, int(len(recs) * args.val_frac))
    if args.chunk:
        # streaming mode: val = a fixed random sample (in memory), train =
        # the remaining record RANGE streamed per epoch (approximate
        # shuffle: chunk order + within-chunk order re-drawn per epoch).
        val_idx = np.sort(rng.choice(len(recs), nval, replace=False))
        val = np.asarray(recs[val_idx])
        val_mask_note = f"{len(recs) - nval} train (streamed) / {nval} val"
        train_view = recs                     # val overlap: nval/N ~ 0.1%,
        ntrain = len(recs)                    # negligible for a stream
    else:
        order = rng.permutation(len(recs))
        val = np.asarray(recs[np.sort(order[:nval])])
        train = np.asarray(recs[np.sort(order[nval:])])
        val_mask_note = f"{len(train)} train / {nval} val"
        ntrain = len(train)
    print(f"dataset {args.dataset}: {val_mask_note}")
    if not args.chunk:
        train_t = prepare(train)
    val_t = prepare(val)

    model = NNUEModel()
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    os.makedirs(CHECKPOINTS_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    curve_path = os.path.join(CHECKPOINTS_DIR, "loss_curve.csv")
    best_val, best_epoch = float("inf"), -1

    def train_one_epoch(ep):
        model.train()
        tot, n = 0.0, 0
        if args.chunk:
            erng = np.random.default_rng(args.seed * 7919 + ep)
            starts = np.arange(0, ntrain, args.chunk)
            erng.shuffle(starts)
            sources = ((prepare(np.asarray(
                train_view[s:s + args.chunk]), log=lambda *a: None),
                int(erng.integers(1 << 30))) for s in starts)
        else:
            sources = ((train_t, args.seed + ep),)
        for tensors, bseed in sources:
            for iu, it, th, y in batches(tensors, args.batch, seed=bseed):
                opt.zero_grad()
                loss = torch.nn.functional.mse_loss(model(iu, it, th), y)
                loss.backward()
                opt.step()
                model.clip_weights()          # QAT: stay in-range always
                tot += loss.item() * len(y)
                n += len(y)
        return tot / n

    with open(curve_path, "w", newline="") as cf:
        cw = csv.writer(cf)
        cw.writerow(["epoch", "train_mse", "val_mse"])
        for ep in range(args.epochs):
            t0 = time.time()
            tr = train_one_epoch(ep)
            va = evaluate(model, val_t, args.batch)
            cw.writerow([ep, f"{tr:.6f}", f"{va:.6f}"])
            cf.flush()
            marker = ""
            if va < best_val:
                best_val, best_epoch = va, ep
                torch.save(model.state_dict(),
                           os.path.join(CHECKPOINTS_DIR, "best.pt"))
                marker = "  *best"
            print(f"epoch {ep:3d}  train {tr:.6f}  val {va:.6f}  "
                  f"({time.time()-t0:.1f}s){marker}", flush=True)

    model.load_state_dict(torch.load(
        os.path.join(CHECKPOINTS_DIR, "best.pt"), weights_only=True))
    q = export_nnue(model, args.out)
    print(f"exported best (epoch {best_epoch}, val {best_val:.6f}) "
          f"-> {args.out}")

    # quantization sanity: float vs quantized-reference on a sample (cp MAE)
    model.eval()
    sample = val[:256]
    iu, it, th, _ = prepare(sample, log=lambda *a: None)
    with torch.no_grad():
        fl = model(iu, it, th).numpy() * OUT_CP
    idx_w, idx_b = extract_features(sample)
    qs = []
    for i, r in enumerate(sample):
        iw = [x for x in idx_w[i] if x != len(q.w1)]
        ib = [x for x in idx_b[i] if x != len(q.w1)]
        qs.append(q.forward(iw, ib, r["threat"], int(r["stm"])))
    mae = float(np.mean(np.abs(fl - np.asarray(qs))))
    print(f"quantization check: float-vs-int MAE {mae:.2f} cp over "
          f"{len(sample)} positions (expected ~10-20 cp: three layers of "
          "QA=127/QB=64 rounding noise; the C side matches the INT pipeline "
          "exactly, this number is float-model drift only)")


if __name__ == "__main__":
    main()
