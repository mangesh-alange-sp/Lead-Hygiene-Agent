"""
Rule-based tests for the libphonenumber-backed phone resolver.

Cases are generated from data/pipeline_config.json, so every calling code the
pipeline claims to support is exercised instead of a handful of known rows.

The two guarantees under test:
  * a country code present on input is never lost, and
  * a valid number is always strict E.164, an unresolved one is always the
    untouched original plus phone_status=needs_review.
"""

import unittest

from pipeline.config import CALLING_TO_REGION, CC_NATIONAL_LEN, REGION_CALLING
from pipeline.invariants import phone_lost_country_code
from pipeline.phone import (
    DEFAULT_NATIONAL_LEN,
    E164_RE,
    MIN_DIGITS_FOR_CC_PREFIX,
    STATUS_NEEDS_REVIEW,
    STATUS_NONE,
    STATUS_UNPARSEABLE,
    STATUS_VALID,
    classify_phone,
    format_cc_national,
    infer_region,
    is_valid_nanp,
    normalize_phone,
    parse_phone,
)

CALLING_CODES = sorted(set(REGION_CALLING.values()), key=len)
# Digits that keep every generated number structurally plausible: no leading 0,
# and no 0/1 in the NANP area-code or exchange positions.
NATIONAL_SEED = "923456789012345"


def _national(cc: str, target_total: int = MIN_DIGITS_FOR_CC_PREFIX) -> str:
    low, high = CC_NATIONAL_LEN.get(cc, DEFAULT_NATIONAL_LEN)
    length = min(max(low, target_total - len(cc)), high)
    return NATIONAL_SEED[:length]


class OutputContractRules(unittest.TestCase):
    """Exactly two shapes are allowed to leave this module."""

    def test_every_result_is_e164_or_the_untouched_original(self):
        raws = [
            "+44-7771-695-127", "2065550100", "099300286056", "022-1234-5678",
            "98006498", "0167458214", "+31 40 268 3000", "819012345678",
            "(206) 555-0100", "12065550100", "(+1) 206-555-0100",
        ]
        for raw in raws:
            with self.subTest(raw=raw):
                result = parse_phone(raw, country="US" if raw == "2065550100" else "")
                self.assertIn(result["status"], (STATUS_VALID, STATUS_NEEDS_REVIEW))
                if result["status"] == STATUS_VALID:
                    self.assertRegex(result["value"], E164_RE)
                    self.assertNotIn("-", result["value"])
                    self.assertNotIn("(", result["value"])
                    self.assertNotIn(" ", result["value"])
                else:
                    self.assertEqual(result["value"], raw.strip())

    def test_raw_value_is_always_preserved(self):
        for raw in ("+44-7771-695-127", "099300286056", "12", ""):
            with self.subTest(raw=raw):
                self.assertEqual(parse_phone(raw)["raw"], raw)


class CallingCodeRules(unittest.TestCase):
    def test_explicit_international_never_loses_its_calling_code(self):
        for cc in CALLING_CODES:
            national = _national(cc)
            for raw in (f"+{cc}{national}", f"+{cc} {national}", f"00{cc}{national}"):
                with self.subTest(cc=cc, raw=raw):
                    result = normalize_phone(raw)
                    self.assertIsNotNone(result)
                    self.assertFalse(
                        phone_lost_country_code(raw, result),
                        f"{raw} -> {result} dropped calling code {cc}",
                    )

    def test_bare_digits_with_a_leading_calling_code_keep_it(self):
        for cc in CALLING_CODES:
            national = _national(cc)
            digits = cc + national
            if len(digits) < MIN_DIGITS_FOR_CC_PREFIX:
                continue  # shorter than a NANP number: genuinely ambiguous
            if len(digits) == 10 and is_valid_nanp(digits):
                continue  # NANP shape wins by design
            with self.subTest(cc=cc, digits=digits):
                result = parse_phone(digits)
                if result["status"] == STATUS_VALID:
                    self.assertTrue(result["value"].startswith(f"+{cc}"))
                else:
                    self.assertEqual(result["value"], digits)

    def test_email_tld_signal_gives_the_region_hint(self):
        cases = {
            "raj@corp.co.in": "+91",
            "hans@buchert.de": "+49",
            "sato@corp.co.jp": "+81",
            "sam@corp.co.uk": "+44",
        }
        for email, prefix in cases.items():
            with self.subTest(email=email):
                result = parse_phone("9234567890", email=email)
                if result["status"] == STATUS_VALID:
                    self.assertTrue(result["value"].startswith(prefix), f"{email} -> {result}")

    def test_region_hint_is_rejected_when_it_does_not_validate(self):
        # 12 digits: too long for a real Indian mobile even with +91.
        result = parse_phone("099300286056", email="pat@corp.in")
        self.assertEqual(result["status"], STATUS_NEEDS_REVIEW)
        self.assertEqual(result["value"], "099300286056")

    def test_region_inference_never_defaults_to_us(self):
        self.assertEqual(infer_region(email="jane@amazon.com"), "")
        self.assertEqual(infer_region(), "")
        self.assertEqual(infer_region(email="", country="", phone="0923456789"), "")

    def test_no_country_evidence_falls_back_to_a_validated_nanp_parse(self):
        # .com carries no region, so the NANP fallback is the last step tried.
        result = parse_phone("2065550100", email="jane@amazon.com")
        self.assertEqual(result["status"], STATUS_VALID)
        self.assertEqual(result["value"], "+12065550100")
        self.assertIn("no other country evidence", result["reason"])
        self.assertEqual(parse_phone("2065550100", country="US")["value"], "+12065550100")


class NanpRules(unittest.TestCase):
    def test_invalid_nanp_is_never_force_formatted(self):
        for raw in ("092-266-8345", "016-745-8214", "+1-092-266-8345", "+1-016-745-8214"):
            with self.subTest(raw=raw):
                result = parse_phone(raw, email="a@b.us", country="United States")
                self.assertNotEqual(result["status"], STATUS_VALID)

    def test_valid_nanp_formats_to_e164(self):
        for raw in ("+1-206-555-0100", "12065550100", "(+1) 206-555-0100"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_phone(raw), "+12065550100")
        for raw in ("2065550100", "(206) 555-0100", "206.555.0100"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_phone(raw, country="US"), "+12065550100")

    def test_is_valid_nanp_rule(self):
        for national in ("0234567890", "1234567890", "9230456789", "9231456789"):
            self.assertFalse(is_valid_nanp(national), national)
        for national in ("2065550100", "9234567890"):
            self.assertTrue(is_valid_nanp(national), national)


class SpecCaseRules(unittest.TestCase):
    """The exact cases named in the improvement spec."""

    def test_iaw_uk_number_validates_and_keeps_its_country_code(self):
        result = parse_phone("+44-7771-695-127")
        self.assertEqual(result["status"], STATUS_VALID)
        self.assertEqual(result["value"], "+447771695127")

    def test_indian_number_one_digit_too_long_needs_review(self):
        result = parse_phone("099300286056", email="gokul.s@yahoo.in")
        self.assertEqual(result["status"], STATUS_NEEDS_REVIEW)
        self.assertEqual(result["value"], "099300286056")
        self.assertNotIn("+", result["value"])

    def test_singapore_number_resolves_only_with_sg_evidence(self):
        self.assertEqual(
            parse_phone("98006498", email="user@sprinklr.com.sg")["value"], "+6598006498"
        )
        bare = parse_phone("98006498", email="user@sprinklr.com", company="Sprinklr")
        self.assertEqual(bare["status"], STATUS_NEEDS_REVIEW)
        self.assertEqual(bare["value"], "98006498")


class OutputShapeRules(unittest.TestCase):
    def test_never_starts_with_a_text_marker(self):
        for raw in ("'+1-206-555-0100", "\t+1-206-555-0100", "'2065550100", "=+49-170-111-2223"):
            with self.subTest(raw=raw):
                result = normalize_phone(raw) or ""
                self.assertFalse(result.startswith("'"))
                self.assertFalse(result.startswith("\t"))
                self.assertFalse(result[:1] in "=@")

    def test_marker_does_not_change_the_result(self):
        for raw in ("+1-206-555-0100", "819234567890", "0923456789"):
            self.assertEqual(normalize_phone("'" + raw), normalize_phone(raw), raw)

    def test_format_cc_national_returns_e164_or_nothing(self):
        self.assertEqual(format_cc_national("1", "2065550100"), "+12065550100")
        self.assertEqual(format_cc_national("33", "167458214"), "+33167458214")
        self.assertEqual(format_cc_national("1", "0922668345"), "")


class ClassifyRules(unittest.TestCase):
    def test_classes(self):
        self.assertEqual(classify_phone(""), "empty")
        self.assertEqual(classify_phone("12"), "garbage")
        self.assertEqual(classify_phone("+44-7771-695-127"), "has_cc")
        self.assertEqual(classify_phone("98006498"), "missing_cc")
        self.assertEqual(classify_phone("2065550100 ext. 12"), "noise")


class JunkRules(unittest.TestCase):
    # Nothing was supplied, so there is no phone to hold a state.
    BLANK = ("", "   ", "n/a", "unknown", None)
    # Something was supplied but carries no recoverable number.
    GARBAGE = ("12", "123", "0000000000", "000-000-000", "1234567890",
               "9999999999", "5551234567", "555-123-4567", "1-555-123-4567")

    def test_junk_values_return_none(self):
        for raw in self.BLANK + self.GARBAGE:
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_phone(raw))

    def test_a_blank_field_carries_no_status_at_all(self):
        for raw in self.BLANK:
            with self.subTest(raw=raw):
                self.assertEqual(parse_phone(raw)["status"], STATUS_NONE)

    def test_supplied_garbage_is_unparseable(self):
        for raw in self.GARBAGE:
            with self.subTest(raw=raw):
                self.assertEqual(parse_phone(raw)["status"], STATUS_UNPARSEABLE)

    def test_absurdly_long_numbers_rejected(self):
        self.assertIsNone(normalize_phone("1234567890123456789"))


class IdempotenceRules(unittest.TestCase):
    def test_running_twice_changes_nothing(self):
        raws = [
            "2065550100", "+49 170 111 2223", "092-266-8345", "0923456789",
            "819234567890", "98324009", "+31 40 268 3000", "099300286056",
        ]
        for raw in raws:
            once = normalize_phone(raw)
            if once is None:
                continue
            with self.subTest(raw=raw):
                self.assertEqual(normalize_phone(once), once)

    def test_idempotent_with_signals(self):
        once = normalize_phone("092-266-8345", email="kt.lin@corp.com.tw")
        self.assertEqual(normalize_phone(once, email="kt.lin@corp.com.tw"), once)


class CallingCodeMapIntegrity(unittest.TestCase):
    def test_every_calling_code_maps_back_to_a_region(self):
        for cc in CALLING_CODES:
            self.assertIn(cc, CALLING_TO_REGION, cc)

    def test_shared_codes_resolve_deterministically(self):
        self.assertEqual(CALLING_TO_REGION["1"], "US")


if __name__ == "__main__":
    unittest.main()
