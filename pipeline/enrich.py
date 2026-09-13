"""
enrich.py
Lusha Search and Enrich: fill blank lead fields without overwriting
existing values, except an unvalidated Email which may be replaced by a
company-aligned address (or blanked when none is trusted).
https://docs.lusha.com/apis/openapi/search-and-enrich/searchandenrichcontacts
"""

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import urlparse

import pandas as pd

from .domains import (
    domain_matches_company,
    host_of,
    is_personal_domain,
    is_plausible_domain,
)
from .emailcheck import DELIVERABLE
from .normalize import normalize_industry
from .textnorm import BLANK_TOKENS, alias_key, email_domain

LUSHA_CONTACTS_URL = "https://api.lusha.com/v3/contacts/search-and-enrich"
LUSHA_COMPANIES_URL = "https://api.lusha.com/v3/companies/search-and-enrich"
LUSHA_BATCH_SIZE = 100
LUSHA_TIMEOUT_SECONDS = 45
# Cloudflare rejects the default urllib user-agent with error 1010.
LUSHA_USER_AGENT = "lead-hygiene-agent/1.0"

PERSON_FIELDS = ("FirstName", "LastName", "Email", "Company", "Title", "Phone")
COMPANY_FIELDS = ("Industry", "Website", "AnnualRevenue", "NumberOfEmployees")
ENRICHABLE_FIELDS = PERSON_FIELDS + COMPANY_FIELDS
EMAIL_TYPE_ORDER = {"work": 0, "unknown": 1, "private": 2}
PHONE_TYPE_ORDER = {"work": 0, "direct": 1, "mobile": 2, "unknown": 3}
# Lusha API list price per revealed data point.
REVEAL_COST = {"emails": 1, "phones": 5}
# Informal CSV headers still write back to the original column name.
COLUMN_ALIASES = {
    "AnnualRevenue": (
        "Annual Revenue",
        "annual_revenue",
        "Revenue",
    ),
    "NumberOfEmployees": (
        "No of employee",
        "No of employees",
        "Number of Employees",
        "NoOfEmployees",
        "Employees",
        "Employee Count",
    ),
}


def _cell(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in BLANK_TOKENS else text


def _is_blank(value) -> bool:
    return not _cell(value)


def _actual_column(columns, field: str):
    """Salesforce name, or the informal header the CSV actually uses."""
    lookup = {str(name).strip().lower(): name for name in columns}
    for candidate in (field, *COLUMN_ALIASES.get(field, ())):
        found = lookup.get(candidate.strip().lower())
        if found is not None:
            return found
    return None


def _needs_fill(row: pd.Series, fields) -> bool:
    return any(
        (col := _actual_column(row.index, field)) is not None and _is_blank(row.get(col))
        for field in fields
    )


def _lead_id(df: pd.DataFrame, idx) -> str:
    if "Id" in df.columns:
        return _cell(df.at[idx, "Id"])
    return str(idx)


def _email_host(email) -> str:
    return email_domain(email)


def _hosts_equivalent(left, right) -> bool:
    a, b = host_of(left), host_of(right)
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _email_is_unvalidated(row) -> bool:
    """True when hygiene said this address is not deliverable (MX miss, unknown)."""
    status = _cell(row.get("email_status", "")).lower()
    if not status:
        return False
    return status != DELIVERABLE


def _email_is_company_aligned(email, row) -> bool:
    """Accept a Lusha email only when its host belongs with this lead."""
    host = _email_host(email)
    if not host or is_personal_domain(host):
        return False
    company = _cell(row.get("Company", ""))
    if company and domain_matches_company(host, company):
        return True
    website_host = host_of(row.get("Website", ""))
    if website_host and not is_personal_domain(website_host) and _hosts_equivalent(host, website_host):
        return True
    current_host = _email_host(row.get("Email", ""))
    if current_host and not is_personal_domain(current_host) and _hosts_equivalent(host, current_host):
        return True
    return False


def _website_is_trusted(url, row) -> bool:
    host = host_of(url)
    if not host or not is_plausible_domain(host):
        return False
    company = _cell(row.get("Company", ""))
    if company and domain_matches_company(host, company):
        return True
    existing = host_of(row.get("Website", ""))
    email_host = _email_host(row.get("Email", ""))
    if email_host and is_personal_domain(email_host):
        email_host = ""
    preferred = existing or email_host
    return bool(preferred) and _hosts_equivalent(host, preferred)


def _needs_email_reveal(row) -> bool:
    col = _actual_column(row.index, "Email")
    if col is None:
        return False
    if _is_blank(row.get(col)):
        return True
    return _email_is_unvalidated(row)


def _add_review(stats: dict, df: pd.DataFrame, idx, reason: str) -> None:
    lead_id = _lead_id(df, idx)
    reviews = stats.setdefault("enrichment_review", [])
    for item in reviews:
        if item["id"] == lead_id:
            if reason not in item["reasons"]:
                item["reasons"].append(reason)
            return
    reviews.append({
        "id": lead_id,
        "email": _cell(df.at[idx, "Email"]) if "Email" in df.columns else "",
        "email_raw": _cell(df.at[idx, "Email_raw"]) if "Email_raw" in df.columns else "",
        "company": _cell(df.at[idx, "Company"]) if "Company" in df.columns else "",
        "website": _cell(df.at[idx, "Website"]) if "Website" in df.columns else "",
        "reasons": [reason],
    })


def _blank_unvalidated_email(df: pd.DataFrame, idx, stats: dict) -> None:
    col = _actual_column(df.columns, "Email")
    if col is None:
        return
    current = _cell(df.at[idx, col])
    if not current or not _email_is_unvalidated(df.loc[idx]):
        return
    df.at[idx, col] = ""
    stats.setdefault("email_blanked_no_replacement_ids", []).append(_lead_id(df, idx))
    _add_review(
        stats, df, idx,
        "unvalidated email had no company-aligned replacement",
    )


def _host_from_website(url: str) -> str:
    text = _cell(url)
    if not text:
        return ""
    if "://" not in text:
        text = f"https://{text}"
    try:
        host = (urlparse(text).hostname or "").lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _company_domain(row: pd.Series) -> str:
    host = _host_from_website(row.get("Website", ""))
    if host:
        return host
    email = _cell(row.get("Email", "")).lower()
    if "@" not in email:
        return ""
    domain = email.split("@", 1)[1]
    return "" if is_personal_domain(domain) else domain


def _contact_identifier(row: pd.Series, row_id: str) -> Optional[dict]:
    email = _cell(row.get("Email", "")).lower()
    first = _cell(row.get("FirstName", ""))
    last = _cell(row.get("LastName", ""))
    company = _cell(row.get("Company", ""))
    domain = _company_domain(row)

    item = {"clientReferenceId": row_id}
    if email:
        item["email"] = email
    if first:
        item["firstName"] = first
    if last:
        item["lastName"] = last
    if company:
        item["companyName"] = company
    if domain:
        item["companyDomain"] = domain

    has_email = "email" in item
    has_name_company = (
        "firstName" in item
        and "lastName" in item
        and ("companyName" in item or "companyDomain" in item)
    )
    if not has_email and not has_name_company:
        return None
    return item


def _pick_email(emails) -> str:
    if not isinstance(emails, list):
        return ""
    ranked = sorted(
        (e for e in emails if isinstance(e, dict) and _cell(e.get("email"))),
        key=lambda e: EMAIL_TYPE_ORDER.get(str(e.get("type") or "unknown"), 9),
    )
    return _cell(ranked[0]["email"]).lower() if ranked else ""


def _pick_phone(phones) -> str:
    if not isinstance(phones, list):
        return ""
    ranked = sorted(
        (p for p in phones if isinstance(p, dict) and _cell(p.get("number"))),
        key=lambda p: PHONE_TYPE_ORDER.get(str(p.get("type") or "unknown"), 9),
    )
    return _cell(ranked[0]["number"]) if ranked else ""


def _fill(df: pd.DataFrame, idx, field: str, new_value: str) -> int:
    col = _actual_column(df.columns, field)
    if col is None or not _cell(new_value) or not _is_blank(df.at[idx, col]):
        return 0
    df.at[idx, col] = new_value
    return 1


def _fill_website(df: pd.DataFrame, idx, url, stats: dict) -> int:
    col = _actual_column(df.columns, "Website")
    if col is None or not _cell(url) or not _is_blank(df.at[idx, col]):
        return 0
    if not _website_is_trusted(url, df.loc[idx]):
        stats["websites_rejected"] = stats.get("websites_rejected", 0) + 1
        _add_review(stats, df, idx, "rejected Lusha website (not company-aligned)")
        return 0
    text = _cell(url)
    if "://" not in text:
        text = f"https://{text}"
    df.at[idx, col] = text
    return 1


def _apply_email(df: pd.DataFrame, idx, candidate: str, stats: dict, reveal) -> int:
    if "emails" not in (reveal or ()):
        return 0
    col = _actual_column(df.columns, "Email")
    if col is None:
        return 0
    row = df.loc[idx]
    current = _cell(df.at[idx, col])
    if candidate and _email_is_company_aligned(candidate, row):
        if current.lower() == candidate.lower():
            return 0
        df.at[idx, col] = candidate
        return 1
    if candidate:
        stats["emails_rejected"] = stats.get("emails_rejected", 0) + 1
        _add_review(stats, df, idx, "rejected Lusha email (not company-aligned)")
    if current and _email_is_unvalidated(row):
        _blank_unvalidated_email(df, idx, stats)
    return 0


def _apply_contact(df: pd.DataFrame, idx, result: dict, stats: dict, reveal=()) -> int:
    filled = 0
    filled += _fill(df, idx, "FirstName", result.get("firstName"))
    filled += _fill(df, idx, "LastName", result.get("lastName"))
    filled += _apply_email(df, idx, _pick_email(result.get("emails")), stats, reveal)
    phone_col = _actual_column(df.columns, "Phone")
    if phone_col is None or _is_blank(df.at[idx, phone_col]):
        filled += _fill(df, idx, "Phone", _pick_phone(result.get("phones")))

    job = result.get("jobTitle") or {}
    title = job.get("title") if isinstance(job, dict) else job
    filled += _fill(df, idx, "Title", title)

    company = result.get("company") or {}
    if isinstance(company, dict):
        filled += _fill(df, idx, "Company", company.get("name"))
        filled += _fill(df, idx, "Industry", company.get("industry"))
        domain = _cell(company.get("domain"))
        filled += _fill_website(df, idx, domain, stats)
    return filled


def _as_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _record_revenue_range(stats: dict, df: pd.DataFrame, idx, revenue: dict) -> None:
    rmin = _as_int(revenue.get("min"))
    rmax = _as_int(revenue.get("max"))
    if rmin is None and rmax is None:
        return
    stats.setdefault("revenue_ranges", []).append({
        "id": _lead_id(df, idx),
        "revenue_min": "" if rmin is None else rmin,
        "revenue_max": "" if rmax is None else rmax,
    })


def _apply_revenue(df: pd.DataFrame, idx, result: dict, stats: dict) -> int:
    """
    AnnualRevenue is an exact figure. Lusha's revenueRange.min/max is a
    bucket, so it is stored in stats only and never written to the CSV.
    """
    revenue = result.get("revenueRange")
    if not isinstance(revenue, dict):
        revenue = {}
    exact = _as_int(revenue.get("exact"))
    if exact is None and not isinstance(result.get("revenue"), dict):
        exact = _as_int(result.get("revenue"))
    if exact is not None:
        return _fill(df, idx, "AnnualRevenue", str(exact))
    _record_revenue_range(stats, df, idx, revenue)
    return 0


def _apply_company(df: pd.DataFrame, idx, result: dict, stats: dict) -> int:
    filled = 0
    filled += _fill(df, idx, "Company", result.get("name"))
    filled += _fill(df, idx, "Industry", result.get("industry"))
    filled += _fill_website(df, idx, result.get("domain"), stats)

    employees = result.get("employeeCount") or {}
    if isinstance(employees, dict):
        count = employees.get("exact")
        if count is None:
            count = employees.get("min")
        if count is not None:
            filled += _fill(df, idx, "NumberOfEmployees", str(int(count)))

    filled += _apply_revenue(df, idx, result, stats)
    return filled


def _api_key() -> str:
    return (os.environ.get("LUSHA_API_KEY") or os.environ.get("LUSHA_APIKEY") or "").strip()


def _tls_context() -> ssl.SSLContext:
    """
    A python.org macOS install ships no trust store, so the default context
    verifies against nothing and every HTTPS call fails. Fall back to the
    certifi bundle rather than weakening verification.
    """
    context = ssl.create_default_context()
    if context.get_ca_certs():
        return context
    try:
        import certifi
    except ImportError:
        return context
    return ssl.create_default_context(cafile=certifi.where())


def _lusha_post(url: str, payload: dict) -> dict:
    key = _api_key()
    if not key:
        return {
            "status": "error",
            "message": "LUSHA_API_KEY is not set. Add it to the environment and retry.",
        }

    body = json.dumps(payload).encode("utf-8")
    context = _tls_context()
    last_error = "Lusha request failed."
    for attempt in range(3):
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "api_key": key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": LUSHA_USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=LUSHA_TIMEOUT_SECONDS, context=context
            ) as response:
                raw = response.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
            data["status"] = "ok"
            return data
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(detail)
                last_error = parsed.get("message") or detail or f"Lusha HTTP {exc.code}"
            except json.JSONDecodeError:
                last_error = detail or f"Lusha HTTP {exc.code}"
            if exc.code == 429 and attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            return {"status": "error", "message": last_error, "http_status": exc.code}
        except urllib.error.URLError as exc:
            last_error = f"Could not reach Lusha: {exc.reason}"
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            return {"status": "error", "message": last_error}
        except Exception as exc:
            return {"status": "error", "message": f"Lusha request failed: {exc}"}
    return {"status": "error", "message": last_error}


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _needed_reveal(df: pd.DataFrame, idx) -> tuple:
    """Paid data points this one row is missing. Never widened to the batch."""
    reveal = []
    if _needs_email_reveal(df.loc[idx]):
        reveal.append("emails")
    phone_col = _actual_column(df.columns, "Phone")
    if phone_col is not None and _is_blank(df.at[idx, phone_col]):
        reveal.append("phones")
    return tuple(reveal)


def _company_key(row: pd.Series):
    """Domain first so 'CEA' and 'Globalcorp' on globalcorp.com are one lookup."""
    domain = _company_domain(row).lower()
    if domain:
        return ("domain", domain)
    name = alias_key(row.get("Company", ""))
    return ("name", name) if name else None


def _enrich_companies(df: pd.DataFrame, stats: dict) -> tuple:
    """One 1-credit company profile per unique company, copied onto every row that shares it."""
    rows_by_key = {}
    for idx, row in df.iterrows():
        if not _needs_fill(row, COMPANY_FIELDS):
            continue
        key = _company_key(row)
        if key is None:
            continue
        rows_by_key.setdefault(key, []).append(idx)

    items = []
    key_by_ref = {}
    for number, (key, indices) in enumerate(rows_by_key.items()):
        row = df.loc[indices[0]]
        ref = f"co{number}"
        item = {"clientReferenceId": ref}
        name = _cell(row.get("Company", ""))
        domain = _company_domain(row)
        if name:
            item["name"] = name
        if domain:
            item["domain"] = domain
        items.append(item)
        key_by_ref[ref] = key

    sent, matched = set(), set()
    results_by_key = {}
    for batch in _chunks(items, LUSHA_BATCH_SIZE):
        payload = {"companies": batch, "options": {"includePartialProfiles": True}}
        response = _lusha_post(LUSHA_COMPANIES_URL, payload)
        if response.get("status") != "ok":
            return sent, matched, response.get("message", "Company enrichment failed.")

        billing = response.get("billing") or {}
        stats["credits_charged"] += int(billing.get("creditsCharged") or 0)

        for item in batch:
            sent.update(rows_by_key[key_by_ref[item["clientReferenceId"]]])
        for result in response.get("results") or []:
            if not isinstance(result, dict) or result.get("error"):
                continue
            key = key_by_ref.get(str(result.get("clientReferenceId")))
            if key is not None:
                results_by_key[key] = result

    for key, result in results_by_key.items():
        for idx in rows_by_key[key]:
            filled = _apply_company(df, idx, result, stats)
            stats["fields_filled"] += filled
            if filled:
                matched.add(idx)
    return sent, matched, None


def _enrich_contacts(df: pd.DataFrame, stats: dict) -> tuple:
    """
    One request per reveal cohort. A row that already has a phone is never
    billed for one, and rows missing only profile fields reveal nothing.
    """
    cohorts = {}
    for idx, row in df.iterrows():
        if not (_needs_fill(row, PERSON_FIELDS) or _needs_email_reveal(row)):
            continue
        item = _contact_identifier(row, str(idx))
        if not item:
            continue
        cohorts.setdefault(_needed_reveal(df, idx), []).append((idx, item))

    sent, matched = set(), set()
    # Cheapest cohorts first, so a credit limit is hit on phones rather than emails.
    for reveal in sorted(cohorts, key=lambda r: sum(REVEAL_COST[field] for field in r)):
        jobs = cohorts[reveal]
        for batch in _chunks(jobs, LUSHA_BATCH_SIZE):
            payload = {
                "contacts": [item for _, item in batch],
                "reveal": list(reveal),
                "options": {"includePartialProfiles": True},
            }
            response = _lusha_post(LUSHA_CONTACTS_URL, payload)
            if response.get("status") != "ok":
                if reveal:
                    return sent, matched, response.get("message", "Contact enrichment failed.")
                # Profile-only lookups are optional; skip them rather than fail the run.
                stats["profile_lookup_skipped"] = True
                break

            billing = response.get("billing") or {}
            stats["credits_charged"] += int(billing.get("creditsCharged") or 0)

            by_ref = {}
            for result in response.get("results") or []:
                if isinstance(result, dict) and result.get("clientReferenceId") is not None:
                    by_ref[str(result["clientReferenceId"])] = result

            for idx, item in batch:
                sent.add(idx)
                result = by_ref.get(item["clientReferenceId"])
                if not result or result.get("error"):
                    if "emails" in reveal:
                        _blank_unvalidated_email(df, idx, stats)
                    continue
                stats["fields_filled"] += _apply_contact(
                    df, idx, result, stats, reveal=reveal,
                )
                matched.add(idx)
    return sent, matched, None


def enrich_dataframe(df: pd.DataFrame) -> tuple:
    """
    Search-and-enrich rows with missing enrichable fields.

    Companies run first: a 1-credit company profile fills Website, Industry and
    firmographics for every row sharing that company, which keeps rows that were
    only missing company data out of the pricier contact calls.
    Returns (df, stats).
    """
    df = df.copy()
    stats = {
        "rows_attempted": 0,
        "rows_matched": 0,
        "rows_not_found": 0,
        "rows_skipped": 0,
        "fields_filled": 0,
        "credits_charged": 0,
        "email_blanked_no_replacement_ids": [],
        "enrichment_review": [],
        "emails_rejected": 0,
        "websites_rejected": 0,
        "revenue_ranges": [],
    }

    company_sent, company_matched, error = _enrich_companies(df, stats)
    if error:
        return df, {**stats, "error": error}

    contact_sent, contact_matched, error = _enrich_contacts(df, stats)
    if error:
        return df, {**stats, "error": error}

    sent = company_sent | contact_sent
    matched = company_matched | contact_matched
    stats["rows_attempted"] = len(sent)
    stats["rows_matched"] = len(matched)
    stats["rows_not_found"] = len(sent - matched)
    stats["rows_skipped"] = len(df) - len(sent)
    _normalize_industries(df, stats)
    _flag_company_mismatches(df, stats)
    return df, stats


def _normalize_industries(df: pd.DataFrame, stats: dict) -> None:
    """Map Lusha (and leftover) Industry values onto the closed picklist."""
    col = _actual_column(df.columns, "Industry")
    if col is None:
        return
    for idx in df.index:
        raw = df.at[idx, col]
        if _is_blank(raw):
            continue
        mapped, unknown = normalize_industry(raw)
        if mapped != _cell(raw):
            df.at[idx, col] = mapped
        if unknown:
            _add_review(
                stats, df, idx,
                "unmapped industry (not on the Salesforce picklist)",
            )


def _flag_company_mismatches(df: pd.DataFrame, stats: dict) -> None:
    """Flag leftover email/website vs Company mismatches for the review file."""
    for idx, row in df.iterrows():
        company = _cell(row.get("Company", ""))
        email = _cell(row.get("Email", ""))
        host = _email_host(email)
        if company and host and not is_personal_domain(host) and not domain_matches_company(host, company):
            _add_review(stats, df, idx, "email domain does not match Company")
        site_host = host_of(row.get("Website", ""))
        if not site_host:
            continue
        if not is_plausible_domain(site_host):
            _add_review(stats, df, idx, "website is not a plausible company domain")
        elif company and not domain_matches_company(site_host, company):
            _add_review(stats, df, idx, "website does not match Company")
