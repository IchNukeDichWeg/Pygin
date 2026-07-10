#!/usr/bin/env python3
"""HTTP bridge: Tampermonkey userscript -> local pygin UCI engine.

Run:  python3 pygin_server.py
Test: curl -s -X POST http://127.0.0.1:8181 -d '{"fen":"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w","depth":10}'
"""
import json
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

ENGINE = "/Users/sam/Desktop/bot/NeuerOrdner/ClaudeChess/dist/pygin"

eng = subprocess.Popen([ENGINE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       text=True, bufsize=1)


def send(cmd):
    eng.stdin.write(cmd + "\n")
    eng.stdin.flush()


def wait_for(prefix):
    while True:
        line = eng.stdout.readline()
        if not line:
            raise RuntimeError("engine died")
        if line.startswith(prefix):
            return line.strip()


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


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        send("position fen " + full_fen(req["fen"]))
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
    print("pygin bridge on http://127.0.0.1:8181")
    HTTPServer(("127.0.0.1", 8181), Handler).serve_forever()
