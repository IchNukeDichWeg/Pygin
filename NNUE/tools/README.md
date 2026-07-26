# NNUE/tools

## Opening it locally

    python3 NNUE/tools/make_local.py
    open NNUE/tools/nnue_inspector_local.html

`nnue_inspector.html` is page CONTENT (no doctype, no head) because the
artifact host supplies the skeleton. Opened straight off disk it still
renders, but in QUIRKS MODE with no charset or viewport declared.
`make_local.py` wraps it into a real document; that copy is GENERATED, so
edit `nnue_inspector.html` and re-run the script rather than editing both.

Verified from `file://`: standards mode, UTF-8, `isSecureContext` true (so
`crypto.subtle` is available for the net hash), board renders, no console
errors. Nothing is ever fetched, so there is no server and no network.
If a browser ever does refuse the hash, the page says "eval skipped (cannot
hash here)" and still checks features and threats -- it does not claim an
agreement it could not establish.

`nnue_inspector.html` -- a self-contained page that loads a Pygin `.nnue` v1
net and evaluates positions in the browser, re-implementing the engine's
KA8T feature extraction, T16 threat computation and integer forward pass in
JavaScript. Open the file directly, or publish it; the net is chosen with a
file picker and never leaves the machine.

Shows the change in evaluation when each piece is lifted off the board (the
Stockfish Evaluation Guide's per-square table), plus two things that tool
cannot show for our architecture: the 16 threat scalars broken out by term
with their underlying square counts and saturation state, and the king
bucket / mirror state of each perspective.

## Play mode and Edit mode

**Play** shows a piece's legal moves as markers (a dot for a quiet move, a ring
around a piece for a capture) and enforces them. Castling, en passant and pins
are handled; promotion is automatic to a queen. `Flip board` swaps the
orientation; `Switch side to move` flips the mover, which matters because the
net's output is side-to-move relative.

**Edit** drops legality entirely: drag pieces from the tray onto the board,
drag them around it, drop one on the tray to remove it. That is deliberate --
the question an evaluation tool answers is "what would the net say if this
knight were there", and a good share of those positions are unreachable in a
real game. Kings cannot be deleted and a second king of the same colour
replaces the first, because the feature set needs exactly one of each.

Castling rights are cleared by an edit, which changes no evaluation: KA8T
reads piece bitboards and king squares, T16 reads attacks, and neither looks
at castling or en passant.

`move_vectors.json` holds 186 positions with every legal move of every movable
piece, exported from python-chess. The JS generator is checked against it
(2062/2062 pieces matched at the last run); regenerate if the editor ever
grows variant rules.

## compare_eval.py -- HCE vs NNUE

    python3 NNUE/tools/compare_eval.py --net <net> "<fen>" ["<fen>" ...]
    python3 NNUE/tools/compare_eval.py --net <net> --random 300

Both numbers come from the engine (`csearch_eval_white` and
`nnue_eval_oracle`), so the comparison is exact. This is deliberately NOT in
the browser tool: the .nnue forward pass is ~150 lines of integer arithmetic
and was portable, but the HCE is thousands of lines of tapered, phase-scaled,
clamped terms with mop-up and cant-win shaping on top. A subtly wrong port
would disagree with the engine while looking authoritative -- the same
failure the reference vectors exist to prevent, with no vectors to catch it.

## Why the reference vectors exist

A browser re-implementation that silently disagrees with the engine is worse
than no tool at all -- it looks authoritative while showing numbers the
engine would never produce. `reference_vectors.json` holds 40 positions
exported from the C engine with their feature indices, threat bytes and
exact evaluation. The page re-derives all three on load and reports the
agreement in its header.

Feature and threat agreement is NET-INDEPENDENT, so it is checked for any
net you load -- that is the part most likely to drift, because it duplicates
bitboard attack generation. The evaluation column is only meaningful for the
net the vectors came from (`8cc4d9d6caeb`) and is reported as skipped for
any other net rather than quietly passed.

## Regenerating the vectors

After any change to the feature set, the threat formulas (`THREAT_VER`), or
the `.nnue` format, the vectors are stale and the page will report a
disagreement that is really the vectors' fault. Regenerate them against the
current engine, then re-embed into the page's `VECTORS` literal.
