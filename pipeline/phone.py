"""
phone.py
Phone normalization in isolation: normalize_phone(raw_phone, email, company, country) -> str | None.

Rules this module guarantees (see tests/test_phone_rules.py):
  * A number that starts with a known calling code keeps that (+CC) prefix.
  * A NANP result never has an area code or exchange starting with 0 or 1.
  * The result never starts with +, =, -, @, an apostrophe, or a tab (Excel formula/text markers).
  * With no resolvable country signal the cleaned original is returned, not a guess.
"""

import re

import phonenumbers

from .config import (
    CALLING_CODES,
    CALLING_TO_REGION,
    CC_NATIONAL_LEN,
    COUNTRY_NAME_TO_REGION,
    DUMMY_PHONE_VALUES,
    EMAIL_COMPOUND_TLD_TO_REGION,
    EMAIL_TLD_TO_REGION,
    MAX_PHONE_DIGITS,
    MIN_PHONE_DIGITS,
    REGION_CALLING,
)
from .textnorm import cell, collapse_whitespace, digits_only, email_domain, strip_excel_artifacts

DEFAULT_NATIONAL_LEN = (7, 13)
# Below this length a leading calling code is indistinguishable from a national
# number, so digit-prefix inference stays off.
MIN_DIGITS_FOR_CC_PREFIX = 10


def region_from_email(email) -> str:
    """Map an email host TLD (.de, .com.tw, .co.uk) to a region code."""
    host = email_domain(email)
    labels = [part for part in host.split(".") if part]
    if len(labels) >= 2:
        compound = EMAIL_COMPOUND_TLD_TO_REGION.get(f"{labels[-2]}.{labels[-1]}")
        if compound:
            return compound
    if not labels:
        return ""
    return EMAIL_TLD_TO_REGION.get(labels[-1], "")


def region_from_country(country) -> str:
    text = cell(country).lower().strip()
    if not text:
        return ""
    if text.upper() in REGION_CALLING:
        return text.upper()
    key = re.sub(r"[.,]", "", text).strip()
    return COUNTRY_NAME_TO_REGION.get(text) or COUNTRY_NAME_TO_REGION.get(key, "")


def region_from_digits(digits: str) -> str:
    """
    Detect a region from a leading calling code.

    Exactly ten digits that form a valid NANP number are ambiguous (a US number
    and, say, a Singapore number can share that shape), so NANP wins and no
    country is inferred from the prefix.
    """
    if not digits or len(digits) < MIN_DIGITS_FOR_CC_PREFIX:
        return ""
    if len(digits) == 10 and is_valid_nanp(digits):
        return ""
    for cc in CALLING_CODES:
        if not digits.startswith(cc):
            continue
        national = digits[len(cc):]
        low, high = CC_NATIONAL_LEN.get(cc, DEFAULT_NATIONAL_LEN)
        if not low <= len(national) <= high:
            continue
        if cc == "1" and not is_valid_nanp(national):
            continue
        return CALLING_TO_REGION.get(cc, "")
    return ""


def infer_region(email="", country="", phone="") -> str:
    """Email TLD, then Country, then a leading calling code. Never defaults to US."""
    return (
        region_from_email(email)
        or region_from_country(country)
        or region_from_digits(digits_only(_unwrap_excel_safe(strip_excel_artifacts(phone))))
    )


def is_valid_nanp(national: str) -> bool:
    """NANP: 10 digits, area code and exchange both starting 2-9."""
    return (
        len(national) == 10
        and national[0] not in "01"
        and national[3] not in "01"
    )


def is_junk_phone(raw_phone) -> bool:
    raw = strip_excel_artifacts(raw_phone)
    digits = digits_only(raw)
    if not digits:
        return True
    if len(digits) < MIN_PHONE_DIGITS or len(digits) > MAX_PHONE_DIGITS:
        return True
    if len(set(digits)) == 1:
        return True
    if digits in DUMMY_PHONE_VALUES:
        return True
    if digits in {"1234567890", "0123456789", "9876543210"}:
        return True
    # NANP 555 is reserved for fiction; treat it as missing data, not a real line.
    national = digits[1:] if len(digits) == 11 and digits.startswith("1") else digits
    if len(national) == 10 and national[:3] == "555":
        return True
    return False


def format_cc_national(cc: str, national: str) -> str:
    """Format as '(+CC) …'. Parentheses stop Excel treating '+' as a formula."""
    national = digits_only(national)
    if not national:
        return ""
    if cc == "1" and not is_valid_nanp(national):
        return ""
    try:
        parsed = phonenumbers.parse("+" + cc + national, None)
        if phonenumbers.is_possible_number(parsed):
            intl = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
            dashed = _dashes_from_international(intl)
            safe = _excel_safe(dashed)
            if safe.startswith(f"(+{cc})"):
                return safe
    except phonenumbers.NumberParseException:
        pass
    if len(national) >= 7:
        groups = [national[:-7], national[-7:-4], national[-4:]]
        return _excel_safe(f"+{cc}-" + "-".join(part for part in groups if part))
    return _excel_safe(f"+{cc}-{national}")


def _dashes_from_international(intl: str) -> str:
    """'+33 1 67 45 82 14' / '+1 206-555-0100' -> '+33-1-67-45-82-14' / '+1-206-555-0100'."""
    parts = [part for part in re.split(r"[\s.-]+", (intl or "").strip()) if part]
    if parts and len(digits_only(parts[-1])) >= 7 and digits_only(parts[-1]) == parts[-1]:
        last = parts[-1]
        parts = parts[:-1] + [last[:-4], last[-4:]]
    return "-".join(parts)


def _excel_safe(plus_dashed: str) -> str:
    """'+1-206-555-0100' -> '(+1) 206-555-0100' so spreadsheet apps keep it as text."""
    match = re.match(r"^\+(\d{1,3})-(.*)$", plus_dashed or "")
    if match:
        return f"(+{match.group(1)}) {match.group(2)}"
    return plus_dashed


def is_international_format(value) -> bool:
    """True when the value is already in the Excel-safe '(+CC) …' shape."""
    return bool(re.match(r"^\(\+\d{1,3}\) ", cell(value)))


def _unwrap_excel_safe(raw: str) -> str:
    """'(+1) 206-555-0100' -> '+1 206-555-0100' so parsing still sees the calling code."""
    return re.sub(r"^\(\+(\d{1,3})\)\s*", r"+\1 ", raw or "").strip()


def _length_ok(cc: str, national: str) -> bool:
    low, high = CC_NATIONAL_LEN.get(cc, DEFAULT_NATIONAL_LEN)
    return low <= len(national) <= high


def _possible(cc: str, national: str) -> bool:
    """Cross-check against libphonenumber, but never let it veto a length-valid number."""
    try:
        parsed = phonenumbers.parse("+" + cc + national, None)
    except phonenumbers.NumberParseException:
        return False
    return phonenumbers.is_possible_number(parsed)


def _national_for_cc(digits: str, cc: str) -> str:
    """Pick the national part for a known calling code, tolerating trunk prefixes."""
    candidates = []
    if digits.startswith(cc):
        candidates.append(digits[len(cc):])
    if digits.startswith("0"):
        candidates.append(digits.lstrip("0"))
    candidates.append(digits)
    for national in candidates:
        if not national:
            continue
        if cc == "1" and not is_valid_nanp(national):
            continue
        if _length_ok(cc, national) and _possible(cc, national):
            return national
    for national in candidates:
        if national and _length_ok(cc, national) and not (cc == "1" and not is_valid_nanp(national)):
            return national
    return ""


def format_for_region(raw_phone, region: str) -> str:
    """Format digits against an explicit region. Returns '' when it does not fit."""
    digits = digits_only(strip_excel_artifacts(raw_phone))
    cc = REGION_CALLING.get((region or "").upper(), "")
    if not digits or not cc:
        return ""
    national = _national_for_cc(digits, cc)
    if not national:
        return ""
    return format_cc_national(cc, national)


def _from_explicit_international(raw: str, digits: str) -> str:
    """Handle values the source already marked international ('+…' or '00…')."""
    if raw.startswith("+"):
        payload = digits
    elif raw.startswith("00") and len(digits) > 4:
        payload = digits[2:]
    else:
        return ""
    for cc in CALLING_CODES:
        if not payload.startswith(cc):
            continue
        national = payload[len(cc):]
        if cc == "1" and not is_valid_nanp(national):
            continue
        if _length_ok(cc, national):
            return format_cc_national(cc, national)
    return ""


def _demote_bad_international(raw: str, digits: str) -> tuple:
    """
    The source claimed an international form the digits cannot support, e.g. '+1'
    in front of a number with a 0 area code. Drop the impossible country code
    instead of emitting a fake +1 number.
    """
    body = raw[2:] if raw.startswith("00") else raw[1:]
    payload = digits[2:] if raw.startswith("00") else digits
    if payload.startswith("1") and not is_valid_nanp(payload[1:]):
        stripped = payload[1:]
        if len(stripped) >= MIN_PHONE_DIGITS:
            return re.sub(r"^\s*1\s*[-.\s]?\s*", "", body).strip(" -."), stripped
    return body.strip(" -."), payload


def normalize_phone(raw_phone, email="", company="", country=""):
    """
    Normalize one phone value to '(+CC) …' using only country signals that
    are actually present. Returns None for junk/empty input.

    The parentheses are required: a leading '+' makes Excel/Sheets evaluate the
    value as arithmetic (so '+1-206-555-0100' becomes a negative number).

    company is accepted so callers can pass the full record shape; it is only
    consulted for a country hint and never used to invent a calling code.
    """
    raw = collapse_whitespace(strip_excel_artifacts(raw_phone))
    raw = _unwrap_excel_safe(raw)
    if not raw:
        return None
    if is_junk_phone(raw):
        return None
    digits = digits_only(raw)

    explicit = _from_explicit_international(raw, digits)
    if explicit:
        return explicit
    if raw.startswith("+") or raw.startswith("00"):
        raw, digits = _demote_bad_international(raw, digits)

    region = infer_region(email=email, country=country, phone=digits)
    if not region:
        region = region_from_country(company)
    if region:
        formatted = format_for_region(digits, region)
        if formatted:
            return formatted

    # Structural NANP shape is itself a signal; a 10-digit number with a valid
    # area code and exchange is treated as +1.
    if is_valid_nanp(digits):
        return format_cc_national("1", digits)
    if len(digits) == 11 and digits.startswith("1") and is_valid_nanp(digits[1:]):
        return format_cc_national("1", digits[1:])

    # No resolvable signal: hand back the cleaned original rather than guessing.
    # If the leftover still starts with '+CC-…', wrap it so Excel will not
    # evaluate the cell as arithmetic.
    if raw.startswith("+"):
        wrapped = _excel_safe(raw)
        if wrapped and wrapped[0] not in "+=-@":
            return wrapped
    return raw
