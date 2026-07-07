# Design: C search core (roadmap #29/#30)

**Status:** planning / phase 0. **Date:** 2026-07-08. **Baseline:** v30 (~90k NPS, ~2560 internal Elo).

## The finding that reframes the project

`movegen.c` already contains a complete, perft-verified C board: an ~80-byte
`Board` struct, `apply_move` (copy-make), `gen_legal`, `in_check`, and a tight
all-C `perft_rec` loop. Measured throughput of that loop (gen + copy-make):

| Loop | Nodes/sec |
|------|-----------|
| C perft (Kiwipete d5/d6) | **~168,000,000** |
| Current engine search (v30) | ~90,000 |

**Make/unmake is therefore NOT the bottleneck** — it runs ~1,800× faster than
the search already. The ~90k ceiling is the *per-node Python work*: the eval
glue (accumulator + pawn structure + taper + the ctypes mobility call),
`order_moves` (MVV-LVA + history + SEE), the dict transposition table, the
pruning gates (RFP/null/razor/futility/LMR/LMP), and the ctypes boundary
crossings between all of them.

Consequence: **porting make/unmake alone would barely help** (confirmed
historically — `fastboard.py`, a faster pure-Python board layer, gave only +9%
CPython / ~0 PyPy). The win only materialises if the **entire per-node loop**
runs in C, so there is no Python interpreter cost and no ctypes crossing per
node. That is a "C search core", not a "make/unmake port".

## Upside / risk

- **Upside:** even reaching 1–2M NPS (a small fraction of the 168M board
  ceiling, throttled by eval/ordering/TT complexity) is a **10–20× NPS gain**
  → several extra plies of depth. That is worth well over +100 Elo (the
  ~3 Elo/1% rule is for small deltas and saturates over 10×, but the depth
  gain alone is large). Plausibly ~2560 → 2700+.
- **Cost:** weeks-to-months. It is effectively a second engine in C that must
  reproduce (or beat) the Python engine's play. The whole feature set —
  negamax, PVS, aspiration, TT, killers/history/continuation history, SEE
  ordering, LMR/LMP, null-move, RFP, razoring, futility, extensions, quiescence
  — has to be ported.
- **Risk:** high (large surface), but the project's proven perft + differential
  verification recipe de-risks each phase, and phases 1–2 are node-identical
  verifiable before any behaviour changes.

## Phased plan (each phase independently verifiable + GO/NO-GO gate)

**Phase 1 — C board layer the search can drive.**
Foundation mostly exists (`Board`, `apply_move`, `gen_legal`, `in_check`,
perft). Copy-make is the chosen model — an 80-byte struct copy is free in C and
avoids undo-stack bugs. Remaining work: the accessor/state API a search needs
(zobrist maintained on the struct, repetition detection, halfmove clock,
insufficient-material, piece lookups). **Gate:** perft exact on the full suite
(already passing) + differential vs python-chess over millions of positions.

**Phase 2 — C static eval (one entry point).**
Port the full static eval into one C function: material + PST + phase taper +
tempo + pawn structure + mobility + king safety (mobility/king-safety already
in `eval_c.c`; add the rest). **Gate:** differential vs the Python eval on
millions of positions, bit-exact (the Python eval is the oracle).
**GO/NO-GO #1:** with board + eval in C, prototype a fixed-depth C alpha-beta
(no TT, no move ordering beyond MVV-LVA) and measure NPS. If it is not already
≥ 5–10× the Python engine, the per-node constant factors are worse than this
analysis predicts — stop and reassess before phase 3.

**Phase 3 — C negamax core (the big phase).**
Port ordering (history/killers/continuation/SEE), a C array TT (fixed-size,
packed entries — `shared_tt.py`'s layout is the prototype), and every pruning
rule. Driven from Python only at the root; Python keeps time management, the
book/tablebase probe, and the UCI/GUI boundary. **Gate:** this is a NEW engine,
not byte-identical to v30 — verify by (a) same-or-better tactical suite,
(b) A/B vs v30 (must be strongly positive), (c) perft still exact.

**Phase 4 — integration.** Time management hooks, SMP (the C core is
GIL-free, so real threads become possible), UCI/GUI. Snapshot as the first
`v3x` of the C era.

## Recommendation

The upside is the single largest remaining lever (the only path to *multiples*
of NPS, and NPS is what converts to Elo here), but it is a genuine
weeks-to-months commitment that forks the codebase into a C engine. Do it
**only** with the GO/NO-GO gate at the end of phase 2 respected — that gate is
cheap (board + eval + a toy alpha-beta) and answers "will the constant factors
actually deliver the 10×?" before committing to the expensive phase 3.

If that appetite isn't there, v30 is a strong place to stop (~2560 internal,
dead-even with full Stockfish at rook odds, +139 Elo in a week).
