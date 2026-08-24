"""
phone.py
Phone normalization in isolation: normalize_phone(raw_phone, email, company, country) -> str | None.

Rules this module guarantees (see tests/test_phone_rules.py):
  * Output shape is +CC-xxx-xxx-xxxx when a country code is known.
  * A number that arrives with + / 00 / (+CC) never loses that country code.
  * A NANP result never has an area code or exchange starting with 0 or 1.
  * Country codes are inferred only from the same record (email TLD, country
    field, or an explicit prefix). +1 is never a default guess.
  * With no resolvable country signal the original value is kept and marked
    needs_review — it is never force-grouped into a fake-valid shape.
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
    region, _source = infer_region_with_source(email=email, country=country, phone=phone)
    return region


def infer_region_with_source(email="", country="", phone="", company=""):
    """Same as infer_region, plus a human-readable evidence label."""
    if region_from_email(email):
        return region_from_email(email), "email TLD"
    if region_from_country(country):
        return region_from_country(country), "country field"
    digits = digits_only(_unwrap_excel_safe(strip_excel_artifacts(phone)))
    if region_from_digits(digits):
        return region_from_digits(digits), "leading calling-code digits"
    if company and region_from_country(company):
        return region_from_country(company), "company field"
    return "", ""


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
    """Format as +CC-xxx-xxx-xxxx using libphonenumber when the number is possible."""
    national = digits_only(national)
    if not national:
        return ""
    if cc == "1" and not is_valid_nanp(national):
        return ""
    try:
        parsed = phonenumbers.parse("+" + cc + national, None)
        if phonenumbers.is_possible_number(parsed):
            dashed = _dashes_from_international(
                phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
            )
            if dashed.startswith(f"+{cc}-"):
                return dashed
    except phonenumbers.NumberParseException:
        pass
    # Do not force-fit an unvalidated number into a valid-looking shape.
    return ""


def _dashes_from_international(intl: str) -> str:
    """'+33 1 67 45 82 14' / '+1 206-555-0100' -> '+33-1-67-45-82-14' / '+1-206-555-0100'."""
    parts = [part for part in re.split(r"[\s.-]+", (intl or "").strip()) if part]
    if parts and len(digits_only(parts[-1])) >= 7 and digits_only(parts[-1]) == parts[-1]:
        last = parts[-1]
        parts = parts[:-1] + [last[:-4], last[-4:]]
    return "-".join(parts)


def is_international_format(value) -> bool:
    """True when the value already carries +CC-…"""
    return bool(re.match(r"^\+\d{1,3}-", cell(value)))


def input_has_calling_code(raw_phone) -> bool:
    """True for explicit +CC / 00CC. Dummy zeros like 000-000-000 are not a CC."""
    text = _unwrap_excel_safe(strip_excel_artifacts(raw_phone))
    if text.startswith("+"):
        return True
    return text.startswith("00") and not text.startswith("000")


def _unwrap_excel_safe(raw: str) -> str:
    """Accept leftover '(+CC) …' inputs and turn them back into '+CC …'."""
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
    try:
        parsed = phonenumbers.parse("+" + payload, None)
        if phonenumbers.is_possible_number(parsed):
            return _dashes_from_international(
                phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
            )
    except phonenumbers.NumberParseException:
        pass
    for cc in CALLING_CODES:
        if not payload.startswith(cc):
            continue
        national = payload[len(cc):]
        if cc == "1" and not is_valid_nanp(national):
            continue
        if _length_ok(cc, national):
            return format_cc_national(cc, national)
    return ""


def _lib_parse(raw: str, region: str = ""):
    try:
        parsed = phonenumbers.parse(raw, region or None)
    except phonenumbers.NumberParseException:
        return None
    if phonenumbers.is_possible_number(parsed):
        return parsed
    return None


def _formatted_ok(value: str) -> bool:
    """Reject a +1-… result whose NANP area/exchange starts with 0 or 1."""
    if not value:
        return False
    if value.startswith("+1-"):
        digits = digits_only(value)
        national = digits[1:] if digits.startswith("1") else digits
        if len(national) == 10 and not is_valid_nanp(national):
            return False
    return True


def classify_phone(raw_phone) -> str:
    """Classify before mutating: empty, garbage, noise, has_cc, missing_cc."""
    raw = collapse_whitespace(strip_excel_artifacts(raw_phone))
    raw = _unwrap_excel_safe(raw)
    if not raw:
        return "empty"
    digits = digits_only(raw)
    if re.search(r"\bext\.?\b|\bx\d+|/", raw, re.I) or raw.count("+") > 1:
        return "noise"
    if not digits or is_junk_phone(raw):
        return "garbage"
    if input_has_calling_code(raw):
        return "has_cc"
    return "missing_cc"


def parse_phone(raw_phone, email="", company="", country=""):
    """
    Return {value, status, reason, raw, class_}.
    status is valid | needs_review | unparseable.
    value is the write-back phone, or '' when unparseable junk.
    """
    submitted = cell(raw_phone)
    raw = collapse_whitespace(strip_excel_artifacts(raw_phone))
    raw = _unwrap_excel_safe(raw)
    kind = classify_phone(raw_phone)
    empty = {
        "value": "", "status": "unparseable", "reason": "", "raw": submitted, "class_": kind,
    }
    if kind == "empty":
        return empty
    if kind in {"garbage", "noise"}:
        return {**empty, "reason": f"phone: {kind}, left unparseable"}

    digits = digits_only(raw)
    region, evidence = infer_region_with_source(
        email=email, country=country, phone=raw, company=company,
    )

    if kind == "has_cc":
        explicit = _from_explicit_international(raw, digits)
        if _formatted_ok(explicit):
            return {
                "value": explicit, "status": "valid", "raw": submitted, "class_": kind,
                "reason": "phone: reformatted, country code preserved from input",
            }
        parsed = _lib_parse(raw if raw.startswith("+") else "+" + digits)
        if parsed:
            value = _dashes_from_international(
                phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
            )
            if _formatted_ok(value):
                return {
                    "value": value, "status": "valid", "raw": submitted, "class_": kind,
                    "reason": "phone: reformatted via libphonenumber",
                }
        # Keep the cleaned international form. Never strip the country code.
        return {
            "value": raw, "status": "needs_review", "raw": submitted, "class_": kind,
            "reason": "phone: country code present but number failed validation",
        }

    if region:
        formatted = format_for_region(digits, region)
        if _formatted_ok(formatted):
            return {
                "value": formatted, "status": "valid", "raw": submitted, "class_": kind,
                "reason": f"phone: reformatted, country code inferred from {evidence}",
            }
        parsed = _lib_parse(digits, region)
        if parsed:
            value = _dashes_from_international(
                phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
            )
            if _formatted_ok(value):
                return {
                    "value": value, "status": "valid", "raw": submitted, "class_": kind,
                    "reason": f"phone: reformatted, country code inferred from {evidence}",
                }

    if len(digits) == 11 and digits.startswith("1") and is_valid_nanp(digits[1:]):
        value = format_cc_national("1", digits[1:])
        if _formatted_ok(value):
            return {
                "value": value, "status": "valid",
                "raw": submitted, "class_": kind,
                "reason": "phone: reformatted, leading 1 is the NANP country code",
            }

    # Same-record evidence only. Do not assume +1 from a bare 10-digit NANP.
    return {
        "value": raw or submitted, "status": "needs_review", "raw": submitted,
        "class_": kind,
        "reason": "phone: no country code and no same-record evidence",
    }


def normalize_phone(raw_phone, email="", company="", country=""):
    """Normalized +CC-… value, original kept on review, or None for junk."""
    result = parse_phone(raw_phone, email=email, company=company, country=country)
    if result["status"] == "unparseable":
        return None
    return result["value"] or None
