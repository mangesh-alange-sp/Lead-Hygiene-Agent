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
    # host_of already dropped www. and the path/trailing slash.
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
    """Corporate email host only. Personal/placeholder hosts never become a Website."""
    host = email_domain(email)
    if not host or not is_plausible_domain(host):
        return None
    if is_personal_domain(host) or is_placeholder_domain(host):
        return None
    return normalize_website(host)


def derive_website(company="", email=""):
    """Email domain first, then a domain token already in the company name."""
    return website_from_email(company, email) or website_from_company(company)


def resolve_website(existing="", company="", email=""):
    """
    Precedence: keep a valid existing website, else email domain, else company
    token. A lookup miss must never delete a value that step 1 or 2 supplied.
    """
    return resolve_website_with_reason(existing, company, email)[0]


def resolve_website_with_reason(existing="", company="", email=""):
    """Return (website_or_None, change_reason)."""
    current = normalize_website(existing)
    if current:
        if current != cell(existing):
            return current, "website: kept existing value and standardized"
        return current, "website: kept existing value"
    from_email = website_from_email(company, email)
    if from_email:
        return from_email, "website: derived from email domain (lookup table had no entry)"
    from_company = website_from_company(company)
    if from_company:
        return from_company, "website: derived from company-name token"
    if cell(existing):
        return None, "website: existing value was invalid and was cleared"
    return None, ""


def is_free_provider_website(value) -> bool:
    """Invariant helper: does this Website point at a personal/free mail provider?"""
    host = host_of(value)
    return bool(host) and is_personal_domain(host)
