#!/usr/bin/env bash
# Tar up ALL engine match-result files (*.txt, *.pgn) in the current dir
# and delete the originals, leaving a single archive to scp elsewhere.
#
# Exports EVERYTHING it matches (2026-07-19, user call -- the old version
# skipped the 2 newest files as an assumed live match). Because the
# originals are DELETED after archiving, only run this when no match is
# writing: a live run's log would be tarred mid-write and unlinked.
#
#     ./export.sh
#
set -euo pipefail
shopt -s nullglob
all=(*engine*.txt *engine*.pgn)
if [ ${#all[@]} -eq 0 ]; then
    echo "no *engine*.txt / *engine*.pgn files here -- nothing to export (existing archive, if any, left untouched)"
    exit 1
fi
files=("${all[@]}")
# rotate any existing archive out of the way first -- it's never overwritten, just renamed
if [ -f /tmp/match_export.tar.gz ]; then
    n=1
    while [ -f "/tmp/match_export.$n.tar.gz" ]; do
        n=$((n + 1))
    done
    mv /tmp/match_export.tar.gz "/tmp/match_export.$n.tar.gz"
    echo "kept previous archive -> /tmp/match_export.$n.tar.gz"
fi
# write to a temp name first so a failed/interrupted tar never clobbers anything
tar czf /tmp/match_export.tar.gz.new "${files[@]}"
mv /tmp/match_export.tar.gz.new /tmp/match_export.tar.gz
rm -f "${files[@]}"
echo "done -> /tmp/match_export.tar.gz (${#files[@]} files)"
echo ""
echo "next steps (from your LOCAL terminal, not the VM):"
echo "1) exit"
echo "2) gcloud compute scp $(whoami)@chess-match-vm:/tmp/match_export.tar.gz . --zone=us-east1-b"
echo "   (lands in whatever local directory you run it from)"
