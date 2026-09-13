"""
Salesforce Lead picklist catalog and resolver.

Legal dropdown values come from data/salesforce_picklists.json (or a live
Lead describe). reference_taxonomy.md only supplies extra nicknames.
"""

import json
from pathlib import Path

from .taxonomy import TAXONOMY
from .textnorm import alias_key, cell as _cell

CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "salesforce_picklists.json"

FIELD_FLAGS = {
    "Industry": "unmapped_industry",
    "Country": "unmapped_country",
    "State": "unmapped_state",
    "LeadSource": "unmapped_lead_source",
    "Status": "unmapped_status",
}
TAXONOMY_SECTIONS = {
    "Industry": "industry",
    "Country": "country",
    "State": "state",
    "LeadSource": "lead source",
}
# Status is membership-only: never rewrite a sales stage via synonyms.
SYNONYM_FIELDS = frozenset(TAXONOMY_SECTIONS)

_CATALOG = None


class PicklistCatalogError(Exception):
    """The picklist catalog could not be loaded."""


def _index_field(spec: dict) -> dict:
    options = []
    by_key = {}
    for raw in spec.get("options") or []:
        value = (raw.get("value") or "").strip()
        if not value or not raw.get("active", True):
            continue
        option = {
            "value": value,
            "label": (raw.get("label") or value).strip(),
            "validForCountries": [
                _cell(item) for item in (raw.get("validForCountries") or []) if _cell(item)
            ],
        }
        options.append(option)
        for token in (option["value"], option["label"]):
            key = alias_key(token)
            if key:
                by_key.setdefault(key, value)
    return {
        "restricted": bool(spec.get("restricted", True)),
        "controller": spec.get("controller") or "",
        "options": options,
        "by_key": by_key,
    }


def _prepare_catalog(raw: dict) -> dict:
    fields = {}
    for name, spec in (raw.get("fields") or {}).items():
        fields[name] = _index_field(spec)
    return {"source": raw.get("source") or "cache", "fields": fields}


def load_catalog_file(path: Path = CACHE_PATH) -> dict:
    if not path.exists():
        raise PicklistCatalogError(
            f"Picklist cache not found at {path}. Add data/salesforce_picklists.json "
            "or connect Salesforce so Lead describe can create it."
        )
    return _prepare_catalog(json.loads(path.read_text(encoding="utf-8")))


def save_catalog(raw: dict, path: Path = CACHE_PATH) -> None:
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")


def load_catalog(*, refresh: bool = False, path: Path = CACHE_PATH) -> dict:
    """
    Load picklists into the process-wide catalog.

    When refresh=True and Salesforce is connected, describe Lead and rewrite
    the cache. CSV/test runs use the committed cache.
    """
    global _CATALOG
    if refresh:
        from . import salesforce_io
        if salesforce_io.is_connected():
            raw = salesforce_io.catalog_from_describe(salesforce_io.describe_lead())
            save_catalog(raw, path)
            _CATALOG = _prepare_catalog(raw)
            return _CATALOG
    _CATALOG = load_catalog_file(path)
    return _CATALOG


def get_catalog() -> dict:
    global _CATALOG
    if _CATALOG is None:
        load_catalog(refresh=False)
    return _CATALOG


def reset_catalog() -> None:
    """Test helper so suites can reload after swapping the cache path."""
    global _CATALOG
    _CATALOG = None


def _field_spec(field: str) -> dict:
    catalog = get_catalog()
    spec = (catalog.get("fields") or {}).get(field)
    if spec is None:
        raise PicklistCatalogError(
            f"Picklist cache has no {field} field. Refresh Lead describe or edit "
            f"{CACHE_PATH.name}."
        )
    return spec


def _country_key(country: str) -> str:
    return alias_key(country)


def _option_allowed(option: dict, country: str) -> bool:
    allowed = option.get("validForCountries") or []
    if not allowed:
        return True
    # Full names can match without Country. Abbreviations go through synonyms,
    # which require a country so CA is not always California.
    if not country:
        return True
    key = _country_key(country)
    return any(_country_key(item) == key for item in allowed)


def _picklist_match(field: str, text: str, country: str) -> str:
    spec = _field_spec(field)
    key = alias_key(text)
    if not key:
        return ""
    if field == "State":
        for option in spec["options"]:
            if not _option_allowed(option, country):
                continue
            if alias_key(option["value"]) == key or alias_key(option["label"]) == key:
                return option["value"]
        return ""
    return spec["by_key"].get(key) or ""


def _synonym_match(field: str, text: str, country: str) -> str:
    if field not in SYNONYM_FIELDS:
        return ""
    if field == "State" and not country:
        return ""
    section = TAXONOMY.get(TAXONOMY_SECTIONS[field], {})
    target = section.get(alias_key(text))
    if not target:
        return ""
    return _picklist_match(field, target, country)


def resolve_picklist(field: str, raw, *, country: str = "") -> dict:
    """
    Map messy input to an active Salesforce picklist value.

    Returns {value, matched, flag}. matched False means keep the submitted
    text and flag HITL. Status never uses the synonym table.
    """
    text = _cell(raw)
    flag = FIELD_FLAGS.get(field, f"unmapped_{field.lower()}")
    if not text:
        return {"value": "", "matched": True, "flag": ""}
    country_text = _cell(country)
    matched = _picklist_match(field, text, country_text)
    if matched:
        return {"value": matched, "matched": True, "flag": ""}
    if field != "Status":
        matched = _synonym_match(field, text, country_text)
        if matched:
            return {"value": matched, "matched": True, "flag": ""}
    return {"value": text, "matched": False, "flag": flag}


# Placeholders that appear in exports but are not Salesforce picklist values.
_SEED_SKIP = frozenset({
    "missingqs", "n/a", "na", "none", "null", "unknown", "test", "-", "--",
})
SEED_FIELDS = ("Industry", "LeadSource", "Status", "Country", "State")


def _load_raw_catalog(path: Path = CACHE_PATH) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"source": "cache", "fields": {}}


def seed_catalog_from_values(field_values: dict, path: Path = CACHE_PATH) -> dict:
    """
    Add observed CSV values to the committed mock picklist.

    Use this when Salesforce is not connected. Values whose alias_key already
    matches an option are skipped. Status/LeadSource/Industry from a real
    export are treated as that org's legal answers.
    """
    raw = _load_raw_catalog(path)
    fields = raw.setdefault("fields", {})
    added = {}
    for field, values in field_values.items():
        spec = fields.setdefault(
            field,
            {"restricted": True, "controller": "", "options": []},
        )
        existing = {
            alias_key(opt.get("value") or "")
            for opt in spec.get("options") or []
            if opt.get("value")
        }
        existing.update(
            alias_key(opt.get("label") or "")
            for opt in spec.get("options") or []
            if opt.get("label")
        )
        existing.discard("")
        new_values = []
        for value in values:
            text = _cell(value)
            if not text or text.lower() in _SEED_SKIP:
                continue
            key = alias_key(text)
            if not key or key in existing:
                continue
            spec.setdefault("options", []).append({
                "value": text,
                "label": text,
                "active": True,
            })
            existing.add(key)
            new_values.append(text)
        if new_values:
            added[field] = new_values
    raw["source"] = "mock"
    save_catalog(raw, path)
    reset_catalog()
    load_catalog(refresh=False, path=path)
    return added


def seed_catalog_from_csv(csv_path: Path, path: Path = CACHE_PATH) -> dict:
    import pandas as pd

    frame = pd.read_csv(csv_path, dtype=str).fillna("")
    field_values = {}
    for field in SEED_FIELDS:
        if field not in frame.columns:
            continue
        field_values[field] = sorted({_cell(v) for v in frame[field] if _cell(v)})
    return seed_catalog_from_values(field_values, path=path)


def main(argv=None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Mock Salesforce Lead picklists from a CSV export.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    seed = sub.add_parser(
        "seed",
        help="Add unique Status/LeadSource/Industry/Country/State values from a Lead CSV.",
    )
    seed.add_argument("csv_path", type=Path)
    seed.add_argument("--out", type=Path, default=CACHE_PATH)
    args = parser.parse_args(argv)
    if args.command == "seed":
        added = seed_catalog_from_csv(args.csv_path, path=args.out)
        if not added:
            print("No new picklist values. Cache already covered this file.")
            return 0
        for field, values in added.items():
            print(f"{field}: added {len(values)}")
            for value in values:
                print(f"  {value}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

