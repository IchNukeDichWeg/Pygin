"""
texel.py
========
Local Texel tuning of the **live C eval**, on this project's own self-play
game results. Two stages, one file:

    python3 texel.py extract                 # logs -> texel_positions.npy
    python3 texel.py tune                    # tune -> engine_tuned.py
    python3 texel.py selftest                # 10-second sanity check

Why this exists next to tune.py
-------------------------------
``tune.py`` (the 2026-06 Python-era tuner) fits ``engine.py``'s *Python*
eval against *Stockfish depth-12 scores* from ``score_positions.py``. Both
halves are the wrong instrument now:

  * fitting to SF scores makes a better SF clone, not a better Pygin -- the
    textbook Texel label is the **game result**, and this project has ~10 GB
    of its own near-equal self-play logs sitting in "New logs/";
  * the Python eval is a mirror of the C eval, not the thing that ships, and
    it is ~400x slower to call.

So this file keeps tune.py's coordinate descent and write-back (imported,
not copied) and swaps both halves: labels come from game results, and the
loss calls ``csearch_eval_white`` in csearch.so -- the exact function the
engine evaluates leaves with, at ~620k evals/s/core.

Nothing here touches the live engine
------------------------------------
No C is rebuilt, no ``.so`` is written, ``engine.py`` is never modified (the
tuner refuses ``--out engine.py``). Workers mutate only their own process's
eval globals. The output is a standalone ``engine_tuned.py``, which is a
CANDIDATE -- a tuned eval is worth exactly what an A/B says it is worth, and
it changes the eval, so it also invalidates the CE_LADDER node pins and the
bench signature. Ship it like any eval change: re-pin, new SUBSET_SEED era.

What gets tuned (44 params, no PST)
-----------------------------------
PST is excluded on purpose -- per-square signal is too thin for coordinate
descent (tune.py:36 says the same, and it is why ``--pst`` is off by
default there). What is tuned instead is every *live* scalar eval term:

    MG_VALUES / EG_VALUES  P N B R Q            10   material, tapered
    PASSED_PAWN_MG / _EG   ranks 1-6            12   passers, tapered
    DOUBLED / ISOLATED / BACKWARD_PAWN           3   pawn structure
    BISHOP_PAIR_MG / _EG                         2
    ROOK_OPEN_FILE / ROOK_SEMIOPEN_FILE          2
    ROOK_ON_7TH_MG / _EG                         2
    THREAT_PAWN / THREAT_MINOR                   2
    MOBILITY_WEIGHT        N B R Q               4
    KING_RING_ATTACK / SHIELD / OPEN_FILE MG+EG  6
    TEMPO                                        1

Terms whose toggle is OFF in the shipping config (outpost, space, phalanx,
storm, king-shelter, simplify) are deliberately absent: tuning a weight that
is multiplied by zero fits noise. MOPUP_* and PHASE_MAX are structural, not
weights, and stay fixed.
"""

import argparse
import ctypes
import glob
import math
import multiprocessing as mp
import os
import random
import sys
import time
import zlib

import chess
import numpy as np

# ====================================================================== #
#  CONFIG
# ====================================================================== #
POSITIONS_NPY   = "texel_positions.npy"
DEFAULT_OUT     = "engine_tuned.py"
SKIP_PLIES      = 6        # book positions repeat across games -- drop them
MAX_PER_GAME    = 12       # evenly spaced, to decorrelate within a game
TARGET_POSITIONS = 1_000_000
DEFAULT_ROUNDS  = 12
DEFAULT_DELTAS  = (8, 3, 1)  # coordinate-descent step schedule (cp)
# Bound every parameter to +/-BOUND_FRAC of its shipped value (never tighter
# than BOUND_MIN cp). One rule instead of a hand-kept table that goes stale
# -- tune.py's PASSED_BOUNDS (0,60) already excludes the shipped 105.
BOUND_FRAC      = 0.60
BOUND_MIN       = 10
# ====================================================================== #

RECORD = np.dtype([
    ("bb",   "<u8", (8,)),   # pawns knights bishops rooks queens kings occW occB
    ("cast", "<u8"),
    ("turn", "u1"),          # 1 = White to move
    ("ep",   "i1"),          # -1 or square
    ("res",  "u1"),          # White-POV game result: 0 loss, 1 draw, 2 win
])


# ====================================================================== #
#  Parameter table
# ====================================================================== #
def _piece_params(attr):
    return [(f"{attr}[{chess.piece_name(pt)}]", attr, pt)
            for pt in (chess.PAWN, chess.KNIGHT, chess.BISHOP,
                       chess.ROOK, chess.QUEEN)]


PARAMS = (
    _piece_params("MG_VALUES") + _piece_params("EG_VALUES")
    + [(f"PASSED_PAWN_MG[{i}]", "PASSED_PAWN_MG", i) for i in range(1, 7)]
    + [(f"PASSED_PAWN_EG[{i}]", "PASSED_PAWN_EG", i) for i in range(1, 7)]
    + [(n, n, None) for n in (
        "DOUBLED_PAWN", "ISOLATED_PAWN", "BACKWARD_PAWN",
        "BISHOP_PAIR_MG", "BISHOP_PAIR_EG",
        "ROOK_OPEN_FILE", "ROOK_SEMIOPEN_FILE",
        "ROOK_ON_7TH_MG", "ROOK_ON_7TH_EG",
        "THREAT_PAWN", "THREAT_MINOR",
        "KING_RING_ATTACK_MG", "KING_RING_ATTACK_EG",
        "KING_SHIELD_MG", "KING_SHIELD_EG",
        "KING_OPEN_FILE_MG", "KING_OPEN_FILE_EG",
        "TEMPO")]
    + [(f"MOBILITY_WEIGHT[{chess.piece_name(pt)}]", "MOBILITY_WEIGHT", pt)
       for pt in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)]
)


def _get(eng, attr, key):
    v = getattr(eng, attr)
    return v if key is None else v[key]


def _set(eng, attr, key, val):
    if key is None:
        setattr(eng, attr, val)
    else:
        getattr(eng, attr)[key] = val


_PASSED_IDX = [[i for i, (_, a, _k) in enumerate(PARAMS) if a == attr]
               for attr in ("PASSED_PAWN_MG", "PASSED_PAWN_EG")]


def _valid(vec):
    """A passed pawn must never be worth less than one a rank behind it.
    Unconstrained coordinate descent happily produces a spiky ramp
    ([0,18,8,13,...] on a 40k smoke set) -- the same incoherent-table failure
    that keeps PST out of the tuner (tune.py:36). Cheap to forbid, and a
    non-monotonic passer ramp is not a fit worth A/B-ing."""
    for idxs in _PASSED_IDX:
        vals = [vec[i] for i in idxs]
        if any(nxt < prv for prv, nxt in zip(vals, vals[1:])):
            return False
    return True


def baseline_vector(eng):
    return [_get(eng, a, k) for _, a, k in PARAMS]


def bounds_for(vec):
    """+/-BOUND_FRAC around the shipped value, floor 0 (no negative weights);
    mobility weights are small ints, so they get an absolute +/-4 instead."""
    out = []
    for (label, _, _), v in zip(PARAMS, vec):
        if label.startswith("MOBILITY_WEIGHT"):
            out.append((max(0, v - 4), v + 4))
        else:
            span = max(BOUND_MIN, int(abs(v) * BOUND_FRAC))
            out.append((max(0, v - span), v + span))
    return out


# ====================================================================== #
#  Stage 1: extraction  (logs -> positions + White-POV game result)
# ====================================================================== #
def _quiet_positions(block, max_per_game):
    """Quiet positions from one '=== Game N ===' block, with the game's
    White-POV result. Quiet = side to move not in check and the move that
    reached the position was neither a capture nor a promotion (the standard
    Texel filter, same one score_positions.py applies)."""
    import fit_wdl_model as F

    fen_m, res_m = F.FEN_RE.search(block), F.RESULT_RE.search(block)
    if not (fen_m and res_m):
        return []
    res = {"1-0": 2, "1/2-1/2": 1, "0-1": 0}.get(res_m.group(1))
    if res is None:
        return []
    try:
        board = chess.Board(fen_m.group(1).strip())
    except ValueError:
        return []

    quiet = []
    for ply, line in enumerate(
            m for m in (F.MOVE_RE.match(l.strip()) for l in block.splitlines())
            if m):
        try:
            mv = board.parse_san(line.group("san"))
        except ValueError:
            break                      # replay desynced; rest of game unusable
        noisy = board.is_capture(mv) or mv.promotion
        board.push(mv)
        if ply >= SKIP_PLIES and not noisy and not board.is_check():
            quiet.append((board.pawns, board.knights, board.bishops,
                          board.rooks, board.queens, board.kings,
                          board.occupied_co[chess.WHITE],
                          board.occupied_co[chess.BLACK],
                          board.castling_rights,
                          1 if board.turn == chess.WHITE else 0,
                          -1 if board.ep_square is None else board.ep_square,
                          res))
    if len(quiet) > max_per_game:      # evenly spaced, not the first N
        step = len(quiet) / max_per_game
        quiet = [quiet[int(i * step)] for i in range(max_per_game)]
    return quiet


def _extract_file(args):
    path, max_per_game, per_file_cap = args
    import fit_wdl_model as F
    if not F.classify_file(path):
        return path, []
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except OSError:
        return path, []
    rows = []
    for block in F.iter_game_blocks(text):
        rows.extend(_quiet_positions(block, max_per_game))
    # One match log holds ~20k games = ~240k quiet positions, so an uncapped
    # 1M target is met by four or five files -- i.e. four or five engine
    # pairings. Cap per file so the fit spans eras instead.
    if per_file_cap and len(rows) > per_file_cap:
        # crc32, not hash(): str hashing is salted per process, and workers
        # are spawned, so hash() would make extraction unreproducible.
        seed = zlib.crc32(os.path.basename(path).encode())
        rows = random.Random(seed).sample(rows, per_file_cap)
    return path, rows


def cmd_extract(a):
    import fit_wdl_model as F
    files = []
    for d in F.LOG_DIRS:
        files.extend(sorted(glob.glob(os.path.join(d, "*.txt"))))
    if not files:
        sys.exit(f"no logs found under {F.LOG_DIRS}")
    random.Random(52).shuffle(files)   # spread eras when --limit cuts short
    cap = a.per_file_cap or max(1, a.limit // 20)
    print(f"Scanning {len(files):,} log files with {a.workers} workers "
          f"(target {a.limit:,} positions, <= {cap:,} per match log)...")

    rows, used, t0 = [], 0, time.time()
    with mp.Pool(a.workers) as pool:
        it = pool.imap_unordered(_extract_file,
                                 ((f, a.max_per_game, cap) for f in files))
        for i, (path, got) in enumerate(it, 1):
            if got:
                used += 1
                rows.extend(got)
            if i % 25 == 0 or len(rows) >= a.limit:
                print(f"  {i:>4}/{len(files)} files  {used} usable  "
                      f"{len(rows):,} positions  "
                      f"[{time.time() - t0:.0f}s]")
            if len(rows) >= a.limit:
                pool.terminate()
                break

    if not rows:
        sys.exit("no usable positions -- every log was filtered out "
                 "(odds / mismatched-strength / Stockfish logs are excluded)")
    random.Random(52).shuffle(rows)
    rows = rows[:a.limit]
    arr = np.zeros(len(rows), dtype=RECORD)
    for i, r in enumerate(rows):
        arr[i]["bb"] = r[:8]
        arr[i]["cast"], arr[i]["turn"], arr[i]["ep"], arr[i]["res"] = r[8:]
    np.save(a.out, arr)
    w = int((arr["res"] == 2).sum()); d = int((arr["res"] == 1).sum())
    print(f"\nWrote {len(arr):,} positions to {a.out} "
          f"({arr.nbytes / 1e6:.0f} MB, {time.time() - t0:.0f}s)")
    print(f"  White-POV labels: {w:,} win / {d:,} draw / "
          f"{len(arr) - w - d:,} loss  "
          f"(White scores {(w + d / 2) / len(arr) * 100:.1f}%)")
    # ~62%, not ~50%: the UHO books are UNBALANCED by design, so White starts
    # better in every game. Colours swap per opening pair so engine-vs-engine
    # stays fair, but White-vs-Black does not. The eval can see those
    # imbalances, so this is fittable rather than broken -- TEMPO is the one
    # parameter that would absorb any residue it CAN'T see, so if TEMPO ends
    # pinned to its upper bound, distrust the fit.


# ====================================================================== #
#  Stage 2: tuning  (C eval + coordinate descent)
# ====================================================================== #
_W = {}          # per-worker state


def _worker_init(npy_path, nchunks):
    import engine as E
    lib = ctypes.CDLL("./csearch.so")
    lib.csearch_eval_white.restype = ctypes.c_int
    lib.csearch_eval_white.argtypes = [ctypes.c_uint64] * 8 + \
        [ctypes.c_int, ctypes.c_int, ctypes.c_uint64]
    _W.update(lib=lib, E=E, eng=E.Engine(), arr=np.load(npy_path, mmap_mode="r"),
              nchunks=nchunks, cid=-1, cols=None, res=None)
    _push(_W["eng"], lib, E)


_ORDER = [chess.PAWN, chess.KNIGHT, chess.BISHOP,
          chess.ROOK, chess.QUEEN, chess.KING]


def _push(eng, lib, E):
    """Mirror cengine.py's eval-sync block: engine.py's attributes are the
    single source of truth for the C eval (cengine.py:940 calls the Python
    Engine 'the eval-param oracle'), so pushing them is what makes a tuned
    value live. _sync_c_params is retargeted at csearch.so's own copy of the
    eval_c globals exactly as cengine.py:1111 does."""
    orig = E._eval_lib
    E._eval_lib = lib
    try:
        eng._sync_c_params()
    finally:
        E._eval_lib = orig
    IA = lambda s: (ctypes.c_int * len(s))(*s)
    lib.csearch_set_eval(
        IA([v for pt in _ORDER for v in eng.mg_tables[pt]]),
        IA([v for pt in _ORDER for v in eng.eg_tables[pt]]),
        IA([0] + [eng.MG_VALUES[pt] for pt in _ORDER]),
        IA([0] + [eng.EG_VALUES[pt] for pt in _ORDER]),
        IA([0] + [eng.PHASE_WEIGHTS[pt] for pt in _ORDER]),
        eng.TEMPO, eng.DOUBLED_PAWN, eng.ISOLATED_PAWN, eng.BACKWARD_PAWN,
        IA(eng.PASSED_PAWN_MG), IA(eng.PASSED_PAWN_EG),
        eng.MOPUP_MIN_ADV, eng.MOPUP_STRONG_CMD_WEIGHT,
        eng.MOPUP_STRONG_KING_WEIGHT)


def _chunk(cid):
    """Columns of chunk `cid` as plain Python int lists -- ~4x faster in the
    eval loop than indexing the numpy record, and only one chunk is held at a
    time so 10 workers never materialise the whole set."""
    if _W["cid"] != cid:
        arr = _W["arr"]
        lo = len(arr) * cid // _W["nchunks"]
        hi = len(arr) * (cid + 1) // _W["nchunks"]
        a = arr[lo:hi]
        bb = a["bb"]
        _W["cols"] = list(zip(*[bb[:, i].tolist() for i in range(8)],
                              a["turn"].tolist(), a["ep"].tolist(),
                              a["cast"].tolist(),
                              [r / 2.0 for r in a["res"].tolist()]))
        _W["cid"] = cid
    return _W["cols"]


def _loss_task(job):
    """(sum of squared error, n) over one chunk for one parameter vector."""
    cid, vec, k = job
    if vec is not None:
        eng = _W["eng"]
        for (_, attr, key), v in zip(PARAMS, vec):
            _set(eng, attr, key, v)
        _push(eng, _W["lib"], _W["E"])
    f = _W["lib"].csearch_eval_white
    c = -k * math.log(10.0) / 400.0
    exp = math.exp
    s = 0.0
    for p, n, b, r, q, kg, ow, ob, turn, ep, cast, res in _chunk(cid):
        d = res - 1.0 / (1.0 + exp(c * f(p, n, b, r, q, kg, ow, ob,
                                         turn, ep, cast)))
        s += d * d
    return s, len(_W["cols"])


def _loss(pool, nchunks, vec, k):
    out = pool.map(_loss_task, [(c, vec, k) for c in range(nchunks)],
                   chunksize=1)
    tot = sum(s for s, _ in out)
    n = sum(m for _, m in out)
    return tot / n


def cmd_tune(a):
    if os.path.basename(a.out) in ("engine.py", "cengine.py"):
        sys.exit("refusing to overwrite the live engine -- pick another --out")
    if not os.path.exists(a.positions):
        sys.exit(f"{a.positions} not found -- run: python3 texel.py extract")

    import engine as E
    eng = E.Engine()
    base = baseline_vector(eng)
    bounds = bounds_for(base)
    arr = np.load(a.positions, mmap_mode="r")
    nchunks = a.workers
    print(f"{len(arr):,} positions, {len(PARAMS)} parameters, "
          f"{a.workers} workers")

    with mp.Pool(a.workers, initializer=_worker_init,
                 initargs=(a.positions, nchunks)) as pool:
        # --- fit K (the cp -> win-probability scale) on the shipped eval --- #
        t0 = time.time()
        best_k, best = None, None
        for k in [round(0.6 + 0.1 * i, 1) for i in range(20)]:
            L = _loss(pool, nchunks, base if best_k is None else None, k)
            if best is None or L < best:
                best, best_k = L, k
        k = best_k
        print(f"K = {k} (loss {best:.6f}, {time.time() - t0:.0f}s)\n")

        cur = list(base)
        cur_loss = best
        start_loss = best
        for rnd in range(1, a.rounds + 1):
            # Anneal the step: the delta schedule is spread evenly over the
            # rounds (12 rounds, 3 deltas -> 4 coarse, 4 medium, 4 fine)
            # rather than one delta per round with the rest at the finest.
            delta = a.deltas[min((rnd - 1) * len(a.deltas) // a.rounds,
                                 len(a.deltas) - 1)]
            t0, changed = time.time(), 0
            order = list(range(len(PARAMS)))
            random.Random(rnd).shuffle(order)
            for idx in order:
                lo, hi = bounds[idx]
                for step in (delta, -delta):
                    trial = list(cur)
                    v = trial[idx] + step
                    if not (lo <= v <= hi) or v == cur[idx]:
                        continue
                    trial[idx] = v
                    if not _valid(trial):
                        continue
                    L = _loss(pool, nchunks, trial, k)
                    if L < cur_loss:
                        cur, cur_loss, changed = trial, L, changed + 1
                        break
            print(f"round {rnd:>2}/{a.rounds}  delta {delta:>2}cp  "
                  f"loss {cur_loss:.6f}  ({changed} params moved, "
                  f"{time.time() - t0:.0f}s)")
            if not changed:
                print("  converged at this step size")
                if delta == min(a.deltas):
                    break

    print(f"\nloss {start_loss:.6f} -> {cur_loss:.6f}  "
          f"({(start_loss - cur_loss) / start_loss * 100:.2f}% better)")
    print("\nchanged parameters:")
    for (label, _, _), b0, b1 in zip(PARAMS, base, cur):
        if b0 != b1:
            print(f"  {label:<28} {b0:>6} -> {b1:>6}  ({b1 - b0:+d})")

    for (_, attr, key), v in zip(PARAMS, cur):
        _set(E.Engine, attr, key, v)
    _write_back(a.out)
    print(f"\nWrote {a.out}. This is a CANDIDATE, not a release: A/B it "
          f"against Old Engine/52 before shipping,\nand remember an eval "
          f"change re-pins CE_LADDER and moves the bench signature.")


def _write_back(dst):
    """Patch the tuned constants into a copy of engine.py. Reuses tune.py's
    line-rewriting helpers so there is one implementation of the fiddly part;
    PST blocks are never touched."""
    import shutil
    import re
    from tune import _replace_dict_block
    E = __import__("engine").Engine

    def _replace_scalar(lines, name, value):
        """Like tune.py's helper, but also matches the `self.NAME = 35` form:
        THREAT_PAWN / THREAT_MINOR are set in __init__, not at class level,
        and tune.py's class-level-only regex silently warns past them."""
        for pat in (rf'^(\s+{re.escape(name)}\s*=\s*)-?\d+',
                    rf'^(\s+self\.{re.escape(name)}\s*=\s*)-?\d+'):
            p = re.compile(pat)
            for i, line in enumerate(lines):
                m = p.match(line)
                if m:
                    lines[i] = m.group(1) + str(round(value))
                    return
        sys.exit(f"write-back: {name} not found in engine.py -- refusing to "
                 f"write a partially-tuned file")

    shutil.copy("engine.py", dst)
    with open(dst) as f:
        lines = f.read().splitlines()

    for name in ("MG_VALUES", "EG_VALUES"):
        v = getattr(E, name)
        _replace_dict_block(lines, name, [
            f"    {name} = {{",
            f"        chess.PAWN: {v[chess.PAWN]}, chess.KNIGHT: "
            f"{v[chess.KNIGHT]}, chess.BISHOP: {v[chess.BISHOP]},",
            f"        chess.ROOK: {v[chess.ROOK]}, chess.QUEEN: "
            f"{v[chess.QUEEN]}, chess.KING: 0,",
            "    }",
        ])
    mob = E.MOBILITY_WEIGHT
    _replace_dict_block(lines, "MOBILITY_WEIGHT", [
        "    MOBILITY_WEIGHT = {",
        f"        chess.KNIGHT: {mob[chess.KNIGHT]}, chess.BISHOP: "
        f"{mob[chess.BISHOP]}, chess.ROOK: {mob[chess.ROOK]}, "
        f"chess.QUEEN: {mob[chess.QUEEN]},",
        "    }",
    ])
    for attr in ("PASSED_PAWN_MG", "PASSED_PAWN_EG"):
        pat = re.compile(r'^(\s+' + attr + r'\s*=\s*)\[.*\]')
        for i, line in enumerate(lines):
            m = pat.match(line)
            if m:
                lines[i] = m.group(1) + str(list(getattr(E, attr)))
                break
    for label, attr, key in PARAMS:
        if key is None:
            _replace_scalar(lines, attr, getattr(E, attr))

    with open(dst, "w") as f:
        f.write("\n".join(lines) + "\n")


# ====================================================================== #
#  selftest -- the one runnable check
# ====================================================================== #
def cmd_selftest(a):
    import engine as E
    lib = ctypes.CDLL("./csearch.so")
    lib.csearch_eval_white.restype = ctypes.c_int
    lib.csearch_eval_white.argtypes = [ctypes.c_uint64] * 8 + \
        [ctypes.c_int, ctypes.c_int, ctypes.c_uint64]
    eng = E.Engine()
    _push(eng, lib, E)

    def cev(b):
        return lib.csearch_eval_white(
            b.pawns, b.knights, b.bishops, b.rooks, b.queens, b.kings,
            b.occupied_co[chess.WHITE], b.occupied_co[chess.BLACK],
            1 if b.turn == chess.WHITE else 0,
            -1 if b.ep_square is None else b.ep_square, b.castling_rights)

    # A probe set from seeded random playouts. Hand-picked FENs are a trap
    # here: a symmetric position cancels every material term and a full-phase
    # one zeroes every EG term, so a dead parameter reads as live. Random
    # play breaks both symmetries and spans the whole phase range.
    boards, rng = [], random.Random(52)
    for _ in range(40):
        b = chess.Board()
        for _ in range(rng.randint(4, 90)):
            moves = list(b.legal_moves)
            if not moves:
                break
            b.push(rng.choice(moves))
            if not b.is_check() and rng.random() < 0.25:
                boards.append(b.copy())
    # Motifs random play effectively never produces. Rook-on-7th needs a rook
    # on rank 7 AND the enemy king on rank 8 (or an enemy pawn on rank 7) --
    # eval_c.c:471 -- which random kings wander out of. Material must also
    # stay within MOPUP_MIN_ADV (400): a side up a whole rook triggers the
    # mop-up shortcut, which REPLACES every positional term (csearch.c:930).
    boards += [chess.Board(f) for f in (
        "4k3/1R3ppp/8/8/8/8/5PPP/r3K3 w - - 0 1",
        "r2qk3/1R3ppp/2n5/8/8/2N5/5PPP/3QK3 w - - 0 1",
    )]
    phases = [min(24, chess.popcount(b.knights | b.bishops)
                  + 2 * chess.popcount(b.rooks)
                  + 4 * chess.popcount(b.queens)) for b in boards]

    # 1. the C eval the tuner optimises IS engine.py's eval (the mirror).
    #    Both are White-perspective (engine.py:2359, csearch.c:1146) -- do NOT
    #    flip for side to move; that only looks right on White-to-move probes.
    for b in boards:
        py = eng._evaluate_static(b)
        assert cev(b) == py, \
            f"eval mirror broken on {b.fen()}: {cev(b)} != {py}"

    # 2. every tuned parameter actually reaches the C eval, on at least one
    #    probe position, and every push is exactly reversible.
    befores = [cev(b) for b in boards]
    dead = []
    for label, attr, key in PARAMS:
        _set(eng, attr, key, _get(eng, attr, key) + 64)
        _push(eng, lib, E)
        moved = any(cev(b) != v for b, v in zip(boards, befores))
        _set(eng, attr, key, _get(eng, attr, key) - 64)
        _push(eng, lib, E)
        assert all(cev(b) == v for b, v in zip(boards, befores)), \
            f"{label}: push is not reversible"
        if not moved:
            dead.append(label)
    assert not dead, ("these parameters never reach the C eval (a dead term "
                      f"would be fit to noise): {dead}")
    print(f"selftest OK -- eval mirror exact on {len(boards)} positions "
          f"(phase {min(phases)}-{max(phases)}), "
          f"all {len(PARAMS)} parameters live and reversible")


# ====================================================================== #
def main():
    ap = argparse.ArgumentParser(
        description="Texel-tune the live C eval on self-play game results")
    sub = ap.add_subparsers(dest="cmd", required=True)
    cores = mp.cpu_count()

    e = sub.add_parser("extract", help="logs -> positions .npy")
    e.add_argument("--out", default=POSITIONS_NPY)
    e.add_argument("--workers", type=int, default=cores)
    e.add_argument("--limit", type=int, default=TARGET_POSITIONS)
    e.add_argument("--max-per-game", type=int, default=MAX_PER_GAME)
    e.add_argument("--per-file-cap", type=int, default=0,
                   help="max positions from one match log (default: limit/20, "
                        "so at least 20 different pairings contribute)")
    e.set_defaults(fn=cmd_extract)

    t = sub.add_parser("tune", help="coordinate descent -> engine_tuned.py")
    t.add_argument("--positions", default=POSITIONS_NPY)
    t.add_argument("--out", default=DEFAULT_OUT)
    t.add_argument("--workers", type=int, default=cores)
    t.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    t.add_argument("--deltas", type=int, nargs="+", default=list(DEFAULT_DELTAS),
                   help="coordinate-descent step sizes in cp, annealed evenly "
                        "across --rounds (default: 8 3 1)")
    t.set_defaults(fn=cmd_tune)

    s = sub.add_parser("selftest", help="verify the eval mirror + param push")
    s.set_defaults(fn=cmd_selftest)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
