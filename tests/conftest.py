"""Pytest bootstrap. MX lookups stay cache-only so the suite is deterministic."""

import os

os.environ.setdefault("LEAD_HYGIENE_MX_LOOKUP", "0")
