"""
regression.py
Field-by-field diff of a new run against the last known-good output.

Only Ids present in BOTH files are compared: new leads and intentional drops
are not regressions. Any surviving field that changed value is returned for
explicit review, and the pipeline treats a non-empty result as a hard gate.
"""

import csv
import io
from pathlib import Path
from typing import NamedTuple

from .textnorm import cell

GOLDEN_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "golden_deduped.csv"


class RegressionDetected(Exception):
    """Raised when a surviving Id's field value moved against the baseline."""

    def __init__(self, changes):
        self.changes = list(changes)
        super().__init__(
            f"{len(self.changes)} field change(s) against the last known-good "
            "output: " + "; ".join(format_changes(self.changes))
        )


class FieldChange(NamedTuple):
    """A single (id, field, old, new) regression candidate."""

    id: str
    field: str
    old: str
    new: str


def _rows_by_id(csv_text: str) -> dict:
    rows = {}
    for row in csv.DictReader(io.StringIO(csv_text)):
        lead_id = cell(row.get("Id", ""))
        if lead_id:
            rows[lead_id] = row
    return rows


def diff_records(baseline_rows: dict, current_rows: dict) -> list:
    """Compare two {id: row} maps and return FieldChange tuples."""
    changes = []
    for lead_id, old_row in baseline_rows.items():
        new_row = current_rows.get(lead_id)
        if new_row is None:
            continue
        for field in old_row:
            if field == "Id" or field not in new_row:
                continue
            before, after = cell(old_row.get(field)), cell(new_row.get(field))
            if before != after:
                changes.append(FieldChange(lead_id, field, before, after))
    return changes


def diff_against_golden(csv_text: str, golden_path: Path = GOLDEN_PATH) -> list:
    """
    Diff a finished CSV against the committed baseline.

    Returns a list of (id, field, old, new) tuples. An empty list means every
    Id that already existed came out byte-identical.
    """
    path = Path(golden_path)
    if not path.exists():
        return []
    return diff_records(
        _rows_by_id(path.read_text(encoding="utf-8")), _rows_by_id(csv_text)
    )


def assert_no_regression(csv_text: str, golden_path: Path = GOLDEN_PATH) -> None:
    """
    Hard gate. Raises RegressionDetected when any Id that exists in both the
    baseline and this run changed a field value.

    This is the automated replacement for diffing every upload by hand: an
    intentional change is accepted by regenerating the baseline, never by
    skipping the check.
    """
    changes = diff_against_golden(csv_text, golden_path=golden_path)
    if changes:
        raise RegressionDetected(changes)


def as_dicts(changes) -> list:
    """JSON-friendly view for the agent payload / technical log."""
    return [
        {"id": change.id, "field": change.field, "from": change.old, "to": change.new}
        for change in changes
    ]


def format_changes(changes, limit: int = 20) -> list:
    lines = [
        f"{change.id} {change.field}: {change.old or '(blank)'} -> {change.new or '(blank)'}"
        for change in changes[:limit]
    ]
    if len(changes) > limit:
        lines.append(f"(+{len(changes) - limit} more)")
    return lines
