#!/bin/bash
# Fetch the Syzygy WDL tables onto a box from the GitHub release.
#
#     ./scripts/fetch_syzygy.sh              # -> ./syzygy
#     ./scripts/fetch_syzygy.sh /data/syzygy # -> that directory
#
# This is the rented-box path: one command, no auth needed (the repo is
# public), and it VERIFIES every table against the SHA256SUMS shipped inside
# the zip. That check is the point -- a truncated table does not raise, it
# just makes some positions unprobeable, and gen_data would then quietly fall
# back to the search labels that the tablebase exists to replace.
#
# Re-runnable: an already-complete directory is detected and re-verified
# without downloading again.
set -eu
cd "$(dirname "$0")/.."
DEST="${1:-syzygy}"
TAG=syzygy-345
ASSET=syzygy-wdl-345.zip
URL="https://github.com/IchNukeDichWeg/Pygin/releases/download/${TAG}/${ASSET}"
N_EXPECT=145

verify() {                       # $1 = dir; returns 0 if all tables match
    [ -f "$1/SHA256SUMS" ] || return 1
    ( cd "$1" && shasum -a 256 --quiet -c SHA256SUMS >/dev/null 2>&1 )
}

if verify "$DEST"; then
    echo "syzygy: $DEST already complete and verified ($N_EXPECT tables)"
    exit 0
fi

mkdir -p "$DEST"
TMP="$DEST/.$ASSET.part"
echo "syzygy: fetching $ASSET (~379 MB) from release $TAG"
# curl draws its own progress bar with rate and ETA on a tty; off a tty it
# would smear, so fall back to periodic lines rather than silence.
if [ -t 1 ]; then
    curl -fL --retry 3 --retry-delay 2 -o "$TMP" "$URL"
else
    curl -fL --retry 3 --retry-delay 2 -o "$TMP" "$URL" --silent --show-error &
    CP=$!
    while kill -0 $CP 2>/dev/null; do
        sz=$(du -m "$TMP" 2>/dev/null | cut -f1 || echo 0)
        echo "  syzygy: ${sz:-0}/379 MB"
        sleep 20
    done
    wait $CP
fi

echo "syzygy: unpacking..."
unzip -o -q "$TMP" -d "$DEST"
rm -f "$TMP"

n=$(ls "$DEST"/*.rtbw 2>/dev/null | wc -l | tr -d ' ')
if [ "$n" -ne "$N_EXPECT" ]; then
    echo "syzygy: FAILED -- expected $N_EXPECT .rtbw, got $n" >&2
    exit 1
fi
echo "syzygy: verifying $n tables against SHA256SUMS..."
if ! verify "$DEST"; then
    echo "syzygy: FAILED -- checksum mismatch; delete $DEST and re-run" >&2
    exit 1
fi
echo "syzygy: OK -- $n tables verified in $DEST"
echo "  use it:  python3 NNUE/gen_data.py out.pygdata --syzygy $DEST ..."
