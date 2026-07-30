#!/usr/bin/env python3
"""TT-size candidate: 192 MB -> 768 MB default.

    python3 match.py cengine_tt.py cengine.py 2500 --offset 0 \
            --workers 0 --sprt --tag tt768

Candidate in slot 1 -- match.py scores its pentanomial from Engine 1.

WHY. Measured 2026-07-30, warm table across a real game at 1.4s/move (the
50+0.20 operating point), hashfull in permille:

              ply 8   ply 16   ply 24   ply 32   ply 40
    192 MB      833      974      995     1000     1000
    768 MB      329      564      721      813      886

The shipped default is COMPLETELY SATURATED by move 16 and stays full for the
rest of every game -- every store from then on evicts something. A cold single
search never shows this (bench hashfull reads 17 permille), which is why it
went unnoticed.

TT size last moved at v47. Since then the engine got 1.79x faster
single-thread and searches far more nodes per move, so the table shrank
relative to the search. The two prior increases both paid: 96 MB +5.94 (v46),
192 MB +3.16 (v47) -- diminishing, so price this at +1-3, not +6.

No tactical risk and no node-count change at fixed depth: a bigger table
changes which entries survive, not what the search is allowed to do.
"""
from cengine import Engine as _Base


class Engine(_Base):
    TT_BITS = 25            # 2^25 * 24 B = 768 MB (was 23 = 192 MB)
