"""CLI enrich: refuse to run without a key or a write-back file."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.tools import enrich_csv


class EnrichCliGuardTests(unittest.TestCase):
    def test_cli_exits_when_the_api_key_is_missing(self):
        env = {k: v for k, v in os.environ.items() if k not in {"LUSHA_API_KEY", "LUSHA_APIKEY"}}
        result = subprocess.run(
            [sys.executable, "enrich_cli.py"],
            cwd=Path(__file__).resolve().parent.parent,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LUSHA_API_KEY", result.stdout)

    def test_cli_exits_when_deduped_csv_is_missing(self):
        env = {**os.environ, "LUSHA_API_KEY": "test-key"}
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "deduped.csv"
            result = subprocess.run(
                [sys.executable, "enrich_cli.py", str(missing)],
                cwd=Path(__file__).resolve().parent.parent,
                env=env,
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deduped.csv", result.stdout)

    def test_enrich_csv_writes_review_rows_for_unmapped_industry(self):
        csv_in = (
            "Id,FirstName,LastName,Email,Phone,Company,Website,Industry\n"
            "1,Pat,Lee,pat@acme.com,+14155550100,Acme,https://acme.com,\n"
        )

        def api(url, payload):
            results = []
            for item in payload.get("companies") or []:
                results.append({
                    "clientReferenceId": item["clientReferenceId"],
                    "industry": "Underwater Basket Weaving",
                })
            return {"status": "ok", "results": results, "billing": {"creditsCharged": 1}}

        with patch("pipeline.enrich._lusha_post", api):
            result = enrich_csv(csv_in)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["review_csv"])
        self.assertTrue(any("unmapped industry" in line for line in result["enrichment_review_lines"]))


if __name__ == "__main__":
    unittest.main()
