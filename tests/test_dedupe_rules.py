"""
Rule-based tests for dedupe_leads.

The invariants asserted here hold for any input, so they are checked against
generated batches as well as the known-tricky records.
"""

import random
import unittest

from pipeline.dedupe import dedupe_leads, normalized_email, score_pair

FIELDS = ("Id", "FirstName", "LastName", "Email", "Phone", "Company")


def _record(lead_id, first="", last="", email="", phone="", company=""):
    return {
        "Id": lead_id, "FirstName": first, "LastName": last,
        "Email": email, "Phone": phone, "Company": company,
        "data_quality_flags": "", "hitl_review": "",
    }


def _random_batch(seed, size=60):
    rng = random.Random(seed)
    firsts = ["Jane", "Lee", "Hans", "Pat", "Alex", "Maya", "Raj", "Osei", "Terry", "J"]
    lasts = ["Doe", "Bailey", "Buchert", "Smith", "Ng", "Brooks", "Patel", "Stephen"]
    companies = ["Acme Corp", "Amazon.com Inc.", "Honda North America", "TSMC", "", "Bfg"]
    domains = ["acme.com", "amazon.com", "honda.com", "gmail.com", ""]
    records = []
    for i in range(size):
        first = rng.choice(firsts)
        last = rng.choice(lasts)
        domain = rng.choice(domains)
        email = f"{first.lower()}.{last.lower()}@{domain}" if domain else ""
        phone = rng.choice(["2065550100", "3125550100", "", "+49-170-111-2223"])
        records.append(_record(f"00Q{i:03d}", first, last, email, phone, rng.choice(companies)))
    return records


class UniversalInvariants(unittest.TestCase):
    def _assert_invariants(self, records):
        survivors, merge_log = dedupe_leads(records)

        self.assertLessEqual(len(survivors), len(records))

        input_ids = {r["Id"] for r in records}
        for survivor in survivors:
            self.assertIn(survivor["Id"], input_ids)

        emails = [normalized_email(s.get("Email", "")) for s in survivors]
        populated = [e for e in emails if e]
        self.assertEqual(len(populated), len(set(populated)), "duplicate emails survived")

        for entry in merge_log:
            self.assertIn("surviving_lead_id", entry)
            self.assertIn("confidence_score", entry)
        return survivors, merge_log

    def test_generated_batches(self):
        for seed in range(12):
            with self.subTest(seed=seed):
                self._assert_invariants(_random_batch(seed))

    def test_empty_and_single_row_batches(self):
        self.assertEqual(dedupe_leads([]), ([], []))
        survivors, _ = dedupe_leads([_record("00Q001", "Jane", "Doe", "jane@acme.com")])
        self.assertEqual(len(survivors), 1)

    def test_all_identical_rows_collapse_to_one(self):
        records = [_record(f"00Q{i:03d}", "Jane", "Doe", "jane@amazon.com", "2065550100", "Amazon") for i in range(8)]
        survivors, _ = self._assert_invariants(records)
        self.assertEqual(len(survivors), 1)

    def test_blank_emails_are_never_merged_on_email_alone(self):
        records = [
            _record("00Q001", "Ann", "Alpha", "", "", "Alpha Co"),
            _record("00Q002", "Bob", "Beta", "", "", "Beta Co"),
            _record("00Q003", "Cid", "Gamma", "", "", "Gamma Co"),
        ]
        survivors, _ = self._assert_invariants(records)
        self.assertEqual(len(survivors), 3)

    def test_input_records_are_not_mutated(self):
        records = [
            _record("00Q001", "Osei", "Stephen", "osei@honda.com", "3105551234", "Honda North America"),
            _record("00Q002", "Takehiko", "Yazawa", "yazawa@honda.com", "3105551234", "American Honda Motor Co"),
        ]
        before = [dict(record) for record in records]
        dedupe_leads(records)
        self.assertEqual(records, before)

    def test_output_keys_match_input_keys(self):
        records = _random_batch(99, size=10)
        survivors, _ = dedupe_leads(records)
        for survivor in survivors:
            self.assertEqual(set(survivor), set(records[0]))


class SignalRules(unittest.TestCase):
    def test_exact_email_auto_merges(self):
        result = score_pair(
            _record("00Q001", "Jane", "Doe", "jane@amazon.com", company="Amazon.com Inc."),
            _record("DUPE001", "Jane", "Doe", "jane@amazon.com", company="Amazon Inc"),
        )
        self.assertIn("exact_email", result["signals"])
        self.assertTrue(result["auto_merge"])

    def test_shared_phone_alone_is_hitl_not_auto_merge(self):
        result = score_pair(
            _record("00Q004", "Osei", "Stephen", "osei.stephen@honda.com", "+1-310-555-1234", "Honda North America"),
            _record("00Q005", "Takehiko", "Yazawa", "takehiko.yazawa@honda.com", "+1-310-555-1234", "American Honda Motor Co."),
        )
        self.assertIn("phone_shared", result["signals"])
        self.assertFalse(result["auto_merge"])
        self.assertTrue(result["hitl"])

    def test_shared_phone_flags_both_rows_for_review(self):
        records = [
            _record("00Q004", "Osei", "Stephen", "osei.stephen@honda.com", "3105551234", "Honda North America"),
            _record("00Q005", "Takehiko", "Yazawa", "takehiko.yazawa@honda.com", "3105551234", "American Honda Motor Co"),
        ]
        survivors, merge_log = dedupe_leads(records)
        self.assertEqual(len(survivors), 2)
        self.assertTrue(all(s["hitl_review"] == "Yes" for s in survivors))
        self.assertTrue(any(entry["decision"] == "hitl_review" for entry in merge_log))

    def test_unicode_folded_names_merge(self):
        records = [
            _record("00Q003", "Hans", "Büchert", "h.buechert@buchert.de", "+491701112223", "Büchert GmbH"),
            _record("DUPE003", "Hans", "Buchert", "h.buchert@buchert.de", "+491701112223", "Buchert"),
        ]
        survivors, merge_log = dedupe_leads(records)
        self.assertEqual(len(survivors), 1)
        self.assertEqual(survivors[0]["Id"], "00Q003")
        self.assertTrue(any("DUPE003" in entry["merged_from_ids"] for entry in merge_log))

    def test_different_people_at_the_same_company_do_not_merge(self):
        records = [
            _record("00Q001", "Jane", "Doe", "jane@acme.com", "2065550100", "Acme Corp"),
            _record("00Q002", "Mark", "Lopez", "mark@acme.com", "2065550101", "Acme Corp"),
        ]
        survivors, _ = dedupe_leads(records)
        self.assertEqual(len(survivors), 2)

    def test_merge_log_carries_provenance(self):
        records = [
            _record("00Q001", "Jane", "Doe", "jane@amazon.com", "2065550100", "Amazon.com Inc."),
            _record("DUPE001", "Jane", "Doe", "jane@amazon.com", "2065550100", "Amazon Inc"),
        ]
        _, merge_log = dedupe_leads(records)
        entry = next(e for e in merge_log if e["surviving_lead_id"] == "00Q001")
        self.assertIn("DUPE001", entry["merged_from_ids"])
        self.assertIn("exact_email", entry["match_signals_used"])
        self.assertEqual(entry["confidence_score"], "High")
        self.assertEqual(entry["decision"], "auto_merge")

    def test_field_merge_prefers_the_fuller_email(self):
        records = [
            _record("00Q002", "Lee", "Bailey", "l.bailey@mcd.com", "3125550100", "McDonald's GmbH"),
            _record("DUPE002", "L", "Bailey", "lee.bailey@us.mcd.com", "3125550100", "McDonalds"),
        ]
        survivors, _ = dedupe_leads(records)
        self.assertEqual(len(survivors), 1)
        self.assertEqual(survivors[0]["Email"], "lee.bailey@us.mcd.com")
        self.assertEqual(survivors[0]["FirstName"], "Lee")

    def test_same_person_at_the_same_company_merges(self):
        records = [
            _record("00Q100", "David", "Manaster", "dmanaster@pg.com", "", "PROCTER AND GAMBLE"),
            _record("00Q101", "Dave", "Manaster", "david.manaster@pg.com", "5135550100", "Procter & Gamble"),
        ]
        survivors, merge_log = dedupe_leads(records)
        self.assertEqual(len(survivors), 1)
        self.assertEqual(survivors[0]["FirstName"], "David")
        self.assertTrue(any("00Q101" in entry["merged_from_ids"] or "00Q100" in entry["merged_from_ids"] for entry in merge_log))

    def test_different_people_at_procter_and_gamble_do_not_merge(self):
        records = [
            _record("00Q100", "David", "Manaster", "dmanaster@pg.com", "", "Procter & Gamble"),
            _record("00Q102", "A.", "Mazhar", "amazhar@pg.com", "", "Procter & Gamble"),
        ]
        survivors, _ = dedupe_leads(records)
        self.assertEqual(len(survivors), 2)

    def test_test_named_rows_do_not_merge_on_name_and_company(self):
        records = [
            _record("T1", "Test", "User", "", "", "Test Co"),
            _record("T2", "Test", "User", "", "", "Test Arp"),
            _record("T3", "Dummy", "Person", "", "", "Dummy Company"),
            _record("T4", "Fake", "Name", "", "", "Sample Company"),
        ]
        for row in records:
            row["data_quality_flags"] = "test_data"
        result = score_pair(records[0], records[1])
        self.assertFalse(result["auto_merge"])
        self.assertFalse(result["hitl"])
        survivors, merge_log = dedupe_leads(records)
        self.assertEqual(len(survivors), 4)
        self.assertFalse(any(entry.get("merged_from_ids") for entry in merge_log))

    def test_first_name_edit_distance_is_not_identity(self):
        result = score_pair(
            _record("00Q001", "Jane", "Doe", "jane@acme.com", "", "Acme Corp"),
            _record("00Q002", "Jake", "Doe", "jake@acme.com", "", "Acme Corp"),
        )
        self.assertFalse(result["auto_merge"])
        self.assertNotIn("name+company", result["signals"])

    def test_subset_company_tokens_are_not_the_same_company(self):
        result = score_pair(
            _record("00Q001", "Jane", "Doe", "jane@amazon.com", "", "Amazon.com Inc."),
            _record("00Q002", "Jane", "Doe", "jane.doe@gmail.com", "", "Amazon Inc"),
        )
        self.assertFalse(result["auto_merge"])
        self.assertNotIn("name+company", result["signals"])

    def test_test_row_does_not_taint_a_real_survivor(self):
        real = _record("00Q200", "Ada", "Chen", "ada.chen@sailpoint.com", "4155550100", "SailPoint Technologies")
        test = _record("00Q201", "Ada", "Chen", "", "", "SailPoint Technologies")
        test["data_quality_flags"] = "test_data"
        survivors, merge_log = dedupe_leads([real, test])
        self.assertEqual(len(survivors), 1)
        self.assertEqual(survivors[0]["Id"], "00Q200")
        self.assertNotIn("test_data", survivors[0]["data_quality_flags"].split("|"))
        self.assertTrue(any("00Q201" in entry.get("merged_from_ids", "") for entry in merge_log))
        self.assertTrue(
            any("name+company" in entry.get("absorbed_match_signals", "") for entry in merge_log)
        )

    def test_company_alias_table_is_a_dedupe_key(self):
        result = score_pair(
            _record("00Q1", "David", "Manaster", "dmanaster@pg.com", "", "P&G"),
            _record("00Q2", "Dave", "Manaster", "david.manaster@pg.com", "", "Procter & Gamble"),
        )
        self.assertIn("name+company", result["signals"])
        self.assertTrue(result["auto_merge"])

    def test_same_name_and_phone_different_company_is_review_not_merge(self):
        result = score_pair(
            _record("00Q1", "Natalie", "Baggio", "nbaggio@lakeland.org", "2699838300", "Lakeland Health"),
            _record("00Q2", "Natalie", "Baggio", "nbaggio@corewell.org", "2699838300", "Corewell Health"),
        )
        self.assertIn("name+phone", result["signals"])
        self.assertIn("weak_company", result["signals"])
        self.assertFalse(result["auto_merge"])
        self.assertTrue(result["hitl"])
        records = [
            _record("00Q1", "Natalie", "Baggio", "nbaggio@lakeland.org", "2699838300", "Lakeland Health"),
            _record("00Q2", "Natalie", "Baggio", "nbaggio@corewell.org", "2699838300", "Corewell Health"),
        ]
        survivors, merge_log = dedupe_leads(records)
        self.assertEqual(len(survivors), 2)
        self.assertTrue(all(s["hitl_review"] == "Yes" for s in survivors))
        self.assertFalse(any(entry.get("merged_from_ids") for entry in merge_log))
        self.assertTrue(any(entry["decision"] == "hitl_review" for entry in merge_log))

    def test_same_name_and_phone_same_company_still_merges(self):
        records = [
            _record("00Q1", "Natalie", "Baggio", "n.baggio@corewell.org", "2699838300", "Corewell Health"),
            _record("00Q2", "Natalie", "Baggio", "natalie.baggio@corewell.org", "2699838300", "Corewell Health"),
        ]
        result = score_pair(records[0], records[1])
        self.assertIn("name+phone", result["signals"])
        self.assertNotIn("weak_company", result["signals"])
        self.assertTrue(result["auto_merge"])
        survivors, merge_log = dedupe_leads(records)
        self.assertEqual(len(survivors), 1)
        self.assertTrue(any(entry.get("merged_from_ids") for entry in merge_log))

    def test_shared_company_switchboard_does_not_merge(self):
        result = score_pair(
            _record("00Q1", "Jane", "Doe", "jane@amazon.com", "2065550100", "Amazon"),
            _record("00Q2", "Mark", "Lopez", "mark@amazon.com", "2065550100", "Amazon"),
        )
        self.assertIn("switchboard_phone", result["signals"])
        self.assertFalse(result["auto_merge"])
        self.assertTrue(result["hitl"])

    def test_near_miss_phone_is_a_review_signal_not_identity(self):
        result = score_pair(
            _record("00Q1", "Jane", "Doe", "jane@acme.com", "2065550100", "Acme Corp"),
            _record("00Q2", "Jane", "Doe", "jane.d@other.com", "2065550101", "Other Co"),
        )
        self.assertIn("name+phone_near", result["signals"])
        self.assertFalse(result["auto_merge"])
        self.assertTrue(result["hitl"])

    def test_merge_log_records_a_reason(self):
        records = [
            _record("00Q001", "Jane", "Doe", "jane@amazon.com", "2065550100", "Amazon.com Inc."),
            _record("DUPE001", "Jane", "Doe", "jane@amazon.com", "2065550100", "Amazon Inc"),
        ]
        _, merge_log = dedupe_leads(records)
        entry = next(e for e in merge_log if e["surviving_lead_id"] == "00Q001")
        self.assertIn("exact email", entry["match_reason"])
        self.assertTrue(entry["match_reason"].startswith("High"))

    def test_formal_company_suffix_is_taken_from_the_merged_record(self):
        from pipeline.dedupe import prefer_formal_company

        self.assertEqual(prefer_formal_company("Castelity", "Castelity GmbH"), "Castelity GmbH")
        self.assertEqual(prefer_formal_company("Sprinklr", "Sprinklr Inc."), "Sprinklr Inc.")
        self.assertEqual(prefer_formal_company("Castelity GmbH", "Castelity"), "Castelity GmbH")
        records = [
            _record("00Q1", "Ada", "Chen", "ada@castelity.de", "", "Castelity"),
            _record("00Q2", "Ada", "Chen", "ada.chen@castelity.de", "", "Castelity GmbH"),
        ]
        survivors, _ = dedupe_leads(records)
        self.assertEqual(len(survivors), 1)
        self.assertEqual(survivors[0]["Company"], "Castelity GmbH")


if __name__ == "__main__":
    unittest.main()
