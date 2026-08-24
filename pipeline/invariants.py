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
from .dedupe import IDENTITY_SIGNALS, normalized_email
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


def _ids(values) -> list:
    return [cell(value) for value in (values or []) if cell(value)]


def _parse_absorbed_signals(entry: dict) -> dict:
    """Map each absorbed Id to the signals that justified that hop."""
    parsed = {}
    raw = cell(entry.get("absorbed_match_signals", ""))
    if raw:
        for part in raw.split(";"):
            if ":" not in part:
                continue
            lead_id, sigs = part.split(":", 1)
            lead_id = cell(lead_id)
            if lead_id:
                parsed[lead_id] = [s for s in sigs.split("|") if s]
    fallback = [s for s in cell(entry.get("match_signals_used", "")).split("|") if s]
    for lead_id in cell(entry.get("merged_from_ids", "")).split(";"):
        lead_id = cell(lead_id)
        if lead_id and lead_id not in parsed:
            parsed[lead_id] = fallback
    return parsed


def check_drop_provenance(input_ids, output_ids, merge_log=None, dropped_test_ids=None) -> list:
    """
    Every input Id missing from write-back must be either an explicit test-data
    drop or absorbed into a survivor via email, phone, or name+company.
    """
    violations = []
    inputs = set(_ids(input_ids))
    outputs = set(_ids(output_ids))
    dropped_test = set(_ids(dropped_test_ids))
    absorbed = {}
    for entry in merge_log or []:
        survivor = cell(entry.get("surviving_lead_id", ""))
        per_loser = _parse_absorbed_signals(entry)
        for loser, signals in per_loser.items():
            absorbed[loser] = (survivor, signals)

    unaccounted = []
    weak = []
    dangling = []
    for lead_id in sorted(inputs):
        if lead_id in outputs or lead_id in dropped_test:
            continue
        if lead_id not in absorbed:
            unaccounted.append(lead_id)
            continue
        survivor, signals = absorbed[lead_id]
        if not IDENTITY_SIGNALS.intersection(signals):
            weak.append(lead_id)
        seen = {lead_id}
        cursor = survivor
        while cursor in absorbed and cursor not in outputs and cursor not in dropped_test:
            if cursor in seen:
                break
            seen.add(cursor)
            cursor = absorbed[cursor][0]
        if cursor not in outputs and cursor not in dropped_test:
            dangling.append(f"{lead_id}->{survivor}")

    if unaccounted:
        violations.append(
            f"Leads disappeared with no paper trail: {_sample(unaccounted)}"
        )
    if weak:
        violations.append(
            "Leads merged without email/phone/name+company identity: "
            f"{_sample(weak)}"
        )
    if dangling:
        violations.append(
            f"Merged leads point at a survivor that also vanished: {_sample(dangling)}"
        )
    return violations


def check_records(records, rows_in=None, input_ids=None, merge_log=None, dropped_test_ids=None) -> list:
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

    if input_ids is not None:
        output_ids = [cell(r.get("Id", "")) for r in records]
        violations.extend(
            check_drop_provenance(
                input_ids,
                output_ids,
                merge_log=merge_log,
                dropped_test_ids=dropped_test_ids,
            )
        )

    return violations


def assert_records(records, rows_in=None) -> None:
    violations = check_records(records, rows_in=rows_in)
    if violations:
        raise InvariantViolation("; ".join(violations))


def assert_dataframe(df, rows_in=None) -> None:
    assert_records(df.to_dict("records"), rows_in=rows_in)
