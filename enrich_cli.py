"""
enrich_cli.py
Run Lusha enrichment on a hygiene write-back. No LLM required.

Usage: python enrich_cli.py [deduped.csv] [enriched.csv]

Reads deduped.csv by default. Writes enriched.csv and, when there are
flags, enrichment_review.csv. Needs LUSHA_API_KEY. If dedup_log.csv sits
next to the input, email_status is joined so undeliverable addresses can
be replaced.
"""

import csv
import io
import os
import sys
from pathlib import Path

from pipeline.tools import ENRICH_REVIEW_FILE, enrich_csv


def main():
    if not (os.environ.get("LUSHA_API_KEY") or os.environ.get("LUSHA_APIKEY") or "").strip():
        print("Cannot enrich: set LUSHA_API_KEY.")
        sys.exit(1)

    args = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    src = Path(args[0]) if args else Path("deduped.csv")
    dest = Path(args[1]) if len(args) > 1 else Path("enriched.csv")
    if not src.exists():
        print("Cannot enrich: deduped.csv was not found. Run hygiene first.")
        sys.exit(1)

    audit_rows = []
    log_path = src.with_name("dedup_log.csv")
    if log_path.exists():
        audit_rows = list(csv.DictReader(io.StringIO(log_path.read_text(encoding="utf-8"))))

    result = enrich_csv(src.read_text(encoding="utf-8"), audit_rows)
    if result["status"] != "ok":
        print(result["message"])
        sys.exit(1)

    dest.write_text(result["csv"], encoding="utf-8")
    print(f"{dest} is ready.")
    print(
        f"Attempted {result['rows_attempted']}, matched {result['rows_matched']}, "
        f"filled {result['fields_filled']} fields, "
        f"{result['credits_charged']} Lusha credits."
    )
    if result.get("review_csv"):
        review_path = dest.with_name(ENRICH_REVIEW_FILE)
        review_path.write_text(result["review_csv"], encoding="utf-8")
        print(f"{review_path}: {len(result.get('enrichment_review_lines') or [])} row(s) to review.")


if __name__ == "__main__":
    main()
