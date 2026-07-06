#!/usr/bin/env bash
# ======================================================================
# Pygin one-shot setup: make sure python3, a C compiler and stockfish are
# present (install them if missing), then build the C libraries so you can
# immediately run a headless match. Safe to re-run.
#
#     ./setup.sh
#
# Works on macOS (Homebrew) and Linux (apt / dnf / pacman / zypper).
# ======================================================================
set -euo pipefail
cd "$(dirname "$0")"

echo "== Pygin setup =="

have () { command -v "$1" >/dev/null 2>&1; }

# --- 0. detect OS + package manager ----------------------------------- #
OS="$(uname -s)"
PM=""            # how to install a package: "$PM <name>"
case "$OS" in
  Darwin)
    if ! have brew; then
        echo "-> Homebrew not found, installing it (non-interactive) ..."
        NONINTERACTIVE=1 /bin/bash -c \
          "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
          || echo "   (brew install failed -- install it from https://brew.sh and re-run)"
        # add brew to PATH for this session (Apple Silicon vs Intel prefix)
        for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
            [ -x "$b" ] && eval "$("$b" shellenv)" && break
        done
    fi
    have brew && PM="brew install" \
      || echo "!! still no brew; install python3 / a compiler / stockfish yourself." ;;
  Linux)
    if   have apt-get; then PM="sudo apt-get install -y"
    elif have dnf;     then PM="sudo dnf install -y"
    elif have pacman;  then PM="sudo pacman -S --noconfirm"
    elif have zypper;  then PM="sudo zypper install -y"
    else echo "!! No known package manager found; install deps manually if missing."; fi ;;
  MINGW*|MSYS*|CYGWIN*)
    echo "!! Native Windows shell detected. This project builds a Unix .so and is"
    echo "   meant to run under WSL (Ubuntu) or Git Bash+MSYS. Recommended: install"
    echo "   WSL ('wsl --install' in PowerShell), then run ./setup.sh inside it."
    have pacman && PM="pacman -S --noconfirm" ;;   # MSYS2, best effort
  *)
    echo "!! Unknown OS '$OS'; install python3 / a compiler / stockfish yourself." ;;
esac

ensure () {   # ensure <command> <package>  -> install <package> if <command> missing
    local cmd="$1" pkg="$2"
    if have "$cmd"; then echo "-> $cmd: present ($(command -v "$cmd"))"; return 0; fi
    echo "-> $cmd: missing, installing '$pkg' ..."
    if [ -n "$PM" ]; then $PM "$pkg" || echo "   (install of '$pkg' failed -- do it manually)"
    else echo "   (no package manager -- install '$pkg' manually)"; fi
}

# --- 1. python3 ------------------------------------------------------- #
ensure python3 python3
have python3 || { echo "ERROR: python3 still not available"; exit 1; }
echo "   $(python3 --version)"

# --- 2. C compiler (clang or gcc) ------------------------------------- #
if have clang || have cc || have gcc; then
    echo "-> C compiler: present"
else
    if [ "$OS" = "Darwin" ]; then
        echo "-> C compiler: missing, launching Xcode command-line tools installer ..."
        xcode-select --install 2>/dev/null || true
        echo "   Finish that GUI install, then re-run ./setup.sh"
        exit 1
    else
        case "$PM" in
            *apt-get*) ensure gcc build-essential ;;   # apt package is build-essential
            *)         ensure gcc gcc ;;
        esac
    fi
fi
CC="$(command -v clang || command -v cc || command -v gcc)"

# --- 3. stockfish (optional: absolute-strength / odds testing) -------- #
ensure stockfish stockfish || true

# --- 4. python deps --------------------------------------------------- #
have pip3 || ensure pip3 python3-pip
export PIP_BREAK_SYSTEM_PACKAGES=1   # Debian/Ubuntu PEP 668 guard -- this box is dedicated to the engine
echo "-> installing Python dependencies (python-chess) ..."
python3 -m pip install --upgrade pip >/dev/null 2>&1 || true
python3 -m pip install -r requirements.txt

# --- 5. compiler target flags ----------------------------------------- #
ARCH="$(uname -m)"
case "$ARCH" in
    x86_64|amd64)  TUNE="-march=native" ;;   # Intel/AMD
    arm64|aarch64) TUNE="-mcpu=native"  ;;    # Apple Silicon / ARM
    *)             TUNE=""              ;;    # unknown: portable, no tuning
esac
CFLAGS="-O3 $TUNE -shared -fPIC -I."
echo "-> compiler: $CC   arch: $ARCH   flags: $CFLAGS"

build_so () {   # build_so <srcdir> <name>  ->  <srcdir>/<name>.so from <name>.c
    local dir="$1" name="$2"
    if [ -f "$dir/$name.c" ]; then
        # snapshots include Constants.h via -I. (main dir); their .c + the
        # main Constants.c compile into the snapshot's own .so.
        "$CC" $CFLAGS -o "$dir/$name.so" "$dir/$name.c" Constants.c \
            2>/dev/null && echo "   built $dir/$name.so" \
            || echo "   (skip $dir/$name.c -- did not compile against current Constants.c)"
    fi
}

# --- 6. current engine (required) ------------------------------------- #
echo "-> building the current engine's C libraries ..."
"$CC" $CFLAGS -o eval_c.so   eval_c.c   Constants.c
"$CC" $CFLAGS -o movegen.so  movegen.c  Constants.c
echo "   built eval_c.so + movegen.so"

# --- 7. Old Engine snapshots (best effort, for A/B matches) ----------- #
if [ -d "Old Engine" ]; then
    echo "-> building Old Engine snapshot libraries (for A/B; best effort) ..."
    for d in "Old Engine"/*/; do
        d="${d%/}"
        build_so "$d" eval_c
        build_so "$d" movegen
    done
fi

# --- 8. sanity check + next steps ------------------------------------- #
echo "-> verifying the engine loads ..."
python3 - <<'PY'
import chess, engine, random
e = engine.Engine(); e.use_book = False; random.seed(42)
b = chess.Board('r3k2r/8/8/8/8/8/8/R2QK2R w KQkq - 0 1')
m = e.get_best_move(b, 6)
print(f"   engine OK -- test search returned {m} ({e.nodes_searched} nodes, "
      f"C eval={'yes' if engine._USE_C_EVAL else 'PYTHON FALLBACK'})")
PY

echo ""
echo "== setup complete =="
echo "Run a headless engine-vs-engine match (uses the bundled fen.txt book):"
echo "    python3 match.py engine.py \"Old Engine/26/engine26.py\" 100 0 --workers 4"
