#!/usr/bin/env python3
"""uci/module_uci.py -- minimal UCI front end for ANY engine module.

    python3 uci/module_uci.py NNUE/shims/engine_v13s1.py

cuci.py is hard-wired to the live cengine, so a probe could only ever score the
shipped net. This exposes whichever module it is given -- a shim, a snapshot --
so a UCI-driven tool (bench/imbalance_probe.py) can score the exact
configuration an A/B measured, which is what calibrating a proxy against Elo
requires.

Deliberately minimal: uci, isready, ucinewgame, position fen/startpos [moves],
go depth N, quit. No clock, no ponder, no options. Book and tablebases off and
one search thread, matching bench/bench_progress.py, so two modules differ only
in what the modules themselves change. Scores are reported side-to-move POV as
UCI requires; the engine keeps them White POV internally.

One module per process, always: every shim loads the same csearch .so, and two
builds in one process silently run the first one's code.
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path[:0] = [ROOT, os.path.join(ROOT, "lib")]
import chess


def load(path):
    spec = importlib.util.spec_from_file_location("_uci_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fresh_engine(mod):
    e = mod.Engine()
    for attr in ("use_book", "use_tb"):
        if hasattr(e, attr):
            setattr(e, attr, False)
    if hasattr(e, "smp_workers"):
        e.smp_workers = 1
    return e


def main():
    if len(sys.argv) < 2:
        print("usage: module_uci.py <engine module .py>", file=sys.stderr)
        return 2
    mod = load(sys.argv[1])
    eng = fresh_engine(mod)
    board = chess.Board()
    out = sys.stdout
    for line in sys.stdin:
        t = line.split()
        if not t:
            continue
        cmd = t[0]
        if cmd == "uci":
            out.write(f"id name {os.path.basename(sys.argv[1])}\nuciok\n")
        elif cmd == "isready":
            out.write("readyok\n")
        elif cmd == "ucinewgame":
            eng = fresh_engine(mod)
        elif cmd == "position":
            if len(t) > 1 and t[1] == "startpos":
                board = chess.Board()
                rest = t[2:]
            else:
                i = t.index("moves") if "moves" in t else len(t)
                board = chess.Board(" ".join(t[2:i]))
                rest = t[i:]
            if rest and rest[0] == "moves":
                for mv in rest[1:]:
                    board.push_uci(mv)
        elif cmd == "go":
            depth = int(t[t.index("depth") + 1]) if "depth" in t else 8
            mv = eng.get_best_move(board, depth)
            white = int(getattr(eng, "last_score", 0) or 0)
            stm = white if board.turn == chess.WHITE else -white
            out.write(f"info depth {depth} score cp {stm} "
                      f"nodes {getattr(eng, 'nodes_searched', 0)} "
                      f"pv {mv.uci() if mv else '0000'}\n")
            out.write(f"bestmove {mv.uci() if mv else '0000'}\n")
        elif cmd == "quit":
            break
        out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
