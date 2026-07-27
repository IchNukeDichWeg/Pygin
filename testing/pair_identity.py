#!/usr/bin/env python3
"""FI-100: an EXACT engagement meter for `--nodes` logs, read post-hoc.

    python3 testing/pair_identity.py "New logs/cengine_vs_engine53_*.pgn"
    python3 testing/pair_identity.py --selftest

WHY. The house's cheapest habit is the engagement dead-gate: FI-48, FI-63 and
P-33 were all closed BEFORE an A/B because the mechanism barely fires (~$54 and
three campaign slots saved). But engagement has been *estimated* from the ptnml
middle bucket, which is a proxy: DD_WL also collects genuine win+loss pairs, so
it reads high for a change that engages hard and splits the point.

A `--nodes` run is deterministic per side, and match.py plays every FEN as a
mirror PAIR -- round 2k+1 with Engine 1 as White, round 2k+2 with Engine 2 as
White, same position. If the two engines are functionally identical on that
position, both games of the pair are the SAME GAME, move for move. So the exact
engagement rate is recoverable from the logs by counting move-identical pairs,
no proxy involved.

  identical pair  ->  the change did not fire here; the pair carries ZERO
                      information about it, whatever its ptnml bucket says.

READ IT ONLY ON `--nodes` LOGS. Under a clock, two mirror games diverge from
timing jitter alone, so identity measures the host, not the change -- the
timed null below reads 0% on two copies of ONE engine. That is the meter
working, not failing, and it is why the header line is checked and reported.

**THE NULL DOES NOT READ 100%, AND THE FLOOR MOVES WITH THE NODE BUDGET.**
Measured 2026-07-27, cengine vs cengine, 25 pairs each:

  budget    identical   Elo      calibration      reading
  300k        80%       -6.95    ratio -> 1.000   healthy
  300k        80%       -0.00    ratio -> 1.000   healthy (SAME 5 pairs)
  1.75M       88%       -6.95    ratio -> 1.000   healthy, campaign point
  300k         0%      +48.96    out of deadband  ** RUN INVALID **
  A/B v54 cand vs 53, 1953 pairs @1.75M:  0.0% identical, -10.37 Elo
  timed null, 20 ms/move, 2 pairs:        0.0% identical  <- WRONG MODE

So a candidate is compared against the NULL at the same budget, never against
100%. The residual is cross-game state, not the change: TT_KEEP_WARM (P-14) is
on, so the table is NEVER wiped between games in a worker, and the two halves
of a mirror pair are played by different workers with different game histories.

**CONFIRMED 2026-07-27, and the floor is a function of GAMES PER WORKER.** The
same null on a 95-worker box with 50 games -- so at most ONE game per worker,
no carry-over possible -- read **100.00%, 25/25 pairs**, with ptnml
[0, 0, 25, 0, 0] and Elo -0.00. The Mac's 80-88% came from 9 workers sharing 50
games, ~6 games each, every one of them inheriting the previous game's table.
That is the mechanism, measured from both ends rather than argued.

Practical consequence: compute the null's floor at the SAME games-per-worker
ratio as the run being read. A 2,000-game screen on 95 workers is ~21 games per
worker and will sit well below 100% on that count alone; comparing it against a
50-game null's 100% would read as engagement that is not there.

THE ACCIDENT WORTH KEEPING. Row 4 is a real failure caught live: the same
binary on both sides scored **+48.96 Elo** because the NPS calibration landed
outside its 1% deadband and the two sides got different node budgets. The
summary looked ordinary -- a plausible Elo, a plausible ptnml. Identity read
0% and named it instantly, because two copies of one engine cannot legitimately
disagree on every pair. On a null, this meter is a HOST CHECK before it is an
engagement meter, so `--selftest`-style null detection is built into report().

The ptnml vector is printed beside it because the two disagreeing is the
interesting case: a high middle bucket with a LOW identical fraction means the
change fires everywhere and draws anyway, which is a real result, not a
dead gate. Row 3 above is exactly that shape at the null's own floor.
"""
import glob
import re
import sys

_RESULT_SCORE = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}
_MOVENO = re.compile(r"^\d+\.+$")


def parse_pgn(text):
    """[(round, fen, result, (san, ...), (white, black)), ...] -- the headers
    we use, nothing else."""
    games, hdr, moves = [], None, []

    def flush():
        if hdr is None:
            return
        toks = tuple(t for t in " ".join(moves).split()
                     if not _MOVENO.match(t) and t not in _RESULT_SCORE
                     and t != "*")
        games.append((int(hdr.get("Round", -1)), hdr.get("FEN", ""),
                      hdr.get("Result", "*"), toks,
                      (hdr.get("White", ""), hdr.get("Black", ""))))

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[Event "):
            flush()
            hdr, moves = {}, []
        elif hdr is None:
            continue
        elif line.startswith("[") and line.endswith("]"):
            m = re.match(r'\[(\w+)\s+"(.*)"\]$', line)
            if m:
                hdr[m.group(1)] = m.group(2)
        elif line:
            moves.append(line)
    flush()
    return games


def pair_report(games):
    """Group mirror pairs by round and measure identity + the ptnml vector.

    Engine 1's score is read off the ROUND, not the player names: a null runs
    the same file on both sides, so [White]/[Black] cannot tell them apart,
    while the schedule (odd round -> Engine 1 White) always can.
    """
    pairs = {}
    for rnd, fen, result, toks, _players in games:
        if rnd < 1:
            continue
        pairs.setdefault((rnd - 1) // 2, {})[(rnd - 1) % 2] = (fen, result, toks)

    penta = [0] * 5
    complete = identical = fen_mismatch = 0
    for _, slots in sorted(pairs.items()):
        if len(slots) != 2:
            continue
        (fen_a, res_a, toks_a), (fen_b, res_b, toks_b) = slots[0], slots[1]
        if res_a not in _RESULT_SCORE or res_b not in _RESULT_SCORE:
            continue                     # errored / excluded game -- no pair
        if fen_a != fen_b:
            fen_mismatch += 1            # not a mirror pair; log is malformed
            continue
        complete += 1
        if toks_a == toks_b:
            identical += 1
        # slot 0 = odd round = Engine 1 White; slot 1 = Engine 1 Black
        penta[round((_RESULT_SCORE[res_a] +
                     (1.0 - _RESULT_SCORE[res_b])) * 2)] += 1
    return complete, identical, penta, fen_mismatch


def mode_of(pgn_path):
    """The `Mode:` header from the sibling .txt, so a clock log can be named."""
    try:
        with open(pgn_path[:-4] + ".txt", encoding="utf-8", errors="replace") as fh:
            for _ in range(8):
                line = fh.readline()
                if line.startswith("Mode:"):
                    return line.strip()[6:]
    except OSError:
        pass
    return "unknown (no sibling .txt)"


def report(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        games = parse_pgn(fh.read())
    complete, identical, penta, bad_fen = pair_report(games)
    mode = mode_of(path)
    print(f"\n{path}")
    print(f"  mode                : {mode}")
    if "node" not in mode.lower():
        print("  ** NOT a --nodes log -- identity below measures host timing "
              "jitter, not the change **")
    print(f"  games / complete pairs: {len(games)} / {complete}")
    if bad_fen:
        print(f"  ** {bad_fen} pairs had mismatched FENs (malformed log) **")
    if not complete:
        return
    frac = identical / complete
    print(f"  move-identical pairs : {identical}  ({frac * 100:.2f}%)")
    print(f"  ENGAGEMENT           : {(1 - frac) * 100:.2f}%")
    print(f"  ptnml [LL LD DD/WL WD WW] : {penta}"
          f"   (middle bucket {penta[2] / complete * 100:.1f}%)")
    # A null (one engine on both sides) has a known answer, so it is a host
    # check: it must sit near the budget's floor (80-97%), and a low reading
    # means the two sides got DIFFERENT node budgets -- see the module header,
    # where exactly this scored +48.96 Elo on two copies of one engine.
    if {w for _, _, _, _, (w, b) in games if w == b} and "node" in mode.lower():
        verdict = ("** NULL INVALID: the same engine cannot disagree on this "
                   "many pairs. The --nodes calibration almost certainly "
                   "missed its deadband; the Elo from this run is noise. **"
                   if frac < 0.5 else
                   "null looks healthy (residual is cross-game TT warmth)")
        print(f"  NULL RUN (same engine both sides): {verdict}")


def selftest():
    """The control: the meter must SEE a difference of one move, and must not
    be fooled by move numbering or a colour-swapped result."""
    def g(rnd, fen, result, mv, white="A", black="B"):
        return (f'[Event "t"]\n[Round "{rnd}"]\n[White "{white}"]\n'
                f'[Black "{black}"]\n[FEN "{fen}"]\n'
                f'[Result "{result}"]\n{mv}\n')

    F, G = "fen-a", "fen-b"
    text = (g(1, F, "1-0", "1. e4 e5 2. Nf3 1-0") +
            g(2, F, "0-1", "1. e4 e5\n2. Nf3 0-1") +      # identical, wrapped
            g(3, G, "1-0", "1. e4 e5 2. Nf3 1-0") +
            g(4, G, "1-0", "1. e4 e5 2. Nc3 1-0"))        # one move apart
    complete, identical, penta, bad = pair_report(parse_pgn(text))
    assert complete == 2, complete
    assert identical == 1, identical          # pair 0 only
    assert bad == 0, bad
    # pair 0: E1 white wins, E1 black wins -> 1.0 + 1.0 = WW
    # pair 1: E1 white wins, E1 black loses -> 1.0 + 0.0 = middle
    assert penta == [0, 0, 1, 0, 1], penta

    # a truncated pair (one game missing) must not be counted at all
    complete, identical, _, _ = pair_report(parse_pgn(text[:text.index('[Event "t"]\n[Round "2"')]))
    assert (complete, identical) == (0, 0), (complete, identical)

    # an errored game ("*") drops its pair rather than scoring it
    err = g(1, F, "1-0", "1. e4 1-0") + g(2, F, "*", "1. e4 *")
    assert pair_report(parse_pgn(err))[0] == 0

    # the null detector keys off the player names, so they must survive parsing
    ab = parse_pgn(g(1, F, "1-0", "1. e4 1-0"))
    null = parse_pgn(g(1, F, "1-0", "1. e4 1-0", white="A", black="A"))
    assert ab[0][4] == ("A", "B") and null[0][4] == ("A", "A")
    assert not {w for *_, (w, b) in ab if w == b}
    assert {w for *_, (w, b) in null if w == b} == {"A"}
    print("pair_identity selftest: OK")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "--selftest":
        selftest()
    else:
        paths = [p for a in args for p in sorted(glob.glob(a)) or [a]]
        for p in paths:
            report(p.replace(".txt", ".pgn"))
