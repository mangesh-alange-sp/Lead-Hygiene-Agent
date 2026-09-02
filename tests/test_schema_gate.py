"""Pandera output-schema gate: these must be hard failures, not warnings."""

import unittest

import pandas as pd

from pipeline.schema import (
    OutputSchemaViolation,
    assert_output_schema,
    check_output_schema,
    check_phone_states,
)
from pipeline.tools import process_csv

COLUMNS = ["Id", "Email", "Phone", "Website"]


def make_frame(**overrides):
    data = {
        "Id": ["00Q1", "00Q2"],
        "Email": ["a@acme.com", "b@acme.com"],
        "Phone": ["+12065550100", "+447771695127"],
        "Website": ["https://acme.com", ""],
    }
    data.update(overrides)
    return pd.DataFrame(data)


class CleanFrameTests(unittest.TestCase):
    def test_clean_frame_passes(self):
        self.assertEqual(check_output_schema(make_frame(), COLUMNS), [])
        assert_output_schema(make_frame(), COLUMNS)


class HeaderTests(unittest.TestCase):
    def test_missing_column_fails(self):
        frame = make_frame().drop(columns=["Website"])
        self.assertTrue(check_output_schema(frame, COLUMNS))

    def test_extra_column_fails(self):
        frame = make_frame()
        frame["Sneaky"] = "x"
        self.assertTrue(check_output_schema(frame, COLUMNS))

    def test_reordered_columns_fail(self):
        frame = make_frame()[["Email", "Id", "Phone", "Website"]]
        self.assertTrue(check_output_schema(frame, COLUMNS))

    def test_double_quoted_header_is_caught(self):
        frame = make_frame().rename(columns={"Id": '"Id""'})
        violations = check_output_schema(frame, COLUMNS)
        self.assertTrue(any("headers" in v for v in violations))


class IdTests(unittest.TestCase):
    def test_duplicate_ids_fail(self):
        violations = check_output_schema(make_frame(Id=["00Q1", "00Q1"]), COLUMNS)
        self.assertTrue(violations)

    def test_blank_id_fails(self):
        violations = check_output_schema(make_frame(Id=["00Q1", ""]), COLUMNS)
        self.assertTrue(violations)


class WebsiteTests(unittest.TestCase):
    def test_non_url_website_fails(self):
        violations = check_output_schema(
            make_frame(Website=["acme.com", ""]), COLUMNS
        )
        self.assertTrue(violations)

    def test_blank_website_is_allowed(self):
        self.assertEqual(check_output_schema(make_frame(Website=["", ""]), COLUMNS), [])


class PhoneStateTests(unittest.TestCase):
    def test_country_code_less_phone_fails_without_the_review_flag(self):
        frame = make_frame(Phone=["2065550100", "+447771695127"])
        frame["phone_status"] = ["valid", "valid"]
        violations = check_phone_states(frame)
        self.assertTrue(violations)
        self.assertIn("00Q1", violations[0])

    def test_country_code_less_phone_passes_when_flagged_needs_review(self):
        frame = make_frame(Phone=["2065550100", "+447771695127"])
        frame["phone_status"] = ["needs_review", "valid"]
        self.assertEqual(check_phone_states(frame), [])

    def test_gate_raises_on_violation(self):
        with self.assertRaises(OutputSchemaViolation):
            assert_output_schema(make_frame(Id=["00Q1", "00Q1"]), COLUMNS)


class PipelineGateTests(unittest.TestCase):
    def test_schema_failure_blocks_the_run_and_emits_no_csv(self):
        from unittest.mock import patch

        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company,Country\n"
            "00Q1,Jane,Doe,jane@amazon.com,2065550100,Amazon,US\n"
        )
        with patch("pipeline.tools.check_output_schema", return_value=["forced failure"]):
            result = process_csv(csv_in)
        self.assertEqual(result["status"], "error")
        self.assertNotIn("csv", result)
        self.assertIn("forced failure", result["message"])

    def test_real_run_passes_the_gate(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company,Country\n"
            "00Q1,Jane,Doe,jane@amazon.com,2065550100,Amazon,US\n"
            "00Q2,Pat,Lee,pat@acme.com,099300286056,Acme,\n"
        )
        result = process_csv(csv_in)
        self.assertEqual(result["status"], "ok", result.get("message"))
        self.assertEqual(result["invariant_violations"], [])


if __name__ == "__main__":
    unittest.main()
