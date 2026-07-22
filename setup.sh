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

# --- FI-15: NNUE datagen opening book --------------------------------- #
# The 2.63M-line UHO Lichess book ships COMPRESSED (GitHub rejects files
# over 100 MB; the .gz is 41 MB). gen_data.py --book samples the plain
# text by byte offset, so extract once after a fresh pull. -k keeps the
# .gz so git status stays clean.
if [ -f UHO_Lichess_4852_v1.epd.gz ] && [ ! -f UHO_Lichess_4852_v1.epd ]; then
    echo "-> extracting UHO_Lichess_4852_v1.epd (2.63M openings, NNUE datagen book) ..."
    gunzip -k UHO_Lichess_4852_v1.epd.gz
fi

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

# --- 3. stockfish (absolute-strength / odds testing) ------------------ #
# The VM's yardstick/adjudication Stockfish should be the CURRENT DEV build
# (latest official-stockfish master), not the old STABLE release a package
# manager ships. On Linux we compare the installed build's commit against
# master and rebuild from source only when it has actually moved on (so a
# same-day re-run of setup.sh doesn't rebuild); other OSes just ensure some
# stockfish exists (manage the dev build there via `brew install --HEAD`).

sf_installed_hash() {          # commit hash embedded in the installed SF, or ""
    have stockfish || { echo ""; return 0; }
    # id name is like "Stockfish dev-20260713-1a2b3c4d" (dev) or "... 18" (stable)
    local idn
    idn="$(printf 'uci\nquit\n' | stockfish 2>/dev/null \
           | sed -n 's/^id name //p' | head -1 || true)"
    case "$idn" in
        *dev-*) echo "${idn##*-}" ;;   # last '-' field = the short commit hash
        *)      echo "" ;;             # a stable release => "not the dev build"
    esac
}

sf_latest_master_sha() {       # full 40-char SHA of official-stockfish master, or ""
    have curl || { echo ""; return 0; }
    curl -fsSL "https://api.github.com/repos/official-stockfish/Stockfish/commits/master" \
        2>/dev/null | sed -n 's/.*"sha": *"\([0-9a-f]\{40\}\)".*/\1/p' | head -1 || true
}

sf_pick_arch() {               # best Stockfish ARCH for THIS x86 CPU (avoid SIGILL)
    local f=/proc/cpuinfo
    if   grep -qm1 'avx512'  "$f" 2>/dev/null; then echo x86-64-avx512
    elif grep -qm1 'bmi2'    "$f" 2>/dev/null && grep -qm1 'avx2' "$f" 2>/dev/null; then echo x86-64-bmi2
    elif grep -qm1 'avx2'    "$f" 2>/dev/null; then echo x86-64-avx2
    elif grep -qm1 'sse4_1'  "$f" 2>/dev/null && grep -qm1 'popcnt' "$f" 2>/dev/null; then echo x86-64-sse41-popcnt
    else echo x86-64
    fi
}

build_stockfish_dev() {        # clone master, PGO-build (net embedded), install on PATH
    have git || ensure git git
    have git || { echo "   (git missing -- cannot build dev stockfish)"; return 1; }
    local tmp arch jobs
    tmp="$(mktemp -d)" || return 1
    echo "   cloning official-stockfish master ..."
    if ! git clone --depth 1 "https://github.com/official-stockfish/Stockfish" \
            "$tmp/SF" >/dev/null 2>&1; then
        echo "   (clone failed)"; rm -rf "$tmp"; return 1
    fi
    arch="$(sf_pick_arch)"
    jobs="$(nproc 2>/dev/null || echo 4)"
    echo "   building (ARCH=$arch, -j$jobs, profile-build; downloads+embeds the NNUE net) ..."
    if ! ( cd "$tmp/SF/src" && make -j"$jobs" profile-build ARCH="$arch" >/dev/null 2>&1 ); then
        echo "   (build failed for ARCH=$arch)"; rm -rf "$tmp"; return 1
    fi
    if cp "$tmp/SF/src/stockfish" /usr/local/bin/stockfish 2>/dev/null \
       || sudo cp "$tmp/SF/src/stockfish" /usr/local/bin/stockfish; then
        rm -rf "$tmp"; hash -r 2>/dev/null || true; return 0
    fi
    echo "   (install to /usr/local/bin failed)"; rm -rf "$tmp"; return 1
}

if [ "$OS" = "Linux" ]; then
    echo "-> Stockfish: ensuring the current dev (master) build ..."
    _sf_have="$(sf_installed_hash)"
    _sf_master="$(sf_latest_master_sha)"
    _sf_current="no"
    if [ -n "$_sf_have" ] && [ -n "$_sf_master" ]; then
        case "$_sf_master" in "$_sf_have"*) _sf_current="yes" ;; esac
    fi
    if [ "$_sf_current" = "yes" ]; then
        echo "   up to date (dev build $_sf_have matches master)"
    elif [ -z "$_sf_master" ]; then
        echo "   (could not reach GitHub to check master -- leaving stockfish as-is)"
        have stockfish || ensure stockfish stockfish || true
    else
        echo "   installed=${_sf_have:-none/stable}, master=$(printf '%.8s' "$_sf_master") -> building dev ..."
        if build_stockfish_dev; then
            echo "   now: $(printf 'uci\nquit\n' | stockfish 2>/dev/null | sed -n 's/^id name //p' | head -1)"
        else
            echo "   dev build failed -- falling back to the package stockfish"
            have stockfish || ensure stockfish stockfish || true
        fi
    fi
else
    # macOS/other: keep whatever's here; the dev build is a Homebrew concern
    #   brew uninstall stockfish; brew install --HEAD stockfish
    if [ -z "$(sf_installed_hash)" ]; then
        ensure stockfish stockfish || true
        echo "   (for the DEV build on macOS: brew uninstall stockfish; brew install --HEAD stockfish)"
    else
        echo "-> Stockfish: dev build present ($(sf_installed_hash))"
    fi
fi

# --- 4. python deps --------------------------------------------------- #
have pip3 || ensure pip3 python3-pip
export PIP_BREAK_SYSTEM_PACKAGES=1   # Debian/Ubuntu PEP 668 guard -- this box is dedicated to the engine
echo "-> installing Python dependencies (python-chess, numpy, scipy) ..."
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

up_to_date () {   # up_to_date <so> <src>  ->  0 if <so> is newer than src,
                  # Constants.c AND Constants.h (a header edit must rebuild too)
    [ "$1" -nt "$2" ] && [ "$1" -nt Constants.c ] && [ "$1" -nt Constants.h ]
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
    # FI-15: csearch.c single-TU-includes NNUE/nnue.c -- it is a real
    # dependency of the staleness check (missing file = always rebuild).
    if [ csearch.so -nt csearch.c ] && [ csearch.so -nt eval_c.c ] \
        && [ csearch.so -nt Constants.c ] && [ csearch.so -nt Constants.h ] \
        && [ -f NNUE/nnue.c ] && [ csearch.so -nt NNUE/nnue.c ]; then
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
                && [ "$d/csearch.so" -nt Constants.c ] \
                && [ "$d/csearch.so" -nt Constants.h ]; then
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
