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
echo ""
echo "next steps:"
echo "1) exit"
echo "2) gcloud compute scp chess-match-vm:/tmp/match_export.tar.gz . --zone=us-east1-b"
echo "3) cloudshell download match_export.tar.gz"
echo "4) rm -f match_export.tar.gz   # delete from Cloud Shell once downloaded"
