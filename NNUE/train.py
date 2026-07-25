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

NAMING: the exported file is renamed to carry the first 12 hex of its own
sha256 (config.stamp_net_hash), Stockfish-style -- pass
`--out NNUE/nets/nnue_v1.nnue` and you get `nnue_v1_52724f038139.nnue`.
The printed "exported ... -> PATH" line is the real name; use THAT in
--net flags and in cengine.NNUE_FILE. `toy.nnue` is exempt (not a version).

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
                    LABEL_MAX_ABS_CP, stamp_net_hash)
from data_format import read_pygdata
from model import NNUEModel
from nnue_ref import extract_features, QuantNet

VAL_BLOCK = 5000       # records per held-out block (see val_block_mask)
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


def val_block_mask(idx, stride, block):
    """True where record index `idx` belongs to the held-out set: every
    `stride`-th CONTIGUOUS BLOCK of `block` records.

    Why blocks and not `recs[::stride]`: gen_data writes each game's
    positions contiguously (~65 per game) and RECORD_DTYPE's `result` is the
    GAME's WDL, shared by every record of that game. Taking every 20th record
    therefore puts 3-4 positions of every game in val and the other ~62 in
    train -- measured 100.0% of val games also present in train. Adjacent
    plies differ by one move, so the model can read a val position's answer
    off its own siblings, and val stops measuring generalisation. It is
    record-disjoint but not GAME-disjoint, and the label is per-game.

    Contiguous blocks make the split game-granular: only the two games
    straddling each block boundary leak. Measured over the first 4M records
    at 5% held out -- every-20th-record 100.0%, 500-blocks 33.7%,
    2000-blocks 9.9%, 5000-blocks 3.9%, 20000-blocks 1.0%. 5000 is the
    default: near-clean, and still ~30 blocks inside the smallest merged
    slice (endgame_b, 3.0M) so val stays spread over all six sources."""
    return ((idx // block) % stride) == 0


def val_indices(n, stride, block):
    """The held-out indices, built without materialising arange(n) -- at 82M
    records that alone would be 660 MB."""
    return np.concatenate([np.arange(s, min(s + block, n))
                           for s in range(0, n, block * stride)])


def eval_net(args, recs):
    """Score an exported .nnue on the GAME-GRANULAR holdout and report.

    Scores the quantized net through nnue_ref's exact integer forward -- the
    artifact that actually ships, not the float model -- so the number needs
    no trust in the export. Reports against a predict-the-mean baseline,
    because an MSE in cp/400 units means nothing on its own."""
    from nnue_ref import QuantNet, extract_features
    stride = max(2, int(round(1.0 / max(args.val_frac, 1e-9))))
    idx = val_indices(len(recs), stride, args.val_block)
    if len(idx) > args.eval_sample:
        # linspace, NOT idx[::len//sample]: integer division collapses the
        # stride to 1 whenever the ratio is under 2, and the slice then just
        # truncates to the head of the file -- which is one slice
        # (random_a), not a sample of the merged set.
        idx = idx[np.linspace(0, len(idx) - 1,
                              args.eval_sample).astype(np.int64)]
    val = np.asarray(recs[idx])
    print(f"{args.eval_net}: scoring {len(val):,} held-out positions "
          f"(every {stride}th block of {args.val_block:,} records)",
          flush=True)

    q = QuantNet.load(args.eval_net)
    idx_w, idx_b = extract_features(val)
    target = prepare(val, log=lambda *a: None)[3].numpy()
    pad = len(q.w1)
    pred = np.empty(len(val), dtype=np.float32)
    for i in range(len(val)):
        pred[i] = q.forward([x for x in idx_w[i] if x != pad],
                            [x for x in idx_b[i] if x != pad],
                            val["threat"][i], int(val["stm"][i])) / OUT_CP
        if (i + 1) % 10_000 == 0:
            print(f"  {i + 1:,}/{len(val):,}", flush=True)

    mse = float(((pred - target) ** 2).mean())
    base = float(target.var())
    print(f"\n  held-out MSE   {mse:.6f}   RMSE {mse ** 0.5 * OUT_CP:6.1f} cp")
    print(f"  baseline       {base:.6f}   RMSE {base ** 0.5 * OUT_CP:6.1f} cp"
          f"   (predict the mean)")
    print(f"  R^2            {1 - mse / base:+.4f}")


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
    ap.add_argument("--export-only", action="store_true",
                    help="skip training: export checkpoints/best.pt to --out "
                         "and exit. Recovery for a run that died after some "
                         "epochs (there is no resume; best.pt is the only "
                         "thing that survives).")
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch", type=int, default=8192)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--val-frac", type=float, default=0.05)
    ap.add_argument("--val-block", type=int, default=VAL_BLOCK,
                    help="records per held-out block (default %d). Blocks, "
                         "not strided records, so the holdout is GAME-"
                         "granular -- see val_block_mask()." % VAL_BLOCK)
    ap.add_argument("--eval-net", metavar="NET",
                    help="score an exported .nnue on the held-out split and "
                         "exit -- no training. Gives the honest val number "
                         "for a net whose run used a leaky split.")
    ap.add_argument("--eval-sample", type=int, default=50_000,
                    help="positions to score in --eval-net (default 50,000; "
                         "the reference forward is one position at a time)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--limit", type=int, help="use only the first N records")
    ap.add_argument("--chunk", type=int, default=0,
                    help="stream the train split in chunks of N records "
                         "(0 = all in memory; use ~2000000 for 50M-scale)")
    args = ap.parse_args()

    torch.manual_seed(args.seed)
    if args.export_only:
        # Recovery path. best.pt is written every time val improves, but only
        # the export turns it into a .nnue -- so a run that dies at epoch 15
        # (crash, OOM, machine sleep, closed terminal) leaves hours of work on
        # disk in a form nothing can load. There is no resume, so without this
        # the only option is to train again from scratch.
        model = NNUEModel()
        ck = os.path.join(CHECKPOINTS_DIR, "best.pt")
        if not os.path.exists(ck):
            sys.exit(f"train --export-only: no {ck} to export")
        model.load_state_dict(torch.load(ck, weights_only=True))
        export_nnue(model, args.out)
        out = stamp_net_hash(args.out)
        print(f"exported checkpoints/best.pt (RECOVERED, epoch unknown -- "
              f"read checkpoints/loss_curve.csv for the val curve) -> {out}")
        return
    recs = read_pygdata(args.dataset)              # mmap, read-only
    if args.limit:
        recs = recs[:args.limit]
    if args.eval_net:
        eval_net(args, recs)
        return
    rng = np.random.default_rng(args.seed)
    val_stride = max(2, int(round(1.0 / max(args.val_frac, 1e-9))))
    val = np.asarray(recs[val_indices(len(recs), val_stride, args.val_block)])
    ntrain = len(recs) - len(val)
    val_mask_note = (f"{ntrain} train / {len(val)} val (every "
                     f"{val_stride}th block of {args.val_block:,} records, "
                     f"game-granular)")
    if args.chunk:
        train_view = recs
    else:
        keep = ~val_block_mask(np.arange(len(recs)), val_stride,
                               args.val_block)
        train = np.asarray(recs[keep])
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
            starts = np.arange(0, len(train_view), args.chunk)
            erng.shuffle(starts)

            def _chunk(st):
                sub = np.asarray(train_view[st:st + args.chunk])
                keep = ~val_block_mask(np.arange(st, st + len(sub)),
                                       val_stride, args.val_block)
                return prepare(sub[keep], log=lambda *a: None)

            sources = ((_chunk(st), int(erng.integers(1 << 30)))
                       for st in starts)
        else:
            sources = ((train_t, args.seed + ep),)
        # Per-chunk heartbeat: an epoch over 82M records is ~40 chunks and
        # many minutes, and prepare()'s own logging is suppressed in chunk
        # mode, so without this the run prints NOTHING between epoch lines --
        # indistinguishable from a hang on an overnight run. Plain lines, not
        # an in-place bar, so they survive redirection to a log.
        nchunk = len(starts) if args.chunk else 1
        t_ep = time.time()
        for ci, (tensors, bseed) in enumerate(sources, 1):
            for iu, it, th, y in batches(tensors, args.batch, seed=bseed):
                opt.zero_grad()
                loss = torch.nn.functional.mse_loss(model(iu, it, th), y)
                loss.backward()
                opt.step()
                model.clip_weights()          # QAT: stay in-range always
                tot += loss.item() * len(y)
                n += len(y)
            if nchunk > 1:
                el = time.time() - t_ep
                print(f"  epoch {ep:3d}  chunk {ci:3d}/{nchunk}  "
                      f"train {tot / n:.6f}  {el / 60:.1f}m elapsed  "
                      f"epoch ETA {el / ci * (nchunk - ci) / 60:.1f}m",
                      flush=True)
        return tot / n

    stopped = False
    with open(curve_path, "w", newline="") as cf:
        cw = csv.writer(cf)
        cw.writerow(["epoch", "train_mse", "val_mse"])
        try:
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
        except KeyboardInterrupt:
            # A full run is hours, so stopping early has to be a normal exit,
            # not a traceback: the export below is the ONLY thing that turns
            # best.pt into a usable .nnue, and skipping it would throw away
            # every completed epoch. Val loss flattens well before the epoch
            # budget, so "watch it and stop when it plateaus" is the expected
            # way to use this -- it must produce a net.
            stopped = True
            print(f"\ntraining interrupted after {best_epoch + 1} completed "
                  f"epoch(s) -- exporting the best one", flush=True)

    if best_epoch < 0:
        sys.exit("train: interrupted before the first epoch finished; no net "
                 "written (checkpoints/best.pt is from an earlier run and is "
                 "NOT this dataset's -- exporting it would be a silent lie)")
    model.load_state_dict(torch.load(
        os.path.join(CHECKPOINTS_DIR, "best.pt"), weights_only=True))
    q = export_nnue(model, args.out)
    args.out = stamp_net_hash(args.out)     # -> nnue_v1_<12 hex>.nnue
    print(f"exported best (epoch {best_epoch}, val {best_val:.6f}"
          f"{', STOPPED EARLY' if stopped else ''}) -> {args.out}")

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
