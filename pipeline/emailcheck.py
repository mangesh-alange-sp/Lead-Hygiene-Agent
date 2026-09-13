"""
emailcheck.py
Email validation in isolation, backed by `email-validator`.

Syntax comes from email_validator; deliverability comes from a real MX lookup
rather than a hand-maintained list of "fake" domains.

Two operational concerns are handled here so the pipeline stays deterministic:

  * Results are cached (data/mx_cache.json plus an in-process memo), so a batch
    never issues the same lookup twice and offline runs still reproduce.
  * A DNS failure is not proof of undeliverability. When the resolver cannot be
    reached the address is kept and flagged `unknown`, never silently nulled.
  * A negative MX result is also not proof the address should be deleted. The
    value stays on the row, flagged `undeliverable`, so enrichment can try a
    replacement. Only invalid syntax and configured placeholder/reserved
    domains are cleared here.

Set LEAD_HYGIENE_MX_LOOKUP=1 to do live MX lookups. Hygiene defaults to
cache-only (or unknown when uncached) so batches are fast; failed MX never
blanks a syntactically valid address anyway.
"""

import json
import os
from pathlib import Path

from email_validator import EmailNotValidError, caching_resolver, validate_email

from .domains import is_placeholder_domain
from .textnorm import cell

MX_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "mx_cache.json"

DELIVERABLE = "deliverable"
UNDELIVERABLE = "undeliverable"
INVALID_SYNTAX = "invalid_syntax"
UNKNOWN = "unknown"

# RFC 2606 / RFC 6761 reserved names. These can never receive mail, and some of
# them do publish MX records, so they are settled before any lookup.
RESERVED_TLDS = ("test", "example", "invalid", "localhost")
RESERVED_SECOND_LEVEL = ("example.com", "example.net", "example.org")

_memo = {}
_resolver = None


def _lookups_enabled() -> bool:
    return os.environ.get("LEAD_HYGIENE_MX_LOOKUP", "0") in {"1", "true", "True"}


def _load_cache() -> dict:
    if not MX_CACHE_PATH.exists():
        return {}
    try:
        with MX_CACHE_PATH.open(encoding="utf-8") as handle:
            return {str(k).lower(): bool(v) for k, v in json.load(handle).items()}
    except (OSError, ValueError):
        return {}


_disk_cache = _load_cache()


def _get_resolver():
    global _resolver
    if _resolver is None:
        _resolver = caching_resolver(timeout=5)
    return _resolver


def is_reserved_domain(domain) -> bool:
    """True for domains reserved by RFC and therefore never mail-routable."""
    host = cell(domain).lower().strip(".")
    if not host:
        return False
    if host in RESERVED_SECOND_LEVEL:
        return True
    labels = host.split(".")
    if labels[-1] in RESERVED_TLDS:
        return True
    # example.com.tw and friends: a reserved label used as the registrable name.
    return len(labels) >= 2 and labels[0] == "example" and labels[1] in {"com", "net", "org"}


def domain_accepts_mail(domain) -> bool:
    """
    Cached MX resolution. Returns True when mail is routable, False when the
    domain provably does not accept mail. Raises LookupError when unknown.
    """
    host = cell(domain).lower()
    if not host:
        raise LookupError("no domain")
    if host in _memo:
        return _memo[host]
    if host in _disk_cache:
        _memo[host] = _disk_cache[host]
        return _memo[host]
    if not _lookups_enabled():
        raise LookupError(f"MX lookup disabled and {host} is not cached")
    try:
        validate_email(
            f"probe@{host}", check_deliverability=True, dns_resolver=_get_resolver(),
        )
        _memo[host] = True
    except EmailNotValidError:
        _memo[host] = False
    except Exception as exc:  # resolver/network failure, not a verdict
        raise LookupError(f"MX lookup failed for {host}: {exc}") from exc
    return _memo[host]


def validate_lead_email(email):
    """
    Returns {value, status, reason, raw}.

    status is deliverable | undeliverable | invalid_syntax | unknown.
    `value` is the normalized address, or '' when it must be nulled
    (invalid syntax or a placeholder/reserved domain).
    """
    raw = cell(email)
    if not raw:
        return {"value": "", "status": INVALID_SYNTAX, "reason": "", "raw": raw}

    try:
        info = validate_email(raw, check_deliverability=False)
    except EmailNotValidError as exc:
        return {
            "value": "", "status": INVALID_SYNTAX, "raw": raw,
            "reason": f"email: cleared, failed syntax validation ({exc})",
        }

    normalized = info.normalized.lower()
    domain = info.domain.lower()

    if is_reserved_domain(domain) or is_placeholder_domain(domain):
        return {
            "value": "", "status": UNDELIVERABLE, "raw": raw,
            "reason": (
                f"email: cleared, {domain} is a placeholder or RFC-reserved "
                "domain that cannot receive mail"
            ),
        }

    try:
        routable = domain_accepts_mail(domain)
    except LookupError as exc:
        return {
            "value": normalized, "status": UNKNOWN, "raw": raw,
            "reason": f"email: kept, deliverability could not be determined ({exc})",
        }

    if not routable:
        return {
            "value": normalized, "status": UNDELIVERABLE, "raw": raw,
            "reason": (
                f"email: kept, {domain} has no mail exchanger (MX lookup); "
                "flagged so enrichment can search for a replacement"
            ),
        }
    return {"value": normalized, "status": DELIVERABLE, "reason": "", "raw": raw}
