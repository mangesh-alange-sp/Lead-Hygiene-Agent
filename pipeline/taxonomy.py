"""Load data/reference_taxonomy.md into section → {alias: canonical} maps."""

from pathlib import Path
import re

from .textnorm import alias_key

TAXONOMY_FILE = Path(__file__).resolve().parent.parent / "data" / "reference_taxonomy.md"


def load_taxonomy(filepath: Path = TAXONOMY_FILE) -> dict:
    """Reads markdown tables and returns a dict of section_name -> {alias: canonical}."""
    taxonomies = {}
    if not filepath.exists():
        return taxonomies

    content = filepath.read_text(encoding="utf-8")
    sections = re.split(r"^##\s+", content, flags=re.MULTILINE)

    for section in sections[1:]:
        lines = section.strip().splitlines()
        section_name = lines[0].strip().lower()
        table_map = {}

        for line in lines[1:]:
            if line.startswith("|") and not line.startswith("|---"):
                parts = [p.strip() for p in line.split("|")[1:-1]]
                if len(parts) >= 2:
                    alias, canonical = alias_key(parts[0]), parts[1]
                    if alias and canonical and alias != "alias":
                        table_map[alias] = canonical

        taxonomies[section_name] = table_map

    return taxonomies


TAXONOMY = load_taxonomy()


def resolve_company_alias(raw) -> str:
    """Canonical company name from the alias table, or '' if the name is unknown."""
    from .domains import company_match_key
    from .textnorm import alias_key, cell

    text = cell(raw)
    if not text:
        return ""
    company_tax = TAXONOMY.get("company", {})
    mapped = company_tax.get(alias_key(text))
    if mapped:
        return mapped
    brand = company_match_key(text)
    return company_tax.get(brand) or company_tax.get(brand.replace(" ", "")) or ""


def company_alias_key(raw) -> str:
    """Dedupe key after alias resolution, so P&G and Procter & Gamble share one key."""
    from .domains import company_match_key

    return company_match_key(resolve_company_alias(raw) or raw)
