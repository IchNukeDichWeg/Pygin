#!/usr/bin/env python3
"""
nps_report_to_md.py
====================
Turn an nps_history_bench.py results JSON file into a clean, readable
Markdown report: source/metadata header + the same full NPS/depth
breakdown tables and per-version summaries the benchmark prints to the
terminal, saved to a file you can open, share, or diff between runs.

Usage:
    python3 nps_report_to_md.py
        Reports on the most recently modified nps_history_results*.json
        in this directory.

    python3 nps_report_to_md.py nps_history_results_123.json
        Reports on a specific results file.

    python3 nps_report_to_md.py nps_history_results_123.json -o report.md
        Custom output path (default: same name as the input file, with a
        .md extension, in the same directory).
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nps_history_bench as bench


def build_markdown(results, source_path):
    versions = bench.versions_in(results)
    position_names = bench.positions_in(results)
    n_errors = sum(1 for c in results.values() if "error" in c)
    cfg = bench.infer_run_config(results)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    src_mtime = (datetime.fromtimestamp(os.path.getmtime(source_path)).strftime("%Y-%m-%d %H:%M")
                 if os.path.exists(source_path) else "unknown")

    lines = ["# NPS / Depth Benchmark Report\n"]
    lines.append(f"- **Source file:** `{os.path.basename(source_path)}` (created {src_mtime})")
    lines.append(f"- **Report generated:** {generated}")
    if versions:
        lines.append(f"- **Versions covered:** v{versions[0]}-v{versions[-1]} "
                      f"({len(versions)} version{'s' if len(versions) != 1 else ''})")
    else:
        lines.append("- **Versions covered:** none")
    # Positions actually found in THIS file, not the live default list in
    # nps_history_bench.py -- those can differ if a position was swapped
    # out after this file was generated.
    lines.append(f"- **Positions:** {len(position_names)} "
                 f"({', '.join(position_names)})")
    if cfg["runs_per_position"] is not None:
        lines.append(f"- **Runs per position:** {cfg['runs_per_position']}")
    # Seconds/run and max_depth are what make a depth number interpretable
    # (depth 20 in 5s vs. depth 20 in 60s is not the same result, and a
    # depth == max_depth means the tree fully solved rather than being
    # time-limited) -- see nps_history_bench.run_config_line's docstring.
    if cfg["seconds_per_run"] is not None:
        lines.append(f"- **Seconds per run:** {cfg['seconds_per_run']}")
    if cfg["max_depth"] is not None:
        lines.append(f"- **Max depth (hard cap):** {cfg['max_depth']}")
    if cfg["seconds_per_run"] is None and cfg["max_depth"] is None:
        lines.append("- **Run config:** not recorded in this file (pre-dates "
                      "config tracking -- don't compare its depth numbers "
                      "across files without checking how it was actually run)")
    lines.append(f"- **Total cells:** {len(results)} "
                 f"({n_errors} error{'s' if n_errors != 1 else ''})")
    lines.append("")
    lines.append(bench.build_tables(results, versions, position_names, fmt="html"))
    lines.append("")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(
        description="Convert an nps_history_bench.py results JSON file into a "
                     "readable Markdown report.")
    p.add_argument("input", nargs="?", default=None,
                    help="Path to a nps_history_results*.json file. "
                         "Default: most recently modified one.")
    p.add_argument("-o", "--output", default=None,
                    help="Output .md path. Default: same name as the input "
                         "file with a .md extension.")
    args = p.parse_args()

    input_path = args.input or bench.latest_results_file()
    if input_path is None or not os.path.exists(input_path):
        print(f"No results file found ({input_path or 'none saved yet'}).")
        sys.exit(1)

    with open(input_path) as f:
        results = json.load(f)

    output_path = args.output or os.path.splitext(input_path)[0] + ".md"

    md = build_markdown(results, input_path)
    with open(output_path, "w") as f:
        f.write(md)

    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
