"""
invariants.py
Production safety net. These checks run on every batch before the CSV is written
and hard-fail the run, so a bad transformation cannot reach Salesforce.

They are data-shape rules, not fixture comparisons, so they hold for any real
Salesforce export.
"""

import re

from .casing import is_domain_like
from .config import DOTTED_LOWERCASE_WHITELIST
from .dedupe import normalized_email
from .textnorm import cell
from .website import is_free_provider_website

MAX_VIOLATION_SAMPLES = 5
_DOT_LOWER_RE = re.compile(r"\.[a-z]")
# Compared case-sensitively: 'S.r.l.' is the accepted form, 's.r.l.' is not.
_WHITELISTED_DOTTED = set(DOTTED_LOWERCASE_WHITELIST) | {
    form.rstrip(".") for form in DOTTED_LOWERCASE_WHITELIST
}


class InvariantViolation(Exception):
    """Raised when a post-pipeline invariant fails. The run must not write output."""


def _sample(items) -> str:
    items = list(items)
    head = ", ".join(str(item) for item in items[:MAX_VIOLATION_SAMPLES])
    suffix = f" (+{len(items) - MAX_VIOLATION_SAMPLES} more)" if len(items) > MAX_VIOLATION_SAMPLES else ""
    return head + suffix


def phone_has_text_marker(value) -> bool:
    text = "" if value is None else str(value)
    return text.startswith("'") or text.startswith("\t")


def phone_looks_like_excel_formula(value) -> bool:
    """Excel/Sheets treat cells starting with + = - @ as formulas, not phone numbers."""
    text = "" if value is None else str(value)
    return bool(text) and text[0] in "+=-@"


def company_has_bad_dotted_case(value) -> bool:
    """
    Flags 'Amazon.com'-style lowercase-after-dot only when the token is neither a
    real domain nor a whitelisted form such as B.V. or S.r.l.
    """
    text = cell(value)
    if not text:
        return False
    for token in text.split():
        bare = token.strip(",;:()[]\"'")
        if not _DOT_LOWER_RE.search(bare):
            continue
        if bare in _WHITELISTED_DOTTED or bare.rstrip(".") in _WHITELISTED_DOTTED:
            continue
        if is_domain_like(bare):
            continue
        return True
    return False


def check_records(records, rows_in=None) -> list:
    """Return a list of human-readable invariant violations (empty means clean)."""
    violations = []
    records = list(records)

    marked = [cell(r.get("Id", "")) for r in records if phone_has_text_marker(r.get("Phone"))]
    marked += [cell(r.get("Id", "")) for r in records if phone_has_text_marker(r.get("MobilePhone"))]
    if marked:
        violations.append(f"Phone values carry an Excel text marker: {_sample(marked)}")

    formulas = [cell(r.get("Id", "")) for r in records if phone_looks_like_excel_formula(r.get("Phone"))]
    formulas += [cell(r.get("Id", "")) for r in records if phone_looks_like_excel_formula(r.get("MobilePhone"))]
    if formulas:
        violations.append(f"Phone values would be evaluated as Excel formulas: {_sample(formulas)}")

    bad_case = [cell(r.get("Id", "")) for r in records if company_has_bad_dotted_case(r.get("Company"))]
    if bad_case:
        violations.append(f"Company has lowercase after a dot outside the allowlist: {_sample(bad_case)}")

    free_sites = [
        f"{cell(r.get('Id', ''))}={cell(r.get('Website'))}"
        for r in records
        if is_free_provider_website(r.get("Website"))
    ]
    if free_sites:
        violations.append(f"Website points at a free mail provider: {_sample(free_sites)}")

    seen = {}
    dupes = []
    for record in records:
        key = normalized_email(record.get("Email", ""))
        if not key:
            continue
        if key in seen:
            dupes.append(f"{key} ({seen[key]}, {cell(record.get('Id', ''))})")
        else:
            seen[key] = cell(record.get("Id", ""))
    if dupes:
        violations.append(f"Duplicate normalized emails survived: {_sample(dupes)}")

    if rows_in is not None and len(records) > rows_in:
        violations.append(f"Row count grew: {rows_in} in, {len(records)} out")

    return violations


def assert_records(records, rows_in=None) -> None:
    violations = check_records(records, rows_in=rows_in)
    if violations:
        raise InvariantViolation("; ".join(violations))


def assert_dataframe(df, rows_in=None) -> None:
    assert_records(df.to_dict("records"), rows_in=rows_in)
