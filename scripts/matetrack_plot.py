#!/usr/bin/env python3
"""scripts/matetrack_plot.py -- mate-finding across releases, as an SVG.

    python3 scripts/matetrack_plot.py matetrack_history_*.tsv
    python3 scripts/matetrack_plot.py run.tsv -o mates.svg

Reads the TSV that scripts/matetrack_history.sh writes and draws found% and
best% against version. No dependencies: matplotlib is not worth an install for
one line chart, and hand-written SVG stays readable in a diff.

Two things the picture must NOT hide, so both are drawn:
  * the ERA BOUNDARIES (v31 the C core, v58 NNUE). A jump at a boundary is an
    architecture change, not a search improvement, and reading it as progress
    is the obvious way to be fooled by this chart.
  * that only v56+ have a verified bench signature. Earlier points come from
    compatibility layers and are drawn hollow -- indicative, not proof.

Mate score is a CORRECTNESS signal, not Elo. It has been positive on changes
that measured negative in games. A reproducible decline deserves weight; a
single good number does not.
"""

import argparse
import csv
import glob
import os
import sys

W, H = 1000, 520
L, R, T, B = 70, 30, 60, 70          # margins


def read_rows(paths):
    rows = []
    for p in paths:
        with open(p) as fh:
            for r in csv.DictReader(fh, delimiter="\t"):
                if not r.get("version", "").startswith("v"):
                    continue
                if r.get("total") in (None, "", "FAILED"):
                    continue
                try:
                    rows.append({
                        "v": int(r["version"][1:]),
                        "found": float(r["found_pct"]),
                        "best": float(r["best_pct"]),
                        "epd": r.get("epd", "?"),
                        "time": r.get("time_ms", "?"),
                    })
                except (ValueError, KeyError):
                    continue
    rows.sort(key=lambda d: d["v"])
    return rows


def build(rows, title):
    vs = [r["v"] for r in rows]
    vmin, vmax = min(vs), max(vs)
    span = max(1, vmax - vmin)

    def x(v):
        return L + (v - vmin) / span * (W - L - R)

    def y(pct):
        return T + (100.0 - pct) / 100.0 * (H - T - B)

    o = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
         'font-family="ui-sans-serif,system-ui,sans-serif">' % (W, H),
         '<style>'
         '.bg{fill:#ffffff}.fg{fill:#1a1a1a}.mut{fill:#6b7280}'
         '.grid{stroke:#e5e7eb;stroke-width:1}'
         '.era{stroke:#9ca3af;stroke-width:1;stroke-dasharray:4 4}'
         '@media (prefers-color-scheme:dark){'
         '.bg{fill:#0f1115}.fg{fill:#e5e7eb}.mut{fill:#9ca3af}'
         '.grid{stroke:#232733}.era{stroke:#4b5563}}'
         '</style>',
         '<rect class="bg" width="%d" height="%d"/>' % (W, H),
         '<text class="fg" x="%d" y="30" font-size="17" font-weight="600">%s</text>'
         % (L, title)]

    for pct in range(0, 101, 20):                       # y grid
        yy = y(pct)
        o.append('<line class="grid" x1="%d" y1="%.1f" x2="%d" y2="%.1f"/>'
                 % (L, yy, W - R, yy))
        o.append('<text class="mut" x="%d" y="%.1f" font-size="11" '
                 'text-anchor="end">%d%%</text>' % (L - 8, yy + 4, pct))

    for ev, lab in ((31, "v31 C core"), (58, "v58 NNUE")):
        if vmin <= ev <= vmax:
            xx = x(ev)
            o.append('<line class="era" x1="%.1f" y1="%d" x2="%.1f" y2="%.1f"/>'
                     % (xx, T, xx, y(0)))
            o.append('<text class="mut" x="%.1f" y="%d" font-size="10" '
                     'text-anchor="middle">%s</text>' % (xx, T - 8, lab))

    step = max(1, round(span / 12))
    for v in range(vmin, vmax + 1, step):               # x ticks
        o.append('<text class="mut" x="%.1f" y="%.1f" font-size="11" '
                 'text-anchor="middle">v%d</text>' % (x(v), y(0) + 18, v))

    for key, colour, label in (("found", "#2563eb", "found mates"),
                               ("best", "#dc2626", "best mates")):
        pts = " ".join("%.1f,%.1f" % (x(r["v"]), y(r[key])) for r in rows)
        o.append('<polyline fill="none" stroke="%s" stroke-width="2" '
                 'stroke-linejoin="round" points="%s"/>' % (colour, pts))
        for r in rows:
            verified = r["v"] >= 56          # only these have a bench signature
            o.append('<circle cx="%.1f" cy="%.1f" r="3" fill="%s" '
                     'fill-opacity="%s" stroke="%s" stroke-width="1.5"/>'
                     % (x(r["v"]), y(r[key]), colour,
                        "1" if verified else "0", colour))

    ly = H - 26
    for i, (colour, label) in enumerate((("#2563eb", "found mates"),
                                         ("#dc2626", "best mates"))):
        lx = L + i * 150
        o.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" '
                 'stroke-width="2"/>' % (lx, ly, lx + 22, ly, colour))
        o.append('<text class="fg" x="%d" y="%d" font-size="12">%s</text>'
                 % (lx + 28, ly + 4, label))
    o.append('<text class="mut" x="%d" y="%d" font-size="11" text-anchor="end">'
             'hollow = no verified bench signature (pre-v56)</text>'
             % (W - R, ly + 4))
    o.append("</svg>")
    return "\n".join(o)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tsv", nargs="+")
    ap.add_argument("-o", "--out", default=None)
    a = ap.parse_args()
    paths = []
    for pat in a.tsv:
        paths.extend(sorted(glob.glob(pat)) or [pat])
    rows = read_rows(paths)
    if not rows:
        sys.exit("no usable rows -- did the run produce FAILED everywhere?")
    epd = rows[0]["epd"]
    t = rows[0]["time"]
    title = f"Pygin mate-finding across releases  ({epd} @ {t}s, {len(rows)} versions)"
    out = a.out or os.path.splitext(paths[0])[0] + ".svg"
    open(out, "w").write(build(rows, title))
    lo = min(rows, key=lambda r: r["found"])
    hi = max(rows, key=lambda r: r["found"])
    print(f"wrote {out}  ({len(rows)} versions)")
    print(f"  best  found%: v{hi['v']} at {hi['found']:.1f}%")
    print(f"  worst found%: v{lo['v']} at {lo['found']:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
