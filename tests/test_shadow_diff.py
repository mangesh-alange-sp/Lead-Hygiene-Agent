"""Tests for the shadow-diff harness used before shipping any pipeline change."""

import io
import unittest

import pandas as pd

from pipeline.shadow_diff import diff_frames, format_report, unexpected_changes

BASE = """Id,FirstName,Company,Phone,Website
00Q001,Jane,Amazon.com Inc.,+1-206-555-0100,https://amazon.com
00Q002,Raj,L&T Construction,022-1234-5678,
"""


def frame(text):
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False).fillna("")


class DiffTests(unittest.TestCase):
    def test_identical_batches_have_no_changes(self):
        report = diff_frames(frame(BASE), frame(BASE))
        self.assertEqual(report["changes"], [])
        self.assertEqual(report["columns_changed"], [])

    def test_change_scoped_to_the_intended_column_is_expected(self):
        new = BASE.replace("022-1234-5678", "+91-221-234-5678")
        report = diff_frames(frame(BASE), frame(new))
        self.assertEqual(report["columns_changed"], ["Phone"])
        self.assertEqual(unexpected_changes(report, ["Phone"]), [])

    def test_collateral_change_in_another_column_is_flagged(self):
        new = BASE.replace("022-1234-5678", "+91-221-234-5678").replace(
            "L&T Construction", "L&t Construction"
        )
        report = diff_frames(frame(BASE), frame(new))
        unexpected = unexpected_changes(report, ["Phone"])
        self.assertEqual(len(unexpected), 1)
        self.assertEqual(unexpected[0]["column"], "Company")
        self.assertIn("Company", format_report(report, ["Phone"]))

    def test_schema_drift_is_reported(self):
        new = BASE.replace("Id,FirstName", "Id,merged_from_ids,FirstName").replace(
            "00Q001,Jane", "00Q001,DUPE001,Jane"
        ).replace("00Q002,Raj", "00Q002,,Raj")
        report = diff_frames(frame(BASE), frame(new))
        self.assertEqual(report["added_columns"], ["merged_from_ids"])

    def test_row_disappearance_is_reported(self):
        new = "\n".join(BASE.splitlines()[:2]) + "\n"
        report = diff_frames(frame(BASE), frame(new))
        self.assertEqual(report["only_in_old"], ["00Q002"])
        self.assertIn("rows no longer present", format_report(report, []))


if __name__ == "__main__":
    unittest.main()
