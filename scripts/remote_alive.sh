#!/bin/bash
# Is a long job still running on this box?  Portable, and immune to matching
# ITSELF -- which is the whole point.
#
#     ./scripts/remote_alive.sh match.py            -> "match.py: 3 running"
#     ./scripts/remote_alive.sh match.py odds.py    -> one line each
#
# exit 0 = at least one pattern has a live process, 1 = none.
#
# WHY THIS EXISTS. The obvious `pgrep -f match.py` matches its own invocation,
# the ssh command line that carried it, and any grep/awk in the same pipeline:
# a naive read counted 6 processes where 1 was running. And `pgrep -c` is
# procps-only, so on this Mac (and any BSD) it is not a miscount, it is a
# usage error printed to stderr while the caller reads an empty string.
#
# Two guards make a false match impossible. The process's OWN executable
# (argv[0]) must be a python, which no shell, ssh, awk or pgrep ever is; and
# the pattern must match a WHOLE argv token's basename, never a substring, so
# "match.py" does not match "rematch.py.log". No /proc, no procps flags.
set -u
[ $# -gt 0 ] || { echo "usage: remote_alive.sh <script.py> [...]" >&2; exit 2; }
any=0
for pat in "$@"; do
    n=$(ps -Ao pid=,args= 2>/dev/null | awk -v pat="$pat" -v self=$$ '
        {
            pid = $1
            if (pid == self) next
            exe = $2
            sub(/.*\//, "", exe)                    # argv[0] basename
            if (tolower(exe) !~ /^python[0-9._]*$/) next   # real python only:
                                                          # macOS ships it as
                                                          # "Python", hence
                                                          # tolower()
            for (i = 3; i <= NF; i++) {
                tok = $i
                sub(/.*\//, "", tok)                # whole-token basename
                if (tok == pat) { c++; break }
            }
        }
        END { print c + 0 }')
    echo "$pat: $n running"
    [ "$n" -gt 0 ] && any=1
done
[ "$any" -eq 1 ]
