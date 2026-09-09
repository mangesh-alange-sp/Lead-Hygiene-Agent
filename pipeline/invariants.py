"""
invariants.py
Production safety net. These checks run on every batch before the CSV is written
and hard-fail the run, so a bad transformation cannot reach Salesforce.

They are data-shape rules, not fixture comparisons, so they hold for any real
Salesforce export.

Email and Phone preservation: a keepable source Email may not be blanked at
hygiene (only invalid syntax and placeholder/reserved domains may clear). A
source Phone may not be blanked unless the base number is junk. Enrichment
must not blank a filled Email or Phone except Email Ids on the explicit
unvalidated-no-replacement list.
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


# E.164 (+447771695127) and the legacy dashed form are both real phone shapes.
_PLUS_CC_RE = re.compile(r"^\+\d[\d-]*$")


def phone_looks_like_excel_formula(value) -> bool:
    """Flag = @ and non-phone '+' / '-'. A leading +CC phone number is allowed."""
    text = "" if value is None else str(value)
    if not text:
        return False
    if text[0] in "=@":
        return True
    if text[0] == "-":
        return True
    if text[0] == "+":
        return not bool(_PLUS_CC_RE.match(text))
    return False


def phone_lost_country_code(raw, cleaned) -> bool:
    """
    True when the input carried a country code and the output no longer does.

    An unchanged value still carries whatever the source supplied (including a
    00-prefixed form), and a value cleared as junk is a separate, deliberate
    decision flagged as garbage_phone. Neither counts as a lost country code.
    """
    from .phone import input_has_calling_code, is_international_format
    from .textnorm import digits_only

    if not input_has_calling_code(raw):
        return False
    out = cell(cleaned)
    if not out:
        return False
    if is_international_format(out):
        return False
    return digits_only(out) != digits_only(raw)


def snapshot_email_phone(records) -> dict:
    """Id -> original Email/Phone. Used to prove hygiene or enrich did not wipe them."""
    snaps = {}
    for record in records:
        lead_id = cell(record.get("Id", ""))
        if not lead_id:
            continue
        snaps[lead_id] = {
            "Email": cell(record.get("Email", "")),
            "Phone": cell(record.get("Phone", "")),
        }
    return snaps


def _source_email(record, prior) -> str:
    if prior and "Email" in prior:
        return cell(prior.get("Email", ""))
    return cell(record.get("Email_raw", ""))


def _source_phone(record, prior) -> str:
    if prior and "Phone" in prior:
        return cell(prior.get("Phone", ""))
    return cell(record.get("Phone_raw", ""))


def hygiene_email_blank_allowed(source_email, record) -> bool:
    """
    A submitted email may be cleared only for invalid syntax or a
    placeholder/RFC-reserved domain. MX misses must stay on the row.
    """
    from .domains import is_placeholder_domain
    from .emailcheck import INVALID_SYNTAX, is_reserved_domain
    from .textnorm import email_domain

    if not cell(source_email):
        return True
    if cell(record.get("Email", "")):
        return True
    status = cell(record.get("email_status", ""))
    if status == INVALID_SYNTAX:
        return True
    raw = cell(record.get("Email_raw", "")) or cell(source_email)
    domain = email_domain(raw)
    return is_reserved_domain(domain) or is_placeholder_domain(domain)


def hygiene_phone_blank_allowed(source_phone, record) -> bool:
    """
    A submitted phone may be cleared only when the base number is junk.
    An extension suffix is not junk: classify runs after the suffix is split.
    """
    from .phone import classify_phone

    if not cell(source_phone):
        return True
    if cell(record.get("Phone", "")):
        return True
    raw = cell(record.get("Phone_raw", "")) or cell(source_phone)
    return classify_phone(raw) in {"empty", "garbage", "noise"}


def check_hygiene_preservation(records, source_contacts=None) -> list:
    """Hard-fail when hygiene blanked a keepable Email or Phone."""
    source_contacts = source_contacts or {}
    lost_email, lost_phone = [], []
    for record in records:
        lead_id = cell(record.get("Id", ""))
        prior = source_contacts.get(lead_id) or {}
        if not hygiene_email_blank_allowed(_source_email(record, prior), record):
            lost_email.append(lead_id or "(missing Id)")
        if not hygiene_phone_blank_allowed(_source_phone(record, prior), record):
            lost_phone.append(lead_id or "(missing Id)")
    violations = []
    if lost_email:
        violations.append(
            "Email was blanked though the source address was keepable: "
            f"{_sample(lost_email)}"
        )
    if lost_phone:
        violations.append(
            "Phone was blanked though the source number had a recoverable base: "
            f"{_sample(lost_phone)}"
        )
    return violations


def check_enrich_preservation(
    before,
    after_records,
    allowed_email_blank_ids=None,
) -> list:
    """
    Enrichment must not wipe a cell that arrived filled from deduped.csv.

    Email may go blank only for Ids on the explicit unvalidated-no-replacement
    list (filled by the later trust-gate step). Phone must never go blank here.
    """
    allowed = {cell(value) for value in (allowed_email_blank_ids or [])}
    after_by_id = {}
    for record in after_records:
        lead_id = cell(record.get("Id", ""))
        if lead_id:
            after_by_id[lead_id] = record
    lost_email, lost_phone = [], []
    for lead_id, prior in (before or {}).items():
        now = after_by_id.get(lead_id)
        if now is None:
            continue
        if cell(prior.get("Email", "")) and not cell(now.get("Email", "")):
            if lead_id not in allowed:
                lost_email.append(lead_id)
        if cell(prior.get("Phone", "")) and not cell(now.get("Phone", "")):
            lost_phone.append(lead_id)
    violations = []
    if lost_email:
        violations.append(
            "Enrichment blanked Email without the unvalidated-no-replacement path: "
            f"{_sample(lost_email)}"
        )
    if lost_phone:
        violations.append(
            "Enrichment blanked a Phone that was present on deduped.csv: "
            f"{_sample(lost_phone)}"
        )
    return violations


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


def row_level_violations(record) -> list:
    """
    Reasons this one row must not be written back. Empty means the row is clean.

    Only faults that belong to a single record appear here, so a caller can
    quarantine the row and keep the rest of the batch. Batch-shaped faults
    (row count grew, a lead vanished, duplicate emails survived) are excluded
    on purpose: those mean the pipeline misbehaved, not that one lead is
    dirty, and they must keep failing the run.
    """
    reasons = []
    for column in ("Phone", "MobilePhone"):
        value = record.get(column)
        if phone_has_text_marker(value):
            reasons.append(f"{column} carries an Excel text marker")
        if phone_looks_like_excel_formula(value):
            reasons.append(f"{column} would be evaluated as an Excel formula")
    if phone_lost_country_code(record.get("Phone_raw", record.get("Phone")), record.get("Phone")):
        reasons.append("Phone lost a country code that was present on input")
    if phone_lost_country_code(record.get("MobilePhone_raw", ""), record.get("MobilePhone")):
        reasons.append("MobilePhone lost a country code that was present on input")
    if company_has_bad_dotted_case(record.get("Company")):
        reasons.append("Company has lowercase after a dot outside the allowlist")
    if is_free_provider_website(record.get("Website")):
        reasons.append(
            f"Website points at a free mail provider ({cell(record.get('Website'))})"
        )
    return reasons


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


def check_drop_provenance(
    input_ids,
    output_ids,
    merge_log=None,
    dropped_test_ids=None,
    quarantined_ids=None,
) -> list:
    """
    Every input Id missing from write-back must be either an explicit test-data
    drop, a row held back for review, or absorbed into a survivor via email,
    phone, or name+company.
    """
    violations = []
    inputs = set(_ids(input_ids))
    outputs = set(_ids(output_ids))
    dropped_test = set(_ids(dropped_test_ids)) | set(_ids(quarantined_ids))
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


def check_records(
    records,
    rows_in=None,
    input_ids=None,
    merge_log=None,
    dropped_test_ids=None,
    quarantined_ids=None,
    source_contacts=None,
) -> list:
    """Return a list of human-readable invariant violations (empty means clean)."""
    violations = []
    records = list(records)

    violations.extend(check_hygiene_preservation(records, source_contacts=source_contacts))

    marked = [cell(r.get("Id", "")) for r in records if phone_has_text_marker(r.get("Phone"))]
    marked += [cell(r.get("Id", "")) for r in records if phone_has_text_marker(r.get("MobilePhone"))]
    if marked:
        violations.append(f"Phone values carry an Excel text marker: {_sample(marked)}")

    formulas = [cell(r.get("Id", "")) for r in records if phone_looks_like_excel_formula(r.get("Phone"))]
    formulas += [cell(r.get("Id", "")) for r in records if phone_looks_like_excel_formula(r.get("MobilePhone"))]
    if formulas:
        violations.append(f"Phone values would be evaluated as Excel formulas: {_sample(formulas)}")

    lost_cc = [
        cell(r.get("Id", ""))
        for r in records
        if phone_lost_country_code(r.get("Phone_raw", r.get("Phone")), r.get("Phone"))
    ]
    lost_cc += [
        cell(r.get("Id", ""))
        for r in records
        if phone_lost_country_code(r.get("MobilePhone_raw", ""), r.get("MobilePhone"))
    ]
    if lost_cc:
        violations.append(
            f"Phone lost a country code that was present on input: {_sample(lost_cc)}"
        )

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
                quarantined_ids=quarantined_ids,
            )
        )

    return violations


def check_exported_schema(csv_text: str, expected_columns) -> list:
    """Re-read the write-back CSV and assert the header is exactly the source set."""
    import csv
    import io

    expected = [str(col) for col in expected_columns]
    try:
        header = next(csv.reader(io.StringIO(csv_text)))
    except Exception as exc:
        return [f"Exported CSV could not be re-read: {exc}"]
    if header != expected:
        return [f"Exported headers {header} != {expected}"]
    if any(name.startswith('"') or name.endswith('"') for name in header):
        return [f"Exported headers still carry quote characters: {header}"]
    return []


def assert_records(records, rows_in=None) -> None:
    violations = check_records(records, rows_in=rows_in)
    if violations:
        raise InvariantViolation("; ".join(violations))


def assert_dataframe(df, rows_in=None) -> None:
    assert_records(df.to_dict("records"), rows_in=rows_in)
