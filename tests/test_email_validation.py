"""
Email validation via email-validator plus a real MX deliverability check.

The suite answers deliverability from data/mx_cache.json (see tests/__init__.py)
so results are deterministic and do not depend on DNS.
"""

import csv
import io
import os
import unittest
from unittest.mock import patch

import pandas as pd

from pipeline import emailcheck
from pipeline.emailcheck import (
    DELIVERABLE,
    INVALID_SYNTAX,
    UNDELIVERABLE,
    UNKNOWN,
    is_reserved_domain,
    validate_lead_email,
)
from pipeline.tools import process_csv
from pipeline.validate import validate_dataframe


class SyntaxRules(unittest.TestCase):
    def test_malformed_addresses_are_cleared(self):
        for raw in ("not-an-email", "a@@b.com", "pat@", "@acme.com", "pat acme.com"):
            with self.subTest(raw=raw):
                result = validate_lead_email(raw)
                self.assertEqual(result["status"], INVALID_SYNTAX)
                self.assertEqual(result["value"], "")

    def test_valid_address_is_normalized_lowercase(self):
        result = validate_lead_email("Jane@Amazon.com")
        self.assertEqual(result["status"], DELIVERABLE)
        self.assertEqual(result["value"], "jane@amazon.com")

    def test_raw_is_always_returned(self):
        self.assertEqual(validate_lead_email(" Jane@Amazon.com ")["raw"], "Jane@Amazon.com")


class DeliverabilityRules(unittest.TestCase):
    def test_placeholder_domains_are_still_cleared(self):
        for raw in ("willy@wonka.com", "potter@stinks.com"):
            with self.subTest(raw=raw):
                result = validate_lead_email(raw)
                self.assertEqual(result["status"], UNDELIVERABLE)
                self.assertEqual(result["value"], "")
                self.assertIn("placeholder", result["reason"])

    def test_mx_miss_keeps_a_syntactically_valid_address(self):
        # sprinklr.com.sg is cached as having no MX, and is not a placeholder.
        result = validate_lead_email("pat@sprinklr.com.sg")
        self.assertEqual(result["status"], UNDELIVERABLE)
        self.assertEqual(result["value"], "pat@sprinklr.com.sg")
        self.assertIn("kept", result["reason"])
        self.assertEqual(result["raw"], "pat@sprinklr.com.sg")

    def test_real_corporate_domains_pass(self):
        for raw in ("jane@amazon.com", "mehmet.arikan@sap.com", "gokul.s@yahoo.in"):
            with self.subTest(raw=raw):
                self.assertEqual(validate_lead_email(raw)["status"], DELIVERABLE)

    def test_rfc_reserved_domains_are_settled_without_a_lookup(self):
        for domain in ("example.com", "example.com.tw", "foo.test", "bar.invalid"):
            with self.subTest(domain=domain):
                self.assertTrue(is_reserved_domain(domain))
        self.assertEqual(validate_lead_email("kt.lin@example.com.tw")["status"], UNDELIVERABLE)

    def test_unknown_domain_is_kept_not_nulled(self):
        # Cache miss with lookups disabled: we cannot prove undeliverability.
        result = validate_lead_email("someone@nonexistent-domain-not-in-cache.com")
        self.assertEqual(result["status"], UNKNOWN)
        self.assertEqual(result["value"], "someone@nonexistent-domain-not-in-cache.com")

    def test_resolver_failure_never_nulls_an_address(self):
        def boom(domain):
            raise LookupError("resolver down")

        original = emailcheck.domain_accepts_mail
        emailcheck.domain_accepts_mail = boom
        try:
            result = validate_lead_email("jane@amazon.com")
        finally:
            emailcheck.domain_accepts_mail = original
        self.assertEqual(result["status"], UNKNOWN)
        self.assertEqual(result["value"], "jane@amazon.com")

    def test_mx_lookups_are_off_unless_opted_in(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LEAD_HYGIENE_MX_LOOKUP", None)
            self.assertFalse(emailcheck._lookups_enabled())
        with patch.dict(os.environ, {"LEAD_HYGIENE_MX_LOOKUP": "1"}):
            self.assertTrue(emailcheck._lookups_enabled())

    def test_syntax_and_placeholders_still_clear_when_mx_is_off(self):
        self.assertEqual(validate_lead_email("not-an-email")["value"], "")
        self.assertEqual(validate_lead_email("pat@example.com")["value"], "")


class LeadRecordRules(unittest.TestCase):
    def test_placeholder_email_is_nulled_but_the_lead_survives(self):
        df = pd.DataFrame({
            "Id": ["00Q1"], "FirstName": ["Willy"], "LastName": ["Wonka"],
            "Email": ["willy@wonka.com"], "Phone": ["3125550199"], "Company": ["Bfg"],
        })
        out, _ = validate_dataframe(df)
        self.assertEqual(out.at[0, "Email"], "")
        self.assertEqual(out.at[0, "Email_raw"], "willy@wonka.com")
        self.assertEqual(out.at[0, "email_status"], UNDELIVERABLE)
        self.assertIn("undeliverable_email", out.at[0, "data_quality_flags"])
        self.assertEqual(out.at[0, "FirstName"], "Willy")

    def test_mx_miss_keeps_the_email_on_the_row(self):
        df = pd.DataFrame({
            "Id": ["00Q1"], "FirstName": ["Pat"], "LastName": ["Lee"],
            "Email": ["Pat@Sprinklr.com.sg"], "Phone": ["4155550100"],
            "Company": ["Sprinklr"],
        })
        out, _ = validate_dataframe(df)
        self.assertEqual(out.at[0, "Email"], "pat@sprinklr.com.sg")
        self.assertEqual(out.at[0, "Email_raw"], "Pat@Sprinklr.com.sg")
        self.assertEqual(out.at[0, "email_status"], UNDELIVERABLE)
        self.assertIn("undeliverable_email", out.at[0, "data_quality_flags"])
        self.assertEqual(out.at[0, "hitl_review"], "Yes")

    def test_end_to_end_keeps_the_mx_miss_and_logs_the_reason(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company,Country\n"
            "00Q1,Pat,Lee,pat@sprinklr.com.sg,4155550100,Sprinklr,US\n"
        )
        result = process_csv(csv_in)
        self.assertEqual(result["status"], "ok", result.get("message"))
        row = next(csv.DictReader(io.StringIO(result["csv"])))
        self.assertEqual(row["Email"], "pat@sprinklr.com.sg")
        audit = {r["surviving_lead_id"]: r for r in csv.DictReader(io.StringIO(result["audit_csv"]))}
        self.assertEqual(audit["00Q1"]["email_status"], UNDELIVERABLE)
        self.assertEqual(audit["00Q1"]["email_raw"], "pat@sprinklr.com.sg")
        reasons = " ".join(entry["reasons"] for entry in result["summary"]["technical_log"])
        self.assertIn("email:", reasons)
        self.assertIn("kept", reasons)

    def test_a_bad_email_alone_never_drops_the_record(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company\n"
            "00Q1,Pat,Lee,not-an-email,4155550100,Acme\n"
        )
        result = process_csv(csv_in)
        self.assertEqual(result["status"], "ok", result.get("message"))
        self.assertIn("00Q1", result["csv"])
        self.assertEqual(result["records_dropped"], 0)


if __name__ == "__main__":
    unittest.main()
