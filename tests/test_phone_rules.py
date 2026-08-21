"""
Rule-based tests for normalize_phone.

Cases are generated from data/pipeline_config.json, so every calling code the
pipeline claims to support is exercised instead of a handful of known rows.
"""

import unittest

from pipeline.config import CALLING_TO_REGION, CC_NATIONAL_LEN, REGION_CALLING
from pipeline.phone import (
    DEFAULT_NATIONAL_LEN,
    MIN_DIGITS_FOR_CC_PREFIX,
    format_cc_national,
    infer_region,
    is_valid_nanp,
    normalize_phone,
)

CALLING_CODES = sorted(set(REGION_CALLING.values()), key=len)
# Digits that keep every generated number structurally plausible: no leading 0,
# and no 0/1 in the NANP area-code or exchange positions.
NATIONAL_SEED = "923456789012345"


def _national(cc: str, target_total: int = MIN_DIGITS_FOR_CC_PREFIX) -> str:
    low, high = CC_NATIONAL_LEN.get(cc, DEFAULT_NATIONAL_LEN)
    length = min(max(low, target_total - len(cc)), high)
    return NATIONAL_SEED[:length]


class CallingCodeRules(unittest.TestCase):
    def test_explicit_international_keeps_its_calling_code(self):
        for cc in CALLING_CODES:
            national = _national(cc)
            for raw in (f"+{cc}{national}", f"+{cc} {national}", f"00{cc}{national}"):
                with self.subTest(cc=cc, raw=raw):
                    result = normalize_phone(raw)
                    self.assertIsNotNone(result)
                    self.assertTrue(
                        result.startswith(f"(+{cc})"),
                        f"{raw} -> {result}, expected (+{cc}) prefix",
                    )

    def test_bare_digits_with_leading_calling_code_keep_it(self):
        for cc in CALLING_CODES:
            national = _national(cc)
            if len(cc) + len(national) < MIN_DIGITS_FOR_CC_PREFIX:
                continue  # shorter than a NANP number: genuinely ambiguous
            digits = cc + national
            if len(digits) == 10 and is_valid_nanp(digits):
                continue  # NANP shape wins by design
            with self.subTest(cc=cc, digits=digits):
                result = normalize_phone(digits)
                self.assertTrue(
                    result.startswith(f"(+{cc})"),
                    f"{digits} -> {result}, expected (+{cc}) prefix",
                )

    def test_email_tld_signal_beats_nanp_shape(self):
        cases = {
            "kt.lin@example.com.tw": "(+886)",
            "raj@larsentoubro.co.in": "(+91)",
            "hans@buchert.de": "(+49)",
            "sato@corp.co.jp": "(+81)",
            "sam@corp.co.uk": "(+44)",
        }
        for email, prefix in cases.items():
            with self.subTest(email=email):
                result = normalize_phone("9234567890", email=email)
                self.assertTrue(result.startswith(prefix), f"{email} -> {result}")

    def test_country_field_signal(self):
        for country, prefix in (("France", "(+33)"), ("Taiwan", "(+886)"), ("India", "(+91)"), ("Germany", "(+49)")):
            with self.subTest(country=country):
                result = normalize_phone("0923456789", country=country)
                self.assertTrue(result.startswith(prefix), f"{country} -> {result}")

    def test_region_inference_never_defaults_to_us(self):
        self.assertEqual(infer_region(email="jane@amazon.com"), "")
        self.assertEqual(infer_region(), "")
        self.assertEqual(infer_region(email="", country="", phone="0923456789"), "")


class NanpRules(unittest.TestCase):
    def test_output_never_has_invalid_nanp_area_or_exchange(self):
        bad = [
            "092-266-8345", "016-745-8214", "+1-092-266-8345", "+1-016-745-8214",
            "1234567", "0123456789", "5551234567", "0922668345",
        ]
        for raw in bad:
            with self.subTest(raw=raw):
                result = normalize_phone(raw) or ""
                if result.startswith("(+1)"):
                    rest = result.split(") ", 1)[-1]
                    area, exchange = rest.split("-")[0], rest.split("-")[1]
                    self.assertNotIn(area[0], "01", f"{raw} -> {result}")
                    self.assertNotIn(exchange[0], "01", f"{raw} -> {result}")

    def test_no_us_country_signal_can_force_an_invalid_nanp(self):
        for raw in ("092-266-8345", "016-745-8214"):
            with self.subTest(raw=raw):
                result = normalize_phone(raw, email="a@b.us", country="United States") or ""
                self.assertFalse(result.startswith("(+1) 0"))
                self.assertFalse(result.startswith("(+1) 1"))

    def test_valid_nanp_is_formatted(self):
        for raw in ("2065550100", "(206) 555-0100", "206.555.0100", "+1-206-555-0100", "12065550100", "(+1) 206-555-0100"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_phone(raw), "(+1) 206-555-0100")

    def test_is_valid_nanp_rule(self):
        for national in ("0234567890", "1234567890", "9230456789", "9231456789"):
            self.assertFalse(is_valid_nanp(national), national)
        for national in ("2065550100", "9234567890"):
            self.assertTrue(is_valid_nanp(national), national)


class OutputShapeRules(unittest.TestCase):
    def test_never_starts_with_a_text_marker(self):
        raws = [
            "'+1-206-555-0100", "\t+1-206-555-0100", "'2065550100",
            "'+886-92-266-8345", "=+49-170-111-2223",
        ]
        for raw in raws:
            with self.subTest(raw=raw):
                result = normalize_phone(raw) or ""
                self.assertFalse(result.startswith("'"))
                self.assertFalse(result.startswith("\t"))
                self.assertFalse(result[:1] in "+=-@")

    def test_marker_does_not_change_the_result(self):
        for raw in ("+1-206-555-0100", "819234567890", "0923456789"):
            self.assertEqual(normalize_phone("'" + raw), normalize_phone(raw), raw)

    def test_formatted_output_uses_one_shape(self):
        result = normalize_phone("+49 170 111 2223")
        self.assertRegex(result, r"^\(\+\d{1,3}\) \d+(-\d+)*$")

    def test_format_cc_national_groups_last_seven_digits(self):
        self.assertEqual(format_cc_national("1", "2065550100"), "(+1) 206-555-0100")
        self.assertEqual(format_cc_national("33", "167458214"), "(+33) 1-67-45-82-14")
        self.assertEqual(format_cc_national("1", "0922668345"), "")


class NoSignalRules(unittest.TestCase):
    def test_unresolvable_number_returns_the_cleaned_original(self):
        for raw in ("98324009", "12 34 56 78"):
            with self.subTest(raw=raw):
                result = normalize_phone(raw)
                self.assertEqual(result, raw.strip())

    def test_cleaned_original_is_stripped_of_markers_only(self):
        self.assertEqual(normalize_phone("'98324009"), "98324009")
        self.assertEqual(normalize_phone("  98324009  "), "98324009")

    def test_no_signal_never_invents_a_calling_code(self):
        for raw in ("98324009", "12 34 56 78"):
            self.assertNotIn("+", normalize_phone(raw), raw)

    def test_trunk_zero_formats_when_country_is_known(self):
        self.assertEqual(normalize_phone("0167458214", country="France"), "(+33) 1-67-45-82-14")

    def test_trunk_zero_without_country_is_not_guessed(self):
        for raw in ("0167458214", "099300286056"):
            with self.subTest(raw=raw):
                result = normalize_phone(raw)
                self.assertEqual(result, raw)
                self.assertFalse(result.startswith("(+"))


class JunkRules(unittest.TestCase):
    def test_junk_values_return_none(self):
        for raw in ("", "   ", "12", "123", "0000000000", "000-000-000",
                    "1234567890", "9999999999", "n/a", "unknown", None,
                    "5551234567", "555-123-4567", "1-555-123-4567"):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_phone(raw))

    def test_absurdly_long_numbers_rejected(self):
        self.assertIsNone(normalize_phone("1234567890123456789"))


class IdempotenceRules(unittest.TestCase):
    def test_running_twice_changes_nothing(self):
        raws = [
            "2065550100", "+49 170 111 2223", "092-266-8345", "0923456789",
            "819234567890", "98324009", "+31 40 268 3000",
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
