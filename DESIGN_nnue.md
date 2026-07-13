# Design: NNUE evaluation (roadmap FI-15 / Phase 7)

**Status:** NOT STARTED. Planning only. **Prerequisite:** per FI-15's own
pricing note in `final_improvements.md`, do not start until the cheap
queue items are mined out (FI-23 running now; then FI-24a/b, FI-21, FI-05,
FI-12, FI-26 leftovers, FI-20, FI-22, FI-11) — this is a multi-week project,
those are hours-to-days each, and [[engine-feature-workflow]]'s "one
candidate live at a time" rule means it shouldn't jump the queue. **Prior
art:** an unrelated, already-built self-play MLP (`selfplay/` package) exists
in this repo but is NOT reusable as-is — see "What's already here" below for
exactly what carries over and what doesn't.

## Why this is the last +100-class item

Every search-side feature since v31 has been mined into single digits or
nulls (FI-04 +2.15, FI-06 +2.26, FI-18 −1.25 — the "finer-quiet-signal" and
"shallow-prune" veins are running dry). The two levers that still pay in
whole-Elo-per-percent terms are NPS (still being harvested item by item) and
SMP (confirmed +110 at 4 threads). NNUE is different in kind: it doesn't
sharpen the existing HCE, it **replaces the evaluation function's ceiling**.
Every engine that has shipped it (Stockfish, plenty of others) gained
hundreds of Elo. That upside is why it's worth the cost even though it will
almost certainly look like a *regression* on day one (see Phase 5).

## Honest pricing

- **Time:** weeks to months, not a single-session A/B like everything else
  in the ledger.
- **NPS:** expect a 40-60% drop even with a well-optimized incremental
  accumulator + int8/int16 SIMD forward pass — a NN eval is simply more
  arithmetic per node than the current `eval_c.c` (which is already a fast,
  branchy, mostly-integer function).
- **First net usually loses to HCE.** This is the normal NNUE bring-up
  curve, not a sign to abandon the project — Stockfish's own early nets
  lost to its HCE too. Budget for 2-4 bootstrap iterations before the first
  positive A/B.
- **Pessimistic first-success estimate: +80 to +150 Elo. Ceiling is much
  higher** (net architecture, feature set, and data volume are all still
  headroom after the first shipped version) — this is the one item in the
  whole backlog that isn't capped by the shallow-search-regime doctrine that
  killed P-41/P-33/P-42/Q-01, because it changes the *evaluation*, not the
  *tree*.

## What's already here (and what isn't)

The repo has a previous, **unrelated** neural-net effort — [[nn-engine-v2-architecture]],
the `selfplay/` package (`engine.py`, `features.py`, `model.py`,
`trainer.py`, `nnue_infer.c`, `quiet-labeled.v7.epd`). It is useful as prior
art and a cautionary tale, not as a codebase to extend:

| Piece | What it is | Reusable for FI-15? |
|---|---|---|
| `features.py` (776 = 12×64 planes + 8 scalars) | Full board recomputed from scratch every position | **No** — real NNUE needs incremental add/remove-feature updates in `apply_move`, not a from-scratch extraction. Different feature set entirely (plain piece-square, no king-relative buckets). |
| `model.py` (776→512→256→64→1, single perspective, tanh output) | Plain MLP, not "efficiently updatable" at all | **No** — no accumulator, no two-perspective split, no quantization. The name NNUE literally refers to the accumulator-update property this lacks. |
| `trainer.py` (numpy forward/backward, TD-leaf self-play) | A working gradient-descent + self-play data loop | **Partially** — the *shape* of a training loop (data loader, batch grad step, checkpointing) is worth skimming, but the loss function should change (regression to search-eval labels, not TD-leaf on a weak engine) and it needs a quantization-aware training pass added. |
| `quiet-labeled.v7.epd` | ~500k labeled quiet positions from the OLD engine | **Yes, as a bootstrap seed** — cheap to reuse for a first warmstart pass, but it was labeled by a much weaker engine (pre-C-core) so it should be re-labeled or heavily supplemented by v47/v48 self-play data before the net matters for A/B. |
| The result: this MLP engine sat at **~-200 Elo vs the HCE baseline** and was never integrated into `csearch.c`'s search loop at all — it was its own standalone top-level engine. | | **Key lesson carried forward:** a hybrid eval (NN main search, HCE qsearch stand-pat) was necessary — overriding qsearch stand-pat with the NN made things WORSE (-203 to -273 Elo). Expect the same split to matter for the real NNUE. |

**Bottom line:** budget for a mostly-fresh implementation in C, reusing only
the *data* (as a seed) and the *lessons*, not the code.

## Architecture (concrete shape, not just theory)

768→256×2→1, HalfKP-style two-perspective feature transformer — the
standard small "NNUE-lite" shape (Stockfish's own first shipped net was
this size before it grew). Sketch already scoped in `final_improvements.md`
FI-15:

```c
/* csearch.c: the accumulator lives on the Board and updates in apply_move,
 * NOT recomputed from scratch -- this is the entire point of NNUE. */
#define NN_H 256
typedef struct { int16_t v[2][NN_H]; } Accum;   /* [white-persp][black-persp] */
/* feature = (perspective king square/bucket, piece type, colour, square)
 *         -> a column of W1 (the feature-transformer weight matrix)      */
/* apply_move: for each feature REMOVED and ADDED by the move (typically
 * 2-4 total: mover's from/to, captured piece if any, rook on castling):
 *     acc.v[persp] += W1[:, feat_added] - W1[:, feat_removed]            */
static int nn_eval(const Board* b, const Accum* a)   /* forward pass, ~100ns target */
{
    const int16_t* us = a->v[b->turn], *them = a->v[!b->turn];
    int32_t s = B2;
    for (int i = 0; i < NN_H; i++) {                 /* SIMD-friendly loop */
        s += W2[i]        * (int32_t)clamp_relu(us[i]);
        s += W2[NN_H + i] * (int32_t)clamp_relu(them[i]);
    }
    return s / NN_SCALE;                             /* centipawn-ish */
}
```

Feature set decision to make explicitly in Phase 1 (don't skip this — it's
the single biggest strength/complexity knob):

- **Simplest (recommended start):** plain 768 = 12 piece-types × 64 squares
  per perspective, no king bucket. Easiest to implement and verify first;
  weakest ceiling.
- **HalfKP (Stockfish's original):** feature = (own king square, piece
  type, piece square) — buckets the whole transformer by king position, so
  the accumulator must be **fully recomputed** (not incrementally updated)
  whenever a king moves. Meaningfully stronger, meaningfully more code
  (king-move special case in `apply_move`).
- **Recommendation:** ship the plain-768 version first end-to-end (Phases
  1-6 below), confirm the whole pipeline and get a real A/B verdict, THEN
  upgrade the feature set to HalfKP as a follow-up campaign once the
  machinery is proven. Don't design both at once.

The C core's copy-make `Board` struct (`movegen.c`) is already a good fit
for this: `apply_move` is the one place every board mutation flows through,
so the accumulator update is a natural addition there, not a new subsystem.

## Phased plan (each phase independently verifiable + GO/NO-GO gate)

**Phase 0 — readiness gate.** Confirm the cheap-item queue is actually
mined out (see Status above) and the SMP campaign is landed (it is, +110
Elo confirmed 2026-07-13). Re-read this doc's "honest pricing" section and
get explicit go-ahead before touching code — this is the point where the
project stops looking like every other item in the ledger.

**Phase 1 — architecture + data format lock-in.** Decide feature set
(plain 768, per above), net shape (256×2 hidden — can revisit), label
source (see Phase 2), and the on-disk weight format (a flat quantized
`.nnue`-style binary the C side `dlopen`/`fread`s, NOT numpy `.npz` — that's
a Python-only format the search core can't read directly). Deliverable: a
short frozen spec (append to this file) before any code lands. **Gate:**
the spec exists and names exact tensor shapes/quantization scales.

**Phase 2 — data generation pipeline.** Reuse `match.py`'s self-play +
the existing WDL-model plumbing (`fit_wdl_model.py` already parses match
logs for labels) to generate quiet positions labeled by **the current
engine's own search score** at a fixed depth/nodes budget (Stockfish's own
method) — NOT TD-leaf on a weak player, which is what sat the old MLP at
-200 Elo. `quiet-labeled.v7.epd` can seed a first warmstart, but the bulk
of training data should come from v47/v48 self-play (millions of positions
target, filtered to quiet non-capture/non-check positions the same way
qsearch already identifies them). **Gate:** a labeled dataset of >=5-10M
positions, holdout split, sanity-checked score distribution.

**Phase 3 — trainer.** Python/PyTorch (or numpy if avoiding the new
dependency matters more than convenience — CPU-only training of a net this
small is fine either way), regression loss vs the search-score labels
(optionally blended with game WDL, Stockfish-style). Must support
**quantization-aware training** (int16 feature-transformer weights, int8
second layer, clipped ReLU) since the C side needs quantized weights, not
float — training in float then naively rounding at the end reliably tanks
net quality. **Gate:** training converges (RMSE falling, holdout not
diverging from train) on the Phase-2 dataset.

**Phase 4 — C inference + incremental accumulator.** Add `Accum` to
`Board`, wire add/remove-feature updates into `apply_move` (movegen.c),
write `nn_eval` (csearch.c or a new `nnue_eval.c`). **Verify bit-exact vs
the trainer's own quantized forward pass** over a large random-position set
— this repo's established bar for this kind of port is "zero mismatches
over millions of positions" (eval_c.c's original port did 3M, FI-01's
incremental-Zobrist did 52.4M) and NNUE deserves the same rigor, since a
silent accumulator-update bug is a *desync* bug (correct-looking scores
that are subtly wrong from some earlier move), not a crash. **Gate:** 0
mismatches over >=1M positions, covering ordinary moves, captures, castling,
promotions, and null moves.

**Phase 5 — hybrid integration + first honest A/B.** Route `nn_eval`
into the main search's static eval; **keep qsearch stand-pat on the
existing HCE** per the old project's hard-won lesson (NN stand-pat measured
-203 to -273 Elo when tried). Screen with a **2k-game run vs the current
Old Engine snapshot** (cheap, fast — this is expected to LOSE on the first
net, that's fine, it's a screen not a verdict). **Gate:** the pipeline
produces a legal, non-crashing, non-embarrassing engine (screen result
doesn't have to be positive yet, just sane — no illegal moves, no
NaN/overflow scores, no obvious blunder-every-game pattern).

**Phase 6 — bootstrap iteration.** Generate a new self-play dataset with
the NN-equipped engine from Phase 5, retrain, re-screen. Repeat. This is
the normal NNUE improvement loop (every shipped Stockfish net was trained
on data from its own predecessor). Budget 2-4 iterations before expecting a
positive screen. **Gate:** a 2k-game screen clears roughly +15 (this repo's
existing pre-registered bar from the FI-15 note) against the current Old
Engine snapshot.

**Phase 7 — full A/B + ship.** Once a net clears the 2k screen, run the
standard **10,000-game A/B** (5000 positions, per [[match-py-positions-arg]])
vs the current snapshot, same SPRT/reporting conventions as every other
item in the ledger. Positive → the usual snapshot ritual (new `Old
Engine/N`, `release_exe.sh`, CE_LADDER re-pin — though note the ladder's
whole *methodology* may need rethinking since NN eval scores won't
node-match the HCE ladder at all; a new NN-era reference ladder gets
established here, it does not need to match the old one).

**Phase 8 (ongoing, not gated) — NPS recovery + net growth.** Once shipped,
this becomes its own mini-ledger: SIMD-optimize the forward pass (AVX2/NEON
intrinsics), consider a bigger/deeper net now that the harness exists,
consider upgrading the feature set to HalfKP (see Phase 1 note), consider
king-bucketed nets. Each of these is a normal single-session A/B item once
the base NNUE machinery exists — this is where the "ceiling much higher"
half of the pricing note gets cashed in.

## Recommendation

Don't start Phase 1 until the current cheap-item queue is actually dry —
running the numbers, that's still several sessions away. When it's time,
treat Phases 0-4 as an internal, non-A/B'd build-out (like the C-core
project's own Phase 1-3), and don't run a real 10k campaign until Phase 6's
2k screen clears — this avoids burning the SPRT/10k-game budget on a net
that everyone should expect to lose on iteration 1.
