#!/usr/bin/env python3
"""HTTP bridge: Tampermonkey userscript -> local pygin UCI engine.

Run:  python3 pygin_server.py
Test: curl -s -X POST http://127.0.0.1:8181 -d '{"fen":"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w","depth":10}'
"""
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

import chess

ENGINE = "/Users/sam/Desktop/bot/NeuerOrdner/ClaudeChess/dist/pygin"

# cwd = repo root so the engine finds Perfect2023.bin (book lookup searches
# the working directory) no matter where the server is launched from.
# start_new_session: own process group so terminal Ctrl+C hits only us, not
# the engine (otherwise pygin dumps its own KeyboardInterrupt traceback).
eng = subprocess.Popen([ENGINE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       text=True, bufsize=1, start_new_session=True,
                       cwd="/Users/sam/Desktop/bot/NeuerOrdner/ClaudeChess")


def send(cmd):
    print(">>", cmd, flush=True)
    eng.stdin.write(cmd + "\n")
    eng.stdin.flush()


def wait_for(prefix):
    while True:
        line = eng.stdout.readline()
        if not line:
            raise RuntimeError("engine died")
        print("<<", line.rstrip(), flush=True)
        if line.startswith(prefix):
            return line.strip()


def normalize_moves(moves_str):
    """Replay the UCI history on a board and repair a promotion that lost its
    piece char -- lichess's move feed (msg.d.uci) sometimes hands the bridge a
    bare 'a7a8' for what was really 'a7a8q'. Left as-is, that reaches the
    engine, whose all-or-nothing `position` guard rejects it, the engine keeps
    the STALE pre-promotion board, and the game gets stuck re-answering the
    same move (observed 2026-07-11). A bare from-to pawn move to the last rank
    is auto-completed to the matching legal promotion (queen first -- the
    intent in ~every case, and exactly what the engine had chosen). If a token
    is genuinely illegal, return the input unchanged and let the engine's guard
    be the backstop."""
    b = chess.Board()
    out = []
    fixed = False
    for tok in moves_str.split():
        mv = None
        try:
            cand = chess.Move.from_uci(tok)
            if cand in b.legal_moves:
                mv = cand
        except ValueError:
            pass
        if mv is None and len(tok) == 4:       # maybe a promo missing its piece
            for p in "qnrb":
                try:
                    c2 = chess.Move.from_uci(tok + p)
                except ValueError:
                    continue
                if c2 in b.legal_moves:
                    mv = c2
                    fixed = True
                    break
        if mv is None:
            return moves_str                   # truly illegal: engine guard handles it
        out.append(mv.uci())
        b.push(mv)
    if fixed:
        print("   (repaired a promotion missing its piece char)",
              file=sys.stderr, flush=True)
    return " ".join(out)


def full_fen(fen):
    """Lichess ws FENs are board+side only; pad to 6 fields.
    ponytail: castling rights guessed from start squares — wrong only if
    king/rook moved away and back (rare); ep square always '-' (misses
    rare en-passant best moves)."""
    parts = fen.split()
    if len(parts) >= 6:
        return fen
    board, side = parts[0], parts[1]
    rows = board.split("/")

    def expand(row):  # digits -> dots so index = file
        return "".join("." * int(c) if c.isdigit() else c for c in row)

    white, black = expand(rows[7]), expand(rows[0])
    castle = ""
    if white[4] == "K":
        if white[7] == "R": castle += "K"
        if white[0] == "R": castle += "Q"
    if black[4] == "k":
        if black[7] == "r": castle += "k"
        if black[0] == "r": castle += "q"
    return "%s %s %s - 0 1" % (board, side, castle or "-")


send("uci")
wait_for("uciok")
send("setoption name OwnBook value true")
send("setoption name Threads value 4")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        # Prefer full history (startpos + moves) so the engine sees repetitions
        # and can avoid/claim threefold draws; FEN loses that context.
        if "moves" in req:
            mv = normalize_moves(req["moves"]) if req["moves"] else ""
            send("position startpos" + (" moves " + mv if mv else ""))
        else:
            send("position fen " + full_fen(req["fen"]))
        if req.get("movetime", 0) > 0:
            send("go movetime %d" % req["movetime"])
        else:
            send("go depth %d" % req.get("depth", 12))
        best = wait_for("bestmove").split()[1]
        body = json.dumps({"bestmove": best}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("pygin bridge on http://127.0.0.1:8118")
    srv = HTTPServer(("127.0.0.1", 8118), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        srv.server_close()
        eng.terminate()
