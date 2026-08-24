#!/bin/bash
# Package the 3-4-5 Syzygy WDL tables and publish them as a GitHub release
# asset, so a rented box can fetch them in one command:
#
#     ./scripts/release_syzygy.sh            # build + upload
#     ./scripts/release_syzygy.sh --zip-only # just build the zip locally
#
# WDL ONLY (145 .rtbw, 379 MB). Training labels need win/draw/loss and
# nothing else; DTZ is another 561 MB that only matters for PLAYING an
# ending out (the engine's UseTB), so shipping it here would double the
# download for no training benefit. See NNUE/tablebase.py.
#
# Ships a SHA256SUMS manifest inside the zip -- fetch_syzygy.sh verifies
# against it, because a silently truncated table does not error, it just
# makes some positions unprobeable and quietly returns labels to the search.
set -eu
cd "$(dirname "$0")/.."
SRC=syzygy
TAG=syzygy-345
ZIP=dist/syzygy-wdl-345.zip
N_EXPECT=145

[ -d "$SRC" ] || { echo "no $SRC/ directory -- run $SRC/fetch.sh first"; exit 1; }
n=$(ls "$SRC"/*.rtbw 2>/dev/null | wc -l | tr -d ' ')
[ "$n" -eq "$N_EXPECT" ] || { echo "expected $N_EXPECT .rtbw, found $n"; exit 1; }

mkdir -p dist
rm -f "$ZIP" "$SRC/SHA256SUMS"

echo "hashing $n tables..."
( cd "$SRC" && shasum -a 256 *.rtbw > SHA256SUMS )

echo "zipping -> $ZIP (stored, tables are already compressed)..."
( cd "$SRC" && zip -0 -q "../$ZIP" *.rtbw SHA256SUMS )
rm -f "$SRC/SHA256SUMS"
ls -lh "$ZIP" | awk '{print "  built", $9, $5}'

[ "${1:-}" = "--zip-only" ] && { echo "zip only, not uploading"; exit 0; }

NOTES="Syzygy 3-4-5 endgame tablebases, WDL only (145 .rtbw files, 379 MB).

Used for TRAINING LABELS: NNUE/gen_data.py --syzygy and NNUE/relabel_tb.py
replace the 5,000-node search label with tablebase truth for positions of 5
men or fewer, where the search is measurably wrong (15.7% of <=5-man result
labels and 41.6% of score signs, measured 2026-08-24).

Fetch onto a box with:
    ./scripts/fetch_syzygy.sh

DTZ (.rtbz) is deliberately NOT included -- training never needs it.
Source: the standard Syzygy distribution; see syzygy/fetch.sh for the URLs."

if gh release view "$TAG" >/dev/null 2>&1; then
    gh release upload "$TAG" "$ZIP" --clobber
else
    gh release create "$TAG" "$ZIP" \
        --title "Syzygy 3-4-5 tablebases (WDL, for training labels)" \
        --notes "$NOTES" --latest=false
fi
echo "published $TAG"
