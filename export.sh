#!/usr/bin/env bash
# Tar up all engine match-result files (*.txt, *.pgn) in the current dir and
# delete the originals, leaving a single archive to scp/download elsewhere.
#
#     ./export.sh
#
set -euo pipefail
shopt -s nullglob
files=(*engine*.txt *engine*.pgn)
if [ ${#files[@]} -eq 0 ]; then
    echo "no *engine*.txt / *engine*.pgn files here -- nothing to export (existing archive, if any, left untouched)"
    exit 1
fi
# write to a temp name first so a failed/interrupted tar never clobbers a good existing archive
tar czf /tmp/match_export.tar.gz.new "${files[@]}"
mv /tmp/match_export.tar.gz.new /tmp/match_export.tar.gz
rm -f "${files[@]}"
echo "done -> /tmp/match_export.tar.gz"
echo ""
echo "next steps:"
echo "1) exit"
echo "2) gcloud compute scp chess-match-vm:/tmp/match_export.tar.gz . --zone=us-east1-b"
echo "3) cloudshell download match_export.tar.gz"
echo "4) rm -f match_export.tar.gz   # delete from Cloud Shell once downloaded"
