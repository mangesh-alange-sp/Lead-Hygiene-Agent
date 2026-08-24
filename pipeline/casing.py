"""
casing.py
Company-name casing in isolation: normalize_company_casing(raw_company) -> str.

Allowlists come from data/pipeline_config.json, so adding an acronym never
requires touching this module or the tests.
"""

import re

from .config import (
    COMPANY_ACRONYMS,
    COMPANY_MIXED_CASE,
    LEGAL_SUFFIX_DISPLAY,
    LEGAL_SUFFIX_MATCH_TOKENS,
)
from .textnorm import cell, collapse_whitespace

LEGAL_SUFFIX_RE = re.compile(r"\b(" + "|".join(LEGAL_SUFFIX_MATCH_TOKENS) + r")\.?\b", re.I)


def _key(token: str) -> str:
    return re.sub(r"\s+", " ", token.strip().lower())


def _build_display_map() -> dict:
    """Lookup keyed by lowercase form and by dot-stripped lowercase form."""
    display = {}
    for group in (COMPANY_ACRONYMS, COMPANY_MIXED_CASE):
        for form in group:
            key = _key(form)
            display.setdefault(key, form)
            display.setdefault(key.replace(".", ""), form)
            display.setdefault(key.replace("&", " and "), form)
    for key, form in LEGAL_SUFFIX_DISPLAY.items():
        display.setdefault(_key(key), form)
        display.setdefault(_key(key).replace(".", ""), form)
    return display


DISPLAY_MAP = _build_display_map()
SMALL_WORDS = frozenset({"of", "the", "and", "for", "to", "in", "at", "by", "de", "von", "van"})


def strip_legal_suffixes(folded: str) -> str:
    """Remove legal suffix tokens from an already-folded string."""
    return re.sub(r"\s+", " ", LEGAL_SUFFIX_RE.sub("", folded or "")).strip()


def is_domain_like(token: str) -> bool:
    """'Amazon.com' and 'siemens.de' are domain-like; 'R.O.C' and 'B.V.' are not."""
    from .domains import is_plausible_domain

    bare = token.strip(".,")
    return bool(re.match(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$", bare)) and is_plausible_domain(bare)


def case_domain_token(token: str) -> str:
    """Amazon.Com -> Amazon.com (first label capitalized, TLD labels lowercase)."""
    labels = token.split(".")
    head = labels[0][:1].upper() + labels[0][1:].lower() if labels[0] else ""
    return ".".join([head] + [part.lower() for part in labels[1:]])


def case_company_token(token: str) -> str:
    """Case a single company token, checking allowlists before generic title-casing."""
    leading = re.match(r"^[(\[\"']*", token).group(0)
    trailing = re.search(r"[)\]\"',.]*$", token).group(0)
    bare = token[len(leading):len(token) - len(trailing)] if trailing else token[len(leading):]
    if not bare:
        return token

    key = _key(bare)
    for candidate in (key, key.replace(".", ""), key.replace("&", " and ")):
        if candidate in DISPLAY_MAP:
            display = DISPLAY_MAP[candidate]
            # A display form that already ends in '.' absorbs a trailing period.
            tail = "" if display.endswith(".") and trailing.startswith(".") else trailing
            return leading + display + tail

    if "-" in bare and not is_domain_like(bare):
        bits = []
        for part in bare.split("-"):
            cased = case_company_token(part) if part else ""
            if part and not part.endswith(".") and cased.endswith("."):
                cased = cased[:-1]
            bits.append(cased)
        return leading + "-".join(bits) + trailing

    if is_domain_like(bare):
        return leading + case_domain_token(bare) + trailing

    if "&" in bare:
        parts = [part for part in bare.split("&") if part]
        if parts and all(part.isalpha() and len(part) <= 3 for part in parts):
            return leading + "&".join(part.upper() for part in parts) + trailing

    if "." in bare:
        # Dotted token that is not a domain (U.S., Acme.Corp): capitalize each label.
        labels = [part[:1].upper() + part[1:].lower() for part in bare.split(".")]
        return leading + ".".join(labels) + trailing

    # Short all-caps / vowelless tokens are acronyms (BFG, NCI), not "Bfg".
    if bare.isalpha() and 2 <= len(bare) <= 3 and bare.lower() not in SMALL_WORDS:
        if bare.isupper() or not re.search(r"[aeiou]", bare, re.I):
            return leading + bare.upper() + trailing

    titled = bare[:1].upper() + bare[1:].lower()
    titled = re.sub(r"\bMc([a-z])", lambda m: "Mc" + m.group(1).upper(), titled)
    titled = re.sub(r"\bO'([a-z])", lambda m: "O'" + m.group(1).upper(), titled)
    return leading + titled + trailing


def normalize_company_casing(raw_company) -> str:
    """
    Case a company name using the acronym/legal-suffix allowlists.
    Pure: no taxonomy lookups, no I/O, no shared mutable state.
    """
    text = collapse_whitespace(cell(raw_company))
    if not text:
        return ""
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r"\band\b", "&", text, flags=re.IGNORECASE)
    # Join short initialisms so 'L and T' / 'P & G' reach the allowlist as one token.
    text = re.sub(r"\b([A-Za-z]{1,3})\s*&\s*([A-Za-z]{1,3})\b", r"\1&\2", text)
    tokens = [case_company_token(token) for token in text.split()]
    tokens = [token for token in tokens if token]
    cased = []
    last_idx = len(tokens) - 1
    for idx, token in enumerate(tokens):
        bare = re.sub(r"^[(\[\"']+|[)\]\"',.]+$", "", token)
        if 0 < idx < last_idx and bare.lower() in SMALL_WORDS and bare.isalpha():
            cased.append(token.replace(bare, bare.lower(), 1))
        else:
            cased.append(token)
    return " ".join(cased)
