#!/usr/bin/env bash
# Tar up all engine match-result files (*.txt, *.pgn) in the current dir and
# delete the originals, leaving a single archive to scp/download elsewhere.
#
#     ./export.sh
#
set -euo pipefail
tar czf /tmp/match_export.tar.gz *engine*.txt *engine*.pgn
rm -f *engine*.txt *engine*.pgn
echo "done -> /tmp/match_export.tar.gz"
