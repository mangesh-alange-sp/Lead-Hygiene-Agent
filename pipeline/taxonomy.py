"""Load data/reference_taxonomy.md into section → {alias: canonical} maps."""

from pathlib import Path
import re

TAXONOMY_FILE = Path(__file__).resolve().parent.parent / "data" / "reference_taxonomy.md"


def _alias_key(value: str) -> str:
    return re.sub(r"[,.]", "", (value or "").strip().lower())


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
                    alias, canonical = _alias_key(parts[0]), parts[1]
                    if alias and canonical and alias != "alias":
                        table_map[alias] = canonical

        taxonomies[section_name] = table_map

    return taxonomies


TAXONOMY = load_taxonomy()
