"""
summary.py
Deterministic changelog for the CLI and the agent. No lead values are invented here.
"""

import math

from .domains import company_match_key, host_of
from .textnorm import cell, digits_only, fold_text

MAX_FIELD_EXAMPLES = 12
MAX_CHANGE_ROWS = 400


def person_name(row) -> str:
    if not row:
        return ""
    return " ".join(
        part for part in (cell(row.get("FirstName", "")), cell(row.get("LastName", ""))) if part
    )


def as_text(value) -> str:
    """Compare and display cells as strings. None and NaN become empty."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def flag_counts(audit_rows: list) -> dict:
    counts = {}
    for row in audit_rows:
        for flag in (row.get("data_quality_flags") or "").split("|"):
            if flag:
                counts[flag] = counts.get(flag, 0) + 1
    return counts


FIELD_HEADINGS = {
    "Phone": "Phone numbers",
    "MobilePhone": "Mobile numbers",
    "Email": "Email addresses",
    "FirstName": "First names",
    "LastName": "Last names",
    "Company": "Company names",
    "Title": "Job titles",
    "Industry": "Industries",
    "Website": "Websites",
    "Country": "Countries",
    "State": "States",
    "City": "Cities",
    "Street": "Street addresses",
    "LeadSource": "Lead sources",
    "Status": "Statuses",
}

CLEAR_REASON = {
    "Email": "it was a placeholder or invalid address",
    "Phone": "it was a dummy or unusable number",
    "MobilePhone": "it was a dummy or unusable number",
    "Website": "it did not match the company, or was a personal/placeholder site",
    "FirstName": "it was not a real name",
    "LastName": "it was not a real name",
    "Company": "it was a placeholder company name",
}


def fold_without_spaces(value: str) -> str:
    """Same person: 'J. Doe' and 'J Doe' compare equal after folding."""
    return fold_text(value).replace(" ", "")


def is_critical_change(field: str, old: str, new: str) -> bool:
    """True when a value was removed or materially replaced, not just reformatted."""
    if old and not new:
        return True
    if not old or old == new:
        return False
    if field in {"Phone", "MobilePhone"}:
        return digits_only(old) != digits_only(new)
    if field == "Website":
        return host_of(old) != host_of(new)
    if field == "Email":
        return old.lower() != new.lower()
    if field == "Company":
        return company_match_key(old) != company_match_key(new)
    if field in {"FirstName", "LastName"}:
        return fold_without_spaces(old) != fold_without_spaces(new)
    return False


def plain_change(field: str, old: str, new: str) -> str:
    before = old if old else "(blank)"
    if old and not new:
        return f"{before} was cleared because {CLEAR_REASON.get(field, 'it was invalid or a placeholder')}"
    if field == "Website":
        return f"Website host {before} was replaced with {new}"
    if field == "Email":
        return f"Email {before} was replaced with {new}"
    if field in {"Phone", "MobilePhone"}:
        return f"Phone {before} was replaced with {new}"
    if field == "Company":
        return f"Company {before} was replaced with {new}"
    if field in {"FirstName", "LastName"}:
        return f"{before} was replaced with {new}"
    return f"{before} was replaced with {new}"


def counted(n: int, singular: str, plural: str = "") -> str:
    word = singular if n == 1 else (plural or singular + "s")
    return f"{n} {word}"


def field_what(field: str, entries: list) -> str:
    cleared = sum(1 for item in entries if item["from"] and not item["to"])
    replaced = len(entries) - cleared
    bits = []
    if cleared:
        bits.append(
            f"{counted(cleared, 'value')} removed because "
            f"{CLEAR_REASON.get(field, 'it was invalid or a placeholder')}"
        )
    if replaced:
        bits.append(f"{counted(replaced, 'value')} replaced with a different value")
    if not bits:
        return f"{FIELD_HEADINGS.get(field, field)} needed a real data fix."
    text = "; ".join(bits) + "."
    return text[:1].upper() + text[1:]


def pick_plain_examples(field: str, entries: list, limit: int = 3) -> list:
    cleared = [item for item in entries if item["from"] and not item["to"]]
    replaced = [item for item in entries if item["to"]]
    picked = []
    for pool in (cleared, replaced):
        for item in pool:
            if len(picked) >= limit:
                break
            picked.append(plain_change(field, item["from"], item["to"]))
    return picked


def critical_entries(field_changes: dict) -> dict:
    critical = {}
    for field, bucket in field_changes.items():
        kept = [
            entry for entry in (bucket.get("entries") or [])
            if is_critical_change(field, entry["from"], entry["to"])
        ]
        if kept:
            critical[field] = {"count": len(kept), "entries": kept, "examples": kept[:MAX_FIELD_EXAMPLES]}
    return critical


def normalization_sections(field_changes: dict) -> list:
    """Critical-only sections. Casing, https, and taxonomy expansions are omitted."""
    critical = critical_entries(field_changes)
    preferred = (
        "Email", "Phone", "MobilePhone", "Website", "FirstName", "LastName", "Company",
    )
    fields = [field for field in preferred if field in critical]
    fields += [field for field in critical if field not in fields]
    sections = []
    for field in fields:
        bucket = critical[field]
        sections.append({
            "field": field,
            "heading": FIELD_HEADINGS.get(field, field),
            "count": bucket["count"],
            "what": field_what(field, bucket["entries"]),
            "examples": pick_plain_examples(field, bucket["entries"]),
        })
    return sections


def routine_cleanup_line(field_changes: dict) -> str:
    parts = []
    for field, bucket in field_changes.items():
        routine = sum(
            1 for entry in (bucket.get("entries") or [])
            if not is_critical_change(field, entry["from"], entry["to"])
        )
        if routine:
            parts.append(f"{routine} {FIELD_HEADINGS.get(field, field).lower()}")
    if not parts:
        return ""
    return (
        "Routine cleanup (casing, phone formatting, https, and taxonomy) also ran on "
        + ", ".join(parts)
        + ". Those edits are in deduped.csv and are omitted here."
    )


def critical_lines(sections: list, dropped: list, routine: str) -> list:
    lines = []
    for section in sections:
        lines.append(f"{section['heading']} ({section['count']}): {section['what']}")
        lines.extend(f"- {example}" for example in section.get("examples", []))
    if dropped:
        names = ", ".join(
            f"{row['id']} {row['name']}".strip() for row in dropped[:8]
        )
        extra = f" (+{len(dropped) - 8} more)" if len(dropped) > 8 else ""
        lines.append(f"Dropped {len(dropped)} test row(s): {names}{extra}.")
    if routine:
        lines.append(routine)
    return lines


def build_run_summary(source_df, writable, merge_log, audit_rows, counts) -> dict:
    """Deterministic changelog the agent narrates. Never invented by the model."""
    source_by_id = {cell(row.get("Id", "")): row for row in source_df.to_dict("records")}
    out_by_id = {cell(row.get("Id", "")): row for row in writable.to_dict("records")}
    compare_cols = [col for col in writable.columns if col != "Id"]

    field_changes = {}
    change_list = []
    for lead_id, out_row in out_by_id.items():
        src = source_by_id.get(lead_id)
        if not src:
            continue
        for col in compare_cols:
            old, new = as_text(src.get(col)), as_text(out_row.get(col))
            if old == new:
                continue
            bucket = field_changes.setdefault(col, {"count": 0, "examples": [], "entries": []})
            bucket["count"] += 1
            entry = {"id": lead_id, "field": col, "from": old, "to": new}
            bucket["entries"].append(entry)
            if len(bucket["examples"]) < MAX_FIELD_EXAMPLES:
                bucket["examples"].append(entry)
            if len(change_list) < MAX_CHANGE_ROWS:
                change_list.append(entry)

    merges = []
    for entry in merge_log:
        if entry.get("decision") != "auto_merge":
            continue
        absorbed = [part for part in (entry.get("merged_from_ids") or "").split(";") if part]
        if not absorbed:
            continue
        survivor = entry.get("surviving_lead_id", "")
        src = source_by_id.get(survivor, {})
        out = out_by_id.get(survivor, {})
        survivor_name = person_name(out) or person_name(src)
        absorbed_people = []
        for absorbed_id in absorbed:
            person = source_by_id.get(absorbed_id, {})
            absorbed_people.append({
                "id": absorbed_id,
                "name": person_name(person),
                "company": cell(person.get("Company", "")),
            })
        signals = [part for part in (entry.get("match_signals_used") or "").split("|") if part]
        confidence = entry.get("confidence_score", "")
        absorbed_label = ", ".join(
            f"{person['id']}" + (f" ({person['name']})" if person["name"] else "")
            for person in absorbed_people
        ) or ", ".join(absorbed)
        survivor_label = survivor + (f" ({survivor_name})" if survivor_name else "")
        merges.append({
            "survivor_id": survivor,
            "survivor_name": survivor_name,
            "merged_from_ids": absorbed,
            "merged_from": absorbed_people,
            "signals": signals,
            "confidence": confidence,
            "line": (
                f"{survivor_label} absorbed {absorbed_label}"
                f" | signals: {'+'.join(signals) or 'match'}"
                f" | confidence: {confidence or 'n/a'}"
            ),
        })

    dropped, hitl = [], []
    for row in audit_rows:
        lead_id = row.get("surviving_lead_id", "")
        src = source_by_id.get(lead_id, {})
        name = " ".join(
            part for part in (cell(src.get("FirstName", "")), cell(src.get("LastName", ""))) if part
        )
        flags = [part for part in (row.get("data_quality_flags") or "").split("|") if part]
        item = {"id": lead_id, "name": name, "company": cell(src.get("Company", "")), "flags": flags}
        if row.get("decision") in {"dropped_test_data", "dropped_junk_lead"}:
            dropped.append(item)
        elif row.get("decision") == "hitl_review" or "hitl_review" in flags:
            if lead_id in out_by_id:
                hitl.append(item)

    directory = []
    for lead_id, row in out_by_id.items():
        directory.append({
            "id": lead_id,
            "name": " ".join(
                part for part in (cell(row.get("FirstName", "")), cell(row.get("LastName", ""))) if part
            ),
            "company": cell(row.get("Company", "")),
            "email": cell(row.get("Email", "")),
            "phone": cell(row.get("Phone", "")),
        })
    for item in dropped:
        directory.append({
            "id": item["id"],
            "name": item["name"],
            "company": item["company"],
            "email": cell(source_by_id.get(item["id"], {}).get("Email", "")),
            "phone": cell(source_by_id.get(item["id"], {}).get("Phone", "")),
            "dropped": True,
        })

    omitted = max(0, sum(b["count"] for b in field_changes.values()) - len(change_list))
    sections = normalization_sections(field_changes)
    routine = routine_cleanup_line(field_changes)
    return {
        "totals": {
            "leads_in": counts["leads_in"],
            "leads_out": counts["leads_out"],
            "validation_issues": counts["validation_issues"],
            "values_normalized": counts["values_normalized"],
            "duplicates_merged": counts["duplicates_merged"],
            "records_dropped": counts["records_dropped"],
            "hitl_records": counts["hitl_records"],
            "blank_email_kept": counts["blank_email_kept"],
        },
        "files": [
            "deduped.csv — Salesforce write-back (same columns as the upload)",
            "dedup_log.csv — merges, dropped test rows, and review flags",
            "deduped_excel_review.csv — same rows, phones readable in Excel "
            "(review only, do not import)",
        ],
        "merges": merges,
        "merge_lines": [merge["line"] for merge in merges],
        "dropped": dropped,
        "hitl": hitl,
        "field_changes": field_changes,
        "normalization_sections": sections,
        "routine_cleanup": routine,
        "critical_lines": critical_lines(sections, dropped, routine),
        "change_list": change_list,
        "changes_omitted": omitted,
        "flag_counts": flag_counts(audit_rows),
        "directory": directory,
        "technical_log": counts.get("technical_log") or [],
        "phone_states": counts.get("phone_states") or {},
        "field_diffs": counts.get("field_diffs") or [],
        "field_diff_lines": counts.get("field_diff_lines") or [],
    }


def format_run_summary(summary: dict) -> str:
    """Plain-text summary for the CLI. The agent writes its own prose from the dict."""
    totals = summary.get("totals", {})
    lines = [
        "deduped.csv is ready.",
        f"Leads in: {totals.get('leads_in', 0)}",
        f"Validation issues: {totals.get('validation_issues', 0)}",
        f"Values normalized: {totals.get('values_normalized', 0)}",
        f"Duplicates merged: {totals.get('duplicates_merged', 0)}",
        f"Test rows dropped: {totals.get('records_dropped', 0)}",
        f"Leads to write back: {totals.get('leads_out', 0)}",
    ]
    if totals.get("blank_email_kept"):
        lines.append(f"Leads with no email (kept, not merged): {totals['blank_email_kept']}")
    if totals.get("hitl_records"):
        lines.append(f"HITL review: {totals['hitl_records']}")
    lines.append("dedup_log.csv is ready.")
    states = summary.get("phone_states") or {}
    if states:
        lines.append(
            "Phones: "
            + ", ".join(f"{count} {state}" for state, count in sorted(states.items()))
        )
        lines.append(
            "  deduped.csv holds bare E.164 (+12065550100) for Salesforce. Excel "
            "hides the leading + on that form, so use deduped_excel_review.csv to "
            "eyeball phones."
        )
    if summary.get("merges"):
        lines.append("Merges:")
        for line in summary.get("merge_lines") or [merge.get("line", "") for merge in summary["merges"]]:
            if line:
                lines.append(f"  {line}")
    if summary.get("dropped"):
        lines.append("Dropped test rows:")
        for row in summary["dropped"]:
            lines.append(f"  {row['id']} {row['name']}".rstrip())
    if summary.get("critical_lines"):
        lines.append("Critical changes:")
        for line in summary["critical_lines"]:
            lines.append(f"  {line}")
    elif summary.get("normalization_sections"):
        lines.append("Critical changes:")
        for section in summary["normalization_sections"]:
            lines.append(f"  {section['heading']} ({section['count']} leads): {section['what']}")
            for example in section.get("examples", []):
                lines.append(f"    - {example}")
    elif summary.get("field_changes"):
        lines.append("Fields changed:")
        for field, bucket in summary["field_changes"].items():
            lines.append(f"  {field}: {bucket['count']}")
    return "\n".join(lines)


def agent_facing_summary(summary: dict) -> dict:
    """
    Slim payload for the model. merge_lines is first so it cannot be truncated
    behind the per-lead directory. The full summary stays in session state.
    """
    return {
        "totals": summary.get("totals", {}),
        "files": summary.get("files", []),
        "merge_lines": summary.get("merge_lines", []),
        "merges": summary.get("merges", []),
        "dropped": summary.get("dropped", []),
        "critical_lines": summary.get("critical_lines", []),
        "routine_cleanup": summary.get("routine_cleanup", ""),
        "flag_counts": summary.get("flag_counts", {}),
        "technical_log": summary.get("technical_log", []),
        "field_diffs": summary.get("field_diffs", []),
        "field_diff_lines": summary.get("field_diff_lines", []),
        "instruction": (
            "What changed must be copied from critical_lines only. "
            "Do not list company casing, industry maps, title expansions, "
            "https prefixes, or phone regrouping. Do not write "
            "Company: N changes, e.g. Copy every merge_lines entry."
        ),
    }
