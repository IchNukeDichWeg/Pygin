"""
uci_engine.py
=============
A GENERIC UCI opponent wearing the project's ``Engine`` interface, so any
engine binary -- not just Stockfish -- can play in ``match.py`` / ``odds.py``.

Why it exists: ``UCI_Elo`` is not a rating. Stockfish weakens itself by
choosing deliberately worse moves, and the number it takes is self-declared,
calibrated against nothing. An engine with a published CCRL rating is anchored
to a real pool of games, so a match against one places us on that scale.

    # an anchor lives in opponents/<name>.py and sets BINARY/OPTIONS:
    python3 match.py dist/pygin opponents/anchor_example.py 500 0 \
        --tc 10+0.1 --workers $(($(nproc)/2))

WHAT AN ANCHOR IS NOT. The result is "our strength on the CCRL scale, under
OUR conditions", never a CCRL rating. Those lists run their own hardware, book,
time control and opening set; every one of those differs here. Say the
conditions next to the number or it will be read as something it is not.

CONFIGURATION IS IN-FILE, NEVER AN ENVIRONMENT VARIABLE (house rule): an
opponent module sets BINARY, NAME, RATING, RATING_LIST and OPTIONS as module
constants, and `match.py` records the engine's own `id name` in the campaign
state, so a swapped binary shows up as a CONFIG-CHANGED diff rather than a
silently re-based yardstick.

Subclasses stockfish_engine.Engine for the UCI plumbing (send/read, respawn on
crash, info parsing, the clock-aware `go wtime/btime` path) and replaces only
the handshake: an arbitrary engine gets Threads and Hash plus whatever OPTIONS
the anchor names, and none of Stockfish's strength-limiting options.
"""

import os
import shutil
import subprocess

import stockfish_engine

# --- what this module plays, when used directly -------------------------- #
# An anchor module normally sets these instead (see opponents/README.md).
BINARY = ""             # path to the UCI binary; "" = must be set by the anchor
NAME = ""               # label for logs; "" = ask the engine for its id name
RATING = None           # the anchor's published rating, e.g. 3100
RATING_LIST = ""        # WHICH list that rating is from, e.g. "CCRL Blitz 2+1"
                        # (a rating without its list is not a measurement)
OPTIONS = {}            # extra UCI options, e.g. {"EvalFile": "net.nnue"}
THREADS = 1             # CCRL lists are single-threaded; match that
HASH_MB = 64


def resolve_binary(path=None):
    """Absolute path to the engine binary, or raise with a usable message."""
    p = path or BINARY
    if not p:
        raise RuntimeError("uci_engine: BINARY is not set -- an anchor module "
                           "must set it (see opponents/README.md)")
    p = os.path.expanduser(p)
    if os.path.isfile(p):
        return os.path.abspath(p)
    found = shutil.which(p)
    if found:
        return found
    raise RuntimeError(f"uci_engine: no such engine binary: {p}")


def id_name(path=None, timeout=10.0):
    """The engine's OWN version string. Provenance, so a swap is visible.

    Never raises: a provenance field must not be able to stop a run."""
    try:
        p = subprocess.run([resolve_binary(path)], input="uci\nquit\n",
                           capture_output=True, text=True, timeout=timeout)
        for line in p.stdout.splitlines():
            if line.startswith("id name "):
                return line[len("id name "):].strip()
    except Exception:
        pass
    return None


class Engine(stockfish_engine.Engine):
    """Any UCI binary, wearing the project's Engine interface."""

    # An anchor subclass overrides these; the module constants are the default.
    BINARY = None
    OPTIONS = None
    THREADS = None
    HASH_MB = None

    def _cfg(self, attr, fallback):
        v = getattr(self, attr, None)
        return fallback if v is None else v

    def _spawn(self):
        self._proc = subprocess.Popen(
            [resolve_binary(self._cfg("BINARY", BINARY))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self._uci_handshake()

    def _uci_handshake(self):
        self._send("uci")
        opts = self._read_until("uciok")
        # Only set what the engine advertises: an unknown setoption makes some
        # engines print noise, and a few treat it as a fatal error.
        advertised = {ln.split("name ", 1)[1].split(" type ")[0]
                      for ln in opts if ln.startswith("option name ")
                      and " type " in ln}
        self._has_wdl = "UCI_ShowWDL" in advertised
        if self._has_wdl:
            self._send("setoption name UCI_ShowWDL value true")
        if "Threads" in advertised:
            self._send(f"setoption name Threads value "
                       f"{int(self._cfg('THREADS', THREADS))}")
        if "Hash" in advertised:
            self._send(f"setoption name Hash value "
                       f"{int(self._cfg('HASH_MB', HASH_MB))}")
        for k, v in (self._cfg("OPTIONS", OPTIONS) or {}).items():
            if k in advertised:
                self._send(f"setoption name {k} value {v}")
            else:
                # Loud, not silent: an anchor that wanted a net file and did not
                # get one is a different opponent than the rating describes.
                print(f"!! uci_engine: {k} is not an option of this engine "
                      f"-- NOT set", flush=True)
        self._send("isready")
        self._read_until("readyok")
        self._send("ucinewgame")
