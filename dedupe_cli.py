"""
dedupe_cli.py
CLI runner for local non-LLM execution.
Usage: python dedupe_cli.py input.csv [output.csv]
"""

import sys
from pathlib import Path
from pipeline.tools import process_csv


def main():
    if len(sys.argv) < 2:
        print("Usage: python dedupe_cli.py <input.csv> [output.csv]")
        sys.exit(1)

    input_path = Path(sys.argv[1])
    if not input_path.exists():
        print(f"Error: File {input_path} not found.")
        sys.exit(1)

    dest = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("deduped.csv")
    result = process_csv(input_path.read_text(encoding="utf-8"))
    if result["status"] != "ok":
        print(result["message"])
        sys.exit(1)

    dest.write_text(result["csv"], encoding="utf-8")
    print("deduped.csv is ready." if dest.name == "deduped.csv" else f"{dest} is ready.")
    print(f"Leads in: {result['leads_in']}")
    print(f"Validation issues: {result['validation_issues']}")
    print(f"Values normalized: {result['values_normalized']}")
    print(f"Duplicates merged: {result['duplicates_merged']}")
    print(f"Leads to write back: {result['leads_out']}")
    if result["blank_email_kept"]:
        print(f"Leads with no email (kept, not merged): {result['blank_email_kept']}")


if __name__ == "__main__":
    main()
