"""
Demo: company normalization without a taxonomy.

For every row the code produces:
  display  — cleaned Company text written back to Salesforce
  match    — folded name with Inc/LLC stripped (same-file grouping)
  org_id   — website/email domain when present, otherwise match

Run from the repo root:

    python demos/company_normalize_demo.py
"""

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.casing import normalize_company_casing
from pipeline.domains import company_match_key, host_of, is_personal_domain
from pipeline.textnorm import cell, email_domain

JUNK = {"n/a", "na", "none", "null", "unknown", "-", "--"}


def tidy_display(company: str) -> str:
    text = cell(company)
    if not text or text.lower() in JUNK:
        return ""
    return normalize_company_casing(text)


def org_id(company: str, email: str = "", website: str = "") -> str:
    """Domain wins. No domain → the folded company name. Blank company → no id."""
    if not cell(company):
        return ""
    host = host_of(website)
    if host and not is_personal_domain(host):
        return host
    domain = email_domain(email)
    if domain and not is_personal_domain(domain):
        return domain
    return company_match_key(company)


def normalize_companies(rows: list[dict]) -> list[dict]:
    """
    Pass 1: tidy each name and attach org_id.
    Pass 2: among rows that share a name-only org_id (no domain),
            pick the longest tidy spelling as display.
    """
    out = []
    for row in rows:
        display = tidy_display(row.get("Company", ""))
        key = org_id(display, row.get("Email", ""), row.get("Website", ""))
        out.append({
            "id": row.get("Id", ""),
            "raw": row.get("Company", ""),
            "display": display,
            "match": company_match_key(display),
            "org_id": key,
        })

    groups = defaultdict(list)
    for item in out:
        if item["display"] and "." not in item["org_id"]:
            groups[item["org_id"]].append(item)
    for members in groups.values():
        winner = _longest_extension([m["display"] for m in members])
        if winner:
            for member in members:
                member["display"] = winner
    return out


def _longest_extension(names: list[str]) -> str:
    """Castelity + Castelity GmbH → Castelity GmbH. Acme Inc. vs Acme LLC stay split."""
    if len(set(names)) < 2:
        return ""
    best = max(names, key=len)
    shorter = [n for n in names if n != best and best.lower().startswith(n.lower())]
    return best if shorter else ""


SAMPLE = [
    {"Id": "1", "Company": "amazon inc", "Email": "jane@amazon.com", "Website": ""},
    {"Id": "2", "Company": "AMAZON.COM INC.", "Email": "bob@amazon.com", "Website": "https://www.amazon.com/careers"},
    {"Id": "3", "Company": "acme inc", "Email": "pat@gmail.com", "Website": ""},
    {"Id": "4", "Company": "Acme, LLC", "Email": "alex@gmail.com", "Website": ""},
    {"Id": "5", "Company": "Castelity", "Email": "a@gmail.com", "Website": ""},
    {"Id": "6", "Company": "Castelity GmbH", "Email": "b@gmail.com", "Website": ""},
    {"Id": "7", "Company": "pg", "Email": "x@gmail.com", "Website": ""},
    {"Id": "8", "Company": "n/a", "Email": "y@acme.com", "Website": ""},
    {"Id": "9", "Company": "IBM", "Email": "t@ibm.com", "Website": ""},
]


def main():
    print(f"{'Id':<4} {'raw':<22} {'display':<22} {'match':<14} {'org_id'}")
    print("-" * 88)
    for item in normalize_companies(SAMPLE):
        print(
            f"{item['id']:<4} {item['raw']:<22} {item['display']:<22} "
            f"{item['match']:<14} {item['org_id']}"
        )


if __name__ == "__main__":
    main()
