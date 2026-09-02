"""
enrich.py
Lusha Search and Enrich: fill blank lead fields without overwriting existing values.
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

from .domains import is_personal_domain
from .textnorm import BLANK_TOKENS, alias_key

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


def cell(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in BLANK_TOKENS else text


def is_blank(value) -> bool:
    return not cell(value)


def actual_column(columns, field: str):
    """Salesforce name, or the informal header the CSV actually uses."""
    lookup = {str(name).strip().lower(): name for name in columns}
    for candidate in (field, *COLUMN_ALIASES.get(field, ())):
        found = lookup.get(candidate.strip().lower())
        if found is not None:
            return found
    return None


def needs_fill(row: pd.Series, fields) -> bool:
    return any(
        (col := actual_column(row.index, field)) is not None and is_blank(row.get(col))
        for field in fields
    )


def host_from_website(url: str) -> str:
    text = cell(url)
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


def company_domain(row: pd.Series) -> str:
    host = host_from_website(row.get("Website", ""))
    if host:
        return host
    email = cell(row.get("Email", "")).lower()
    if "@" not in email:
        return ""
    domain = email.split("@", 1)[1]
    return "" if is_personal_domain(domain) else domain


def contact_identifier(row: pd.Series, row_id: str) -> Optional[dict]:
    email = cell(row.get("Email", "")).lower()
    first = cell(row.get("FirstName", ""))
    last = cell(row.get("LastName", ""))
    company = cell(row.get("Company", ""))
    domain = company_domain(row)

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


def pick_email(emails) -> str:
    if not isinstance(emails, list):
        return ""
    ranked = sorted(
        (e for e in emails if isinstance(e, dict) and cell(e.get("email"))),
        key=lambda e: EMAIL_TYPE_ORDER.get(str(e.get("type") or "unknown"), 9),
    )
    return cell(ranked[0]["email"]).lower() if ranked else ""


def pick_phone(phones) -> str:
    if not isinstance(phones, list):
        return ""
    ranked = sorted(
        (p for p in phones if isinstance(p, dict) and cell(p.get("number"))),
        key=lambda p: PHONE_TYPE_ORDER.get(str(p.get("type") or "unknown"), 9),
    )
    return cell(ranked[0]["number"]) if ranked else ""


def fill(df: pd.DataFrame, idx, field: str, new_value: str) -> int:
    col = actual_column(df.columns, field)
    if col is None or not cell(new_value) or not is_blank(df.at[idx, col]):
        return 0
    df.at[idx, col] = new_value
    return 1


def apply_contact(df: pd.DataFrame, idx, result: dict) -> int:
    filled = 0
    filled += fill(df, idx, "FirstName", result.get("firstName"))
    filled += fill(df, idx, "LastName", result.get("lastName"))
    filled += fill(df, idx, "Email", pick_email(result.get("emails")))
    filled += fill(df, idx, "Phone", pick_phone(result.get("phones")))

    job = result.get("jobTitle") or {}
    title = job.get("title") if isinstance(job, dict) else job
    filled += fill(df, idx, "Title", title)

    company = result.get("company") or {}
    if isinstance(company, dict):
        filled += fill(df, idx, "Company", company.get("name"))
        filled += fill(df, idx, "Industry", company.get("industry"))
        domain = cell(company.get("domain"))
        if domain and "://" not in domain:
            domain = f"https://{domain}"
        filled += fill(df, idx, "Website", domain)
    return filled


def apply_company(df: pd.DataFrame, idx, result: dict) -> int:
    filled = 0
    filled += fill(df, idx, "Company", result.get("name"))
    filled += fill(df, idx, "Industry", result.get("industry"))
    domain = cell(result.get("domain"))
    if domain and "://" not in domain:
        domain = f"https://{domain}"
    filled += fill(df, idx, "Website", domain)

    employees = result.get("employeeCount") or {}
    if isinstance(employees, dict):
        count = employees.get("exact")
        if count is None:
            count = employees.get("min")
        if count is not None:
            filled += fill(df, idx, "NumberOfEmployees", str(int(count)))

    revenue = result.get("revenueRange") or {}
    if isinstance(revenue, dict) and revenue.get("min") is not None:
        filled += fill(df, idx, "AnnualRevenue", str(int(revenue["min"])))
    return filled


def api_key() -> str:
    return (os.environ.get("LUSHA_API_KEY") or os.environ.get("LUSHA_APIKEY") or "").strip()


def tls_context() -> ssl.SSLContext:
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


def lusha_post(url: str, payload: dict) -> dict:
    key = api_key()
    if not key:
        return {
            "status": "error",
            "message": "LUSHA_API_KEY is not set. Add it to the environment and retry.",
        }

    body = json.dumps(payload).encode("utf-8")
    context = tls_context()
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


def chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def needed_reveal(df: pd.DataFrame, idx) -> tuple:
    """Paid data points this one row is missing. Never widened to the batch."""
    reveal = []
    if "Email" in df.columns and is_blank(df.at[idx, "Email"]):
        reveal.append("emails")
    if "Phone" in df.columns and is_blank(df.at[idx, "Phone"]):
        reveal.append("phones")
    return tuple(reveal)


def company_key(row: pd.Series):
    """Domain first so 'CEA' and 'Globalcorp' on globalcorp.com are one lookup."""
    domain = company_domain(row).lower()
    if domain:
        return ("domain", domain)
    name = alias_key(row.get("Company", ""))
    return ("name", name) if name else None


def enrich_companies(df: pd.DataFrame, stats: dict) -> tuple:
    """One 1-credit company profile per unique company, copied onto every row that shares it."""
    rows_by_key = {}
    for idx, row in df.iterrows():
        if not needs_fill(row, COMPANY_FIELDS):
            continue
        key = company_key(row)
        if key is None:
            continue
        rows_by_key.setdefault(key, []).append(idx)

    items = []
    key_by_ref = {}
    for number, (key, indices) in enumerate(rows_by_key.items()):
        row = df.loc[indices[0]]
        ref = f"co{number}"
        item = {"clientReferenceId": ref}
        name = cell(row.get("Company", ""))
        domain = company_domain(row)
        if name:
            item["name"] = name
        if domain:
            item["domain"] = domain
        items.append(item)
        key_by_ref[ref] = key

    sent, matched = set(), set()
    results_by_key = {}
    for batch in chunks(items, LUSHA_BATCH_SIZE):
        payload = {"companies": batch, "options": {"includePartialProfiles": True}}
        response = lusha_post(LUSHA_COMPANIES_URL, payload)
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
            filled = apply_company(df, idx, result)
            stats["fields_filled"] += filled
            if filled:
                matched.add(idx)
    return sent, matched, None


def enrich_contacts(df: pd.DataFrame, stats: dict) -> tuple:
    """
    One request per reveal cohort. A row that already has a phone is never
    billed for one, and rows missing only profile fields reveal nothing.
    """
    cohorts = {}
    for idx, row in df.iterrows():
        if not needs_fill(row, PERSON_FIELDS):
            continue
        item = contact_identifier(row, str(idx))
        if not item:
            continue
        cohorts.setdefault(needed_reveal(df, idx), []).append((idx, item))

    sent, matched = set(), set()
    # Cheapest cohorts first, so a credit limit is hit on phones rather than emails.
    for reveal in sorted(cohorts, key=lambda r: sum(REVEAL_COST[field] for field in r)):
        jobs = cohorts[reveal]
        for batch in chunks(jobs, LUSHA_BATCH_SIZE):
            payload = {
                "contacts": [item for _, item in batch],
                "reveal": list(reveal),
                "options": {"includePartialProfiles": True},
            }
            response = lusha_post(LUSHA_CONTACTS_URL, payload)
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
                    continue
                stats["fields_filled"] += apply_contact(df, idx, result)
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
    }

    company_sent, company_matched, error = enrich_companies(df, stats)
    if error:
        return df, {**stats, "error": error}

    contact_sent, contact_matched, error = enrich_contacts(df, stats)
    if error:
        return df, {**stats, "error": error}

    sent = company_sent | contact_sent
    matched = company_matched | contact_matched
    stats["rows_attempted"] = len(sent)
    stats["rows_matched"] = len(matched)
    stats["rows_not_found"] = len(sent - matched)
    stats["rows_skipped"] = len(df) - len(sent)
    return df, stats
