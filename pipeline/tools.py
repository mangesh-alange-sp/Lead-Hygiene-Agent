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
from .textnorm import strip_excel_artifacts
from .validate import validate_dataframe
from .website import resolve_website

MAX_ROWS = 5000
MAX_CSV_CHARS = 2_000_000
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
    audit_csv = _build_audit_log(df_out, merge_log)
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

    violations = check_records(writable.to_dict("records"), rows_in=leads_in)
    if violations:
        return {
            "status": "error",
            "message": "Output blocked by invariant checks: " + "; ".join(violations),
            "invariant_violations": violations,
        }

    output = io.StringIO()
    writable.to_csv(output, index=False, encoding="utf-8", quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    csv_text_out = output.getvalue().lstrip("\ufeff")

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
    }


async def run_dedup_pipeline(csv_text: str, tool_context: ToolContext) -> dict:
    """
    Validates leads, normalizes fields from the reference taxonomy,
    then deduplicates by exact email and by same-person name plus
    domain or company. Keeps one surviving row per group.
    Returns counts only; the write-back CSV is saved as deduped.csv.
    """
    result = process_csv(csv_text)
    if result["status"] != "ok":
        return result

    tool_context.state["leads_in"] = result["leads_in"]
    tool_context.state["leads_out"] = result["leads_out"]

    payload = {k: result[k] for k in (
        "status", "leads_in", "validation_issues", "values_normalized",
        "duplicates_merged", "leads_out", "blank_email_kept", "hitl_records",
        "file", "audit_file",
    )}

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
