"""Company-aligned Lusha gates: email/website trust, no phone swap, review."""

import unittest

import pandas as pd

from pipeline import enrich
from pipeline.emailcheck import UNDELIVERABLE


COLUMNS = [
    "Id", "FirstName", "LastName", "Email", "Phone", "Company",
    "Website", "Industry", "AnnualRevenue", "NumberOfEmployees",
    "email_status", "Email_raw",
]


def _frame(rows, columns=None):
    return pd.DataFrame(rows, columns=columns or COLUMNS).fillna("")


class GateApi:
    def __init__(self, email="found@stackable.tech", phone="+15550009999",
                 domain="stackable.tech"):
        self.calls = []
        self.email = email
        self.phone = phone
        self.domain = domain

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        results = []
        for item in payload.get("contacts") or []:
            results.append({
                "clientReferenceId": item["clientReferenceId"],
                "emails": [{"email": self.email, "type": "work"}],
                "phones": [{"number": self.phone, "type": "mobile"}],
                "company": {"name": "Stackable", "domain": self.domain},
            })
        for item in payload.get("companies") or []:
            results.append({
                "clientReferenceId": item["clientReferenceId"],
                "name": "Stackable",
                "domain": self.domain,
            })
        return {"status": "ok", "results": results, "billing": {"creditsCharged": 1}}

    def contact_payloads(self):
        return [payload for url, payload in self.calls if "contacts" in payload]


class EmailWebsiteGates(unittest.TestCase):
    def setUp(self):
        self.api = GateApi()
        self._real = enrich._lusha_post
        enrich._lusha_post = self.api

    def tearDown(self):
        enrich._lusha_post = self._real

    def test_unaligned_lusha_email_is_not_written_onto_a_blank_cell(self):
        df = _frame([
            ["1", "Pat", "Smith", "", "", "Avari", "https://avari.com",
             "Tech", "", "", "", ""],
        ])
        out, stats = enrich.enrich_dataframe(df)
        self.assertEqual(out.at[0, "Email"], "")
        self.assertGreater(stats["emails_rejected"], 0)

    def test_company_aligned_email_is_accepted(self):
        self.api.email = "pat@avari.com"
        self.api.domain = "avari.com"
        df = _frame([
            ["1", "Pat", "Smith", "", "", "Avari", "https://avari.com",
             "Tech", "", "", "", ""],
        ])
        out, _ = enrich.enrich_dataframe(df)
        self.assertEqual(out.at[0, "Email"], "pat@avari.com")

    def test_unvalidated_email_is_replaced_when_aligned(self):
        self.api.email = "pat@avari.com"
        df = _frame([
            ["1", "Pat", "Smith", "old@avari.com", "+14155550100", "Avari",
             "https://avari.com", "Tech", "1", "1", UNDELIVERABLE, "old@avari.com"],
        ])
        out, stats = enrich.enrich_dataframe(df)
        self.assertEqual(out.at[0, "Email"], "pat@avari.com")
        self.assertEqual(stats["email_blanked_no_replacement_ids"], [])
        reveals = [p["reveal"] for p in self.api.contact_payloads()]
        self.assertTrue(any("emails" in r for r in reveals))

    def test_unvalidated_email_is_blanked_when_lusha_is_a_different_company(self):
        df = _frame([
            ["1", "Pat", "Smith", "old@avari.com", "+14155550100", "Avari",
             "https://avari.com", "Tech", "1", "1", UNDELIVERABLE, "old@avari.com"],
        ])
        out, stats = enrich.enrich_dataframe(df)
        self.assertEqual(out.at[0, "Email"], "")
        self.assertEqual(stats["email_blanked_no_replacement_ids"], ["1"])
        reasons = " ".join(
            "; ".join(item["reasons"]) for item in stats["enrichment_review"]
        )
        self.assertIn("no company-aligned replacement", reasons)

    def test_conflicting_lusha_website_is_not_written(self):
        df = _frame([
            ["1", "Pat", "Smith", "pat@avari.com", "+14155550100", "Avari",
             "", "Tech", "", "", "", ""],
        ])
        out, stats = enrich.enrich_dataframe(df)
        self.assertEqual(out.at[0, "Website"], "")
        self.assertGreater(stats["websites_rejected"], 0)

    def test_existing_phone_is_never_swapped_for_a_lusha_mobile(self):
        df = _frame([
            ["1", "Pat", "Smith", "", "+14155550100", "Avari",
             "https://avari.com", "Tech", "1", "1", "", ""],
        ])
        out, _ = enrich.enrich_dataframe(df)
        self.assertEqual(out.at[0, "Phone"], "+14155550100")
        for payload in self.api.contact_payloads():
            self.assertNotIn("phones", payload["reveal"])


class MismatchReview(unittest.TestCase):
    def test_leftover_email_company_mismatch_is_flagged(self):
        df = _frame([
            ["1", "Pat", "Smith", "pat@othercorp.com", "+14155550100", "Avari",
             "https://avari.com", "Tech", "1", "1", "deliverable", ""],
        ])
        _, stats = enrich.enrich_dataframe(df)
        reasons = " ".join(
            "; ".join(item["reasons"]) for item in stats["enrichment_review"]
        )
        self.assertIn("email domain does not match Company", reasons)


if __name__ == "__main__":
    unittest.main()
