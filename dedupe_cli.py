"""
dedupe_cli.py
CLI runner for local non-LLM execution.

  python dedupe_cli.py input.csv [output.csv] [--accept-changes]
  python dedupe_cli.py --source salesforce [output.csv] [--accept-changes]

csv (default) reads a Salesforce-shaped export, including the synthetic fixture.
salesforce queries Lead when SALESFORCE_INSTANCE_URL and SALESFORCE_ACCESS_TOKEN
are set. Hygiene is the same job either way.
"""

import sys
from pathlib import Path

from pipeline.lead_io import (
    SOURCE_CSV,
    SOURCE_SALESFORCE,
    LeadStoreError,
    load_leads,
    save_leads,
)
from pipeline.salesforce_io import SalesforceApiError, SalesforceNotConnected
from pipeline.tools import format_run_summary, process_csv


def parse_args(argv):
    source = SOURCE_CSV
    accept_changes = False
    positional = []
    expecting_source = False
    for arg in argv:
        if expecting_source:
            source = arg
            expecting_source = False
            continue
        if arg == "--source":
            expecting_source = True
            continue
        if arg.startswith("--source="):
            source = arg.split("=", 1)[1]
            continue
        if arg == "--accept-changes":
            accept_changes = True
            continue
        if arg.startswith("--"):
            print(f"Unknown flag: {arg}")
            sys.exit(1)
        positional.append(arg)
    if expecting_source:
        print("Usage: --source csv|salesforce")
        sys.exit(1)
    return {
        "source": source,
        "positional": positional,
        "accept_changes": accept_changes,
    }


def main():
    parsed = parse_args(sys.argv[1:])
    source = parsed["source"]
    positional = parsed["positional"]

    if source == SOURCE_CSV and not positional:
        print("Usage: python dedupe_cli.py <input.csv> [output.csv] [--accept-changes]")
        print("       python dedupe_cli.py --source salesforce [output.csv] [--accept-changes]")
        sys.exit(1)

    input_path = positional[0] if source == SOURCE_CSV else None
    dest_index = 1 if source == SOURCE_CSV else 0
    dest = Path(positional[dest_index]) if len(positional) > dest_index else Path("deduped.csv")

    try:
        csv_text = load_leads(source, path=input_path)
    except (LeadStoreError, SalesforceNotConnected, SalesforceApiError) as exc:
        print(exc)
        sys.exit(1)

    result = process_csv(csv_text, accept_changes=parsed["accept_changes"])
    if result["status"] != "ok":
        print(result["message"])
        for violation in result.get("invariant_violations", []):
            print(f"  invariant: {violation}")
        sys.exit(1)

    try:
        save_leads(source, result, dest=dest)
    except (LeadStoreError, SalesforceNotConnected, SalesforceApiError) as exc:
        print(exc)
        sys.exit(1)

    if dest.name != "deduped.csv":
        print(f"{dest} is ready.")
    print(format_run_summary(result["summary"]))


if __name__ == "__main__":
    main()
