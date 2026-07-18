"""NNUE/data_format.py -- .pygdata training-data format v1 (DESIGN_nnue.md).

Header (32 bytes): magic "PYGNDATA", u32 version, u32 record_size(=88),
u64 count, u32 threat_ver, u32 reserved. Then `count` fixed 88-byte records
(RECORD_DTYPE below) -- mmap-able, seekable, trivially concatenatable.

API:
    write_pygdata(path, records)         records = numpy array of RECORD_DTYPE
    read_pygdata(path) -> numpy array    (mmap, read-only)
    append-merge: merge_pygdata(out, [shard, ...])
"""

import os
import struct
import numpy as np

from config import DATA_MAGIC, DATA_VERSION, RECORD_SIZE, THREAT_VER

RECORD_DTYPE = np.dtype([
    ("pawns", "<u8"), ("knights", "<u8"), ("bishops", "<u8"),
    ("rooks", "<u8"), ("queens", "<u8"), ("kings", "<u8"),
    ("occ_w", "<u8"), ("castling", "<u8"),
    ("score", "<i2"),        # search label, White POV cp (CYCLE_DETECT=0)
    ("result", "i1"),        # game WDL, White POV: +1/0/-1
    ("stm", "u1"),           # 1 = White to move
    ("ep", "i1"),            # -1 or square
    ("hmc", "u1"),
    ("threat", "u1", (16,)),  # T16 bytes: White vec then Black vec
    ("flags", "u1"), ("pad", "u1"),
])
assert RECORD_DTYPE.itemsize == RECORD_SIZE

_HDR = struct.Struct("<8sIIQII")        # magic, ver, recsize, count, tver, rsvd
HEADER_SIZE = _HDR.size
assert HEADER_SIZE == 32


def write_pygdata(path, records):
    records = np.ascontiguousarray(records, dtype=RECORD_DTYPE)
    with open(path, "wb") as f:
        f.write(_HDR.pack(DATA_MAGIC, DATA_VERSION, RECORD_SIZE,
                          len(records), THREAT_VER, 0))
        f.write(records.tobytes())


def read_pygdata(path):
    with open(path, "rb") as f:
        magic, ver, rsize, count, tver, _ = _HDR.unpack(f.read(HEADER_SIZE))
    if magic != DATA_MAGIC:
        raise ValueError(f"{path}: bad magic {magic!r}")
    if ver != DATA_VERSION or rsize != RECORD_SIZE:
        raise ValueError(f"{path}: version/record-size mismatch ({ver}/{rsize})")
    if tver != THREAT_VER:
        raise ValueError(f"{path}: threat_ver {tver} != {THREAT_VER} -- "
                         "regenerate (T16 formulas changed)")
    data = np.memmap(path, dtype=RECORD_DTYPE, mode="r",
                     offset=HEADER_SIZE, shape=(count,))
    return data


def merge_pygdata(out_path, shard_paths):
    total = 0
    parts = [read_pygdata(p) for p in shard_paths]
    total = sum(len(p) for p in parts)
    with open(out_path, "wb") as f:
        f.write(_HDR.pack(DATA_MAGIC, DATA_VERSION, RECORD_SIZE,
                          total, THREAT_VER, 0))
        for p in parts:
            f.write(np.asarray(p).tobytes())
    return total


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 4 and sys.argv[1] == "merge":
        # python3 NNUE/data_format.py merge OUT IN1 IN2 [...]
        n = merge_pygdata(sys.argv[2], sys.argv[3:])
        print(f"merged {len(sys.argv) - 3} files -> {sys.argv[2]} "
              f"({n:,} records)")
        raise SystemExit(0)
    # ponytail: smallest runnable check
    import tempfile
    r = np.zeros(3, dtype=RECORD_DTYPE)
    r["score"] = [10, -20, 30]
    r["threat"][1] = np.arange(16)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "t.pygdata")
        write_pygdata(p, r)
        back = read_pygdata(p)
        assert len(back) == 3 and back["score"][1] == -20
        assert list(back["threat"][1]) == list(range(16))
        p2 = os.path.join(d, "m.pygdata")
        assert merge_pygdata(p2, [p, p]) == 6
        assert read_pygdata(p2)["score"][4] == -20
    print("data_format self-check OK")
