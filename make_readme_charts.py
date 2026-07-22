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
    31: (0.0, 2.34), 32: (7.30, 2.39), 33: (23.52, 2.11), 34: (6.81, 2.13),
    35: (72.0, 2.70), 36: (24.67, 3.19), 37: (0.17, 3.16), 38: (1.36, 3.09),
    39: (8.86, 3.36), 40: (4.31, 3.34), 41: (-2.88, 3.31), 42: (3.27, 3.31),
    43: (5.18, 3.23), 44: (13.31, 3.67), 45: (13.52, 3.38), 46: (5.94, 3.74),
    47: (3.16, 3.19), 48: (4.73, 3.07), 49: (0.97, 3.14), 50: (1.60, 3.22),
    51: (11.12, 3.79), 52: (6.63, 3.79), 53: (37.52, 3.76),
}

# Knight odds win% vs FULL-STRENGTH Stockfish 18 -- the external yardstick.
# Three real measurements, 1,000+ games each (README "Measured strength").
ODDS_KNIGHT = [(31, 76.75), (49, 79.05), (52, 81.65)]
# The odds LADDER vs full-strength SF: how big a material handicap the engine
# can spot it and still win. Latest measurement of each (queen saturated at
# 100/100 games; rook 95.5% at v49; knight 81.65% at v52).
ODDS_LADDER = [("Queen", 100.0, "v-"), ("Rook", 95.5, "v49"),
               ("Knight", 81.65, "v52")]

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
    s.append(f'<text x="{ex-8:.1f}" y="{ey-9:.1f}" fill="{colour}" font-size="13" '
             f'font-weight="700" text-anchor="end">{fmt(ys[-1])}</text>')
    s.append('</svg>')
    return "\n".join(s)


def _line_points(title, unit, pts, colour, ylo, yhi):
    """A sparse line chart (few, irregular x) with a labelled dot per point."""
    xs = [p[0] for p in pts]
    px = lambda x: ML + (x - xs[0]) / (xs[-1] - xs[0]) * (W - ML - MR)
    py = lambda y: H - MB - (y - ylo) / (yhi - ylo) * (H - MT - MB)
    s = [SVG_OPEN]
    s.append(f'<defs><linearGradient id="g{colour[1:]}" x1="0" y1="0" x2="0" y2="1">'
             f'<stop offset="0" stop-color="{colour}" stop-opacity="0.28"/>'
             f'<stop offset="1" stop-color="{colour}" stop-opacity="0"/></linearGradient></defs>')
    s.append(f'<text x="{ML}" y="24" fill="{AXIS}" font-size="15" font-weight="700">{title}</text>')
    s.append(f'<text x="{W-MR}" y="24" fill="{AXIS}" font-size="12" text-anchor="end">{unit}</text>')
    for i in range(5):
        yv = ylo + (yhi - ylo) * i / 4; yy = py(yv)
        s.append(f'<line x1="{ML}" y1="{yy:.1f}" x2="{W-MR}" y2="{yy:.1f}" stroke="{GRID}"/>')
        s.append(f'<text x="{ML-8}" y="{yy+4:.1f}" fill="{AXIS}" font-size="11" '
                 f'text-anchor="end">{yv:.0f}%</text>')
    P = [(px(x), py(y)) for x, y, *_ in pts]
    area = f'M{P[0][0]:.1f},{py(ylo):.1f} ' + " ".join(f'L{x:.1f},{y:.1f}' for x, y in P) \
        + f' L{P[-1][0]:.1f},{py(ylo):.1f} Z'
    s.append(f'<path d="{area}" fill="url(#g{colour[1:]})"/>')
    s.append(f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x,y in P)}" '
             f'fill="none" stroke="{colour}" stroke-width="2.5" stroke-linejoin="round"/>')
    for (x, y), (cx, cy) in zip(pts, P):
        s.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="{colour}"/>')
        s.append(f'<text x="{cx:.1f}" y="{cy-11:.1f}" fill="{colour}" font-size="12.5" '
                 f'font-weight="700" text-anchor="middle">{y:.2f}%</text>')
        s.append(f'<text x="{cx:.1f}" y="{H-12}" fill="{AXIS}" font-size="11" '
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
        s.append(f'<text x="{px(v)+8:.1f}" y="{y+bh/2+5:.1f}" fill="{colour}" font-size="13" '
                 f'font-weight="700">{v:.1f}%</text>')
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
        lambda y: f"+{y:.0f}", y0=0, ymax=260))
    open("docs/speed_progression.svg", "w").write(_chart(
        "Single-thread speed", "x v31", vs, mult, "#58a6ff",
        lambda y: f"{y:.2f}x", y0=0.8, ymax=1.7))
    open("docs/odds_knight.svg", "w").write(_line_points(
        "Knight odds win% vs full-strength SF-18", "external yardstick",
        ODDS_KNIGHT, "#a371f7", 74, 84))
    open("docs/odds_ladder.svg", "w").write(_bars(
        "Odds it can spot full-strength SF-18 and still win", "latest each",
        ODDS_LADDER, "#f0883e"))
    print("wrote 4 SVGs to docs/")
    print(f"  cumulative Elo v31->v53: +{cum[-1]:.0f}   speed: {mult[-1]:.2f}x")
    print(f"  knight odds: {ODDS_KNIGHT[-1][1]}%   ladder: "
          + ", ".join(f"{l} {v}%" for l, v, _ in ODDS_LADDER))


if __name__ == "__main__":
    main()
