"""
normalize.py
Rewrites field values in place without adding extra columns.
"""

import re
from urllib.parse import urlparse, urlunparse

import pandas as pd
import phonenumbers

from .taxonomy import TAXONOMY

SOCIAL_HOSTS = {
    "facebook.com", "twitter.com", "x.com", "linkedin.com",
    "instagram.com", "youtube.com", "tiktok.com",
}


def _cell(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat", "n/a", "unknown", "-", "--"} else text


def _tax_key(value: str) -> str:
    return re.sub(r"[,.]", "", (value or "").strip().lower())


def _lookup(section: str, value: str):
    return TAXONOMY.get(section, {}).get(_tax_key(value))


def normalize_name(first_name: str, last_name: str) -> tuple:
    """Proper cases names, cleans honorifics, handles initials like 'Jk' -> 'J.K.'."""
    fn = _cell(first_name)
    ln = _cell(last_name)

    fn = re.sub(r"^(Mr\.|Ms\.|Mrs\.|Dr\.|Jr\.|Sr\.)\s+", "", fn, flags=re.IGNORECASE)

    if re.match(r"^[A-Za-z][a-z]$", fn) and not re.search(r"[aeiou]", fn, re.I):
        fn = f"{fn[0].upper()}.{fn[1].upper()}."

    return fn.title() if fn else "", ln.title() if ln else ""


COUNTRY_CALLING = (
    "971", "966", "81", "82", "86", "91", "61", "65", "44", "49",
    "33", "31", "32", "34", "39", "41", "43", "46", "47", "48",
    "52", "55", "62", "63", "66", "90", "92", "93", "94", "95",
    "20", "27", "1",
)


def _phone_digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def _format_us(digits: str) -> str:
    return f"+1-{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"


def _format_e164(e164: str) -> str:
    if e164.startswith("+1") and len(e164) == 12:
        return _format_us(e164[2:])
    digits = e164[1:] if e164.startswith("+") else e164
    if digits.startswith("44") and len(digits) == 12 and digits[2] == "7":
        rest = digits[2:]
        return f"+44-{rest[0:4]}-{rest[4:7]}-{rest[7:10]}"
    for cc in COUNTRY_CALLING:
        if digits.startswith(cc) and len(digits) > len(cc) + 4:
            return f"+{cc}-{digits[len(cc):]}"
    return e164 if e164.startswith("+") else "+" + digits


def _try_parse_phone(raw: str, region, require_valid: bool = False) -> str:
    try:
        parsed = phonenumbers.parse(raw, region)
    except Exception:
        return ""
    if not phonenumbers.is_possible_number(parsed):
        return ""
    if require_valid and not phonenumbers.is_valid_number(parsed):
        return ""
    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    return _format_e164(e164)


def normalize_phone(phone_raw: str, default_region: str = "US") -> str:
    """Format phones; blank dummy or too-short values."""
    raw = _cell(phone_raw)
    if not raw:
        return ""
    digits = _phone_digits(raw)
    if not digits or set(digits) <= {"0"} or raw in {"000-000-000", "1234567890"}:
        return ""
    if len(digits) < 7:
        return ""

    candidates = []
    if raw.startswith("+") or raw.startswith("00"):
        candidates.append(_try_parse_phone(raw, None))
    candidates.append(_try_parse_phone(raw, default_region))
    if digits.startswith("0"):
        for region in ("GB", "DE", "FR", "IN", "JP"):
            candidates.append(_try_parse_phone(raw, region, require_valid=True))
    for formatted in candidates:
        if formatted:
            return formatted

    if digits.startswith("0"):
        national = digits.lstrip("0")
        if len(national) == 10 and national.startswith("7"):
            return f"+44-{national[0:4]}-{national[4:7]}-{national[7:10]}"
        digits = national
        if len(digits) < 7:
            return ""

    if len(digits) == 11 and digits.startswith("1"):
        return _format_us(digits[1:])
    if len(digits) == 10 and not (phone_raw or "").strip().startswith("0"):
        return _format_us(digits)

    for cc in COUNTRY_CALLING:
        if digits.startswith(cc) and len(digits) > len(cc) + 4:
            return f"+{cc}-{digits[len(cc):]}"
    return "+" + digits


def normalize_company(company_raw: str) -> str:
    """Maps company to canonical taxonomy or cleans legal suffixes."""
    raw_clean = _cell(company_raw)
    if not raw_clean:
        return ""

    if _lookup("invalid", raw_clean) == "INVALID":
        return ""

    mapped = _lookup("company", raw_clean)
    if mapped:
        return mapped

    cleaned = re.sub(r"\.{2,}", ".", raw_clean)
    cleaned = re.sub(r"\band\b", "&", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bInc\.\.\b", "Inc.", cleaned, flags=re.IGNORECASE)
    return cleaned.title()


def normalize_title(title_raw: str) -> str:
    """Applies title taxonomy or title cases job titles."""
    raw_clean = _cell(title_raw)
    if not raw_clean:
        return ""

    mapped = _lookup("title", raw_clean)
    if mapped:
        return mapped

    title_tax = TAXONOMY.get("title", {})
    words = raw_clean.split()
    norm_words = [title_tax.get(_tax_key(w), w.capitalize()) for w in words]
    return " ".join(norm_words)


def normalize_website(url_raw: str) -> str:
    """Standardizes URLs to https://host with no path or tracking params."""
    url = _cell(url_raw)
    if not url:
        return ""
    if "@" in url and "://" not in url:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = f"https://{url}"
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or host in SOCIAL_HOSTS:
        return ""
    return urlunparse(("https", host, "", "", "", ""))


def _apply_map(section: str, value: str, title_case: bool = False) -> str:
    text = _cell(value)
    if not text:
        return ""
    mapped = _lookup(section, text)
    if mapped:
        return mapped
    return text.title() if title_case else text


def normalize_street(value: str) -> str:
    text = _cell(value)
    if not text:
        return ""
    street = TAXONOMY.get("street", {})
    words = []
    for word in text.split():
        mapped = street.get(_tax_key(word))
        words.append(mapped or word.title())
    return " ".join(words)


def normalize_revenue(value: str) -> str:
    text = _cell(value)
    if not text or text.startswith("-"):
        return ""
    compact = text.replace(",", "").replace(" ", "").replace("$", "").replace("€", "")
    compact = compact.replace("usd", "").replace("USD", "")
    match = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*([kmb])?", compact, flags=re.I)
    if not match:
        digits = re.sub(r"[^\d.]", "", text)
        return digits if digits else text
    number = float(match.group(1))
    suffix = (match.group(2) or "").lower()
    factor = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[suffix]
    return str(int(number * factor))


def normalize_employees(value: str) -> str:
    text = _cell(value)
    if not text:
        return ""
    text = text.replace(",", "").replace("+", "")
    match = re.search(r"([0-9]*\.?[0-9]+)\s*([km])?", text, flags=re.I)
    if not match:
        return ""
    number = float(match.group(1))
    if number <= 0:
        return ""
    suffix = (match.group(2) or "").lower()
    factor = {"": 1, "k": 1_000, "m": 1_000_000}[suffix]
    return str(int(number * factor))


def _set_if_changed(df, idx, field, new_value, old_value) -> int:
    if field not in df.columns:
        return 0
    new_value = "" if new_value is None else str(new_value)
    old_text = "" if old_value is None or (isinstance(old_value, float) and pd.isna(old_value)) else str(old_value)
    if new_value != old_text:
        df.at[idx, field] = new_value
        return 1
    return 0


def normalize_dataframe(df: pd.DataFrame) -> tuple:
    """Normalizes all fields in place and returns (df, normalized_values_count)."""
    df = df.copy()
    norm_count = 0

    for idx, row in df.iterrows():
        fn, ln = normalize_name(row.get("FirstName"), row.get("LastName"))
        norm_count += _set_if_changed(df, idx, "FirstName", fn, row.get("FirstName"))
        norm_count += _set_if_changed(df, idx, "LastName", ln, row.get("LastName"))

        email = _cell(row.get("Email", "")).lower()
        norm_count += _set_if_changed(df, idx, "Email", email, row.get("Email"))

        norm_count += _set_if_changed(
            df, idx, "Company", normalize_company(row.get("Company")), row.get("Company")
        )
        norm_count += _set_if_changed(
            df, idx, "Title", normalize_title(row.get("Title")), row.get("Title")
        )
        norm_count += _set_if_changed(
            df, idx, "Phone", normalize_phone(row.get("Phone")), row.get("Phone")
        )
        if "MobilePhone" in df.columns:
            norm_count += _set_if_changed(
                df, idx, "MobilePhone", normalize_phone(row.get("MobilePhone")), row.get("MobilePhone")
            )
        norm_count += _set_if_changed(
            df, idx, "Website", normalize_website(row.get("Website")), row.get("Website")
        )
        norm_count += _set_if_changed(
            df, idx, "Industry", _apply_map("industry", row.get("Industry"), True), row.get("Industry")
        )
        norm_count += _set_if_changed(
            df, idx, "Country", _apply_map("country", row.get("Country"), True), row.get("Country")
        )
        norm_count += _set_if_changed(
            df, idx, "State", _apply_map("state", row.get("State"), True), row.get("State")
        )
        norm_count += _set_if_changed(
            df, idx, "LeadSource", _apply_map("lead source", row.get("LeadSource")), row.get("LeadSource")
        )
        norm_count += _set_if_changed(
            df, idx, "Status", _apply_map("status", row.get("Status")), row.get("Status")
        )
        norm_count += _set_if_changed(
            df, idx, "Street", normalize_street(row.get("Street")), row.get("Street")
        )
        norm_count += _set_if_changed(
            df, idx, "City", _cell(row.get("City")).title(), row.get("City")
        )
        norm_count += _set_if_changed(
            df, idx, "AnnualRevenue", normalize_revenue(row.get("AnnualRevenue")), row.get("AnnualRevenue")
        )
        norm_count += _set_if_changed(
            df, idx, "NumberOfEmployees", normalize_employees(row.get("NumberOfEmployees")), row.get("NumberOfEmployees")
        )
        consent = _apply_map("consent", row.get("HasOptedOutOfEmail"))
        if consent.lower() == "null":
            consent = ""
        norm_count += _set_if_changed(
            df, idx, "HasOptedOutOfEmail", consent, row.get("HasOptedOutOfEmail")
        )

    return df, norm_count
