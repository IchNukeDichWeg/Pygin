# Rating anchors

One file per opponent. Each sets the binary, the published rating and **which
list that rating is from**, and nothing else:

```python
"""opponents/anchor_example.py -- <engine> <version>, CCRL Blitz 2+1: 3100."""
import uci_engine

BINARY = "~/engines/example/example"      # the UCI binary
RATING = 3100                              # as published, on the day we ran
RATING_LIST = "CCRL Blitz 2+1 (read 2026-09-12)"


class Engine(uci_engine.Engine):
    BINARY = BINARY
    OPTIONS = {}          # e.g. {"EvalFile": "/abs/path/net.nnue"}
    THREADS = 1           # the rating lists are single-threaded; match them
    HASH_MB = 64
```

Rules that keep an anchor honest:

* **Read the rating from the list on the day you run**, and write the date into
  `RATING_LIST`. Ratings drift as lists update; a number from memory is not a
  measurement.
* **One list per comparison.** CCRL 40/15 and CCRL Blitz are different pools;
  anchors from different lists do not pool into one estimate.
* **Match the list's conditions where you can** (single thread, ponder off,
  comparable time control) and state every difference next to the result. What
  comes out is "our strength on that scale, under our conditions".
* **Verify the binary plays sanely before trusting the anchor** --
  `scripts/rating_ladder.py --check` runs the handshake, prints the engine's own
  `id name`, and plays one move.
* **Bracket, don't span.** An opponent 400 points away decides nearly every game
  the same way and buys almost no information. Three or four anchors around our
  own level beat eight spread across the whole range.
