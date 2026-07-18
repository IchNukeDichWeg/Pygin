# Design: NNUE evaluation (roadmap FI-15 / Phase 7)

**Status:** PHASES 1-5 BUILT-DORMANT 2026-07-18 (user call, overriding the
queue gate and the plain-768-first recommendation below -- see "Phase 1
spec (FROZEN)" at the end of this file for the locked architecture, and
`NNUE/README.md` for every command). The infrastructure is code-complete and
verified: architecture + weight format locked, data pipeline runs (smoke
dataset generated), PyTorch trainer overfits and exports, C inference is
bit-exact vs the trainer and incremental-exact vs full refresh, hybrid
integration sits behind `g_use_nnue` / `cengine.USE_NNUE` (default 0 =
v50+armed-defaults byte-exact). Phases 6-8 (real 50M-position data,
bootstrap iterations, A/B, ship) remain open.

**Original status:** NOT STARTED. Planning only. **Prerequisite:** per FI-15's own
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
qsearch already identifies them). **Label amendment (fable5 F5-19, v47+
audit wave 2026-07-16):** EXCLUDE positions where score-shaping fired
(cantwin_clamp, the mop-up shortcut, draw_score/contempt shaping — all
detectable at label time); keep those mechanisms POST-network at inference,
exactly as they wrap `eval_white` today. Letting the net learn clamp and
contempt artifacts wastes capacity and pollutes the regression target, and
a bad-label finding in Phase 3 costs a full dataset regeneration. Any
drawishness terms shipped from the eval-sweep menu (scale factor, winnable)
also stay outside the net, preserving the twin-oracle structure through the
NNUE transition.

**F5-19 extension (F49-30, v49 audit 2026-07-17):** FI-29 (CYCLE_DETECT)
is a NEW shaping source the list above cannot catch: it is a search-
interior alpha-raise to the contempt draw (csearch.c, FI-29 probe in
negamax), not an eval-wrap, so it is invisible to label-time leaf checks.
The labeling harness MUST run with CYCLE_DETECT = 0 (v48 node-exact per
csearch.c's set_cycle comment); FI-29 stays active at inference, like all
shaping. The label-audit gate extends: sample N labeled positions,
re-search each with CYCLE_DETECT on and off, assert stored label ==
off-value. (Implemented: `NNUE/gen_data.py` constructs its engine with the
visible class attr `CYCLE_DETECT = False`; `NNUE/verify_labels.py` runs the
on/off re-search spot-check.)

**Gate:** a labeled dataset of >=5-10M positions, holdout
split, sanity-checked score distribution, and a label-set audit showing
zero shaped positions.

**Phase 3 — trainer.** Python/PyTorch (or numpy if avoiding the new
dependency matters more than convenience — CPU-only training of a net this
small is fine either way), regression loss vs the search-score labels
(optionally blended with game WDL, Stockfish-style). Must support
**quantization-aware training** (int16 feature-transformer weights, int8
second layer, clipped ReLU) since the C side needs quantized weights, not
float — training in float then naively rounding at the end reliably tanks
net quality. **Gate:** training converges (RMSE falling, holdout not
diverging from train) on the Phase-2 dataset.

**Phase 4 — C inference + incremental accumulator.**

**Amendment (F49-31, v49 audit 2026-07-18) — accumulator OFF the Board.**
The sketch below originally put the Accum (int16_t v[2][256], 1KB) ON the
88-byte copy-make Board, so every `Board c = *b;` per-node copy (and the
make_null copy) would drag 1KB of accumulator along -- an SF StateInfo
idiom that only makes sense under make/unmake. Instead the accumulator
lives in a per-thread ply-indexed stack (`static __thread Accum
g_acc[CS_MAXPLY + 62];`, mirroring g_path's sizing), updated beside the
negamax/root apply_move call sites; the child reads slot ply+1 and
copy-make needs no unmake/copy-back. Riders: feature add/remove piece
types come free from the packed move word (MV_SHIFT_MOVER/MV_SHIFT_VICTIM,
FI-02.2); make_null moves no pieces, so null moves need only the
perspective swap inside nn_eval (no accumulator write at all); qsearch
keeps HCE stand-pat in the Phase-5 hybrid, so qsearch nodes never write
the stack either -- the stack placement saves exactly those pure-waste
copies. The two-variant NPS A/B the original amendment called for is
mooted: the stack placement is decided (this build); the measured number
to record instead is nn-on vs nn-off NPS.

Wire add/remove-feature updates beside the apply_move call sites,
write `nn_eval` (`NNUE/nnue.c`, single-TU-included by csearch.c). **Verify bit-exact vs
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
-203 to -273 Elo when tried).

**Amendment (F49-B02, v49 audit 2026-07-18) — shared FI-03 TT eval cache:**
qsearch stores HCE raw_stand at depth 0 (qs_tt_store -> tt_store_raw
depth=0); negamax stores NN eval at depth>=1. During the hybrid era each
consumer accepts the cached eval only from its own origin, discriminated
by the existing TT depth field -- zero format bits. Contamination path
otherwise: a position first reached at the horizon gets a fresh-key
depth-0 store carrying an HCE raw_stand; the next ID iteration's negamax
visit of the same key reads that HCE value back as if it were an NN eval,
feeding a wrong-scale number into RFP margins, the P-04 improving flag,
and the null-move gate; symmetrically a negamax-stored NN eval would seed
qsearch stand-pat, partially reintroducing the NN-stand-pat failure this
phase exists to avoid. Implementation (~2 lines, behind g_use_nnue):
negamax rejects cached evals with TT_DEPTH < 1; qsearch rejects TT_DEPTH
!= 0. With g_use_nnue==0 both gates compile to no-ops on the hot path and
the pure-HCE tree is byte-identical (node-exact toggle-off requirement). Screen with a **2k-game run vs the current
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

---

# Phase 1 spec (FROZEN 2026-07-18) — architecture + formats

**Override note (user call, 2026-07-18):** the plain-768-first
recommendation above is overridden — the ADVANCED feature set ships first
(king-bucketed HalfKA-style inputs + a threat encoding), with plain-768
kept as a config fallback behind the same interface, to be time-efficient.
Everything below is the locked contract between `NNUE/train.py` (trainer),
`NNUE/gen_data.py` (data pipeline) and `NNUE/nnue.c` (C inference). Any
change re-freezes this section and bumps the affected format version.

## Feature set "KA8T" (feature_set id 1; plain-768 fallback = id 0)

Two perspectives (side to move first). For perspective P:

- **Orientation:** `o(sq) = sq` for P=White, `sq ^ 56` (rank flip) for
  P=Black. **Horizontal mirror:** if the oriented own-king file >= e
  (`file(o(ksq)) >= 4`), additionally `^ 7` every oriented square (king
  lands in files a–d). A king move can flip the mirror; king moves refresh
  that perspective anyway (standard HalfKA behavior), so no extra rule.
- **King buckets (KB = 8):** from the oriented+mirrored own-king square,
  `bucket = QK_MAP[rank][file >> 1]` with
  `QK_MAP = { r0:[0,1], r1:[2,3], r2:[4,5], r3:[6,6], r4-r7:[7,7] }`
  (files ab / cd after the mirror). Castled-short king (g1 -> mirrored b1)
  = bucket 0; central/advanced kings pool in 7.
- **Piece planes (12):** own P,N,B,R,Q,K = 0..5, their P,N,B,R,Q,K =
  6..11 (both kings included, HalfKA-style "A"; the own-king plane gives
  fine position within the coarse bucket).
- **Feature index** = `bucket*768 + plane*64 + o'(sq)`;
  **IN = 8 x 768 = 6144** per perspective. Active features per
  perspective = number of pieces on the board (<= 32).
- **Plain-768 fallback (id 0):** the SAME code path with KB=1, mirror
  off, threat dim 0 — selected by the weight-file header (C side) and the
  `FEATURE_SET` constant (`NNUE/config.py`, trainer side). Not a parallel
  codebase.

## Threat encoding "T16" (16 int8 inputs; the documented design choice)

Full per-piece-type attacked/defended planes (12 x 64 x 2 persp) were
rejected on measured-cost grounds: they are not incrementally updatable
(any move can change every attack ray), and with ~40–80 active plane bits
per side each eval would pay an extra ~5,000 int16 accumulate ops — an
NPS cliff on the tree's hottest call. The equivalent documented encoding
keeps the salient content (what is attacked, what is defended, what
hangs, king pressure) as 8 aggregate scalars per side, recomputed per
eval from one attack-union pass (cost measured in NNUE/README.md):

For side S (White vec then Black vec computed once; fed to the net as
[stm vec, nstm vec]):
```
A_S    = union of attacks by all S pieces (P,N,B,R,Q,K)
ring_S = KING_ATT[S king square]
t0 = min(127, 2  * popcount(A_S))                          mobility union
t1 = min(127, 16 * popcount(A_S & ring_opp))               king pressure
t2 = min(127, 32 * popcount(S minors+majors & pawn_att_opp)) pawn-on-piece
t3 = min(127, 32 * popcount(S pieces & A_opp & ~A_S))      hanging (ours)
t4 = min(127, 32 * popcount(opp pieces & A_S & ~A_opp))    hanging (theirs)
t5 = min(127, 16 * popcount(S pieces & A_opp))             attacked (all)
t6 = min(127, 16 * popcount(S non-pawns & pawn_att_S))     pawn-defended
t7 = min(127, 32 * popcount(A_S & {d4,e4,d5,e5}))          center control
```
Values are already in activation units (int8 0..127 == float 0..1).
Computed by ONE shared C function (`nnue_threat_vec`, exported as
`nnue_threats` for the Python pipeline): the dataset generator stores the
16 bytes per record at generation time, so trainer and engine consume
byte-identical threat inputs by construction. threat_ver = 1 is stamped
in the dataset header; a change to the formulas bumps it and requires
regeneration.

## Net shape (float model)

```
FT   : per perspective, acc = b1 + sum W1[:,f] over active f   (IN -> H=256)
       CReLU(x) = clamp(x, 0, 1)
tail : x  = [CReLU(acc_stm) | CReLU(acc_nstm) | T16]           (528)
       h1 = CReLU(W2 x + b2)                                   (528 -> 32)
       h2 = CReLU(W3 h1 + b3)                                  (32 -> 32)
       out= W4 h2 + b4                                         (32 -> 1)
```
H is configurable (`NNUE/config.py: HIDDEN`); 256 is the locked default.
The float model is trained to predict `cp / 400`, clamped to [-5, 5].

## Quantization (locked integer semantics — the bit-exact contract)

- QA = 127 (activation scale: float 1.0 == int 127), QB = 64 (weight
  scale for the tail), OUT_CP = 400 (float output unit in centipawns).
- FT: `W1_q = round(W1 * 127)` int16, `b1_q = round(b1 * 127)` int16.
  Accumulator = int16 sums of columns (no saturation needed: trainer
  clips FT weights to |w| <= 2.0 and biases to |b| <= 4.0, so |acc| <=
  127*(4 + 32*2) = 8636 << 32767 at <= 32 active features).
- Activation: `a = clamp(acc, 0, 127)` (int8).
- Tail layers (L2, L3): `W_q = round(W * 64)` int8 (trainer clips |w| <=
  127/64), `b_q = round(b * 127 * 64)` int32.
  `out32 = sum(W_q * a) + b_q`; next activation
  `a' = min(max(out32, 0), 8128) >> 6` — clamp FIRST to [0, 127*64],
  then shift (pure, exact, identical in C and the numpy reference).
- Output layer: `W4_q = round(W4 * 64)` int8, `b4_q = round(b4*127*64)`
  int32; `cp = (int)((int64)out32 * 400 / 8128)` (C truncating division;
  the numpy reference uses trunc-division to match).
- The trainer ships a quantized-reference forward (`NNUE/nnue_ref.py`,
  pure numpy int32) — `nnue.c`'s scalar and NEON paths must match it
  EXACTLY (not eps) on the Phase-4 gate, and match each other exactly.

## On-disk weight format (.nnue, version 1)

Little-endian, flat binary:
```
offset  size  field
0       8     magic "PYGINNUE"
8       4     u32 version        = 1
12      4     u32 feature_set    (1 = KA8T, 0 = plain768)
16      4     u32 in_dim         (6144 / 768)
20      4     u32 hidden         (H, 256)
24      4     u32 threat_dim     (16 / 0)
28      4     u32 king_buckets   (8 / 1)
32      4     u32 d2, 36 u32 d3  (tail dims, 32/32)
40      4     u32 qa, 44 u32 qb, 48 u32 out_cp   (127, 64, 400)
52      4     u32 crc32 of the payload (everything after offset 64)
56      8     u64 payload size in bytes
64      —     payload, in order:
              W1 int16 [in_dim][H]  (row per feature = one accumulate slab)
              b1 int16 [H]
              W2 int8  [d2][2H+threat_dim]
              b2 int32 [d2]
              W3 int8  [d3][d2]
              b3 int32 [d3]
              W4 int8  [1][d3]
              b4 int32 [1]
```
Loader rejects: bad magic/version, dim mismatch vs compiled limits,
CRC mismatch, or FT weights that could overflow int16 accumulation.

## Training-data format (.pygdata, version 1)

Header: `"PYGNDATA"` (8) + u32 version + u32 record_size(=88) +
u64 count + u32 threat_ver + u32 reserved. Then `count` records, 88 B:
```
u64 pawns knights bishops rooks queens kings occ_w   (56)  occ_b derived
u64 castling                                          (8)
i16 score_white_cp   (search label, White POV, CYCLE_DETECT=0 engine)
i8  result_white     (+1/0/-1 game WDL, White POV)
u8  stm  (1=White)   i8 ep (-1/sq)   u8 hmc
u8  threat[16]       (T16 bytes: White vec then Black vec)
u8  flags            (bit0 reserved)   u8 pad
```
50M positions = 4.4 GB. Records are fixed-size -> mmap-able, seekable,
trivially splittable for train/val.

## Hybrid-era inference rules (locked)

- negamax static eval = `nn_eval` (stm-relative); qsearch stand-pat stays
  HCE; the FI-03 TT eval cache is depth-gated per the F49-B02 amendment.
- Post-network shaping at inference: `cantwin_clamp` wraps `nn_eval`'s
  White-POV value exactly as it wraps `eval_white` today. The mop-up
  shortcut and simplify are HCE-internal terms and do NOT apply to the
  net. draw_score/contempt and all search-side shaping are untouched.
- Null move: `nn_eval` at a null child reads the parent's accumulator
  slot with perspectives swapped; no accumulator write.
- King move (incl. any mirror flip): full refresh of the mover's
  perspective at the child slot; the opponent perspective updates
  incrementally (kings are ordinary planes there).
