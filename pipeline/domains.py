"""
domains.py
Pure domain/host predicates. Tested independently (tests/test_domains.py) because
website derivation, validation, and dedupe all depend on them.
"""

import re

from .config import (
    KNOWN_TLDS,
    PERSONAL_DOMAIN_PREFIXES,
    PERSONAL_EMAIL_DOMAINS,
    PLACEHOLDER_DOMAINS,
    SOCIAL_HOSTS,
)
from .textnorm import cell, fold_text

DOMAIN_TOKEN_RE = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")


def host_of(value) -> str:
    """Extract a bare lowercase host from a URL, host, or 'https://host/path'."""
    text = cell(value).lower()
    if not text:
        return ""
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = text.split("/")[0].split("?")[0].split("#")[0].split(":")[0].strip().strip(".")
    if text.startswith("www."):
        text = text[4:]
    return text


def is_personal_domain(host) -> bool:
    host = host_of(host)
    if not host:
        return False
    if host in PERSONAL_EMAIL_DOMAINS:
        return True
    if any(host.startswith(prefix) for prefix in PERSONAL_DOMAIN_PREFIXES):
        return True
    return any(host.endswith("." + domain) for domain in PERSONAL_EMAIL_DOMAINS)


def is_placeholder_domain(host) -> bool:
    """RFC 2606 reserved names plus the configured placeholder hosts."""
    host = host_of(host)
    if not host:
        return False
    if host in PLACEHOLDER_DOMAINS:
        return True
    labels = [part for part in host.split(".") if part]
    if not labels:
        return False
    if labels[-1] in {"example", "invalid", "localhost"}:
        return True
    return "example" in labels


def is_social_host(host) -> bool:
    return host_of(host) in SOCIAL_HOSTS


def is_plausible_domain(host) -> bool:
    """
    True only for a syntactically real, non-junk company domain.
    Rejects personal/placeholder/social hosts and initialisms like 'r.o.c'.
    """
    host = host_of(host)
    if not host:
        return False
    if is_personal_domain(host) or is_placeholder_domain(host) or is_social_host(host):
        return False
    if not DOMAIN_TOKEN_RE.match(host):
        return False
    labels = [part for part in host.split(".") if part]
    if len(labels) < 2:
        return False
    if labels[-1] not in KNOWN_TLDS:
        return False
    if any(len(label) < 2 for label in labels[:-1]):
        return False
    if sum(len(label) for label in labels[:-1]) < 3:
        return False
    if labels[0] in {"mail", "email", "smtp", "imap", "webmail", "mx", "pop"}:
        return False
    return True


def registrable_labels(host) -> list:
    """Host labels with generic TLD-ish parts removed: 'mail.acme.co.uk' -> ['mail','acme']."""
    generic = {"www", "com", "net", "org", "io", "co", "gov", "edu", "mil", "ac"}
    root = host_of(host)
    return [part for part in root.split(".") if part and part not in generic and part not in KNOWN_TLDS]


def company_match_key(company) -> str:
    """Folded company name with legal suffixes removed, for fuzzy comparison."""
    from .casing import strip_legal_suffixes

    return strip_legal_suffixes(fold_text(company))


def domain_matches_company(host, company) -> bool:
    """Does this host plausibly belong to this company name?"""
    from rapidfuzz import fuzz

    root = host_of(host)
    key = company_match_key(company)
    if not root or not key:
        return False
    compact = key.replace(" ", "")
    host_compact = root.replace(".", "")
    labels = registrable_labels(root)
    if compact and len(compact) >= 4 and compact in host_compact:
        return True
    if any(len(label) >= 3 and (label in compact or compact in label) for label in labels):
        return True
    if any(len(label) >= 3 and label in key for label in labels):
        return True
    # "NCI Information Systems" vs nciinc.com: the leading acronym is the brand.
    tokens = key.split()
    if tokens:
        head = tokens[0]
        if 3 <= len(head) <= 5 and head.isalpha():
            if root.startswith(head) or (labels and labels[0].startswith(head)):
                return True
    # "Société Générale" vs socgen.com: first three letters of each significant word.
    skip = {"of", "the", "and", "for", "to", "in", "at", "by", "de"}
    significant = [token for token in tokens if token not in skip]
    if len(significant) >= 2:
        stem = "".join(token[:3] for token in significant)
        if len(stem) >= 5 and any(label == stem or label.startswith(stem) for label in labels):
            return True
    # "L&T Construction" vs larsentoubro.com: short tokens expand in order inside the host.
    short = [token for token in significant if 1 <= len(token) <= 2]
    if 2 <= len(short) <= 4:
        acronym = "".join(short)
        if any(_acronym_expands_in_label(acronym, label) for label in labels):
            return True
    # mail.mil.tw / *.gov belongs with a military or government organization name.
    dotted = f".{root}."
    if ".mil." in dotted or ".gov." in dotted or root.endswith(".mil") or root.endswith(".gov"):
        if any(word in key for word in ("military", "government", "academy", "ministry", "department")):
            return True
    return fuzz.token_set_ratio(root.replace(".", " "), key) >= 70


def _acronym_expands_in_label(acronym: str, label: str) -> bool:
    """True when each acronym letter starts a 3+ character chunk, e.g. L+T in larsentoubro."""
    if not acronym or not label or not label.startswith(acronym[0]):
        return False
    cursor = 0
    last = len(acronym) - 1
    for idx, char in enumerate(acronym):
        found = label.find(char, cursor)
        if found < 0:
            return False
        cursor = found + (1 if idx == last else 3)
        if cursor > len(label) and idx < last:
            return False
    return True
