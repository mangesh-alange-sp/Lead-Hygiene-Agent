"""
Loads the externalized rule config once so every transformation module and
every test reads the same allowlists.
"""

import json
from functools import lru_cache
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "pipeline_config.json"


@lru_cache(maxsize=1)
def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


CONFIG = load_config()

TITLE_ACRONYMS = frozenset(CONFIG.get("title_acronyms", ()))
REGION_TO_COUNTRY = dict(CONFIG.get("region_to_country", {}))

COMPANY_ACRONYMS = tuple(CONFIG["company_acronyms"])
COMPANY_MIXED_CASE = tuple(CONFIG["company_mixed_case"])
LEGAL_SUFFIX_DISPLAY = dict(CONFIG["legal_suffix_display"])
LEGAL_SUFFIX_MATCH_TOKENS = tuple(CONFIG["legal_suffix_match_tokens"])
DOTTED_LOWERCASE_WHITELIST = tuple(CONFIG["dotted_lowercase_whitelist"])

PERSONAL_EMAIL_DOMAINS = frozenset(CONFIG["personal_email_domains"])
PERSONAL_DOMAIN_PREFIXES = tuple(CONFIG["personal_domain_prefixes"])
PLACEHOLDER_DOMAINS = frozenset(CONFIG["placeholder_domains"])
SOCIAL_HOSTS = frozenset(CONFIG["social_hosts"])
KNOWN_TLDS = frozenset(CONFIG["known_tlds"])

REGION_CALLING = dict(CONFIG["region_calling_codes"])
CC_NATIONAL_LEN = {cc: tuple(bounds) for cc, bounds in CONFIG["calling_code_national_length"].items()}
EMAIL_TLD_TO_REGION = dict(CONFIG["email_tld_to_region"])
EMAIL_COMPOUND_TLD_TO_REGION = dict(CONFIG["email_compound_tld_to_region"])
COUNTRY_NAME_TO_REGION = dict(CONFIG["country_name_to_region"])
DUMMY_PHONE_VALUES = frozenset(CONFIG["dummy_phone_values"])
FIRST_NAME_ALIASES = {
    str(alias).lower(): str(canonical).lower()
    for alias, canonical in dict(CONFIG.get("first_name_aliases", {})).items()
}

MIN_PHONE_DIGITS = int(CONFIG["min_phone_digits"])
MAX_PHONE_DIGITS = int(CONFIG["max_phone_digits"])

# Longest calling code first so 886 wins over 88/8 style prefixes.
CALLING_CODES = tuple(sorted(set(REGION_CALLING.values()), key=len, reverse=True))

# One region per calling code. +1 resolves to US, +7 to RU, shared codes pick the
# larger numbering plan holder so formatting stays stable.
_CALLING_PREFERRED = {"1": "US", "7": "RU"}
CALLING_TO_REGION = {}
for _region, _cc in REGION_CALLING.items():
    if _cc in _CALLING_PREFERRED:
        CALLING_TO_REGION[_cc] = _CALLING_PREFERRED[_cc]
    else:
        CALLING_TO_REGION.setdefault(_cc, _region)
