#!/usr/bin/env bash
# Tar up engine match-result files (*.txt, *.pgn) in the current dir and
# delete the originals, leaving a single archive to scp elsewhere.
#
# Skips the 2 NEWEST files by modification time -- those belong to a match
# that is still running (its live .txt/.pgn pair), so it's safe to export
# finished results mid-A/B without corrupting or losing the active run.
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
# sort by mtime, newest first; skip the first 2 (the live match's pair)
files=()
while IFS= read -r f; do
    files+=("$f")
done < <(ls -t -- "${all[@]}" | tail -n +3)
if [ ${#files[@]} -eq 0 ]; then
    echo "only ${#all[@]} file(s) here and the newest 2 are skipped (assumed live match) -- nothing to export"
    exit 1
fi
echo "skipping newest 2 (assumed live match):"
ls -t -- "${all[@]}" | head -n 2 | sed 's/^/    /'
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
echo "2) gcloud compute scp USER@chess-match-vm:/tmp/match_export.tar.gz . --zone=us-east1-b"
echo "   (lands in whatever local directory you run it from)"
