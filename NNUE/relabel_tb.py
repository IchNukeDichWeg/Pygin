#!/usr/bin/env python3
"""NNUE/relabel_tb.py -- replace search labels with tablebase truth.

    python3 NNUE/relabel_tb.py in.pygdata out.pygdata --syzygy syzygy

Sweeps a corpus for records inside the Syzygy set (<=5 pieces) and rewrites
BOTH label columns from the tablebase. Everything outside the set is copied
through byte-for-byte.

WHY BOTH COLUMNS. The training target is
    LAMBDA * cp/OUT_CP + (1 - LAMBDA) * result * RESULT_CP/OUT_CP
so fixing only `result` leaves LAMBDA (0.75 by default) of the target still
carrying the wrong number. A position the tablebase calls drawn gets cp 0 AND
result 0; a win gets +WIN_CP and +1.

WHY IT IS WORTH DOING. Measured on gen_only 2026-08-23: 5.1% of records are
<=5 pieces, and **31% of those carry a label the tablebase contradicts** --
about 915,000 provably wrong labels in one corpus. They are not scattered
noise either; they are the R+B vs R and R+N vs R shapes eval_bench found the
net getting wrong at +400 to +475, straight out of the training data.

The gradation inside a won position is lost -- every tablebase win becomes the
same WIN_CP -- and that is deliberate. DTZ could rank them by difficulty, but
"won is won" is the label the net should learn, and DTZ is 561 MB of tables
that training has no other use for.
"""

import argparse
import os
import sys
import time

NNUE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, NNUE_DIR)

import chess
import numpy as np

from data_format import RECORD_DTYPE, HEADER_SIZE, read_pygdata, _HDR
from config import DATA_MAGIC, DATA_VERSION, RECORD_SIZE, THREAT_VER
from tablebase import Tablebase, add_arg, MAX_PIECES

CHUNK = 500_000

_PT = (("pawns", chess.PAWN), ("knights", chess.KNIGHT), ("bishops", chess.BISHOP),
       ("rooks", chess.ROOK), ("queens", chess.QUEEN), ("kings", chess.KING))


def board_of(rec):
    """Rebuild a chess.Board from the record's bitboards (no history)."""
    b = chess.Board(None)
    occ_w = int(rec["occ_w"])
    for name, pt in _PT:
        bb = int(rec[name])
        while bb:
            sq = (bb & -bb).bit_length() - 1
            b.set_piece_at(sq, chess.Piece(pt, bool(occ_w >> sq & 1)))
            bb &= bb - 1
    b.turn = chess.WHITE if rec["stm"] else chess.BLACK
    return b


def _fmt(sec):
    sec = int(max(0, sec))
    return (f"{sec//3600}h{(sec%3600)//60:02d}m" if sec >= 3600 else
            f"{sec//60}m{sec%60:02d}s" if sec >= 60 else f"{sec}s")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("dst")
    add_arg(ap, required=True)
    ap.add_argument("--win-cp", type=int, default=1000,
                    help="cp written for a tablebase WIN (loss gets its "
                         "negation, draw gets 0). Default %(default)s")
    args = ap.parse_args()

    if os.path.abspath(args.src) == os.path.abspath(args.dst):
        print("refusing to relabel in place -- pass a different output path")
        return 1

    tb = Tablebase(args.syzygy)          # raises loudly if the path is unusable
    print(f"tablebase: {tb.n_tables} WDL tables, <= {MAX_PIECES} pieces")

    src = read_pygdata(args.src)
    total = len(src)
    print(f"source: {args.src}  {total:,} records")

    n_small = n_probed = n_draw = n_win = n_loss = n_changed = 0
    t0 = time.time()
    tty = sys.stdout.isatty()
    last = 0

    with open(args.dst, "wb") as out:
        out.write(_HDR.pack(DATA_MAGIC, DATA_VERSION, RECORD_SIZE,
                            total, THREAT_VER, 0))
        for start in range(0, total, CHUNK):
            buf = np.array(src[start:start + CHUNK])       # writable copy
            occ = (buf["pawns"] | buf["knights"] | buf["bishops"]
                   | buf["rooks"] | buf["queens"] | buf["kings"])
            pc = np.zeros(len(buf), dtype=np.uint8)
            v = occ.copy()
            while v.any():
                pc += (v & 1).astype(np.uint8)
                v >>= np.uint64(1)
            for i in np.nonzero(pc <= MAX_PIECES)[0]:
                n_small += 1
                verdict = tb.probe(board_of(buf[i]))
                if verdict is None:
                    continue
                n_probed += 1
                cp = 0 if verdict == 0 else args.win_cp * verdict
                if (int(buf[i]["score"]), int(buf[i]["result"])) != (cp, verdict):
                    n_changed += 1
                buf[i]["score"] = cp
                buf[i]["result"] = verdict
                n_draw += verdict == 0
                n_win += verdict > 0
                n_loss += verdict < 0
            out.write(np.ascontiguousarray(buf).tobytes())

            done = min(start + CHUNK, total)
            el = time.time() - t0
            rate = done / el if el else 0
            eta = (total - done) / rate if rate else 0
            if tty:
                frac = done / total
                fill = int(36 * frac)
                sys.stdout.write(f"\r  [{'#'*fill}{'.'*(36-fill)}] {done:,}/{total:,} "
                                 f"{frac*100:5.1f}%  {rate/1000:,.0f}k/s  "
                                 f"elapsed {_fmt(el)}  ETA {_fmt(eta)}   ")
                sys.stdout.flush()
            elif done - last >= 5_000_000 or done == total:
                last = done
                print(f"  {done:,}/{total:,} ({done/total*100:.1f}%)  "
                      f"elapsed {_fmt(el)}  ETA {_fmt(eta)}", flush=True)
    if tty:
        sys.stdout.write("\r" + " " * 110 + "\r")

    tb.close()
    el = time.time() - t0
    print(f"wrote {args.dst}  ({total:,} records, {_fmt(el)})")
    print(f"  <= {MAX_PIECES} pieces      {n_small:>10,}  ({n_small/total*100:.2f}% of corpus)")
    print(f"  in the tablebase  {n_probed:>10,}  ({n_probed/max(1,n_small)*100:.1f}% of those)")
    print(f"  LABEL CHANGED     {n_changed:>10,}  ({n_changed/max(1,n_probed)*100:.1f}% of probed,"
          f" {n_changed/total*100:.2f}% of corpus)")
    print(f"    -> draw {n_draw:,}   win {n_win:,}   loss {n_loss:,}")
    if n_probed == 0:
        print("  !! nothing was probed -- that is a broken setup, not a clean corpus")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
