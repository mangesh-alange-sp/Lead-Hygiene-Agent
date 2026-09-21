"""Salesforce picklist catalog: match official names, nicknames, or flag."""

import unittest

import pandas as pd

from pipeline.normalize import normalize_dataframe
from pipeline.picklists import load_catalog, reset_catalog, resolve_picklist
from pipeline.salesforce_io import catalog_from_describe


class DescribeParserTests(unittest.TestCase):
    def test_describe_keeps_active_picklist_values(self):
        payload = {
            "fields": [
                {
                    "name": "Industry",
                    "type": "picklist",
                    "restrictedPicklist": True,
                    "controllerName": None,
                    "picklistValues": [
                        {"value": "Technology", "label": "Technology", "active": True},
                        {"value": "Old", "label": "Old", "active": False},
                    ],
                },
                {"name": "Title", "type": "string", "picklistValues": []},
            ]
        }
        catalog = catalog_from_describe(payload)
        options = catalog["fields"]["Industry"]["options"]
        self.assertEqual([item["value"] for item in options], ["Technology", "Old"])
        self.assertTrue(catalog["fields"]["Industry"]["restricted"])


class ResolvePicklistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_catalog()
        load_catalog(refresh=False)

    def test_official_label_matches_without_a_synonym(self):
        result = resolve_picklist("Industry", "Transportation And Logistics")
        self.assertEqual(result["value"], "Transportation & Logistics")
        self.assertTrue(result["matched"])

    def test_synonym_maps_only_when_the_target_exists(self):
        result = resolve_picklist("Industry", "saas")
        self.assertEqual(result["value"], "Technology")
        self.assertTrue(result["matched"])

    def test_unknown_value_is_preserved_and_flagged(self):
        result = resolve_picklist("Industry", "widgets")
        self.assertEqual(result["value"], "widgets")
        self.assertFalse(result["matched"])
        self.assertEqual(result["flag"], "unmapped_industry")

    def test_legal_value_round_trips(self):
        once = resolve_picklist("Industry", "Technology")["value"]
        again = resolve_picklist("Industry", once)["value"]
        self.assertEqual(once, again)
        self.assertEqual(once, "Technology")

    def test_status_is_not_rewritten(self):
        result = resolve_picklist("Status", "mql")
        self.assertEqual(result["value"], "mql")
        self.assertFalse(result["matched"])
        self.assertEqual(result["flag"], "unmapped_status")

    def test_legal_status_is_kept(self):
        result = resolve_picklist("Status", "Open")
        self.assertEqual(result["value"], "Open")
        self.assertTrue(result["matched"])

    def test_state_abbreviation_needs_country(self):
        bare = resolve_picklist("State", "CA")
        self.assertEqual(bare["value"], "CA")
        self.assertFalse(bare["matched"])
        with_us = resolve_picklist("State", "CA", country="United States")
        self.assertEqual(with_us["value"], "California")
        self.assertTrue(with_us["matched"])
        with_canada = resolve_picklist("State", "CA", country="Canada")
        self.assertEqual(with_canada["value"], "CA")
        self.assertFalse(with_canada["matched"])

    def test_country_synonym(self):
        result = resolve_picklist("Country", "USA")
        self.assertEqual(result["value"], "United States")
        self.assertTrue(result["matched"])

    def test_lead_source_nickname(self):
        result = resolve_picklist("LeadSource", "website form")
        self.assertEqual(result["value"], "Web")
        self.assertTrue(result["matched"])
        webinar = resolve_picklist("LeadSource", "webinar")
        self.assertEqual(webinar["value"], "Webinar")
        self.assertTrue(webinar["matched"])

    def test_lusha_industry_label_maps_only_when_target_is_on_the_list(self):
        result = resolve_picklist("Industry", "Technology, Information & Media")
        self.assertEqual(result["value"], "Technology")
        self.assertTrue(result["matched"])
        oil = resolve_picklist("Industry", "Oil, Gas & Mining")
        self.assertEqual(oil["value"], "Energy")
        self.assertTrue(oil["matched"])

    def test_seed_skips_placeholders_and_existing_keys(self):
        from pipeline.picklists import seed_catalog_from_values, CACHE_PATH
        added = seed_catalog_from_values({
            "Status": ["missingQS", "Open", ""],
            "Industry": ["Technology"],
        })
        self.assertNotIn("Status", added)
        self.assertNotIn("Industry", added)
        # Reload after seed_catalog_from_values reset the catalog.
        load_catalog(refresh=False, path=CACHE_PATH)
        result = resolve_picklist("Industry", "")
        self.assertEqual(result["value"], "")
        self.assertTrue(result["matched"])


class NormalizePicklistFrameTests(unittest.TestCase):
    def test_unmapped_country_is_not_title_cased(self):
        df = pd.DataFrame({
            "FirstName": ["Pat"], "LastName": ["Lee"],
            "Email": ["pat@acme.com"], "Company": ["Acme"],
            "Country": ["narnia"],
            "State": ["CA"],
            "LeadSource": ["webinar"],
            "Status": ["mql"],
            "Industry": ["widgets"],
        })
        out, _ = normalize_dataframe(df)
        self.assertEqual(out.at[0, "Country"], "narnia")
        self.assertIn("unmapped_country", str(out.at[0, "data_quality_flags"]))
        self.assertEqual(out.at[0, "State"], "CA")
        self.assertIn("unmapped_state", str(out.at[0, "data_quality_flags"]))
        self.assertEqual(out.at[0, "LeadSource"], "Webinar")
        self.assertEqual(out.at[0, "Status"], "mql")
        self.assertIn("unmapped_status", str(out.at[0, "data_quality_flags"]))
        self.assertEqual(out.at[0, "Industry"], "widgets")
        self.assertIn("unmapped_industry", str(out.at[0, "data_quality_flags"]))
        self.assertEqual(out.at[0, "hitl_review"], "Yes")

    def test_us_country_unlocks_california(self):
        df = pd.DataFrame({
            "FirstName": ["Pat"], "LastName": ["Lee"],
            "Email": ["pat@acme.com"], "Company": ["Acme"],
            "Country": ["US"],
            "State": ["ca"],
            "Industry": ["Technology"],
        })
        out, _ = normalize_dataframe(df)
        self.assertEqual(out.at[0, "Country"], "United States")
        self.assertEqual(out.at[0, "State"], "California")
        self.assertNotIn("unmapped_state", str(out.at[0, "data_quality_flags"]))


class LiveCatalogTests(unittest.TestCase):
    def test_refresh_loads_salesforce_when_credentials_are_set(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        payload = {
            "fields": [
                {
                    "name": "Industry",
                    "type": "picklist",
                    "restrictedPicklist": True,
                    "controllerName": None,
                    "picklistValues": [
                        {"value": "Technology", "label": "Technology", "active": True},
                    ],
                },
            ]
        }
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        handle.close()
        path = Path(handle.name)
        env = {
            "SALESFORCE_INSTANCE_URL": "https://example.my.salesforce.com",
            "SALESFORCE_ACCESS_TOKEN": "token",
        }
        try:
            with patch.dict(os.environ, env, clear=False):
                with patch(
                    "pipeline.salesforce_io.describe_lead", return_value=payload
                ) as describe:
                    reset_catalog()
                    catalog = load_catalog(refresh=True, path=path)
            describe.assert_called_once()
            self.assertEqual(catalog["source"], "salesforce")
            self.assertEqual(
                [opt["value"] for opt in catalog["fields"]["Industry"]["options"]],
                ["Technology"],
            )
        finally:
            reset_catalog()
            load_catalog(refresh=False)
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

