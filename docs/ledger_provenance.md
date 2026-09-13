# What the ledger is allowed to count

The ledger is the running total of confirmed Elo over the v31 baseline. Every
release adds its measured gain to it. The open question (D-18) has always been
which measurements may be added, because most of them stopped at a sequential
bound and a bound-stopped magnitude is biased upward by construction: the test
stops the instant it crosses, which is always at a favourable fluctuation.

Until 2026-09-13 that bias was an argument. Now it is a measurement.

## The measurement

v64 was played against v58 on a **fixed** 10,000-game budget at 50+0.5, no
SPRT, nothing stopped early:

```
Elo   | +93.87 +/- 4.6   (63.19%, nElo +145.31, pair ratio 4.35)
Games | N: 10000  W: 4217  L: 1579  D: 4204
Penta | [53, 556, 1742, 1998, 651]  5000 pairs
Conf  | TIMED 50+0.50, x86, 48 workers, Threads=1, Hash=192MB both sides
```

The individually claimed gains across the same span sum to about **+146**:

| Release | Claimed | How it stopped |
|---|---|---|
| v59 | +13.84 | bound |
| v60 | +0.52 | kept on null |
| v61 | +15.89 | **fixed budget, unbiased** |
| v63 (clean core + window) | +20.95 | bound |
| v63 (FI-38 K=50) | +9.50 | bound |
| v64 (the bundle) | +6.19 | bound (first tranche unbiased at +5.94) |
| v64 (the net) | +78.82 | bound, at the minimum |
| **sum** | **~+145.7** | |
| **measured in one jump** | **+93.87 +/- 4.6** | fixed |

**About two thirds of the chained total survives.** The missing third is what
bound-stopped magnitudes accumulate to when they are banked one after another.

## What this does not say

- It does not say any single verdict was wrong. Every one of those changes was
  measured as better than what preceded it, and the sign of each is not in
  question. What inflates is the SIZE, and only the size.
- It does not transfer to another span without measuring it. The inflation
  depends on how many links stopped early and how close each ran to its bound.
- It is one measurement of one chain. A second jump (v64 vs v61) is running and
  will say whether the shortfall is spread evenly or concentrated in particular
  releases.

## The options, for the owner to choose

1. **Strict.** Only fixed-budget numbers enter the ledger. Bound-stopped
   verdicts ship the change but add nothing. Under this rule the C-era total
   reads roughly +278 rather than ~+354, and it only moves when someone pays for
   a full-budget run.
2. **Corrected.** Bound-stopped gains enter at a haircut calibrated by jumps like
   this one (two thirds, on the evidence so far). Cheap, but the haircut is
   itself an estimate and would need re-calibrating as more jumps are measured.
3. **Anchored.** The ledger is defined by periodic fixed-budget jumps against an
   older release, and per-release verdicts are recorded but never summed. The
   total is then always something that was directly measured, at the cost of one
   long run every few releases.

Whichever is chosen, the rule belongs in this file, and every release note
should keep saying which kind of number it is quoting.
