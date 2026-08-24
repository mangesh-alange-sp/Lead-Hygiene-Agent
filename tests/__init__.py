"""
Test package bootstrap.

Email deliverability is answered from data/mx_cache.json only, so the suite is
deterministic and never depends on DNS being reachable. Tests that want to
exercise a live lookup must opt in explicitly.
"""

import os

os.environ.setdefault("LEAD_HYGIENE_MX_LOOKUP", "0")
