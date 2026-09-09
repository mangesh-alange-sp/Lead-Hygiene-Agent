"""search_and_enrich may only read deduped.csv from a completed dedup run."""

import unittest
from unittest.mock import patch

from pipeline.tools import require_deduped_csv, search_and_enrich


class FakeContext:
    def __init__(self, artifacts=None, state=None):
        self._artifacts = artifacts or {}
        self.state = state if state is not None else {}

    async def load_artifact(self, filename=None, **kwargs):
        name = filename or kwargs.get("filename")
        return self._artifacts.get(name)

    async def save_artifact(self, filename, artifact):
        self._artifacts[filename] = artifact
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

    async def test_hygiene_audit_is_joined_before_enrich(self):
        class Artifact:
            text = (
                "Id,FirstName,LastName,Email,Phone,Company\n"
                "1,Pat,Smith,pat@avari.com,+12065550100,Avari\n"
            )

        ctx = FakeContext(
            artifacts={"deduped.csv": Artifact()},
            state={
                "deduped_csv_ready": True,
                "last_audit": [{
                    "surviving_lead_id": "1",
                    "email_status": "undeliverable",
                    "email_raw": "pat@avari.com",
                }],
            },
        )

        captured = {}

        def fake_enrich(df):
            captured["email_status"] = list(df["email_status"])
            captured["Email_raw"] = list(df["Email_raw"])
            return df, {
                "rows_attempted": 0,
                "rows_matched": 0,
                "rows_not_found": 0,
                "rows_skipped": 1,
                "fields_filled": 0,
                "credits_charged": 0,
                "email_blanked_no_replacement_ids": [],
                "enrichment_review": [],
            }

        with patch("pipeline.tools.enrich_dataframe", side_effect=fake_enrich):
            result = await search_and_enrich("", ctx)

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(captured["email_status"], ["undeliverable"])
        self.assertEqual(captured["Email_raw"], ["pat@avari.com"])
        saved = ctx._artifacts["enriched.csv"]
        blob = getattr(getattr(saved, "inline_data", None), "data", None)
        text = blob.decode("utf-8") if isinstance(blob, (bytes, bytearray)) else saved.text
        header = text.splitlines()[0]
        self.assertNotIn("email_status", header)
        self.assertNotIn("Email_raw", header)

    async def test_enrichment_hard_fails_if_it_blanks_a_filled_email(self):
        class Artifact:
            text = (
                "Id,FirstName,LastName,Email,Phone,Company\n"
                "1,Pat,Smith,pat@acme.com,+12065550100,Acme\n"
            )

        ctx = FakeContext(
            artifacts={"deduped.csv": Artifact()},
            state={"deduped_csv_ready": True},
        )

        def wipe_email(df):
            wiped = df.copy()
            wiped["Email"] = ""
            return wiped, {
                "rows_attempted": 1,
                "rows_matched": 1,
                "rows_not_found": 0,
                "rows_skipped": 0,
                "fields_filled": 0,
                "credits_charged": 1,
            }

        with patch("pipeline.tools.enrich_dataframe", side_effect=wipe_email):
            result = await search_and_enrich("", ctx)

        self.assertEqual(result["status"], "error")
        self.assertTrue(
            any("blanked Email" in v for v in result.get("invariant_violations", [])),
            result,
        )
        self.assertNotIn("enriched.csv", ctx._artifacts)


if __name__ == "__main__":
    unittest.main()
