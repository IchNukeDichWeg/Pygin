#!/usr/bin/env python3
"""uci_match.py -- match runner for two arbitrary UCI engines.

Unlike match.py (which imports this repo's Python engine modules directly),
this speaks plain UCI over stdin/stdout via python-chess, so ANY UCI engine
works. Reporting is match.py's, byte-compatible where it matters: the same
live ETA/progress line, the same per-game "[engine] move SAN: info depth ..."
log blocks with PVs, the same BATTLE SUMMARY (Elo, pentanomial, normalized
Elo, SPRT), the same ``<e1>_vs_<e2>_<stamp>_<pid>.txt`` / ``.pgn`` pair, and
the same pooled SPRT state files.

Engines are given with --engine1/--engine2 (full command line, quoted).
Every setting exists in three forms: --X applies to both engines, --X1/--X2
override it for one engine. E.g. --threads 1 --hash2 256 means both play
1 thread, engine 2 gets 256 MB hash.

Search limit, first one that is set per engine wins over the clock:
    --depth N        fixed depth
    --movetime MS    fixed time per move, milliseconds
    --nodes N        fixed node count
    --tc BASE[+INC]  clock in seconds (default 30+0.3 when nothing is set)

    python3 uci_match.py \
        --engine1 "python3 cuci.py" --dir1 . --name1 cengine \
        --engine2 "venv/bin/python uci.py" --dir2 ../numbfish --name2 numbfish \
        --threads 1 --hash 128 --book off \
        --positions 50 --tc 30+0.3 --sprt

Each position is played twice (colours swapped), so N positions = 2N games;
the pair is scored pentanomially exactly as in match.py. --sprt enables the
same [0, 4] normalized GSPRT early stop; state pools across runs in an
auto-named sprt_*.json (--tag / --sprt-resume as in match.py). A crashed
engine forfeits its game (excluded from scoring, like match.py's errors) and
is restarted for the next one. Options an engine does not advertise are
skipped with a warning, not an error.
"""
import argparse
import datetime
import os
import queue
import random
import re
import shlex
import sys
import threading
import time

import chess
import chess.engine
import chess.pgn

# match.py carries the whole reporting/stats/SPRT toolbox; reuse it wholesale
# (importing it only defines config constants + functions, ~0.2 s).
import match as m

MAX_PLIES = 500          # safety net: unfinished game past this = draw
DEFAULT_EPD = "data/UHO_4060_v4.epd"

# every per-engine setting: (flag, kwargs for add_argument)
PER_ENGINE = {
    "dir":      dict(help="working directory for the engine"),
    "threads":  dict(type=int),
    "hash":     dict(type=int, metavar="MB"),
    "ponder":   dict(choices=("on", "off"), help="only 'off' is supported"),
    "book":     dict(choices=("on", "off"),
                     help="the engine's OWN opening book (default off)"),
    "option":   dict(action="append", metavar="NAME=VALUE",
                     help="extra UCI option, repeatable"),
    "depth":    dict(type=int, help="fixed search depth"),
    "movetime": dict(type=int, metavar="MS", help="fixed time per move, ms"),
    "nodes":    dict(type=int, help="fixed nodes per move"),
    "tc":       dict(metavar="BASE[+INC]", help="clock in seconds, e.g. 30+0.3"),
}


def pick(args, name, n):
    """--X1/--X2 wins over --X; lists (--option) are concatenated instead."""
    shared = getattr(args, name)
    specific = getattr(args, f"{name}{n}")
    if isinstance(shared, list) or isinstance(specific, list):
        return (shared or []) + (specific or [])
    return shared if specific is None else specific


def parse_tc(spec):
    base, _, inc = spec.partition("+")
    return float(base), float(inc) if inc else 0.0


def configure(engine, name, opts, quiet=False):
    for key, val in opts.items():
        if key in engine.options:
            engine.configure({key: val})
        elif not quiet:
            print(f"[{name}] does not advertise option {key!r} -- skipped")


def engine_opts(args, n):
    g = lambda a: pick(args, a, n)
    opts = {}
    if g("threads") is not None:
        opts["Threads"] = g("threads")
    if g("hash") is not None:
        opts["Hash"] = g("hash")
    # Ponder is managed by python-chess and we never send `go ponder`,
    # so pondering is always off; --ponder on is not supported.
    if g("ponder") == "on":
        sys.exit("pondering is not supported by this runner")
    opts["OwnBook"] = g("book") == "on"
    for kv in g("option") or []:
        k, _, v = kv.partition("=")
        opts[k] = {"true": True, "false": False}.get(v.lower(), v)
    return opts


class Side:
    """One engine's play mode: a fixed limit, or a clock it can flag on."""

    def __init__(self, args, n):
        g = lambda a: pick(args, a, n)
        fixed = {}
        if g("depth") is not None:
            fixed["depth"] = g("depth")
        if g("movetime") is not None:
            fixed["time"] = g("movetime") / 1000.0
        if g("nodes") is not None:
            fixed["nodes"] = g("nodes")
        self.fixed = chess.engine.Limit(**fixed) if fixed else None
        self.base, self.inc = parse_tc(g("tc") or "30+0.3")

    def describe(self):
        if self.fixed:
            f = self.fixed
            return " ".join(s for s in (
                f"depth {f.depth}" if f.depth else "",
                f"movetime {round(f.time*1000)}ms" if f.time else "",
                f"nodes {f.nodes}" if f.nodes else "") if s)
        return f"clock {self.base:g}+{self.inc:g}"

    def instrument(self):
        """Short filesystem-safe tag for the SPRT state-file name."""
        if self.fixed:
            f = self.fixed
            return "".join(s for s in (
                f"d{f.depth}" if f.depth else "",
                f"mt{round(f.time*1000)}" if f.time else "",
                f"n{f.nodes}" if f.nodes else "") if s)
        return f"tc{self.base:g}+{self.inc:g}"


class NamedEngine:
    """Name/path stub compatible with match.py's summary helpers."""

    def __init__(self, name, cmd, cwd):
        self.name = name
        script = next((t for t in shlex.split(cmd) if t.endswith(".py")), "")
        if script and cwd and not os.path.isabs(script):
            script = os.path.join(cwd, script)
        self.path = script or cmd


def info_line(info):
    """Rebuild the engine's final UCI info line from python-chess's parse,
    in the field order match.py's logs use."""
    parts = ["info"]
    for key in ("depth", "seldepth"):
        if key in info:
            parts += [key, str(info[key])]
    score = info.get("score")
    if score is not None:
        rel = score.relative
        parts += (["score", "mate", str(rel.mate())] if rel.is_mate()
                  else ["score", "cp", str(rel.score())])
    if "nodes" in info:
        parts += ["nodes", str(info["nodes"])]
    if "nps" in info:
        parts += ["nps", str(round(info["nps"]))]
    if "time" in info:
        parts += ["time", str(round(info["time"] * 1000))]
    return " ".join(parts)


def play_game(engines, sides, names, fen, round_no, game_id):
    """engines/sides/names keyed by chess.WHITE/BLACK. Returns a game dict in
    match.py's shape: result/reason/error/board/log."""
    board = chess.Board(fen)
    clocks = {c: sides[c].base for c in (chess.WHITE, chess.BLACK)}
    engine_log = []
    result, reason, error = None, None, None
    # claim_draw=False on purpose -- see match.py's copy of this loop.
    # claim_draw=True fires when the side to move merely HAS a move producing
    # a third repetition, and claims the draw for a player who is often
    # winning and would never claim it.
    def _over(b):
        return (b.is_game_over() or b.is_repetition(3)
                or b.halfmove_clock >= 100)

    while not _over(board) and len(board.move_stack) < MAX_PLIES:
        side = board.turn
        s = sides[side]
        limit = s.fixed or chess.engine.Limit(
            white_clock=clocks[chess.WHITE], black_clock=clocks[chess.BLACK],
            white_inc=sides[chess.WHITE].inc, black_inc=sides[chess.BLACK].inc)
        t0 = time.monotonic()
        try:
            res = engines[side].play(board, limit, game=game_id,
                                     info=chess.engine.INFO_ALL)
        except chess.engine.EngineTerminatedError:
            result = "0-1" if side == chess.WHITE else "1-0"
            reason = "ENGINE_CRASH"
            error = f"{names[side]} (engine process) died mid-game"
            break
        if s.fixed is None:                  # only clock players tick and flag
            clocks[side] -= time.monotonic() - t0
            if clocks[side] < 0:
                result = "0-1" if side == chess.WHITE else "1-0"
                reason = "TIME_FORFEIT"
                break
            clocks[side] += s.inc
        if res.move is None or res.move not in board.legal_moves:
            result = "0-1" if side == chess.WHITE else "1-0"
            reason = "ILLEGAL_MOVE"
            error = f"{names[side]} played illegal move {res.move}"
            break
        san = board.san(res.move)
        engine_log.append(f"[{names[side]}] move {san}: {info_line(res.info)}")
        pv = res.info.get("pv")
        if pv:
            engine_log.append("    PV: " + " ".join(mv.uci() for mv in pv))
        board.push(res.move)
    if result is None:
        outcome = board.outcome()
        if outcome is not None:
            result, reason = outcome.result(), outcome.termination.name
        elif board.is_repetition(3):
            result, reason = "1/2-1/2", "THREEFOLD_REPETITION"
        elif board.halfmove_clock >= 100:
            result, reason = "1/2-1/2", "FIFTY_MOVES"
        else:                                # MAX_PLIES tripped
            result, reason = "1/2-1/2", "MAX_PLIES (adjudicated draw)"
    return {"round": round_no, "fen": fen, "result": result, "reason": reason,
            "error": error, "board": board, "log": engine_log}


def write_game_block(fh, pgn_fh, g, wstub, bstub, e1s, mode_desc):
    """match.py's per-game .txt block + PGN, for UCI engines."""
    pgn_str = m.build_pgn(g["round"], g["fen"], wstub, bstub, g["board"],
                          g["result"], datetime.datetime.now(), None, None)
    wlab = "Engine 1" if wstub is e1s else "Engine 2"
    blab = "Engine 1" if bstub is e1s else "Engine 2"
    out = [f"=== Game {g['round']} ===", f"FEN: {g['fen']}",
           f"{wlab} (White): {wstub.path}", f"{blab} (Black): {bstub.path}",
           f"Mode: {mode_desc}"]
    if g["error"]:
        out.append(f"Outcome: ERROR / excluded -- {g['error']}")
    elif g["result"] == "1/2-1/2":
        out.append(f"Outcome: draw ({g['reason']})")
    else:
        winner = wstub if g["result"] == "1-0" else bstub
        wl = "Engine 1" if winner is e1s else "Engine 2"
        wc = "White" if winner is wstub else "Black"
        out.append(f"Outcome: {winner.name} ({wl}, {wc}) won -- "
                   f"{g['result']} ({g['reason']})")
    out.append("--- Engine Logs ---")
    out.extend(g["log"] if g["log"] else ["(no moves played)"])
    out += ["--- PGN ---", pgn_str, ""]
    fh.write("\n".join(out) + "\n")
    fh.flush()
    pgn_fh.write(pgn_str + "\n\n")
    pgn_fh.flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine1", required=True, help="command line for engine 1")
    ap.add_argument("--engine2", required=True, help="command line for engine 2")
    ap.add_argument("--name1", help="display name for engine 1")
    ap.add_argument("--name2", help="display name for engine 2")
    for flag, kw in PER_ENGINE.items():
        ap.add_argument(f"--{flag}", **kw)
        for n in ("1", "2"):
            ap.add_argument(f"--{flag}{n}", **{**kw, "help": argparse.SUPPRESS})
    ap.add_argument("--positions", type=int, default=50,
                    help="openings to play; each is played twice (default 50)")
    ap.add_argument("--workers", type=int, default=1, metavar="N",
                    help="parallel game workers, each with its own engine "
                         "pair; 0 = system cores - 1 (default 1)")
    ap.add_argument("--openings", default=DEFAULT_EPD,
                    help=f"EPD/FEN file, one position per line (default {DEFAULT_EPD})")
    ap.add_argument("--offset", type=int, default=0,
                    help="skip this many positions into the shuffled pool "
                         "(disjoint shards / SPRT tranches)")
    ap.add_argument("--seed", type=int,
                    help="shuffle seed for the opening pool (default: random; "
                         "pass a value for reproducible / poolable shards)")
    ap.add_argument("--pgn", help="PGN output path (default match.py-style auto-name)")
    ap.add_argument("--sprt", action="store_true",
                    help="GSPRT early stop, defaults [0, 4] normalized")
    ap.add_argument("--sprt-elo0", type=float, default=0.0)
    ap.add_argument("--sprt-elo1", type=float, default=4.0)
    ap.add_argument("--sprt-alpha", type=float, default=0.05)
    ap.add_argument("--sprt-beta", type=float, default=0.05)
    ap.add_argument("--sprt-model", default="normalized",
                    choices=("normalized", "logistic"))
    ap.add_argument("--sprt-resume", metavar="STATE.json",
                    help="pool with this state file (default: auto-named, stable)")
    ap.add_argument("--tag", help="campaign tag: names the state file sprt_<tag>.json")
    args = ap.parse_args()

    if args.seed is None:
        args.seed = random.randrange(1_000_000)
        # printed (and recorded in Openings/state file) so any run can still
        # be reproduced or sharded after the fact
        print(f"shuffle seed: {args.seed} (random; --seed {args.seed} reproduces)")

    # Names land in a log filename AND the state-file name, so anything a
    # shell command line can contain (slashes, spaces) has to go first.
    def clean(s):
        return re.sub(r"[^A-Za-z0-9._-]", "_", s.strip()) or "engine"

    name1 = clean(args.name1 or shlex.split(args.engine1)[-1])
    name2 = clean(args.name2 or shlex.split(args.engine2)[-1])
    side1, side2 = Side(args, "1"), Side(args, "2")
    e1s = NamedEngine(name1, args.engine1, pick(args, "dir", "1"))
    e2s = NamedEngine(name2, args.engine2, pick(args, "dir", "2"))

    if side1.describe() == side2.describe():
        mode_desc = side1.describe()
        instrument = side1.instrument()
    else:
        mode_desc = f"{side1.describe()} (E1) / {side2.describe()} (E2)"
        instrument = f"{side1.instrument()}_{side2.instrument()}"

    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = f"{name1}_vs_{name2}_{stamp}_{os.getpid()}.txt"
    pgn_path = args.pgn or log_path.replace(".txt", ".pgn")

    with open(args.openings) as f:
        pool = [ln.strip() for ln in f if ln.strip()]
    random.Random(args.seed).shuffle(pool)
    fens = []
    consumed = 0                 # POOL LINES eaten, not usable positions
    for line in pool[args.offset:]:
        consumed += 1
        p = line.split()
        # keep real move counters when the line has them; EPD opcodes get 0 1
        fen_try = (" ".join(p[:6]) if len(p) >= 6 and p[4].isdigit()
                   and p[5].isdigit() else " ".join(p[:4]) + " 0 1")
        try:
            fens.append(chess.Board(fen_try).fen())
        except ValueError:
            continue
        if len(fens) == args.positions:
            break
    if len(fens) < args.positions:
        print(f"warning: only {len(fens)} usable positions in {args.openings}")
    total_games = 2 * len(fens)
    openings_desc = (f"positions [{args.offset}, {args.offset + len(fens)}) of "
                     f"{os.path.basename(args.openings)} (shuffle seed {args.seed})")

    # ---- SPRT config + pooled state file, exactly match.py's behaviour ----
    if args.sprt and m._sprt is None:
        print("!! --sprt requested but sprt.py could not be imported -- "
              "running the full game budget without early-stop.")
    sprt_cfg = None
    if args.sprt and m._sprt is not None:
        sprt_cfg = {"elo0": args.sprt_elo0, "elo1": args.sprt_elo1,
                    "alpha": args.sprt_alpha, "beta": args.sprt_beta,
                    "model": args.sprt_model}
        lo, hi = m._sprt.bounds(args.sprt_alpha, args.sprt_beta)
        print(f"SPRT early-stop ON: [{args.sprt_elo0:g}, {args.sprt_elo1:g}] "
              f"{args.sprt_model}, alpha={args.sprt_alpha:g} "
              f"beta={args.sprt_beta:g} (bounds {lo:+.3f} .. {hi:+.3f}); "
              f"stops as soon as a bound is crossed, else runs the full "
              f"{total_games:,}-game budget.")
    sprt_resume_auto = not args.sprt_resume
    sprt_resume_path = args.sprt_resume or (
        f"sprt_{args.tag}.json" if args.tag else
        f"sprt_{name1}_vs_{name2}_{instrument}.json")
    base_penta, prior_next_off, prior_runs = [0] * 5, 0, []
    if os.path.exists(sprt_resume_path):
        base_penta, prior_next_off, prior_runs, note = m.sprt_resume_load(
            sprt_resume_path, args.offset)
        print(note)
    else:
        print(f"State file {sprt_resume_path!r} does not exist yet -- this is "
              f"run 1; it is created now and refreshed every {m.SAVE_EVERY} games.")
    this_run = {"engine1": name1, "engine2": name2,
                "offset": args.offset, "positions": len(fens),
                "seed": args.seed, "fen_file": os.path.basename(args.openings),
                "mode": mode_desc, "smp": int(pick(args, "threads", "1") or 1),
                "started": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    prev = next((r for r in reversed(prior_runs) if "engine1" in r), None)
    if prev:
        moved = [(k, prev.get(k), this_run[k])
                 for k in ("engine1", "engine2", "mode", "smp", "seed", "fen_file")
                 if k in prev and prev.get(k) != this_run[k]]
        if moved:
            print("  ** CONFIG CHANGED since the last run in this file -- "
                  "pooling anyway (nothing is gated on it):")
            for k, was, now in moved:
                print(f"       {k}: {was!r} -> {now!r}")
            print("     Different instruments must NOT be pooled. Use --tag to "
                  "give this campaign its own file if that is what you meant.")
    runs_log = list(prior_runs) + [this_run]
    # `consumed`, NOT len(fens): an unparseable line is skipped but still eaten
    # out of the pool, so counting usable positions would hand the next tranche
    # an offset that REPLAYS them. Overlapping tranches break the pentanomial
    # independence the pooled LLR assumes, and nothing downstream would notice.
    next_off = max(prior_next_off, args.offset + consumed)
    sprt_state = {"cfg": sprt_cfg, "llr": None, "lower": None, "upper": None,
                  "decision": None, "decided": None, "last_n": 0,
                  "base_pairs": sum(base_penta),
                  "resume_path": sprt_resume_path, "resume_auto": sprt_resume_auto}
    try:
        m.sprt_resume_save(sprt_resume_path, base_penta, next_off, None, runs_log)
    except OSError as ex:
        print(f"!! could not create state file {sprt_resume_path!r}: {ex}")

    # ---- engines ----------------------------------------------------------
    specs = [(args.engine1, name1, "1"), (args.engine2, name2, "2")]

    def start_engine(ix, quiet=False):
        cmd, name, n = specs[ix]
        e = chess.engine.SimpleEngine.popen_uci(
            shlex.split(cmd), cwd=pick(args, "dir", n))
        configure(e, name, engine_opts(args, n), quiet=quiet)
        return e

    n_workers = args.workers if args.workers > 0 else max(1, (os.cpu_count() or 2) - 1)
    n_workers = min(n_workers, max(1, len(fens)))

    # Worker 0's pair starts eagerly in the main thread: a wrong command or
    # cwd should fail loudly up front, and a python-chess traceback buries
    # which of the two engines it was. Extra workers start their own pairs
    # lazily inside their threads (warnings silenced -- one set is enough).
    engines = []
    try:
        for ix in (0, 1):
            engines.append(start_engine(ix))
    except Exception as ex:
        for e in engines:
            e.close()
        cmd, nm, n = specs[len(engines)]
        sys.exit(f"could not start engine {len(engines) + 1} ({nm}): "
                 f"{type(ex).__name__}: {ex}\n"
                 f"  command: {cmd}\n"
                 f"  cwd:     {pick(args, 'dir', n) or os.getcwd()}")

    fh = open(log_path, "w", encoding="utf-8")
    pgn_fh = open(pgn_path, "w", encoding="utf-8")
    impl = getattr(sys, "implementation", None)
    interp = f"{impl.name} {sys.version.split()[0]}" if impl else "python"
    banner = (f"Match: {name1}  vs  {name2}\n"
              f"Engine 1: {name1}  ({args.engine1})\n"
              f"Engine 2: {name2}  ({args.engine2})\n"
              f"Interpreter: {interp}\n"
              f"Mode: {mode_desc}   |   "
              f"{len(fens)} positions x 2 colours = {total_games} games\n"
              f"Workers: {f'{n_workers} parallel' if n_workers > 1 else '1 sequential'}\n"
              f"Openings: {openings_desc}\n"
              f"Log: {log_path}\n" + "-" * 72)
    print(banner)
    fh.write(banner + "\n")

    tally = {"completed": 0, "e1": 0, "e2": 0, "draws": 0, "errors": 0,
             "penta": {i: 0 for i in range(5)}, "penta_incomplete": 0}
    start_t = time.time()
    stopped = False

    # ---- live ETA status line, match.py's format --------------------------
    _is_tty = sys.stdout.isatty()
    eta_state = {"shown": False}

    def _status_text():
        played = tally["completed"] + tally["errors"]
        remaining = total_games - played
        elapsed = time.time() - start_t
        if played > 0 and elapsed > 0:
            rate = played / elapsed
            eta_s = m.fmt_duration_short(remaining / rate)
            rate_s = f"{rate * 60:.2f} Games per min"
        else:
            eta_s, rate_s = "estimating...", "--"
        pct = 100 * played / total_games if total_games else 0
        base = (f">> {played}/{total_games} ({pct:.2f}%)  |  "
                f"elapsed {m.fmt_duration_short(elapsed)}  |  ETA {eta_s}  |  {rate_s}")
        if sprt_state["cfg"] is not None and sprt_state["llr"] is not None:
            base += (f"  |  SPRT LLR {sprt_state['llr']:+.2f} "
                     f"[{sprt_state['lower']:+.2f}, {sprt_state['upper']:+.2f}]")
            if sprt_state["decided"]:
                base += " -> DECIDED"
        return base

    def _draw_status():
        if _is_tty:
            sys.stdout.write("\r\033[K" + _status_text())
            sys.stdout.flush()
            eta_state["shown"] = True

    def _clear_status():
        if _is_tty and eta_state["shown"]:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            eta_state["shown"] = False

    def _update_sprt():
        cfg = sprt_state["cfg"]
        if cfg is None or sprt_state["decided"] is not None:
            return
        counts = [base_penta[k] + tally["penta"][k] for k in range(5)]
        n_pairs = sum(counts)
        if n_pairs < m.SPRT_MIN_PAIRS or n_pairs == sprt_state["last_n"]:
            return
        sprt_state["last_n"] = n_pairs
        try:
            r = m._sprt.evaluate(counts, cfg["elo0"], cfg["elo1"],
                                 cfg["model"], cfg["alpha"], cfg["beta"])
        except Exception as ex:
            if not sprt_state.get("err_shown"):
                sprt_state["err_shown"] = True
                print(f"\n!! SPRT evaluation failed ({type(ex).__name__}: {ex}) "
                      f"-- the LLR shown is STALE from here on.",
                      file=sys.stderr, flush=True)
            return
        sprt_state.update(llr=r["llr"], lower=r["lower"], upper=r["upper"],
                          decision=r["decision"])
        if r["decision"] != "continue":
            sprt_state["decided"] = r["decision"]

    def _save_state():
        try:
            m.sprt_resume_save(sprt_resume_path,
                               [base_penta[k] + tally["penta"][k] for k in range(5)],
                               next_off, sprt_state, runs_log)
        except (OSError, ValueError):
            pass

    # ---- game loop --------------------------------------------------------
    # One machinery for both cases: N worker threads (each owning an engine
    # PAIR, both games of a position stay on one worker) push finished games
    # to a queue; the main thread does all scoring/printing and writes the
    # .txt/.pgn in schedule order through a reorder buffer, like match.py.
    work_q = queue.SimpleQueue()
    for i in range(len(fens)):
        work_q.put(i)
    res_q = queue.SimpleQueue()
    stop_evt = threading.Event()

    def worker(wid, pair):
        try:
            if pair is None:
                pair = [start_engine(0, quiet=True), start_engine(1, quiet=True)]
            while not stop_evt.is_set():
                try:
                    i = work_q.get_nowait()
                except queue.Empty:
                    break
                for e1_is_white in (True, False):
                    white_ix = 0 if e1_is_white else 1
                    eng = {chess.WHITE: pair[white_ix],
                           chess.BLACK: pair[1 - white_ix]}
                    sid = {chess.WHITE: (side1, side2)[white_ix],
                           chess.BLACK: (side2, side1)[white_ix]}
                    nam = {chess.WHITE: (name1, name2)[white_ix],
                           chess.BLACK: (name2, name1)[white_ix]}
                    rno = 2 * i + 2 - e1_is_white
                    # a fresh game id makes python-chess send ucinewgame itself
                    g = play_game(eng, sid, nam, fens[i], rno,
                                  (wid, i, e1_is_white))
                    if g["error"] is not None:
                        # dead process stays dead: restart it for the next game
                        for ix in (0, 1):
                            if pair[ix].transport.get_returncode() is not None:
                                pair[ix].close()
                                pair[ix] = start_engine(ix, quiet=True)
                    res_q.put(("game", i, e1_is_white, g))
        except Exception as ex:
            res_q.put(("worker_error", wid, f"{type(ex).__name__}: {ex}"))
        finally:
            for e in pair or []:
                try:
                    e.quit()
                except Exception:
                    e.close()
            res_q.put(("done", wid))

    threads = [threading.Thread(target=worker, args=(w, engines if w == 0 else None),
                                daemon=True)
               for w in range(n_workers)]
    for t in threads:
        t.start()

    pending = {}                 # round -> (g, e1_is_white), files stay in order
    next_rno = 1
    pos_scores = {}              # position index -> [e1 scores of the pair]
    done_workers = 0
    try:
        while done_workers < n_workers:
            msg = res_q.get()
            if msg[0] == "done":
                done_workers += 1
                continue
            if msg[0] == "worker_error":
                _clear_status()
                print(f"!! worker {msg[1]} died: {msg[2]} -- its remaining "
                      f"games fall to the other workers")
                continue
            _, i, e1_is_white, g = msg

            if g["error"] is not None:
                tally["errors"] += 1
                score = None
                tag = f"ERR ({g['error'][:40]})"
            else:
                tally["completed"] += 1
                score = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}[g["result"]]
                if not e1_is_white:
                    score = 1.0 - score
                if score == 0.5:
                    tally["draws"] += 1
                    tag = f"draw  {m.short_reason(g['reason'])}"
                elif score == 1.0:
                    tally["e1"] += 1
                    tag = f"{name1} wins  {m.short_reason(g['reason'])}"
                else:
                    tally["e2"] += 1
                    tag = f"{name2} wins  {m.short_reason(g['reason'])}"

            pos_scores.setdefault(i, []).append(score)
            if len(pos_scores[i]) == 2:
                a, b = pos_scores.pop(i)
                if a is None or b is None:
                    tally["penta_incomplete"] += 1
                else:
                    tally["penta"][m.pentanomial_bucket(a, b)] += 1

            pending[g["round"]] = (g, e1_is_white)
            while next_rno in pending:
                pg, pw = pending.pop(next_rno)
                wstub, bstub = (e1s, e2s) if pw else (e2s, e1s)
                write_game_block(fh, pgn_fh, pg, wstub, bstub, e1s, mode_desc)
                next_rno += 1

            if tally["completed"]:
                sc = (tally["e1"] + 0.5 * tally["draws"]) / tally["completed"]
                el, mar = m.elo(sc, tally["completed"])
                run = (f"{name1} {tally['e1']:,}W | {tally['draws']:,} D | "
                       f"{name2} {tally['e2']:,}W "
                       f"({100*sc:.2f}%, {el:+.2f} +/-{mar:.1f} Elo)")
            else:
                run = "no scored games yet"
            _update_sprt()
            played = tally["completed"] + tally["errors"]
            if played % m.SAVE_EVERY == 0:
                _save_state()
            wname, bname = (name1, name2) if e1_is_white else (name2, name1)
            line = (f"[{played:>4}/{total_games}] {wname} vs {bname}  ->  "
                    f"{g['result']:>7}  {tag:<24} | {run}")
            _clear_status()
            print(line)
            _draw_status()
            if not _is_tty and played % 500 == 0:
                print(_status_text())

            if args.sprt and sprt_state["decided"] and not stop_evt.is_set():
                stopped = True
                stop_evt.set()      # workers stop after their current game
    except KeyboardInterrupt:
        stopped = True
    finally:
        stop_evt.set()
        _clear_status()
        # flush whatever the reorder buffer still holds (early stop leaves
        # gaps in the round numbering; keep ascending order)
        for rno in sorted(pending):
            pg, pw = pending[rno]
            wstub, bstub = (e1s, e2s) if pw else (e2s, e1s)
            write_game_block(fh, pgn_fh, pg, wstub, bstub, e1s, mode_desc)
        _save_state()
        m.write_summary(fh, e1s, e2s, tally, total_games, start_t, stopped,
                        n_workers=n_workers, sprt_info=dict(sprt_state),
                        mode_desc=mode_desc, openings=openings_desc)
        fh.close()
        pgn_fh.close()
        print(f"\nLog written to: {log_path}")
        print(f"PGN written to: {pgn_path}")


if __name__ == "__main__":
    main()
