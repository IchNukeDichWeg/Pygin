#!/bin/sh
# UCI entry for tuning/GUI hosts (cutechess-cli, chess-tuning-tools):
# cd to the repo ROOT so cuci.py and the .so files resolve (this script
# moved into scripts/ on 2026-07-24)
# no matter which working directory the host spawns engines from.
cd "$(dirname "$0")/.." && exec python3 cuci.py
