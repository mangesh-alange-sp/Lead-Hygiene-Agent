"""
Rule-based tests for normalize_company_casing.

The acronym and legal-suffix allowlists come from data/pipeline_config.json, so
adding an acronym there is automatically covered here.
"""

import unittest

from pipeline.casing import normalize_company_casing
from pipeline.config import COMPANY_ACRONYMS, COMPANY_MIXED_CASE, LEGAL_SUFFIX_DISPLAY

SURROUNDINGS = (
    "{token}",
    "{token} Group",
    "The {token} Company",
    "{token} Information Systems",
    "Global {token} Holdings Inc.",
    "acme {token} partners llc",
)


class AcronymRules(unittest.TestCase):
    def test_configured_acronyms_stay_uppercase_in_any_surrounding_text(self):
        for acronym in COMPANY_ACRONYMS:
            for pattern in SURROUNDINGS:
                for variant in (acronym, acronym.lower(), acronym.title(), acronym.upper()):
                    raw = pattern.format(token=variant)
                    with self.subTest(acronym=acronym, raw=raw):
                        result = normalize_company_casing(raw)
                        self.assertIn(
                            acronym, result.split(),
                            f"{raw!r} -> {result!r}, expected token {acronym}",
                        )

    def test_acronym_tokens_are_never_partially_lowercased(self):
        for acronym in COMPANY_ACRONYMS:
            result = normalize_company_casing(f"{acronym.lower()} solutions")
            token = result.split()[0]
            with self.subTest(acronym=acronym):
                self.assertEqual(token, token.upper(), f"{acronym} -> {result}")

    def test_mixed_case_brands_keep_their_shape(self):
        for brand in COMPANY_MIXED_CASE:
            for variant in (brand.lower(), brand.upper(), brand):
                result = normalize_company_casing(f"{variant} Global")
                with self.subTest(brand=brand, variant=variant):
                    self.assertIn(brand, result.split())

    def test_acronym_with_trailing_punctuation(self):
        self.assertEqual(normalize_company_casing("nci, information systems"), "NCI, Information Systems")


class LegalSuffixRules(unittest.TestCase):
    def test_configured_suffixes_render_canonically(self):
        for key, display in LEGAL_SUFFIX_DISPLAY.items():
            raw = f"Northwind {key}"
            with self.subTest(suffix=key):
                self.assertEqual(normalize_company_casing(raw), f"Northwind {display}")

    def test_suffix_with_source_period(self):
        self.assertEqual(normalize_company_casing("northwind inc."), "Northwind Inc.")
        self.assertEqual(normalize_company_casing("northwind gmbh"), "Northwind GmbH")
        self.assertEqual(normalize_company_casing("asml netherlands b.v."), "ASML Netherlands B.V.")


class DomainTokenRules(unittest.TestCase):
    def test_domain_like_tokens_keep_a_lowercase_tld(self):
        cases = {
            "Amazon.Com Inc.": "Amazon.com Inc.",
            "amazon.com inc": "Amazon.com Inc.",
            "BOOKING.COM": "Booking.com",
            "siemens.DE gmbh": "Siemens.de GmbH",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_company_casing(raw), expected)

    def test_initialisms_are_not_treated_as_domains(self):
        self.assertEqual(
            normalize_company_casing("r.o.c military academy"),
            "R.O.C Military Academy",
        )


class GeneralCasingRules(unittest.TestCase):
    def test_short_all_caps_tokens_stay_acronyms(self):
        self.assertEqual(normalize_company_casing("BFG"), "BFG")
        self.assertEqual(normalize_company_casing("bfg"), "BFG")

    def test_ampersand_initialisms_are_joined(self):
        for raw in ("l&t construction", "L and T Construction", "l & t construction"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_company_casing(raw), "L&T Construction")

    def test_hyphenated_corp_suffix_keeps_its_case(self):
        self.assertEqual(
            normalize_company_casing("Associated Banc-Corp"),
            "Associated Banc-Corp",
        )
        self.assertEqual(
            normalize_company_casing("associated banc-corp"),
            "Associated Banc-Corp",
        )

    def test_small_words_lowercased_in_the_middle(self):
        self.assertEqual(
            normalize_company_casing("bank OF america"),
            "Bank of America",
        )

    def test_scottish_and_irish_prefixes(self):
        self.assertEqual(normalize_company_casing("mcdonalds"), "McDonalds")
        self.assertEqual(normalize_company_casing("o'reilly media"), "O'Reilly Media")

    def test_blank_input(self):
        for raw in ("", "   ", None, "n/a"):
            self.assertEqual(normalize_company_casing(raw), "")

    def test_idempotent(self):
        raws = [
            "Amazon.Com Inc.", "l&t construction", "asml netherlands b.v.",
            "bcbs association", "r.o.c military academy", "cea", "nci information systems",
            "PROCTER AND GAMBLE", "AMGEN INC.",
        ]
        for raw in raws:
            once = normalize_company_casing(raw)
            with self.subTest(raw=raw):
                self.assertEqual(normalize_company_casing(once), once)

    def test_no_lowercase_letter_follows_a_dot_outside_the_allowlist(self):
        from pipeline.invariants import company_has_bad_dotted_case

        raws = [
            "Amazon.Com Inc.", "r.o.c military academy", "asml netherlands b.v.",
            "acme.corp holdings", "s.r.l. milano", "u.s. steel",
        ]
        for raw in raws:
            result = normalize_company_casing(raw)
            with self.subTest(raw=raw):
                self.assertFalse(company_has_bad_dotted_case(result), f"{raw} -> {result}")


class BrandCanonicalRules(unittest.TestCase):
    def test_procter_and_gamble_unifies(self):
        from pipeline.normalize import normalize_company

        for raw in ("PROCTER AND GAMBLE", "Procter & Gamble", "P&G", "pg"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_company(raw), "Procter & Gamble")

    def test_amgen_title_cases_and_canonicalizes_inc(self):
        from pipeline.normalize import normalize_company

        for raw in ("AMGEN INC.", "Amgen Inc.", "Amgen Inc..", "amgen"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_company(raw), "Amgen Inc.")

    def test_alias_table_is_the_company_lookup(self):
        from pipeline.taxonomy import resolve_company_alias

        self.assertEqual(resolve_company_alias("MCDONALDS CORPORATION"), "McDonald's")
        self.assertEqual(resolve_company_alias("p&g"), "Procter & Gamble")
        self.assertEqual(resolve_company_alias("Some Unknown LLC"), "")


if __name__ == "__main__":
    unittest.main()
