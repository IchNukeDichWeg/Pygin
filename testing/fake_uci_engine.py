#!/usr/bin/env python3
"""fake_uci_engine.py -- a scripted UCI binary for testing the UCI adapters.

stockfish_engine.py / uci_engine.py drive real engines that are not installed
on every box, so their protocol handling had no test -- which is how a 960
change once crashed every anchor in STANDARD chess (uci_engine's _spawn never
set the flag stockfish_engine's _go read). This stand-in speaks just enough
UCI for selftest to drive those adapters end to end, with no dependencies.

Its answers reveal which rule set the PROCESS is in, so a test can tell
whether the adapter armed UCI_Chess960 after every (re)spawn:

    RK5R back rank (b1 king, a1/h1 rooks):  b1a1 with UCI_Chess960 on (the
                                            a-side castle), b1c1 with it off
                                            (what a real engine prints for the
                                            same castle in standard mode -- a
                                            legal but DIFFERENT move on a 960
                                            board)
    any other position:                     g1f3 with UCI_Chess960 on, e2e4 off

FAKE_UCI_NO960=1 in the environment makes it NOT advertise UCI_Chess960.
"""
import os
import sys


def main():
    advertise_960 = os.environ.get("FAKE_UCI_NO960") != "1"
    mode960 = False
    fen = ""
    for line in sys.stdin:
        tok = line.split()
        if not tok:
            continue
        cmd = tok[0]
        if cmd == "uci":
            print("id name FakeUCI")
            print("option name Hash type spin default 16 min 1 max 1024")
            if advertise_960:
                print("option name UCI_Chess960 type check default false")
            print("uciok")
        elif cmd == "isready":
            print("readyok")
        elif cmd == "setoption" and "UCI_Chess960" in tok:
            mode960 = tok[-1].lower() == "true"
        elif cmd == "position":
            fen = " ".join(tok[2:]) if len(tok) > 2 and tok[1] == "fen" else ""
        elif cmd == "go":
            if fen.split(" ")[0].endswith("/RK5R"):
                mv = "b1a1" if mode960 else "b1c1"
            else:
                mv = "g1f3" if mode960 else "e2e4"
            print(f"info depth 1 score cp 0 nodes 1 pv {mv}")
            print(f"bestmove {mv}")
        elif cmd == "quit":
            break
        sys.stdout.flush()


if __name__ == "__main__":
    main()
