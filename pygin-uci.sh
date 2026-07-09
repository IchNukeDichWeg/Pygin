#!/bin/sh
# UCI entry for tuning/GUI hosts (cutechess-cli, chess-tuning-tools):
# cd to this script's own directory so cuci.py and the .so files resolve
# no matter which working directory the host spawns engines from.
cd "$(dirname "$0")" && exec python3 cuci.py
