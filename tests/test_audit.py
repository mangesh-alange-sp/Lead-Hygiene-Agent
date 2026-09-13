"""Post-run audit of write-back counts, emails, leftover merges, and missing Ids."""

import tempfile
import unittest
from pathlib import Path

from pipeline.audit import audit_run, format_audit
from pipeline.tools import process_csv


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class AuditHelperTests(unittest.TestCase):
    def test_clean_run_counts_add_up(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company,Country\n"
            "ZZ1,Jane,Doe,jane@acme.com,2065550100,Acme,US\n"
            "ZZ2,Mark,Lopez,mark@acme.com,2065550101,Acme,US\n"
        )
        result = process_csv(csv_in, check_regression=False)
        self.assertEqual(result["status"], "ok", result.get("message"))
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            out = _write(folder / "deduped.csv", result["csv"])
            source = _write(folder / "source.csv", csv_in)
            log = _write(folder / "dedup_log.csv", result["audit_csv"])
            report = audit_run(out, source_csv=source, log_csv=log)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["leads_in"], 2)
        self.assertEqual(report["leads_out"], 2)
        self.assertEqual(report["duplicate_emails"], [])
        self.assertEqual(report["leftover_auto_merges"], [])
        self.assertEqual(report["missing_ids"], [])
        self.assertIn("Audit: ok", format_audit(report))

    def test_merged_id_is_explained(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company,Country\n"
            "ZZ1,Jane,Doe,jane@amazon.com,2065550100,Amazon.com Inc.,US\n"
            "ZZ2,Jane,Doe,jane@amazon.com,2065550100,Amazon Inc,US\n"
        )
        result = process_csv(csv_in, check_regression=False)
        self.assertEqual(result["status"], "ok", result.get("message"))
        self.assertEqual(result["duplicates_merged"], 1)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            report = audit_run(
                _write(folder / "deduped.csv", result["csv"]),
                source_csv=_write(folder / "source.csv", csv_in),
                log_csv=_write(folder / "dedup_log.csv", result["audit_csv"]),
            )
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(len(report["missing_ids"]), 1)
        self.assertEqual(report["missing_unexplained"], [])
        self.assertTrue(any("auto_merge" in why for why in report["missing_explained"].values()))

    def test_leftover_auto_merge_is_a_problem(self):
        text = (
            "Id,FirstName,LastName,Email,Phone,Company\n"
            "ZZ1,Jane,Doe,jane@amazon.com,2065550100,Amazon.com Inc.\n"
            "ZZ2,Jane,Doe,jane@amazon.com,2065550100,Amazon Inc\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = _write(Path(tmp) / "deduped.csv", text)
            report = audit_run(out)
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["leftover_auto_merges"]), 1)
        self.assertEqual(len(report["duplicate_emails"]), 1)

    def test_dropped_test_row_is_explained(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company,Country\n"
            "ZZ1,Jane,Doe,jane@acme.com,2065550100,Acme,US\n"
            "ZZ2,Test,User,abcd@test.com,,Test Arp,US\n"
        )
        result = process_csv(csv_in, check_regression=False)
        self.assertEqual(result["status"], "ok", result.get("message"))
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            report = audit_run(
                _write(folder / "deduped.csv", result["csv"]),
                source_csv=_write(folder / "source.csv", csv_in),
                log_csv=_write(folder / "dedup_log.csv", result["audit_csv"]),
            )
        self.assertTrue(report["ok"], report["problems"])
        self.assertIn("ZZ2", report["missing_explained"])
        self.assertEqual(report["missing_explained"]["ZZ2"], "dropped_test_data")
