"""CSV now, Salesforce later: same CSV shape either way."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dedupe_cli import parse_args
from pipeline.lead_io import SOURCE_CSV, SOURCE_SALESFORCE, LeadStoreError, load_leads, save_leads
from pipeline.salesforce_io import (
    SalesforceNotConnected,
    csv_to_update_records,
    is_connected,
    load_leads_csv,
    records_to_csv,
    save_leads_csv,
)

FIXTURE = Path(__file__).parent / "fixtures" / "lead_data_with_duplicates.csv"


class CliSourceTests(unittest.TestCase):
    def test_default_source_is_csv(self):
        parsed = parse_args(["leads.csv", "out.csv"])
        self.assertEqual(parsed["source"], SOURCE_CSV)
        self.assertEqual(parsed["positional"], ["leads.csv", "out.csv"])
        self.assertFalse(parsed["accept_changes"])

    def test_salesforce_source_does_not_need_an_input_file(self):
        parsed = parse_args(["--source", "salesforce", "--accept-changes"])
        self.assertEqual(parsed["source"], SOURCE_SALESFORCE)
        self.assertEqual(parsed["positional"], [])
        self.assertTrue(parsed["accept_changes"])


class CsvStoreTests(unittest.TestCase):
    def test_load_reads_utf8_and_strips_a_bom(self):
        text = load_leads(SOURCE_CSV, path=FIXTURE)
        self.assertTrue(text.startswith("Id,") or text.startswith('"Id"') or "Id" in text.splitlines()[0])
        self.assertFalse(text.startswith("\ufeff"))

    def test_load_rejects_a_missing_file(self):
        with self.assertRaises(LeadStoreError):
            load_leads(SOURCE_CSV, path="/no/such/leads.csv")

    def test_save_writes_the_salesforce_file_and_the_review_copy(self):
        tmp = Path(tempfile.mkdtemp())
        dest = tmp / "deduped.csv"
        result = {
            "status": "ok",
            "csv": "Id,Phone\n00Q1,+12065550100\n",
            "audit_csv": "surviving_lead_id\n00Q1\n",
            "audit_file": "dedup_log.csv",
            "excel_csv": "Id,Phone\n00Q1,=\"+12065550100\"\n",
            "excel_file": "deduped_excel_review.csv",
        }
        save_leads(SOURCE_CSV, result, dest=dest)
        self.assertEqual(dest.read_text(encoding="utf-8"), result["csv"])
        self.assertIn("+12065550100", dest.read_text(encoding="utf-8"))
        review = dest.with_name("deduped_excel_review.csv").read_text(encoding="utf-8")
        self.assertIn("+12065550100", review)


class SalesforceSeamTests(unittest.TestCase):
    def test_not_connected_without_env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("SALESFORCE_")}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(is_connected())
            with self.assertRaises(SalesforceNotConnected):
                load_leads_csv()

    def test_query_records_become_lead_csv(self):
        records = [
            {"Id": "00Q1", "FirstName": "Carsten", "LastName": "Büchert", "Phone": "+491724597285"},
        ]
        csv_text = records_to_csv(records, ["Id", "FirstName", "LastName", "Phone"])
        self.assertIn("Büchert", csv_text)
        self.assertIn("+491724597285", csv_text)
        self.assertTrue(csv_text.startswith("Id,FirstName,LastName,Phone"))

    def test_writeback_keeps_plus_phones_as_strings(self):
        csv_text = "Id,Phone,CreatedDate\n00Q1,+491724597285,2026-01-01\n"
        records = csv_to_update_records(csv_text)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["Id"], "00Q1")
        self.assertEqual(records[0]["Phone"], "+491724597285")
        self.assertNotIn("CreatedDate", records[0])

    def test_save_uses_composite_patch_when_connected(self):
        captured = {}

        def fake_request(method, url, payload=None):
            captured["method"] = method
            captured["url"] = url
            captured["payload"] = payload
            return {}

        env = {
            "SALESFORCE_INSTANCE_URL": "https://example.my.salesforce.com",
            "SALESFORCE_ACCESS_TOKEN": "token",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch("pipeline.salesforce_io.salesforce_request", side_effect=fake_request):
                save_leads_csv("Id,Phone\n00Q1,+12065550100\n")
        self.assertEqual(captured["method"], "PATCH")
        self.assertIn("composite/sobjects", captured["url"])
        self.assertEqual(captured["payload"]["records"][0]["Phone"], "+12065550100")
