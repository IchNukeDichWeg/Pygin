#!/usr/bin/env bash
# Stage a buildable engine directory from any git ref, for A/B against HEAD.
#
#   ./testing/stage_ref.sh <git-ref> [dir]
#   ./testing/stage_ref.sh fba8748 Base
#   python3 bench/nps13.py Base/cengine.py cengine.py --rounds 32 ...
#
# WHY. Comparing HEAD against `Old Engine/NN` measures everything since that
# snapshot. To price ONE change you need its immediate predecessor, and the
# snapshots are cut per release, not per item. This stages any commit.
#
# It is the texel.py-stage layout: engine.py + cengine.py + the three .so
# files in one directory, with cengine resolving _DIR to its own copies.
# csearch.so/eval_c.so/movegen.so are built FROM THE REF'S OWN SOURCES, so a
# ref whose C differs is genuinely a different engine, not HEAD's .so wearing
# an old Python file.
#
# The staged dir is auto-gitignored by the /*/ rule.
set -euo pipefail
cd "$(dirname "$0")/.."
REF="${1:?usage: stage_ref.sh <git-ref> [dir]}"
DIR="${2:-Base}"
[ -e "$DIR" ] && { echo "$DIR exists -- rm -rf $DIR first"; exit 1; }

git rev-parse --verify "$REF^{commit}" >/dev/null || { echo "bad ref $REF"; exit 1; }
mkdir "$DIR"
# Required: without any of these the build is not the ref's engine.
for f in engine.py cengine.py csearch.c eval_c.c movegen.c Constants.c Constants.h; do
    git show "$REF:$f" > "$DIR/$f" 2>/dev/null || {
        echo "!! required file $f absent at $REF"; rm -rf "$DIR"; exit 1; }
done
# Optional: present in some eras only. Absence is not an error.
for f in engine_eval.py eval_c.h movegen.h; do
    git show "$REF:$f" > "$DIR/$f" 2>/dev/null || rm -f "$DIR/$f"
done

CC="${CC:-cc}"
case "$(uname -m)" in
    x86_64|amd64)  TUNE="-march=native" ;;
    arm64|aarch64) TUNE="-mcpu=native"  ;;
    *)             TUNE="" ;;
esac
echo "-> building $DIR from $REF ($(git log -1 --format=%s "$REF" | cut -c1-60))"
# -I.. lets csearch.c's `#include "NNUE/nnue.c"` resolve to the REPO ROOT's
# copy. CAVEAT, stated rather than hidden: if the ref's NNUE sources differ
# from HEAD's, the staged build gets HEAD's. That is harmless for search-side
# comparisons (NNUE is dormant, USE_NNUE=False) and WRONG for anything that
# touches NNUE -- stage NNUE/ explicitly if you ever price an FI-15 change.
( cd "$DIR"
  $CC -O3 $TUNE -shared -fPIC -I. -I.. -w -o eval_c.so   eval_c.c   Constants.c -lm
  $CC -O3 $TUNE -shared -fPIC -I. -I.. -w -o movegen.so  movegen.c  Constants.c -lm
  $CC -O3 $TUNE -shared -fPIC -I. -I.. -w -o csearch.so  csearch.c eval_c.c Constants.c -lm -lpthread )

# The abi is the honest compatibility check: a staged ref whose cengine.py
# demands a newer abi than its own csearch.so provides would fail at load,
# and it is better to hear that here than mid-measurement.
python3 - "$DIR" <<'PY'
import ctypes, os, sys
d = sys.argv[1]
a = ctypes.CDLL(os.path.join(os.getcwd(), "csearch.so"))
b = ctypes.CDLL(os.path.join(os.getcwd(), d, "csearch.so"))
for l in (a, b): l.csearch_abi.restype = ctypes.c_int
print(f"   HEAD abi {a.csearch_abi()}   {d} abi {b.csearch_abi()}")
print(f"   sizes {os.path.getsize('csearch.so'):,} vs "
      f"{os.path.getsize(os.path.join(d,'csearch.so')):,}")
PY
echo "staged $DIR/ -- compare with:"
echo "  python3 bench/nps13.py $DIR/cengine.py cengine.py --rounds 32 --repeat 1 --cpu N"
