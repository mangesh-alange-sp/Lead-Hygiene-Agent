"""
Post-run check of a write-back file.

Counts must add up, no two survivors may share an email, leftover
auto-merge pairs must be zero, and every missing source Id must be a
merge or an explicit drop.
"""

import argparse
import csv
import io
from collections import Counter
from pathlib import Path

from .dedupe import normalized_email, score_pair
from .textnorm import cell


def _rows(path: Path) -> list:
    text = path.read_text(encoding="utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _ids(rows) -> list:
    return [cell(row.get("Id", "")) for row in rows if cell(row.get("Id", ""))]


def _duplicate_emails(rows) -> list:
    counts = Counter()
    for row in rows:
        key = normalized_email(row.get("Email", ""))
        if key:
            counts[key] += 1
    return sorted(email for email, count in counts.items() if count > 1)


def _leftover_auto_merges(rows) -> list:
    leftovers = []
    for i, left in enumerate(rows):
        for right in rows[i + 1 :]:
            result = score_pair(left, right)
            if result.get("auto_merge"):
                leftovers.append({
                    "id_a": cell(left.get("Id", "")),
                    "id_b": cell(right.get("Id", "")),
                    "signals": result.get("signals") or [],
                })
    return leftovers


def _explain_missing(missing, log_rows) -> dict:
    absorbed = {}
    drops = {}
    for row in log_rows:
        decision = cell(row.get("decision", ""))
        survivor = cell(row.get("surviving_lead_id", ""))
        for absorbed_id in (cell(row.get("merged_from_ids", "")).split(";")):
            absorbed_id = absorbed_id.strip()
            if absorbed_id:
                absorbed[absorbed_id] = survivor
        if decision in {"dropped_test_data", "dropped_junk_lead"} and survivor:
            drops[survivor] = decision
    explained = {}
    unexplained = []
    for lead_id in missing:
        if lead_id in absorbed:
            explained[lead_id] = f"auto_merge into {absorbed[lead_id]}"
        elif lead_id in drops:
            explained[lead_id] = drops[lead_id]
        else:
            unexplained.append(lead_id)
    return {"explained": explained, "unexplained": unexplained}


def audit_run(output_csv: Path, source_csv: Path = None, log_csv: Path = None) -> dict:
    """Check a finished write-back. Paths are the files on disk after a run."""
    out_rows = _rows(output_csv)
    out_ids = _ids(out_rows)
    report = {
        "leads_out": len(out_rows),
        "leads_in": None,
        "missing_ids": [],
        "missing_explained": {},
        "missing_unexplained": [],
        "duplicate_emails": _duplicate_emails(out_rows),
        "leftover_auto_merges": _leftover_auto_merges(out_rows),
        "ok": True,
        "problems": [],
    }

    if source_csv:
        source_rows = _rows(source_csv)
        source_ids = _ids(source_rows)
        report["leads_in"] = len(source_rows)
        out_set = set(out_ids)
        report["missing_ids"] = [lead_id for lead_id in source_ids if lead_id not in out_set]
        if log_csv:
            explained = _explain_missing(report["missing_ids"], _rows(log_csv))
            report["missing_explained"] = explained["explained"]
            report["missing_unexplained"] = explained["unexplained"]
        else:
            report["missing_unexplained"] = list(report["missing_ids"])

    if report["duplicate_emails"]:
        report["problems"].append(
            f"{len(report['duplicate_emails'])} duplicate email(s) among survivors"
        )
    if report["leftover_auto_merges"]:
        report["problems"].append(
            f"{len(report['leftover_auto_merges'])} leftover auto-merge pair(s)"
        )
    if report["missing_unexplained"]:
        report["problems"].append(
            f"{len(report['missing_unexplained'])} missing Id(s) not in the merge/drop log"
        )
    report["ok"] = not report["problems"]
    return report


def format_audit(report: dict) -> str:
    lines = []
    if report.get("leads_in") is not None:
        lines.append(f"Leads in: {report['leads_in']}")
    lines.append(f"Leads out: {report['leads_out']}")
    missing = report.get("missing_ids") or []
    lines.append(f"Missing Ids: {len(missing)}")
    for lead_id, why in (report.get("missing_explained") or {}).items():
        lines.append(f"  {lead_id}: {why}")
    for lead_id in report.get("missing_unexplained") or []:
        lines.append(f"  {lead_id}: unexplained")
    dupes = report.get("duplicate_emails") or []
    lines.append(f"Duplicate emails: {len(dupes)}")
    for email in dupes:
        lines.append(f"  {email}")
    leftovers = report.get("leftover_auto_merges") or []
    lines.append(f"Leftover auto-merges: {len(leftovers)}")
    for pair in leftovers:
        signals = "+".join(pair.get("signals") or []) or "match"
        lines.append(f"  {pair['id_a']} + {pair['id_b']} ({signals})")
    if report.get("ok"):
        lines.append("Audit: ok")
    else:
        lines.append("Audit: problems")
        for problem in report.get("problems") or []:
            lines.append(f"  {problem}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Check a hygiene write-back: counts, emails, leftover merges, missing Ids.",
    )
    parser.add_argument("output_csv", type=Path, help="deduped.csv from the run")
    parser.add_argument("--source", type=Path, help="original Lead export")
    parser.add_argument("--log", type=Path, help="dedup_log.csv from the run")
    args = parser.parse_args(argv)
    if not args.output_csv.exists():
        print(f"Error: {args.output_csv} not found.")
        return 1
    if args.source and not args.source.exists():
        print(f"Error: {args.source} not found.")
        return 1
    if args.log and not args.log.exists():
        print(f"Error: {args.log} not found.")
        return 1
    report = audit_run(args.output_csv, source_csv=args.source, log_csv=args.log)
    print(format_audit(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
