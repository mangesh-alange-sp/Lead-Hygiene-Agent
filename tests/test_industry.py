"""Closed Industry picklist: aliases unify, unknowns are preserved not title-cased."""

import unittest

import pandas as pd

from pipeline import enrich
from pipeline.normalize import normalize_dataframe, normalize_industry


class NormalizeIndustryTests(unittest.TestCase):
    def test_and_and_ampersand_forms_unify_to_the_canonical(self):
        for raw in (
            "Transportation & Logistics",
            "Transportation And Logistics",
            "transportation and logistics",
        ):
            with self.subTest(raw=raw):
                value, unknown = normalize_industry(raw)
                self.assertEqual(value, "Transportation & Logistics")
                self.assertFalse(unknown)

    def test_expanded_aliases_map(self):
        self.assertEqual(normalize_industry("saas")[0], "Technology")
        self.assertEqual(normalize_industry("software and technology")[0], "Software and Technology")
        self.assertEqual(normalize_industry("healthcare and medical")[0], "Healthcare and Medical")

    def test_unknown_value_is_preserved_not_title_cased(self):
        value, unknown = normalize_industry("widgets")
        self.assertEqual(value, "widgets")
        self.assertTrue(unknown)

    def test_unknown_industry_is_flagged_for_hitl(self):
        df = pd.DataFrame({
            "FirstName": ["Pat"], "LastName": ["Lee"],
            "Email": ["pat@acme.com"], "Company": ["Acme"],
            "Industry": ["widgets"],
        })
        out, _ = normalize_dataframe(df)
        self.assertEqual(out.at[0, "Industry"], "widgets")
        self.assertIn("unmapped_industry", str(out.at[0, "data_quality_flags"]))
        self.assertEqual(out.at[0, "hitl_review"], "Yes")


class PostEnrichIndustryTests(unittest.TestCase):
    def setUp(self):
        self._real = enrich._lusha_post

        def api(url, payload):
            results = []
            for item in payload.get("companies") or []:
                results.append({
                    "clientReferenceId": item["clientReferenceId"],
                    "industry": "Transportation And Logistics",
                    "employeeCount": {"exact": 10},
                })
            return {"status": "ok", "results": results, "billing": {"creditsCharged": 1}}

        enrich._lusha_post = api

    def tearDown(self):
        enrich._lusha_post = self._real

    def test_lusha_industry_is_mapped_to_the_canonical_picklist(self):
        df = pd.DataFrame([{
            "Id": "1",
            "FirstName": "Pat",
            "LastName": "Lee",
            "Email": "pat@acme.com",
            "Phone": "+14155550100",
            "Company": "Acme",
            "Website": "https://acme.com",
            "Industry": "",
            "AnnualRevenue": "",
            "NumberOfEmployees": "",
        }])
        out, _ = enrich.enrich_dataframe(df)
        self.assertEqual(out.at[0, "Industry"], "Transportation & Logistics")


if __name__ == "__main__":
    unittest.main()
