"""Tests for the production invariant checks that gate every batch."""

import unittest

from pipeline.invariants import (
    InvariantViolation,
    assert_records,
    check_drop_provenance,
    check_records,
    company_has_bad_dotted_case,
    phone_has_text_marker,
    phone_looks_like_excel_formula,
)


def _row(**overrides):
    row = {"Id": "00Q001", "Email": "pat@acme.com", "Phone": "(+1) 206-555-0100",
           "Company": "Acme Corp", "Website": "https://acme.com"}
    row.update(overrides)
    return row


class CleanBatchTests(unittest.TestCase):
    def test_clean_batch_passes(self):
        records = [_row(), _row(Id="00Q002", Email="jane@amazon.com", Company="Amazon.com Inc.",
                              Website="https://amazon.com")]
        self.assertEqual(check_records(records, rows_in=2), [])
        assert_records(records, rows_in=2)


class PhoneMarkerTests(unittest.TestCase):
    def test_detects_apostrophe_and_tab(self):
        self.assertTrue(phone_has_text_marker("'+1-206-555-0100"))
        self.assertTrue(phone_has_text_marker("\t+1-206-555-0100"))
        self.assertFalse(phone_has_text_marker("+1-206-555-0100"))
        self.assertFalse(phone_has_text_marker("(+1) 206-555-0100"))

    def test_detects_excel_formula_prefixes(self):
        for value in ("+1-206-555-0100", "=1-206-555-0100", "-2088", "@2065550100"):
            self.assertTrue(phone_looks_like_excel_formula(value), value)
        self.assertFalse(phone_looks_like_excel_formula("(+1) 206-555-0100"))
        self.assertFalse(phone_looks_like_excel_formula(""))

    def test_batch_fails_on_marked_phone(self):
        violations = check_records([_row(Phone="'+1-206-555-0100")])
        self.assertTrue(any("text marker" in v for v in violations))

    def test_batch_fails_on_plus_prefixed_phone(self):
        violations = check_records([_row(Phone="+1-206-555-0100")])
        self.assertTrue(any("Excel formulas" in v for v in violations))

    def test_batch_fails_on_marked_mobile(self):
        violations = check_records([_row(MobilePhone="'+1-206-555-0100")])
        self.assertTrue(any("text marker" in v for v in violations))


class CompanyCasingTests(unittest.TestCase):
    def test_domain_tokens_are_allowed(self):
        for company in ("Amazon.com Inc.", "Booking.com", "Siemens.de GmbH"):
            self.assertFalse(company_has_bad_dotted_case(company), company)

    def test_whitelisted_forms_are_allowed(self):
        for company in ("ASML Netherlands B.V.", "Milano S.r.l.", "R.O.C Military Academy"):
            self.assertFalse(company_has_bad_dotted_case(company), company)

    def test_bad_casing_detected(self):
        for company in ("Acme.corp Holdings", "U.s. Steel", "R.o.c Military Academy"):
            self.assertTrue(company_has_bad_dotted_case(company), company)

    def test_batch_fails_on_bad_casing(self):
        violations = check_records([_row(Company="R.o.c Military Academy")])
        self.assertTrue(any("lowercase after a dot" in v for v in violations))


class WebsiteTests(unittest.TestCase):
    def test_batch_fails_on_free_provider_website(self):
        for site in ("https://gmail.com", "yahoo.in", "https://www.hotmail.com"):
            violations = check_records([_row(Website=site)])
            self.assertTrue(any("free mail provider" in v for v in violations), site)


class EmailAndRowCountTests(unittest.TestCase):
    def test_batch_fails_on_duplicate_emails(self):
        records = [_row(), _row(Id="00Q002")]
        violations = check_records(records)
        self.assertTrue(any("Duplicate normalized emails" in v for v in violations))

    def test_case_differences_still_count_as_duplicates(self):
        records = [_row(Email="Pat@Acme.com"), _row(Id="00Q002", Email="pat@acme.com")]
        self.assertTrue(any("Duplicate" in v for v in check_records(records)))

    def test_blank_emails_are_allowed_to_repeat(self):
        records = [_row(Id="00Q001", Email=""), _row(Id="00Q002", Email="")]
        self.assertEqual(check_records(records), [])

    def test_batch_fails_when_row_count_grows(self):
        violations = check_records([_row(), _row(Id="00Q002", Email="b@acme.com")], rows_in=1)
        self.assertTrue(any("Row count grew" in v for v in violations))

    def test_assert_records_raises(self):
        with self.assertRaises(InvariantViolation):
            assert_records([_row(Phone="'+1-206-555-0100")])


class DropProvenanceTests(unittest.TestCase):
    def test_absorbed_duplicate_with_email_passes(self):
        merge_log = [{
            "surviving_lead_id": "00Q001",
            "merged_from_ids": "DUPE001",
            "match_signals_used": "exact_email",
            "absorbed_match_signals": "DUPE001:exact_email",
            "decision": "auto_merge",
        }]
        self.assertEqual(
            check_drop_provenance(
                ["00Q001", "DUPE001"], ["00Q001"], merge_log=merge_log,
            ),
            [],
        )

    def test_missing_lead_with_no_trail_fails(self):
        violations = check_drop_provenance(
            ["00Q001", "00Q002", "00Q003", "00Q004", "00Q005"],
            ["00Q001"],
            merge_log=[],
            dropped_test_ids=[],
        )
        self.assertTrue(any("no paper trail" in v for v in violations))
        self.assertTrue(any("00Q002" in v for v in violations))

    def test_merge_without_identity_signal_fails(self):
        merge_log = [{
            "surviving_lead_id": "T1",
            "merged_from_ids": "T2;T3;T4",
            "match_signals_used": "name+company_fuzzy",
            "absorbed_match_signals": "T2:name+company_fuzzy;T3:name+company_fuzzy;T4:name+company_fuzzy",
            "decision": "auto_merge",
        }]
        violations = check_drop_provenance(
            ["T1", "T2", "T3", "T4"],
            [],
            merge_log=merge_log,
            dropped_test_ids=["T1"],
        )
        self.assertTrue(any("without email/phone/name+company identity" in v for v in violations))

    def test_explicit_test_drops_are_accounted(self):
        self.assertEqual(
            check_drop_provenance(
                ["00Q001", "T1", "T2"],
                ["00Q001"],
                merge_log=[],
                dropped_test_ids=["T1", "T2"],
            ),
            [],
        )

    def test_batch_gate_fails_when_leads_vanish(self):
        records = [_row(Id="00Q001")]
        violations = check_records(
            records,
            rows_in=4,
            input_ids=["00Q001", "T1", "T2", "T3"],
            merge_log=[],
            dropped_test_ids=[],
        )
        self.assertTrue(any("no paper trail" in v for v in violations))


if __name__ == "__main__":
    unittest.main()
