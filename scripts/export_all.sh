#!/usr/bin/env bash
# Tar up ALL engine match-result files (*.txt, *.pgn) in the current dir,
# skipping nothing. Only run this when no match is writing.
#
#     ./export_all.sh              archive, then move originals to ./exported/
#     ./export_all.sh --delete     archive, verify, then delete originals
#
# T-25: this used to be export.sh copied and pasted with the skip removed, and
# the copy deleted its originals unconditionally -- a rule written into one of
# two identical scripts protects neither. It now DELEGATES, so the archive
# verification and the move-instead-of-delete default exist in exactly one
# place and cannot drift apart again.
set -euo pipefail
exec "$(dirname "$0")/export.sh" --all "$@"
