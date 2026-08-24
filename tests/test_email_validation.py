"""
Email validation via email-validator plus a real MX deliverability check.

The suite answers deliverability from data/mx_cache.json (see tests/__init__.py)
so results are deterministic and do not depend on DNS.
"""

import unittest

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
    def test_domains_with_no_mail_exchanger_are_cleared(self):
        for raw in ("willy@wonka.com", "potter@stinks.com"):
            with self.subTest(raw=raw):
                result = validate_lead_email(raw)
                self.assertEqual(result["status"], UNDELIVERABLE)
                self.assertEqual(result["value"], "")
                self.assertIn("MX", result["reason"])

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


class LeadRecordRules(unittest.TestCase):
    def test_undeliverable_email_is_nulled_but_the_lead_survives(self):
        df = pd.DataFrame({
            "Id": ["00Q1"], "FirstName": ["Willy"], "LastName": ["Wonka"],
            "Email": ["willy@wonka.com"], "Phone": ["3125550199"], "Company": ["Bfg"],
        })
        out, _ = validate_dataframe(df)
        self.assertEqual(out.at[0, "Email"], "")
        self.assertEqual(out.at[0, "email_status"], UNDELIVERABLE)
        self.assertIn("undeliverable_email", out.at[0, "data_quality_flags"])
        self.assertEqual(out.at[0, "FirstName"], "Willy")

    def test_end_to_end_keeps_the_lead_and_logs_the_reason(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company,Country\n"
            "00Q1,Willy,Wonka,willy@wonka.com,3125550199,Bfg,US\n"
        )
        result = process_csv(csv_in)
        self.assertEqual(result["status"], "ok", result.get("message"))
        self.assertIn("00Q1", result["csv"])
        reasons = " ".join(row["reasons"] for row in result["summary"]["technical_log"])
        self.assertIn("email:", reasons)

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
