"""
dedupe_cli.py
CLI runner for local non-LLM execution.
Usage: python dedupe_cli.py input.csv [output.csv]
"""

import sys
from pathlib import Path
from pipeline.tools import format_run_summary, process_csv


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
        for violation in result.get("invariant_violations", []):
            print(f"  invariant: {violation}")
        sys.exit(1)

    dest.write_text(result["csv"], encoding="utf-8")
    log_path = dest.with_name("dedup_log.csv")
    if result.get("audit_csv"):
        log_path.write_text(result["audit_csv"], encoding="utf-8")
    if dest.name != "deduped.csv":
        print(f"{dest} is ready.")
    print(format_run_summary(result["summary"]))


if __name__ == "__main__":
    main()
