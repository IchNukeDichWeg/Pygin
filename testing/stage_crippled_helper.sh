#!/usr/bin/env bash
# FI-95's SECOND acceptance control: a deliberately CRIPPLED helper path that
# must read SLOWER on nps13 --threads.
#
#   ./testing/stage_crippled_helper.sh
#   python3 bench/nps13.py cengine.py Crippled/cengine.py \
#           --threads 4 --depth 16 --rounds 150 --repeat 3 --cpu 2
#
# WHY BOTH CONTROLS ARE NEEDED. A centred null proves the instrument does not
# INVENT a difference. It says nothing about whether the instrument can SEE
# one -- an instrument that always reads 1.00 passes a null perfectly and is
# useless. This control supplies a real, known-direction regression: helpers
# that search but publish NOTHING.
#
# The cripple is FI-96's -DFI96_HELPER_MUTE, reused rather than reinvented.
# It closes the only helper->main channel (the shared TT), so at Threads=4 the
# main thread does all the useful work while three helpers burn cores for
# nothing. Time-to-depth MUST get worse. FI-96 already proved the flag does
# what it claims: muted, the 4-thread search replays the 1-thread one node for
# node, 6/6 pairs.
#
# EXPECTED READING: ratio well BELOW 1.0 (B = crippled is slower), by clearly
# more than the ~1.5pp floor the 150-round null establishes. If it reads ~1.00,
# the instrument cannot detect a helper regression and no SMP verdict from it
# means anything -- including FI-47 S-06's.
#
# Crippled/ is gitignored by the /*/ rule (new top-level dirs are auto-ignored).
set -euo pipefail
cd "$(dirname "$0")/.."

[ -d Crippled ] && { echo "Crippled/ exists -- rm -rf Crippled first"; exit 1; }
for f in csearch.c eval_c.c Constants.c engine.py cengine.py eval_c.so movegen.so; do
    [ -e "$f" ] || { echo "missing $f -- run ./setup.sh first"; exit 1; }
done

mkdir Crippled
cp engine.py cengine.py Crippled/
cp eval_c.so movegen.so Crippled/
[ -e engine_eval.py ] && cp engine_eval.py Crippled/

# The ONLY difference: csearch.so built with helper stores suppressed.
CC="${CC:-cc}"
case "$(uname -m)" in
    x86_64|amd64)  TUNE="-march=native" ;;
    arm64|aarch64) TUNE="-mcpu=native"  ;;
    *)             TUNE="" ;;
esac
echo "-> building Crippled/csearch.so with -DFI96_HELPER_MUTE ($TUNE)"
$CC -O3 $TUNE -shared -fPIC -I. -w -DFI96_HELPER_MUTE \
    -o Crippled/csearch.so csearch.c eval_c.c Constants.c -lm -lpthread

# Prove the two builds really differ, so a no-op cripple cannot be mistaken
# for "the instrument sees nothing".
python3 - <<'PY'
import ctypes, os
a = ctypes.CDLL(os.path.join(os.getcwd(), "csearch.so"))
b = ctypes.CDLL(os.path.join(os.getcwd(), "Crippled", "csearch.so"))
for lib in (a, b):
    lib.csearch_abi.restype = ctypes.c_int
assert a.csearch_abi() == b.csearch_abi(), "abi mismatch -- rebuild both"
sa = os.path.getsize("csearch.so"); sb = os.path.getsize("Crippled/csearch.so")
print(f"   abi {a.csearch_abi()} both; sizes {sa:,} vs {sb:,}"
      f"{'  (identical size -- check the -D took effect)' if sa == sb else ''}")
PY

cat <<'MSG'

staged Crippled/ -- helpers search but publish nothing.

Run the control (B = crippled, so the ratio MUST come out below 1.0):
  python3 bench/nps13.py cengine.py Crippled/cengine.py \
          --threads 4 --depth 16 --rounds 150 --repeat 3 --cpu 2

Read it against the null from the same box: if the null's between-run spread
is ~1.5pp, this must be worse than 1.0 by clearly more than that. A reading of
~1.00 means the instrument cannot see a helper regression at all.
MSG
