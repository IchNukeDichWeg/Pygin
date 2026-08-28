#!/usr/bin/env python3
"""make_readme_charts.py -- regenerate the README's progression SVGs.

    python3 make_readme_charts.py    # writes docs/elo_progression.svg
                                     #    and docs/speed_progression.svg

Two hand-rolled SVG line charts (no matplotlib): cumulative A/B Elo and the
single-thread NPS multiplier, both across the C era (v31+). Colours are the
GitHub palette on a transparent background, with mid-grey axes/labels that
read on both the light and dark site themes.

The DATA table below is the source of truth for the charts. Update it in the
same commit that adds a version's README row -- ELO_DELTA is that version's
A/B result, NPS_M its single-thread bench (millions of nodes/s).
"""
import os

# v: (A/B Elo delta vs the previous version, single-thread NPS in millions).
# v31 is the C-era baseline (delta 0); its +215 vs v30 is odds-derived, not an
# A/B, so it does not enter the cumulative line.
DATA = {
    31: (0.00, 5.23), 32: (7.30, 5.18), 33: (23.52, 5.23), 34: (6.81, 5.26),
    35: (72.00, 6.00), 36: (24.67, 6.72), 37: (0.17, 6.37), 38: (1.36, 6.16),
    39: (8.86, 7.01), 40: (4.31, 7.07), 41: (-2.88, 7.04), 42: (3.27, 7.36),
    43: (5.18, 7.29), 44: (13.31, 7.57), 45: (13.52, 7.23), 46: (5.94, 7.06),
    47: (3.16, 6.99), 48: (4.73, 6.96), 49: (0.97, 6.93), 50: (1.60, 6.84),
    51: (11.12, 6.84), 52: (6.63, 6.89), 53: (37.52, 6.90), 54: (31.20, 6.74),
    55: (9.66, 6.93), 56: (11.44, 7.16), 57: (0.00, 6.99), 58: (19.11, 5.19),
    59: (13.84, 5.75), 60: (0.00, 5.68), 61: (15.89, 5.59),
}

# EVERY NPS FIGURE ABOVE IS FROM ONE MACHINE and one instrument:
# nps_history_bench.py's trimmed mean across 10 positions at 4s/run, swept
# v1-v61 on a Mac17,8 (Apple M5 Pro), 2026-08-27/29. The column used to mix
# a per-version bench-signature NPS taken on older hardware, which made the
# multiplier chart read 1.43x at v58 where one consistent machine reads
# 0.99x -- a hardware artefact plotted as an engine trend. NPS is
# architecture-specific (the same node-identical change measured +8.3% on
# x86 and +13.5% on arm64), so this column is comparable to itself and to
# nothing else. Re-sweep the whole range when the machine changes; do not
# append a single version measured somewhere else.

# Mate-finding across the C era: matetrack on mates2000.epd @ 0.25s,
# concurrency 6, one machine. (version, found%, best%) -- "found" is any
# mate, "best" is the SHORTEST mate. v1-v30 predate the C core and are not
# runnable through cuci_old, so the series starts at v31.
MATETRACK = [
    (31, 23.4, 21.1), (32, 26.2, 23.3), (33, 26.1, 23.3), (34, 26.9, 23.9),
    (35, 40.5, 35.4), (36, 43.35, 37.6), (37, 43.3, 38.0), (38, 50.55, 44.15),
    (39, 51.55, 45.2), (40, 51.35, 44.95), (41, 50.75, 44.2), (42, 51.05, 44.6),
    (43, 51.05, 44.85), (44, 51.45, 45.15), (45, 51.55, 44.5), (46, 51.1, 44.0),
    (47, 50.2, 43.15), (48, 50.35, 43.05), (49, 51.6, 44.15), (50, 51.15, 44.55),
    (51, 51.4, 44.35), (52, 47.7, 41.0), (53, 48.75, 41.45), (54, 48.45, 41.05),
    (55, 48.25, 41.0), (56, 42.75, 36.2), (57, 43.25, 36.6), (58, 37.15, 33.1),
    (59, 39.95, 35.8), (60, 40.45, 35.75), (61, 41.0, 36.35),
]

# Knight odds win% vs FULL-STRENGTH Stockfish 18 -- the external yardstick.
# Four real measurements (odds.py records them all): v31/v49/v52 at 400-1,000
# games each, then the PST candidate that shipped as v54 running it OUT --
# 197 games, zero SF wins and zero draws. Knight odds is a closed rung now;
# pawn odds (f2) is the live yardstick, not yet measured.
ODDS_KNIGHT = [(31, 76.75), (49, 79.05), (52, 81.65), (54, 100.0)]
# Pawn odds (f2) is the ACTIVE rung -- the only handicap SF still scores
# against. One version measured so far (v54), so it draws as a lone dot beside
# the closed knight line rather than a one-point "trend".
# 87.66% is the pooled NEW-regime figure (1,900 games: 1,500 + 400), i.e. SF
# managing its own clock via go wtime/btime (FI-88). The pre-FI-88 number was
# 84.88% over 2,000 games, when OUR time_manager budgeted SF's moves -- that
# turned out to OVER-feed it (~926ms/move where SF gives itself 549ms median),
# so the old figure UNDERSTATED us by +2.78% +/-2.16 (z=2.53).
# v58 2026-08-05: 90.30% over 500 games (420W/63D/17L). NOT a clean
# continuation of the v54 dot -- the TC era moved 45+0.15 -> 50+0.50 AND the
# worker count halved to cores/2 so SF is never starved. Both points are real
# measurements of the same rung; the line between them crosses an era change.
ODDS_PAWN = [(54, 87.66), (58, 90.30)]
# The odds LADDER vs full-strength SF: how big a material handicap the engine
# can spot it and still win. Latest measurement of each. Queen, rook and
# knight are all SATURATED at v54 -- rook was re-measured 2026-07-24 (106
# games, zero SF wins and zero draws) and the old 95.5% turned out to be a
# stale v49 number, not a real inversion under knight. Pawn is the only rung
# with headroom left, which is why it is the active yardstick.
ODDS_LADDER = [("Queen", 100.0, "v-"), ("Rook", 100.0, "v54"),
               ("Knight", 100.0, "v54"), ("Pawn", 90.30, "v58")]

W, H = 760, 300
ML, MR, MT, MB = 58, 22, 44, 34          # margins
AXIS = "#8b949e"                          # readable on light AND dark GitHub
GRID = "#8b949e33"
SVG_OPEN = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
            f'viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid meet" '
            f'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">')


def _chart(title, unit, xs, ys, colour, fmt, y0=None, ymax=None):
    y0 = min(ys) if y0 is None else y0
    ymax = max(ys) if ymax is None else ymax
    pad = (ymax - y0) * 0.08 or 1
    lo, hi = y0 - pad * (y0 != 0), ymax + pad
    px = lambda x: ML + (x - xs[0]) / (xs[-1] - xs[0]) * (W - ML - MR)
    py = lambda y: H - MB - (y - lo) / (hi - lo) * (H - MT - MB)
    pts = [(px(x), py(y)) for x, y in zip(xs, ys)]

    s = [SVG_OPEN]
    s.append(f'<defs><linearGradient id="g{colour[1:]}" x1="0" y1="0" x2="0" y2="1">'
             f'<stop offset="0" stop-color="{colour}" stop-opacity="0.28"/>'
             f'<stop offset="1" stop-color="{colour}" stop-opacity="0"/></linearGradient></defs>')
    s.append(f'<text x="{ML}" y="24" fill="{AXIS}" font-size="15" '
             f'font-weight="700">{title}</text>')
    s.append(f'<text x="{W-MR}" y="24" fill="{AXIS}" font-size="12" '
             f'text-anchor="end">{unit}</text>')

    # horizontal gridlines + y labels
    for i in range(5):
        yv = lo + (hi - lo) * i / 4
        yy = py(yv)
        s.append(f'<line x1="{ML}" y1="{yy:.1f}" x2="{W-MR}" y2="{yy:.1f}" stroke="{GRID}"/>')
        s.append(f'<text x="{ML-8}" y="{yy+4:.1f}" fill="{AXIS}" font-size="11" '
                 f'text-anchor="end">{fmt(yv)}</text>')
    # x labels
    for xv in [xs[0]] + list(range(35, xs[-1], 5)) + [xs[-1]]:
        s.append(f'<text x="{px(xv):.1f}" y="{H-12}" fill="{AXIS}" font-size="11" '
                 f'text-anchor="middle">v{xv}</text>')

    area = f'M{pts[0][0]:.1f},{py(lo):.1f} ' + " ".join(
        f'L{x:.1f},{y:.1f}' for x, y in pts) + f' L{pts[-1][0]:.1f},{py(lo):.1f} Z'
    s.append(f'<path d="{area}" fill="url(#g{colour[1:]})"/>')
    s.append(f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x,y in pts)}" '
             f'fill="none" stroke="{colour}" stroke-width="2.5" '
             f'stroke-linejoin="round"/>')
    # end dot + value
    ex, ey = pts[-1]
    s.append(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="4" fill="{colour}"/>')
    # Keep the final value INSIDE the plot. On a monotonically rising series
    # the last point is also the highest, so ey-9 lands above the top margin
    # and the number is clipped by the chart frame -- which is exactly the
    # number a reader came for. Flip it below the dot when it would not fit.
    ly = ey - 9 if ey - 9 > MT + 12 else ey + 19
    s.append(f'<text x="{ex-8:.1f}" y="{ly:.1f}" fill="{colour}" font-size="13" '
             f'font-weight="700" text-anchor="end">{fmt(ys[-1])}</text>')
    s.append('</svg>')
    return "\n".join(s)


def _line_points(title, unit, series, ylo, yhi):
    """Sparse line chart (few, irregular x), one labelled dot per point.

    `series` is [(name, colour, pts)]. A series with a single point draws
    as a lone dot -- deliberate: pawn odds has exactly one measurement, and
    a one-point "trend" line would be a drawn claim we cannot support.
    """
    xs = sorted({p[0] for _, _, pts in series for p in pts})
    span = (xs[-1] - xs[0]) or 1
    px = lambda x: ML + (x - xs[0]) / span * (W - ML - MR)
    py = lambda y: H - MB - (y - ylo) / (yhi - ylo) * (H - MT - MB)
    s = [SVG_OPEN]
    s.append('<defs>' + "".join(
        f'<linearGradient id="g{c[1:]}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{c}" stop-opacity="0.28"/>'
        f'<stop offset="1" stop-color="{c}" stop-opacity="0"/></linearGradient>'
        for _, c, _ in series) + '</defs>')
    s.append(f'<text x="{ML}" y="24" fill="{AXIS}" font-size="15" font-weight="700">{title}</text>')
    s.append(f'<text x="{W-MR}" y="24" fill="{AXIS}" font-size="12" text-anchor="end">{unit}</text>')
    for i in range(5):
        yv = ylo + (yhi - ylo) * i / 4; yy = py(yv)
        s.append(f'<line x1="{ML}" y1="{yy:.1f}" x2="{W-MR}" y2="{yy:.1f}" stroke="{GRID}"/>')
        s.append(f'<text x="{ML-8}" y="{yy+4:.1f}" fill="{AXIS}" font-size="11" '
                 f'text-anchor="end">{yv:.0f}%</text>')
    for name, colour, pts in series:
        P = [(px(x), py(y)) for x, y, *_ in pts]
        if len(P) > 1:
            area = f'M{P[0][0]:.1f},{py(ylo):.1f} ' \
                + " ".join(f'L{x:.1f},{y:.1f}' for x, y in P) \
                + f' L{P[-1][0]:.1f},{py(ylo):.1f} Z'
            s.append(f'<path d="{area}" fill="url(#g{colour[1:]})"/>')
            s.append(f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x,y in P)}" '
                     f'fill="none" stroke="{colour}" stroke-width="2.5" '
                     f'stroke-linejoin="round"/>')
        for i, ((x, y), (cx, cy)) in enumerate(zip(pts, P)):
            s.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="{colour}"/>')
            # a value label on the last dot would overhang the viewBox: right-align it
            anchor, tx = ("end", W - MR) if cx > W - MR - 30 else ("middle", cx)
            # a lone dot at the right edge would put its value on top of the
            # other series' line -- hang it to the LEFT of the dot instead
            if len(P) == 1 and cx > W - MR - 30:
                anchor, tx = "end", cx - 9
            # ...and a steep NEXT segment would draw straight through a centred
            # label (v52's 81.65% vs the climb to 100%), so push that one left
            if i + 1 < len(P) and P[i + 1][1] < cy - 40:
                anchor, tx = "end", cx - 7
            s.append(f'<text x="{tx:.1f}" y="{cy-11:.1f}" fill="{colour}" font-size="12.5" '
                     f'font-weight="700" text-anchor="{anchor}">{y:.2f}%</text>')
        # series name rides the FIRST dot, so the two lines are tellable apart
        # -- flipped to the left when that dot is hard against the right edge
        # (the single-point pawn series sits on the last x)
        nx, nanch = ((P[0][0] - 9, "end") if P[0][0] > W - MR - 40
                     else (P[0][0] + 9, "start"))
        s.append(f'<text x="{nx:.1f}" y="{P[0][1]+16:.1f}" fill="{colour}" '
                 f'font-size="12" font-weight="600" text-anchor="{nanch}">{name}</text>')
    for x in xs:
        s.append(f'<text x="{px(x):.1f}" y="{H-12}" fill="{AXIS}" font-size="11" '
                 f'text-anchor="middle">v{x}</text>')
    s.append('</svg>')
    return "\n".join(s)


def _bars(title, unit, rows, colour):
    """Horizontal bars, one per label, values 0..100%."""
    n = len(rows); gap = 16
    bh = (H - MT - MB - gap * (n - 1)) / n
    x0 = ML + 46
    px = lambda v: x0 + v / 100 * (W - MR - x0 - 52)
    s = [SVG_OPEN]
    s.append(f'<text x="{ML-30}" y="24" fill="{AXIS}" font-size="15" font-weight="700">{title}</text>')
    s.append(f'<text x="{W-MR}" y="24" fill="{AXIS}" font-size="12" text-anchor="end">{unit}</text>')
    for i, (label, v, note) in enumerate(rows):
        y = MT + i * (bh + gap)
        s.append(f'<rect x="{x0}" y="{y:.1f}" width="{W-MR-x0-52:.1f}" height="{bh:.1f}" '
                 f'rx="4" fill="{GRID}"/>')
        s.append(f'<rect x="{x0}" y="{y:.1f}" width="{px(v)-x0:.1f}" height="{bh:.1f}" '
                 f'rx="4" fill="{colour}"/>')
        s.append(f'<text x="{x0-10}" y="{y+bh/2+5:.1f}" fill="{AXIS}" font-size="13" '
                 f'font-weight="600" text-anchor="end">{label}</text>')
        # a near-full bar pushes its value label into the note column, so put
        # that one INSIDE the bar (dark on orange reads in both GitHub themes)
        vx, van, vfill = px(v) + 8, "start", colour
        if vx + 46 > W - MR - 26:
            vx, van, vfill = px(v) - 10, "end", "#0d1117"
        s.append(f'<text x="{vx:.1f}" y="{y+bh/2+5:.1f}" fill="{vfill}" font-size="13" '
                 f'font-weight="700" text-anchor="{van}">{v:.1f}%</text>')
        s.append(f'<text x="{W-MR}" y="{y+bh/2+5:.1f}" fill="{AXIS}" font-size="11" '
                 f'text-anchor="end">{note}</text>')
    s.append('</svg>')
    return "\n".join(s)


def main():
    os.makedirs("docs", exist_ok=True)
    vs = sorted(DATA)
    cum, running = [], 0.0
    for v in vs:
        running += DATA[v][0]
        cum.append(running)
    base = DATA[vs[0]][1]
    mult = [DATA[v][1] / base for v in vs]

    open("docs/elo_progression.svg", "w").write(_chart(
        "Cumulative A/B Elo", "vs v31 baseline", vs, cum, "#3fb950",
        lambda y: f"+{y:.0f}", y0=0, ymax=380))
    open("docs/speed_progression.svg", "w").write(_chart(
        "Single-thread speed", "x v31", vs, mult, "#58a6ff",
        lambda y: f"{y:.2f}x", y0=0.9, ymax=1.55))
    open("docs/mate_progression.svg", "w").write(_line_points(
        "Mate-finding on mates2000.epd", "0.25s/position",
        [("any mate", "#f0883e", [(v, f) for v, f, _ in MATETRACK]),
         ("shortest", "#a371f7", [(v, b) for v, _, b in MATETRACK])],
        18, 56))
    open("docs/odds_knight.svg", "w").write(_line_points(
        "Odds win% vs full-strength SF-18", "knight closed -> pawn active",
        [("knight", "#a371f7", ODDS_KNIGHT), ("pawn", "#3fb950", ODDS_PAWN)],
        74, 102))
    open("docs/odds_ladder.svg", "w").write(_bars(
        "Odds it can spot full-strength SF-18 and still win", "latest each",
        ODDS_LADDER, "#f0883e"))
    print("wrote 5 SVGs to docs/")
    print(f"  cumulative Elo v31->newest: +{cum[-1]:.0f}   "
          f"speed: {mult[-1]:.2f}x (peak {max(mult):.2f}x)")
    print(f"  mate-finding v31->newest: {MATETRACK[0][1]:.1f}% -> "
          f"{MATETRACK[-1][1]:.1f}% found, {MATETRACK[-1][2]:.1f}% shortest")
    print(f"  knight odds: {ODDS_KNIGHT[-1][1]}%   ladder: "
          + ", ".join(f"{l} {v}%" for l, v, _ in ODDS_LADDER))


if __name__ == "__main__":
    main()
