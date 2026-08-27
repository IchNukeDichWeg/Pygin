"""NNUE/shims/engine_v61b_deadtag.py -- v61 candidate B: FI-115 dead-entry TT tag.

A SHIP CANDIDATE, not an A/B control: this is v60 exactly, plus TT_DEADTAG on.
Baseline is NNUE/shims/engine_nnue_v12.py (v60's net and settings), so a
confirmed gain here is a version, not just a finding.

    python3 match.py NNUE/shims/engine_v61b_deadtag.py NNUE/shims/engine_nnue_v12.py \
        5000 0 --workers $(($(nproc)/2)) --tc 10+0.1 --seed 59 \
        --sprt --sprt-min-pairs 1500

Same instrument as every screen since s1 (seed 59, UHO_4060_v4, offset 0,
cores/2 workers). Budget note: this resolves ~+9 Elo and up; a null means
"not >= 9", not "no effect".

WHY. Material is irreversible: once the game passes a capture, no
position with MORE men can recur, so a TT entry whose stored piece count
exceeds the root's is provably unreachable -- garbage with certainty, not a
heuristic. Audited in a self-play game with a 12 MiB table kept across moves:
by ply 56-64, 53-72% of the table was provably dead, while old-generation
entries supplied 39-82% of ALL hits. The existing "different generation ->
overwrite" rule was therefore discarding the treasure along with the garbage.
The tag separates them: take a DEAD incumbent always, depth-protect a
REACHABLE old one.

Nothing is ever cleared -- a dead entry cannot produce a hit, it only holds a
slot. This is purely a better victim choice at store time, no sweep.

First measurement, 24-ply self-play at depth 10 with a 12 MiB table:
1,226,107 -> 1,058,229 nodes, -13.7% for the same moves. That is tree
quality, not speed, so only games decide it."""

import cengine


class Engine(cengine.Engine):
    USE_NNUE = True
    NNUE_FILE = "NNUE/nets/nnue_v12_bf86c4ced057.nnue"   # the net v60 ships
    LAZY_NNUE = True
    TT_DEADTAG = True     # FI-115
