"""
validate.py
File and row validation checks for Lead Hygiene pipeline.
"""

import re
import pandas as pd

EMAIL_REGEX = re.compile(r"^[\w\.-]+@[\w\.-]+\.\w+$")
BLANK_TOKENS = {"", "n/a", "unknown", "nan", "none", "null", "-", "--"}


def _strip_formula(value) -> str:
    """Strip spreadsheet formulas, but keep +49... phone numbers and emails."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value)
    if text.startswith("="):
        return text[1:]
    if text.startswith("+") and re.match(r"^\+[A-Za-z]", text):
        return text[1:]
    if text.startswith("@") and re.match(r"^@[A-Za-z]", text):
        return text[1:]
    return text


def _cell(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def validate_dataframe(df: pd.DataFrame) -> tuple:
    """
    Validates CSV contents in place.
    - Strips formula injection characters (=, +, @) from cell starts.
    - Counts validation issues (missing required names, malformed emails).
    Returns (cleaned_df, issue_count).
    """
    issue_count = 0
    df = df.copy()

    for col in df.columns:
        df[col] = df[col].map(_strip_formula)

    for _, row in df.iterrows():
        email = _cell(row.get("Email", ""))
        last_name = _cell(row.get("LastName", ""))
        company = _cell(row.get("Company", ""))

        if not email or not EMAIL_REGEX.match(email):
            issue_count += 1
        if last_name.lower() in BLANK_TOKENS:
            issue_count += 1
        if company.lower() in BLANK_TOKENS:
            issue_count += 1

    return df, issue_count
