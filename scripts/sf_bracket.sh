#!/bin/bash
# Bracket Pygin's strength on the SF-18 UCI_Elo scale: 2900 then 2950.
#
#     ./scripts/sf_bracket.sh
#
# WHY TWO CAPS. The only strength figure on the README (~2868, measured at
# v58) carries its own caveat: it extrapolates from a SINGLE cap. Score above
# 50% at one cap and below at the other and the crossing point is bracketed
# instead of extrapolated, which retires that caveat.
#
# WHY NOT 2850. v58 already read 45.45% at 2900, so 2850 is a win we can
# already infer -- it would spend 3.4h confirming what we know. User's call.
#
# WHY THE GUARD BETWEEN THEM. match.py EXITS 0 ON INTERRUPT (verified: it
# traps SIGTERM/Ctrl-C, writes the summary, returns success). So a plain
# `&&` would launch cap 2 the instant you interrupted cap 1. This checks the
# first run actually reached its full budget before starting the second.
#
# INSTRUMENT, matched to the v58 measurement so the numbers are comparable:
# 50+0.5, Threads=1, seed 62, disjoint openings (offset 0 then 500).
# Workers differ from that run (9 here vs 4 then) because density is
# per-machine: 18 cores / 2, one core per engine since a game runs two.
set -u
cd "$(dirname "$0")/.."
W=$(( $(sysctl -n hw.ncpu 2>/dev/null || nproc) / 2 ))
say(){ echo "[$(TZ=Europe/Zurich date '+%H:%M:%S %Z')] $*"; }

run_cap () {            # run_cap <elo> <offset> <log>
    say "=== SF UCI_Elo $1, offset $2, $W workers, 50+0.5 ==="
    python3 match.py cengine.py stockfish_engine.py 500 "$2" \
        --workers "$W" --tc 50+0.5 --seed 62 --sf-elo "$1" 2>&1 | tee "$3"
    # T-16/T-19: read the EXIT STATUS, not the log wording. `| tee` makes $? the
    # status of tee, so the tool's own status has to come out of PIPESTATUS --
    # and this guard used to grep for wording match.py never printed ("SIGTERM
    # received" only appeared post-T-16; a plain Ctrl-C printed "interrupted"),
    # so a Ctrl-C on cap 1 launched cap 2 anyway. The grep stays as a second
    # line of defence for an older match.py that still exits 0.
    ST=${PIPESTATUS[0]}
    if [ "$ST" -ne 0 ] || grep -qa "SIGTERM received\|interrupted -- writing summary" "$3"; then
        say "cap $1 was INTERRUPTED -- stopping here, not starting the next cap"
        return 1
    fi
    grep -qa "^Ptnml:" "$3" || { say "cap $1 produced no Ptnml -- stopping"; return 1; }
    say "cap $1 complete"
}

run_cap 2900 0   sf_2900.log || exit 1
run_cap 2950 500 sf_2950.log || exit 1

say "=== BRACKET ==="
for f in sf_2900.log sf_2950.log; do
    printf '  %-13s %s\n' "${f%.log}" "$(grep -a 'Engine 1 score:' "$f" | tail -1)"
done
echo ""
echo "Above 50% at 2900 and below at 2950 brackets the crossing point."
echo "Both below 50% means v62 sits under 2900 -- drop a rung and re-run."
