"""
enrich.py
Lusha Search and Enrich: fill blank lead fields without overwriting existing values.
https://docs.lusha.com/apis/openapi/search-and-enrich/searchandenrichcontacts
"""

import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import urlparse

import pandas as pd

from .dedupe import PUBLIC_DOMAINS
from .validate import BLANK_TOKENS

LUSHA_CONTACTS_URL = "https://api.lusha.com/v3/contacts/search-and-enrich"
LUSHA_COMPANIES_URL = "https://api.lusha.com/v3/companies/search-and-enrich"
LUSHA_BATCH_SIZE = 100
LUSHA_TIMEOUT_SECONDS = 45

PERSON_FIELDS = ("FirstName", "LastName", "Email", "Company", "Title", "Phone")
COMPANY_FIELDS = ("Industry", "Website", "AnnualRevenue", "NumberOfEmployees")
ENRICHABLE_FIELDS = PERSON_FIELDS + COMPANY_FIELDS
EMAIL_TYPE_ORDER = {"work": 0, "unknown": 1, "private": 2}
PHONE_TYPE_ORDER = {"work": 0, "direct": 1, "mobile": 2, "unknown": 3}


def _cell(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in BLANK_TOKENS else text


def _is_blank(value) -> bool:
    return not _cell(value)


def _needs_fill(row: pd.Series, fields) -> bool:
    return any(field in row.index and _is_blank(row.get(field)) for field in fields)


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
    return "" if domain in PUBLIC_DOMAINS else domain


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
    if field not in df.columns or not _cell(new_value) or not _is_blank(df.at[idx, field]):
        return 0
    df.at[idx, field] = new_value
    return 1


def _apply_contact(df: pd.DataFrame, idx, result: dict) -> int:
    filled = 0
    filled += _fill(df, idx, "FirstName", result.get("firstName"))
    filled += _fill(df, idx, "LastName", result.get("lastName"))
    filled += _fill(df, idx, "Email", _pick_email(result.get("emails")))
    filled += _fill(df, idx, "Phone", _pick_phone(result.get("phones")))

    job = result.get("jobTitle") or {}
    title = job.get("title") if isinstance(job, dict) else job
    filled += _fill(df, idx, "Title", title)

    company = result.get("company") or {}
    if isinstance(company, dict):
        filled += _fill(df, idx, "Company", company.get("name"))
        filled += _fill(df, idx, "Industry", company.get("industry"))
        domain = _cell(company.get("domain"))
        if domain and "://" not in domain:
            domain = f"https://{domain}"
        filled += _fill(df, idx, "Website", domain)
    return filled


def _apply_company(df: pd.DataFrame, idx, result: dict) -> int:
    filled = 0
    filled += _fill(df, idx, "Company", result.get("name"))
    filled += _fill(df, idx, "Industry", result.get("industry"))
    domain = _cell(result.get("domain"))
    if domain and "://" not in domain:
        domain = f"https://{domain}"
    filled += _fill(df, idx, "Website", domain)

    employees = result.get("employeeCount") or {}
    if isinstance(employees, dict):
        count = employees.get("exact")
        if count is None:
            count = employees.get("min")
        if count is not None:
            filled += _fill(df, idx, "NumberOfEmployees", str(int(count)))

    revenue = result.get("revenueRange") or {}
    if isinstance(revenue, dict) and revenue.get("min") is not None:
        filled += _fill(df, idx, "AnnualRevenue", str(int(revenue["min"])))
    return filled


def _api_key() -> str:
    return (os.environ.get("LUSHA_API_KEY") or os.environ.get("LUSHA_APIKEY") or "").strip()


def _lusha_post(url: str, payload: dict) -> dict:
    key = _api_key()
    if not key:
        return {
            "status": "error",
            "message": "LUSHA_API_KEY is not set. Add it to the environment and retry.",
        }

    body = json.dumps(payload).encode("utf-8")
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
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=LUSHA_TIMEOUT_SECONDS) as response:
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


def _reveal_for_rows(df: pd.DataFrame, indices) -> list:
    reveal = []
    if any("Email" in df.columns and _is_blank(df.at[idx, "Email"]) for idx in indices):
        reveal.append("emails")
    if any("Phone" in df.columns and _is_blank(df.at[idx, "Phone"]) for idx in indices):
        reveal.append("phones")
    return reveal


def enrich_dataframe(df: pd.DataFrame) -> tuple:
    """
    Search-and-enrich rows with missing enrichable fields.
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

    contact_jobs = []
    for idx, row in df.iterrows():
        if not _needs_fill(row, ENRICHABLE_FIELDS):
            stats["rows_skipped"] += 1
            continue
        item = _contact_identifier(row, str(idx))
        if not item:
            stats["rows_skipped"] += 1
            continue
        contact_jobs.append((idx, item))

    stats["rows_attempted"] = len(contact_jobs)

    for batch in _chunks(contact_jobs, LUSHA_BATCH_SIZE):
        indices = [idx for idx, _ in batch]
        payload = {
            "contacts": [item for _, item in batch],
            "options": {"includePartialProfiles": True},
        }
        reveal = _reveal_for_rows(df, indices)
        if reveal:
            payload["reveal"] = reveal

        response = _lusha_post(LUSHA_CONTACTS_URL, payload)
        if response.get("status") != "ok":
            return df, {**stats, "error": response.get("message", "Contact enrichment failed.")}

        billing = response.get("billing") or {}
        stats["credits_charged"] += int(billing.get("creditsCharged") or 0)

        by_ref = {}
        for result in response.get("results") or []:
            if isinstance(result, dict) and result.get("clientReferenceId") is not None:
                by_ref[str(result["clientReferenceId"])] = result

        for idx, item in batch:
            result = by_ref.get(item["clientReferenceId"])
            if not result or result.get("error"):
                stats["rows_not_found"] += 1
                continue
            filled = _apply_contact(df, idx, result)
            stats["fields_filled"] += filled
            stats["rows_matched"] += 1

    company_rows = []
    unique_items = []
    seen = {}
    for idx, row in df.iterrows():
        if not _needs_fill(row, COMPANY_FIELDS):
            continue
        name = _cell(row.get("Company", ""))
        domain = _company_domain(row)
        if not name and not domain:
            continue
        key = (name.lower(), domain.lower())
        company_rows.append((idx, key))
        if key in seen:
            continue
        seen[key] = str(idx)
        item = {"clientReferenceId": str(idx)}
        if name:
            item["name"] = name
        if domain:
            item["domain"] = domain
        unique_items.append(item)

    results_by_ref = {}
    for batch in _chunks(unique_items, LUSHA_BATCH_SIZE):
        payload = {
            "companies": batch,
            "options": {"includePartialProfiles": True},
        }
        response = _lusha_post(LUSHA_COMPANIES_URL, payload)
        if response.get("status") != "ok":
            return df, {**stats, "error": response.get("message", "Company enrichment failed.")}

        billing = response.get("billing") or {}
        stats["credits_charged"] += int(billing.get("creditsCharged") or 0)

        for result in response.get("results") or []:
            if isinstance(result, dict) and not result.get("error"):
                results_by_ref[str(result.get("clientReferenceId"))] = result

    contact_ids = {idx for idx, _ in contact_jobs}
    for idx, key in company_rows:
        result = results_by_ref.get(seen[key])
        if not result:
            continue
        filled = _apply_company(df, idx, result)
        stats["fields_filled"] += filled
        if filled and idx not in contact_ids:
            stats["rows_matched"] += 1

    return df, stats
