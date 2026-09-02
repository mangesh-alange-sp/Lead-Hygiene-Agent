"""
shadow_diff.py
Run two pipeline versions over the same batch and diff every column of every row.

Any change in a column other than the one being fixed is an unexplained side
effect and must be reviewed before the change ships.

    # capture a baseline before editing the pipeline
    python -m pipeline.shadow_diff capture leads.csv --out baseline.csv

    # after editing, compare and scope the change to one field
    python -m pipeline.shadow_diff compare baseline.csv leads.csv --expect Phone
"""

import argparse
import sys
from pathlib import Path

import pandas as pd


def read(path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")


def index(df: pd.DataFrame, key: str) -> dict:
    return {str(row.get(key, "")): row for row in df.to_dict("records")}


def diff_frames(old: pd.DataFrame, new: pd.DataFrame, key: str = "Id") -> dict:
    """Cell-level diff keyed by lead Id, plus row-level appearances/disappearances."""
    old_rows, new_rows = index(old, key), index(new, key)
    columns = [col for col in old.columns if col in new.columns]
    changes = []
    for lead_id, old_row in old_rows.items():
        new_row = new_rows.get(lead_id)
        if new_row is None:
            continue
        for col in columns:
            before, after = str(old_row.get(col, "")), str(new_row.get(col, ""))
            if before != after:
                changes.append({"Id": lead_id, "column": col, "before": before, "after": after})
    return {
        "changes": changes,
        "columns_changed": sorted({change["column"] for change in changes}),
        "only_in_old": sorted(set(old_rows) - set(new_rows)),
        "only_in_new": sorted(set(new_rows) - set(old_rows)),
        "dropped_columns": [col for col in old.columns if col not in new.columns],
        "added_columns": [col for col in new.columns if col not in old.columns],
        "rows_old": len(old),
        "rows_new": len(new),
    }


def unexpected_changes(report: dict, expected_columns) -> list:
    expected = set(expected_columns or ())
    return [change for change in report["changes"] if change["column"] not in expected]


def format_report(report: dict, expected_columns) -> str:
    lines = [
        f"rows: {report['rows_old']} -> {report['rows_new']}",
        f"columns changed: {', '.join(report['columns_changed']) or 'none'}",
    ]
    if report["added_columns"]:
        lines.append(f"ADDED COLUMNS (schema drift): {', '.join(report['added_columns'])}")
    if report["dropped_columns"]:
        lines.append(f"DROPPED COLUMNS (schema drift): {', '.join(report['dropped_columns'])}")
    if report["only_in_old"]:
        lines.append(f"rows no longer present: {', '.join(report['only_in_old'][:10])}")
    if report["only_in_new"]:
        lines.append(f"rows newly present: {', '.join(report['only_in_new'][:10])}")

    unexpected = unexpected_changes(report, expected_columns)
    lines.append(f"unexpected cell changes: {len(unexpected)}")
    for change in unexpected[:40]:
        lines.append(
            f"  {change['Id']} {change['column']}: {change['before']!r} -> {change['after']!r}"
        )
    if len(unexpected) > 40:
        lines.append(f"  ... {len(unexpected) - 40} more")
    return "\n".join(lines)


def run_pipeline(input_csv: Path) -> pd.DataFrame:
    from .tools import process_csv

    result = process_csv(input_csv.read_text(encoding="utf-8"))
    if result["status"] != "ok":
        raise SystemExit(f"pipeline failed: {result.get('message')}")
    import io

    return read(io.StringIO(result["csv"]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Shadow-diff two pipeline versions.")
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture", help="Run the current pipeline and save its output.")
    capture.add_argument("input_csv", type=Path)
    capture.add_argument("--out", type=Path, required=True)

    compare = sub.add_parser("compare", help="Diff a saved baseline against the current pipeline.")
    compare.add_argument("baseline_csv", type=Path)
    compare.add_argument("input_csv", type=Path)
    compare.add_argument(
        "--expect", action="append", default=[],
        help="Column the change is allowed to touch. Repeatable.",
    )
    compare.add_argument("--key", default="Id")

    args = parser.parse_args(argv)

    if args.command == "capture":
        run_pipeline(args.input_csv).to_csv(args.out, index=False, lineterminator="\n")
        print(f"baseline written to {args.out}")
        return 0

    report = diff_frames(read(args.baseline_csv), run_pipeline(args.input_csv), key=args.key)
    print(format_report(report, args.expect))
    unexpected = unexpected_changes(report, args.expect)
    schema_drift = report["added_columns"] or report["dropped_columns"]
    return 1 if (unexpected or schema_drift) else 0


if __name__ == "__main__":
    sys.exit(main())
