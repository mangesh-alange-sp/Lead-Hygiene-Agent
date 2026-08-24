"""
tools.py
ADK tool wrappers: hygiene pipeline and Lusha search-and-enrich.
"""

import io
import pandas as pd
from google.adk.tools import ToolContext
from google.genai import types

from .dedupe import deduplicate_dataframe
from .enrich import enrich_dataframe
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


def _part_to_text(part) -> str:
    if part is None:
        return ""
    inline = getattr(part, "inline_data", None) or getattr(part, "inlineData", None)
    if inline is not None:
        data = getattr(inline, "data", b"")
        if isinstance(data, bytes):
            return data.decode("utf-8")
        return str(data)
    text = getattr(part, "text", None)
    return text or ""


async def _load_csv_text(csv_text: str, tool_context: ToolContext) -> dict:
    text = (csv_text or "").strip()
    if text:
        return {"status": "ok", "csv_text": csv_text}

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
            "message": "No CSV text was provided and deduped.csv was not found. "
            "Pass csv_text or run run_dedup_pipeline first.",
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


async def search_and_enrich(csv_text: str, tool_context: ToolContext) -> dict:
    """
    Enrich missing lead CSV fields with Lusha Search and Enrich.

    Uses POST https://api.lusha.com/v3/contacts/search-and-enrich (up to 100
    contacts per request). Looks up each row by email, or by firstName +
    lastName + companyName/companyDomain. Reveals emails and phones only when
    those CSV cells are blank. Fills blank FirstName, LastName, Email, Company,
    Title, Phone, Industry, and Website. Firmographics missing after the
    contact call (AnnualRevenue, NumberOfEmployees, Industry, Website) are
    filled from company search-and-enrich. Existing values are never overwritten.
    LeadSource, Status, CreatedDate, and OwnerId are left unchanged.

    Pass the CSV text, or an empty string to enrich the saved deduped.csv
    artifact. Returns counts only; the write-back file is saved as enriched.csv.
    """
    loaded = await _load_csv_text(csv_text, tool_context)
    if loaded["status"] != "ok":
        return loaded

    parsed = _parse_lead_csv(loaded["csv_text"])
    if parsed["status"] != "ok":
        return parsed

    df = parsed["df"].fillna("")
    leads_in = len(df)
    df_enriched, stats = enrich_dataframe(df)
    if stats.get("error"):
        return {"status": "error", "message": stats["error"]}

    df_enriched = df_enriched.fillna("")
    output = io.StringIO()
    df_enriched.to_csv(output, index=False)

    payload = {
        "status": "ok",
        "leads_in": leads_in,
        "rows_attempted": stats["rows_attempted"],
        "rows_matched": stats["rows_matched"],
        "rows_not_found": stats["rows_not_found"],
        "rows_skipped": stats["rows_skipped"],
        "fields_filled": stats["fields_filled"],
        "credits_charged": stats["credits_charged"],
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
        return payload
    except Exception:
        return {
            "status": "error",
            "message": "Enrichment finished but the file could not be saved. Re-run the job.",
        }
