"""Permanent edge cases from the improvement spec (A6)."""

import csv
import io
import unittest

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
        self.assertEqual(parsed["value"], "+65-9800-6498")
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
