"""
The NANP fallback and the two automated gates that make it self-verifying:
every phone lands in exactly one of two visible states, and no field on a known
Id may move without an explicit acceptance.
"""

import csv
import io
import unittest
from pathlib import Path

from pipeline.invariants import (
    check_records,
    phone_has_text_marker,
    phone_looks_like_excel_formula,
)
from pipeline.phone import (
    E164_RE,
    NANP_FALLBACK_REGION,
    STATUS_NEEDS_REVIEW,
    STATUS_VALID,
    parse_phone,
)
from pipeline.regression import (
    RegressionDetected,
    assert_no_regression,
    diff_against_golden,
)
from pipeline.schema import check_phone_states
from pipeline.textnorm import digits_only
from pipeline.tools import process_csv

import pandas as pd

FIXTURE = Path(__file__).parent / "fixtures" / "lead_data_with_duplicates.csv"
GOLDEN = Path(__file__).parent / "fixtures" / "golden_deduped.csv"

# Formats that carried no country code and were previously skipped entirely.
US_SHAPES = [
    "(910) 689-5753",
    "(206) 555-0100",
    "206-555-0100",
    "206.555.0100",
    "206 555 0100",
    "9106895753",
    "8124835574",
    "5714220949",
]

# Same bucket, but no reliable country evidence and not NANP-shaped.
AMBIGUOUS = [
    ("98006498", "Singapore, 8 digits"),
    ("0167458214", "France, national trunk 0"),
    ("01724597285", "Germany, 11 digits with trunk 0"),
    ("017245972", "Germany, too short for NANP"),
    ("099300286056", "India, 12 digits"),
    ("022-1234-5678", "India landline"),
]


class NanpFallbackTests(unittest.TestCase):
    def test_every_us_shape_resolves_to_e164_without_a_plus_prefix(self):
        for raw in US_SHAPES:
            with self.subTest(raw=raw):
                parsed = parse_phone(raw)
                self.assertEqual(parsed["status"], STATUS_VALID, parsed)
                self.assertRegex(parsed["value"], r"^\+1\d{10}$")
                self.assertEqual(parsed["raw"], raw)

    def test_the_fallback_is_named_in_the_reason(self):
        parsed = parse_phone("(910) 689-5753")
        self.assertIn(NANP_FALLBACK_REGION, parsed["reason"])
        self.assertIn("no other country evidence", parsed["reason"])

    def test_ambiguous_numbers_stay_flagged_rather_than_guessed(self):
        for raw, why in AMBIGUOUS:
            with self.subTest(raw=raw, why=why):
                parsed = parse_phone(raw)
                self.assertEqual(parsed["status"], STATUS_NEEDS_REVIEW, parsed)
                self.assertEqual(parsed["value"], raw, "value must be untouched")
                self.assertIn("no country-code evidence", parsed["reason"])

    def test_the_fallback_never_runs_when_the_record_has_its_own_evidence(self):
        # Each of these would be a valid US number if the fallback were applied
        # blindly; the record's own evidence has to win first.
        cases = [
            ({"country": "India"}, "+919845550100"),
            ({"email": "a@corp.co.in"}, "+919845550100"),
        ]
        for kwargs, expected in cases:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(parse_phone("9845550100", **kwargs)["value"], expected)

    def test_an_explicit_country_code_is_never_reinterpreted_as_us(self):
        parsed = parse_phone("+44-7771-695-127")
        self.assertEqual(parsed["value"], "+447771695127")
        self.assertEqual(parsed["status"], STATUS_VALID)


class PhoneStatusColumnTests(unittest.TestCase):
    """Every phone record must be one of exactly two states, visible in data."""

    @classmethod
    def setUpClass(cls):
        cls.result = process_csv(FIXTURE.read_text(encoding="utf-8"))
        assert cls.result["status"] == "ok", cls.result.get("message")
        cls.audit = list(csv.DictReader(io.StringIO(cls.result["audit_csv"])))

    def test_audit_log_exposes_phone_status_and_reason(self):
        header = list(csv.reader(io.StringIO(self.result["audit_csv"])))[0]
        for column in ("phone_status", "phone_reason", "phone_raw",
                       "phone_extension", "email_status", "email_raw"):
            self.assertIn(column, header)

    def test_no_phone_record_is_left_without_a_state(self):
        statuses = {row["phone_status"] for row in self.audit if row["phone_status"]}
        self.assertTrue(statuses)
        self.assertTrue(
            statuses <= {STATUS_VALID, STATUS_NEEDS_REVIEW, "unparseable"}, statuses
        )

    def test_a_flagged_record_always_carries_a_reason(self):
        flagged = [r for r in self.audit if r["phone_status"] == STATUS_NEEDS_REVIEW]
        self.assertTrue(flagged, "fixture should exercise the needs_review path")
        for row in flagged:
            with self.subTest(lead=row["surviving_lead_id"]):
                self.assertTrue(row["phone_reason"], "review needs a stated reason")
                self.assertTrue(row["phone_raw"], "review needs the submitted value")

    def test_every_phone_in_the_output_is_e164_or_flagged(self):
        for row in csv.DictReader(io.StringIO(self.result["csv"])):
            phone = row.get("Phone", "")
            if not phone:
                continue
            status = next(
                (
                    entry["phone_status"]
                    for entry in self.audit
                    if entry["surviving_lead_id"] == row["Id"]
                ),
                "",
            )
            with self.subTest(lead=row["Id"], phone=phone):
                if status == STATUS_VALID:
                    self.assertRegex(phone, r"^\+\d{6,15}$")
                else:
                    self.assertEqual(status, STATUS_NEEDS_REVIEW)

    def test_a_cleared_junk_phone_still_keeps_its_submitted_value(self):
        row = next(
            (r for r in self.audit if "garbage_phone" in r["data_quality_flags"]), None
        )
        self.assertIsNotNone(row, "fixture should contain a garbage phone")
        self.assertTrue(row["phone_raw"], "the as-submitted value must survive")


class PhoneStateGateTests(unittest.TestCase):
    def _frame(self, phone, status):
        return pd.DataFrame({"Id": ["00Q1"], "Phone": [phone], "phone_status": [status]})

    def test_missing_status_is_a_violation(self):
        violations = check_phone_states(self._frame("+12065550100", ""))
        self.assertTrue(violations)
        self.assertIn("no phone_status", violations[0])

    def test_valid_status_on_a_non_e164_value_is_a_violation(self):
        violations = check_phone_states(self._frame("2065550100", STATUS_VALID))
        self.assertTrue(violations)
        self.assertIn("not E.164", violations[0])

    def test_a_preserved_plus_value_under_review_is_not_a_violation(self):
        # An input that already carried +CC and then failed validation is kept
        # verbatim, so it looks like E.164 while being legitimately needs_review.
        frame = pd.DataFrame({
            "Id": ["00Q1"],
            "Phone": ["+49172459728"],
            "Phone_raw": ["+49172459728"],
            "phone_status": [STATUS_NEEDS_REVIEW],
        })
        self.assertEqual(check_phone_states(frame), [])

    def test_a_flagged_value_that_drifted_from_the_input_is_a_violation(self):
        frame = pd.DataFrame({
            "Id": ["00Q1"],
            "Phone": ["+491724597280000"],
            "Phone_raw": ["+49172459728"],
            "phone_status": [STATUS_NEEDS_REVIEW],
        })
        violations = check_phone_states(frame)
        self.assertTrue(violations)
        self.assertIn("no longer matches what was submitted", violations[0])

    def test_an_unvalidatable_plus_number_survives_the_whole_pipeline(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Company,Phone,Title\n"
            "00QZ1,Carsten,Buechert,c@casteliy.de,Casteliy GmbH,+49172459728,Founder\n"
        )
        result = process_csv(csv_in)
        self.assertEqual(result["status"], "ok", result.get("message"))
        row = next(csv.DictReader(io.StringIO(result["csv"])))
        self.assertEqual(row["Phone"], "+49172459728")

    def test_the_two_legal_states_pass(self):
        self.assertEqual(check_phone_states(self._frame("+12065550100", STATUS_VALID)), [])
        self.assertEqual(
            check_phone_states(self._frame("2065550100", STATUS_NEEDS_REVIEW)), []
        )


class ExcelRenderingTests(unittest.TestCase):
    """
    Excel and Sheets parse a leading +, -, = or @ as a formula, so bare E.164
    displays as 12065550100. The write-back must stay bare for Salesforce; the
    review copy carries the guard instead.
    """

    @classmethod
    def setUpClass(cls):
        cls.result = process_csv(FIXTURE.read_text(encoding="utf-8"))
        assert cls.result["status"] == "ok", cls.result.get("message")

    def _phones(self, key):
        return [
            row["Phone"]
            for row in csv.DictReader(io.StringIO(self.result[key]))
            if row.get("Phone")
        ]

    def test_the_salesforce_writeback_keeps_bare_e164(self):
        phones = self._phones("csv")
        self.assertTrue(phones)
        for phone in phones:
            with self.subTest(phone=phone):
                self.assertRegex(phone, r"^(\+\d{6,15}|[\d\- ()./]+)$")
                self.assertNotIn('="', phone)

    def test_the_review_copy_guards_every_plus_prefixed_value(self):
        for phone in self._phones("excel_csv"):
            with self.subTest(phone=phone):
                if phone.startswith('="'):
                    self.assertRegex(phone, r'^="\+\d{6,15}"$')
                else:
                    self.assertFalse(phone.startswith("+"))

    def test_the_two_files_describe_the_same_numbers(self):
        def unwrap(value):
            return value[2:-1] if value.startswith('="') else value

        self.assertEqual(
            self._phones("csv"), [unwrap(v) for v in self._phones("excel_csv")]
        )

    def test_the_review_copy_is_bom_prefixed_and_the_writeback_is_not(self):
        # Without a BOM Excel guesses a legacy codepage and mojibakes accents.
        self.assertTrue(self.result["excel_csv"].startswith("\ufeff"))
        self.assertFalse(self.result["csv"].startswith("\ufeff"))

    def test_accented_names_survive_both_files(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Company,Phone,Title\n"
            "00QZ2,Peter,König,p@asml.com,ASML,+31402683000,Head Of IT\n"
        )
        result = process_csv(csv_in)
        self.assertEqual(result["status"], "ok", result.get("message"))
        for key in ("csv", "excel_csv"):
            with self.subTest(file=key):
                text = result[key].lstrip("\ufeff")
                row = next(csv.DictReader(io.StringIO(text)))
                self.assertEqual(row["LastName"], "König")

    def test_only_phone_columns_are_guarded(self):
        for row in csv.DictReader(io.StringIO(self.result["excel_csv"])):
            for column, value in row.items():
                if column in ("Phone", "MobilePhone"):
                    continue
                with self.subTest(column=column):
                    self.assertFalse(str(value).startswith('="'), value)

    def test_the_guard_only_fires_on_formula_leading_characters(self):
        from pipeline.tools import _excel_text_guard

        self.assertEqual(_excel_text_guard("+12065550100"), '="+12065550100"')
        self.assertEqual(_excel_text_guard("2065550100"), "2065550100")
        self.assertEqual(_excel_text_guard(""), "")
        self.assertEqual(_excel_text_guard("(206) 555-0100"), "(206) 555-0100")


class UnvalidatedInternationalPhoneTests(unittest.TestCase):
    """
    A +CC number libphonenumber rejects is kept for review, but it must still
    be written in a shape the Excel-formula invariant accepts. '+971 4 123 4567'
    used to reach the gate with spaces intact and hard-fail the whole batch.
    """

    UNVALIDATED = "+971 4 123 4567"

    def _run(self, raw):
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company,Country\n"
            f'00QZ9,Pat,Lee,pat@acme.com,"{raw}",Acme Corp,\n'
        )
        return process_csv(csv_in)

    def test_regrouping_keeps_every_digit_and_the_country_code(self):
        from pipeline.phone import excel_safe_international

        self.assertEqual(
            excel_safe_international("+971 4 123 4567"), "+971-4-123-4567"
        )
        self.assertEqual(
            excel_safe_international("+1 (206) 555-0100"), "+1-206-555-0100"
        )
        self.assertEqual(digits_only(excel_safe_international(self.UNVALIDATED)),
                         digits_only(self.UNVALIDATED))

    def test_already_safe_and_national_values_are_untouched(self):
        from pipeline.phone import excel_safe_international

        for value in ("+12065550100", "+971-4-123-4567", "2065550100",
                      "(206) 555-0100", "022-1234-5678", ""):
            with self.subTest(value=value):
                self.assertEqual(excel_safe_international(value), value)

    def test_a_leading_minus_at_or_equals_is_dropped_not_kept(self):
        from pipeline.phone import excel_safe_international

        # Only '+' carries dialling meaning; the rest are spreadsheet artifacts.
        for value in ("-971 4 123 4567", "=971 4 123 4567", "@971 4 123 4567"):
            with self.subTest(value=value):
                self.assertEqual(excel_safe_international(value), "971-4-123-4567")
        self.assertEqual(excel_safe_international("+971 4 123 4567"), "+971-4-123-4567")

    def test_a_value_with_no_digits_at_all_becomes_blank(self):
        from pipeline.phone import excel_safe_international

        for value in ("+", "-", "=", "@", "- - -"):
            with self.subTest(value=value):
                self.assertEqual(excel_safe_international(value), "")

    def test_a_minus_prefixed_number_does_not_block_the_batch(self):
        result = self._run("-971 4 123 4567")
        self.assertEqual(result["status"], "ok", result.get("message"))
        row = next(csv.DictReader(io.StringIO(result["csv"])))
        self.assertFalse(phone_looks_like_excel_formula(row["Phone"]), row["Phone"])
        self.assertEqual(digits_only(row["Phone"]), "97141234567")

    def test_the_batch_is_not_blocked(self):
        result = self._run(self.UNVALIDATED)
        self.assertEqual(result["status"], "ok", result.get("message"))

    def test_the_written_value_cannot_be_read_as_a_formula(self):
        result = self._run(self.UNVALIDATED)
        self.assertEqual(result["status"], "ok", result.get("message"))
        row = next(csv.DictReader(io.StringIO(result["csv"])))
        self.assertFalse(phone_looks_like_excel_formula(row["Phone"]), row["Phone"])
        self.assertEqual(check_records([row]), [])

    def test_the_number_is_still_flagged_for_review_not_silently_fixed(self):
        result = self._run(self.UNVALIDATED)
        self.assertEqual(result["status"], "ok", result.get("message"))
        row = next(csv.DictReader(io.StringIO(result["csv"])))
        self.assertEqual(digits_only(row["Phone"]), digits_only(self.UNVALIDATED))
        self.assertNotRegex(row["Phone"], E164_RE)
        self.assertEqual(result["summary"]["phone_states"].get(STATUS_NEEDS_REVIEW), 1)

    def test_excel_markers_in_front_of_an_unvalidated_number_are_handled(self):
        for prefix in ("'", "=", "\t"):
            with self.subTest(prefix=prefix):
                result = self._run(prefix + self.UNVALIDATED)
                self.assertEqual(result["status"], "ok", result.get("message"))
                row = next(csv.DictReader(io.StringIO(result["csv"])))
                self.assertFalse(phone_looks_like_excel_formula(row["Phone"]))
                self.assertFalse(phone_has_text_marker(row["Phone"]))

    def test_valid_international_numbers_still_collapse_to_e164(self):
        for raw, expected in (
            ("+44 7771 695127", "+447771695127"),
            ("+31 40 268 3000", "+31402683000"),
            ("+1 (206) 555-0100", "+12065550100"),
        ):
            with self.subTest(raw=raw):
                result = self._run(raw)
                self.assertEqual(result["status"], "ok", result.get("message"))
                row = next(csv.DictReader(io.StringIO(result["csv"])))
                self.assertEqual(row["Phone"], expected)


class RegressionGateWiringTests(unittest.TestCase):
    def test_the_current_run_matches_the_baseline(self):
        result = process_csv(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "ok", result.get("message"))
        self.assertEqual(result["field_diffs"], [])
        assert_no_regression(result["csv"])

    def test_assert_no_regression_raises_on_a_changed_field(self):
        rows = list(csv.DictReader(io.StringIO(GOLDEN.read_text(encoding="utf-8"))))
        rows[0]["Title"] = "Tampered Title"
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

        with self.assertRaises(RegressionDetected) as caught:
            assert_no_regression(out.getvalue())
        self.assertIn("Title", str(caught.exception))
        self.assertEqual(len(caught.exception.changes), 1)
        change = caught.exception.changes[0]
        self.assertEqual((change.field, change.new), ("Title", "Tampered Title"))

    def test_process_csv_blocks_when_a_field_moves(self):
        rows = list(csv.DictReader(io.StringIO(FIXTURE.read_text(encoding="utf-8"))))
        target = next(row for row in rows if row.get("Title"))
        target["Title"] = "Chief Tamper Officer"
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        tampered = out.getvalue()

        blocked = process_csv(tampered)
        self.assertEqual(blocked["status"], "error")
        self.assertIn("regression gate", blocked["message"])
        self.assertTrue(blocked["field_diffs"])

        # The same input is only allowed through by accepting the diff outright.
        accepted = process_csv(tampered, accept_changes=True)
        self.assertEqual(accepted["status"], "ok", accepted.get("message"))
        self.assertTrue(accepted["field_diffs"])

    def test_diff_reports_id_field_old_and_new(self):
        result = process_csv(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(diff_against_golden(result["csv"]), [])


class PhoneExtensionWritebackTests(unittest.TestCase):
    def test_extension_stays_off_phone_and_lands_in_the_audit_log(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company,Country\n"
            "00Q1,Pat,Lee,pat@acme.com,989.345.3660 Ext. 3151,Acme,US\n"
        )
        result = process_csv(csv_in)
        self.assertEqual(result["status"], "ok", result.get("message"))
        row = next(csv.DictReader(io.StringIO(result["csv"])))
        self.assertEqual(row["Phone"], "+19893453660")
        self.assertNotIn("PhoneExtension", row)
        self.assertNotIn("3151", row["Phone"])
        audit = {r["surviving_lead_id"]: r for r in csv.DictReader(io.StringIO(result["audit_csv"]))}
        self.assertEqual(audit["00Q1"]["phone_extension"], "3151")
        self.assertEqual(audit["00Q1"]["phone_raw"], "989.345.3660 Ext. 3151")
        self.assertEqual(audit["00Q1"]["phone_status"], STATUS_VALID)


if __name__ == "__main__":
    unittest.main()
