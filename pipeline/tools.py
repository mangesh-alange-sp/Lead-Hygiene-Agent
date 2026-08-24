"""
tools.py
ADK tool wrapper: parse → validate → normalize → dedupe → invariants → artifact.
Output CSV keeps the source columns only (Salesforce write-back shape).
"""

import csv
import io

import pandas as pd
from google.adk.tools import ToolContext
from google.genai import types

from .dedupe import deduplicate_dataframe
from .invariants import check_records
from .normalize import normalize_dataframe
from .domains import company_match_key, host_of
from .textnorm import cell, digits_only, fold_text, strip_excel_artifacts
from .validate import validate_dataframe
from .website import resolve_website

MAX_ROWS = 5000
MAX_CSV_CHARS = 2_000_000
MAX_FIELD_EXAMPLES = 12
MAX_CHANGE_ROWS = 400
INTERNAL_COLS = ("data_quality_flags", "hitl_review")
PHONE_WRITE_COLS = ("Phone", "MobilePhone")
AUDIT_FIELDS = (
    "surviving_lead_id",
    "merged_from_ids",
    "match_signals_used",
    "confidence_score",
    "decision",
    "data_quality_flags",
)


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
        entry = by_id.pop(lead_id, None)
        if not entry and not flags:
            continue
        entry = entry or {}
        rows.append({
            "surviving_lead_id": lead_id,
            "merged_from_ids": entry.get("merged_from_ids", ""),
            "match_signals_used": entry.get("match_signals_used", ""),
            "confidence_score": entry.get("confidence_score", ""),
            "decision": entry.get("decision") or _audit_decision(flags),
            "data_quality_flags": flags,
        })
    # Ids that were merged away still deserve a log line.
    for lead_id, entry in by_id.items():
        rows.append({
            "surviving_lead_id": lead_id,
            "merged_from_ids": entry.get("merged_from_ids", ""),
            "match_signals_used": entry.get("match_signals_used", ""),
            "confidence_score": entry.get("confidence_score", ""),
            "decision": entry.get("decision", ""),
            "data_quality_flags": "",
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
    if "missing_hard_required" in parts:
        return "hitl_review"
    if any(flag in parts for flag in (
        "missing_email", "missing_phone", "unformatted_phone", "low_quality_title",
        "test_data",
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
        if row.get("decision") == "dropped_test_data":
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
        },
        "files": [
            "deduped.csv — Salesforce write-back (same columns as the upload)",
            "dedup_log.csv — merges, dropped test rows, and review flags",
        ],
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
        "instruction": (
            "What changed must be copied from critical_lines only. "
            "Do not list company casing, industry maps, title expansions, "
            "https prefixes, or phone regrouping. Do not write "
            "Company: N changes, e.g. Copy every merge_lines entry."
        ),
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


def process_csv(csv_text: str) -> dict:
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
    df_valid, issue_count = validate_dataframe(df)
    df_norm, norm_count = normalize_dataframe(df_valid)
    df_norm = df_norm.copy()
    df_norm["__row_order"] = range(len(df_norm))
    work, merged_count, merge_log = deduplicate_dataframe(df_norm)
    df_out = work.fillna("").reset_index(drop=True)
    if "__row_order" in df_out.columns:
        df_out = df_out.sort_values("__row_order", kind="stable").drop(columns=["__row_order"])
    df_out = _fill_websites(df_out)

    test_mask = (
        df_out["data_quality_flags"].astype(str).str.contains("test_data", na=False)
        if "data_quality_flags" in df_out.columns else pd.Series(False, index=df_out.index)
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

    for col in PHONE_WRITE_COLS:
        if col in df_out.columns:
            df_out[col] = df_out[col].map(strip_excel_artifacts)

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
        writable.to_dict("records"),
        rows_in=leads_in,
        input_ids=input_ids,
        merge_log=merge_log,
        dropped_test_ids=dropped_test_ids,
    )
    if violations:
        return {
            "status": "error",
            "message": "Output blocked by invariant checks: " + "; ".join(violations),
            "invariant_violations": violations,
        }

    output = io.StringIO()
    writable.to_csv(output, index=False, encoding="utf-8", quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    csv_text_out = output.getvalue().lstrip("\ufeff")

    counts = {
        "leads_in": leads_in,
        "leads_out": len(df_out),
        "validation_issues": issue_count,
        "values_normalized": norm_count,
        "duplicates_merged": merged_count,
        "records_dropped": dropped_count,
        "hitl_records": hitl_records,
        "blank_email_kept": blank_email_count,
    }
    summary = _build_run_summary(df, writable, merge_log, audit_rows, counts)

    return {
        "status": "ok",
        "csv": csv_text_out,
        "audit_csv": audit_csv,
        "audit_file": "dedup_log.csv",
        "leads_in": leads_in,
        "validation_issues": issue_count,
        "values_normalized": norm_count,
        "duplicates_merged": merged_count,
        "leads_out": len(df_out),
        "blank_email_kept": blank_email_count,
        "hitl_records": hitl_records,
        "records_dropped": dropped_count,
        "invariant_violations": [],
        "file": "deduped.csv",
        "summary": summary,
    }


async def run_dedup_pipeline(csv_text: str, tool_context: ToolContext) -> dict:
    """
    Validates leads, normalizes fields from the reference taxonomy,
    then deduplicates by exact email and by same-person name plus
    domain or company. Keeps one surviving row per group.

    Saves deduped.csv and dedup_log.csv as artifacts. Returns counts plus a
    structured summary of every merge, drop, review flag, and field change
    so the agent can narrate the run and answer follow-up questions.
    """
    result = process_csv(csv_text)
    if result["status"] != "ok":
        return result

    summary = result["summary"]
    tool_context.state["leads_in"] = result["leads_in"]
    tool_context.state["leads_out"] = result["leads_out"]
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
        "file": result["file"],
        "audit_file": result["audit_file"],
        "summary": _agent_facing_summary(summary),
    }

    try:
        csv_part = types.Part.from_bytes(
            data=result["csv"].encode("utf-8"), mime_type="text/csv; charset=utf-8"
        )
        payload["artifact_version"] = await tool_context.save_artifact(
            filename="deduped.csv", artifact=csv_part
        )
        log_part = types.Part.from_bytes(
            data=result["audit_csv"].encode("utf-8"), mime_type="text/csv; charset=utf-8"
        )
        await tool_context.save_artifact(filename="dedup_log.csv", artifact=log_part)
        return payload
    except Exception:
        return {
            "status": "error",
            "message": "Dedup finished but the file could not be saved. Re-run the job.",
        }
