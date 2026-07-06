"""
profile_bench.py -- real NPS + function-time bottlenecks in ONE fast pass.

Runs fixed-depth searches on the same 10-position suite as
nps_history_bench.py, with a low-overhead SAMPLING profiler (a background
thread snapshots the search stack every ~0.5 ms). Because it samples instead
of instrumenting every call, the NPS you measure is real (~full speed) AND
you get a per-function time breakdown at the same time -- unlike cProfile,
which ~halves NPS.

    python3 profile_bench.py                # NPS table + hottest functions
    python3 profile_bench.py --depth 7      # deeper => more samples, steadier
    python3 profile_bench.py --graph        # + write profile_report.html (full speed)
    python3 profile_bench.py --graph --open # ...and open it in the browser
    python3 profile_bench.py --top 40       # list more functions
    python3 profile_bench.py --cprofile     # exact CALL COUNTS (slow; NPS invalid)

Node counts are seeded (42), book/TB off -> reproducible run-to-run, and MUST
stay identical across a byte-identical change (that's the correctness gate;
the NPS delta is the measurement). Use --cprofile only when you need exact
call counts (e.g. "how many dict.get calls") -- it instruments every call so
its NPS numbers are meaningless.
"""
import argparse
import collections
import datetime
import html
import signal
import subprocess
import sys
import time

import chess

import engine as engine_mod

# Same suite as nps_history_bench.py (keep in sync).
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
    ("Perf Position",   "r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1"),
]

# Endgames explode in depth cheaply; give them a couple extra plies so their
# share isn't negligible. Everything else uses --depth.
DEPTH_BONUS = {"Endgame (K+P)": 4, "Knight endgame": 3, "Rook endgame": 2}


def fresh_engine():
    e = engine_mod.Engine()
    e.use_book = False
    e.use_tb = False
    e.smp_workers = 1
    return e


# ---------------------------------------------------------------------- #
# Sampling profiler: an interval timer (ITIMER_PROF) fires SIGPROF on the
# main thread every `interval` seconds; the handler snapshots the frame that
# was interrupted. ITIMER_PROF counts CPU time, and the signal interrupts
# the main thread directly -- so, unlike a GIL-scheduled background thread,
# it doesn't over-sample the ctypes calls that release the GIL. Overhead is
# a tiny Python handler a few hundred times/sec, so measured NPS stays real.
# Time spent inside eval_c.so / movegen.so is attributed to the Python
# function that called it (the frame we return into) -- exactly the hot
# call site you'd want to optimise.
# ---------------------------------------------------------------------- #
class Sampler:
    def __init__(self, interval=0.001):
        self.interval = interval
        self.self_hits = collections.Counter()   # interrupted (leaf) frame
        self.cum_hits = collections.Counter()     # every frame in the stack
        self.samples = 0

    def _handler(self, signum, frame):
        self.samples += 1
        c = frame.f_code
        self.self_hits[(c.co_filename, c.co_firstlineno, c.co_name)] += 1
        seen = set()
        f = frame
        while f is not None:
            c = f.f_code
            k = (c.co_filename, c.co_firstlineno, c.co_name)
            if k not in seen:                 # count each function once/stack
                self.cum_hits[k] += 1
                seen.add(k)
            f = f.f_back

    def __enter__(self):
        signal.signal(signal.SIGPROF, self._handler)
        signal.setitimer(signal.ITIMER_PROF, self.interval, self.interval)
        return self

    def __exit__(self, *a):
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.signal(signal.SIGPROF, signal.SIG_IGN)

    def ranked(self, n):
        """[(label, self_frac, self_hits, cum_frac)] by own time."""
        tot = self.samples or 1
        rows = []
        for key, sh in self.self_hits.most_common(n):
            fn, line, name = key
            base = fn.rsplit("/", 1)[-1]
            rows.append((f"{base}:{line}({name})", sh / tot,
                         sh, self.cum_hits[key] / tot))
        return rows


def search_suite(depth, sampler=None):
    rows = []
    for name, fen in POSITIONS:
        e = fresh_engine()
        import random
        random.seed(42)
        board = chess.Board(fen)
        d = depth + DEPTH_BONUS.get(name, 0)
        t0 = time.perf_counter()
        e.get_best_move(board, d)
        dt = time.perf_counter() - t0
        rows.append((name, d, e.nodes_searched, dt))
    return rows


def print_nps(rows, header):
    print(header)
    print(f"  {'position':<20} {'depth':>5} {'nodes':>12} {'time':>8} {'kNPS':>8}")
    tn = tt = 0
    for name, d, nodes, dt in rows:
        tn += nodes
        tt += dt
        print(f"  {name:<20} {d:>5} {nodes:>12,} {dt:>7.2f}s {nodes/dt/1000:>8.1f}")
    print(f"  {'TOTAL':<20} {'':>5} {tn:>12,} {tt:>7.2f}s {tn/tt/1000:>8.1f}")
    return tn, tt


def print_funcs(sampler, wall, top):
    print(f"\n=== HOTTEST FUNCTIONS by OWN time ({sampler.samples:,} samples "
          f"@ {sampler.interval*1000:g}ms) ===")
    print(f"  {'own%':>6} {'own s':>7} {'cum%':>6}  function")
    for label, self_frac, _sh, cum_frac in sampler.ranked(top):
        print(f"  {100*self_frac:>5.1f}% {self_frac*wall:>6.2f}s "
              f"{100*cum_frac:>5.1f}%  {label}")
    if sampler.samples < 200:
        print("  (few samples -- run --depth 7+ for a steadier ranking)")


# ---------------------------------------------------------------------- #
# HTML report (--graph): no dependencies, opens in the browser.
# ---------------------------------------------------------------------- #
def _bars(items, unit, color):
    vmax = max((v for _, v, _ in items), default=1) or 1
    out = []
    for label, v, note in items:
        pct = 100.0 * v / vmax
        out.append(
            f'<div class="row"><div class="lbl" title="{html.escape(label)}">'
            f'{html.escape(label)}</div><div class="track"><div class="bar" '
            f'style="width:{pct:.1f}%;background:{color}"></div></div>'
            f'<div class="val">{v:,.2f}{unit}</div>'
            f'<div class="note">{html.escape(note)}</div></div>')
    return "\n".join(out)


def write_html(path, depth, rows, nps_sub, func_items, func_sub):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tn = sum(r[2] for r in rows)
    tt = sum(r[3] for r in rows)
    nps_items = [(name, nodes / dt / 1000, f"d{d} · {nodes:,} nodes · {dt:.2f}s")
                 for name, d, nodes, dt in rows]
    body = f"""
<h2>NPS per position <span class="sub">(depth {depth}, seed 42, book/TB off
&mdash; total {tn:,} nodes in {tt:.2f}s = {tn/tt/1000:,.1f} kNPS; {nps_sub})
</span></h2>
{_bars(nps_items, ' k', '#4c9be8')}
<h2>Hottest functions by OWN time <span class="sub">({func_sub})</span></h2>
{_bars(func_items, 's', '#e8734c')}"""
    open(path, "w", encoding="utf-8").write(f"""<!doctype html>
<meta charset="utf-8"><title>profile_bench {stamp}</title>
<style>
 body {{ font: 14px -apple-system, sans-serif; max-width: 1080px;
        margin: 2rem auto; color: #222; }}
 h1 {{ font-size: 1.3rem }} h2 {{ font-size: 1.05rem; margin-top: 2rem }}
 .sub {{ font-weight: normal; color: #777; font-size: .85rem }}
 .row {{ display: flex; align-items: center; gap: .6rem; margin: 3px 0 }}
 .lbl {{ flex: 0 0 320px; text-align: right; font-family: ui-monospace, monospace;
        font-size: .8rem; white-space: nowrap; overflow: hidden;
        text-overflow: ellipsis }}
 .track {{ flex: 1; background: #eee; border-radius: 3px }}
 .bar {{ height: 14px; border-radius: 3px }}
 .val {{ flex: 0 0 90px; font-family: ui-monospace, monospace; font-size: .8rem }}
 .note {{ flex: 0 0 200px; color: #888; font-size: .75rem }}
</style>
<h1>profile_bench &mdash; {stamp}</h1>
{body}
""")


def report_written(path, do_open):
    print(f"graph written: {path}")
    if do_open and sys.platform == "darwin":
        subprocess.run(["open", path], check=False)   # only with --open


def run_cprofile(depth, top, graph, do_open):
    import cProfile
    import pstats
    print("cProfile mode: exact call counts, but NPS is ~2x low (every call "
          "instrumented) -- rank/count functions, don't quote speeds.\n")
    prof = cProfile.Profile()
    prof.enable()
    rows = search_suite(depth)
    prof.disable()
    print_nps(rows, "Searches profiled (times inflated by cProfile):")
    st = pstats.Stats(prof)
    st.strip_dirs()
    print(f"\n=== TOP {top} by OWN time (tottime) with CALL COUNTS ===")
    st.sort_stats("tottime").print_stats(top)

    if graph:
        # Build the function bars from cProfile stats: own seconds (tottime),
        # with the CALL COUNT + cumulative time in the note (cProfile's edge).
        ranked = sorted(st.stats.items(), key=lambda kv: -kv[1][2])[:25]
        func_items = []
        for (fn, line, name), (cc, nc, tt, ct, _c) in ranked:
            base = fn.rsplit("/", 1)[-1] if fn != "~" else fn
            func_items.append((f"{base}:{line}({name})", tt,
                               f"{nc:,} calls · cum {ct:.2f}s"))
        write_html("profile_report.html", depth, rows,
                   "times inflated ~2x by cProfile", func_items,
                   "cProfile: exact tottime + CALL COUNTS; wall speeds not comparable")
        report_written("profile_report.html", do_open)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--depth", type=int, default=6,
                    help="base search depth (default 6; endgames get a bonus)")
    ap.add_argument("--top", type=int, default=25,
                    help="functions to list (default 25)")
    ap.add_argument("--graph", action="store_true",
                    help="write profile_report.html (NPS + function bars)")
    ap.add_argument("--open", action="store_true",
                    help="also open the report in the browser (default: just write it)")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="sampler interval in ms (default 1.0)")
    ap.add_argument("--cprofile", action="store_true",
                    help="exact call-count mode (slow; NPS invalid)")
    args = ap.parse_args()

    if args.cprofile:
        run_cprofile(args.depth, args.top, args.graph, args.open)
        return

    # Sampling path: real NPS + function time in one pass (SIGPROF timer).
    if not hasattr(signal, "setitimer"):
        print("SIGPROF sampling unavailable on this platform; "
              "use --cprofile instead.", file=sys.stderr)
        return
    with Sampler(args.interval / 1000.0) as smp:
        rows = search_suite(args.depth)

    _, wall = print_nps(rows, f"NPS pass (depth {args.depth}, seed 42, "
                              f"book/TB off -- real speed, sampled)")
    print_funcs(smp, wall, args.top)
    print("\nnode counts must be IDENTICAL across a byte-identical change; "
          "NPS delta is the measurement.")
    if args.graph:
        func_items = [(label, self_frac * wall, f"{100*self_frac:.1f}% own · "
                       f"{100*cum_frac:.1f}% cum")
                      for label, self_frac, _sh, cum_frac in smp.ranked(25)]
        write_html("profile_report.html", args.depth, rows,
                   f"real speed, {smp.samples:,} samples", func_items,
                   f"sampling profiler, {smp.samples:,} samples -- "
                   "estimated seconds in each function itself")
        report_written("profile_report.html", args.open)


if __name__ == "__main__":
    main()
