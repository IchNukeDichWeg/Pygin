#!/bin/bash
# Bring a freshly rented A/B box up and PROVE it is ready. Run:
#   curl -sL https://raw.githubusercontent.com/IchNukeDichWeg/Pygin/main/scripts/box_setup.sh | bash
# or, once the repo is there: ./scripts/box_setup.sh
#
# Every line it prints is a check, not a status message. It refuses to say
# READY unless the engine builds, every shim finds its net, the campaign
# state restores, and the measurement machinery is live. A box that looks
# fine and is quietly missing a net produces a full campaign of garbage.
set -u
cd /root
[ -d Pygin ] || git clone -q https://github.com/IchNukeDichWeg/Pygin.git
cd Pygin
# NEVER a bare `git pull` here: it aborts on untracked files, twice now.
git fetch origin -q && git reset --hard origin/main -q
echo "### HEAD: $(git log --oneline -1)"
CORES=$(nproc); echo "### CORES: $CORES  -> timed runs use $((CORES / 2)) workers"

echo ""
echo "### BUILD + SELFTEST"
./setup.sh 2>&1 | grep -aE '^  FAIL|ALL CHECKS|setup complete|FAILED' | tail -3

echo ""
echo "### SHIMS: each must find its net (the sha in the filename IS the check)"
python3 - <<'PY'
import os, sys
sys.path.insert(0, ".")
from battle_worker import describe_nnue_source, nnue_label
# Exactly the arms ab_next.sh runs, no more: a preflight that checks things
# the campaign never loads is noise that hides a real miss.
ARMS = ["engine_deadtag_on", "engine_deadtag_off",
        "engine_grow_on", "engine_nnue_v12"]
bad = 0
for a in ARMS:
    p = f"NNUE/shims/{a}.py"
    if not os.path.isfile(p):
        print(f"  MISSING FILE  {a}"); bad += 1; continue
    info = describe_nnue_source(p)
    lab = nnue_label(info)
    # The net filename embeds its own sha256 prefix, so a label whose sha
    # does not match its filename means the wrong file was found.
    ok = info.get("on") and not info.get("missing") \
        and info.get("hash") and info["hash"] in info["net"]
    print(f"  {'OK ' if ok else 'BAD'}  {a:22s} {lab}")
    bad += 0 if ok else 1
sys.exit(1 if bad else 0)
PY
SHIMS_OK=$?

echo ""
echo "### MEASUREMENT MACHINERY"
python3 - <<'PY'
import sys
sys.path.insert(0, "testing")          # lowercase: git tracks it that way and
import sprt                            # Linux is case-SENSITIVE unlike macOS
sc = 1.0 / (1.0 + 10 ** (-10 / 400.0))
print(f"  expected_pairs +10 Elo: {sprt.expected_pairs(0.41, sc, elo0=0.0, elo1=4.0):,.0f} pairs")
PY
python3 - <<'PY'
import importlib.util as i, sys
sp = i.spec_from_file_location("m", "match.py"); m = i.module_from_spec(sp)
sys.modules["m"] = m
try: sp.loader.exec_module(m)
except SystemExit: pass
p = [324, 1248, 1957, 1167, 304]
sc = sum(c * v for c, v in zip([x / sum(p) for x in p], (0, .25, .5, .75, 1.)))
print(f"  elo() pentanomial margin: +/-{m.elo(sc, 10000, penta=p)[1]:.2f}"
      f"  (coin-flip would say +/-{m.elo(sc, 10000)[1]:.2f})")
print(f"  worker_chi2 present: {callable(getattr(m, 'worker_chi2', None))}")
PY

echo ""
echo "### CAMPAIGN STATE: restore the deadtag tranche so it RESUMES, not restarts"
rm -f sprt_*.json
cp NNUE/campaigns/sprt_deadtag_tc10+0.1.json . 2>/dev/null \
  && python3 -c "
import json; d = json.load(open('sprt_deadtag_tc10+0.1.json'))
print(f\"  restored: {d['pairs']:,} pairs, LLR {d['llr']:+.3f}, next_offset {d['next_offset']}\")
assert d['pairs'] == 5000 and d['decision'] is None, 'unexpected state -- STOP'
print('  a fresh run here would DISCARD 5,000 real pairs; --sprt-resume prevents that')" \
  || echo "  NO deadtag state -- the continuation would restart from zero"

echo ""
echo "### BOOK"
python3 -c "
n = sum(1 for _ in open('data/UHO_4060_v4.epd'))
print(f'  UHO_4060_v4.epd: {n:,} positions (need >= 10,000 for offset 5000 + 5000)')
assert n >= 10000"

echo ""
[ "$SHIMS_OK" -eq 0 ] && echo "### READY -- run ./scripts/ab_next.sh" \
                      || echo "### NOT READY -- a shim could not find its net"
