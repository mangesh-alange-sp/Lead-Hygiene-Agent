"""
tools.py
ADK tool wrappers: hygiene pipeline and Lusha search-and-enrich.

Pipeline flow: parse → validate → normalize → dedupe → invariants → artifact.
Output CSV keeps the source columns only (Salesforce write-back shape).
"""

import csv
import io

import pandas as pd
from google.adk.tools import ToolContext
from google.genai import types

from .dedupe import deduplicate_dataframe
from .enrich import enrich_dataframe
from .invariants import (
    check_enrich_preservation,
    check_exported_schema,
    check_records,
    phone_lost_country_code,
    row_level_violations,
    snapshot_email_phone,
)

from .normalize import normalize_dataframe
from .domains import company_match_key, host_of
from .phone import STATUS_NEEDS_REVIEW, excel_safe_international
from .regression import as_dicts, diff_against_golden, format_changes
from .schema import check_output_schema
from .textnorm import cell, digits_only, fold_text, strip_excel_artifacts
from .validate import is_junk_lead_values, validate_dataframe
from .website import normalize_website, resolve_website

MAX_ROWS = 5000
MAX_CSV_CHARS = 2_000_000
CHUNK_SIZE = 200
MAX_FIELD_EXAMPLES = 12
MAX_CHANGE_ROWS = 400
INTERNAL_COLS = (
    "data_quality_flags", "hitl_review", "phone_status", "phone_reason",
    "Phone_raw", "PhoneExtension", "Email_raw", "change_reasons",
    "completeness_flag", "email_status",
)
PHONE_WRITE_COLS = ("Phone", "MobilePhone")
ENRICH_REQUIRES_DEDUP_MESSAGE = (
    "Cannot enrich: run_dedup_pipeline has not produced deduped.csv in this "
    "session. Run dedup first."
)
REVIEW_FILE = "needs_review.csv"
ENRICH_REVIEW_FILE = "enrichment_review.csv"
# Written by this module. Never mistaken for the user's upload.
GENERATED_ARTIFACTS = (
    "deduped.csv",
    "dedup_log.csv",
    "deduped_excel_review.csv",
    "enriched.csv",
    REVIEW_FILE,
)
NO_UPLOAD_MESSAGE = (
    "No lead CSV found for this session. Attach the CSV file to the chat, then "
    "ask again. Do not paste the rows into the tool call."
)
AUDIT_FIELDS = (
    "surviving_lead_id",
    "merged_from_ids",
    "match_signals_used",
    "match_reason",
    "confidence_score",
    "decision",
    "data_quality_flags",
    # Phone resolution is visible per record: valid vs needs_review, plus why.
    "phone_status",
    "phone_reason",
    "phone_raw",
    "phone_extension",
    "email_status",
    "email_raw",
)


def _export_header(name) -> str:
    """Keep '_' as a bare header so the writer does not emit '\"_\"\"'."""
    text = _clean_column_name(name)
    if text.replace("_", "") == "" and text:
        return "_"
    return text


def _write_salesforce_csv(df: pd.DataFrame, columns) -> str:
    headers = [_export_header(col) for col in columns]
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(headers)
    for _, row in df.iterrows():
        writer.writerow([
            "" if pd.isna(row.get(col, "")) else str(row.get(col, ""))
            for col in columns
        ])
    return output.getvalue().lstrip("\ufeff")


def _excel_text_guard(value: str) -> str:
    """
    Wrap a value so Excel and Sheets render it literally.

    A CSV field starting with +, -, = or @ is parsed as a formula, so a correct
    E.164 number like +12065550100 is evaluated and displayed as 12065550100 —
    the plus is in the file but never on screen. The ="..." form is the one
    spelling both applications render verbatim.

    Review copies only. The Salesforce write-back must stay unwrapped.
    """
    text = cell(value)
    if text[:1] in {"+", "-", "=", "@"}:
        return '="' + text.replace('"', '""') + '"'
    return text


def _write_excel_safe_csv(df: pd.DataFrame, columns) -> str:
    """
    The finished frame with phone columns guarded for spreadsheet viewing.

    Carries a UTF-8 BOM. Excel assumes a legacy codepage for a BOM-less file and
    mojibakes accented names (Büchert, König), so the byte order mark is what
    makes this copy readable. The write-back stays BOM-free for Salesforce.
    """
    guarded = df.copy()
    for col in PHONE_WRITE_COLS:
        if col in guarded.columns:
            guarded[col] = guarded[col].map(_excel_text_guard)
    return "\ufeff" + _write_salesforce_csv(guarded, columns)


def _clean_column_name(name) -> str:
    """Strip BOM/wrapping quotes only; do not rename source headings."""
    text = str(name).replace("\ufeff", "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    return text


def _clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    renamed = {col: _clean_column_name(col) for col in df.columns if _clean_column_name(col) != col}
    return df.rename(columns=renamed) if renamed else df


def _build_audit_log(df: pd.DataFrame, merge_log: list) -> str:
    """Side file only — never merged into the Salesforce-shaped CSV."""
    by_id = {row["surviving_lead_id"]: dict(row) for row in merge_log}
    rows = []
    for _, record in df.iterrows():
        lead_id = str(record.get("Id", "") or "")
        flags = str(record.get("data_quality_flags", "") or "")
        phone_status = cell(record.get("phone_status", ""))
        email_status = cell(record.get("email_status", ""))
        entry = by_id.pop(lead_id, None)
        # A resolved phone still gets a line, so "validated, done" is visible
        # in the data rather than inferred from the value's shape. The same
        # for email: MX/status must be readable after the write-back is stripped.
        if not entry and not flags and not phone_status and not email_status:
            continue
        entry = entry or {}
        rows.append({
            "surviving_lead_id": lead_id,
            "merged_from_ids": entry.get("merged_from_ids", ""),
            "match_signals_used": entry.get("match_signals_used", ""),
            "match_reason": entry.get("match_reason", ""),
            "confidence_score": entry.get("confidence_score", ""),
            "decision": entry.get("decision") or _audit_decision(flags),
            "data_quality_flags": flags,
            "phone_status": phone_status,
            "phone_reason": cell(record.get("phone_reason", "")),
            "phone_raw": cell(record.get("Phone_raw", "")),
            "phone_extension": cell(record.get("PhoneExtension", "")),
            "email_status": email_status,
            "email_raw": cell(record.get("Email_raw", "")),
        })
    # Ids that were merged away still deserve a log line.
    for lead_id, entry in by_id.items():
        rows.append({
            "surviving_lead_id": lead_id,
            "merged_from_ids": entry.get("merged_from_ids", ""),
            "match_signals_used": entry.get("match_signals_used", ""),
            "match_reason": entry.get("match_reason", ""),
            "confidence_score": entry.get("confidence_score", ""),
            "decision": entry.get("decision", ""),
            "data_quality_flags": "",
            "phone_status": "",
            "phone_reason": "",
            "phone_raw": "",
            "phone_extension": "",
            "email_status": "",
            "email_raw": "",
        })

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(AUDIT_FIELDS), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _audit_decision(flags: str) -> str:
    parts = [part for part in (flags or "").split("|") if part]
    if "test_data" in parts:
        return "dropped_test_data"
    if "junk_lead" in parts:
        return "dropped_junk_lead"
    if "missing_hard_required" in parts:
        return "hitl_review"
    if any(flag in parts for flag in (
        "missing_email", "missing_phone", "unformatted_phone", "low_quality_title",
        "test_data", "junk_lead", "website_is_email", "needs_country_code_review",
        "undeliverable_email", "email_deliverability_unknown",
    )):
        return "hitl_review"
    return ""


def _person_name(row) -> str:
    if not row:
        return ""
    return " ".join(
        part for part in (cell(row.get("FirstName", "")), cell(row.get("LastName", ""))) if part
    )


def _raw(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value != value:
        return ""
    return str(value).strip()


def _flag_counts(audit_rows: list) -> dict:
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


def _compact(value: str) -> str:
    return fold_text(value).replace(" ", "")


def _is_critical_change(field: str, old: str, new: str) -> bool:
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
        return _compact(old) != _compact(new)
    return False


def _plain_change(field: str, old: str, new: str) -> str:
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


def _qty(n: int, singular: str, plural: str = "") -> str:
    word = singular if n == 1 else (plural or singular + "s")
    return f"{n} {word}"


def _field_what(field: str, entries: list) -> str:
    cleared = sum(1 for item in entries if item["from"] and not item["to"])
    replaced = len(entries) - cleared
    bits = []
    if cleared:
        bits.append(
            f"{_qty(cleared, 'value')} removed because "
            f"{CLEAR_REASON.get(field, 'it was invalid or a placeholder')}"
        )
    if replaced:
        bits.append(f"{_qty(replaced, 'value')} replaced with a different value")
    if not bits:
        return f"{FIELD_HEADINGS.get(field, field)} needed a real data fix."
    text = "; ".join(bits) + "."
    return text[:1].upper() + text[1:]


def _pick_plain_examples(field: str, entries: list, limit: int = 3) -> list:
    cleared = [item for item in entries if item["from"] and not item["to"]]
    replaced = [item for item in entries if item["to"]]
    picked = []
    for pool in (cleared, replaced):
        for item in pool:
            if len(picked) >= limit:
                break
            picked.append(_plain_change(field, item["from"], item["to"]))
    return picked


def _critical_entries(field_changes: dict) -> dict:
    critical = {}
    for field, bucket in field_changes.items():
        kept = [
            entry for entry in (bucket.get("entries") or [])
            if _is_critical_change(field, entry["from"], entry["to"])
        ]
        if kept:
            critical[field] = {"count": len(kept), "entries": kept, "examples": kept[:MAX_FIELD_EXAMPLES]}
    return critical


def _normalization_sections(field_changes: dict) -> list:
    """Critical-only sections. Casing, https, and taxonomy expansions are omitted."""
    critical = _critical_entries(field_changes)
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
            "what": _field_what(field, bucket["entries"]),
            "examples": _pick_plain_examples(field, bucket["entries"]),
        })
    return sections


def _routine_cleanup_line(field_changes: dict) -> str:
    parts = []
    for field, bucket in field_changes.items():
        routine = sum(
            1 for entry in (bucket.get("entries") or [])
            if not _is_critical_change(field, entry["from"], entry["to"])
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


def _critical_lines(sections: list, dropped: list, routine: str) -> list:
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


def _build_run_summary(source_df, writable, merge_log, audit_rows, counts) -> dict:
    """Deterministic changelog the agent narrates. Never invented by the model."""
    quarantine = counts.get("quarantine") or {}
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
            old, new = _raw(src.get(col)), _raw(out_row.get(col))
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
        survivor_name = _person_name(out) or _person_name(src)
        absorbed_people = []
        for absorbed_id in absorbed:
            person = source_by_id.get(absorbed_id, {})
            absorbed_people.append({
                "id": absorbed_id,
                "name": _person_name(person),
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
    sections = _normalization_sections(field_changes)
    routine = _routine_cleanup_line(field_changes)
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
            "rows_held_for_review": len(quarantine.get("ids") or []),
        },
        "files": [
            "deduped.csv — Salesforce write-back (same columns as the upload)",
            "dedup_log.csv — merges, dropped test rows, and review flags",
            "deduped_excel_review.csv — same rows, phones readable in Excel "
            "(review only, do not import)",
        ] + ([
            f"{REVIEW_FILE} — {len(quarantine['ids'])} row(s) held back from the "
            "write-back with the reason attached"
        ] if quarantine.get("ids") else []),
        "merges": merges,
        "merge_lines": [merge["line"] for merge in merges],
        "dropped": dropped,
        "hitl": hitl,
        "field_changes": field_changes,
        "normalization_sections": sections,
        "routine_cleanup": routine,
        "critical_lines": _critical_lines(sections, dropped, routine),
        "change_list": change_list,
        "changes_omitted": omitted,
        "flag_counts": _flag_counts(audit_rows),
        "directory": directory,
        "technical_log": counts.get("technical_log") or [],
        "phone_states": counts.get("phone_states") or {},
        "field_diffs": counts.get("field_diffs") or [],
        "field_diff_lines": counts.get("field_diff_lines") or [],
        "held_for_review": quarantine.get("rows") or [],
        "held_for_review_lines": quarantine.get("lines") or [],
        "held_for_review_reasons": quarantine.get("reason_counts") or {},
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
    if totals.get("rows_held_for_review"):
        lines.append(
            f"{REVIEW_FILE}: {totals['rows_held_for_review']} row(s) held back "
            "from the write-back, everything else was delivered."
        )
        for line in summary.get("held_for_review_lines") or []:
            lines.append(f"  {line}")
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


def _agent_facing_summary(summary: dict) -> dict:
    """
    Slim payload for the model. merge_lines is first so it cannot be truncated
    behind the per-lead directory. The full summary stays in session state.

    Chunked runs omit per-lead technical_log so the chat reply stays inside
    the model output cap; those reasons remain in dedup_log.csv.
    """
    chunked = int(summary.get("chunk_count") or 1) > 1
    instruction = (
        "What changed must be copied from critical_lines only. "
        "Do not list company casing, industry maps, title expansions, "
        "https prefixes, or phone regrouping. Do not write "
        "Company: N changes, e.g. Copy every merge_lines entry."
    )
    if chunked:
        instruction += (
            " This run was batched. Copy chunk_note and every chunk_lines "
            "entry. Per-lead reasons are in dedup_log.csv; do not invent them."
        )
    held_lines = summary.get("held_for_review_lines") or []
    if held_lines:
        instruction += (
            f" {len(held_lines)} row(s) were held back from deduped.csv and "
            f"written to {REVIEW_FILE} instead. Say so, give the count, and "
            "copy every held_for_review_lines entry verbatim. The rest of the "
            "file was delivered normally."
        )
    return {
        "totals": summary.get("totals", {}),
        "files": summary.get("files", []),
        "held_for_review_lines": held_lines[:MAX_FIELD_EXAMPLES],
        "held_for_review_reasons": summary.get("held_for_review_reasons") or {},
        "merge_lines": summary.get("merge_lines", []),
        "merges": summary.get("merges", []),
        "dropped": summary.get("dropped", []),
        "critical_lines": summary.get("critical_lines", []),
        "routine_cleanup": summary.get("routine_cleanup", ""),
        "flag_counts": summary.get("flag_counts", {}),
        "phone_states": summary.get("phone_states", {}),
        "technical_log": [] if chunked else (summary.get("technical_log") or []),
        "chunk_count": summary.get("chunk_count") or 1,
        "chunk_size": summary.get("chunk_size") or CHUNK_SIZE,
        "chunk_note": summary.get("chunk_note") or "",
        "chunk_lines": summary.get("chunk_lines") or [],
        "field_diffs": summary.get("field_diffs", []),
        "field_diff_lines": summary.get("field_diff_lines", []),
        "instruction": instruction,
    }


def lookup_lead(query: str, tool_context: ToolContext) -> dict:
    """Look up a lead from the last pipeline run by Id, name, email, or company."""
    summary = tool_context.state.get("last_summary") or {}
    needle = (query or "").strip().lower()
    if not needle:
        return {"status": "error", "message": "Provide an Id, name, email, or company to look up."}
    if not summary:
        return {"status": "error", "message": "No prior run in this session. Upload a CSV first."}

    matches = []
    for row in summary.get("directory", []):
        blob = " ".join(
            str(row.get(key, "")) for key in ("id", "name", "company", "email", "phone")
        ).lower()
        if needle in blob:
            matches.append(row)
    merges = []
    for merge in summary.get("merges", []):
        blob = " ".join([
            merge.get("line", ""),
            merge.get("survivor_id", ""),
            merge.get("survivor_name", ""),
            " ".join(merge.get("merged_from_ids", [])),
        ]).lower()
        if needle in blob:
            merges.append(merge)
    return {
        "status": "ok",
        "query": query,
        "matches": matches[:25],
        "merges": merges,
        "match_count": len(matches),
    }


def _append_reason(current, extra: str) -> str:
    return " | ".join(part for part in (cell(current), extra) if part)


def _enforce_phone_website_gates(source_df: pd.DataFrame, df_out: pd.DataFrame) -> pd.DataFrame:
    """
    Tool-level safety net matching A1/A2: never silently drop a country code
    or a valid existing website. Restore the source value and mark review.
    """
    if "Id" not in df_out.columns or "Id" not in source_df.columns:
        return df_out
    source_by_id = {cell(row.get("Id", "")): row for row in source_df.to_dict("records")}
    df_out = df_out.copy()
    for col in ("data_quality_flags", "hitl_review", "phone_status", "change_reasons"):
        if col not in df_out.columns:
            df_out[col] = ""
    for idx, row in df_out.iterrows():
        src = source_by_id.get(cell(row.get("Id", "")))
        if not src:
            continue
        if "Phone" in df_out.columns and phone_lost_country_code(src.get("Phone"), row.get("Phone")):
            # Regrouped, not reformatted: restoring the source value verbatim can
            # put a spreadsheet-evaluable string into the write-back.
            raw = excel_safe_international(strip_excel_artifacts(src.get("Phone")))
            df_out.at[idx, "Phone"] = raw
            df_out.at[idx, "phone_status"] = STATUS_NEEDS_REVIEW
            df_out.at[idx, "hitl_review"] = "Yes"
            flags = cell(df_out.at[idx, "data_quality_flags"])
            if "needs_country_code_review" not in flags.split("|"):
                df_out.at[idx, "data_quality_flags"] = (
                    f"{flags}|needs_country_code_review" if flags else "needs_country_code_review"
                )
            df_out.at[idx, "change_reasons"] = _append_reason(
                df_out.at[idx, "change_reasons"],
                "phone: restored original; country code would have been dropped",
            )
        if "Website" in df_out.columns:
            kept = normalize_website(src.get("Website"))
            if kept and not cell(row.get("Website")):
                df_out.at[idx, "Website"] = kept
                df_out.at[idx, "change_reasons"] = _append_reason(
                    df_out.at[idx, "change_reasons"],
                    "website: restored existing valid value (lookup must not delete)",
                )
    return df_out


def _technical_log_rows(df: pd.DataFrame) -> list:
    rows = []
    if "change_reasons" not in df.columns:
        return rows
    for _, row in df.iterrows():
        reasons = cell(row.get("change_reasons", ""))
        if not reasons:
            continue
        rows.append({
            "id": cell(row.get("Id", "")),
            "phone_status": cell(row.get("phone_status", "")),
            "reasons": reasons,
        })
    return rows


def _phone_state_counts(df: pd.DataFrame) -> dict:
    """How many phones ended up validated vs still needing a human."""
    if "phone_status" not in df.columns:
        return {}
    counts = {}
    for value in df["phone_status"].map(cell):
        if value:
            counts[value] = counts.get(value, 0) + 1
    return counts


def _fill_websites(df: pd.DataFrame) -> pd.DataFrame:
    """Run website resolution on every row, including HITL-held records."""
    if "Website" not in df.columns:
        return df
    df = df.copy()
    for idx, row in df.iterrows():
        df.at[idx, "Website"] = resolve_website(
            row.get("Website"), row.get("Company", ""), row.get("Email", ""),
        ) or ""
    return df


def _quarantine_unwritable_rows(df_out: pd.DataFrame, source_cols) -> tuple:
    """
    Hold back rows that fail a row-level invariant instead of failing the batch.

    A single dirty lead used to block every row it shipped with. The offending
    rows go to needs_review.csv with the reason attached; the rest are written
    back. Batch-shaped invariants still hard-fail in check_records.
    """
    empty = {"ids": [], "rows": [], "lines": [], "reason_counts": {}, "csv": ""}
    if df_out.empty:
        return df_out, empty

    reasons_by_index = {}
    for idx, record in zip(df_out.index, df_out.to_dict("records")):
        reasons = row_level_violations(record)
        if reasons:
            reasons_by_index[idx] = reasons
    if not reasons_by_index:
        return df_out, empty

    held = df_out.loc[list(reasons_by_index)]
    rows, lines, reason_counts = [], [], {}
    for idx, record in zip(held.index, held.to_dict("records")):
        reasons = reasons_by_index[idx]
        lead_id = cell(record.get("Id", ""))
        rows.append({
            "id": lead_id,
            "name": " ".join(
                part for part in
                (cell(record.get("FirstName", "")), cell(record.get("LastName", "")))
                if part
            ),
            "company": cell(record.get("Company", "")),
            "email": cell(record.get("Email", "")),
            "phone": cell(record.get("Phone", "")),
            "phone_raw": cell(record.get("Phone_raw", "")),
            "phone_extension": cell(record.get("PhoneExtension", "")),
            "reasons": reasons,
        })
        lines.append(f"{lead_id} — {'; '.join(reasons)}")
        for reason in reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    review_cols = [col for col in source_cols if col in held.columns]
    review = held.reindex(columns=review_cols).fillna("").copy()
    review["needs_review_reason"] = [
        "; ".join(reasons_by_index[idx]) for idx in held.index
    ]
    review["Phone_raw"] = [
        cell(value) for value in held.get("Phone_raw", pd.Series("", index=held.index))
    ]
    review["phone_extension"] = [
        cell(value) for value in held.get("PhoneExtension", pd.Series("", index=held.index))
    ]
    quarantine = {
        "ids": [row["id"] for row in rows if row["id"]],
        "rows": rows,
        "lines": lines,
        "reason_counts": reason_counts,
        "csv": _write_salesforce_csv(review, list(review.columns)),
    }
    kept = df_out.drop(index=list(reasons_by_index))
    return kept.reset_index(drop=True), quarantine


def _merge_quarantines(parts: list) -> dict:
    ids, rows, lines, reason_counts = [], [], [], {}
    csvs = []
    for part in parts:
        quarantine = part.get("quarantine") or {}
        ids.extend(quarantine.get("ids") or [])
        rows.extend(quarantine.get("rows") or [])
        lines.extend(quarantine.get("lines") or [])
        for reason, count in (quarantine.get("reason_counts") or {}).items():
            reason_counts[reason] = reason_counts.get(reason, 0) + count
        if quarantine.get("csv"):
            csvs.append(quarantine["csv"])
    return {
        "ids": ids,
        "rows": rows,
        "lines": lines,
        "reason_counts": reason_counts,
        "csv": _concat_csv_documents(csvs) if csvs else "",
    }


def process_csv(csv_text: str, accept_changes: bool = False) -> dict:
    """
    Run the pipeline end to end.

    Pass accept_changes=True only to regenerate the known-good baseline; it is
    the one way to get output past the regression gate, and it makes accepting a
    diff an explicit act rather than an unnoticed one.
    """
    if len(csv_text) > MAX_CSV_CHARS:
        return {
            "status": "error",
            "message": f"CSV input too large ({len(csv_text)} chars). "
            f"Split into batches under {MAX_CSV_CHARS} characters and run separately.",
        }

    try:
        df = pd.read_csv(io.StringIO(csv_text), dtype=str, keep_default_na=False)
    except Exception as exc:
        return {"status": "error", "message": f"Error parsing CSV: {exc}"}

    df = _clean_columns(df)
    source_cols = [col for col in df.columns if col not in INTERNAL_COLS]
    if "Email" not in df.columns:
        return {"status": "error", "message": "Cannot continue: the file has no Email column."}

    if len(df) > MAX_ROWS:
        return {
            "status": "error",
            "message": f"CSV has more than {MAX_ROWS} rows. Split it into smaller batches.",
        }

    leads_in = len(df)
    source_contacts = snapshot_email_phone(df.to_dict("records"))
    df_valid, issue_count = validate_dataframe(df)
    df_norm, norm_count = normalize_dataframe(df_valid)
    df_norm = df_norm.copy()
    df_norm["__row_order"] = range(len(df_norm))
    work, merged_count, merge_log = deduplicate_dataframe(df_norm)
    df_out = work.fillna("").reset_index(drop=True)
    if "__row_order" in df_out.columns:
        df_out = df_out.sort_values("__row_order", kind="stable").drop(columns=["__row_order"])
    df_out = _fill_websites(df_out)

    has_phone_col = "Phone" in df_out.columns
    post_norm_junk = df_out.apply(
        lambda row: is_junk_lead_values(
            row.get("Company", ""),
            row.get("Email", ""),
            row.get("Phone", "") if has_phone_col else "",
            has_phone=has_phone_col,
        ),
        axis=1,
    )
    flagged_junk = (
        df_out["data_quality_flags"].astype(str).str.contains(
            r"test_data|junk_lead", na=False, regex=True,
        )
        if "data_quality_flags" in df_out.columns else pd.Series(False, index=df_out.index)
    )
    test_mask = flagged_junk | post_norm_junk
    if "data_quality_flags" in df_out.columns:
        for idx in df_out.index[post_norm_junk & ~flagged_junk]:
            flags = str(df_out.at[idx, "data_quality_flags"] or "")
            df_out.at[idx, "data_quality_flags"] = (
                f"{flags}|junk_lead" if flags else "junk_lead"
            )
    dropped_count = int(test_mask.sum())
    dropped_test_ids = (
        list(df_out.loc[test_mask, "Id"].astype(str))
        if dropped_count and "Id" in df_out.columns else []
    )
    audit_csv = _build_audit_log(df_out, merge_log)
    audit_rows = list(csv.DictReader(io.StringIO(audit_csv)))
    if dropped_count:
        df_out = df_out.loc[~test_mask].copy().reset_index(drop=True)

    df_out = _enforce_phone_website_gates(df, df_out)

    for col in PHONE_WRITE_COLS:
        if col in df_out.columns:
            df_out[col] = df_out[col].map(strip_excel_artifacts)

    df_out, quarantine = _quarantine_unwritable_rows(df_out, source_cols)
    if quarantine["ids"] and df_out.empty:
        # Holding back one dirty lead is normal. Holding back every lead means a
        # transformation broke, not that the data is dirty, so nothing is
        # written and the run says so instead of shipping an empty file.
        reasons = sorted(quarantine["reason_counts"])
        return {
            "status": "error",
            "message": (
                "Output blocked by invariant checks: every row failed a "
                "row-level check, which points at the pipeline rather than the "
                "data: " + "; ".join(reasons)
            ),
            "invariant_violations": reasons,
        }

    blank_email_count = (
        int(df_out["Email"].astype(str).str.strip().isin(["", "nan"]).sum())
        if "Email" in df_out.columns else 0
    )
    hitl_records = (
        int((df_out["hitl_review"].astype(str) == "Yes").sum())
        if "hitl_review" in df_out.columns else 0
    )

    writable = df_out.reindex(columns=source_cols).fillna("")

    input_ids = list(df["Id"].astype(str)) if "Id" in df.columns else []
    violations = check_records(
        df_out.to_dict("records"),
        rows_in=leads_in,
        input_ids=input_ids,
        merge_log=merge_log,
        dropped_test_ids=dropped_test_ids,
        quarantined_ids=quarantine["ids"],
        source_contacts=source_contacts,
    )
    if violations:
        # Whatever survives the per-row hold is batch-shaped: a lead vanished,
        # the row count grew, duplicate emails survived. Those mean the
        # pipeline misbehaved, so the run still writes nothing.
        return {
            "status": "error",
            "message": "Output blocked by invariant checks: " + "; ".join(violations),
            "invariant_violations": violations,
        }

    expected_headers = [_export_header(col) for col in source_cols]
    schema_violations = check_output_schema(writable, source_cols, internal=df_out)
    if schema_violations:
        return {
            "status": "error",
            "message": "Output blocked by the pandera schema gate: "
            + "; ".join(schema_violations),
            "invariant_violations": schema_violations,
        }

    csv_text_out = _write_salesforce_csv(writable, source_cols)
    export_violations = check_exported_schema(csv_text_out, expected_headers)
    if export_violations:
        return {
            "status": "error",
            "message": "Output blocked by invariant checks: " + "; ".join(export_violations),
            "invariant_violations": export_violations,
        }

    field_changes = diff_against_golden(csv_text_out)
    field_diffs = as_dicts(field_changes)
    if field_changes and not accept_changes:
        return {
            "status": "error",
            "message": (
                f"Output blocked by the regression gate: {len(field_changes)} field "
                "change(s) on Ids that also exist in the last known-good output. "
                "Review each one, then re-run with accept_changes=True to move the "
                "baseline."
            ),
            "invariant_violations": format_changes(field_changes),
            "field_diffs": field_diffs,
        }
    counts = {
        "leads_in": leads_in,
        "leads_out": len(df_out),
        "validation_issues": issue_count,
        "values_normalized": norm_count,
        "duplicates_merged": merged_count,
        "records_dropped": dropped_count,
        "hitl_records": hitl_records,
        "blank_email_kept": blank_email_count,
        "technical_log": _technical_log_rows(df_out),
        "phone_states": _phone_state_counts(df_out),
        "field_diffs": field_diffs,
        "field_diff_lines": format_changes(field_changes),
        "quarantine": quarantine,
    }
    summary = _build_run_summary(df, writable, merge_log, audit_rows, counts)

    return {
        "status": "ok",
        "csv": csv_text_out,
        "excel_csv": _write_excel_safe_csv(writable, source_cols),
        "excel_file": "deduped_excel_review.csv",
        "audit_csv": audit_csv,
        "audit_file": "dedup_log.csv",
        "review_csv": quarantine["csv"],
        "review_file": REVIEW_FILE,
        "leads_in": leads_in,
        "validation_issues": issue_count,
        "values_normalized": norm_count,
        "duplicates_merged": merged_count,
        "leads_out": len(df_out),
        "blank_email_kept": blank_email_count,
        "hitl_records": hitl_records,
        "records_dropped": dropped_count,
        "rows_held_for_review": len(quarantine["ids"]),
        "quarantine": quarantine,
        "invariant_violations": [],
        "field_diffs": field_diffs,
        "expected_headers": [_export_header(col) for col in source_cols],
        "file": "deduped.csv",
        "summary": summary,
    }


def _sort_for_chunking(df: pd.DataFrame) -> pd.DataFrame:
    """Put matching emails next to each other so a 200-row slice can still merge them."""
    keys = []
    extra = pd.DataFrame(index=df.index)
    if "Email" in df.columns:
        extra["_chunk_email"] = df["Email"].map(lambda value: cell(value).lower())
        keys.append("_chunk_email")
    if "Company" in df.columns:
        extra["_chunk_company"] = df["Company"].map(lambda value: cell(value).lower())
        keys.append("_chunk_company")
    if "Id" in df.columns:
        extra["_chunk_id"] = df["Id"].map(cell)
        keys.append("_chunk_id")
    if not keys:
        return df
    return df.join(extra).sort_values(keys, kind="stable").drop(columns=keys)


def _concat_csv_documents(texts: list) -> str:
    if not texts:
        return ""
    chunks = []
    for index, text in enumerate(texts):
        body = (text or "").lstrip("\ufeff")
        if index == 0:
            chunks.append(body.rstrip("\n"))
            continue
        lines = body.splitlines()
        if len(lines) <= 1:
            continue
        chunks.append("\n".join(lines[1:]))
    return "\n".join(chunks) + "\n"


def _combine_chunk_results(parts: list, rows_in: int) -> dict:
    columns = None
    frames = []
    for part in parts:
        frame = pd.read_csv(io.StringIO(part["csv"]), dtype=str, keep_default_na=False)
        if columns is None:
            columns = list(frame.columns)
        frames.append(frame.reindex(columns=columns).fillna(""))
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    csv_text_out = _write_salesforce_csv(combined, columns)
    excel_csv = _write_excel_safe_csv(combined, columns)
    audit_csv = _concat_csv_documents([part["audit_csv"] for part in parts])

    def _sum(key):
        return sum(part.get(key, 0) or 0 for part in parts)

    phone_states = {}
    flag_counts = {}
    technical_log = []
    merges, merge_lines, dropped, hitl, directory = [], [], [], [], []
    critical_lines, field_diffs, field_diff_lines = [], [], []
    chunk_lines = []
    for index, part in enumerate(parts, start=1):
        summary = part.get("summary") or {}
        totals = summary.get("totals") or {}
        tech = summary.get("technical_log") or []
        technical_log.extend(tech)
        merges.extend(summary.get("merges") or [])
        merge_lines.extend(summary.get("merge_lines") or [])
        dropped.extend(summary.get("dropped") or [])
        hitl.extend(summary.get("hitl") or [])
        directory.extend(summary.get("directory") or [])
        critical_lines.extend(summary.get("critical_lines") or [])
        field_diffs.extend(summary.get("field_diffs") or [])
        field_diff_lines.extend(summary.get("field_diff_lines") or [])
        for state, count in (summary.get("phone_states") or {}).items():
            phone_states[state] = phone_states.get(state, 0) + count
        for flag, count in (summary.get("flag_counts") or {}).items():
            flag_counts[flag] = flag_counts.get(flag, 0) + count
        held = len((part.get("quarantine") or {}).get("ids") or [])
        chunk_lines.append(
            f"Batch {index}/{len(parts)}: {totals.get('leads_in', part['leads_in'])} in, "
            f"{totals.get('leads_out', part['leads_out'])} out, "
            f"{totals.get('duplicates_merged', part['duplicates_merged'])} merged, "
            f"{totals.get('records_dropped', part['records_dropped'])} dropped, "
            f"{totals.get('hitl_records', part['hitl_records'])} HITL, "
            f"{held} held for review, "
            f"{len(tech)} log lines."
        )

    quarantine = _merge_quarantines(parts)
    chunk_note = (
        f"Processed in {len(parts)} batches of {CHUNK_SIZE} rows (sorted by email). "
        "Duplicates that still sit in different batches were not merged. "
        "Per-lead change reasons are in dedup_log.csv."
    )
    counts = {
        "leads_in": rows_in,
        "leads_out": len(combined),
        "validation_issues": _sum("validation_issues"),
        "values_normalized": _sum("values_normalized"),
        "duplicates_merged": _sum("duplicates_merged"),
        "records_dropped": _sum("records_dropped"),
        "hitl_records": _sum("hitl_records"),
        "blank_email_kept": _sum("blank_email_kept"),
        "technical_log": technical_log,
        "phone_states": phone_states,
        "field_diffs": field_diffs,
        "field_diff_lines": field_diff_lines,
        "quarantine": quarantine,
    }
    first_files = [
        entry for entry in ((parts[0].get("summary") or {}).get("files") or [])
        if not entry.startswith(REVIEW_FILE)
    ]
    if quarantine["ids"]:
        first_files = first_files + [
            f"{REVIEW_FILE} — {len(quarantine['ids'])} row(s) held back from the "
            "write-back with the reason attached"
        ]
    summary = {
        "totals": {
            "leads_in": counts["leads_in"],
            "leads_out": counts["leads_out"],
            "validation_issues": counts["validation_issues"],
            "values_normalized": counts["values_normalized"],
            "duplicates_merged": counts["duplicates_merged"],
            "records_dropped": counts["records_dropped"],
            "hitl_records": counts["hitl_records"],
            "blank_email_kept": counts["blank_email_kept"],
            "rows_held_for_review": len(quarantine["ids"]),
        },
        "files": first_files,
        "merges": merges,
        "merge_lines": merge_lines,
        "dropped": dropped,
        "hitl": hitl,
        "directory": directory,
        "critical_lines": critical_lines,
        "routine_cleanup": "",
        "flag_counts": flag_counts,
        "technical_log": technical_log,
        "phone_states": phone_states,
        "field_diffs": field_diffs,
        "field_diff_lines": field_diff_lines,
        "held_for_review": quarantine["rows"],
        "held_for_review_lines": quarantine["lines"],
        "held_for_review_reasons": quarantine["reason_counts"],
        "chunk_count": len(parts),
        "chunk_size": CHUNK_SIZE,
        "chunk_note": chunk_note,
        "chunk_lines": chunk_lines,
    }
    return {
        "status": "ok",
        "csv": csv_text_out,
        "excel_csv": excel_csv,
        "excel_file": parts[0]["excel_file"],
        "audit_csv": audit_csv,
        "audit_file": parts[0]["audit_file"],
        "review_csv": quarantine["csv"],
        "review_file": REVIEW_FILE,
        "rows_held_for_review": len(quarantine["ids"]),
        "quarantine": quarantine,
        "leads_in": counts["leads_in"],
        "validation_issues": counts["validation_issues"],
        "values_normalized": counts["values_normalized"],
        "duplicates_merged": counts["duplicates_merged"],
        "leads_out": counts["leads_out"],
        "blank_email_kept": counts["blank_email_kept"],
        "hitl_records": counts["hitl_records"],
        "records_dropped": counts["records_dropped"],
        "invariant_violations": [],
        "field_diffs": field_diffs,
        "expected_headers": parts[0].get("expected_headers") or [_export_header(col) for col in columns],
        "file": parts[0]["file"],
        "summary": summary,
    }


def process_csv_for_agent(csv_text: str) -> dict:
    """
    Agent entry: same pipeline as process_csv, but files over CHUNK_SIZE rows
    are sorted by email and run in batches of CHUNK_SIZE, then stitched.
    """
    if len(csv_text) > MAX_CSV_CHARS:
        return process_csv(csv_text)
    try:
        df = pd.read_csv(io.StringIO(csv_text), dtype=str, keep_default_na=False)
    except Exception:
        return process_csv(csv_text)
    df = _clean_columns(df)
    if "Email" not in df.columns or len(df) <= CHUNK_SIZE or len(df) > MAX_ROWS:
        return process_csv(csv_text)

    df = _sort_for_chunking(df)
    parts = []
    for index, start in enumerate(range(0, len(df), CHUNK_SIZE)):
        chunk = df.iloc[start:start + CHUNK_SIZE]
        chunk_csv = _write_salesforce_csv(chunk, list(chunk.columns))
        result = process_csv(chunk_csv)
        if result["status"] != "ok":
            prefix = (
                f"Batch {index + 1} (rows {start + 1}-"
                f"{start + len(chunk)} of {len(df)}): "
            )
            result = dict(result)
            result["message"] = prefix + (result.get("message") or "batch failed")
            return result
        parts.append(result)
    return _combine_chunk_results(parts, rows_in=len(df))


async def run_dedup_pipeline(
    csv_text: str = "",
    filename: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """
    Validates leads, normalizes fields from the reference taxonomy,
    then deduplicates by exact email and by same-person name plus
    domain or company. Keeps one surviving row per group.

    Pass filename when the chat shows an uploaded file, for example
    filename="leads.csv", and leave csv_text empty. This tool reads that
    file itself. Never copy CSV rows into csv_text for an uploaded file:
    the rows do not need to pass through the reply, and a large paste
    fails before this tool runs. Use csv_text only for a handful of rows
    typed directly into the chat.

    Saves deduped.csv and dedup_log.csv as artifacts. Returns counts plus a
    structured summary of every merge, drop, review flag, and field change
    so the agent can narrate the run and answer follow-up questions.

    Files larger than CHUNK_SIZE rows are sorted by email and processed in
    batches of CHUNK_SIZE, then written back as one combined file.
    """
    source = await _resolve_dedup_csv(csv_text, filename, tool_context)
    if source["status"] != "ok":
        return source

    result = process_csv_for_agent(source["csv_text"])
    if result["status"] != "ok":
        field_diffs = result.get("field_diffs") or []
        if field_diffs:
            # Re-word the gate for the agent: it has no way to accept a diff, so
            # the only correct move is to report the changes and stop.
            return {
                "status": "error",
                "message": (
                    f"Output blocked: {len(field_diffs)} field change(s) on Ids that "
                    "already exist in the last known-good baseline. Each one needs "
                    "explicit review before this run can be delivered."
                ),
                "field_diffs": field_diffs,
                "field_diff_lines": result.get("invariant_violations", []),
            }
        return result

    summary = result["summary"]

    tool_context.state["leads_in"] = result["leads_in"]
    tool_context.state["leads_out"] = result["leads_out"]
    tool_context.state["source_file"] = source["source"]
    tool_context.state["last_summary"] = summary
    tool_context.state["last_audit"] = list(csv.DictReader(io.StringIO(result["audit_csv"])))

    payload = {
        "status": result["status"],
        "leads_in": result["leads_in"],
        "validation_issues": result["validation_issues"],
        "values_normalized": result["values_normalized"],
        "duplicates_merged": result["duplicates_merged"],
        "records_dropped": result["records_dropped"],
        "leads_out": result["leads_out"],
        "blank_email_kept": result["blank_email_kept"],
        "hitl_records": result["hitl_records"],
        "rows_held_for_review": result.get("rows_held_for_review", 0),
        "file": result["file"],
        "audit_file": result["audit_file"],
        "source_file": source["source"],
        "summary": _agent_facing_summary(summary),
    }
    if result.get("rows_held_for_review"):
        payload["review_file"] = result["review_file"]

    try:
        csv_bytes = result["csv"].encode("utf-8")
        csv_part = types.Part.from_bytes(
            data=csv_bytes, mime_type="text/csv; charset=utf-8"
        )
        payload["artifact_version"] = await tool_context.save_artifact(
            filename="deduped.csv", artifact=csv_part
        )
        schema_after = check_exported_schema(
            csv_bytes.decode("utf-8"), result.get("expected_headers") or [],
        )
        if schema_after:
            return {
                "status": "error",
                "message": "Output blocked by schema assertion after save_artifact: "
                + "; ".join(schema_after),
                "invariant_violations": schema_after,
            }
        log_part = types.Part.from_bytes(
            data=result["audit_csv"].encode("utf-8"), mime_type="text/csv; charset=utf-8"
        )
        await tool_context.save_artifact(filename="dedup_log.csv", artifact=log_part)
        review_part = types.Part.from_bytes(
            data=result["excel_csv"].encode("utf-8"), mime_type="text/csv; charset=utf-8"
        )
        await tool_context.save_artifact(
            filename=result["excel_file"], artifact=review_part
        )
        if result.get("review_csv"):
            held_part = types.Part.from_bytes(
                data=result["review_csv"].encode("utf-8"),
                mime_type="text/csv; charset=utf-8",
            )
            await tool_context.save_artifact(
                filename=result["review_file"], artifact=held_part
            )
        tool_context.state["deduped_csv_ready"] = True
        return payload
    except Exception:
        return {
            "status": "error",
            "message": "Dedup finished but the file could not be saved. Re-run the job.",
        }


def require_deduped_csv(state) -> dict:
    """Block enrichment until this session has saved deduped.csv."""
    if (state or {}).get("deduped_csv_ready"):
        return {"status": "ok"}
    return {"status": "error", "message": ENRICH_REQUIRES_DEDUP_MESSAGE}


def _decode_csv_bytes(data: bytes) -> str:
    """Spreadsheet exports arrive as UTF-8 with or without a BOM, or as Latin-1."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _part_to_text(part) -> str:
    if part is None:
        return ""
    inline = getattr(part, "inline_data", None) or getattr(part, "inlineData", None)
    if inline is not None:
        data = getattr(inline, "data", b"")
        if isinstance(data, bytes):
            return _decode_csv_bytes(data)
        return str(data)
    text = getattr(part, "text", None)
    return text or ""


async def _load_artifact_text(tool_context: ToolContext, filename: str) -> str:
    try:
        part = await tool_context.load_artifact(filename=filename)
    except TypeError:
        try:
            part = await tool_context.load_artifact(filename)
        except Exception:
            part = None
    except Exception:
        part = None
    return _part_to_text(part)


async def _uploaded_artifact_names(tool_context: ToolContext) -> list:
    """Session artifacts that came from the user, oldest first."""
    try:
        names = await tool_context.list_artifacts()
    except Exception:
        return []
    uploads = [name for name in (names or []) if name not in GENERATED_ARTIFACTS]
    csvs = [name for name in uploads if name.lower().endswith(".csv")]
    return csvs or uploads


async def _resolve_dedup_csv(csv_text: str, filename: str, tool_context) -> dict:
    """
    Decide where the lead rows come from.

    Reading the upload from an artifact keeps the rows out of the model's
    function-call arguments. A model that has to retype a large CSV into
    csv_text runs past its output cap and Gemini returns
    MALFORMED_FUNCTION_CALL before this tool is ever invoked.
    """
    name = (filename or "").strip().strip('"').strip("'")
    if name:
        loaded = await _load_artifact_text(tool_context, name)
        if loaded.strip():
            return {"status": "ok", "csv_text": loaded, "source": name}
        available = await _uploaded_artifact_names(tool_context)
        hint = f" Files in this session: {', '.join(available)}." if available else ""
        return {
            "status": "error",
            "message": f"Uploaded file '{name}' was not found in this session.{hint}",
        }

    if (csv_text or "").strip():
        return {"status": "ok", "csv_text": csv_text, "source": "inline"}

    for candidate in reversed(await _uploaded_artifact_names(tool_context)):
        loaded = await _load_artifact_text(tool_context, candidate)
        if loaded.strip():
            return {"status": "ok", "csv_text": loaded, "source": candidate}
    return {"status": "error", "message": NO_UPLOAD_MESSAGE}


async def _load_deduped_csv(tool_context: ToolContext) -> dict:
    """Enrichment may only read the write-back from a completed dedup run."""
    try:
        part = await tool_context.load_artifact(filename="deduped.csv")
    except TypeError:
        part = await tool_context.load_artifact("deduped.csv")
    except Exception:
        part = None

    loaded = _part_to_text(part)
    if not loaded:
        return {
            "status": "error",
            "message": "Cannot enrich: deduped.csv was not found. "
            "Run run_dedup_pipeline first so it can produce that file.",
        }
    return {"status": "ok", "csv_text": loaded}


def _parse_lead_csv(csv_text: str) -> dict:
    if len(csv_text) > MAX_CSV_CHARS:
        return {
            "status": "error",
            "message": f"CSV input too large ({len(csv_text)} chars). "
            f"Split into batches under {MAX_CSV_CHARS} characters and run separately.",
        }
    try:
        df = pd.read_csv(io.StringIO(csv_text), dtype=str, keep_default_na=False)
    except Exception as exc:
        return {"status": "error", "message": f"Error parsing CSV: {exc}"}

    df = _clean_columns(df)
    if "Email" not in df.columns and not (
        "FirstName" in df.columns and "LastName" in df.columns and "Company" in df.columns
    ):
        return {
            "status": "error",
            "message": "Cannot enrich: need an Email column, or FirstName, LastName, and Company.",
        }
    if len(df) > MAX_ROWS:
        return {
            "status": "error",
            "message": f"CSV has more than {MAX_ROWS} rows. Split it into smaller batches.",
        }
    return {"status": "ok", "df": df}


def _attach_hygiene_audit(df: pd.DataFrame, audit_rows) -> pd.DataFrame:
    """Join email_status / Email_raw from dedup_log so enrich can replace MX misses."""
    by_id = {}
    for row in audit_rows or []:
        lead_id = cell(row.get("surviving_lead_id", ""))
        if lead_id:
            by_id[lead_id] = row
    if not by_id:
        return df
    df = df.copy()
    if "email_status" not in df.columns:
        df["email_status"] = ""
    if "Email_raw" not in df.columns:
        df["Email_raw"] = ""
    for idx, record in df.iterrows():
        entry = by_id.get(cell(record.get("Id", "")))
        if not entry:
            continue
        if not cell(df.at[idx, "email_status"]):
            df.at[idx, "email_status"] = cell(entry.get("email_status", ""))
        if not cell(df.at[idx, "Email_raw"]):
            df.at[idx, "Email_raw"] = cell(entry.get("email_raw", ""))
    return df


def _enrichment_review_csv(reviews) -> str:
    if not reviews:
        return ""
    rows = []
    for item in reviews:
        rows.append({
            "Id": item.get("id", ""),
            "Email": item.get("email", ""),
            "email_raw": item.get("email_raw", ""),
            "Company": item.get("company", ""),
            "Website": item.get("website", ""),
            "needs_review_reason": "; ".join(item.get("reasons") or []),
        })
    frame = pd.DataFrame(rows)
    return _write_salesforce_csv(frame, list(frame.columns))


def _enrichment_review_lines(reviews) -> list:
    lines = []
    for item in reviews or []:
        lead_id = item.get("id") or ""
        reasons = "; ".join(item.get("reasons") or [])
        if lead_id or reasons:
            lines.append(f"{lead_id} — {reasons}")
    return lines


async def search_and_enrich(csv_text: str, tool_context: ToolContext) -> dict:
    """
    Enrich missing lead CSV fields with Lusha Search and Enrich.

    Always reads the deduped.csv artifact from a prior run_dedup_pipeline
    call. csv_text is ignored so a raw upload cannot skip hygiene. If
    deduped.csv is missing the tool errors and spends no Lusha credits.

    Uses POST https://api.lusha.com/v3/contacts/search-and-enrich (up to 100
    contacts per request). Looks up each row by email, or by firstName +
    lastName + companyName/companyDomain. Reveals phones only when Phone is
    blank. Reveals emails when Email is blank or hygiene marked it not
    deliverable. A Lusha email is written only when its host matches Company
    or the row's website/email domain. Unvalidated emails with no trusted
    replacement are blanked and listed in enrichment_review.csv. Existing
    Phone values are never replaced. Website fills require a plausible,
    company-aligned host. AnnualRevenue is filled only from an exact
    provider figure; a revenueRange min/max bucket is returned in stats as
    revenue_ranges and is never written to the CSV. LeadSource, Status,
    CreatedDate, and OwnerId are left unchanged.

    Pass an empty string for csv_text. Returns counts only; the write-back
    file is saved as enriched.csv.
    """
    blocked = require_deduped_csv(tool_context.state)
    if blocked["status"] != "ok":
        return blocked
    loaded = await _load_deduped_csv(tool_context)
    if loaded["status"] != "ok":
        return loaded

    parsed = _parse_lead_csv(loaded["csv_text"])
    if parsed["status"] != "ok":
        return parsed

    df = parsed["df"].fillna("")
    source_cols = list(df.columns)
    leads_in = len(df)
    before = snapshot_email_phone(df.to_dict("records"))
    df = _attach_hygiene_audit(df, tool_context.state.get("last_audit"))
    df_enriched, stats = enrich_dataframe(df)
    if stats.get("error"):
        return {"status": "error", "message": stats["error"]}

    df_enriched = df_enriched.fillna("")
    preserve_violations = check_enrich_preservation(
        before,
        df_enriched.to_dict("records"),
        allowed_email_blank_ids=stats.get("email_blanked_no_replacement_ids"),
    )
    if preserve_violations:
        return {
            "status": "error",
            "message": "Output blocked by invariant checks: "
            + "; ".join(preserve_violations),
            "invariant_violations": preserve_violations,
        }
    writable = df_enriched.reindex(columns=source_cols).fillna("")
    output = io.StringIO()
    writable.to_csv(output, index=False)

    review_rows = stats.get("enrichment_review") or []
    review_csv = _enrichment_review_csv(review_rows)
    review_lines = _enrichment_review_lines(review_rows)

    payload = {
        "status": "ok",
        "leads_in": leads_in,
        "rows_attempted": stats["rows_attempted"],
        "rows_matched": stats["rows_matched"],
        "rows_not_found": stats["rows_not_found"],
        "rows_skipped": stats["rows_skipped"],
        "fields_filled": stats["fields_filled"],
        "credits_charged": stats["credits_charged"],
        "emails_rejected": stats.get("emails_rejected", 0),
        "websites_rejected": stats.get("websites_rejected", 0),
        "emails_blanked_no_replacement": len(
            stats.get("email_blanked_no_replacement_ids") or []
        ),
        "revenue_range_rows": len(stats.get("revenue_ranges") or []),
        "revenue_ranges": stats.get("revenue_ranges") or [],
        "enrichment_review_lines": review_lines,
        "file": "enriched.csv",
    }
    tool_context.state["fields_filled"] = stats["fields_filled"]
    tool_context.state["leads_out"] = leads_in

    try:
        csv_part = types.Part.from_bytes(
            data=output.getvalue().encode("utf-8"), mime_type="text/csv"
        )
        version = await tool_context.save_artifact(
            filename="enriched.csv", artifact=csv_part
        )
        payload["artifact_version"] = version
        if review_csv:
            review_part = types.Part.from_bytes(
                data=review_csv.encode("utf-8"), mime_type="text/csv"
            )
            await tool_context.save_artifact(
                filename=ENRICH_REVIEW_FILE, artifact=review_part
            )
            payload["review_file"] = ENRICH_REVIEW_FILE
        return payload
    except Exception:
        return {
            "status": "error",
            "message": "Enrichment finished but the file could not be saved. Re-run the job.",
        }
