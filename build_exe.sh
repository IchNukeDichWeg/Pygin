#!/bin/sh
# Build a self-contained single-file UCI executable of the C-core engine:
#     ./build_exe.sh          ->  dist/pygin
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
cd "$(dirname "$0")"
for f in csearch.so eval_c.so movegen.so; do
    [ -f "$f" ] || { echo "missing $f -- run ./setup.sh first"; exit 1; }
done
D="$(pwd)"
python3 -m PyInstaller --onefile --name pygin cuci.py \
    --add-binary "$D/csearch.so:." \
    --add-binary "$D/eval_c.so:." \
    --add-binary "$D/movegen.so:." \
    --add-data   "$D/Perfect2023.bin:." \
    --hidden-import engine --hidden-import chess.polyglot \
    --exclude-module pygame --exclude-module tkinter --exclude-module numpy \
    --exclude-module PySide6 --exclude-module matplotlib --exclude-module flask \
    --distpath dist --workpath build --specpath build --log-level WARN
echo
echo "built: $D/dist/pygin  ($(du -h dist/pygin | cut -f1))"
echo "smoke: printf 'uci\\nisready\\nposition startpos\\ngo movetime 500\\nquit\\n' | dist/pygin"
