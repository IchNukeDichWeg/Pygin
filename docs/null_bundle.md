# The NULL bundle

Settings that screened POSITIVE but did not cross a bound at the 10,000-game
cap. House rule (owner call 2026-09-12): **one screen per idea, plus at most
one extension when the bound is close.** At the cap, read match.py's own
"approx. N more games to accept/reject" line: if N <= 4,000 (about 14,000 games
total), run ONE extension tranche and let it decide; if N is larger, the screen
is a NULL. A NULL is not swept across more points and never gets a second
extension -- it lands here instead. When the list is long
enough, every independent entry is armed together as ONE arm and measured in a
single run that goes all the way.

Why a bundle at all: each of these is inside its own error bar, so no single
one can be resolved without spending a full campaign on a sub-1-Elo effect.
Several independent ones together are a larger target that one run can resolve.

## The list

Every entry measured at 10+0.1, 48 workers, 2x EPYC 7443, against the v63 tree,
full 10,000-game budget, no bound crossed.

| Setting | HEAD | Candidate | Elo | LLR | State file |
|---|---|---|---|---|---|
| `SEE_SCALED_K` | 50 | 75 | +2.74 +/- 4.7 | +0.656 | `NNUE/campaigns/sprt_see75_tc10+0.1.json` |
| `SOFT_STOP_STABLE_FRAC` | 0.40 | 0.45 | +2.54 +/- 4.7 | +0.547 | `NNUE/campaigns/sprt_ss045_tc10+0.1.json` |
| `SOFT_STOP_STABLE_ITERS` | 2 | 3 | +1.01 +/- 4.7 | -0.180 | `NNUE/campaigns/sprt_ssi3_tc10+0.1.json` |
| `LMR_DIV` | 200 | 170 | +4.66 +/- 4.7 | +1.573 | `NNUE/campaigns/sprt_lmr170_tc10+0.1.json` |

None qualified for the extension: LMR 170 was the closest and still estimated
8,721 more games to accept at the cap, and the other three were estimating tens
of thousands. LMR 170 is the strongest entry here and the only one with a
mechanism behind it -- its opposite direction (230, less reduction) was REJECTED
at -7.94 +/- 5.8, so the sign is corroborated rather than a lone positive draw.

## Excluded, and why

- `SEE_SCALED_K` 100 (+0.63 +/- 4.8): contradicts the K=75 entry. One K per
  bundle, and 75 is the stronger reading.
- `SOFT_STOP_UNSTABLE_FRAC` 0.70 (+1.22 +/- 4.6): the same knob screened
  positive at 0.90 in the other direction, so 0.70 is settled as the wrong side.
- `SOFT_STOP_UNSTABLE_FRAC` 0.90: NOT a bundle candidate. It screened +6.95 +/-
  4.7 at 10+0.1 (near-bound accept) and then read +2.15 +/- 4.3 over a full
  10,000 games at the SHIPPING instrument, LLR +0.47. It already had its run at
  50+0.5 and the shipping instrument decided; a screen gain that does not
  survive the longer clock is not evidence to re-bundle, it is the answer.

## The other bundle: several screens accept at once

Owner call 2026-09-12. A 50+0.5 confirm costs 6-10 box-hours, so confirming
four winners one at a time is a day and a half of billing. When more than one
screen accepts in the same batch:

1. **Cut anything mutually exclusive.** Two points of the SAME knob (LMR 230 vs
   LMR 170) cannot both ride; only the stronger one continues, and two opposite
   directions both "accepting" is evidence the screen is noisy, not that both
   help.
2. **Arm every survivor in ONE shim and run a single 50+0.5 confirm.** The
   shipping instrument decides, once.
3. **Bundle confirms -> it ships as one version, one ledger line**, and the
   notes say the bundle was measured rather than implying each member was.
4. **Bundle fails -> spend ONE confirm on the strongest single member**, and the
   rest join the NULL list above. A gain at 10+0.1 that dies at 50+0.5 usually
   means the change buys shallow-search speed that a deeper search does not
   need, which is worth writing down when it happens.

Members that are not independent (ProbCut margin and the lazy-NNUE margin both
change how far the static eval is trusted) are reported as ONE policy change,
not as several.

## The bundled run, when it happens

The three entries above are independent knobs -- one prunes captures, two shape
the clock -- so they can be armed in a single shim without confounding each
other. Their point estimates sum to about +6.3, which a 10,000-game screen can
resolve if the effect is additive.

A bundle verdict does NOT price its members. FI-24 set the precedent: a batch
that measures positive says the batch pays, not which rider paid. If a bundle
accepts, the members are only separable by spending a screen each, which is
exactly what this list exists to avoid -- so an accepted bundle ships as a
bundle and stays one line in the ledger.
