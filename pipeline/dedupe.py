"""
dedupe.py
2-Pass Deduplication & Survivorship Merging.
"""

import pandas as pd
from rapidfuzz import fuzz

PUBLIC_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.in", "hotmail.com",
    "outlook.com", "aol.com", "live.com", "icloud.com",
}


def _text(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def is_same_person(row1: pd.Series, row2: pd.Series) -> bool:
    """
    Pass 2 check: Same person across different emails.
    Requires matching last names, compatible first names, and same company/domain.
    """
    ln1 = _text(row1.get("LastName", "")).lower()
    ln2 = _text(row2.get("LastName", "")).lower()

    if not ln1 or not ln2 or fuzz.ratio(ln1, ln2) < 85:
        return False

    fn1 = _text(row1.get("FirstName", "")).lower()
    fn2 = _text(row2.get("FirstName", "")).lower()
    name_sim = fuzz.ratio(fn1, fn2)

    if name_sim < 75 and not (
        fn1 and fn2 and (fn1.startswith(fn2[:1]) or fn2.startswith(fn1[:1]))
    ):
        return False

    comp1 = _text(row1.get("Company", "")).lower()
    comp2 = _text(row2.get("Company", "")).lower()
    e1 = _text(row1.get("Email", "")).lower()
    e2 = _text(row2.get("Email", "")).lower()
    e1_dom = e1.split("@")[-1] if "@" in e1 else ""
    e2_dom = e2.split("@")[-1] if "@" in e2 else ""

    same_company = bool(comp1 and comp2 and fuzz.token_set_ratio(comp1, comp2) > 80)
    same_domain = bool(
        e1_dom and e2_dom and e1_dom == e2_dom and e1_dom not in PUBLIC_DOMAINS
    )

    if e1_dom in PUBLIC_DOMAINS or e2_dom in PUBLIC_DOMAINS:
        return same_company
    return same_company or same_domain


def select_winner(row1: pd.Series, row2: pd.Series) -> tuple:
    """
    Survivorship Rules:
    1. Prefer Salesforce 00Q Id over DUPE* Id
    2. Fewest empty fields
    3. Earlier row
    """
    id1, id2 = _text(row1.get("Id", "")), _text(row2.get("Id", ""))

    if id1.startswith("00Q") and id2.startswith("DUPE"):
        return row1, row2
    if id2.startswith("00Q") and id1.startswith("DUPE"):
        return row2, row1

    c1 = int(row1.replace("", pd.NA).notna().sum())
    c2 = int(row2.replace("", pd.NA).notna().sum())

    if c1 >= c2:
        return row1, row2
    return row2, row1


def deduplicate_dataframe(df: pd.DataFrame) -> tuple:
    """
    Executes Pass 1 (Exact Email) & Pass 2 (Fuzzy Same Person).
    Returns (deduped_df, merged_count, blank_email_count).
    """
    df = df.copy()
    initial_count = len(df)

    emails = df["Email"].astype(str).str.strip() if "Email" in df.columns else pd.Series([""] * len(df))
    blank_email_mask = emails.isna() | emails.isin(["", "nan"])
    blank_email_count = int(blank_email_mask.sum())

    records = df.to_dict("records")
    kept_records = []
    visited = set()

    for i in range(len(records)):
        if i in visited:
            continue

        r1 = pd.Series(records[i])
        e1 = _text(r1.get("Email", "")).lower()
        visited.add(i)

        for j in range(i + 1, len(records)):
            if j in visited:
                continue

            r2 = pd.Series(records[j])
            e2 = _text(r2.get("Email", "")).lower()

            is_dup = False
            if e1 and e2 and e1 == e2:
                is_dup = True
            elif is_same_person(r1, r2):
                is_dup = True

            if is_dup:
                winner, _ = select_winner(r1, r2)
                r1 = winner
                visited.add(j)

        kept_records.append(r1)

    deduped_df = pd.DataFrame(kept_records)
    if len(df.columns):
        deduped_df = deduped_df.reindex(columns=df.columns)
    merged_count = initial_count - len(deduped_df)

    return deduped_df, merged_count, blank_email_count
