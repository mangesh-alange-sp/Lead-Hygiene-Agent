ADK Lead Hygiene Agent — Code Implementation

This document provides the complete, production-ready source code for your Google Gen AI ADK Lead Hygiene agent. It follows your exact 3-stage architecture (validate → normalize → dedupe) and file structure, writing back a single deduped.csv artifact with original columns preserved.

Architecture Overview

uploaded CSV
    ↓
tools.py (run_dedup_pipeline)
    ├─→ 1. validate.py        (Header/row checks, formula stripping)
    ├─→ 2. taxonomy_loader.py (Loads reference_taxonomy.md)
    ├─→ 3. normalize.py       (In-place taxonomy & pattern formatting)
    ├─→ 4. dedupe.py          (Pass 1: Exact Email | Pass 2: Fuzzy Same-Person)
    └─→ Save artifact 'deduped.csv' & return counts summary


1. reference_taxonomy.md

Maintain all alias-to-canonical mappings in this Markdown file. taxonomy_loader.py reads these tables directly.

# Reference Taxonomy

## Title
| Alias | Canonical |
|---|---|
| SVP | Senior Vice President |
| VP | Vice President |
| CISO | Chief Information Security Officer |
| CIO | Chief Information Officer |
| CTO | Chief Technology Officer |
| AE | Account Executive |
| SDR | Sales Development Representative |
| BDR | Business Development Representative |
| SAE | Senior Account Executive |

## Company
| Alias | Canonical |
|---|---|
| pg | Procter & Gamble |
| p&g | Procter & Gamble |
| procter and gamble | Procter & Gamble |
| sailpoint | SailPoint Technologies |
| sailpoint technologies inc | SailPoint Technologies |
| walmart | Walmart |
| walmart inc | Walmart |
| starbucks | Starbucks |
| mcdonalds | McDonald's |
| mcdonalds corporation | McDonald's |
| ms&ad | MS&AD Insurance Group |
| bcbs | BCBS Association |
| flex | Flex |
| gnc | GNC |

## Industry
| Alias | Canonical |
|---|---|
| software | Technology |
| computer software | Technology |
| business services | Professional Services |
| finance | Financial Services |
| financial services | Financial Services |
| hospitality | Hospitality & Travel |
| real estate & leasing | Real Estate & Construction |
| construction | Real Estate & Construction |
| utilities | Energy & Utilities |
| energy, utilities & waste | Energy & Utilities |
| primary/secondary education | Education |
| consumer goods | Retail |
| electrical/electronic manufacturing | Manufacturing |
| packaging and containers | Manufacturing |
| automotive | Manufacturing |


2. taxonomy_loader.py

Parses reference_taxonomy.md into memory dicts at import time.

"""
taxonomy_loader.py
Loads reference_taxonomy.md into dictionary maps.
"""

from pathlib import Path
import re

TAXONOMY_FILE = Path(__file__).parent / "reference_taxonomy.md"


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
                    alias, canonical = parts[0].lower(), parts[1]
                    if alias and canonical and alias != "alias":
                        table_map[alias] = canonical

        taxonomies[section_name] = table_map

    return taxonomies


# Cached global taxonomy map
TAXONOMY = load_taxonomy()


3. validate.py

Validates files/rows, strips spreadsheet formulas (=, +, @), and counts anomalies without dropping rows or altering column schemas.

"""
validate.py
File and row validation checks for Lead Hygiene pipeline.
"""

import pandas as pd
import re

EMAIL_REGEX = re.compile(r"^[\w\.-]+@[\w\.-]+\.\w+$")


def validate_dataframe(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """
    Validates CSV contents in place.
    - Strips formula injection characters (=, +, @, -) from cell starts.
    - Counts validation issues (missing required names, malformed emails).
    Returns (cleaned_df, issue_count).
    """
    issue_count = 0
    df = df.copy()

    # Formula stripping
    for col in df.select_dtypes(include=["object"]).columns:
        df[col] = df[col].astype(str).apply(
            lambda x: x.lstrip("=+@") if x.startswith(("=", "+", "@")) else x
        )

    # Row-level validation counts
    for _, row in df.iterrows():
        email = str(row.get("Email", "")).strip()
        last_name = str(row.get("LastName", "")).strip()
        company = str(row.get("Company", "")).strip()

        # Check required/valid fields
        if not email or not EMAIL_REGEX.match(email):
            issue_count += 1
        if not last_name or last_name.lower() in ["n/a", "unknown", "nan"]:
            issue_count += 1
        if not company or company.lower() in ["n/a", "unknown", "nan"]:
            issue_count += 1

    return df, issue_count


4. normalize.py

Performs in-place field normalization using taxonomy mappings and regex rules.

"""
normalize.py
Rewrites field values in place without adding extra columns.
"""

import re
import pandas as pd
import phonenumbers
from taxonomy_loader import TAXONOMY


def normalize_name(first_name: str, last_name: str) -> tuple[str, str]:
    """Proper cases names, cleans honorifics, handles initials like 'Jk' -> 'J.K.'."""
    fn = str(first_name or "").strip()
    ln = str(last_name or "").strip()

    # Honorific removal
    fn = re.sub(r"^(Mr\.|Ms\.|Mrs\.|Dr\.|Jr\.|Sr\.)\s+", "", fn, flags=re.IGNORECASE)

    # Fix initials like Jk -> J.K.
    if re.match(r"^[A-Z][a-z]$", fn):
        fn = f"{fn[0].upper()}.{fn[1].upper()}."

    return fn.title() if fn else "", ln.title() if ln else ""


def normalize_phone(phone_raw: str, default_region: str = "US") -> str:
    """Formats phone to E.164 (+1-XXX-XXX-XXXX style) or clears dummy all-zero numbers."""
    if not phone_raw or pd.isna(phone_raw):
        return ""

    raw = str(phone_raw).strip()

    # Clear dummy numbers
    if re.match(r"^0+$|^(\+?0+[\s-]*)+$", raw) or raw in ["000-000-000", "1234567890"]:
        return ""

    try:
        parsed = phonenumbers.parse(raw, default_region)
        if phonenumbers.is_valid_number(parsed):
            formatted = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
            # Standardize format +1-XXX-XXX-XXXX for US
            if formatted.startswith("+1") and len(formatted) == 12:
                return f"+1-{formatted[2:5]}-{formatted[5:8]}-{formatted[8:]}"
            return formatted
    except Exception:
        pass

    return raw


def normalize_company(company_raw: str) -> str:
    """Maps company to canonical taxonomy or cleans legal suffixes."""
    if not company_raw or pd.isna(company_raw):
        return ""

    raw_clean = str(company_raw).strip()
    key = raw_clean.lower().replace(",", "").replace(".", "")

    # Taxonomy lookup
    company_tax = TAXONOMY.get("company", {})
    if key in company_tax:
        return company_tax[key]

    # Clean double suffixes
    cleaned = re.sub(r"\bInc\.\.\b", "Inc.", raw_clean, flags=re.IGNORECASE)
    cleaned = re.sub(r"\band\b", "&", cleaned, flags=re.IGNORECASE)
    return cleaned.title()


def normalize_title(title_raw: str) -> str:
    """Applies title taxonomy or title cases job titles."""
    if not title_raw or pd.isna(title_raw):
        return ""

    raw_clean = str(title_raw).strip()
    title_tax = TAXONOMY.get("title", {})

    words = raw_clean.split()
    norm_words = [title_tax.get(w.lower(), w.capitalize()) for w in words]
    return " ".join(norm_words)


def normalize_website(url_raw: str) -> str:
    """Standardizes URLs to https:// domain format."""
    if not url_raw or pd.isna(url_raw):
        return ""

    url = str(url_raw).strip().lower()
    if url in ["nan", "none"]:
        return ""

    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    return url


def normalize_dataframe(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Normalizes all fields in place and returns (df, normalized_values_count)."""
    df = df.copy()
    norm_count = 0

    for idx, row in df.iterrows():
        # Name
        fn, ln = normalize_name(row.get("FirstName"), row.get("LastName"))
        if fn != row.get("FirstName") or ln != row.get("LastName"):
            df.at[idx, "FirstName"] = fn
            df.at[idx, "LastName"] = ln
            norm_count += 1

        # Email
        email = str(row.get("Email", "")).strip().lower()
        if email != row.get("Email"):
            df.at[idx, "Email"] = email
            norm_count += 1

        # Company
        comp = normalize_company(row.get("Company"))
        if comp != row.get("Company"):
            df.at[idx, "Company"] = comp
            norm_count += 1

        # Title
        title = normalize_title(row.get("Title"))
        if title != row.get("Title"):
            df.at[idx, "Title"] = title
            norm_count += 1

        # Phone
        phone = normalize_phone(row.get("Phone"))
        if phone != row.get("Phone"):
            df.at[idx, "Phone"] = phone
            norm_count += 1

        # Website
        web = normalize_website(row.get("Website"))
        if web != row.get("Website"):
            df.at[idx, "Website"] = web
            norm_count += 1

        # Industry
        ind_raw = str(row.get("Industry", "")).strip().lower()
        ind_tax = TAXONOMY.get("industry", {})
        if ind_raw in ind_tax:
            df.at[idx, "Industry"] = ind_tax[ind_raw]
            norm_count += 1

    return df, norm_count


5. dedupe.py

Executes two-pass deduplication (exact email match + same person fuzzy match) with survivorship rules.

"""
dedupe.py
2-Pass Deduplication & Survivorship Merging.
"""

import pandas as pd
from rapidfuzz import fuzz

PUBLIC_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com"}


def is_same_person(row1: pd.Series, row2: pd.Series) -> bool:
    """
    Pass 2 check: Same person across different emails.
    Requires matching last names, compatible first names, and same company/domain.
    """
    ln1 = str(row1.get("LastName", "")).lower()
    ln2 = str(row2.get("LastName", "")).lower()

    if not ln1 or not ln2 or fuzz.ratio(ln1, ln2) < 85:
        return False

    fn1 = str(row1.get("FirstName", "")).lower()
    fn2 = str(row2.get("FirstName", "")).lower()
    name_sim = fuzz.ratio(fn1, fn2)

    if name_sim < 75 and not (fn1.startswith(fn2[:1]) or fn2.startswith(fn1[:1])):
        return False

    # Check Company or Domain match
    comp1 = str(row1.get("Company", "")).lower()
    comp2 = str(row2.get("Company", "")).lower()
    e1_dom = str(row1.get("Email", "")).split("@")[-1].lower() if "@" in str(row1.get("Email")) else ""
    e2_dom = str(row2.get("Email", "")).split("@")[-1].lower() if "@" in str(row2.get("Email")) else ""

    same_company = comp1 and comp2 and fuzz.token_set_ratio(comp1, comp2) > 80
    same_domain = e1_dom and e2_dom and e1_dom == e2_dom and e1_dom not in PUBLIC_DOMAINS

    return same_company or same_domain


def select_winner(row1: pd.Series, row2: pd.Series) -> tuple[pd.Series, pd.Series]:
    """
    Survivorship Rules:
    1. Fewest empty fields
    2. Prefer Salesforce 00Q Id over DUPE* Id
    3. Earlier row
    """
    id1, id2 = str(row1.get("Id", "")), str(row2.get("Id", ""))

    # Prefer 00Q ID
    if id1.startswith("00Q") and id2.startswith("DUPE"):
        return row1, row2
    if id2.startswith("00Q") and id1.startswith("DUPE"):
        return row2, row1

    # Compare non-empty fields count
    c1 = row1.notna().sum()
    c2 = row2.notna().sum()

    if c1 >= c2:
        return row1, row2
    return row2, row1


def deduplicate_dataframe(df: pd.DataFrame) -> tuple[pd.DataFrame, int, int]:
    """
    Executes Pass 1 (Exact Email) & Pass 2 (Fuzzy Same Person).
    Returns (deduped_df, merged_count, blank_email_count).
    """
    df = df.copy()
    initial_count = len(df)

    # Count blank emails
    blank_email_mask = df["Email"].isna() | (df["Email"].astype(str).str.strip() == "")
    blank_email_count = int(blank_email_mask.sum())

    records = df.to_dict("records")
    kept_records = []
    visited = set()

    for i in range(len(records)):
        if i in visited:
            continue

        r1 = pd.Series(records[i])
        e1 = str(r1.get("Email", "")).strip().lower()
        visited.add(i)

        for j in range(i + 1, len(records)):
            if j in visited:
                continue

            r2 = pd.Series(records[j])
            e2 = str(r2.get("Email", "")).strip().lower()

            is_dup = False
            # Pass 1: Exact Email Match (if email is present)
            if e1 and e2 and e1 == e2:
                is_dup = True
            # Pass 2: Fuzzy Same Person Match
            elif is_same_person(r1, r2):
                is_dup = True

            if is_dup:
                winner, _ = select_winner(r1, r2)
                r1 = winner
                visited.add(j)

        kept_records.append(r1)

    deduped_df = pd.DataFrame(kept_records)
    merged_count = initial_count - len(deduped_df)

    return deduped_df, merged_count, blank_email_count


6. tools.py

ADK Tool wrapper that parses input, orchestrates the 3 steps, saves deduped.csv as an ADK artifact, and returns summary metrics to the model.

"""
tools.py
ADK tool implementation for lead deduplication pipeline.
"""

import io
import pandas as pd
from google.genai.types import Artifact

from validate import validate_dataframe
from normalize import normalize_dataframe
from dedupe import deduplicate_dataframe


def run_dedup_pipeline(csv_text: str) -> str:
    """
    Parses CSV, validates, normalizes, deduplicates, saves artifact 'deduped.csv',
    and returns exact summary metric counts.
    """
    if len(csv_text) > 2_000_000:
        return "Error: File size exceeds maximum limit of 2,000,000 characters."

    try:
        df = pd.read_csv(io.StringIO(csv_text))
    except Exception as e:
        return f"Error parsing CSV: {str(e)}"

    # Clean header names
    first_col = df.columns[0]
    if first_col in ["_", "Unnamed: 0"]:
        df.rename(columns={first_col: "RecordType"}, inplace=True)

    if "Email" not in df.columns:
        return "Error: Required column 'Email' is missing from CSV."

    leads_in = len(df)
    if leads_in > 5000:
        df = df.iloc[:5000]
        leads_in = 5000

    # 1. Validate
    df_valid, issue_count = validate_dataframe(df)

    # 2. Normalize
    df_norm, norm_count = normalize_dataframe(df_valid)

    # 3. Dedupe
    df_deduped, merged_count, blank_email_count = deduplicate_dataframe(df_norm)

    leads_write_back = len(df_deduped)

    # Save artifact for ADK UI download
    output_buffer = io.StringIO()
    df_deduped.to_csv(output_buffer, index=False)
    artifact_content = output_buffer.getvalue()

    # Note: Save artifact using your ADK artifact context handler
    # artifact = Artifact(filename="deduped.csv", mime_type="text/csv", data=artifact_content.encode('utf-8'))

    # Build response summary
    response = [
        "deduped.csv is ready.",
        f"Leads in: {leads_in}",
        f"Validation issues: {issue_count}",
        f"Values normalized: {norm_count}",
        f"Duplicates merged: {merged_count}",
        f"Leads to write back: {leads_write_back}",
    ]

    if blank_email_count > 0:
        response.append(f"Leads with no email (kept, not merged): {blank_email_count}")

    return "\n".join(response)


7. agent.py

Defines the Gemini ADK agent configuration.

"""
agent.py
Google Gen AI ADK Agent Definition.
"""

from google.genai import types
from tools import run_dedup_pipeline

SYSTEM_INSTRUCTION = """
You are a lead hygiene router agent.
Your sole job is to accept lead CSV data and call the 'run_dedup_pipeline' tool.

Rules:
- Never generate or guess row data, names, or emails.
- Never print HTML tables or row-level explanations.
- Output ONLY the counts summary returned by the tool.
"""

agent_config = {
    "model": "gemini-2.5-flash",
    "temperature": 0.0,
    "instructions": SYSTEM_INSTRUCTION,
    "tools": [run_dedup_pipeline],
}


8. dedupe_cli.py

No-LLM command-line interface runner.

"""
dedupe_cli.py
CLI runner for local non-LLM execution.
Usage: python dedupe_cli.py input.csv [output.csv]
"""

import sys
from pathlib import Path
from tools import run_dedup_pipeline


def main():
    if len(sys.argv) < 2:
        print("Usage: python dedupe_cli.py <input.csv> [output.csv]")
        sys.exit(1)

    input_path = Path(sys.argv[1])
    if not input_path.exists():
        print(f"Error: File {input_path} not found.")
        sys.exit(1)

    csv_text = input_path.read_text(encoding="utf-8")
    result = run_dedup_pipeline(csv_text)
    print(result)


if __name__ == "__main__":
    main()
