"""Per-field diff against the last known-good write-back CSV."""

import csv
import io
from pathlib import Path

from .textnorm import cell

GOLDEN_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "golden_deduped.csv"
COMPARE_FIELDS = (
    "FirstName", "LastName", "Email", "Phone", "Company", "Title",
    "Website", "Industry", "Country",
)


def _rows_by_id(csv_text: str) -> dict:
    rows = {}
    for row in csv.DictReader(io.StringIO(csv_text)):
        lead_id = cell(row.get("Id", ""))
        if lead_id:
            rows[lead_id] = row
    return rows


def diff_against_golden(csv_text: str, golden_path: Path = GOLDEN_PATH) -> list:
    """
    Return one dict per cell that changed for an Id present in both files.
    New or dropped Ids are ignored — only overlapping records are gated.
    """
    if not golden_path.exists():
        return []
    current = _rows_by_id(csv_text)
    golden = _rows_by_id(golden_path.read_text(encoding="utf-8"))
    diffs = []
    for lead_id, old in golden.items():
        new = current.get(lead_id)
        if not new:
            continue
        for field in COMPARE_FIELDS:
            if field not in old and field not in new:
                continue
            before, after = cell(old.get(field, "")), cell(new.get(field, ""))
            if before != after:
                diffs.append({
                    "id": lead_id,
                    "field": field,
                    "from": before,
                    "to": after,
                })
    return diffs
