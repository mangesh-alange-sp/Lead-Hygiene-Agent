"""Job-title cleanup: stacked LinkedIn titles and unexplained acronyms."""

import unittest

import pandas as pd

from pipeline.normalize import is_low_quality_title, normalize_dataframe, normalize_title


class TitleCleanupRules(unittest.TestCase):
    def test_pipe_separated_title_keeps_the_primary_role(self):
        raw = (
            "Director | Security Program Leadership | "
            "Enterprise Cyber Risk, Governance, & Data Privacy"
        )
        self.assertEqual(normalize_title(raw), "Director")

    def test_hyphen_and_slash_are_cased_per_segment(self):
        self.assertEqual(normalize_title("VP-Sales"), "VP-Sales")
        self.assertIn("/", normalize_title("CIO/CISO"))

    def test_mixed_case_title_tokens_are_preserved(self):
        self.assertEqual(normalize_title("eClerx Engineer"), "eClerx Engineer")
        self.assertEqual(normalize_title("iOS Lead"), "iOS Lead")

    def test_unknown_short_acronym_is_flagged(self):
        self.assertTrue(is_low_quality_title("Ats"))
        self.assertEqual(normalize_title("Ats"), "ATS")

        df = pd.DataFrame({
            "FirstName": ["Ada"], "LastName": ["Lovelace"],
            "Email": ["ada@example.org"], "Title": ["Ats"], "Company": ["Acme"],
        })
        out, _ = normalize_dataframe(df)
        self.assertEqual(out.at[0, "Title"], "ATS")
        self.assertIn("low_quality_title", str(out.at[0, "data_quality_flags"]))
        self.assertEqual(out.at[0, "hitl_review"], "Yes")


class UnformattedPhoneFlagRules(unittest.TestCase):
    def test_unresolved_trunk_zero_is_flagged(self):
        df = pd.DataFrame({
            "FirstName": ["Stephane"], "LastName": ["Leprince"],
            "Email": ["stephane@socgen.com"], "Phone": ["0167458214"],
            "Company": ["Societe Generale"],
        })
        out, _ = normalize_dataframe(df)
        self.assertEqual(out.at[0, "Phone"], "0167458214")
        self.assertIn("unformatted_phone", str(out.at[0, "data_quality_flags"]))
        self.assertEqual(out.at[0, "hitl_review"], "Yes")

    def test_french_country_formats_the_trunk_zero_number(self):
        df = pd.DataFrame({
            "FirstName": ["Stephane"], "LastName": ["Leprince"],
            "Email": ["stephane@socgen.com"], "Phone": ["0167458214"],
            "Company": ["Societe Generale"], "Country": ["France"],
        })
        out, _ = normalize_dataframe(df)
        self.assertEqual(out.at[0, "Phone"], "+33167458214")
        self.assertNotIn("unformatted_phone", str(out.at[0, "data_quality_flags"]))


if __name__ == "__main__":
    unittest.main()
