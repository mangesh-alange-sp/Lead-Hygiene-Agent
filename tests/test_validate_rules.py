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

    def test_four_test_rows_are_not_collapsed_into_one(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company\n"
            "T1,Test,User,a@test.com,,Test Co\n"
            "T2,Test,User,b@test.com,,Test Arp\n"
            "T3,Dummy,Person,c@test.com,,Dummy Company\n"
            "T4,Fake,Name,d@test.org,,Sample Company\n"
            "R1,Jane,Doe,jane@amazon.com,2065550100,Amazon\n"
        )
        result = process_csv(csv_in)
        self.assertEqual(result["status"], "ok", result.get("message"))
        self.assertEqual(result["duplicates_merged"], 0)
        self.assertEqual(result["records_dropped"], 4)
        self.assertIn("R1", result["csv"])
        for lead_id in ("T1", "T2", "T3", "T4"):
            self.assertNotIn(lead_id, result["csv"])
        self.assertEqual(result["audit_csv"].count("dropped_test_data"), 4)


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


class AdditiveValidationRules(unittest.TestCase):
    def test_test_idsec_without_contact_is_dropped(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company\n"
            "00Q1,Jane,Doe,jane@amazon.com,2065550100,Amazon\n"
            "00Q2,Test/Idsec,,,,\n"
        )
        result = process_csv(csv_in)
        self.assertEqual(result["status"], "ok", result.get("message"))
        self.assertNotIn("00Q2", result["csv"])
        self.assertGreaterEqual(result["records_dropped"], 1)
        df = pd.DataFrame({
            "Id": ["00Q2"], "FirstName": ["Test/Idsec"], "LastName": [""],
            "Email": [""], "Phone": [""], "Company": [""],
        })
        out, _ = validate_dataframe(df)
        self.assertIn("junk_lead", out.at[0, "data_quality_flags"])
        self.assertEqual(out.at[0, "hitl_review"], "Yes")

    def test_website_that_is_an_email_is_flagged_and_not_rejected(self):
        df = pd.DataFrame({
            "Id": ["00Q1"], "FirstName": ["Pat"], "LastName": ["Lee"],
            "Email": ["pat@acme.com"], "Phone": ["4155550100"],
            "Company": ["Acme"], "Website": ["pat@acme.com"],
        })
        out, _ = validate_dataframe(df)
        self.assertIn("website_is_email", out.at[0, "data_quality_flags"])
        self.assertEqual(out.at[0, "Website"], "pat@acme.com")
        self.assertEqual(out.at[0, "hitl_review"], "Yes")

    def test_incomplete_profile_is_non_blocking(self):
        df = pd.DataFrame({
            "Id": ["00Q1"], "FirstName": ["Pat"], "LastName": ["Lee"],
            "Email": ["pat@acme.com"], "Phone": ["4155550100"],
            "Company": ["Acme"], "Title": [""], "Industry": [""], "AnnualRevenue": [""],
        })
        out, _ = validate_dataframe(df)
        self.assertIn("incomplete_profile", out.at[0, "data_quality_flags"])
        self.assertIn("completeness_flag", out.at[0, "data_quality_flags"])
        self.assertNotEqual(out.at[0, "hitl_review"], "Yes")

    def test_short_phone_is_flagged_for_country_review(self):
        df = pd.DataFrame({
            "Id": ["00Q1"], "FirstName": ["Pat"], "LastName": ["Lee"],
            "Email": ["pat@acme.com"], "Phone": ["12345"], "Company": ["Acme"],
        })
        out, _ = validate_dataframe(df)
        self.assertIn("needs_country_code_review", out.at[0, "data_quality_flags"])


if __name__ == "__main__":
    unittest.main()
