"""Validate rows, flag quality issues, and hold hard-required failures for HITL."""

import re

import pandas as pd

from .domains import (
    domain_matches_company,
    host_of,
    is_personal_domain,
    is_placeholder_domain,
    is_plausible_domain,
)
from .config import DUMMY_FULL_NAMES, TEST_GIVEN_NAMES, TEST_SURNAMES
from .phone import is_junk_phone
from .textnorm import cell, digits_only, email_domain, fold_text, strip_excel_artifacts

FORMULA_PREFIXES = ("=", "@")
PHONE_COLS = {"phone", "mobilephone", "mobile"}
RFC_EMAIL = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
TEST_COMPANY_KEYS = frozenset({
    "test", "test arp", "dummy", "dummy company", "fake company", "sample company",
})
TEST_EMAIL_HOSTS = frozenset({"test.com", "test.org", "test.net", "testing.com"})
AUDIT_COLS = ("data_quality_flags", "hitl_review")


def _strip_formula(value, *, phone_field: bool = False) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if phone_field:
        return strip_excel_artifacts(value)
    text = str(value).strip()
    if text.startswith("'") and len(text) > 1 and text[1] in "+-=":
        text = text[1:]
    while text.startswith(FORMULA_PREFIXES):
        text = text[1:]
    return text


def _add_flag(current: str, flag: str) -> str:
    flags = [part for part in (current or "").split("|") if part]
    if flag not in flags:
        flags.append(flag)
    return "|".join(flags)


def _website_host(value: str) -> str:
    text = cell(value)
    if not text or ("@" in text and "://" not in text):
        return ""
    return host_of(text)


def _garbage_name(value: str) -> bool:
    text = cell(value)
    if not text:
        return False
    if text.isdigit():
        return True
    return len(text) == 1 and not text.isalpha()


WEBSITE_LOOKS_LIKE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PLACEHOLDER_CONTACT = frozenset({"test", "n/a", "na", "none", "null", "unknown", "-"})
COMPLETENESS_FIELDS = ("Title", "Industry", "AnnualRevenue")


def _is_placeholder_contact(value) -> bool:
    text = cell(value)
    if not text:
        return True
    folded = fold_text(text)
    if folded in PLACEHOLDER_CONTACT or text.lower() in PLACEHOLDER_CONTACT:
        return True
    return len(folded) == 1


def is_junk_lead_values(company="", email="", phone="", *, has_phone: bool = True) -> bool:
    """True when Company, Email, and Phone are all missing or placeholder."""
    return (
        _is_placeholder_contact(company)
        and _is_placeholder_contact(email)
        and (not has_phone or _is_placeholder_contact(phone))
    )


def is_website_email(value) -> bool:
    text = cell(value)
    if not text or "://" in text:
        return False
    return bool(WEBSITE_LOOKS_LIKE_EMAIL.match(text) or RFC_EMAIL.match(text))


def is_test_name(first, last) -> bool:
    first_key, last_key = cell(first).lower(), cell(last).lower()
    if first_key in TEST_GIVEN_NAMES:
        return True
    if last_key in TEST_SURNAMES:
        return True
    return False


def is_test_company(company) -> bool:
    key = fold_text(company)
    if not key:
        return False
    if key in TEST_COMPANY_KEYS or key.startswith("test "):
        return True
    return False


def is_test_record(first="", last="", company="", email="") -> bool:
    if is_test_name(first, last) or is_test_company(company):
        return True
    host = email_domain(email)
    if host in TEST_EMAIL_HOSTS or host.endswith(".test"):
        return True
    return False


def validate_dataframe(df: pd.DataFrame) -> tuple:
    """
    Returns (df, issue_count).
    Adds data_quality_flags and hitl_review.
    Hard-required: Email OR Phone. Soft-required: FirstName, LastName.
    """
    issue_count = 0
    df = df.copy()
    for col in AUDIT_COLS:
        if col not in df.columns:
            df[col] = ""

    for col in df.columns:
        if col in AUDIT_COLS:
            continue
        phone_field = str(col).strip().lower() in PHONE_COLS
        df[col] = df[col].map(lambda v, phone_field=phone_field: _strip_formula(v, phone_field=phone_field))

    has_phone = "Phone" in df.columns
    has_website = "Website" in df.columns

    for idx in df.index:
        flags = ""
        hitl = False

        first = cell(df.at[idx, "FirstName"]) if "FirstName" in df.columns else ""
        last = cell(df.at[idx, "LastName"]) if "LastName" in df.columns else ""
        email = cell(df.at[idx, "Email"]) if "Email" in df.columns else ""
        phone = cell(df.at[idx, "Phone"]) if has_phone else ""
        company = cell(df.at[idx, "Company"]) if "Company" in df.columns else ""
        website = cell(df.at[idx, "Website"]) if has_website else ""

        if is_test_record(first, last, company, email):
            flags = _add_flag(flags, "test_data")
            hitl = True
            issue_count += 1

        # Drop later in process_csv: Company + Email + Phone all missing/placeholder.
        if is_junk_lead_values(company, email, phone, has_phone=has_phone):
            flags = _add_flag(flags, "junk_lead")
            hitl = True
            issue_count += 1

        if any(field in df.columns and not cell(df.at[idx, field]) for field in COMPLETENESS_FIELDS):
            flags = _add_flag(flags, "incomplete_profile")
            flags = _add_flag(flags, "completeness_flag")
            issue_count += 1

        if "FirstName" in df.columns and "LastName" in df.columns:
            pair = (first.lower(), last.lower())
            if pair in DUMMY_FULL_NAMES:
                df.at[idx, "FirstName"] = ""
                df.at[idx, "LastName"] = ""
                first = last = ""
                flags = _add_flag(flags, "placeholder_name")
                issue_count += 1
        if "FirstName" in df.columns and _garbage_name(first):
            df.at[idx, "FirstName"] = ""
            first = ""
            flags = _add_flag(flags, "garbage_first_name")
            issue_count += 1
        if "LastName" in df.columns and _garbage_name(last):
            df.at[idx, "LastName"] = ""
            last = ""
            flags = _add_flag(flags, "garbage_last_name")
            issue_count += 1

        if not first:
            flags = _add_flag(flags, "missing_first_name")
            issue_count += 1
        if not last:
            flags = _add_flag(flags, "missing_last_name")
            issue_count += 1

        email_ok = bool(email) and bool(RFC_EMAIL.match(email))
        if email and not email_ok:
            flags = _add_flag(flags, "invalid_email")
            issue_count += 1
            df.at[idx, "Email"] = ""
            email = ""
            email_ok = False
        elif email_ok and is_placeholder_domain(email_domain(email)):
            flags = _add_flag(flags, "placeholder_email")
            issue_count += 1
            df.at[idx, "Email"] = ""
            email = ""
            email_ok = False
        if not email:
            flags = _add_flag(flags, "missing_email")
            hitl = True
            issue_count += 1

        if has_phone and phone:
            digit_len = len(digits_only(phone))
            if 0 < digit_len < 7:
                flags = _add_flag(flags, "needs_country_code_review")
                hitl = True
                issue_count += 1
        if has_phone and phone and is_junk_phone(phone):
            df.at[idx, "Phone"] = ""
            phone = ""
            flags = _add_flag(flags, "garbage_phone")
            issue_count += 1
        if has_phone and not phone:
            flags = _add_flag(flags, "missing_phone")
            hitl = True
            issue_count += 1

        if not email_ok and not phone:
            flags = _add_flag(flags, "missing_hard_required")
            hitl = True
            issue_count += 1

        if has_website and website:
            if is_website_email(website):
                # Flag only. Normalization may clear the type-mismatch; do not
                # drop the lead, and do not hide the error by silent-clearing first.
                flags = _add_flag(flags, "website_is_email")
                hitl = True
                issue_count += 1
            host = _website_host(website)
            if is_website_email(website):
                pass
            elif is_personal_domain(host):
                df.at[idx, "Website"] = ""
                flags = _add_flag(flags, "personal_website")
                hitl = True
                issue_count += 1
            elif is_placeholder_domain(host) or not is_plausible_domain(host):
                df.at[idx, "Website"] = ""
                flags = _add_flag(flags, "invalid_website")
                hitl = True
                issue_count += 1
            elif host and company and not domain_matches_company(host, company):
                flags = _add_flag(flags, "website_company_mismatch")
                hitl = True
                issue_count += 1

        if email_ok and company:
            host = email_domain(email)
            if (
                host
                and not is_personal_domain(host)
                and not is_placeholder_domain(host)
                and not domain_matches_company(host, company)
            ):
                flags = _add_flag(flags, "company_email_mismatch")
                hitl = True
                issue_count += 1

        df.at[idx, "data_quality_flags"] = flags
        df.at[idx, "hitl_review"] = "Yes" if hitl else ""

    return df, issue_count
