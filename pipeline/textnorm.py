"""
textnorm.py
Shared string-cleaning utilities. Every function here is pure and has its own
tests (tests/test_textnorm.py) because the transformation modules depend on it.
"""

import math
import re
import unicodedata

BLANK_TOKENS = frozenset({"", "n/a", "unknown", "nan", "none", "null", "nat", "-", "--"})

CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
EXCEL_LEAD_CHARS = frozenset({"'", "\t", "=", "@", "\u200b", "\u00a0"})


def cell(value) -> str:
    """Coerce any CSV/pandas cell to a trimmed string, mapping blank tokens to ''."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = CONTROL_RE.sub("", str(value)).strip()
    return "" if text.lower() in BLANK_TOKENS else text


def strip_excel_artifacts(value) -> str:
    """
    Remove leading text-marker and formula characters an Excel round-trip adds
    ('+1-..., =1-..., tab-prefixed). The marker is display metadata, never data.
    Intended for numeric-ish fields; do not run it on Email.
    """
    text = cell(value)
    while text and text[0] in EXCEL_LEAD_CHARS:
        text = text[1:].strip()
    return text


def collapse_whitespace(value) -> str:
    return re.sub(r"\s+", " ", cell(value)).strip()


def digits_only(value) -> str:
    return re.sub(r"\D", "", cell(value))


def alias_key(value) -> str:
    """Stable lookup key: 'Procter & Gamble' and 'PROCTER AND GAMBLE' share one key."""
    text = ("" if value is None else str(value)).strip().lower()
    text = re.sub(r"\s*&\s*", " and ", text)
    text = re.sub(r"[,.]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def fold_text(value) -> str:
    """ASCII fold for matching: Büchert -> buchert, O'Neil -> oneil, A & B -> a b."""
    text = cell(value).lower().replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("'", "").replace("`", "").replace("\u2019", "")
    text = text.replace(" & ", " ").replace(" and ", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def email_domain(email) -> str:
    text = cell(email).lower()
    if "@" not in text:
        return ""
    return text.rsplit("@", 1)[-1].strip().strip(".")


def email_local(email) -> str:
    text = cell(email).lower()
    if "@" not in text:
        return text
    return text.rsplit("@", 1)[0].strip()


def email_local_fold(email) -> str:
    return fold_text(email_local(email))
