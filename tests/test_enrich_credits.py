"""
Credit-cost tests for enrich_dataframe.

Core rule: a paid data point is only ever requested for the row that is missing
it. Phones cost 5x an email, so a row that already has a phone must never appear
in a request that reveals phones.
"""

import unittest

import pandas as pd

from pipeline import enrich

COLUMNS = [
    "Id", "FirstName", "LastName", "Email", "Phone", "Company",
    "Website", "Industry", "AnnualRevenue", "NumberOfEmployees",
]


def frame(rows):
    return pd.DataFrame(rows, columns=COLUMNS).fillna("")


class RecordingApi:
    """Stands in for lusha_post and records every payload sent."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        results = []
        for item in payload.get("contacts") or []:
            results.append(
                {
                    "clientReferenceId": item["clientReferenceId"],
                    "firstName": "Given",
                    "emails": [{"email": "found@result.co", "type": "work"}],
                    "phones": [{"number": "+15550000000", "type": "work"}],
                }
            )
        for item in payload.get("companies") or []:
            results.append(
                {
                    "clientReferenceId": item["clientReferenceId"],
                    "name": "Result Co",
                    "domain": "result.co",
                    "industry": "Technology",
                    "employeeCount": {"exact": 250},
                    "revenueRange": {"min": 10000000},
                }
            )
        return {"status": "ok", "results": results, "billing": {"creditsCharged": 1}}

    def contact_payloads(self):
        return [payload for url, payload in self.calls if "contacts" in payload]

    def company_payloads(self):
        return [payload for url, payload in self.calls if "companies" in payload]


class RevealScope(unittest.TestCase):
    def setUp(self):
        self.api = RecordingApi()
        self.real_post = enrich.lusha_post
        enrich.lusha_post = self.api

    def tearDown(self):
        enrich.lusha_post = self.real_post

    def test_phones_are_never_revealed_for_a_row_that_has_a_phone(self):
        df = frame(
            [
                ["1", "Pat", "Smith", "", "+14155550100", "Acme Corp", "https://acme.com", "Tech", "1000000", "50"],
                ["2", "Alex", "Ng", "alex@acme.com", "", "Acme Corp", "https://acme.com", "Tech", "1000000", "50"],
            ]
        )
        enrich.enrich_dataframe(df)

        for payload in self.api.contact_payloads():
            for item in payload["contacts"]:
                idx = int(item["clientReferenceId"])
                with self.subTest(reveal=payload["reveal"], row=df.at[idx, "Id"]):
                    if "phones" in payload["reveal"]:
                        self.assertEqual(df.at[idx, "Phone"], "")
                    if "emails" in payload["reveal"]:
                        self.assertEqual(df.at[idx, "Email"], "")

    def test_each_request_carries_an_explicit_reveal_list(self):
        df = frame(
            [["1", "Pat", "Smith", "", "", "Acme Corp", "https://acme.com", "Tech", "1000000", "50"]]
        )
        enrich.enrich_dataframe(df)

        for payload in self.api.contact_payloads():
            self.assertIn("reveal", payload)

    def test_a_row_missing_only_profile_fields_reveals_nothing(self):
        df = frame(
            [["1", "", "Smith", "pat@acme.com", "+14155550100", "Acme Corp", "https://acme.com", "Tech", "1000000", "50"]]
        )
        enrich.enrich_dataframe(df)

        self.assertEqual([p["reveal"] for p in self.api.contact_payloads()], [[]])

    def test_a_complete_row_is_never_sent(self):
        df = frame(
            [["1", "Pat", "Smith", "pat@acme.com", "+14155550100", "Acme", "https://acme.com", "Tech", "1000000", "50"]]
        )
        _, stats = enrich.enrich_dataframe(df)

        self.assertEqual(self.api.calls, [])
        self.assertEqual(stats["rows_attempted"], 0)
        self.assertEqual(stats["rows_skipped"], 1)


class CompanyReuse(unittest.TestCase):
    def setUp(self):
        self.api = RecordingApi()
        self.real_post = enrich.lusha_post
        enrich.lusha_post = self.api

    def tearDown(self):
        enrich.lusha_post = self.real_post

    def test_one_company_lookup_serves_every_row_on_that_domain(self):
        df = frame(
            [
                ["1", "Terry", "Ng", "terry.ng@globalcorp.com", "+447911123456", "CEA", "", "Tech", "", ""],
                ["2", "Manuel", "Costa", "manuel.costa@globalcorp.com", "+33612345678", "Globalcorp", "", "Tech", "", ""],
            ]
        )
        df, stats = enrich.enrich_dataframe(df)

        payloads = self.api.company_payloads()
        self.assertEqual(len(payloads), 1)
        self.assertEqual(len(payloads[0]["companies"]), 1)
        self.assertEqual(list(df["Website"]), ["https://result.co", "https://result.co"])
        self.assertEqual(list(df["NumberOfEmployees"]), ["250", "250"])
        self.assertEqual(list(df["AnnualRevenue"]), ["10000000", "10000000"])

    def test_a_missing_website_does_not_trigger_a_contact_call(self):
        df = frame(
            [["1", "Anja", "Visser", "anja.visser@asml.com", "+31402683000", "ASML", "", "Manufacturing", "", ""]]
        )
        enrich.enrich_dataframe(df)

        self.assertEqual(self.api.contact_payloads(), [])
        self.assertEqual(len(self.api.company_payloads()), 1)


    def test_blank_employees_and_revenue_trigger_a_company_lookup(self):
        df = frame(
            [["1", "Pat", "Smith", "pat@acme.com", "+14155550100", "Acme", "https://acme.com", "Tech", "", ""]]
        )
        df, _ = enrich.enrich_dataframe(df)

        self.assertEqual(self.api.contact_payloads(), [])
        self.assertEqual(len(self.api.company_payloads()), 1)
        self.assertEqual(df.at[0, "NumberOfEmployees"], "250")
        self.assertEqual(df.at[0, "AnnualRevenue"], "10000000")

    def test_informal_employee_and_revenue_headers_are_filled(self):
        df = pd.DataFrame(
            [{
                "Id": "1",
                "FirstName": "Pat",
                "LastName": "Smith",
                "Email": "pat@acme.com",
                "Phone": "+14155550100",
                "Company": "Acme",
                "Website": "https://acme.com",
                "Industry": "Tech",
                "Annual Revenue": "",
                "No of employee": "",
            }]
        )
        df, _ = enrich.enrich_dataframe(df)

        self.assertEqual(df.at[0, "No of employee"], "250")
        self.assertEqual(df.at[0, "Annual Revenue"], "10000000")
        self.assertNotIn("NumberOfEmployees", df.columns)
        self.assertNotIn("AnnualRevenue", df.columns)


if __name__ == "__main__":
    unittest.main()
