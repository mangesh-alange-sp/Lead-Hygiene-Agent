"""Agent-only 200-row batching, and reading the upload instead of a paste."""

import asyncio
import csv
import io
import unittest

import pandas as pd

from pipeline.tools import (
    CHUNK_SIZE,
    _agent_facing_summary,
    process_csv,
    process_csv_for_agent,
    run_dedup_pipeline,
)


def _distinct_csv(n, shared_email_at=None):
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=[
            "Id", "FirstName", "LastName", "Email", "Phone",
            "Company", "Title", "Website", "Industry", "Country",
        ],
        lineterminator="\n",
    )
    writer.writeheader()
    for i in range(n):
        email = f"pat{i}@acme{i}.com"
        if shared_email_at is not None and i == shared_email_at[1]:
            email = f"pat{shared_email_at[0]}@acme{shared_email_at[0]}.com"
        writer.writerow({
            "Id": f"ZZ{i:06d}",
            "FirstName": f"Pat{i}",
            "LastName": f"Lee{i}",
            "Email": email,
            "Phone": f"(415) 555-{i:04d}"[-14:],
            "Company": f"Acme {i} Inc.",
            "Title": "PM",
            "Website": f"acme{i}.com",
            "Industry": "Software",
            "Country": "US",
        })
    return buf.getvalue()


class FakeInlineData:
    def __init__(self, data):
        self.data = data


class FakePart:
    def __init__(self, data):
        self.inline_data = FakeInlineData(data)
        self.text = None


class FakeContext:
    def __init__(self, uploads=None):
        self.state = {}
        self.saved = []
        self._artifacts = dict(uploads or {})

    async def save_artifact(self, filename, artifact):
        self.saved.append(filename)
        self._artifacts[filename] = artifact
        return 1

    async def load_artifact(self, filename=None, version=None):
        return self._artifacts.get(filename)

    async def list_artifacts(self):
        return list(self._artifacts)


class AgentChunkingTests(unittest.TestCase):
    def test_files_at_chunk_size_are_not_split(self):
        result = process_csv_for_agent(_distinct_csv(CHUNK_SIZE))
        self.assertEqual(result["status"], "ok", result.get("message"))
        self.assertEqual(result["leads_in"], CHUNK_SIZE)
        self.assertEqual(result["summary"].get("chunk_count") or 1, 1)
        facing = _agent_facing_summary(result["summary"])
        self.assertEqual(facing["chunk_count"], 1)
        self.assertEqual(len(facing["technical_log"]), CHUNK_SIZE)

    def test_files_over_chunk_size_are_batched_and_stitched(self):
        rows = CHUNK_SIZE + 1
        result = process_csv_for_agent(_distinct_csv(rows))
        self.assertEqual(result["status"], "ok", result.get("message"))
        self.assertEqual(result["leads_in"], rows)
        self.assertEqual(result["leads_out"], rows)
        self.assertEqual(result["summary"]["chunk_count"], 2)
        self.assertEqual(len(result["summary"]["chunk_lines"]), 2)
        out = pd.read_csv(io.StringIO(result["csv"]), dtype=str)
        self.assertEqual(len(out), rows)
        facing = _agent_facing_summary(result["summary"])
        self.assertEqual(facing["technical_log"], [])
        self.assertEqual(facing["chunk_count"], 2)
        self.assertIn("dedup_log.csv", facing["chunk_note"])

    def test_cli_path_does_not_chunk(self):
        rows = CHUNK_SIZE + 1
        result = process_csv(_distinct_csv(rows))
        self.assertEqual(result["status"], "ok", result.get("message"))
        self.assertEqual(result["summary"].get("chunk_count") or 1, 1)
        self.assertEqual(len(result["summary"]["technical_log"]), rows)

    def test_same_email_far_apart_still_merges_after_sort(self):
        # Row 0 and the last row share an email; without a sort they would
        # land in different 200-row slices and survive as two leads.
        rows = CHUNK_SIZE + 1
        result = process_csv_for_agent(_distinct_csv(rows, shared_email_at=(0, rows - 1)))
        self.assertEqual(result["status"], "ok", result.get("message"))
        self.assertEqual(result["leads_in"], rows)
        self.assertEqual(result["duplicates_merged"], 1)
        self.assertEqual(result["leads_out"], rows - 1)


class AgentChunkingToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_saves_one_combined_artifact(self):
        ctx = FakeContext()
        out = await run_dedup_pipeline(
            _distinct_csv(CHUNK_SIZE + 1), tool_context=ctx
        )
        self.assertEqual(out["status"], "ok", out.get("message"))
        self.assertEqual(out["summary"]["chunk_count"], 2)
        self.assertEqual(out["summary"]["technical_log"], [])
        self.assertIn("deduped.csv", ctx.saved)
        self.assertTrue(ctx.state.get("deduped_csv_ready"))


class UploadedFileTests(unittest.IsolatedAsyncioTestCase):
    """The rows must reach the tool without passing through the model."""

    def _ctx(self, name="leads.csv", rows=CHUNK_SIZE + 1, encoding="utf-8"):
        payload = _distinct_csv(rows).encode(encoding)
        return FakeContext(uploads={name: FakePart(payload)})

    async def test_named_upload_is_read_without_csv_text(self):
        ctx = self._ctx()
        out = await run_dedup_pipeline(filename="leads.csv", tool_context=ctx)
        self.assertEqual(out["status"], "ok", out.get("message"))
        self.assertEqual(out["leads_in"], CHUNK_SIZE + 1)
        self.assertEqual(out["source_file"], "leads.csv")
        self.assertIn("deduped.csv", ctx.saved)

    async def test_quoted_filename_from_the_placeholder_still_resolves(self):
        ctx = self._ctx(rows=5)
        out = await run_dedup_pipeline(filename='"leads.csv"', tool_context=ctx)
        self.assertEqual(out["status"], "ok", out.get("message"))
        self.assertEqual(out["leads_in"], 5)

    async def test_upload_is_found_when_the_model_passes_nothing(self):
        ctx = self._ctx(rows=5)
        out = await run_dedup_pipeline(tool_context=ctx)
        self.assertEqual(out["status"], "ok", out.get("message"))
        self.assertEqual(out["source_file"], "leads.csv")

    async def test_generated_files_are_never_treated_as_the_upload(self):
        ctx = FakeContext(
            uploads={"deduped.csv": FakePart(_distinct_csv(5).encode("utf-8"))}
        )
        out = await run_dedup_pipeline(tool_context=ctx)
        self.assertEqual(out["status"], "error")
        self.assertIn("Attach the CSV", out["message"])

    async def test_missing_filename_reports_what_is_available(self):
        ctx = self._ctx(rows=5)
        out = await run_dedup_pipeline(filename="wrong.csv", tool_context=ctx)
        self.assertEqual(out["status"], "error")
        self.assertIn("wrong.csv", out["message"])
        self.assertIn("leads.csv", out["message"])
        self.assertEqual(ctx.saved, [])

    async def test_latin1_upload_is_decoded_not_rejected(self):
        raw = (
            "Id,FirstName,LastName,Email,Phone,Company,Country\n"
            "ZZ1,Hans,B\u00fcchert,hans@buchert.de,+491701112223,B\u00fcchert GmbH,Germany\n"
        ).encode("latin-1")
        ctx = FakeContext(uploads={"leads.csv": FakePart(raw)})
        out = await run_dedup_pipeline(filename="leads.csv", tool_context=ctx)
        self.assertEqual(out["status"], "ok", out.get("message"))
        self.assertEqual(out["leads_in"], 1)

    async def test_pasted_rows_still_work_without_an_upload(self):
        ctx = FakeContext()
        out = await run_dedup_pipeline(_distinct_csv(3), tool_context=ctx)
        self.assertEqual(out["status"], "ok", out.get("message"))
        self.assertEqual(out["source_file"], "inline")

    async def test_no_csv_at_all_is_a_clear_error(self):
        ctx = FakeContext()
        out = await run_dedup_pipeline(tool_context=ctx)
        self.assertEqual(out["status"], "error")
        self.assertIn("Attach the CSV", out["message"])
        self.assertEqual(ctx.saved, [])


if __name__ == "__main__":
    unittest.main()
