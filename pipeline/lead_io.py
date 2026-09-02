"""
lead_io.py
Where leads enter and leave the hygiene job.

Today the store is a CSV file (synthetic fixture or a Salesforce export).
Later the same load/save functions talk to Salesforce. process_csv never
changes: it always receives Lead-shaped CSV text and returns Lead-shaped CSV text.
"""

from pathlib import Path

from . import salesforce_io

SOURCE_CSV = "csv"
SOURCE_SALESFORCE = "salesforce"
SOURCES = (SOURCE_CSV, SOURCE_SALESFORCE)


class LeadStoreError(Exception):
    """The store could not load or save leads."""


def load_leads(source: str, path=None) -> str:
    """
    Return Lead CSV text for process_csv.

    csv        — read a file (UTF-8, BOM optional so Excel/SF exports work)
    salesforce — query Lead when credentials are set
    """
    if source == SOURCE_CSV:
        if not path:
            raise LeadStoreError("A CSV path is required when source is csv.")
        file_path = Path(path)
        if not file_path.exists():
            raise LeadStoreError(f"File {file_path} not found.")
        return file_path.read_text(encoding="utf-8-sig")
    if source == SOURCE_SALESFORCE:
        return salesforce_io.load_leads_csv()
    raise LeadStoreError(f"Unknown source {source!r}. Use csv or salesforce.")


def save_leads(source: str, result: dict, dest=None) -> None:
    """
    Write a successful process_csv result.

    csv        — deduped.csv, dedup_log.csv, excel review file next to dest
    salesforce — update Lead by Id, then still write the local files for audit
    """
    if result.get("status") != "ok":
        raise LeadStoreError(result.get("message") or "Pipeline did not succeed.")

    if source == SOURCE_SALESFORCE:
        salesforce_io.save_leads_csv(result["csv"])

    if dest is None:
        dest = Path("deduped.csv")
    dest = Path(dest)
    dest.write_text(result["csv"], encoding="utf-8")
    if result.get("audit_csv"):
        dest.with_name(result.get("audit_file") or "dedup_log.csv").write_text(
            result["audit_csv"], encoding="utf-8"
        )
    if result.get("excel_csv"):
        dest.with_name(result.get("excel_file") or "deduped_excel_review.csv").write_text(
            result["excel_csv"], encoding="utf-8"
        )
