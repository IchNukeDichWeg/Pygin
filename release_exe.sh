#!/bin/sh
# Publish a version-release executable to GitHub Releases:
#     ./release_exe.sh 38            -> release "v38" + pygin-v38-macos-arm64
#
# PART OF THE SNAPSHOT RITUAL: run right after freezing Old Engine/<N>,
# BEFORE arming the next A/B candidate -- at that moment the live tree's
# search defaults ARE vN, so the bundled binary is the released version.
# (PyInstaller does not cross-compile: this uploads a binary for THIS
# machine's OS/arch; run the same script on other platforms to add their
# assets to the same release via the upload path below.)
#
# Requires: gh CLI authenticated once (`gh auth login`), pyinstaller.
set -e
cd "$(dirname "$0")"
[ -n "$1" ] || { echo "usage: ./release_exe.sh <version-number> [notes]"; exit 1; }
V="$1"
OS="$(uname -s | tr '[:upper:]' '[:lower:]' | sed 's/darwin/macos/')"
ARCH="$(uname -m)"
ASSET="dist/pygin-v${V}-${OS}-${ARCH}"

./build_exe.sh
cp dist/pygin "$ASSET"

NOTES="${2:-Pygin v${V} -- self-contained UCI engine executable (${OS}/${ARCH}).
Drop into any UCI GUI or lichess-bot; no Python or repo needed.
Options: Hash, Threads, OwnBook, UseTB, Move Overhead (+ tuning spins).
Version details: engine.py version history / DESIGN_c_search_core.md.}"

# Append the GitHub compare link, like the auto-generated notes do. PREV is
# the highest existing vN tag below this one, so a skipped version still
# links to something real. `gh release create` creates the tag itself.
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || echo IchNukeDichWeg/Pygin)"
PREV="$(git tag --list 'v[0-9]*' | sed 's/^v//' | sort -n | awk -v v="$V" '$1 < v' | tail -1)"
if [ -n "$PREV" ]; then
    NOTES="${NOTES}

**Full Changelog**: https://github.com/${REPO}/compare/v${PREV}...v${V}"
fi

if gh release view "v${V}" >/dev/null 2>&1; then
    gh release upload "v${V}" "$ASSET" THIRD_PARTY_LICENSES.md --clobber
    echo "uploaded $ASSET + THIRD_PARTY_LICENSES.md to existing release v${V}"
else
    gh release create "v${V}" "$ASSET" THIRD_PARTY_LICENSES.md --title "Pygin v${V}" --notes "$NOTES"
    echo "created release v${V} with $ASSET + THIRD_PARTY_LICENSES.md"
fi
