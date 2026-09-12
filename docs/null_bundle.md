# The NULL bundle

Settings that screened POSITIVE but did not cross a bound at the 10,000-game
cap. House rule (owner call 2026-09-12): **one screen per idea**. A run that
does not cross is a NULL, it is not re-run at a longer budget, and it is not
swept across more points -- it lands here instead. When the list is long
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

## Excluded, and why

- `SEE_SCALED_K` 100 (+0.63 +/- 4.8): contradicts the K=75 entry. One K per
  bundle, and 75 is the stronger reading.
- `SOFT_STOP_UNSTABLE_FRAC` 0.70 (+1.22 +/- 4.6): the same knob accepted at
  0.90 in the other direction, so 0.70 is settled as the wrong side.

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
