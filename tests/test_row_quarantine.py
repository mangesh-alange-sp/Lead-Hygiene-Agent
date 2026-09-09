"""
Rows that fail a row-level invariant are held back in needs_review.csv instead
of failing the batch they happened to ship with.

Before this, one dirty lead in a 1000-row upload blocked all 1000 rows, because
the invariant gate is all-or-nothing by design. It stays all-or-nothing for
batch-shaped faults (a lead vanished, the row count grew, duplicate emails
survived) and for the case where every row fails, because both mean the
pipeline misbehaved rather than the data being dirty.

Normalization already repairs every row-level fault reachable from real input,
so these tests inject the fault instead of pretending some value is dirty. That
is the point of the net: it catches what normalization did not.
"""

import csv
import io
import unittest
from unittest.mock import patch

import pipeline.tools as tools
from pipeline.invariants import (
    check_records,
    phone_has_text_marker,
    phone_looks_like_excel_formula,
    row_level_violations,
)
from pipeline.textnorm import cell
from pipeline.tools import (
    CHUNK_SIZE,
    REVIEW_FILE,
    _agent_facing_summary,
    format_run_summary,
    process_csv,
    process_csv_for_agent,
    run_dedup_pipeline,
)

from tests.test_agent_chunking import FakeContext

HEADER = "Id,FirstName,LastName,Email,Phone,Company,Title,Website,Industry,Country\n"
DIRTY_ID = "ZZ000000"
REASON = "Phone would be evaluated as an Excel formula"


def _row(index):
    return (
        f"ZZ{index:06d},Pat{index},Lee{index},pat{index}@acme{index}.com,"
        f'"(415) 555-{1000 + index:04d}",Acme {index} Inc.,PM,'
        f"acme{index}.com,Software,US\n"
    )


def _csv(count):
    return HEADER + "".join(_row(i) for i in range(count))


def _flag(lead_id=DIRTY_ID):
    """Make exactly one Id unwritable, whatever its values happen to be."""
    real = tools.row_level_violations

    def fake(record):
        if cell(record.get("Id", "")) == lead_id:
            return [REASON]
        return real(record)

    return patch("pipeline.tools.row_level_violations", fake)


class RowLevelSplitTests(unittest.TestCase):
    """row_level_violations owns the per-row versus per-batch boundary."""

    def test_per_row_faults_are_reported(self):
        cases = [
            ({"Phone": "'+12065550100"}, "text marker"),
            ({"Phone": "+971 4 123 4567"}, "Excel formula"),
            ({"MobilePhone": "=2065550100"}, "Excel formula"),
            ({"Company": "Acme.foo Inc"}, "lowercase after a dot"),
            ({"Website": "https://gmail.com"}, "free mail provider"),
        ]
        for record, expected in cases:
            with self.subTest(record=record):
                reasons = row_level_violations({"Id": "ZZ1", **record})
                self.assertTrue(reasons, record)
                self.assertTrue(any(expected in r for r in reasons), reasons)

    def test_a_clean_row_has_no_reasons(self):
        self.assertEqual(
            row_level_violations({
                "Id": "ZZ1", "Phone": "+12065550100", "Company": "Acme Inc.",
                "Website": "https://acme.com", "Email": "pat@acme.com",
            }),
            [],
        )

    def test_batch_shaped_faults_are_not_row_level(self):
        # Duplicate emails are a relationship between rows, so no single row
        # can be blamed for them and quarantining one would hide the fault.
        rows = [
            {"Id": "ZZ1", "Email": "pat@acme.com", "Phone": "+12065550100"},
            {"Id": "ZZ2", "Email": "PAT@acme.com", "Phone": "+12065550101"},
        ]
        for row in rows:
            self.assertEqual(row_level_violations(row), [])
        self.assertTrue(
            any("Duplicate normalized email" in v for v in check_records(rows))
        )


class QuarantineTests(unittest.TestCase):
    def _run(self, count=5):
        with _flag():
            return process_csv(_csv(count))

    def test_the_run_succeeds_and_delivers_the_clean_rows(self):
        result = self._run(count=5)
        self.assertEqual(result["status"], "ok", result.get("message"))
        self.assertEqual(result["rows_held_for_review"], 1)
        self.assertEqual(result["leads_out"], 4)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(result["csv"])))), 4)

    def test_the_held_row_is_absent_from_the_writeback(self):
        result = self._run()
        ids = {row["Id"] for row in csv.DictReader(io.StringIO(result["csv"]))}
        self.assertNotIn(DIRTY_ID, ids)
        self.assertEqual([row["id"] for row in result["quarantine"]["rows"]], [DIRTY_ID])

    def test_the_writeback_still_passes_every_invariant(self):
        result = self._run()
        rows = list(csv.DictReader(io.StringIO(result["csv"])))
        self.assertEqual(check_records(rows), [])
        for row in rows:
            self.assertFalse(phone_looks_like_excel_formula(row["Phone"]))
            self.assertFalse(phone_has_text_marker(row["Phone"]))

    def test_no_lead_is_reported_as_having_vanished(self):
        # A held row is missing from the write-back, so drop provenance has to
        # count it as accounted for rather than as a lead with no paper trail.
        result = self._run()
        self.assertEqual(result["invariant_violations"], [])

    def test_the_review_file_carries_the_row_and_the_reason(self):
        result = self._run()
        self.assertEqual(result["review_file"], REVIEW_FILE)
        held = list(csv.DictReader(io.StringIO(result["review_csv"])))
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["Id"], DIRTY_ID)
        self.assertEqual(held[0]["needs_review_reason"], REASON)

    def test_the_review_file_keeps_the_lead_columns_and_the_original_phone(self):
        result = self._run()
        held = next(csv.DictReader(io.StringIO(result["review_csv"])))
        for column in ("Id", "FirstName", "LastName", "Email", "Company"):
            self.assertIn(column, held)
        self.assertEqual(held["FirstName"], "Pat0")
        self.assertEqual(held["Phone_raw"], "(415) 555-1000")

    def test_the_summary_counts_and_names_the_file(self):
        summary = self._run()["summary"]
        self.assertEqual(summary["totals"]["rows_held_for_review"], 1)
        self.assertTrue(any(REVIEW_FILE in entry for entry in summary["files"]))
        self.assertEqual(len(summary["held_for_review_lines"]), 1)
        self.assertIn(DIRTY_ID, summary["held_for_review_lines"][0])
        self.assertEqual(summary["held_for_review_reasons"], {REASON: 1})

    def test_the_model_is_told_to_report_the_held_rows(self):
        facing = _agent_facing_summary(self._run()["summary"])
        self.assertEqual(len(facing["held_for_review_lines"]), 1)
        self.assertIn(REVIEW_FILE, facing["instruction"])
        self.assertIn("held back", facing["instruction"])

    def test_the_cli_summary_mentions_the_held_rows(self):
        text = format_run_summary(self._run()["summary"])
        self.assertIn(REVIEW_FILE, text)
        self.assertIn(DIRTY_ID, text)

    def test_a_clean_file_produces_no_review_file(self):
        result = process_csv(_csv(5))
        self.assertEqual(result["status"], "ok", result.get("message"))
        self.assertEqual(result["rows_held_for_review"], 0)
        self.assertEqual(result["review_csv"], "")
        self.assertEqual(result["summary"]["held_for_review_lines"], [])
        self.assertFalse(
            any(REVIEW_FILE in entry for entry in result["summary"]["files"])
        )


class RealDetectionTests(unittest.TestCase):
    """The injected tests above prove the plumbing; this proves the wiring."""

    def test_a_genuinely_unsafe_phone_is_detected_and_held(self):
        # Break the Excel-safety step for one row's phone only.
        original = tools.strip_excel_artifacts

        def break_row_zero(value):
            text = original(value)
            return "'" + text if text == "+14155551000" else text

        with patch("pipeline.tools.strip_excel_artifacts", break_row_zero):
            result = process_csv(_csv(5))
        self.assertEqual(result["status"], "ok", result.get("message"))
        self.assertEqual(result["rows_held_for_review"], 1)
        held = next(csv.DictReader(io.StringIO(result["review_csv"])))
        self.assertEqual(held["Id"], DIRTY_ID)
        self.assertIn("text marker", held["needs_review_reason"])


class EveryRowHeldTests(unittest.TestCase):
    """All rows failing is a pipeline fault, so it must not ship an empty file."""

    def test_the_run_errors_instead_of_delivering_nothing(self):
        with patch("pipeline.tools.strip_excel_artifacts", lambda v: "'" + str(v)):
            result = process_csv(_csv(5))
        self.assertEqual(result["status"], "error")
        self.assertNotIn("csv", result)
        self.assertIn("points at the pipeline", result["message"])
        self.assertTrue(any("text marker" in v for v in result["invariant_violations"]))


class ChunkedQuarantineTests(unittest.TestCase):
    """A held row in one batch must not cost the other batches."""

    COUNT = CHUNK_SIZE + 5

    def _run(self):
        with _flag():
            return process_csv_for_agent(_csv(self.COUNT))

    def test_every_other_batch_is_still_delivered(self):
        result = self._run()
        self.assertEqual(result["status"], "ok", result.get("message"))
        self.assertEqual(result["summary"]["chunk_count"], 2)
        self.assertEqual(result["rows_held_for_review"], 1)
        self.assertEqual(result["leads_out"], self.COUNT - 1)
        self.assertEqual(
            len(list(csv.DictReader(io.StringIO(result["csv"])))), self.COUNT - 1
        )

    def test_the_batch_lines_report_the_hold(self):
        lines = self._run()["summary"]["chunk_lines"]
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("1 held for review" in line for line in lines))
        self.assertTrue(any("0 held for review" in line for line in lines))

    def test_one_review_file_covers_every_batch(self):
        result = self._run()
        held = list(csv.DictReader(io.StringIO(result["review_csv"])))
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["Id"], DIRTY_ID)

    def test_the_combined_writeback_passes_the_invariants(self):
        result = self._run()
        self.assertEqual(
            check_records(list(csv.DictReader(io.StringIO(result["csv"])))), []
        )
        self.assertEqual(result["invariant_violations"], [])


class QuarantineArtifactTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_review_file_is_saved_as_an_artifact(self):
        ctx = FakeContext()
        with _flag():
            out = await run_dedup_pipeline(_csv(5), tool_context=ctx)
        self.assertEqual(out["status"], "ok", out.get("message"))
        self.assertEqual(out["rows_held_for_review"], 1)
        self.assertEqual(out["review_file"], REVIEW_FILE)
        self.assertIn(REVIEW_FILE, ctx.saved)
        self.assertIn("deduped.csv", ctx.saved)

    async def test_a_clean_run_saves_no_review_artifact(self):
        ctx = FakeContext()
        out = await run_dedup_pipeline(_csv(5), tool_context=ctx)
        self.assertEqual(out["status"], "ok", out.get("message"))
        self.assertEqual(out["rows_held_for_review"], 0)
        self.assertNotIn(REVIEW_FILE, ctx.saved)
        self.assertNotIn("review_file", out)

    def test_the_review_file_is_never_mistaken_for_an_upload(self):
        self.assertIn(REVIEW_FILE, tools.GENERATED_ARTIFACTS)


if __name__ == "__main__":
    unittest.main()
