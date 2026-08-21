"""
dedupe.py
Deduplication in isolation: dedupe_leads(records) -> (survivors, merge_log).

Survivors keep exactly the keys of the input records; every merge decision goes
into merge_log so the write-back file never grows a column.

Guarantees (see tests/test_dedupe_rules.py):
  * len(survivors) <= len(records)
  * every surviving Id exists in the input
  * no two survivors share an identical normalized email
"""

import pandas as pd
from rapidfuzz import fuzz

from .config import FIRST_NAME_ALIASES
from .domains import company_match_key, is_personal_domain
from .textnorm import cell, digits_only, email_domain, email_local, email_local_fold, fold_text

AUTO_MERGE_MIN = 70
HIGH_MIN = 90
# Written by validate/dedupe for internal routing; never part of the output CSV.
INTERNAL_FIELDS = ("data_quality_flags", "hitl_review")


def normalized_email(value) -> str:
    return cell(value).lower()


def _canonical_first(folded: str) -> str:
    token = (folded or "").split()[0] if folded else ""
    return FIRST_NAME_ALIASES.get(token, token)


def _first_compatible(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if _canonical_first(a) and _canonical_first(a) == _canonical_first(b):
        return True
    a0, b0 = a.split()[0], b.split()[0]
    if fuzz.ratio(a0, b0) >= 75:
        return True
    if len(a0) == 1 and b0.startswith(a0):
        return True
    if len(b0) == 1 and a0.startswith(b0):
        return True
    return False


def _add_flag(current, flag: str) -> str:
    flags = [part for part in cell(current).split("|") if part]
    if flag not in flags:
        flags.append(flag)
    return "|".join(flags)


def _confidence_label(score: int) -> str:
    if score >= HIGH_MIN:
        return "High"
    if score >= AUTO_MERGE_MIN:
        return "Med"
    if score > 0:
        return "Low"
    return ""


def score_pair(row1, row2) -> dict:
    """Score one candidate pair. Accepts any mapping (dict or Series)."""
    signals = []
    score = 0

    e1 = normalized_email(row1.get("Email", ""))
    e2 = normalized_email(row2.get("Email", ""))
    if e1 and e2 and e1 == e2:
        signals.append("exact_email")
        score += 100

    fn1, fn2 = fold_text(row1.get("FirstName", "")), fold_text(row2.get("FirstName", ""))
    ln1, ln2 = fold_text(row1.get("LastName", "")), fold_text(row2.get("LastName", ""))
    names_ok = bool(
        fn1 and fn2 and ln1 and ln2
        and fuzz.ratio(ln1, ln2) >= 85
        and _first_compatible(fn1, fn2)
    )

    c1 = company_match_key(row1.get("Company", ""))
    c2 = company_match_key(row2.get("Company", ""))
    company_sim = fuzz.token_set_ratio(c1, c2) if c1 and c2 else 0
    same_company = company_sim >= 80

    d1, d2 = email_domain(e1), email_domain(e2)
    same_domain = bool(d1 and d1 == d2 and not is_personal_domain(d1))
    local_sim = fuzz.ratio(email_local_fold(e1), email_local_fold(e2)) if e1 and e2 else 0

    p1, p2 = digits_only(row1.get("Phone", "")), digits_only(row2.get("Phone", ""))
    same_phone = bool(p1 and p1 == p2 and len(p1) >= 7)

    if names_ok and same_company:
        signals.append("name+company_fuzzy")
        score += 80 if fuzz.ratio(ln1, ln2) >= 95 and _canonical_first(fn1) == _canonical_first(fn2) else 70
    elif names_ok and same_domain:
        signals.append("name+email_domain")
        score += 70
    elif names_ok and local_sim >= 80 and (same_company or same_domain):
        signals.append("name+email_local")
        score += 65

    if same_phone:
        signals.append("phone_shared")
        score += 25

    if names_ok and company_sim and company_sim < 70 and not same_phone and "exact_email" not in signals:
        signals.append("weak_company")

    unique = list(dict.fromkeys(signals))
    phone_only = unique == ["phone_shared"]
    weak = "weak_company" in unique and score < AUTO_MERGE_MIN
    auto_merge = score >= AUTO_MERGE_MIN and not phone_only and not weak

    return {
        "signals": unique,
        "score": min(score, 100),
        "auto_merge": auto_merge,
        "hitl": (not auto_merge) and (phone_only or weak or 0 < score < AUTO_MERGE_MIN),
    }


def _populated_count(record) -> int:
    return sum(1 for key, value in record.items() if key not in INTERNAL_FIELDS and cell(value))


def _is_test_row(record) -> bool:
    return "test_data" in cell(record.get("data_quality_flags", "")).split("|")


def select_winner(row1, row2) -> tuple:
    """Prefer a real Salesforce Id over a synthetic one, then the fuller record."""
    t1, t2 = _is_test_row(row1), _is_test_row(row2)
    if t1 and not t2:
        return row2, row1
    if t2 and not t1:
        return row1, row2
    id1, id2 = cell(row1.get("Id", "")), cell(row2.get("Id", ""))
    if id1.startswith("00Q") and id2.startswith("DUPE"):
        return row1, row2
    if id2.startswith("00Q") and id1.startswith("DUPE"):
        return row2, row1
    if _populated_count(row1) >= _populated_count(row2):
        return row1, row2
    return row2, row1


def _better_person_name(kept: str, other: str, *, first: bool) -> str:
    """Prefer the fuller given name when two values are the same person (Dave/David)."""
    if not other:
        return kept
    if not kept:
        return other
    k_fold, o_fold = fold_text(kept), fold_text(other)
    if first:
        compatible = _first_compatible(k_fold, o_fold)
    else:
        compatible = fuzz.ratio(k_fold, o_fold) >= 85
    if compatible and len(other) > len(kept):
        return other
    return kept


def _better_email(kept: str, other: str) -> str:
    """Prefer a corporate address with the more complete local-part."""
    if not other:
        return kept
    if not kept:
        return other
    d_kept, d_other = email_domain(kept), email_domain(other)
    p_kept, p_other = is_personal_domain(d_kept), is_personal_domain(d_other)
    if p_kept and not p_other:
        return other
    if p_other and not p_kept:
        return kept
    related = d_kept == d_other or d_kept.endswith("." + d_other) or d_other.endswith("." + d_kept)
    if related:
        l_kept, l_other = email_local(kept), email_local(other)
        if len(l_other) > len(l_kept):
            return other
        if len(l_other) == len(l_kept) and len(d_other) > len(d_kept):
            return other
    return kept


def _better_company(kept: str, other: str) -> str:
    if not other:
        return kept
    if not kept:
        return other
    k_kept, k_other = company_match_key(kept), company_match_key(other)
    if k_kept == k_other and len(other) > len(kept):
        return other
    return kept


def _better_title(kept: str, other: str) -> str:
    """Keep the more specific role when one title is a stripped-down version of the other."""
    if not other:
        return kept
    if not kept:
        return other
    a, b = kept.lower(), other.lower()
    if a == b:
        return kept
    if a in b and len(other) > len(kept):
        return other
    if b in a:
        return kept
    return kept


def _has_calling_code(phone: str) -> bool:
    """True when the value carries an explicit country code (+CC or (+CC))."""
    return phone.startswith("(+") or phone.startswith("+")


def merge_fields(winner, loser) -> dict:
    """Field-level survivorship: fill blanks, keep the more complete email/title/company."""
    merged = dict(winner)
    for key, value in loser.items():
        if key in INTERNAL_FIELDS or key == "Id":
            continue
        if not cell(merged.get(key)) and cell(value):
            merged[key] = value
    merged["Email"] = _better_email(cell(merged.get("Email", "")), cell(loser.get("Email", "")))
    if "FirstName" in merged or "FirstName" in loser:
        merged["FirstName"] = _better_person_name(
            cell(merged.get("FirstName", "")), cell(loser.get("FirstName", "")), first=True,
        )
    if "LastName" in merged or "LastName" in loser:
        merged["LastName"] = _better_person_name(
            cell(merged.get("LastName", "")), cell(loser.get("LastName", "")), first=False,
        )
    if "Company" in merged or "Company" in loser:
        merged["Company"] = _better_company(cell(merged.get("Company", "")), cell(loser.get("Company", "")))
    if "Title" in merged or "Title" in loser:
        merged["Title"] = _better_title(cell(merged.get("Title", "")), cell(loser.get("Title", "")))
    w_phone, l_phone = cell(merged.get("Phone", "")), cell(loser.get("Phone", ""))
    if "Phone" in merged or "Phone" in loser:
        if _has_calling_code(l_phone) and not _has_calling_code(w_phone):
            merged["Phone"] = l_phone
    w_web, l_web = cell(merged.get("Website", "")), cell(loser.get("Website", ""))
    if "Website" in merged or "Website" in loser:
        if l_web.startswith("https://") and not w_web.startswith("https://"):
            merged["Website"] = l_web
    if "data_quality_flags" in winner or "data_quality_flags" in loser:
        merged["data_quality_flags"] = cell(winner.get("data_quality_flags", ""))
        for part in cell(loser.get("data_quality_flags", "")).split("|"):
            merged["data_quality_flags"] = _add_flag(merged["data_quality_flags"], part)
    if "hitl_review" in winner or "hitl_review" in loser:
        if cell(winner.get("hitl_review", "")) == "Yes" or cell(loser.get("hitl_review", "")) == "Yes":
            merged["hitl_review"] = "Yes"
    return merged


class _MergeTracker:
    """Accumulates merge provenance per surviving record without touching the record."""

    def __init__(self):
        self.merged_from = {}
        self.signals = {}
        self.score = {}
        self.decision = {}

    def record_merge(self, survivor_id: str, loser_id: str, result: dict, absorbed: list):
        ids = self.merged_from.setdefault(survivor_id, [])
        for lead_id in [loser_id, *absorbed]:
            if lead_id and lead_id not in ids:
                ids.append(lead_id)
        self._add_signals(survivor_id, result)
        self.decision[survivor_id] = "auto_merge"

    def record_hitl(self, lead_id: str, result: dict):
        self._add_signals(lead_id, result)
        self.decision.setdefault(lead_id, "hitl_review")

    def _add_signals(self, lead_id: str, result: dict):
        existing = self.signals.setdefault(lead_id, [])
        for signal in result["signals"]:
            if signal not in existing:
                existing.append(signal)
        self.score[lead_id] = max(self.score.get(lead_id, 0), result["score"])

    def log(self) -> list:
        rows = []
        for lead_id in sorted(set(self.signals) | set(self.merged_from)):
            rows.append({
                "surviving_lead_id": lead_id,
                "merged_from_ids": ";".join(self.merged_from.get(lead_id, [])),
                "match_signals_used": "|".join(self.signals.get(lead_id, [])),
                "confidence_score": _confidence_label(self.score.get(lead_id, 0)),
                "decision": self.decision.get(lead_id, ""),
            })
        return rows


def _collapse_duplicate_emails(survivors: list, tracker: _MergeTracker) -> list:
    """Final safety pass so no two survivors can share a normalized email."""
    position_by_email = {}
    result = []
    for record in survivors:
        key = normalized_email(record.get("Email", ""))
        if not key:
            result.append(record)
            continue
        position = position_by_email.get(key)
        if position is None:
            position_by_email[key] = len(result)
            result.append(record)
            continue
        winner, loser = select_winner(result[position], record)
        winner = merge_fields(winner, loser)
        result[position] = winner
        tracker.record_merge(
            cell(winner.get("Id", "")),
            cell(loser.get("Id", "")),
            {"signals": ["exact_email"], "score": 100},
            [],
        )
    return result


def dedupe_leads(records) -> tuple:
    """
    Collapse duplicate leads. Pure: input records are never mutated.
    Returns (survivors, merge_log) where survivors keep the input key set.
    """
    working = [dict(record) for record in records]
    input_ids = {cell(record.get("Id", "")) for record in working}
    tracker = _MergeTracker()
    absorbed_by = {}
    merged_indexes = set()
    survivors = []

    for i, record in enumerate(working):
        if i in merged_indexes:
            continue
        merged_indexes.add(i)
        current = record
        for j in range(i + 1, len(working)):
            if j in merged_indexes:
                continue
            candidate = working[j]
            result = score_pair(current, candidate)
            if result["auto_merge"]:
                winner, loser = select_winner(current, candidate)
                winner = merge_fields(winner, loser)
                winner_id, loser_id = cell(winner.get("Id", "")), cell(loser.get("Id", ""))
                absorbed = absorbed_by.pop(loser_id, [])
                tracker.record_merge(winner_id, loser_id, result, absorbed)
                absorbed_by.setdefault(winner_id, []).extend([loser_id, *absorbed])
                current = winner
                merged_indexes.add(j)
            elif result["hitl"]:
                for row in (current, candidate):
                    row["hitl_review"] = "Yes"
                    row["data_quality_flags"] = _add_flag(
                        row.get("data_quality_flags", ""), "low_confidence_match"
                    )
                    tracker.record_hitl(cell(row.get("Id", "")), result)
        survivors.append(current)

    survivors = _collapse_duplicate_emails(survivors, tracker)

    assert len(survivors) <= len(working), "dedupe produced more rows than it received"
    unknown = {cell(r.get("Id", "")) for r in survivors} - input_ids
    assert not unknown, f"dedupe invented lead ids: {sorted(unknown)}"
    return survivors, tracker.log()


def deduplicate_dataframe(df: pd.DataFrame) -> tuple:
    """DataFrame adapter around dedupe_leads. Returns (df, merged_count, merge_log)."""
    initial_count = len(df)
    survivors, merge_log = dedupe_leads(df.to_dict("records"))
    deduped = pd.DataFrame(survivors, columns=list(df.columns)) if survivors else df.iloc[0:0].copy()
    return deduped, initial_count - len(deduped), merge_log
