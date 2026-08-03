#!/bin/sh
# Build a self-contained single-file UCI executable of the C-core engine:
#     ./scripts/build_exe.sh          ->  dist/pygin
#
# From v58 the NNUE net is bundled too, at the NNUE/nets/ path cengine
# resolves against its own directory (_MEIPASS in a onefile build). It is
# NOT optional: USE_NNUE has no silent HCE fallback, so a binary built
# without it fails to construct the engine at all. Bump the filename here
# whenever the shipped net changes.
#
# Bundles cuci.py + cengine/engine + csearch.so/eval_c.so/movegen.so +
# Perfect2023.bin. The result runs on machines WITHOUT Python or the repo
# (same OS/arch as the build machine only -- PyInstaller does not
# cross-compile; build on macOS for macOS, on Linux for Linux).
#
# Requires: pip3 install pyinstaller ; the .so files built (./setup.sh).
# Note: --onefile extracts to a temp dir per launch (~3-6s cold start,
# GUIs launch the engine once so this is invisible in play). For instant
# startup at the cost of a folder instead of a file, swap in --onedir.
set -e
# moved to scripts/ on 2026-07-24: run from the repo ROOT, where cuci.py
# and the .so files live.
cd "$(dirname "$0")/.."
for f in csearch.so eval_c.so movegen.so; do
    [ -f "$f" ] || { echo "missing $f -- run ./setup.sh first"; exit 1; }
done
D="$(pwd)"
# --paths lib/: the 2026-07-24 reshuffle moved the shared support modules
# into lib/ and cuci.py reaches them with a RUNTIME sys.path insert, which
# PyInstaller's static analysis cannot follow. Without this the binary builds
# clean and dies on the first line ("No module named 'time_manager'") -- which
# is exactly how the v55 release shipped broken.
python3 -m PyInstaller --onefile --name pygin cuci.py \
    --paths "$D/lib" \
    --hidden-import time_manager \
    --add-binary "$D/csearch.so:." \
    --add-binary "$D/eval_c.so:." \
    --add-binary "$D/movegen.so:." \
    --add-data   "$D/data/Perfect2023.bin:." \
    --add-data   "$D/NNUE/nets/nnue_v4_6f910e35bb1e.nnue:NNUE/nets" \
    --hidden-import engine --hidden-import chess.polyglot \
    --exclude-module pygame --exclude-module tkinter --exclude-module numpy \
    --exclude-module PySide6 --exclude-module matplotlib --exclude-module flask \
    --distpath dist --workpath build --specpath build --log-level WARN
echo
echo "built: $D/dist/pygin  ($(du -h dist/pygin | cut -f1))"

# RUN the smoke test, do not merely print it. The old script echoed this
# command as a suggestion and returned 0, so a binary that cannot start still
# looked like a successful build -- and got published.
echo "-> smoke-testing the binary ..."
SMOKE="$(printf 'uci\nisready\nposition startpos\ngo movetime 500\nquit\n' | ./dist/pygin 2>&1)"
for want in uciok readyok bestmove; do
    case "$SMOKE" in
        *"$want"*) ;;
        *) echo "!! SMOKE FAILED: no '$want' in the binary's output:"
           echo "$SMOKE" | head -20
           exit 1 ;;
    esac
done
echo "   smoke OK (uciok + readyok + bestmove)"
