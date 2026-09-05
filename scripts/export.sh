#!/usr/bin/env bash
# Tar up engine match-result files (*.txt, *.pgn) in the current dir and MOVE
# the originals into ./exported/, leaving a single archive to scp elsewhere.
#
# T-25: this used to `rm` the originals, and it deleted files it had not
# created -- which is how the only near-equal Stockfish log was lost. The
# stated purpose ("leave a single archive to scp elsewhere") is satisfied by a
# MOVE, and a moved file cannot be lost. Deleting is now opt-in via --delete,
# and nothing is removed until the archive has been verified to contain
# exactly the files that were archived.
#
# Safe for a live log on the same filesystem: the writer's fd follows the
# inode, so match.py keeps appending into the moved file rather than losing it.
#
# Skips the 2 NEWEST files by modification time -- those belong to a match
# that is still running (its live .txt/.pgn pair), so it's safe to export
# finished results mid-A/B without corrupting or losing the active run.
# To export EVERYTHING (only when no match is running), use ./export_all.sh.
#
#     ./export.sh              archive, then move originals to ./exported/
#     ./export.sh --delete     archive, verify, then delete originals
#     ./export.sh --all        skip nothing (no match running)
#
set -euo pipefail
DELETE=0
SKIP=2                  # the live match's .txt/.pgn pair
for a in "$@"; do
    case "$a" in
        --delete) DELETE=1 ;;
        --all)    SKIP=0 ;;     # export_all.sh: skip nothing
        *) echo "unknown option: $a" >&2; exit 2 ;;
    esac
done
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
done < <(ls -t -- "${all[@]}" | tail -n +$((SKIP + 1)))
if [ ${#files[@]} -eq 0 ]; then
    echo "only ${#all[@]} file(s) here and the newest $SKIP are skipped (assumed live match) -- nothing to export"
    exit 1
fi
if [ "$SKIP" -gt 0 ]; then
    echo "skipping newest $SKIP (assumed live match):"
    ls -t -- "${all[@]}" | head -n "$SKIP" | sed 's/^/    /'
fi
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
# VERIFY before anything is moved or removed: a truncated archive plus a
# delete is the failure this script is one step away from every time it runs.
n_in_tar=$(tar tzf /tmp/match_export.tar.gz | wc -l | tr -d ' ')
if [ "$n_in_tar" -ne "${#files[@]}" ]; then
    echo "ARCHIVE VERIFY FAILED: ${#files[@]} archived, $n_in_tar in the tar -- originals left untouched" >&2
    exit 1
fi
if [ "$DELETE" -eq 1 ]; then
    rm -f "${files[@]}"
    echo "done -> /tmp/match_export.tar.gz (${#files[@]} files, originals DELETED)"
else
    mkdir -p exported
    mv -- "${files[@]}" exported/
    echo "done -> /tmp/match_export.tar.gz (${#files[@]} files, originals moved to ./exported/)"
fi
echo ""
echo "next steps (from your LOCAL terminal, not the VM):"
echo "1) exit"
echo "2) gcloud compute scp $(whoami)@chess-match-vm:/tmp/match_export.tar.gz . --zone=us-east1-b"
echo "   (lands in whatever local directory you run it from)"
