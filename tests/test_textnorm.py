"""
Tests for the shared string utilities. These have their own suite because
phone, casing, website, and dedupe all depend on them.
"""

import unittest

from pipeline.textnorm import (
    alias_key,
    cell,
    collapse_whitespace,
    digits_only,
    email_domain,
    email_local,
    email_local_fold,
    fold_text,
    strip_excel_artifacts,
)


class CellTests(unittest.TestCase):
    def test_blank_tokens_become_empty(self):
        for value in ("", "  ", "N/A", "n/a", "NULL", "None", "nan", "-", "--", "Unknown"):
            self.assertEqual(cell(value), "", value)

    def test_real_values_survive(self):
        for value in ("Na", "0", "None Of The Above", "Null Island"):
            self.assertNotEqual(cell(value), "", value)

    def test_none_and_nan(self):
        self.assertEqual(cell(None), "")
        self.assertEqual(cell(float("nan")), "")

    def test_control_characters_removed(self):
        self.assertEqual(cell("Acme\x00 Corp\x1f"), "Acme Corp")


class ExcelArtifactTests(unittest.TestCase):
    def test_leading_markers_removed(self):
        for value in ("'+1-206-555-0100", "\t+1-206-555-0100", "=+1-206-555-0100", "''+1-206-555-0100"):
            self.assertEqual(strip_excel_artifacts(value), "+1-206-555-0100", value)

    def test_plus_and_digits_preserved(self):
        self.assertEqual(strip_excel_artifacts("+49 170 111 2223"), "+49 170 111 2223")

    def test_idempotent(self):
        once = strip_excel_artifacts("'+886-92-266-8345")
        self.assertEqual(strip_excel_artifacts(once), once)


class FoldTests(unittest.TestCase):
    def test_diacritics_folded(self):
        self.assertEqual(fold_text("Büchert"), fold_text("Buchert"))
        self.assertEqual(fold_text("José"), fold_text("Jose"))
        self.assertEqual(fold_text("Stéphane"), fold_text("Stephane"))
        self.assertEqual(fold_text("Straße"), "strasse")

    def test_apostrophes_and_conjunctions(self):
        self.assertEqual(fold_text("McDonald's"), fold_text("McDonalds"))
        self.assertEqual(fold_text("Acme & Co"), fold_text("Acme and Co"))

    def test_alias_key_unifies_ampersand_and_and(self):
        self.assertEqual(alias_key("PROCTER AND GAMBLE"), alias_key("Procter & Gamble"))
        self.assertEqual(alias_key("AMGEN INC."), "amgen inc")

    def test_folding_applies_to_email_local_part(self):
        self.assertEqual(
            email_local_fold("j.büchert@example.de"),
            email_local_fold("j.buchert@example.de"),
        )


class EmailPartTests(unittest.TestCase):
    def test_domain_and_local(self):
        self.assertEqual(email_domain("Pat.Smith@Acme.COM"), "acme.com")
        self.assertEqual(email_local("Pat.Smith@Acme.COM"), "pat.smith")

    def test_missing_at_sign(self):
        self.assertEqual(email_domain("not-an-email"), "")


class MiscTests(unittest.TestCase):
    def test_digits_only(self):
        self.assertEqual(digits_only("+1 (206) 555-0100"), "12065550100")

    def test_collapse_whitespace(self):
        self.assertEqual(collapse_whitespace("  Acme   Corp \n"), "Acme Corp")


if __name__ == "__main__":
    unittest.main()
