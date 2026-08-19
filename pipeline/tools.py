"""
tools.py
ADK tool wrapper: parse → validate → normalize → dedupe → artifact.
"""

import io
import pandas as pd
from google.adk.tools import ToolContext
from google.genai import types

from .dedupe import deduplicate_dataframe
from .normalize import normalize_dataframe
from .validate import validate_dataframe

MAX_ROWS = 5000
MAX_CSV_CHARS = 2_000_000


def _clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    renamed = {}
    for col in df.columns:
        name = str(col).strip()
        if name.lower() in {"_", "unnamed: 0"} or name.lower().startswith("unnamed:"):
            renamed[col] = "RecordType"
    if renamed:
        df = df.rename(columns=renamed)
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
    df_deduped, merged_count, blank_email_count = deduplicate_dataframe(df_norm)
    df_deduped = df_deduped.fillna("")

    output = io.StringIO()
    df_deduped.to_csv(output, index=False)
    return {
        "status": "ok",
        "csv": output.getvalue(),
        "leads_in": leads_in,
        "validation_issues": issue_count,
        "values_normalized": norm_count,
        "duplicates_merged": merged_count,
        "leads_out": len(df_deduped),
        "blank_email_kept": blank_email_count,
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
        "duplicates_merged", "leads_out", "blank_email_kept", "file",
    )}

    try:
        csv_part = types.Part.from_bytes(
            data=result["csv"].encode("utf-8"), mime_type="text/csv"
        )
        version = await tool_context.save_artifact(
            filename="deduped.csv", artifact=csv_part
        )
        payload["artifact_version"] = version
        return payload
    except Exception:
        return {
            "status": "error",
            "message": "Dedup finished but the file could not be saved. Re-run the job.",
        }
