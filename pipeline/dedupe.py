"""
dedupe.py
Deduplication in isolation: dedupe_leads(records) -> (survivors, merge_log).

Survivors keep exactly the keys of the input records; every merge decision goes
into merge_log so the write-back file never grows a column.

Guarantees (see tests/test_dedupe_rules.py):
  * len(survivors) <= len(records)
  * every surviving Id exists in the input
  * no two survivors share an identical normalized email
  * auto-merge requires exact email, name+company, or name+phone
  * two test-data rows never merge unless they share an exact email
"""

import pandas as pd
from rapidfuzz import fuzz

from rapidfuzz.distance import Levenshtein

from .config import DUMMY_FULL_NAMES, FIRST_NAME_ALIASES, TEST_GIVEN_NAMES, TEST_SURNAMES
from .domains import company_match_key, is_personal_domain
from .taxonomy import company_alias_key
from .textnorm import cell, digits_only, email_domain, email_local, email_local_fold, fold_text

AUTO_MERGE_MIN = 70
HIGH_MIN = 90
# A merge is only legal when one of these is present. Weak fuzzy / junk-pattern
# similarity is never enough, including "both rows look like test data".
IDENTITY_SIGNALS = frozenset({"exact_email", "name+company", "name+phone"})
# Written by validate/dedupe for internal routing; never part of the output CSV.
INTERNAL_FIELDS = (
    "data_quality_flags", "hitl_review", "phone_status", "phone_reason",
    "Phone_raw", "completeness_flag", "email_status",
)


def normalized_email(value) -> str:
    return cell(value).lower()


def canonical_first(folded: str) -> str:
    token = (folded or "").split()[0] if folded else ""
    return FIRST_NAME_ALIASES.get(token, token)


def first_compatible(a: str, b: str) -> bool:
    """Alias or initial match only. Character-ratio similarity is not identity."""
    if not a or not b:
        return False
    if canonical_first(a) and canonical_first(a) == canonical_first(b):
        return True
    a0, b0 = a.split()[0], b.split()[0]
    if len(a0) == 1 and b0.startswith(a0):
        return True
    if len(b0) == 1 and a0.startswith(b0):
        return True
    return False


def placeholder_person(first_folded: str, last_folded: str) -> bool:
    first = (first_folded or "").split()[0] if first_folded else ""
    last = (last_folded or "").split()[0] if last_folded else ""
    if first in TEST_GIVEN_NAMES or last in TEST_SURNAMES:
        return True
    return (first, last) in DUMMY_FULL_NAMES


def phones_near(p1: str, p2: str) -> bool:
    """True when two numbers differ by 1–2 digits. Never an auto-merge by itself."""
    if not p1 or not p2 or p1 == p2:
        return False
    if min(len(p1), len(p2)) < 7 or abs(len(p1) - len(p2)) > 2:
        return False
    return 1 <= Levenshtein.distance(p1, p2) <= 2


def names_identity(row1, row2) -> bool:
    """True only when both rows name the same real person (not junk/test tokens)."""
    fn1, fn2 = fold_text(row1.get("FirstName", "")), fold_text(row2.get("FirstName", ""))
    ln1, ln2 = fold_text(row1.get("LastName", "")), fold_text(row2.get("LastName", ""))
    if not (fn1 and fn2 and ln1 and ln2):
        return False
    if placeholder_person(fn1, ln1) or placeholder_person(fn2, ln2):
        return False
    if ln1 != ln2:
        return False
    return first_compatible(fn1, fn2)


def add_flag(current, flag: str) -> str:
    flags = [part for part in cell(current).split("|") if part]
    if flag not in flags:
        flags.append(flag)
    return "|".join(flags)


SIGNAL_REASONS = {
    "exact_email": "exact email",
    "name+company": "same name and company",
    "name+phone": "same name and phone",
    "name+phone_near": "same name and near-miss phone",
    "phone_shared": "shared phone",
    "switchboard_phone": "shared company switchboard",
    "name+email_domain": "same name and email domain",
    "name+email_local": "similar email local-part",
    "weak_company": "name match with different company",
}


def match_reason(signals, score=0, decision="") -> str:
    """Human-readable why a pair merged or was held. Does not change the decision."""
    parts = [SIGNAL_REASONS.get(signal, signal) for signal in (signals or [])]
    why = " + ".join(parts) if parts else "no identity signal"
    label = confidence_label(score)
    prefix = f"{label}: " if label else ""
    if decision == "hitl_review":
        return f"{prefix}held for review ({why})"
    if decision == "auto_merge":
        return f"{prefix}merged on {why}"
    return f"{prefix}{why}"


def confidence_label(score: int) -> str:
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
        score = max(score, 100)

    names_ok = names_identity(row1, row2)
    fn1 = fold_text(row1.get("FirstName", ""))
    fn2 = fold_text(row2.get("FirstName", ""))

    c1 = company_alias_key(row1.get("Company", ""))
    c2 = company_alias_key(row2.get("Company", ""))
    same_company = bool(c1 and c2 and c1 == c2)

    d1, d2 = email_domain(e1), email_domain(e2)
    same_domain = bool(d1 and d1 == d2 and not is_personal_domain(d1))
    local_sim = fuzz.ratio(email_local_fold(e1), email_local_fold(e2)) if e1 and e2 else 0

    p1, p2 = digits_only(row1.get("Phone", "")), digits_only(row2.get("Phone", ""))
    same_phone = bool(p1 and p1 == p2 and len(p1) >= 7)

    if names_ok and same_company:
        signals.append("name+company")
        score = max(score, 80 if canonical_first(fn1) == canonical_first(fn2) else 70)

    phone_near = phones_near(p1, p2)

    if names_ok and same_phone:
        signals.append("name+phone")
        score = max(score, 75)
    elif same_phone:
        signals.append("phone_shared")
        score = max(score, 25)
    elif names_ok and phone_near:
        signals.append("name+phone_near")
        score = max(score, 20)

    switchboard = bool(
        same_phone and same_company and not names_ok
        and e1 and e2 and e1 != e2
    )
    if switchboard:
        signals.append("switchboard_phone")

    # Review-only: similar people without an identity field. Never auto-merge.
    if names_ok and same_domain and "exact_email" not in signals and not same_company:
        signals.append("name+email_domain")
        score = max(score, 50)
    elif names_ok and local_sim >= 80 and (same_company or same_domain) and "exact_email" not in signals:
        signals.append("name+email_local")
        score = max(score, 50)

    if names_ok and c1 and c2 and not same_company and not same_phone and "exact_email" not in signals:
        signals.append("weak_company")

    unique = list(dict.fromkeys(signals))
    both_test = is_test_row(row1) and is_test_row(row2)
    # Same company switchboard: shared phone is never identity.
    if "switchboard_phone" in unique:
        auto_merge = False
    elif both_test:
        auto_merge = "exact_email" in unique
    else:
        auto_merge = bool(IDENTITY_SIGNALS.intersection(unique))

    phone_only = unique == ["phone_shared"] or unique == ["phone_shared", "switchboard_phone"]
    weak = any(flag in unique for flag in (
        "weak_company", "name+email_domain", "name+email_local", "name+phone_near",
    ))
    hitl = (not auto_merge) and (not both_test) and (
        phone_only or weak or "switchboard_phone" in unique
    )

    return {
        "signals": unique,
        "score": min(score, 100),
        "auto_merge": auto_merge,
        "hitl": hitl,
    }


def populated_count(record) -> int:
    return sum(1 for key, value in record.items() if key not in INTERNAL_FIELDS and cell(value))


def is_test_row(record) -> bool:
    return "test_data" in cell(record.get("data_quality_flags", "")).split("|")


def select_winner(row1, row2) -> tuple:
    """Prefer a real Salesforce Id over a synthetic one, then the fuller record."""
    t1, t2 = is_test_row(row1), is_test_row(row2)
    if t1 and not t2:
        return row2, row1
    if t2 and not t1:
        return row1, row2
    id1, id2 = cell(row1.get("Id", "")), cell(row2.get("Id", ""))
    if id1.startswith("00Q") and id2.startswith("DUPE"):
        return row1, row2
    if id2.startswith("00Q") and id1.startswith("DUPE"):
        return row2, row1
    if populated_count(row1) >= populated_count(row2):
        return row1, row2
    return row2, row1


def better_person_name(kept: str, other: str, *, first: bool) -> str:
    """Prefer the fuller given name when two values are the same person (Dave/David)."""
    if not other:
        return kept
    if not kept:
        return other
    k_fold, o_fold = fold_text(kept), fold_text(other)
    if first:
        compatible = first_compatible(k_fold, o_fold)
    else:
        compatible = fuzz.ratio(k_fold, o_fold) >= 85
    if compatible and len(other) > len(kept):
        return other
    return kept


def better_email(kept: str, other: str) -> str:
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


def prefer_formal_company(kept: str, other: str) -> str:
    """
    When two names are the same company, keep the more complete/formal form
    (Castelity → Castelity GmbH, Sprinklr → Sprinklr Inc.).
    """
    if not other:
        return kept
    if not kept:
        return other
    k_kept, k_other = company_match_key(kept), company_match_key(other)
    if k_kept == k_other and len(other) > len(kept):
        return other
    return kept


def better_company(kept: str, other: str) -> str:
    return prefer_formal_company(kept, other)


def better_title(kept: str, other: str) -> str:
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


def has_calling_code(phone: str) -> bool:
    """True when the value carries an explicit country code (+CC or (+CC))."""
    return phone.startswith("(+") or phone.startswith("+")


def merge_fields(winner, loser) -> dict:
    """Field-level survivorship: fill blanks, keep the more complete email/title/company."""
    merged = dict(winner)
    for key, value in loser.items():
        if key in INTERNAL_FIELDS or key in {"Id", "change_reasons"}:
            continue
        if not cell(merged.get(key)) and cell(value):
            merged[key] = value
    merged["Email"] = better_email(cell(merged.get("Email", "")), cell(loser.get("Email", "")))
    if "FirstName" in merged or "FirstName" in loser:
        merged["FirstName"] = better_person_name(
            cell(merged.get("FirstName", "")), cell(loser.get("FirstName", "")), first=True,
        )
    if "LastName" in merged or "LastName" in loser:
        merged["LastName"] = better_person_name(
            cell(merged.get("LastName", "")), cell(loser.get("LastName", "")), first=False,
        )
    if "Company" in merged or "Company" in loser:
        before = cell(merged.get("Company", ""))
        merged["Company"] = better_company(before, cell(loser.get("Company", "")))
        extra = ""
        if cell(merged.get("Company", "")) != before and cell(loser.get("Id", "")):
            extra = (
                f"merged with {cell(loser.get('Id', ''))}, "
                "company name taken from merged record"
            )
        merged["change_reasons"] = " | ".join(
            part for part in (
                cell(merged.get("change_reasons", "")),
                cell(loser.get("change_reasons", "")),
                extra,
            ) if part
        )
    if "Title" in merged or "Title" in loser:
        merged["Title"] = better_title(cell(merged.get("Title", "")), cell(loser.get("Title", "")))
    w_phone, l_phone = cell(merged.get("Phone", "")), cell(loser.get("Phone", ""))
    if "Phone" in merged or "Phone" in loser:
        if has_calling_code(l_phone) and not has_calling_code(w_phone):
            merged["Phone"] = l_phone
    w_web, l_web = cell(merged.get("Website", "")), cell(loser.get("Website", ""))
    if "Website" in merged or "Website" in loser:
        if l_web.startswith("https://") and not w_web.startswith("https://"):
            merged["Website"] = l_web
    if "data_quality_flags" in winner or "data_quality_flags" in loser:
        merged["data_quality_flags"] = cell(winner.get("data_quality_flags", ""))
        winner_is_test = is_test_row(winner)
        for part in cell(loser.get("data_quality_flags", "")).split("|"):
            # Absorbing a test row must not taint a real survivor (that would
            # drop the real lead from write-back).
            if part == "test_data" and not winner_is_test:
                continue
            merged["data_quality_flags"] = add_flag(merged["data_quality_flags"], part)
    if "hitl_review" in winner or "hitl_review" in loser:
        if cell(winner.get("hitl_review", "")) == "Yes" or cell(loser.get("hitl_review", "")) == "Yes":
            merged["hitl_review"] = "Yes"
    return merged


class MergeTracker:
    """Accumulates merge provenance per surviving record without touching the record."""

    def __init__(self):
        self.merged_from = {}
        self.signals = {}
        self.score = {}
        self.decision = {}
        self.pair_signals = {}
        self.survivor_of = {}

    def record_merge(self, survivor_id: str, loser_id: str, result: dict, absorbed: list):
        ids = self.merged_from.setdefault(survivor_id, [])
        for lead_id in [loser_id, *absorbed]:
            if lead_id and lead_id not in ids:
                ids.append(lead_id)
            if lead_id:
                self.survivor_of[lead_id] = survivor_id
        if loser_id:
            self.pair_signals[loser_id] = list(result.get("signals") or [])
        self.add_signals(survivor_id, result)
        self.decision[survivor_id] = "auto_merge"

    def record_hitl(self, lead_id: str, result: dict):
        self.add_signals(lead_id, result)
        self.decision.setdefault(lead_id, "hitl_review")

    def add_signals(self, lead_id: str, result: dict):
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
                "confidence_score": confidence_label(self.score.get(lead_id, 0)),
                "decision": self.decision.get(lead_id, ""),
                "absorbed_match_signals": ";".join(
                    f"{lid}:{'|'.join(self.pair_signals.get(lid, []))}"
                    for lid in self.merged_from.get(lead_id, [])
                    if lid
                ),
                "match_reason": match_reason(
                    self.signals.get(lead_id, []),
                    self.score.get(lead_id, 0),
                    self.decision.get(lead_id, ""),
                ),
            })
        return rows


def collapse_duplicate_emails(survivors: list, tracker: MergeTracker) -> list:
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
    tracker = MergeTracker()
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
                    row["data_quality_flags"] = add_flag(
                        row.get("data_quality_flags", ""), "low_confidence_match"
                    )
                    tracker.record_hitl(cell(row.get("Id", "")), result)
        survivors.append(current)

    survivors = collapse_duplicate_emails(survivors, tracker)

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
