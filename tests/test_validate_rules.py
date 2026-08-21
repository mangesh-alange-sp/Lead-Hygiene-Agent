"""Validation flags for missing contact data and obvious test rows."""

import unittest

import pandas as pd

from pipeline.tools import process_csv
from pipeline.validate import is_test_record, validate_dataframe


class TestDataRules(unittest.TestCase):
    def test_test_arp_is_detected(self):
        self.assertTrue(is_test_record("Test", "Arp", "Test Arp", "abcd@test.com"))
        self.assertTrue(is_test_record("Test", "Idsec", "", "foo@test.com"))
        self.assertFalse(is_test_record("Jane", "Doe", "Amazon.com Inc.", "jane@amazon.com"))

    def test_test_arp_is_dropped_from_writeback(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company\n"
            "00Q1,Jane,Doe,jane@amazon.com,2065550100,Amazon\n"
            "00Q2,Test,Arp,abcd@test.com,,Test Arp\n"
        )
        result = process_csv(csv_in)
        self.assertEqual(result["status"], "ok", result.get("message"))
        self.assertEqual(result["records_dropped"], 1)
        self.assertNotIn("00Q2", result["csv"])
        self.assertIn("00Q1", result["csv"])
        self.assertIn("dropped_test_data", result["audit_csv"])


class MissingContactRules(unittest.TestCase):
    def test_missing_email_is_held_for_review(self):
        df = pd.DataFrame({
            "Id": ["00Q1"], "FirstName": ["Jane"], "LastName": ["Doe"],
            "Email": [""], "Phone": ["2065550100"], "Company": ["Amazon"],
        })
        out, _ = validate_dataframe(df)
        self.assertIn("missing_email", out.at[0, "data_quality_flags"])
        self.assertEqual(out.at[0, "hitl_review"], "Yes")

    def test_missing_phone_is_held_for_review(self):
        df = pd.DataFrame({
            "Id": ["00Q1"], "FirstName": ["Jane"], "LastName": ["Doe"],
            "Email": ["jane@amazon.com"], "Phone": [""], "Company": ["Amazon"],
        })
        out, _ = validate_dataframe(df)
        self.assertIn("missing_phone", out.at[0, "data_quality_flags"])
        self.assertEqual(out.at[0, "hitl_review"], "Yes")


if __name__ == "__main__":
    unittest.main()
