"""
phone.py
Phone validation/normalization in isolation, backed entirely by
`phonenumbers` (the Python port of Google's libphonenumber).

There is no regex reshaping here. A number is only rewritten when
libphonenumber says `is_valid_number()`; otherwise the submitted value is
preserved untouched and flagged for review. That ordering is what stops a
country code from being silently dropped.

Every record lands in exactly one of three states (see PHONE_STATUSES):

  valid         -> E.164, e.g. +447771695127
  needs_review  -> the cleaned as-submitted value, unchanged
  unparseable   -> no usable digits at all (dummy/garbage), value cleared
  "" (no state) -> the field was blank on arrival; there is nothing to review

Resolution order, each step gated by `is_valid_number()`:

  1. parse(raw, None)                  - raw already carries a country code
  2. parse("+" + digits, None)         - raw was marked international (+ / 00)
  3. parse("+" + digits, None)         - digits lead with a known calling code
  4. parse(raw, <email TLD region>)    - region hint from the lead's own email
  5. parse(raw, <country field region>)- region hint from the lead's own Country
  6. parse(raw, NANP_FALLBACK_REGION)  - last resort for a value with no other
                                         country evidence at all

Step 6 is a validation attempt, not an assumption: a 10-digit NANP-shaped
number in a US-heavy lead list is worth trying, but the result is only accepted
when libphonenumber confirms it. It never runs on a value that already carries
its own country code, so it cannot override real international data. Anything
that still fails is left exactly as submitted and flagged needs_review.

Caveat worth knowing: a foreign number that happens to be NANP-shaped (10
digits, no leading zero) will be claimed by step 6. Populating the record's
Country field is what prevents that, since step 5 wins first.
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

STATUS_VALID = "valid"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_UNPARSEABLE = "unparseable"
# A field that was blank on arrival. Not a state a phone can be "in", so it is
# excluded from the review counts rather than reported as a problem.
STATUS_NONE = ""
PHONE_STATUSES = (STATUS_VALID, STATUS_NEEDS_REVIEW, STATUS_UNPARSEABLE)

# Tried last, and only when the record offers no country evidence of its own.
NANP_FALLBACK_REGION = "US"
NO_EVIDENCE_REASON = (
    "phone: no country-code evidence and not a valid NANP number; "
    "original kept unchanged for review"
)

E164_RE = re.compile(r"^\+\d{6,15}$")


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
    Detect a region from a leading calling code already present in the digits.

    Exactly ten digits that form a valid NANP number are ambiguous (2065550100
    also parses as +20 65550100), so the NANP shape wins and no country is
    inferred from the prefix.
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
    return infer_region_with_source(email=email, country=country, phone=phone)[0]


def infer_region_with_source(email="", country="", phone="", company=""):
    """Same as infer_region, plus a human-readable evidence label."""
    from_email = region_from_email(email)
    if from_email:
        return from_email, "email TLD"
    from_country = region_from_country(country)
    if from_country:
        return from_country, "country field"
    from_digits = region_from_digits(digits_only(unwrap_bracketed_cc(strip_excel_artifacts(phone))))
    if from_digits:
        return from_digits, "leading calling-code digits"
    from_company = region_from_country(company) if company else ""
    if from_company:
        return from_company, "company field"
    return "", ""


def is_valid_nanp(national: str) -> bool:
    """NANP: 10 digits, area code and exchange both starting 2-9."""
    return (
        len(national) == 10
        and national[0] not in "01"
        and national[3] not in "01"
    )


def is_junk_phone(raw_phone) -> bool:
    """Dummy/garbage values that carry no recoverable number at all."""
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
    # NANP 555 area code is reserved for fiction; treat it as missing data.
    national = digits[1:] if len(digits) == 11 and digits.startswith("1") else digits
    if len(national) == 10 and national[:3] == "555":
        return True
    return False


def unwrap_bracketed_cc(raw: str) -> str:
    """Accept legacy '(+CC) …' values and turn them back into '+CC …'."""
    return re.sub(r"^\(\+(\d{1,3})\)\s*", r"+\1 ", raw or "").strip()


def is_international_format(value) -> bool:
    """True when the value carries a leading country code."""
    return bool(re.match(r"^\+\d", cell(value)))


def input_has_calling_code(raw_phone) -> bool:
    """True for explicit +CC / 00CC. Dummy zeros like 000-000-000 are not a CC."""
    text = unwrap_bracketed_cc(strip_excel_artifacts(raw_phone))
    if text.startswith("+"):
        return True
    return text.startswith("00") and not text.startswith("000")


def format_e164(parsed) -> str:
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def valid_or_none(raw: str, region=None):
    """Parse and accept only when libphonenumber says the number is valid."""
    try:
        parsed = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return None
    return parsed if phonenumbers.is_valid_number(parsed) else None


def format_cc_national(cc: str, national: str) -> str:
    """E.164 for a known calling code + national number, or '' when invalid."""
    national = digits_only(national)
    if not cc or not national:
        return ""
    parsed = valid_or_none("+" + cc + national)
    return format_e164(parsed) if parsed else ""


def format_for_region(raw_phone, region: str) -> str:
    """Format against an explicit region hint. '' when it does not validate."""
    cleaned = collapse_whitespace(strip_excel_artifacts(raw_phone))
    if not cleaned or not region:
        return ""
    parsed = valid_or_none(cleaned, (region or "").upper())
    return format_e164(parsed) if parsed else ""


def classify_phone(raw_phone) -> str:
    """Classify the input before touching it: empty, garbage, noise, has_cc, missing_cc."""
    raw = unwrap_bracketed_cc(collapse_whitespace(strip_excel_artifacts(raw_phone)))
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
    Resolve one phone value.

    Returns {value, status, reason, raw, class_} where status is one of
    PHONE_STATUSES and `raw` is always the as-submitted value.
    """
    submitted = cell(raw_phone)
    cleaned = unwrap_bracketed_cc(collapse_whitespace(strip_excel_artifacts(raw_phone)))
    kind = classify_phone(raw_phone)
    base = {"raw": submitted, "class_": kind}

    if kind == "empty":
        # No phone was submitted, so there is no phone to have a state. Calling
        # this "unparseable" would inflate the review count with blank fields.
        return {**base, "value": "", "status": STATUS_NONE, "reason": ""}
    if kind in {"garbage", "noise"}:
        return {
            **base, "value": "", "status": STATUS_UNPARSEABLE,
            "reason": f"phone: {kind}, no recoverable number (left unparseable)",
        }

    digits = digits_only(cleaned)

    # 1. The value already carries its own country code.
    parsed = valid_or_none(cleaned, None)
    if parsed:
        return {
            **base, "value": format_e164(parsed), "status": STATUS_VALID,
            "reason": "phone: validated as submitted, country code preserved",
        }

    # 2/3. Digits that already contain a calling code, either flagged
    # international (+ / 00) or leading with a known code. Nothing is invented.
    if kind == "has_cc" or region_from_digits(digits):
        parsed = valid_or_none("+" + digits, None)
        if parsed:
            return {
                **base, "value": format_e164(parsed), "status": STATUS_VALID,
                "reason": "phone: validated from the country code already in the value",
            }

    # 4/5. Region hint from the same record only: email TLD, then Country.
    had_evidence = False
    for region, evidence in (
        (region_from_email(email), "email TLD"),
        (region_from_country(country), "country field"),
        (region_from_country(company) if company else "", "company field"),
    ):
        if not region:
            continue
        had_evidence = True
        parsed = valid_or_none(cleaned, region.upper())
        if parsed:
            return {
                **base, "value": format_e164(parsed), "status": STATUS_VALID,
                "reason": f"phone: validated with region hint {region.upper()} (from {evidence})",
            }

    # 6. No country evidence anywhere in the record: try NANP and keep the
    # result only if libphonenumber validates it.
    if kind == "missing_cc" and not had_evidence:
        parsed = valid_or_none(cleaned, NANP_FALLBACK_REGION)
        if parsed:
            return {
                **base, "value": format_e164(parsed), "status": STATUS_VALID,
                "reason": (
                    f"phone: validated as a NANP number using the default "
                    f"{NANP_FALLBACK_REGION} region (no other country evidence)"
                ),
            }

    # Nothing validated. Keep the submitted value exactly as it came in.
    return {
        **base, "value": cleaned, "status": STATUS_NEEDS_REVIEW,
        "reason": NO_EVIDENCE_REASON if not had_evidence else (
            "phone: region evidence found but libphonenumber rejected the "
            "number; original kept unchanged for review"
        ),
    }


def normalize_phone(raw_phone, email="", company="", country=""):
    """E.164 when valid, the original value when unresolved, None for junk."""
    result = parse_phone(raw_phone, email=email, company=company, country=country)
    if result["status"] == STATUS_UNPARSEABLE:
        return None
    return result["value"] or None
