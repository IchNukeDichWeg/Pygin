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
# -w: the big generated tables in Constants.c trip thousands of harmless
# constant-conversion warnings that spam the terminal; real errors still print.
CFLAGS="-O3 $TUNE -shared -fPIC -I. -w"
echo "-> compiler: $CC   arch: $ARCH   flags: $CFLAGS"

up_to_date () {   # up_to_date <so> <src>  ->  0 if <so> is newer than BOTH
    [ "$1" -nt "$2" ] && [ "$1" -nt Constants.c ]
}

build_so () {   # build_so <srcdir> <name>  ->  <srcdir>/<name>.so from <name>.c
    local dir="$1" name="$2"
    if [ -f "$dir/$name.c" ]; then
        # incremental: skip when the .so already exists and is newer than
        # its .c source and Constants.c (a fresh clone / new snapshot builds,
        # everything else is silently left alone).
        up_to_date "$dir/$name.so" "$dir/$name.c" && return
        # snapshots include Constants.h via -I. (main dir); their .c + the
        # main Constants.c compile into the snapshot's own .so.
        "$CC" $CFLAGS -o "$dir/$name.so" "$dir/$name.c" Constants.c \
            2>/dev/null && echo "   built $dir/$name.so" \
            || echo "   (skip $dir/$name.c -- did not compile against current Constants.c)"
    fi
}

# --- 6. current engine (required) ------------------------------------- #
echo "-> building the current engine's C libraries ..."
for _name in eval_c movegen; do
    if up_to_date "$_name.so" "$_name.c"; then
        echo "   $_name.so up to date"
    else
        "$CC" $CFLAGS -o "$_name.so" "$_name.c" Constants.c
        echo "   built $_name.so"
    fi
done

# csearch.so (C search core, phase 3 -- cengine.py's engine): multi-source
# build (links eval_c.c) with -lm, so it gets its own rule here.
if [ -f csearch.c ]; then
    if [ csearch.so -nt csearch.c ] && [ csearch.so -nt eval_c.c ] \
        && [ csearch.so -nt Constants.c ]; then
        echo "   csearch.so up to date"
    else
        "$CC" $CFLAGS -o csearch.so csearch.c eval_c.c Constants.c -lm -lpthread
        echo "   built csearch.so"
    fi
fi

# --- 7. Old Engine snapshots (best effort, for A/B matches) ----------- #
if [ -d "Old Engine" ]; then
    echo "-> building Old Engine snapshot libraries (for A/B; best effort) ..."
    for d in "Old Engine"/*/; do
        d="${d%/}"
        build_so "$d" eval_c
        build_so "$d" movegen
        # C-era snapshots (31+): csearch.so links the SNAPSHOT's eval_c.c
        # (multi-source + -lm -lpthread, so it can't reuse build_so).
        if [ -f "$d/csearch.c" ]; then
            if [ "$d/csearch.so" -nt "$d/csearch.c" ] \
                && [ "$d/csearch.so" -nt "$d/eval_c.c" ] \
                && [ "$d/csearch.so" -nt Constants.c ]; then
                :
            else
                "$CC" $CFLAGS -o "$d/csearch.so" "$d/csearch.c" "$d/eval_c.c" \
                    Constants.c -lm -lpthread 2>/dev/null \
                    && echo "   built $d/csearch.so" \
                    || echo "   (skip $d/csearch.c -- did not compile against current Constants.c)"
            fi
        fi
    done
fi

# --- 8. full health check (selftest.py, also runnable standalone) ------ #
echo "-> running selftest ..."
if ! python3 selftest.py; then
    echo ""
    echo "== setup finished but the selftest FAILED -- see the FAIL lines above =="
    exit 1
fi

echo ""
echo "== setup complete =="
echo "Run a headless engine-vs-engine match (uses the bundled fen.txt book):"
echo "    python3 match.py --workers 0"
