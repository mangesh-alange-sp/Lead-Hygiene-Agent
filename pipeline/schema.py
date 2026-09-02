"""
schema.py
Pandera gate on the finished write-back frame. These are hard failures: the
caller must refuse to emit the CSV when any check trips.

Rules enforced:
  * Column headers and their order/count match the source schema exactly
    (catches export bugs such as a header quoted into '"_""' instead of '_').
  * Id is present, non-null, and unique.
  * Every non-null Website matches ^https?://
  * Every non-null Phone is E.164 (^\\+\\d) unless that record explicitly
    carries phone_status == "needs_review".
"""

import re

import pandas as pd
import pandera.pandas as pa
from pandera.errors import SchemaError, SchemaErrors

from .phone import STATUS_NEEDS_REVIEW, STATUS_VALID

WEBSITE_RE = r"^https?://"
E164_RE = r"^\+\d"


class OutputSchemaViolation(Exception):
    """Raised when the finished frame fails the pandera gate."""


def digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def blank_or(series, pattern) -> pd.Series:
    text = series.fillna("").astype(str)
    return (text == "") | text.str.match(pattern)


def build_export_schema(expected_columns) -> pa.DataFrameSchema:
    """Strict, ordered schema so an unexpected/renamed/extra header fails."""
    columns = {}
    for name in expected_columns:
        checks = []
        if name == "Website":
            checks.append(
                pa.Check(
                    lambda s: blank_or(s, WEBSITE_RE),
                    error="Website must be blank or start with http(s)://",
                )
            )
        columns[name] = pa.Column(object, checks=checks, nullable=True, coerce=True)
    if "Id" in columns:
        columns["Id"] = pa.Column(
            object, nullable=False, unique=True, coerce=True,
            checks=[pa.Check(lambda s: s.fillna("").astype(str) != "", error="Id must not be blank")],
        )
    return pa.DataFrameSchema(columns, strict=True, ordered=True, coerce=True)


def check_phone_states(df: pd.DataFrame) -> list:
    """
    Phone must be E.164 or explicitly flagged needs_review. Runs on the
    internal frame because it needs the parallel phone_status column.
    """
    if "Phone" not in df.columns:
        return []
    phone = df["Phone"].fillna("").astype(str)
    status = (
        df["phone_status"].fillna("").astype(str)
        if "phone_status" in df.columns else pd.Series("", index=df.index)
    )
    def describe(mask, message):
        if not mask.any():
            return []
        ids = (
            df.loc[mask, "Id"].astype(str).tolist()
            if "Id" in df.columns else [str(i) for i in df.index[mask]]
        )
        samples = ", ".join(
            f"{lead_id}={value or '(blank)'}"
            for lead_id, value in zip(ids, phone[mask].tolist())
        )
        return [f"{message}: {samples}"]

    has_phone = phone.ne("")
    violations = []
    if "phone_status" not in df.columns:
        # Write-back frame: no status column to lean on, so E.164 is the only
        # shape that can be justified on its own.
        return describe(
            has_phone & ~phone.str.match(E164_RE),
            "Phone is not E.164 and no phone_status column is present to justify it",
        )

    # Exactly two terminal states, and both must be readable from the data.
    violations += describe(
        has_phone & ~status.isin([STATUS_VALID, STATUS_NEEDS_REVIEW]),
        "Phone carries no phone_status of valid or needs_review",
    )
    violations += describe(
        status.eq(STATUS_VALID) & ~phone.str.match(E164_RE),
        "phone_status=valid but the value is not E.164",
    )
    # A flagged value must be the submitted one. Shape alone proves nothing here:
    # an input that already carried +CC and then failed validation is preserved
    # verbatim, so it legitimately looks like E.164 while being needs_review.
    if "Phone_raw" in df.columns:
        submitted = df["Phone_raw"].fillna("").astype(str).map(digits)
        kept = phone.map(digits)
        violations += describe(
            has_phone
            & status.eq(STATUS_NEEDS_REVIEW)
            & submitted.ne("")
            & kept.ne(submitted),
            "phone_status=needs_review but the value no longer matches what was submitted",
        )
    return violations


def check_output_schema(writable: pd.DataFrame, expected_columns, internal=None) -> list:
    """Return human-readable violations (empty means the frame may be written)."""
    violations = []
    actual = [str(col) for col in writable.columns]
    expected = [str(col) for col in expected_columns]
    if actual != expected:
        return [f"Output headers {actual} != expected {expected}"]

    try:
        build_export_schema(expected).validate(writable, lazy=True)
    except (SchemaError, SchemaErrors) as exc:
        failures = getattr(exc, "failure_cases", None)
        if failures is not None and not failures.empty:
            for _, case in failures.head(5).iterrows():
                violations.append(
                    f"pandera: {case.get('column')} failed {case.get('check')} "
                    f"(value={case.get('failure_case')!r})"
                )
        else:
            violations.append(f"pandera: {exc}")

    violations.extend(check_phone_states(internal if internal is not None else writable))
    return violations


def assert_output_schema(writable: pd.DataFrame, expected_columns, internal=None) -> None:
    violations = check_output_schema(writable, expected_columns, internal=internal)
    if violations:
        raise OutputSchemaViolation("; ".join(violations))
