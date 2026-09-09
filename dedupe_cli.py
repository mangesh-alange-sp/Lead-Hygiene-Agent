"""
dedupe_cli.py
CLI runner for local non-LLM execution.
Usage: python dedupe_cli.py input.csv [output.csv] [--accept-changes]

--accept-changes clears the regression gate after the reported field diffs have
been reviewed; without it, any change to a known Id blocks the write.
"""

import sys
from pathlib import Path
from pipeline.tools import format_run_summary, process_csv


def main():
    args = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    accept_changes = "--accept-changes" in sys.argv[1:]
    if not args:
        print("Usage: python dedupe_cli.py <input.csv> [output.csv] [--accept-changes]")
        sys.exit(1)

    input_path = Path(args[0])
    if not input_path.exists():
        print(f"Error: File {input_path} not found.")
        sys.exit(1)

    dest = Path(args[1]) if len(args) > 1 else Path("deduped.csv")
    result = process_csv(
        input_path.read_text(encoding="utf-8"), accept_changes=accept_changes
    )
    if result["status"] != "ok":
        print(result["message"])
        for violation in result.get("invariant_violations", []):
            print(f"  invariant: {violation}")
        sys.exit(1)

    dest.write_text(result["csv"], encoding="utf-8")
    log_path = dest.with_name("dedup_log.csv")
    if result.get("audit_csv"):
        log_path.write_text(result["audit_csv"], encoding="utf-8")
    if result.get("excel_csv"):
        dest.with_name(result["excel_file"]).write_text(
            result["excel_csv"], encoding="utf-8"
        )
    if result.get("review_csv"):
        dest.with_name(result["review_file"]).write_text(
            result["review_csv"], encoding="utf-8"
        )
    if dest.name != "deduped.csv":
        print(f"{dest} is ready.")
    print(format_run_summary(result["summary"]))


if __name__ == "__main__":
    main()
