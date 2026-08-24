"""
End-to-end tests for validate → normalize → dedupe.

The hand-built fixture is a bootstrap: its tricky rows are inputs that exercise
the rules, not a frozen expected-output file. Per-rule coverage lives in
test_phone_rules / test_casing_rules / test_website_rules / test_dedupe_rules.
"""

import csv
import io
import unittest
from pathlib import Path

import pandas as pd

from pipeline.invariants import check_records
from pipeline.tools import process_csv
from pipeline.validate import validate_dataframe

FIXTURES = Path(__file__).parent / "fixtures"
INPUT_CSV = FIXTURES / "lead_data_with_duplicates.csv"


def _load_result():
    result = process_csv(INPUT_CSV.read_text(encoding="utf-8"))
    assert result["status"] == "ok", result.get("message")
    return result, pd.read_csv(io.StringIO(result["csv"]), dtype=str).fillna("")


class SchemaTests(unittest.TestCase):
    def test_output_columns_match_the_source_exactly(self):
        source = pd.read_csv(INPUT_CSV, dtype=str)
        _, out = _load_result()
        self.assertEqual(list(out.columns), list(source.columns))

    def test_no_audit_columns_leak_into_the_output(self):
        result, out = _load_result()
        internal = {
            "merged_from_ids", "match_signals_used", "confidence_score",
            "data_quality_flags", "hitl_review", "decision",
        }
        self.assertTrue(internal.isdisjoint(out.columns))
        self.assertNotIn("merged_from_ids", result["csv"].splitlines()[0])

    def test_audit_information_goes_to_the_side_file(self):
        result, _ = _load_result()
        audit = list(csv.DictReader(io.StringIO(result["audit_csv"])))
        self.assertTrue(audit)
        self.assertIn("merged_from_ids", audit[0])
        self.assertIn("match_signals_used", audit[0])
        self.assertIn("match_reason", audit[0])
        self.assertEqual(result["audit_file"], "dedup_log.csv")

    def test_csv_export_hygiene(self):
        result, _ = _load_result()
        raw = result["csv"]
        self.assertFalse(raw.startswith("\ufeff"))
        header = raw.splitlines()[0]
        self.assertTrue(header.startswith("Id,"))
        self.assertNotIn('"', header)

    def test_underscore_header_is_not_quote_wrapped(self):
        csv_in = "_,Id,Email,Phone,Country\nLead,00Q1,a@acme.com,2065550100,US\n"
        result = process_csv(csv_in)
        self.assertEqual(result["status"], "ok", result.get("message"))
        self.assertEqual(result["csv"].splitlines()[0].split(",")[0], "_")


class BatchInvariantTests(unittest.TestCase):
    def test_written_batch_satisfies_every_invariant(self):
        result, out = _load_result()
        self.assertEqual(
            check_records(out.to_dict("records"), rows_in=result["leads_in"]), []
        )
        self.assertEqual(result["invariant_violations"], [])

    def test_row_count_never_grows(self):
        result, _ = _load_result()
        self.assertLessEqual(result["leads_out"], result["leads_in"])
        self.assertEqual(
            result["leads_in"] - result["leads_out"],
            result["duplicates_merged"] + result.get("records_dropped", 0),
        )

    def test_no_phone_carries_an_excel_marker(self):
        result, _ = _load_result()
        for row in csv.DictReader(io.StringIO(result["csv"])):
            phone = row.get("Phone") or ""
            self.assertFalse(phone.startswith("'"), row.get("Id"))
            if phone.startswith("+"):
                self.assertRegex(phone, r"^\+\d{6,15}$", row.get("Id"))
            else:
                self.assertFalse(bool(phone) and phone[0] in "+=-@", row.get("Id"))

    def test_invariant_gate_blocks_a_broken_transformation(self):
        from unittest.mock import patch

        with patch("pipeline.tools.strip_excel_artifacts", lambda value: "'" + str(value)):
            result = process_csv(INPUT_CSV.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "error")
        self.assertTrue(any("text marker" in v for v in result["invariant_violations"]))
        self.assertNotIn("csv", result)

    def test_pipeline_is_idempotent(self):
        result, _ = _load_result()
        second = process_csv(result["csv"])
        self.assertEqual(second["status"], "ok", second.get("message"))
        self.assertEqual(second["csv"], result["csv"])


class TrickyRowTests(unittest.TestCase):
    """The known-hard records from earlier review passes, asserted as rules."""

    @classmethod
    def setUpClass(cls):
        cls.result, out = _load_result()
        cls.rows = out.set_index("Id")

    def phone(self, lead_id):
        return str(self.rows.loc[lead_id, "Phone"])

    def test_domain_casing_in_company(self):
        self.assertEqual(self.rows.loc["00Q001", "Company"], "Amazon.com Inc.")

    def test_acronyms_survive_the_pipeline(self):
        expected = {
            "00Q017": "L&T Construction",
            "00Q018": "ASML Netherlands B.V.",
            "00Q019": "NCI Information Systems",
            "00Q020": "BCBS Association",
            "00Q023": "CEA",
        }
        for lead_id, company in expected.items():
            with self.subTest(lead_id=lead_id):
                self.assertEqual(self.rows.loc[lead_id, "Company"], company)
        self.assertTrue(str(self.rows.loc["00Q016", "Company"]).startswith("R.O.C"))

    def test_country_signals_win_over_us_default(self):
        expected = {
            "00Q011": "+886",  # Kuo-Tung Lin, Taiwan country
            "00Q012": "+33",   # Stephane Leprince, France
            "00Q018": "+31",   # Anja Visser, explicit +31
            "00Q023": "+44",   # Terry Ng, 44 prefix
            "00Q024": "+33",
            "00Q025": "+60",
            "00Q026": "+81",
            "00Q027": "+81",
        }
        for lead_id, prefix in expected.items():
            with self.subTest(lead_id=lead_id):
                self.assertTrue(
                    self.phone(lead_id).startswith(prefix),
                    f"{lead_id} -> {self.phone(lead_id)}",
                )
                self.assertFalse(self.phone(lead_id).startswith("+1"))

    def test_valid_phones_are_strict_e164(self):
        for lead_id in ("00Q001", "00Q012", "00Q023", "00Q024", "00Q027"):
            with self.subTest(lead_id=lead_id):
                self.assertRegex(self.phone(lead_id), r"^\+\d{6,15}$")

    def test_us_numbers_still_format(self):
        self.assertEqual(self.phone("00Q001"), "+12065550100")

    def test_phones_use_one_uniform_format(self):
        self.assertEqual(self.phone("00Q012"), "+33167458214")
        self.assertEqual(self.phone("00Q023"), "+447911123456")
        self.assertEqual(self.phone("00Q024"), "+33612345678")
        self.assertEqual(self.phone("00Q027"), "+81312345678")

    def test_number_that_fails_validation_keeps_its_original_value(self):
        # 022-1234-5678 is not a valid Indian number even with +91 applied.
        self.assertEqual(self.phone("00Q017"), "022-1234-5678")

    def test_mcdonalds_uses_the_brand_canonical(self):
        self.assertEqual(self.rows.loc["00Q002", "Company"], "McDonald's")

    def test_placeholder_emails_are_cleared(self):
        for lead_id in ("00Q011", "00Q014", "00Q015"):
            with self.subTest(lead_id=lead_id):
                self.assertEqual(self.rows.loc[lead_id, "Email"], "")

    def test_test_com_rows_are_dropped(self):
        self.assertNotIn("00Q013", self.rows.index)
        self.assertGreaterEqual(self.result.get("records_dropped", 0), 1)

    def test_socgen_website_is_kept_for_societe_generale(self):
        self.assertEqual(self.rows.loc["00Q012", "Website"], "https://socgen.com")

    def test_lt_email_domain_becomes_the_website(self):
        self.assertEqual(self.rows.loc["00Q017", "Website"], "https://larsentoubro.com")

    def test_title_acronyms_are_not_title_cased(self):
        self.assertEqual(self.rows.loc["00Q001", "Title"], "PM")
        self.assertEqual(self.rows.loc["00Q010", "Title"], "PM")
        self.assertEqual(self.rows.loc["00Q019", "Title"], "PM")

    def test_industry_aliases_map_to_canonical(self):
        self.assertEqual(self.rows.loc["00Q002", "Industry"], "Food & Beverage")
        self.assertEqual(self.rows.loc["00Q011", "Industry"], "Semiconductor")
        self.assertEqual(self.rows.loc["00Q009", "Industry"], "Technology")

    def test_empty_country_is_inferred_from_the_phone(self):
        self.assertEqual(self.rows.loc["00Q023", "Country"], "United Kingdom")
        self.assertEqual(self.rows.loc["00Q024", "Country"], "France")
        self.assertEqual(self.rows.loc["00Q025", "Country"], "Malaysia")
        self.assertEqual(self.rows.loc["00Q026", "Country"], "Japan")
        self.assertEqual(self.rows.loc["00Q027", "Country"], "Japan")

    def test_held_rows_are_still_normalized(self):
        self.assertEqual(self.rows.loc["00Q009", "Company"], "Acme")
        self.assertEqual(self.rows.loc["00Q009", "Industry"], "Technology")
        self.assertEqual(self.rows.loc["00Q009", "Title"], "REP")

    def test_output_keeps_source_row_order(self):
        ids = [str(i) for i in self.rows.index]
        self.assertLess(ids.index("00Q007"), ids.index("00Q010"))
        self.assertLess(ids.index("00Q007"), ids.index("00Q009"))

    def test_placeholder_contact_name_is_nulled(self):
        df = pd.DataFrame({
            "Id": ["00Q008"], "FirstName": ["No"], "LastName": ["Contact"],
            "Email": [""], "Phone": [""], "Company": ["Missing Co"],
        })
        out, _ = validate_dataframe(df)
        self.assertEqual(out.at[0, "FirstName"], "")
        self.assertEqual(out.at[0, "LastName"], "")

    def test_empty_shell_after_normalize_is_dropped(self):
        self.assertNotIn("00Q008", self.rows.index)

    def test_field_merge_keeps_the_fuller_email(self):
        self.assertEqual(self.rows.loc["00Q002", "Email"], "lee.bailey@us.mcd.com")

    def test_acronym_company_matches_its_email_domain(self):
        self.assertEqual(self.rows.loc["00Q019", "Website"], "https://nciinc.com")

    def test_company_small_words_are_lowercased(self):
        self.assertEqual(
            self.rows.loc["00Q016", "Company"],
            "R.O.C Military Academy Department of Politics",
        )

    def test_unresolvable_websites_are_nulled(self):
        # placeholder emails / personal hosts: nothing legitimate to derive.
        for lead_id in ("00Q014", "00Q015", "00Q022"):
            with self.subTest(lead_id=lead_id):
                self.assertEqual(self.rows.loc[lead_id, "Website"], "")

    def test_valid_existing_websites_are_kept(self):
        self.assertEqual(self.rows.loc["00Q010", "Website"], "https://microsoft.com")
        self.assertEqual(self.rows.loc["00Q021", "Website"], "https://oracle.com")

    def test_personal_existing_website_is_replaced_from_email(self):
        self.assertEqual(self.rows.loc["00Q007", "Website"], "https://acme.com")

    def test_garbage_name_is_nulled(self):
        self.assertEqual(self.rows.loc["00Q006", "FirstName"], "")

    def test_exact_email_duplicates_collapse(self):
        self.assertNotIn("DUPE001", self.rows.index)
        self.assertNotIn("DUPE003", self.rows.index)

    def test_shared_phone_pair_is_kept_for_review(self):
        self.assertIn("00Q004", self.rows.index)
        self.assertIn("00Q005", self.rows.index)
        self.assertGreater(self.result["hitl_records"], 0)


class NameNormalizeTests(unittest.TestCase):
    def test_honorifics_and_initials(self):
        from pipeline.normalize import normalize_name

        self.assertEqual(normalize_name("Mr. John", "Smith"), ("John", "Smith"))
        self.assertEqual(normalize_name("Jk", "Ng"), ("J.K.", "Ng"))
        self.assertEqual(normalize_name("JK", "Rowling"), ("J.K.", "Rowling"))
        self.assertEqual(normalize_name("Gokul", "S"), ("Gokul", "S."))
        self.assertEqual(normalize_name("Gokul", "S."), ("Gokul", "S."))
        self.assertEqual(normalize_name("Kuo-Tung", "Lin"), ("Kuo-Tung", "Lin"))
        self.assertEqual(normalize_name("Stéphane", "Leprince"), ("Stéphane", "Leprince"))


class ValidateTests(unittest.TestCase):
    def test_plus_prefix_survives_validation(self):
        df = pd.DataFrame({
            "FirstName": ["Jane"], "LastName": ["Doe"],
            "Email": ["jane@amazon.com"], "Phone": ["+1-206-555-0100"],
            "Company": ["Amazon"],
        })
        out, _ = validate_dataframe(df)
        self.assertEqual(out.at[0, "Phone"], "+1-206-555-0100")

    def test_excel_marker_is_stripped_on_read(self):
        df = pd.DataFrame({
            "FirstName": ["Jane"], "LastName": ["Doe"],
            "Email": ["jane@amazon.com"], "Phone": ["'+1-206-555-0100"],
            "Company": ["Amazon"],
        })
        out, _ = validate_dataframe(df)
        self.assertEqual(out.at[0, "Phone"], "+1-206-555-0100")

    def test_hard_required_failure_is_held_for_review(self):
        df = pd.DataFrame({
            "Id": ["00Q001"], "FirstName": ["No"], "LastName": ["Contact"],
            "Email": [""], "Phone": [""], "Company": ["Missing Co"],
        })
        out, _ = validate_dataframe(df)
        self.assertIn("missing_hard_required", out.at[0, "data_quality_flags"])
        self.assertEqual(out.at[0, "hitl_review"], "Yes")


class GuardrailTests(unittest.TestCase):
    def test_missing_email_column_is_rejected(self):
        result = process_csv("Id,Phone\n00Q1,2065550100\n")
        self.assertEqual(result["status"], "error")

    def test_unparseable_input_is_rejected(self):
        result = process_csv('Id,Email\n"unterminated,quote\n')
        self.assertIn(result["status"], {"ok", "error"})


class RunSummaryTests(unittest.TestCase):
    def test_process_csv_returns_a_complete_summary(self):
        result, _ = _load_result()
        summary = result["summary"]
        self.assertEqual(summary["totals"]["leads_in"], result["leads_in"])
        self.assertEqual(summary["totals"]["leads_out"], result["leads_out"])
        self.assertEqual(summary["totals"]["duplicates_merged"], result["duplicates_merged"])
        self.assertTrue(summary["merges"])
        self.assertTrue(any(merge["merged_from_ids"] for merge in summary["merges"]))
        self.assertEqual(len(summary["merge_lines"]), len(summary["merges"]))
        absorbed = sum(len(merge["merged_from_ids"]) for merge in summary["merges"])
        self.assertEqual(absorbed, result["duplicates_merged"])
        for merge in summary["merges"]:
            self.assertIn(merge["survivor_id"], merge["line"])
            for absorbed_id in merge["merged_from_ids"]:
                self.assertIn(absorbed_id, merge["line"])
            self.assertIn("signals:", merge["line"])
            self.assertIn("confidence:", merge["line"])
        self.assertIn("Phone", summary["field_changes"])
        self.assertGreater(summary["field_changes"]["Phone"]["count"], 0)
        ids = {entry["id"] for entry in summary["directory"]}
        self.assertIn("00Q001", ids)
        self.assertIn("00Q013", ids)
        dropped_ids = {row["id"] for row in summary["dropped"]}
        self.assertIn("00Q013", dropped_ids)

    def test_summary_covers_review_cases(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company,Title,Country\n"
            "00Q1,David,Manaster,dmanaster@pg.com,,PROCTER AND GAMBLE,Director,\n"
            "00Q2,Dave,Manaster,david.manaster@pg.com,5135550199,Procter & Gamble,Director,\n"
            "00Q3,Test,Arp,abcd@test.com,,Test Arp,Intern,\n"
            "00Q4,Pat,Lee,pat@amgen.com,099300286056,AMGEN INC.,"
            "Director | Security Program Leadership,\n"
        )
        result = process_csv(csv_in)
        self.assertEqual(result["status"], "ok", result.get("message"))
        summary = result["summary"]
        self.assertEqual(summary["totals"]["duplicates_merged"], 1)
        self.assertEqual(summary["totals"]["records_dropped"], 1)
        self.assertEqual(summary["dropped"][0]["id"], "00Q3")
        self.assertTrue(any(merge["survivor_id"] in {"00Q1", "00Q2"} for merge in summary["merges"]))
        from pipeline.tools import _agent_facing_summary, format_run_summary

        self.assertTrue(any("Manaster" in line and "signals:" in line for line in summary["merge_lines"]))
        self.assertIn("Manaster", format_run_summary(summary))
        company_examples = [ex["to"] for ex in summary["field_changes"]["Company"]["examples"]]
        self.assertTrue(any(value == "Amgen Inc." for value in company_examples))
        title_examples = [ex["to"] for ex in summary["field_changes"]["Title"]["examples"]]
        self.assertIn("Director", title_examples)
        self.assertTrue(any("unformatted_phone" in row["flags"] for row in summary["hitl"]))
        text = format_run_summary(summary)
        facing = _agent_facing_summary(summary)
        self.assertEqual(facing["merge_lines"], summary["merge_lines"])
        self.assertNotIn("directory", facing)
        self.assertIn("00Q3", " ".join(summary["critical_lines"]))
        self.assertFalse(any("Amgen" in line for line in summary["critical_lines"]))
        self.assertFalse(any("Account Executive" in line for line in summary.get("critical_lines", [])))
        self.assertIn("Critical changes:", text)
        self.assertIn("deduped.csv is ready.", text)
        self.assertIn("Merges:", text)
        self.assertIn("Dropped test rows:", text)

    def test_critical_summary_skips_cosmetic_edits(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company,Title,Website,Industry\n"
            "00Q1,JK,Rowling,willy@wonka.com,000-000-000,BFG,AE,lntecc.com,Software\n"
            "00Q2,Ada,Chen,ada@castelity.de,+491724597285,Castelity,SDR,castelity.de,Business Services\n"
        )
        result = process_csv(csv_in)
        self.assertEqual(result["status"], "ok", result.get("message"))
        lines = " ".join(result["summary"]["critical_lines"])
        self.assertIn("willy@wonka.com", lines)
        self.assertIn("000-000-000", lines)
        self.assertNotIn("J.K.", lines)
        self.assertNotIn("Account Executive", lines)
        self.assertNotIn("Technology", lines)
        self.assertNotIn("https://castelity.de", lines)
        self.assertNotIn("+49172", lines)


class GoldenRegressionTests(unittest.TestCase):
    def test_known_survivors_and_merges_are_unchanged(self):
        from pipeline.regression import diff_against_golden

        golden = pd.read_csv(FIXTURES / "golden_deduped.csv", dtype=str).fillna("")
        result, out = _load_result()
        self.assertEqual(list(out["Id"]), list(golden["Id"]))
        self.assertEqual(result["duplicates_merged"], 3)
        self.assertEqual(result["records_dropped"], 2)
        self.assertEqual(diff_against_golden(result["csv"]), [])
        self.assertEqual(result.get("field_diffs"), [])

    def test_accented_names_are_not_ascii_folded(self):
        _, out = _load_result()
        rows = out.set_index("Id")
        self.assertEqual(rows.loc["00Q003", "LastName"], "Büchert")
        self.assertEqual(rows.loc["00Q012", "FirstName"], "Stéphane")
        self.assertNotEqual(rows.loc["00Q003", "LastName"], "Buchert")
        self.assertNotEqual(rows.loc["00Q012", "FirstName"], "Stephane")

    def test_castelity_keeps_its_diacritic_phone_country(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company,Country\n"
            "00Q1,Ada,Chen,ada@castelity.de,+491724597285,Castelity,Germany\n"
        )
        result = process_csv(csv_in)
        self.assertEqual(result["status"], "ok", result.get("message"))
        row = pd.read_csv(io.StringIO(result["csv"]), dtype=str).iloc[0]
        self.assertTrue(row["Phone"].startswith("+49"))

    def test_shared_switchboard_groups_stay_separate(self):
        _, out = _load_result()
        self.assertIn("00Q004", set(out["Id"]))
        self.assertIn("00Q005", set(out["Id"]))


class ExportSchemaTests(unittest.TestCase):
    def test_exported_headers_round_trip(self):
        from pipeline.invariants import check_exported_schema

        result, _ = _load_result()
        source = pd.read_csv(INPUT_CSV, dtype=str)
        self.assertEqual(check_exported_schema(result["csv"], list(source.columns)), [])

    def test_malformed_header_is_rejected(self):
        from pipeline.invariants import check_exported_schema

        self.assertTrue(check_exported_schema('"_"" ,Id\n', ["_", "Id"]))


if __name__ == "__main__":
    unittest.main()
