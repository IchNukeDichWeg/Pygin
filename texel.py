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
import re
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

import interruptible

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
    ("game", "<u4"),         # FB-43: per-game id -- lets the split cut on a
                             # GAME boundary so train and val share no game
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
        # FI-86: the EG twins. These are the NEW degrees of freedom -- the
        # MG names above are the old flat scalars, so a tune that leaves
        # these at their shipped (equal) values reproduces the flat eval.
        "DOUBLED_PAWN_EG", "ISOLATED_PAWN_EG", "BACKWARD_PAWN_EG",
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
    + [(f"MOBILITY_WEIGHT_EG[{chess.piece_name(pt)}]", "MOBILITY_WEIGHT_EG", pt)
       for pt in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)]   # FI-86
)


# The five eval terms that are BUILT but toggled OFF -- with the verdict that
# turned each one off. READ THESE BEFORE ENABLING ANY OF THEM.
#
# 2026-07-23: `--with-dormant` blanket-enabled all five, and the campaign came
# back -8.83 Elo over 7,557 games. It re-answered a question the ledger had
# already answered: king shelter and outpost each carry a 10,000-game A/B at
# full C-core depth, and king shelter's own comment says "Do not re-try at
# this TC". Two more were negative in the Python era. The premise this flag
# was built on -- "rejected on hand-guessed weights against a depth-8 engine"
# -- was simply false for half of them.
DORMANT_VERDICTS = {
    "use_king_shelter": "REJECTED at C-core depth, 10k vs v32: -4.27 +/-6.8 "
                        "(cengine.py: 'Do not re-try at this TC')",
    "use_outpost":      "NULL at C-core depth, 10k vs v37: -0.90 +/-6.8 "
                        "('buys nothing and costs eval work, so OFF')",
    "use_space":        "Python-era A/B: -9",
    "use_storm":        "Python-era A/B: -5",
    "use_phalanx":      None,     # +3 Python-era, the only non-negative
}
# Default set = only terms with NO recorded negative. --include-rejected adds
# the rest back, printing each verdict first.
DORMANT_TOGGLES = tuple(k for k, v in DORMANT_VERDICTS.items() if v is None)
DORMANT_ALL = tuple(DORMANT_VERDICTS)
_DORMANT_WEIGHTS = {
    "use_outpost": ("OUTPOST_N_MG", "OUTPOST_N_EG", "OUTPOST_B_MG", "OUTPOST_B_EG"),
    "use_space": ("SPACE_MG",),
    "use_phalanx": ("PHALANX_MG", "PHALANX_EG"),
    "use_storm": ("STORM_MG", "STORM_EG"),
    "use_king_shelter": ("SHELTER_CLOSE", "SHELTER_FAR"),
}


def dormant_params(toggles):
    """Only the weights of the terms actually being enabled -- tuning a weight
    multiplied by a disabled term fits noise."""
    return [(n, n, None) for t in toggles for n in _DORMANT_WEIGHTS[t]]


# ---------------------------------------------------------------------- #
#  PST parameters (--pst). 12 tables x 64 squares, minus the 16 pawn squares
#  on ranks 1 and 8 -- a pawn can never stand there, so those entries are
#  pure noise to fit. The tables are the STOCK PeSTO values: v53 fitted the
#  44 scalars *conditioned on* them, so the tables themselves have never
#  been fitted for this engine. That is the one eval avenue with a proven
#  mechanism and no recorded negative.
#
#  Bounds are +/-PST_BOUND cp around the shipped square (much tighter than
#  the +/-60% scalar rule): per-square signal is thin, and loose bounds are
#  exactly how coordinate descent produces the spiky, incoherent tables that
#  kept PST out of the tuner in the first place (tune.py:36).
PST_TABLES = ["MG_PAWN_TABLE", "EG_PAWN_TABLE", "MG_KNIGHT_TABLE",
              "EG_KNIGHT_TABLE", "MG_BISHOP_TABLE", "EG_BISHOP_TABLE",
              "MG_ROOK_TABLE", "EG_ROOK_TABLE", "MG_QUEEN_TABLE",
              "EG_QUEEN_TABLE", "MG_KING_TABLE", "EG_KING_TABLE"]
PST_BOUND = 25


def pst_params():
    out = []
    for t in PST_TABLES:
        for sq in range(64):
            if "PAWN" in t and (sq < 8 or sq >= 56):
                continue                      # pawns never stand on rank 1/8
            out.append((f"{t}[{sq}]", t, sq))
    return out


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
        if label.split("[")[0] in PST_TABLES:
            out.append((v - PST_BOUND, v + PST_BOUND))   # may go negative
        elif label.startswith("MOBILITY_WEIGHT"):   # incl. _EG (FI-86)
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
    _use_full_corpus()   # spawned workers re-import fit_wdl_model fresh
    import fit_wdl_model as F
    if not F.classify_file(path):
        return path, []
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except OSError:
        return path, []
    base = zlib.crc32(os.path.basename(path).encode())
    rows, gids = [], []
    for bi, block in enumerate(F.iter_game_blocks(text)):
        gid = (base ^ (bi * 2654435761)) & 0xFFFFFFFF   # FB-43: unique per game
        qr = _quiet_positions(block, max_per_game)
        rows.extend(qr); gids.extend([gid] * len(qr))
    # One match log holds ~20k games = ~240k quiet positions, so an uncapped
    # 1M target is met by four or five files -- i.e. four or five engine
    # pairings. Cap per file so the fit spans eras instead.
    if per_file_cap and len(rows) > per_file_cap:
        # crc32, not hash(): str hashing is salted per process, and workers
        # are spawned, so hash() would make extraction unreproducible.
        seed = zlib.crc32(os.path.basename(path).encode())
        idx = random.Random(seed).sample(range(len(rows)), per_file_cap)
        rows = [rows[i] for i in idx]; gids = [gids[i] for i in idx]
    # Pack to the record dtype HERE, not in the parent: a Python tuple costs
    # ~450 B against the record's 80 B, so accumulating tuples for a
    # multi-million-position set would cost GBs and pickle slowly back.
    arr = np.zeros(len(rows), dtype=RECORD)
    for i, r in enumerate(rows):
        arr[i]["bb"] = r[:8]
        arr[i]["cast"], arr[i]["turn"], arr[i]["ep"], arr[i]["res"] = r[8:]
        arr[i]["game"] = gids[i]
    return path, arr


def _use_full_corpus():
    """fit_wdl_model gates its corpus to ONE eval era, because it maps this
    engine's cp to an outcome probability and v53 moved the cp scale. Texel
    labels are GAME RESULTS, which are scale-free -- a v31 win is a win on
    any eval. So reuse that module's log parsing but not its era policy, or
    the whole 10 GB corpus filters down to nothing."""
    import fit_wdl_model as F
    F._MIN_C_ERA_SNAPSHOT = 31
    F.CENGINE_MIN_DATE = None


def cmd_extract(a):
    _use_full_corpus()
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

    parts, n, used, t0 = [], 0, 0, time.time()
    with mp.Pool(a.workers, initializer=interruptible.silence_worker) as pool:
        it = pool.imap_unordered(_extract_file,
                                 ((f, a.max_per_game, cap) for f in files))
        for i, (path, got) in enumerate(it, 1):
            if len(got):
                used += 1
                parts.append(got)
                n += len(got)
            if i % 25 == 0 or n >= a.limit:
                print(f"  {i:>4}/{len(files)} files  {used} usable  "
                      f"{n:,} positions  [{time.time() - t0:.0f}s]")
            if n >= a.limit:
                pool.terminate()
                break

    if not parts:
        sys.exit("no usable positions -- every log was filtered out "
                 "(odds / mismatched-strength / Stockfish logs are excluded)")
    arr = np.concatenate(parts)
    del parts
    # FB-43: shuffle at GAME granularity, not position. Keep each game's rows
    # together and randomise the order of GAMES, so a contiguous tail slice is
    # whole games -- the tune's val split then shares no game with train.
    # (Position-level shuffle put ~97% of games on both sides of the split.)
    rng = np.random.default_rng(52)
    uniq = np.unique(arr["game"])          # sorted
    rank = rng.permutation(len(uniq))      # a random rank per game
    key = rank[np.searchsorted(uniq, arr["game"])]
    arr = arr[np.argsort(key, kind="stable")]   # stable => rows stay grouped
    arr = arr[:a.limit]                    # truncation splits at most ONE game
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


def _worker_init(npy_path, nchunks, ntrain, dormant=()):
    interruptible.silence_worker()   # Ctrl-C hits the whole process
                                     # group; the parent owns shutdown
    import engine as E
    lib = ctypes.CDLL("./csearch.so")
    lib.csearch_eval_white.restype = ctypes.c_int
    lib.csearch_eval_white.argtypes = [ctypes.c_uint64] * 8 + \
        [ctypes.c_int, ctypes.c_int, ctypes.c_uint64]
    eng = E.Engine()
    if dormant:
        for t in dormant:
            setattr(eng, t, True)
    _W.update(lib=lib, E=E, eng=eng, arr=np.load(npy_path, mmap_mode="r"),
              nchunks=nchunks, ntrain=ntrain, cid=-1, cols=None, res=None)
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
    lib.csearch_set_pawn_eg(eng.DOUBLED_PAWN_EG, eng.ISOLATED_PAWN_EG,   # FI-86
                            eng.BACKWARD_PAWN_EG)


def _chunk(cid):
    """Columns of chunk `cid` as plain Python int lists -- ~4x faster in the
    eval loop than indexing the numpy record, and only one chunk is held at a
    time so 10 workers never materialise the whole set.

    Chunk ids >= nchunks address the VALIDATION tail; the split point is a
    fixed index into a set that was shuffled at extraction, so train and
    validation never share a game."""
    if _W["cid"] != cid:
        arr = _W["arr"]
        if cid < _W["nchunks"]:
            base, span, k = 0, _W["ntrain"], cid
        else:
            base, span, k = _W["ntrain"], len(arr) - _W["ntrain"], \
                cid - _W["nchunks"]
        lo = base + span * k // _W["nchunks"]
        hi = base + span * (k + 1) // _W["nchunks"]
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


def _loss(pool, nchunks, vec, k, val=False):
    ids = range(nchunks, 2 * nchunks) if val else range(nchunks)
    out = pool.map(_loss_task, [(c, vec, k) for c in ids], chunksize=1)
    tot = sum(s for s, _ in out)
    n = sum(m for _, m in out)
    return tot / n


# ---------------------------------------------------------------------- #
#  Live progress bar (tune)
# ---------------------------------------------------------------------- #
# A PST round scans ~790 parameters and takes >10 minutes with no output at
# all, which is indistinguishable from a hang on a rented box. This draws an
# in-place line with an ETA for the current round.
#
# isatty-gated for the same reason match.py's bar is: piping through `tee`
# turns every \r into another line and buries the log. When it is off, a
# plain marker every 25% still lands in the redirected output.
_BAR_ON = sys.stdout.isatty()
_BAR_W = 28
_bar_last = [-1]


def _bar(label, done, total, t0, changed):
    if total <= 0:
        return
    frac = done / total
    if not _BAR_ON:
        q = int(frac * 4)
        if q != _bar_last[0]:
            _bar_last[0] = q
            if q:
                print(f"    {label}  {frac * 100:.0f}%  ({changed} moved)",
                      flush=True)
        return
    el = time.time() - t0
    eta = (el / frac - el) if frac > 0.02 else None
    fill = int(frac * _BAR_W)
    sys.stdout.write(
        f"\r  [{'#' * fill}{'-' * (_BAR_W - fill)}] {label}  "
        f"{done}/{total} ({frac * 100:.0f}%)  {changed} moved  "
        f"{_hms(el)} elapsed" + (f"  ETA {_hms(eta)}" if eta else "") + "   ")
    sys.stdout.flush()


def _bar_clear():
    _bar_last[0] = -1
    if _BAR_ON:
        sys.stdout.write("\r" + " " * 110 + "\r")
        sys.stdout.flush()


def _hms(sec):
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"
    if sec >= 60:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec}s"


def cmd_tune(a):
    if os.path.basename(a.out) in ("engine.py", "cengine.py"):
        sys.exit("refusing to overwrite the live engine -- pick another --out")
    if not os.path.exists(a.positions):
        sys.exit(f"{a.positions} not found -- run: python3 texel.py extract")

    import engine as E
    global PARAMS
    dormant = ()
    if a.with_dormant:
        dormant = DORMANT_ALL if a.include_rejected else DORMANT_TOGGLES
        print("dormant terms enabled: " + (", ".join(dormant) or "(none)"))
        for t in dormant:
            if DORMANT_VERDICTS[t]:
                print(f"  !! {t}: {DORMANT_VERDICTS[t]}")
        skipped = [t for t in DORMANT_ALL if t not in dormant]
        for t in skipped:
            print(f"  -- skipped {t}: {DORMANT_VERDICTS[t]}")
        if skipped:
            print("     (--include-rejected re-enables them anyway)")
        PARAMS = PARAMS + dormant_params(dormant)
    if a.pst:
        if a.pst_bound:
            global PST_BOUND
            PST_BOUND = a.pst_bound
        PARAMS = PARAMS + pst_params()
        print(f"PST tuning ON: +{len(pst_params())} table entries "
              f"(+/-{PST_BOUND}cp each), {len(PARAMS)} params total")
    eng = E.Engine()
    for t in dormant:
        setattr(eng, t, True)
    base = baseline_vector(eng)
    bounds = bounds_for(base)
    if a.workers <= 0:            # project convention (match.py / odds.py)
        a.workers = max(1, os.cpu_count() - 1)
    arr = np.load(a.positions, mmap_mode="r")
    nchunks = a.workers
    ntrain = int(len(arr) * (1.0 - a.val_frac))
    if "game" in arr.dtype.names:
        # FB-43: walk the split back to where the game id changes, so no game
        # straddles train/val. arr is game-contiguous from extract.
        g = arr["game"]
        while 0 < ntrain < len(g) and g[ntrain] == g[ntrain - 1]:
            ntrain -= 1
        shared = set(np.unique(g[:ntrain]).tolist()) & set(np.unique(g[ntrain:]).tolist())
        assert not shared, f"FB-43 split leak: {len(shared)} games straddle"
        # The walk-back above terminates at 0 when every row carries the SAME
        # game id -- which is what a pre-FB-43 pack unpacks to. Training on an
        # empty set is not a degraded run, it is a meaningless one.
        if ntrain == 0:
            sys.exit("train split is EMPTY: every position carries the same "
                     "game id, so the FB-43 boundary walk-back consumed the "
                     "whole set. The positions file is a pre-FB-43 pack -- "
                     "re-unpack from a game-tagged .npz.")
    else:
        print("  [warn] positions file predates FB-43 (no game id) -- the "
              "val split may leak; re-extract for a clean held-out number")
    print(f"{len(arr):,} positions ({ntrain:,} train / "
          f"{len(arr) - ntrain:,} validation), {len(PARAMS)} parameters, "
          f"{a.workers} workers, {a.restarts} restart(s)")

    best_vec, best_val, results = None, None, []

    def _salvage():
        """Ctrl-C during a multi-hour tune used to throw the whole run away --
        engine_tuned.py is only written at the very end. Write the
        best-on-validation vector found SO FAR instead; a partial descent is
        still a candidate, and the printed loss says how far it got."""
        if best_vec is None:
            print("  (no round finished yet -- nothing to write)")
            return
        for (_, attr, key), v in zip(PARAMS, best_vec):
            _set(E.Engine, attr, key, v)
        _write_back(a.out, dormant)
        print(f"  wrote {a.out} from the best round so far "
              f"(validation {best_val:.6f}, {(base_val - best_val) / base_val * 100:+.2f}% "
              f"vs shipped) -- a PARTIAL descent, not a converged one")
    with mp.Pool(a.workers, initializer=_worker_init,
                 initargs=(a.positions, nchunks, ntrain,
                           dormant)) as pool:
        # --- fit K (the cp -> win-probability scale) on the shipped eval --- #
        t0 = time.time()
        k, kloss = None, None
        for cand in [round(0.6 + 0.1 * i, 1) for i in range(20)]:
            L = _loss(pool, nchunks, base if k is None else None, cand)
            if kloss is None or L < kloss:
                kloss, k = L, cand
        base_val = _loss(pool, nchunks, base, k, val=True)
        print(f"K = {k}   shipped eval: train {kloss:.6f}  "
              f"validation {base_val:.6f}   ({time.time() - t0:.0f}s)")

        # Ctrl-C / SIGTERM from here on writes the best-so-far vector
        # instead of discarding a multi-hour descent.
        with interruptible.salvage(_salvage, "tuned engine"):
            for restart in range(a.restarts):
                # Restart 0 starts from the shipped values; later restarts start
                # from a random point inside the bounds. Coordinate descent finds
                # a LOCAL optimum, and restarts are the only axis on which more
                # compute buys a better answer -- extra rounds just re-confirm
                # the same optimum once the step size stops moving anything.
                if restart == 0 and not a.skip_base:
                    cur = list(base)
                else:
                    # Jitter around the shipped values, NOT uniform-random inside
                    # the bounds: a uniform start in 44 dimensions lands nowhere
                    # near a chess-sane eval and descent cannot climb back (a
                    # measured 0.1002 against the shipped basin's 0.0891). The
                    # jitter widens with each restart to probe further out.
                    rng = random.Random(1000 + restart + 97 * a.restart_seed)
                    frac = 0.10 * (1 + (restart - 1) % 4)
                    while True:
                        cur = []
                        for v, (lo, hi) in zip(base, bounds):
                            j = max(1, int((hi - lo) * frac / 2))
                            cur.append(min(hi, max(lo, v + rng.randint(-j, j))))
                        if _valid(cur):
                            break
                cur_loss = _loss(pool, nchunks, cur, k)
                start_loss = cur_loss
                print(f"\n--- restart {restart + 1}/{a.restarts} "
                      f"({'shipped values' if restart == 0 else 'jittered start'}), "
                      f"train {cur_loss:.6f} ---")

                for rnd in range(1, a.rounds + 1):
                    # Anneal the step: the delta schedule is spread evenly over
                    # the rounds (12 rounds, 3 deltas -> 4 coarse, 4 mid, 4 fine).
                    delta = a.deltas[min((rnd - 1) * len(a.deltas) // a.rounds,
                                         len(a.deltas) - 1)]
                    t0, changed = time.time(), 0
                    order = list(range(len(PARAMS)))
                    random.Random(restart * 1000 + rnd + 97 * a.restart_seed).shuffle(order)
                    for _done, idx in enumerate(order):
                        _bar(f"r{restart + 1}/{a.restarts} "
                             f"round {rnd}/{a.rounds} d{delta}cp",
                             _done, len(order), t0, changed)
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
                    _bar_clear()
                    vloss = _loss(pool, nchunks, cur, k, val=True)
                    flag = ""
                    if best_val is None or vloss < best_val:
                        best_val, best_vec, flag = vloss, list(cur), "  <- best"
                    print(f"  round {rnd:>3}/{a.rounds}  delta {delta:>2}cp  "
                          f"train {cur_loss:.6f}  val {vloss:.6f}  "
                          f"({changed:>2} moved, {time.time() - t0:.0f}s){flag}")
                    if not changed and delta == min(a.deltas):
                        print("  converged")
                        break
                results.append((restart, start_loss, cur_loss, vloss))

    print(f"\nshipped eval: train {kloss:.6f}  validation {base_val:.6f}")
    print(f"best found:   validation {best_val:.6f}  "
          f"({(base_val - best_val) / base_val * 100:+.2f}% vs shipped)")
    if best_val >= base_val:
        print("\nNO IMPROVEMENT on held-out data -- the shipped eval already "
              "fits this corpus\nbetter than anything the search found. Do "
              "NOT spend an A/B slot on this.")
    print("\nchanged parameters (best-on-validation):")
    for (label, _, _), b0, b1 in zip(PARAMS, base, best_vec):
        if b0 != b1:
            print(f"  {label:<28} {b0:>6} -> {b1:>6}  ({b1 - b0:+d})")

    for (_, attr, key), v in zip(PARAMS, best_vec):
        _set(E.Engine, attr, key, v)
    _write_back(a.out, dormant)
    snaps = sorted((int(d) for d in os.listdir("Old Engine") if d.isdigit()),
                   reverse=True)
    base = f"Old Engine/{snaps[0]}" if snaps else "the newest snapshot"
    print(f"\nWrote {a.out}. This is a CANDIDATE, not a release. Next:\n"
          f"  python3 texel.py stage        # build + verify a testable dir\n"
          f"then screen it against {base} (>= +15 earns the 10k). An eval "
          f"change re-pins BOTH CE_LADDER and REF_NODES, and moves the bench "
          f"signature.")



# ---------------------------------------------------------------------- #
#  Write-back helpers -- VENDORED from tune.py, not imported.
#  tune.py is gitignored (.gitignore:95), so `from tune import ...` worked on
#  the dev Mac and crashed on every fresh clone -- after a 3.5-hour tune had
#  already finished, losing the write-back (2026-07-23). Never import a
#  gitignored module from a tracked one.
# ---------------------------------------------------------------------- #
def _replace_dict_block(lines, name, new_lines):
    start = None
    for i, line in enumerate(lines):
        if re.match(rf'\s+{re.escape(name)}\s*=\s*\{{', line):
            start = i; break
    if start is None:
        print(f"  [warn] {name} not found", file=sys.stderr); return
    end = None
    for i in range(start + 1, len(lines)):
        if lines[i].rstrip().endswith('}'):
            end = i; break
    if end is None:
        return
    lines[start:end + 1] = new_lines


def _replace_pst(lines, name, values):
    start = None
    for i, line in enumerate(lines):
        if re.match(rf'\s+{re.escape(name)}\s*=\s*\[', line):
            start = i; break
    if start is None:
        print(f"  [warn] {name} not found", file=sys.stderr); return
    end = None
    for i in range(start + 1, len(lines)):
        if lines[i].rstrip().endswith(']'):
            end = i; break
    if end is None:
        return
    new = [f"    {name} = ["]
    for rank in range(8):
        row = [round(values[rank * 8 + f]) for f in range(8)]
        new.append("        " + "".join(f"{v:5d}," for v in row))
    new.append("    ]")
    lines[start:end + 1] = new


def _write_back(dst, enable_toggles=()):
    import pathlib
    """Patch the tuned constants into a copy of engine.py. Reuses tune.py's
    line-rewriting helpers so there is one implementation of the fiddly part;
    PST blocks are never touched."""
    import shutil
    import re
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
    if enable_toggles:
        # The weights only reach the C eval when the toggle is on -- writing
        # fitted values into a file that still says `use_outpost = False`
        # would produce a candidate byte-identical to the baseline.
        txt = pathlib.Path(dst).read_text()
        for t in enable_toggles:
            old_line = f"self.{t} = False"
            if old_line not in txt:
                sys.exit(f"write-back: `{old_line}` not found -- refusing to "
                         f"write a candidate whose terms would stay off")
            txt = txt.replace(old_line, f"self.{t} = True")
        pathlib.Path(dst).write_text(txt)
    with open(dst) as f:
        lines = f.read().splitlines()

    if any(l.split("[")[0] in PST_TABLES for l, _, _ in PARAMS):
        for n in PST_TABLES:
            _replace_pst(lines, n, getattr(E, n))

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
    for _attr in ("MOBILITY_WEIGHT", "MOBILITY_WEIGHT_EG"):   # FI-86
        _m = getattr(E, _attr)
        _replace_dict_block(lines, _attr, [
            f"    {_attr} = {{",
            f"        chess.KNIGHT: {_m[chess.KNIGHT]}, chess.BISHOP: "
            f"{_m[chess.BISHOP]}, chess.ROOK: {_m[chess.ROOK]}, "
            f"chess.QUEEN: {_m[chess.QUEEN]},",
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
_PROBE_SRC = """
import sys, ctypes, random, chess
sys.path.insert(0, {dir!r})
import cengine
eng = cengine.Engine()
# cengine declares argtypes for the eval SETTERS only; without these the
# 64-bit bitboards would be passed as C ints and silently truncated.
eng._lib.csearch_eval_white.restype = ctypes.c_int
eng._lib.csearch_eval_white.argtypes = [ctypes.c_uint64] * 8 + \
    [ctypes.c_int, ctypes.c_int, ctypes.c_uint64]
out = []
rng = random.Random(52)
for _ in range(30):
    b = chess.Board()
    for _ in range(rng.randint(4, 60)):
        mv = list(b.legal_moves)
        if not mv:
            break
        b.push(rng.choice(mv))
        if not b.is_check() and rng.random() < 0.3:
            out.append(eng._lib.csearch_eval_white(
                b.pawns, b.knights, b.bishops, b.rooks, b.queens, b.kings,
                b.occupied_co[chess.WHITE], b.occupied_co[chess.BLACK],
                1 if b.turn == chess.WHITE else 0,
                -1 if b.ep_square is None else b.ep_square, b.castling_rights))
print(",".join(map(str, out)))
"""


def _probe(dirpath):
    """Eval a fixed probe set through the cengine in `dirpath`, in its OWN
    process -- csearch.so's eval globals are process-wide and its basename is
    the same in every snapshot dir, so two versions in one process would
    silently share whichever params were pushed last."""
    import subprocess
    r = subprocess.run([sys.executable, "-c", _PROBE_SRC.format(dir=dirpath)],
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"probe of {dirpath} failed:\n{r.stderr.strip()[-2000:]}")
    return [int(x) for x in r.stdout.strip().split(",")]


def cmd_pack(a):
    """positions .npy -> a compact .npz small enough to ship via GitHub.

    358 MB -> ~100 MB, without touching a single label: occ_b is fully
    derivable (all pieces AND NOT occ_w), and castling_rights takes only 16
    distinct values so it stores as a byte index. The servers `unpack` back
    to the .npy because the tuner mmaps it -- 95 workers share one mapping,
    which a compressed archive cannot do.
    """
    arr = np.load(a.src, mmap_mode="r")
    bb = np.ascontiguousarray(arr["bb"][:, :7])          # occ_b dropped
    cast_u = np.unique(arr["cast"])
    np.savez_compressed(
        a.out, bb=bb,
        cast=np.searchsorted(cast_u, arr["cast"]).astype("u1"),
        castmap=cast_u,
        game=arr["game"],                                # FB-43
        meta=np.stack([arr["turn"], arr["ep"].view("u1"), arr["res"]], 1))
    src_mb = os.path.getsize(a.src) / 1048576
    out_mb = os.path.getsize(a.out) / 1048576
    print(f"packed {len(arr):,} positions: {src_mb:.0f} MB -> {out_mb:.0f} MB "
          f"({src_mb / out_mb:.1f}x)")


def cmd_unpack(a):
    """.npz -> the .npy the tuner mmaps. Run once on each server."""
    z = np.load(a.src)
    bb7, meta = z["bb"], z["meta"]
    n = len(bb7)
    out = np.zeros(n, dtype=RECORD)
    out["bb"][:, :7] = bb7
    allp = bb7[:, 0] | bb7[:, 1] | bb7[:, 2] | bb7[:, 3] | bb7[:, 4] | bb7[:, 5]
    out["bb"][:, 7] = allp & ~bb7[:, 6]                  # occ_b reconstructed
    out["cast"] = z["castmap"][z["cast"]]
    out["turn"], out["ep"], out["res"] = meta[:, 0], meta[:, 1].view("i1"), meta[:, 2]
    if "game" in z.files:                                # FB-43
        out["game"] = z["game"]
    else:
        # A pre-FB-43 pack has no game ids, and leaving them at zero is NOT
        # a harmless default: the tuner reads one giant game, walks the split
        # point back to 0, and silently trains on NOTHING (measured on the
        # 2026-07-23 release asset). Refuse rather than write that file.
        sys.exit(f"{a.src} predates FB-43 (no game ids). Unpacking it would "
                 f"produce a file whose train/val split collapses to 0 train "
                 f"positions. Re-pack from a game-tagged .npy and re-upload "
                 f"the release asset.")
    np.save(a.out, out)
    print(f"unpacked {n:,} positions -> {a.out} "
          f"({os.path.getsize(a.out) / 1048576:.0f} MB)")


def cmd_stage(a):
    import pathlib
    """Assemble a ready-to-A/B engine directory from the tuned engine.py.

    engine_tuned.py is a copy of engine.py, NOT of cengine.py -- engine.py is
    where the eval constants live, and cengine.py imports the engine.py
    SITTING NEXT TO IT (cengine.py:320, `_DIR` is inserted at sys.path[0]) to
    push those constants into csearch.so. So the tuned file cannot be handed
    to match.py directly: doing so runs the slow pure-Python engine, and a
    copy left in the repo root would still read the untuned engine.py. It has
    to be staged as `engine.py` in a directory beside a cengine.py and the
    three .so files, exactly like the Old Engine/NN snapshots.
    """
    import shutil
    if not os.path.exists(a.src):
        sys.exit(f"{a.src} not found -- run: python3 texel.py tune")
    if os.path.exists(a.dir):
        sys.exit(f"{a.dir} already exists -- remove it or pick another --dir")
    os.makedirs(a.dir)
    shutil.copy(a.src, os.path.join(a.dir, "engine.py"))
    # cengine OVERRIDES two of engine.py's eval toggles from its OWN class
    # attrs (cengine.py:985-986, `self._py.use_outpost = bool(self.USE_OUTPOST)`)
    # -- they are C-era A/B toggles that live on cengine. So a candidate that
    # enables outpost/king-shelter in engine.py alone gets them silently
    # forced back OFF in match play, and the A/B measures a config that was
    # never tuned. Mirror them onto the staged cengine.
    cen = pathlib.Path("cengine.py").read_text()
    src = pathlib.Path(a.src).read_text()
    for eng_attr, cen_attr in (("use_outpost", "USE_OUTPOST"),
                               ("use_king_shelter", "USE_KING_SHELTER")):
        if f"self.{eng_attr} = True" in src:
            before = cen
            cen = cen.replace(f"    {cen_attr} = False", f"    {cen_attr} = True", 1)
            if cen == before:
                sys.exit(f"stage: {a.src} enables {eng_attr} but "
                         f"`    {cen_attr} = False` was not found in cengine.py "
                         f"-- refusing to stage a candidate whose terms would "
                         f"be silently disabled")
            print(f"  mirrored {eng_attr} -> cengine {cen_attr} = True")
    pathlib.Path(a.dir, "cengine.py").write_text(cen)
    for so in ("csearch.so", "eval_c.so", "movegen.so"):
        shutil.copy(so, os.path.join(a.dir, so))

    print(f"staged {a.dir}/ from {a.src}; verifying ...")
    tuned, live = _probe(a.dir), _probe(".")
    diff = sum(1 for x, y in zip(tuned, live) if x != y)
    if not diff:
        sys.exit(f"\nFAIL: {a.dir} evaluates all {len(tuned)} probe positions "
                 f"identically to the live engine.\nThe tuned parameters did "
                 f"NOT take effect -- an A/B of this would measure nothing.")
    print(f"OK -- {diff}/{len(tuned)} probe positions differ from the live "
          f"engine, so the tuned\n     values are live in the C eval.")
    # SCREEN first, not the full campaign: a Texel fit optimises static
    # prediction, and the terms it drives to their floor tend to be the ones
    # the SEARCH already resolves concretely (passers, rook-on-7th, tempo).
    # That makes the Elo sign genuinely uncertain no matter how good the loss
    # looks, so it does not earn a 10,000-game slot up front.
    # The baseline is the NEWEST snapshot, not a hardcoded version -- this
    # printed "Old Engine/52" for a whole day after v53 shipped.
    snaps = sorted((int(d) for d in os.listdir("Old Engine") if d.isdigit()),
                   reverse=True)
    base = f"Old Engine/{snaps[0]}/engine{snaps[0]}.py" if snaps else "<snapshot>"
    print(f"\nScreen it first -- 2,000 games, ~+/-15 Elo (candidate first, so "
          f"a + score means the tune helped):\n"
          f"  python3 match.py {a.dir}/cengine.py "
          f'"{base}" 1000 --workers 0 --nodes 1750000\n'
          f"Only if that is clearly positive, spend the campaign: same line "
          f"with 5000 instead of 1000.")


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
    t.add_argument("--workers", type=int, default=cores,
                   help="0 = auto (cores - 1), same rule as match.py/odds.py")
    t.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    t.add_argument("--pst-bound", type=int, default=None,
                   help="per-square +/- bound for --pst (default 25). Many "
                        "v54 squares sit AT the 25 rail, so the optimum is "
                        "outside it -- widen to explore past that.")
    t.add_argument("--pst", action="store_true",
                   help="also tune the 736 piece-square-table entries (the "
                        "stock PeSTO tables, never fitted for this engine)")
    t.add_argument("--skip-base", action="store_true",
                   help="skip the restart-0 descent from the SHIPPED values. "
                        "That descent is deterministic, so two machines "
                        "running --restarts N both compute it identically -- "
                        "give one machine this flag and the pair covers 2N-1 "
                        "distinct basins instead of 2N-2 plus a duplicate.")
    t.add_argument("--restart-seed", type=int, default=0,
                   help="offsets the jitter seeds, so two machines running "
                        "the same command explore DIFFERENT restarts")
    t.add_argument("--include-rejected", action="store_true",
                   help="with --with-dormant, also enable terms that already "
                        "carry a recorded negative A/B (see DORMANT_VERDICTS)")
    t.add_argument("--with-dormant", action="store_true",
                   help="also enable and fit the five BUILT-but-OFF eval "
                        "terms (outpost, space, phalanx, storm, king shelter)")
    t.add_argument("--restarts", type=int, default=1,
                   help="independent descents from different starting points, "
                        "keeping the best on held-out data (default 1)")
    t.add_argument("--val-frac", type=float, default=0.2,
                   help="fraction held out to detect overfitting (default 0.2)")
    t.add_argument("--deltas", type=int, nargs="+", default=list(DEFAULT_DELTAS),
                   help="coordinate-descent step sizes in cp, annealed evenly "
                        "across --rounds (default: 8 3 1)")
    t.set_defaults(fn=cmd_tune)

    k = sub.add_parser("pack", help="positions .npy -> compact .npz for GitHub")
    k.add_argument("--src", default=POSITIONS_NPY)
    k.add_argument("--out", default="texel_positions.npz")
    k.set_defaults(fn=cmd_pack)

    u = sub.add_parser("unpack", help="compact .npz -> the .npy the tuner mmaps")
    u.add_argument("--src", default="texel_positions.npz")
    u.add_argument("--out", default=POSITIONS_NPY)
    u.set_defaults(fn=cmd_unpack)

    g = sub.add_parser("stage", help="tuned engine.py -> ready-to-A/B dir")
    g.add_argument("--src", default=DEFAULT_OUT)
    g.add_argument("--dir", default="Tuned")
    g.set_defaults(fn=cmd_stage)

    s = sub.add_parser("selftest", help="verify the eval mirror + param push")
    s.set_defaults(fn=cmd_selftest)

    # Line-buffer stdout so `nohup ... | tail -f` shows progress live;
    # Python block-buffers when stdout is not a tty and a 5-hour run would
    # otherwise print nothing until it finished.
    sys.stdout.reconfigure(line_buffering=True)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
