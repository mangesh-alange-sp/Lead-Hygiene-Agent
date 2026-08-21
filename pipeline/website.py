"""
website.py
Website derivation in isolation.

  normalize_website(raw)                     -> str | None   (clean an existing value)
  derive_website(company, email)             -> str | None   (derive when absent)
  resolve_website(existing, company, email)  -> str | None   (full hierarchy)

Guarantee (see tests/test_website_rules.py): a free/personal email domain never
becomes a Website unless the Company name independently resolves to a real domain.
"""

from .domains import (
    DOMAIN_TOKEN_RE,
    domain_matches_company,
    host_of,
    is_personal_domain,
    is_placeholder_domain,
    is_plausible_domain,
    is_social_host,
)
from .textnorm import cell, email_domain


def normalize_website(raw_website):
    """Reduce a URL to https://host, dropping paths, params, and junk hosts."""
    text = cell(raw_website)
    if not text:
        return None
    if "@" in text and "://" not in text:
        return None
    host = host_of(text)
    if not host or is_social_host(host):
        return None
    if is_personal_domain(host) or is_placeholder_domain(host):
        return None
    if not is_plausible_domain(host):
        return None
    return f"https://{host}"


def website_from_company(company):
    """Use a domain-like token already present in the name ('Amazon.com Inc.'); never invent one."""
    text = cell(company)
    if not text:
        return None
    for token in text.split():
        bare = token.strip(".,;:()[]\"'")
        if DOMAIN_TOKEN_RE.match(bare) and is_plausible_domain(bare):
            return normalize_website(bare)
    return None


def website_from_email(company, email):
    """
    Email host is a fallback only when it is a real corporate domain AND either
    the Company is unknown or the host demonstrably belongs to that Company.
    """
    host = email_domain(email)
    if not host or not is_plausible_domain(host):
        return None
    company_text = cell(company)
    if not company_text:
        return normalize_website(host)
    if domain_matches_company(host, company_text):
        return normalize_website(host)
    return None


def derive_website(company="", email=""):
    """Derive a website from Company first, then Email. Returns None rather than guessing."""
    return website_from_company(company) or website_from_email(company, email)


def resolve_website(existing="", company="", email=""):
    """Keep a trustworthy existing value, otherwise derive one."""
    current = normalize_website(existing)
    if current:
        host = host_of(current)
        if not cell(company) or domain_matches_company(host, company):
            return current
    return derive_website(company, email)


def is_free_provider_website(value) -> bool:
    """Invariant helper: does this Website point at a personal/free mail provider?"""
    host = host_of(value)
    return bool(host) and is_personal_domain(host)
