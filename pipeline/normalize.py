"""
normalize.py
Taxonomy-driven field normalization plus the DataFrame walker.

Phone, company casing, and website derivation live in their own modules
(phone.py, casing.py, website.py); this module only orchestrates them so a fix
to one transformation cannot reach into another.
"""

import re

import pandas as pd

from .casing import normalize_company_casing
from .config import REGION_TO_COUNTRY, TITLE_ACRONYMS
from .domains import company_match_key
from .phone import infer_region, is_international_format, normalize_phone
from .taxonomy import TAXONOMY
from .textnorm import alias_key, cell as _cell
from .website import resolve_website


def _tax_key(value: str) -> str:
    return alias_key(value)


def _lookup(section: str, value: str):
    return TAXONOMY.get(section, {}).get(_tax_key(value))


def infer_phone_region(email: str = "", country: str = "", phone: str = "") -> str:
    """Kept for callers that only need the region signal."""
    mapped = _lookup("country", country) or _cell(country)
    return infer_region(email=email, country=mapped, phone=phone)


def _format_initial(value: str, *, allow_digraph: bool = False) -> str:
    """Single letter or dotted initial -> 'J.'; optional 'Jk' (no vowel) -> 'J.K.'."""
    text = value.strip()
    if re.fullmatch(r"[A-Za-z]\.*", text):
        return f"{text[0].upper()}."
    if (
        allow_digraph
        and re.fullmatch(r"[A-Za-z][a-z]", text)
        and not re.search(r"[aeiou]", text, re.I)
    ):
        return f"{text[0].upper()}.{text[1].upper()}."
    return ""


def _case_name_token(token: str, *, allow_digraph: bool = False) -> str:
    if not token:
        return token
    initial = _format_initial(token, allow_digraph=allow_digraph)
    if initial:
        return initial
    leading = re.match(r"^[^A-Za-z]*", token).group(0)
    trailing = re.search(r"[^A-Za-z]*$", token).group(0)
    bare = token[len(leading):len(token) - len(trailing)] if trailing else token[len(leading):]
    if not bare:
        return token
    if "-" in bare:
        return leading + "-".join(
            _case_name_token(part, allow_digraph=allow_digraph) for part in bare.split("-")
        ) + trailing
    if "." in bare:
        labels = [(part[:1].upper() + part[1:].lower()) if part else "" for part in bare.split(".")]
        rendered = ".".join(labels)
        if trailing.startswith(".") and rendered.endswith("."):
            trailing = trailing[1:]
        return leading + rendered + trailing
    if "'" in bare or "’" in bare:
        sep = "'" if "'" in bare else "’"
        head, tail = re.split(r"['’]", bare, maxsplit=1)
        return leading + head[:1].upper() + head[1:].lower() + sep + (tail[:1].upper() + tail[1:].lower() if tail else "") + trailing
    titled = bare[:1].upper() + bare[1:].lower()
    titled = re.sub(r"^Mc([a-z])", lambda m: "Mc" + m.group(1).upper(), titled)
    return leading + titled + trailing


def normalize_name(first_name: str, last_name: str) -> tuple:
    """Proper cases names, cleans honorifics, handles initials like 'Jk' -> 'J.K.'."""
    fn = _cell(first_name)
    ln = _cell(last_name)

    fn = re.sub(
        r"^(Mr|Ms|Mrs|Miss|Dr|Prof|Sir|Jr|Sr)\.?\s+",
        "",
        fn,
        flags=re.IGNORECASE,
    )

    fn_parts = [_case_name_token(part, allow_digraph=True) for part in fn.split() if part]
    ln_parts = [_case_name_token(part, allow_digraph=False) for part in ln.split() if part]
    return " ".join(fn_parts), " ".join(ln_parts)


def normalize_company(company_raw: str) -> str:
    """Canonical taxonomy value when known, otherwise allowlist-driven casing."""
    raw_clean = _cell(company_raw)
    if not raw_clean:
        return ""
    if _lookup("invalid", raw_clean) == "INVALID":
        return ""
    mapped = _lookup("company", raw_clean)
    if mapped:
        return mapped
    # "McDonald's GmbH" / "bcbs association" should still hit the brand alias.
    brand_key = company_match_key(raw_clean)
    company_tax = TAXONOMY.get("company", {})
    mapped = company_tax.get(brand_key) or company_tax.get(brand_key.replace(" ", ""))
    if mapped:
        return mapped
    return normalize_company_casing(raw_clean)


def normalize_title(title_raw: str) -> str:
    """Applies title taxonomy or title cases job titles, keeping configured acronyms."""
    raw_clean = _cell(title_raw)
    if not raw_clean:
        return ""
    raw_clean = _primary_title(raw_clean)

    mapped = _lookup("title", raw_clean)
    if mapped:
        return mapped

    title_tax = TAXONOMY.get("title", {})
    words = []
    for word in raw_clean.split():
        key = _tax_key(word)
        if key in title_tax:
            words.append(title_tax[key])
            continue
        match = re.match(r"^([.,]*)([^.,]+)([.,]*)$", word)
        if match:
            core = match.group(2)
            if core.upper() in TITLE_ACRONYMS:
                words.append(f"{match.group(1)}{core.upper()}{match.group(3)}")
                continue
        words.append(word.capitalize())
    rendered = " ".join(words)
    if is_low_quality_title(rendered):
        compact = re.sub(r"[.]", "", rendered)
        return compact.upper()
    return rendered


def _primary_title(value: str) -> str:
    """Keep the job title before LinkedIn-style '| role | scope' stuffing."""
    if "|" not in value:
        return value
    parts = [part.strip() for part in value.split("|") if part.strip()]
    return parts[0] if parts else value


def is_low_quality_title(title) -> bool:
    """True for unexplained 2-3 letter titles such as 'Ats' that are not known acronyms."""
    text = _cell(title)
    if not text:
        return False
    compact = re.sub(r"[.]", "", text)
    if not re.fullmatch(r"[A-Za-z]{2,3}", compact):
        return False
    return compact.upper() not in TITLE_ACRONYMS


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


def _add_flag(current: str, flag: str) -> str:
    flags = [part for part in (current or "").split("|") if part]
    if flag not in flags:
        flags.append(flag)
    return "|".join(flags)


def _flag_row(df, idx, flag: str, *, hitl: bool = False):
    if "data_quality_flags" in df.columns:
        df.at[idx, "data_quality_flags"] = _add_flag(df.at[idx, "data_quality_flags"], flag)
    if hitl and "hitl_review" in df.columns:
        df.at[idx, "hitl_review"] = "Yes"


def normalize_dataframe(df: pd.DataFrame) -> tuple:
    """Normalizes all fields in place and returns (df, normalized_values_count)."""
    df = df.copy()
    for col in ("data_quality_flags", "hitl_review"):
        if col not in df.columns:
            df[col] = ""
    norm_count = 0

    for idx, row in df.iterrows():
        fn, ln = normalize_name(row.get("FirstName"), row.get("LastName"))
        norm_count += _set_if_changed(df, idx, "FirstName", fn, row.get("FirstName"))
        norm_count += _set_if_changed(df, idx, "LastName", ln, row.get("LastName"))

        email = _cell(row.get("Email", "")).lower()
        norm_count += _set_if_changed(df, idx, "Email", email, row.get("Email"))
        country = _apply_map("country", row.get("Country"), True)
        company = normalize_company(row.get("Company"))

        phone = normalize_phone(row.get("Phone"), email=email, company=company, country=country) or ""
        if not country:
            region = infer_region(email=email, country="", phone=phone or row.get("Phone", ""))
            if region:
                country = REGION_TO_COUNTRY.get(region, "")
                phone = (
                    normalize_phone(row.get("Phone"), email=email, company=company, country=country)
                    or phone
                )

        norm_count += _set_if_changed(df, idx, "Company", company, row.get("Company"))
        title = normalize_title(row.get("Title"))
        norm_count += _set_if_changed(df, idx, "Title", title, row.get("Title"))
        if is_low_quality_title(title):
            _flag_row(df, idx, "low_quality_title", hitl=True)
        norm_count += _set_if_changed(df, idx, "Phone", phone, row.get("Phone"))
        if phone and not is_international_format(phone):
            _flag_row(df, idx, "unformatted_phone", hitl=True)
        if "MobilePhone" in df.columns:
            mobile = normalize_phone(row.get("MobilePhone"), email=email, company=company, country=country) or ""
            norm_count += _set_if_changed(df, idx, "MobilePhone", mobile, row.get("MobilePhone"))
            if mobile and not is_international_format(mobile):
                _flag_row(df, idx, "unformatted_phone", hitl=True)
        norm_count += _set_if_changed(
            df, idx, "Website",
            resolve_website(row.get("Website"), company, email) or "",
            row.get("Website"),
        )
        norm_count += _set_if_changed(
            df, idx, "Industry", _apply_map("industry", row.get("Industry"), True), row.get("Industry")
        )
        norm_count += _set_if_changed(df, idx, "Country", country, row.get("Country"))
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
