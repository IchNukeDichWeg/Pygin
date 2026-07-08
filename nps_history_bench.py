#!/usr/bin/env python3
"""
nps_history_bench.py
=====================
NPS + search-depth benchmark across engine version snapshots (Old Engine/N/).
For each (version, position) cell: RUNS_PER_POSITION timed searches at
SECONDS_PER_RUN seconds each, averaged. CPython only; opening book and
tablebase probing forced off. Engine-internal SMP (smp_workers) is forced
to 1 on every version that has it, so this measures single-threaded
search speed only -- the --workers parallelism below is purely at the
benchmark-harness level (separate OS processes, each running one
single-threaded cell at a time).

Every fresh run writes to its OWN timestamped results file
(nps_history_results_<unix time.time()>.json) so old runs are never
overwritten -- just re-run the script whenever you want a new sweep and
compare files later.

Each cell records the terms it was run under (seconds_per_run, runs_per_position,
max_depth), not just the averaged numbers -- a depth reading is meaningless
without them (depth 20 in a 5s budget and depth 20 in a 60s budget are not
the same result, and a depth equal to max_depth means the position hit the
hard cap and solved the full tree rather than being time-limited). See
infer_run_config()/run_config_line() to read them back out of a results
file, and the "Config:" line printed above every table.

Usage:
    python3 nps_history_bench.py
        Fresh run, every Old Engine/N/engineN.py found on disk (auto-
        discovered -- all 8 positions, default runs/seconds/workers
        below. Writes nps_history_results_<timestamp>.json and prints
        tables at the end.

    python3 nps_history_bench.py --versions 20-24
        Only test a subset of versions (comma list and/or ranges also
        work, e.g. --versions 1,17,22-24).

    python3 nps_history_bench.py --runs 3 --seconds 2 --workers 4
        Override how many timed runs per position, how long each run is, and
        how many parallel worker processes to use.

    python3 nps_history_bench.py --resume nps_history_results_123.json
        Continue an interrupted run instead of starting a fresh file
        (only re-runs cells missing/errored in that file).

    python3 nps_history_bench.py --list
        List all saved results files (filename, when, how many cells).

    python3 nps_history_bench.py --report
        Print tables from the most recently modified results file, no
        new runs.

    python3 nps_history_bench.py --report nps_history_results_123.json
        Print tables from a specific results file.
"""
import argparse
import concurrent.futures as cf
import glob
import importlib.util
import json
import os
import time
from datetime import datetime

import chess

RUNS_PER_POSITION = 8
SECONDS_PER_RUN = 4.0
MAX_DEPTH = 100         # high enough that time, not depth, is always the limit
WORKERS = 8

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

POSITIONS = [
    ("Startpos",        chess.STARTING_FEN),
    ("Kiwipete",        "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"),
    ("Endgame (K+P)",   "8/8/8/4k3/8/4K3/4P3/8 w - - 0 1"),
    ("Middlegame",      "r2q1rk1/pp2ppbp/2np1np1/2p5/4P3/2NP1N1P/PPP1BPP1/R1BQ1RK1 b - - 0 1"),
    ("Tactical",        "rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 0 1"),
    ("Rook endgame",    "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1"),
    ("Closed/French",   "rnbqkb1r/pp3ppp/4pn2/2pp4/3P4/2P1PN2/PP3PPP/RNBQKB1R w KQkq - 0 1"),
    ("Knight endgame",  "8/8/2n2k2/3p4/3P4/4NK2/8/8 w - - 0 1"),
    ("Mirrored Position", "r4rk1/1pp1qppp/p1np1n2/2b1p1B1/2B1P1b1/P1NP1N2/1PP1QPPP/R4RK1 w - - 0 1"),
    ("Perf Position", "r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1")
]


# The LIVE repo-root cengine.py benches as the next version in the lineage
# (naming scheme): bump this when a new Old Engine/N snapshot freezes the
# previous one. cengine satisfies the same Engine API (nodes_searched/
# last_depth, guarded use_book/use_tb/smp_workers), so every cell/table
# mechanism below works unchanged. Once an Old Engine/<CENGINE_VERSION>
# snapshot exists, engine_path prefers the snapshot (stable) over the live
# file. History: 31 frozen 2026-07-08; 32 (P-03 IIR) 2026-07-08;
# 33 (P-14 TT-warm + SMP fixes) 2026-07-09.
CENGINE_VERSION = 34


def engine_path(v):
    snap = os.path.join("Old Engine", str(v), f"engine{v}.py")
    if v == CENGINE_VERSION and not os.path.isfile(snap):
        return os.path.join(BASE_DIR, "cengine.py")
    return snap


def load_engine_module(v):
    path = engine_path(v)
    modname = f"_bench_engine_{v}"
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fresh_engine(mod):
    e = mod.Engine()
    # Old versions may not have these attributes at all -- guard every one.
    for attr in ("use_book", "use_tb"):
        if hasattr(e, attr):
            setattr(e, attr, False)
    # v19+ default to SMP_WORKERS=4 (Lazy SMP, spawns a persistent worker
    # pool on first timed search). Left alone, this would (a) confound the
    # NPS comparison for v19+ with 4-way parallel search against earlier
    # versions' single-threaded numbers, and (b) risk a multiprocessing
    # spawn error depending on how this script itself is invoked. Forcing
    # smp_workers=1 takes the exact `self.smp_workers > 1` branch in
    # get_best_move_timed that would spawn the pool, so it's a clean,
    # single-threaded path, identical in kind to every pre-SMP version.
    if hasattr(e, "smp_workers"):
        e.smp_workers = 1
    return e


def run_one(mod, fen, seconds_per_run, max_depth):
    """One timed search; returns (nps, depth_reached).
    nps = nodes / actual elapsed. depth_reached = e.last_depth, the last
    fully-completed iterative-deepening depth (present on every version)."""
    e = fresh_engine(mod)
    board = chess.Board(fen)
    t0 = time.perf_counter()
    e.get_best_move_timed(board, seconds_per_run, max_depth)
    elapsed = time.perf_counter() - t0
    nodes = getattr(e, "nodes_searched", 0) or 0
    depth = getattr(e, "last_depth", 0) or 0
    nps = 0.0 if elapsed <= 0 else nodes / elapsed
    return nps, depth


def cell_key(v, pos_name):
    return f"v{v}||{pos_name}"


def run_cell_worker(v, name, fen, runs_per_position, seconds_per_run, max_depth):
    """Runs in a worker process: load engine module for v, run
    runs_per_position timed searches on this position, return
    (v, name, cell_dict). All tuning knobs are passed explicitly (not read
    off module globals) because worker processes re-import this module
    fresh from disk -- a monkeypatched global in the parent process would
    NOT be visible here."""
    path = engine_path(v)
    if not os.path.isfile(path):
        return v, name, {"error": f"{path} not found"}
    try:
        mod = load_engine_module(v)
    except Exception as ex:
        return v, name, {"error": str(ex)}
    runs_nps = []
    runs_depth = []
    try:
        for _ in range(runs_per_position):
            nps, depth = run_one(mod, fen, seconds_per_run, max_depth)
            runs_nps.append(nps)
            runs_depth.append(depth)
    except Exception as ex:
        return v, name, {"error": str(ex)}
    avg = sum(runs_nps) / len(runs_nps)
    avg_depth = sum(runs_depth) / len(runs_depth)
    # Recorded per-cell (not just once in a file-level header) so a --resume
    # that changes --runs/--seconds/--depth-cap can't silently leave stale,
    # mismatched cells in the same file passing as if they used the same
    # terms. This is what makes a depth number interpretable: depth 20 in
    # 5s and depth 20 in 60s are not the same result, and a depth that
    # equals max_depth means the position hit the hard cap (full tree
    # solved) rather than being time-limited -- see MAX_DEPTH's docstring
    # note on the "Opp. bishops EG" outlier for why that distinction matters.
    return v, name, {"runs": runs_nps, "avg": avg,
                      "runs_depth": runs_depth, "avg_depth": avg_depth,
                      "seconds_per_run": seconds_per_run,
                      "runs_per_position": runs_per_position,
                      "max_depth": max_depth}


def load_results(path):
    if path and os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_results(results, path):
    with open(path, "w") as f:
        json.dump(results, f, indent=1)


def new_results_path():
    return os.path.join(BASE_DIR, f"nps_history_results_{int(time.time())}.json")


def all_results_files():
    return sorted(glob.glob(os.path.join(BASE_DIR, "nps_history_results*.json")),
                  key=os.path.getmtime, reverse=True)


def latest_results_file():
    paths = all_results_files()
    return paths[0] if paths else None


def list_results_files():
    paths = all_results_files()
    if not paths:
        print("No results files found.")
        return
    print(f"{'File':<48} {'Modified':<18} {'Cells':>6}")
    for p in paths:
        mtime = datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")
        try:
            n = len(json.load(open(p)))
        except Exception:
            n = "?"
        print(f"{os.path.basename(p):<48} {mtime:<18} {n:>6}")


def parse_versions(spec):
    versions = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-")
            versions.extend(range(int(a), int(b) + 1))
        else:
            versions.append(int(part))
    return sorted(set(versions))


def infer_run_config(results):
    """What terms actually produced these numbers: seconds_per_run,
    runs_per_position, max_depth -- read back from the cells themselves (not a
    single file-level assumption), since a --resume can mix terms within one
    file. For each field: the shared value if every cell agrees, "varies" if
    cells disagree (check individual cells before trusting an aggregate),
    or None if no cell in this file recorded it (an older results file from
    before these fields were added -- e.g. nps_history_results_v1-v22_5run_archive.json)."""
    fields = ("seconds_per_run", "runs_per_position", "max_depth")
    out = {}
    for f in fields:
        vals = [c[f] for c in results.values()
                 if isinstance(c, dict) and "error" not in c and f in c]
        if not vals:
            out[f] = None
        elif len(set(vals)) == 1:
            out[f] = vals[0]
        else:
            out[f] = "varies"
    # Fallback for older files that predate these fields: runs_per_position can
    # still be recovered from how many measurements are in "runs".
    if out["runs_per_position"] is None:
        lens = {len(c["runs"]) for c in results.values()
                 if isinstance(c, dict) and "error" not in c and "runs" in c}
        if len(lens) == 1:
            out["runs_per_position"] = next(iter(lens))
        elif lens:
            out["runs_per_position"] = "varies"
    return out


def run_config_line(results):
    """One human-readable line describing the terms a results file was run
    under, e.g. 'Config: 6 runs x 5.0s/run, max_depth=50'. Depth numbers are
    meaningless without this -- depth 20 reached in a 5s budget is a very
    different result from depth 20 in a 60s budget, and a depth that equals
    max_depth means the position hit the hard cap (full tree solved, not
    time-limited)."""
    cfg = infer_run_config(results)
    if all(v is None for v in cfg.values()):
        return ("Config: not recorded in this file (pre-dates run-config "
                "tracking -- do not compare its depth numbers across files "
                "without checking how it was actually run).")
    def fmt(v, suffix=""):
        if v is None:
            return "unknown"
        if v == "varies":
            return "varies across cells (mixed --resume runs; check per-cell)"
        return f"{v}{suffix}"
    return (f"Config: {fmt(cfg['runs_per_position'])} runs x "
            f"{fmt(cfg['seconds_per_run'], 's/run')}, "
            f"max_depth={fmt(cfg['max_depth'])}")


def versions_in(results):
    vs = set()
    for key in results:
        vs.add(int(key.split("||")[0][1:]))
    return sorted(vs)


def positions_in(results):
    """Position names actually present in a results file -- NOT the live
    POSITIONS list above, which can change over time (e.g. a position got
    swapped out for a better one). Order: current POSITIONS order first
    (for names still in use), then any older/retired names found only in
    this file, so historical files still render their real columns
    instead of silently showing 'n/a' for a renamed position."""
    seen = []
    seen_set = set()
    for key in results:
        name = key.split("||", 1)[1]
        if name not in seen_set:
            seen_set.add(name)
            seen.append(name)
    current_names = {name for name, _ in POSITIONS}
    ordered = [name for name, _ in POSITIONS if name in seen_set]
    extras = [name for name in seen if name not in current_names]
    return ordered + extras


def discover_versions():
    """Scan Old Engine/N/engineN.py for every N present on disk -- so a
    freshly-added snapshot (e.g. Old Engine/25/engine25.py) is picked up
    automatically without editing this script."""
    root = os.path.join(BASE_DIR, "Old Engine")
    found = []
    if not os.path.isdir(root):
        return found
    for entry in os.listdir(root):
        if not entry.isdigit():
            continue
        v = int(entry)
        if os.path.isfile(os.path.join(root, entry, f"engine{v}.py")):
            found.append(v)
    # C search core (v31): included when both its driver and its .so exist
    # (setup.sh builds csearch.so; without it the cell would just FAIL).
    # Skipped if a real Old Engine/31 snapshot was already discovered above.
    if (CENGINE_VERSION not in found
            and os.path.isfile(os.path.join(BASE_DIR, "cengine.py"))
            and os.path.isfile(os.path.join(BASE_DIR, "csearch.so"))):
        found.append(CENGINE_VERSION)
    return sorted(found)


def run_all(versions, positions, runs_per_position, seconds_per_run, max_depth,
            workers, results_path, results):
    total_cells = len(versions) * len(positions)
    todo = []
    for v in versions:
        for name, fen in positions:
            key = cell_key(v, name)
            if key in results and "error" not in results[key]:
                continue   # already done, resuming
            todo.append((v, name, fen))

    done_cells = total_cells - len(todo)
    if (CENGINE_VERSION in versions
            and not os.path.isfile(os.path.join(
                "Old Engine", str(CENGINE_VERSION),
                f"engine{CENGINE_VERSION}.py"))):
        print(f"note: v{CENGINE_VERSION} = cengine.py (C search core), "
              "not snapshotted yet")
    print(f"Results file: {results_path}")
    print(f"{done_cells}/{total_cells} cells already done. "
          f"{len(todo)} to run with {workers} parallel workers "
          f"({runs_per_position} runs x {seconds_per_run}s/run per position).")

    if not todo:
        return results

    # max_tasks_per_child=1 (needs Python 3.11+): a FRESH process per cell.
    # A reused worker that has already dlopened one version's eval_c.so /
    # movegen.so can hand a LATER version's ctypes.CDLL an already-loaded
    # older image (same library name) -- the newer engine then misses newer
    # symbols (e.g. set_positional_params), warns, and silently falls back
    # to the ~2x-slower pure-Python eval, corrupting that cell's NPS/depth.
    # Same bug class as the U-04 in-process bench contamination; process
    # isolation removes it for every version at ~1s spawn cost per cell.
    with cf.ProcessPoolExecutor(max_workers=workers, max_tasks_per_child=1) as ex:
        futures = {ex.submit(run_cell_worker, v, name, fen, runs_per_position,
                              seconds_per_run, max_depth): (v, name)
                   for v, name, fen in todo}
        for fut in cf.as_completed(futures):
            v, name = futures[fut]
            key = cell_key(v, name)
            try:
                _, _, cell = fut.result()
            except Exception as ex:
                cell = {"error": str(ex)}
            results[key] = cell
            save_results(results, results_path)
            if "error" in cell:
                print(f"  [FAIL] v{v} / {name}: {cell['error']}")
            else:
                print(f"  v{v:>2} / {name:<18} avg={cell['avg']:>9,.0f} NPS  "
                      f"depth={cell['avg_depth']:>5.1f}  "
                      f"(runs={['%.0f' % r for r in cell['runs']]}, "
                      f"depths={cell['runs_depth']})")

    return results


# Light backgrounds for the per-position best/worst cells in the HTML report.
# Inline styles render in local markdown viewers (VS Code preview, browsers);
# GitHub sanitizes them, so there the cells show plain -- that's fine.
_HL_BEST = "#d6f5d6"    # light green -- highest in this position (column)
_HL_WORST = "#f9d6d6"   # light red   -- lowest in this position (column)


def _trimmed_mean(vals):
    """Truncated average: mean with the single highest and lowest value
    dropped, so one outlier position (e.g. a depth-capped one) can't dominate
    the aggregate. Falls back to the plain mean when there are too few values
    to trim (< 4)."""
    if not vals:
        return None
    if len(vals) >= 4:
        vals = sorted(vals)[1:-1]
    return sum(vals) / len(vals)


def _per_version_avgs(results, versions, position_names, field):
    version_avgs = {}
    for v in versions:
        vals = []
        for name in position_names:
            cell = results.get(cell_key(v, name))
            if cell and "error" not in cell and field in cell:
                vals.append(cell[field])
        version_avgs[v] = _trimmed_mean(vals)     # truncated average
    return version_avgs


def _col_extremes(results, versions, position_names, field):
    """Per position (column): the highest and lowest value across versions.
    Only returns an entry when >= 2 versions have a value and they differ."""
    hi, lo = {}, {}
    for name in position_names:
        vals = []
        for v in versions:
            c = results.get(cell_key(v, name))
            if c and "error" not in c and field in c:
                vals.append(c[field])
        if len(vals) >= 2 and max(vals) != min(vals):
            hi[name], lo[name] = max(vals), min(vals)
    return hi, lo


def _breakdown_table(results, versions, position_names, title, field, valfmt, fmt):
    """One version x position table. fmt='html' emits an HTML table with the
    per-column best cell shaded green and worst shaded red; fmt='md' emits a
    plain markdown pipe table (no colour -- for the terminal)."""
    head = f"## Full {title} (version x position)\n"
    if fmt == "html":
        hi, lo = _col_extremes(results, versions, position_names, field)
        rows = ["<table>",
                "<tr><th>Version</th>"
                + "".join(f"<th>{n}</th>" for n in position_names) + "</tr>"]
        for v in versions:
            tds = [f"<td>v{v}</td>"]
            for name in position_names:
                c = results.get(cell_key(v, name))
                if c is None:
                    tds.append("<td>n/a</td>")
                elif "error" in c:
                    tds.append("<td>ERR</td>")
                elif field not in c:
                    tds.append("<td>n/a</td>")
                else:
                    val = c[field]
                    bg = ""
                    if name in hi and val == hi[name]:
                        bg = f' style="background:{_HL_BEST}"'
                    elif name in lo and val == lo[name]:
                        bg = f' style="background:{_HL_WORST}"'
                    tds.append(f"<td{bg}>{valfmt(val)}</td>")
            rows.append("<tr>" + "".join(tds) + "</tr>")
        rows.append("</table>")
        return head + "\n".join(rows)

    rows = ["| Version | " + " | ".join(position_names) + " |",
            "|---" * (len(position_names) + 1) + "|"]
    for v in versions:
        row = [f"v{v}"]
        for name in position_names:
            c = results.get(cell_key(v, name))
            if c is None:
                row.append("n/a")
            elif "error" in c:
                row.append("ERR")
            elif field not in c:
                row.append("n/a")
            else:
                row.append(valfmt(c[field]))
        rows.append("| " + " | ".join(row) + " |")
    return head + "\n".join(rows)


def _summary_table(version_avgs, versions, label, unit_fmt, delta_fmt):
    lines = [f"\n## Per-version {label} summary\n",
             f"*Avg = **truncated mean** across positions (the single highest "
             f"and lowest position dropped) so one outlier can't dominate.*\n",
             f"| Version | Avg {label} (trimmed) | Δ vs previous | Δ vs first version |",
             "|---|---|---|---|"]
    first_avg = next((version_avgs[v] for v in versions if version_avgs.get(v)), None)
    prev_avg = None
    for v in versions:
        avg = version_avgs.get(v)
        if avg is None:
            lines.append(f"| v{v} | n/a | - | - |")
            continue
        delta_prev = "-" if (prev_avg is None or prev_avg == 0) else delta_fmt(avg, prev_avg)
        if first_avg is None or first_avg == 0 or avg is first_avg:
            delta_first = "-"
        else:
            delta_first = delta_fmt(avg, first_avg)
        lines.append(f"| v{v} | {unit_fmt(avg)} | {delta_prev} | {delta_first} |")
        prev_avg = avg
    return "\n".join(lines)


def build_tables(results, versions=None, position_names=None, fmt="md"):
    """Assemble the breakdown + summary tables. fmt='md' (default) is plain
    markdown for the terminal; fmt='html' shades the per-position best (green)
    / worst (red) cell in the two breakdown tables for the .md report."""
    if versions is None:
        versions = versions_in(results)
    if position_names is None:
        position_names = positions_in(results)

    lines = []
    if fmt == "html":
        lines.append(
            '*Per position (column): '
            f'<span style="background:{_HL_BEST}">&nbsp;highest&nbsp;</span> and '
            f'<span style="background:{_HL_WORST}">&nbsp;lowest&nbsp;</span> '
            'value across versions is shaded.*\n')

    lines.append(_breakdown_table(results, versions, position_names,
                                  "NPS breakdown", "avg",
                                  lambda x: f"{x:,.0f}", fmt))

    has_depth = any("avg_depth" in c for c in results.values() if "error" not in c)
    if has_depth:
        lines.append("")
        lines.append(_breakdown_table(results, versions, position_names,
                                      "depth-reached breakdown", "avg_depth",
                                      lambda x: f"{x:.1f}", fmt))

    nps_avgs = _per_version_avgs(results, versions, position_names, "avg")
    lines.append(_summary_table(
        nps_avgs, versions, "NPS",
        unit_fmt=lambda x: f"{x:,.0f}",
        delta_fmt=lambda cur, ref: f"{(cur - ref) / ref * 100:+.1f}%"))

    if has_depth:
        depth_avgs = _per_version_avgs(results, versions, position_names, "avg_depth")
        lines.append(_summary_table(
            depth_avgs, versions, "Depth",
            unit_fmt=lambda x: f"{x:.2f}",
            delta_fmt=lambda cur, ref: f"{cur - ref:+.2f} ply"))

    return "\n".join(lines)


def parse_args():
    p = argparse.ArgumentParser(
        description="NPS + search-depth benchmark across engine version snapshots.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    p.add_argument("--report", nargs="?", const="__latest__", default=None,
                    metavar="FILE",
                    help="Print tables from an existing results file (default: most "
                         "recently modified one) instead of running anything.")
    p.add_argument("--resume", metavar="FILE",
                    help="Continue an existing (possibly interrupted) results file "
                         "instead of starting a fresh timestamped one.")
    p.add_argument("--list", action="store_true",
                    help="List all saved results files and exit.")
    p.add_argument("--versions", default=None,
                    help="Version range/list to test, e.g. '1-24' or '1,17,22-24'. "
                         "Default: auto-discover every Old Engine/N/engineN.py on "
                         "disk (so a newly-added snapshot is picked up with no "
                         "flag needed).")
    p.add_argument("--runs", type=int, default=RUNS_PER_POSITION,
                    help=f"Timed runs per position, per version. Default: {RUNS_PER_POSITION}.")
    p.add_argument("--seconds", type=float, default=SECONDS_PER_RUN,
                    help=f"Seconds per timed run. Default: {SECONDS_PER_RUN}.")
    p.add_argument("--workers", type=int, default=WORKERS,
                    help=f"Parallel worker processes. Default: {WORKERS}.")
    return p.parse_args()


def main():
    args = parse_args()

    if args.list:
        list_results_files()
        return

    if args.report is not None:
        path = latest_results_file() if args.report == "__latest__" else args.report
        if path is None or not os.path.exists(path):
            print(f"No results file found ({path or 'none saved yet'}).")
            return
        results = load_results(path)
        print(f"Reporting from {path}")
        print(run_config_line(results) + "\n")
        print(build_tables(results))
        return

    versions = parse_versions(args.versions) if args.versions else discover_versions()
    if not versions:
        print("No Old Engine/N/engineN.py snapshots found -- nothing to run.")
        return

    if args.resume:
        results_path = args.resume
        results = load_results(results_path)
    else:
        results_path = new_results_path()
        results = {}

    results = run_all(versions, POSITIONS, args.runs, args.seconds, MAX_DEPTH,
                       args.workers, results_path, results)
    print("\n\n" + run_config_line(results))
    print(build_tables(results, versions))


if __name__ == "__main__":
    main()
