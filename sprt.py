#!/usr/bin/env python3
"""
sprt.py -- Sequential Probability Ratio Test for paired-game engine matches.

An independent implementation of the published GSPRT statistics (the
generalized sequential probability ratio test via empirical likelihood, per
Michel Van den Bergh's GSPRT write-up -- the method Fishtest also uses),
verified numerically against the tests.stockfishchess.org sprt_calc
calculator on identical inputs. It lets a match.py run STOP EARLY the moment a change is provably good or provably
bad, instead of always playing the full game budget. On a 223-worker server
that is most of the CPU/money saving -- an obvious winner (or loser) is decided
in a fraction of the games; only genuinely borderline changes run to the cap.

WHAT IT COMPUTES
----------------
Given the running PENTANOMIAL counts (game PAIRS bucketed as
LL / LD / {LW,DD,WL} / WD / WW) and two hypotheses expressed in Elo:

    H0: the change is worth <= elo0        (default 0)
    H1: the change is worth >= elo1        (default 2, normalized)

it accumulates the generalized log-likelihood ratio (LLR) and compares it to
two Wald bounds derived from the error rates alpha, beta (both 0.05 by
default):

    accept H1 (ship)    when  LLR >= log((1 - beta) / alpha)      ~ +2.94
    accept H0 (reject)  when  LLR <= log(beta / (1 - alpha))      ~ -2.94
    keep playing        otherwise

ELO MODELS (the same two offered by Fishtest's sprt_calc calculator)
-----------------------------------------
* "normalized" (default, the modern Fishtest standard): the hypotheses are in
  normalized-Elo units. elo -> target mean score uses sqrt(2 * var) of the
  pentanomial pair distribution, so the bound self-corrects for draw rate.
  This is the scale of the sprt_calc "Normalized" model AND of its elo-0/elo-1
  fields -- so `--elo1 2` here means exactly what it means on that page.
  (NOTE: match.py's printed "Normalized Elo" omits the sqrt(2) and so reads a
  factor sqrt(2) larger than this scale; that is a display-only figure, the
  SPRT here is on the sprt_calc scale by construction.)
* "logistic": classic BayesElo-style, elo -> score = 1/(1 + 10^(-elo/400)).

The statistics (GSPRT via empirical likelihood) are the published method
Fishtest also implements,
not a normal approximation: for each hypothesis mean s it finds the
maximum-likelihood pentanomial distribution q(s) constrained to that mean
(a one-parameter tilt of the observed distribution) and
    LLR = N * ( KL(p || q(s0)) - KL(p || q(s1)) ).

The draw-ratio / RMS-bias fields on sprt_calc only parametrise its a-priori
"expected number of games" estimate; the LIVE test reads the real counts, so
they are not needed here.

CLI (sanity-check against sprt_calc by pasting the same ptnml + elos):
    python3 sprt.py --ptnml 264 1157 2014 1274 291 --elo0 0 --elo1 2
    python3 sprt.py --selftest
"""

import math

# Pentanomial bucket values, normalized to a per-pair score in [0, 1]
# (LL=0, LD=1/4, {LW,DD,WL}=2/4, WD=3/4, WW=1) -- matches Fishtest's
# results_to_pdf: value i/(len-1).
_VALUES = (0.0, 0.25, 0.5, 0.75, 1.0)


def _regularize(counts):
    """No empty cell (the constrained-MLE tilt needs every value to carry
    mass). A 1e-3 placeholder in an empty bucket is negligible against the
    thousands of real pairs a decision is made on -- exactly Fishtest's rule."""
    return [c if c > 0 else 1e-3 for c in counts]


def _pdf(counts):
    """(N, [(value, prob), ...]) for the 5 pentanomial buckets."""
    reg = _regularize(counts)
    n = sum(reg)
    return n, [(v, c / n) for v, c in zip(_VALUES, reg)]


def _mean_var(pdf):
    mean = sum(p * v for v, p in pdf)
    var = sum(p * (v - mean) ** 2 for v, p in pdf)
    return mean, var


def _mle_tilt(pdf, s):
    """Constrained MLE: the distribution q_i = p_i / (1 + lam*(v_i - s)) whose
    mean is exactly s, closest in likelihood to the observed pdf. Returns the
    list of q_i. `lam` is the unique root of g(lam)=sum p_i u_i/(1+lam u_i)=0,
    monotone-decreasing on the feasible interval -> bisection is exact and
    can't diverge (unlike a Newton step near the poles)."""
    a = [v for v, _ in pdf]
    p = [pr for _, pr in pdf]
    u = [ai - s for ai in a]
    umax = max(u)
    umin = min(u)
    # feasible lam keeps every (1 + lam*u_i) > 0; s in (0,1) guarantees
    # umax>0 (from value 1) and umin<0 (from value 0), so the bracket exists.
    tiny = 1e-12
    lo = -1.0 / umax + tiny        # g(lo+) -> +inf
    hi = -1.0 / umin - tiny        # g(hi-) -> -inf

    def g(lam):
        return sum(pi * ui / (1.0 + lam * ui) for pi, ui in zip(p, u))

    for _ in range(200):           # 200 halvings ~ 1e-60 bracket: exact
        mid = 0.5 * (lo + hi)
        if g(mid) > 0.0:
            lo = mid
        else:
            hi = mid
    lam = 0.5 * (lo + hi)
    return [pi / (1.0 + lam * ui) for pi, ui in zip(p, u)]


def llr(counts, s0, s1):
    """Exact GSPRT log-likelihood ratio of the whole sample for
    H1(mean=s1) vs H0(mean=s0). >0 favours H1."""
    n, pdf = _pdf(counts)
    q0 = _mle_tilt(pdf, s0)
    q1 = _mle_tilt(pdf, s1)
    # total LLR = sum_i count_i * (log q1_i - log q0_i)
    #           = N * sum_i p_i * (log q1_i - log q0_i)
    per_pair = sum(pr * (math.log(a1) - math.log(a0))
                   for (_, pr), a0, a1 in zip(pdf, q0, q1))
    return n * per_pair


def _score_from_elo(elo, model, pdf):
    """Elo hypothesis -> target mean per-pair score in [0, 1]."""
    if model == "logistic":
        return 1.0 / (1.0 + 10.0 ** (-elo / 400.0))
    # normalized (sprt_calc default): s = 0.5 + (elo*log10/800) * sqrt(2*var)
    _, var = _mean_var(pdf)
    if var <= 0.0:
        return 0.5                 # no variance yet -> no separation, LLR->0
    return 0.5 + (elo * math.log(10.0) / 800.0) * math.sqrt(2.0 * var)


def bounds(alpha=0.05, beta=0.05):
    """(lower, upper) Wald bounds. LLR>=upper accepts H1; LLR<=lower accepts H0."""
    return (math.log(beta / (1.0 - alpha)), math.log((1.0 - beta) / alpha))


def evaluate(counts, elo0=0.0, elo1=2.0, model="normalized",
             alpha=0.05, beta=0.05):
    """Return a dict with the current LLR, bounds and decision.
    decision is 'H1' (accept, ship), 'H0' (reject) or 'continue'."""
    _, pdf = _pdf(counts)
    s0 = _score_from_elo(elo0, model, pdf)
    s1 = _score_from_elo(elo1, model, pdf)
    lo, hi = bounds(alpha, beta)
    if s0 == s1:                    # degenerate (no variance): undecided
        L = 0.0
    else:
        L = llr(counts, s0, s1)
    decision = "H1" if L >= hi else "H0" if L <= lo else "continue"
    return {"llr": L, "lower": lo, "upper": hi, "decision": decision,
            "s0": s0, "s1": s1}


# --------------------------------------------------------------------------- #
# self-test: the GSPRT is a money path, so it ships with a runnable check.
# --------------------------------------------------------------------------- #
def _llr_quadratic(counts, s0, s1):
    """Normal-approximation LLR (Fishtest's LLR_alt) -- an independent formula
    used ONLY to cross-check the exact GSPRT sign and scale in the self-test."""
    n, pdf = _pdf(counts)
    mu, var = _mean_var(pdf)
    if var <= 0.0:
        return 0.0
    return n * (s1 - s0) * (2 * mu - s0 - s1) / (2 * var)


def _selftest():
    lo, hi = bounds(0.05, 0.05)
    assert abs(hi - math.log(19)) < 1e-12 and abs(lo + math.log(19)) < 1e-12

    # 1) symmetric result at the H0 mean (equal wins/losses, mean 0.5) -> LLR<=0
    sym = [300, 1100, 2000, 1100, 300]
    r = evaluate(sym, elo0=0, elo1=2)
    assert r["llr"] < 0, r

    # 2) a real confirmed winner (FI-25 ptnml, mean well above 0.5) -> accept H1
    win = [225, 1100, 2056, 1299, 320]
    r = evaluate(win, elo0=0, elo1=2)
    assert r["decision"] == "H1" and r["llr"] > r["upper"], r

    # 3) a real loser (FI-18 ptnml leaned negative) -> LLR heads to H0
    loss = [288, 1213, 2025, 1195, 279]
    r = evaluate(loss, elo0=0, elo1=2)
    assert r["llr"] < 0, r

    # 4) exact GSPRT agrees in SIGN and within ~3% of the quadratic approx,
    #    across a spread of results (guards a scale/formula slip)
    for c in (sym, win, loss, [260, 1150, 2000, 1300, 290], [400, 1300, 2000, 1000, 300]):
        _, pdf = _pdf(c)
        s0 = _score_from_elo(0, "normalized", pdf)
        s1 = _score_from_elo(2, "normalized", pdf)
        exact = llr(c, s0, s1)
        approx = _llr_quadratic(c, s0, s1)
        assert (exact >= 0) == (approx >= 0), (c, exact, approx)
        assert abs(exact - approx) <= 0.03 * max(1.0, abs(approx)) + 0.02, (c, exact, approx)

    # 5) monotonicity: scaling a winning sample up drives LLR up (more evidence)
    small = evaluate([56, 275, 514, 325, 80], elo0=0, elo1=2)["llr"]   # win/4
    big = evaluate([225, 1100, 2056, 1299, 320], elo0=0, elo1=2)["llr"]
    assert big > small > 0, (small, big)

    # 6) logistic model sanity: a 0.55 mean-score sample favours H1 over H0=0
    r = evaluate([100, 800, 2000, 1200, 900], elo0=0, elo1=5, model="logistic")
    assert r["llr"] > 0, r

    print("sprt self-test: all checks passed")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="SPRT for paired-game matches "
                                 "(published GSPRT statistics; agrees with Fishtest's sprt_calc).")
    ap.add_argument("--ptnml", type=int, nargs=5,
                    metavar=("LL", "LD", "DD_WL", "WD", "WW"),
                    help="pentanomial pair counts")
    ap.add_argument("--elo0", type=float, default=0.0)
    ap.add_argument("--elo1", type=float, default=2.0)
    ap.add_argument("--model", choices=("normalized", "logistic"),
                    default="normalized")
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--beta", type=float, default=0.05)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest or not args.ptnml:
        _selftest()
    else:
        r = evaluate(args.ptnml, args.elo0, args.elo1, args.model,
                     args.alpha, args.beta)
        n = sum(args.ptnml)
        verdict = {"H1": "ACCEPT H1 (change is good -- ship)",
                   "H0": "ACCEPT H0 (change rejected)",
                   "continue": "CONTINUE (no decision yet)"}[r["decision"]]
        print(f"SPRT[{args.elo0:g}, {args.elo1:g}]  model={args.model}  "
              f"alpha={args.alpha:g} beta={args.beta:g}")
        print(f"  pairs        {n:,}")
        print(f"  LLR          {r['llr']:+.3f}   "
              f"(bounds {r['lower']:+.3f} .. {r['upper']:+.3f})")
        print(f"  target score {r['s0']:.5f} (H0) .. {r['s1']:.5f} (H1)")
        print(f"  -> {verdict}")
