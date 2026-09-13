"""Permanent edge cases from the improvement spec (A6)."""

import csv
import io
import unittest
from pathlib import Path

from pipeline.dedupe import dedupe_leads, score_pair
from pipeline.phone import parse_phone
from pipeline.tools import process_csv


def _record(lead_id, first, last, email, phone, company):
    return {
        "Id": lead_id, "FirstName": first, "LastName": last,
        "Email": email, "Phone": phone, "Company": company,
        "data_quality_flags": "", "hitl_review": "",
    }


class SingaporePhoneTests(unittest.TestCase):
    def test_sprinklr_without_evidence_is_needs_review(self):
        parsed = parse_phone("98006498", email="user@sprinklr.com", company="Sprinklr")
        self.assertEqual(parsed["value"], "98006498")
        self.assertEqual(parsed["status"], "needs_review")
        self.assertEqual(parsed["raw"], "98006498")

    def test_sprinklr_with_sg_tld_infers_65(self):
        parsed = parse_phone("98006498", email="user@sprinklr.com.sg")
        self.assertEqual(parsed["value"], "+6598006498")
        self.assertEqual(parsed["status"], "valid")


class AccentedNameTests(unittest.TestCase):
    def test_buechert_keeps_the_umlaut(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company,Country\n"
            "00Q1,Hans,Büchert,h.buechert@buchert.de,+491701112223,Büchert GmbH,Germany\n"
        )
        result = process_csv(csv_in)
        self.assertEqual(result["status"], "ok", result.get("message"))
        row = next(csv.DictReader(io.StringIO(result["csv"])))
        self.assertEqual(row["LastName"], "Büchert")


class WebsiteAsEmailTests(unittest.TestCase):
    def test_website_email_is_flagged_and_lead_is_kept(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company,Website\n"
            "00Q1,Pat,Lee,pat@acme.com,4155550100,Acme,pat@acme.com\n"
        )
        result = process_csv(csv_in)
        self.assertEqual(result["status"], "ok", result.get("message"))
        self.assertIn("00Q1", result["csv"])
        self.assertIn("website_is_email", result["audit_csv"])
        row = next(csv.DictReader(io.StringIO(result["csv"])))
        self.assertNotEqual(row["Website"], "pat@acme.com")


class SwitchboardGroupTests(unittest.TestCase):
    def test_named_switchboard_groups_stay_separate(self):
        groups = (
            ("Amazon", "2065550100", "jane@amazon.com", "mark@amazon.com"),
            ("Starbucks", "2065550200", "a@starbucks.com", "b@starbucks.com"),
            ("Procter & Gamble", "5135550100", "a@pg.com", "b@pg.com"),
            ("BCBS", "3125550111", "a@bcbs.com", "b@bcbs.com"),
            ("Centene", "3145550100", "a@centene.com", "b@centene.com"),
            ("AutoNation", "9545550100", "a@autonation.com", "b@autonation.com"),
        )
        for company, phone, email_a, email_b in groups:
            with self.subTest(company=company):
                result = score_pair(
                    _record("A", "Jane", "Doe", email_a, phone, company),
                    _record("B", "Mark", "Lopez", email_b, phone, company),
                )
                self.assertIn("switchboard_phone", result["signals"])
                self.assertFalse(result["auto_merge"])


class SyntheticDupePairTests(unittest.TestCase):
    def test_eleven_dupe_pairs_still_merge(self):
        records = []
        for i in range(1, 12):
            email = f"user{i:02d}@acme.com"
            records.append(_record(f"DUPEA{i:03d}", "Pat", f"Lee{i}", email, "4155550100", "Acme"))
            records.append(_record(f"DUPEB{i:03d}", "Pat", f"Lee{i}", email, "4155550100", "Acme Corp"))
        survivors, merge_log = dedupe_leads(records)
        self.assertEqual(len(survivors), 11)
        absorbed = []
        for entry in merge_log:
            absorbed.extend([part for part in entry.get("merged_from_ids", "").split(";") if part])
        self.assertEqual(len(absorbed), 11)
        for i in range(1, 12):
            pair = {f"DUPEA{i:03d}", f"DUPEB{i:03d}"}
            self.assertEqual(len(pair.intersection({s["Id"] for s in survivors})), 1)


class WebsitePrecedenceTests(unittest.TestCase):
    def test_email_domain_wins_when_company_is_not_in_any_lookup(self):
        # Prosegur is not in the lookup table; the email domain must still win
        # and must never be nulled by a lookup miss.
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company,Website\n"
            "00Q1,Mehmet,Arikan,mehmet.arikan@sap.com,,Prosegur,\n"
        )
        result = process_csv(csv_in)
        self.assertEqual(result["status"], "ok", result.get("message"))
        row = next(csv.DictReader(io.StringIO(result["csv"])))
        self.assertEqual(row["Website"], "https://sap.com")

    def test_existing_valid_website_survives_a_lookup_miss(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company,Website\n"
            "00Q1,Ana,Ruiz,ana@prosegur.com,,Prosegur,https://www.prosegur.com/careers\n"
        )
        result = process_csv(csv_in)
        self.assertEqual(result["status"], "ok", result.get("message"))
        row = next(csv.DictReader(io.StringIO(result["csv"])))
        self.assertEqual(row["Website"], "https://prosegur.com")


class RegressionDiffTests(unittest.TestCase):
    def test_diff_returns_id_field_old_new_tuples(self):
        from pipeline.regression import FieldChange, diff_records

        baseline = {"00Q1": {"Id": "00Q1", "Phone": "+447771695127", "Email": "a@acme.com"}}
        current = {"00Q1": {"Id": "00Q1", "Phone": "07771695127", "Email": "a@acme.com"}}
        changes = diff_records(baseline, current)
        self.assertEqual(changes, [FieldChange("00Q1", "Phone", "+447771695127", "07771695127")])
        lead_id, field, old, new = changes[0]
        self.assertEqual((lead_id, field, old, new),
                         ("00Q1", "Phone", "+447771695127", "07771695127"))

    def test_new_and_dropped_ids_are_not_regressions(self):
        from pipeline.regression import diff_records

        baseline = {"00Q1": {"Id": "00Q1", "Phone": "+447771695127"}}
        current = {"00Q2": {"Id": "00Q2", "Phone": "+12065550100"}}
        self.assertEqual(diff_records(baseline, current), [])

    def test_pipeline_blocks_delivery_when_a_field_changed(self):
        golden = Path("tests/fixtures/golden_deduped.csv").read_text(encoding="utf-8")
        # A title change survives normalization, so it reaches the diff gate.
        tampered = golden.replace(",PM,", ",Director,", 1)
        self.assertNotEqual(tampered, golden)
        out = process_csv(tampered)
        self.assertEqual(out["status"], "error")
        self.assertTrue(out["field_diffs"])

    def test_agent_path_does_not_block_on_the_fixture_golden_file(self):
        import asyncio

        from pipeline.tools import run_dedup_pipeline

        class _Ctx:
            def __init__(self):
                self.state = {}
                self.saved = []

            async def save_artifact(self, filename, artifact):
                self.saved.append(filename)
                return 1

        golden = Path("tests/fixtures/golden_deduped.csv").read_text(encoding="utf-8")
        tampered = golden.replace(",PM,", ",Director,", 1)
        ctx = _Ctx()
        out = asyncio.run(run_dedup_pipeline(tampered, tool_context=ctx))
        self.assertEqual(out["status"], "ok", out.get("message"))
        self.assertIn("deduped.csv", ctx.saved)


class ChangeReasonTests(unittest.TestCase):
    def test_technical_log_captures_phone_and_website_reasons(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company,Website,Country\n"
            "00Q1,Ada,Chen,ada@castelity.de,01724597285,Castelity,,Germany\n"
        )
        result = process_csv(csv_in)
        self.assertEqual(result["status"], "ok", result.get("message"))
        log = result["summary"]["technical_log"]
        self.assertTrue(log)
        blob = " ".join(row["reasons"] for row in log)
        self.assertIn("phone:", blob)
        self.assertIn("website:", blob)


if __name__ == "__main__":
    unittest.main()
