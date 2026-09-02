"""search_and_enrich may only read deduped.csv from a completed dedup run."""

import unittest
from unittest.mock import patch

from pipeline.tools import require_deduped_csv, search_and_enrich


class FakeContext:
    def __init__(self, artifacts=None, state=None):
        self.artifacts = artifacts or {}
        self.state = state if state is not None else {}

    async def load_artifact(self, filename=None, **kwargs):
        name = filename or kwargs.get("filename")
        return self.artifacts.get(name)

    async def save_artifact(self, filename, artifact):
        self.artifacts[filename] = artifact
        return 1


class RequireDedupedCsv(unittest.TestCase):
    def test_blocked_before_dedup_has_run(self):
        result = require_deduped_csv({})
        self.assertEqual(result["status"], "error")
        self.assertIn("deduped.csv", result["message"])

    def test_allowed_after_dedup_sets_the_flag(self):
        self.assertEqual(
            require_deduped_csv({"deduped_csv_ready": True})["status"], "ok"
        )


class SearchRequiresDedupedCsv(unittest.IsolatedAsyncioTestCase):
    async def test_raw_csv_text_is_rejected_when_dedup_has_not_run(self):
        ctx = FakeContext()
        result = await search_and_enrich("Id,Email\n1,pat@acme.com", ctx)
        self.assertEqual(result["status"], "error")
        self.assertIn("deduped.csv", result["message"])

    async def test_enrichment_reads_deduped_csv_not_the_passed_text(self):
        class Artifact:
            text = "Id,FirstName,LastName,Email,Company\n1,Pat,Smith,pat@acme.com,Acme\n"

        ctx = FakeContext(
            artifacts={"deduped.csv": Artifact()},
            state={"deduped_csv_ready": True},
        )

        def fake_enrich(df):
            return df, {
                "rows_attempted": 0,
                "rows_matched": 0,
                "rows_not_found": 0,
                "rows_skipped": 1,
                "fields_filled": 0,
                "credits_charged": 0,
            }

        with patch("pipeline.tools.enrich_dataframe", side_effect=fake_enrich) as mocked:
            result = await search_and_enrich("THIS_MUST_BE_IGNORED", ctx)

        self.assertEqual(result["status"], "ok")
        mocked.assert_called_once()
        passed = mocked.call_args[0][0]
        self.assertEqual(list(passed["Email"]), ["pat@acme.com"])


if __name__ == "__main__":
    unittest.main()
