# Pipeline isolation and safety nets

Goal: a fix to one transformation cannot silently change another.

## 1. Module boundaries

Each transformation is a pure function in its own module. No transformation
imports another; they share only the utility modules, and those have their own
test suites.

| Function | Module | Signature |
| --- | --- | --- |
| Phone | `pipeline/phone.py` | `normalize_phone(raw_phone, email="", company="", country="") -> str \| None` |
| Company casing | `pipeline/casing.py` | `normalize_company_casing(raw_company) -> str` |
| Website | `pipeline/website.py` | `derive_website(company, email) -> str \| None`, `resolve_website(existing, company, email)` |
| Dedupe | `pipeline/dedupe.py` | `dedupe_leads(records) -> (survivors, merge_log)` |

Shared utilities, each independently tested:

| Module | Responsibility | Tests |
| --- | --- | --- |
| `pipeline/textnorm.py` | trimming, blank tokens, Excel markers, ASCII folding, email parts | `tests/test_textnorm.py` |
| `pipeline/domains.py` | host parsing, personal/placeholder/plausible domain predicates, domain↔company matching | `tests/test_domains.py` |
| `pipeline/config.py` | loads `data/pipeline_config.json` | exercised by every suite |

`dedupe_leads` never mutates its input and returns survivors with exactly the
input key set. All merge provenance travels in `merge_log`, which is written to
`dedup_log.csv`, so the write-back CSV keeps the Salesforce column set.

## 2. Rule-based tests

Tests assert rules over generated inputs, not row-by-row expected output.
Allowlists live in `data/pipeline_config.json`, so adding an acronym or calling
code extends test coverage automatically.

```bash
pytest -vv
```

Rules currently pinned:

- Any number carrying a known calling code keeps that `(+CC)` prefix.
- A NANP result never has an area code or exchange starting with `0` or `1`.
- A phone value never starts with `+`, `=`, `-`, `@`, an apostrophe, or a tab
  (Excel treats those as formulas or text markers; `+1-206-555-0100` becomes
  arithmetic like `-2088`).
- With no resolvable country signal, the cleaned original is returned, never a guessed `+1`.
- Every configured acronym stays uppercase regardless of surrounding text.
- A free/personal email domain never becomes a Website unless the Company
  independently resolves to a real domain.
- Dedupe never grows the row count, never invents an Id, and never leaves two
  survivors sharing a normalized email.
- Every transformation is idempotent: running it on its own output is a no-op.

## 3. Batch invariants (production gate)

`pipeline/invariants.py` runs on every batch in `process_csv` before the CSV is
serialized. On violation the run returns `status: "error"` with
`invariant_violations` and writes nothing.

Checks: no Excel text marker or formula prefix (`+` `=` `-` `@`) on
`Phone`/`MobilePhone`; no lowercase letter after
a dot in `Company` outside the domain/whitelist forms; no `Website` on a
free-provider domain; no duplicate normalized emails; row count never grows.

## 4. Shadow diff before shipping a change

Capture a baseline before editing, then compare afterwards and declare which
column the change is allowed to touch. Any other changed cell, or any column
added or dropped, exits non-zero.

```bash
# before editing
.venv/bin/python -m pipeline.shadow_diff capture leads.csv --out baseline.csv

# after editing, e.g. a phone-only fix
.venv/bin/python -m pipeline.shadow_diff compare baseline.csv leads.csv --expect Phone
```

## 5. The fixture is a bootstrap, not the safety net

`tests/fixtures/lead_data_with_duplicates.csv` holds the known-tricky records
(Kuo-Tung, Stéphane, Terry, Büchert, Amazon.com Inc., BCBS, R.O.C, SailPoint/
test.com, Honda's shared phone). They are inputs that exercise the rules above;
there is no frozen expected-output file to re-bless on every change.
