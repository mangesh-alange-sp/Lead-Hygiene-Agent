# Lead Hygiene

Cleans Salesforce lead CSVs before write-back: **validate → normalize → dedupe**. Hygiene is a deterministic Python pipeline. A Google ADK agent can run the same job and summarize the result; it does not invent or rewrite lead values.

`deduped.csv` keeps the source column set (no extra audit columns). Merge decisions and quality flags go to `dedup_log.csv`.

## What it does

1. **Validate** — strip Excel formula markers, flag missing email/phone, clear junk and placeholder values, hold hard-required failures for review.
2. **Normalize** — company/title/industry taxonomy, name casing, phone format `(+CC) national`, website derivation when a real company/email domain is available.
3. **Dedupe** — collapse true duplicates only. Auto-merge requires an identity signal:
   - exact email
   - same person at the same company
   - same person + same phone **and** the same company

Same name and phone at **different** companies is held for human review, not merged.
4. **Invariants** — if a safety check fails, the run returns an error and writes nothing.

Shared phone alone, weak name/company similarity, and “both rows look like test data” are not enough to merge. Every lead missing from write-back must be either an explicit test-data drop or absorbed into a survivor via email, phone, or name+company.

## What is removed

**Whole rows dropped** from write-back (logged as `dropped_test_data`):

- Test names (`Test`, `Dummy`, `Fake`, …)
- Test companies (`Test Arp`, `Dummy Company`, …)
- Test email hosts (`test.com`, `*.test`, …)

**Fields blanked, row kept:**

- Placeholder or invalid emails (`example.com`, `wonka.com`, bad syntax)
- Dummy/junk phones (all zeros, NANP `555`, sequential junk)
- Garbage names (`89`, `No Contact`)
- Invalid companies (`n/a`, `missing co`)
- Personal, placeholder, or company-mismatched websites

Missing email **or** missing phone is not dropped. The lead is written back and flagged for review as long as the other contact method is present.

The pipeline does not enrich from ZoomInfo, Clearbit, or Salesforce. Website is the only derived field, and only from a remaining real company or corporate email.

## Requirements

- Python 3.11+
- Dependencies in `requirements.txt`: `google-adk`, `pandas`, `phonenumbers`, `rapidfuzz`

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

For the ADK agent, set a Gemini API key in `.env` (see [Google ADK](https://google.github.io/adk-docs/)):

```bash
echo 'GOOGLE_API_KEY=your-key' > .env
```

## Run the CLI

No LLM required. Writes `deduped.csv` and `dedup_log.csv`. After that, fill blanks from Lusha:

```bash
python enrich_cli.py
python enrich_cli.py path/to/deduped.csv path/to/enriched.csv
```

Needs `LUSHA_API_KEY`. Skips if `deduped.csv` is missing. New phones are formatted to E.164; unmapped industries, employee `0`, and placeholder titles go to `enrichment_review.csv`.

```bash
python dedupe_cli.py path/to/leads.csv
python dedupe_cli.py path/to/leads.csv path/to/cleaned.csv
```

Expected input is a Salesforce lead export. A typical header:

```text
Id,FirstName,LastName,Email,Phone,Company,Title,Website,Industry,Country
```

`Email` is required. Batches are capped at 5,000 rows / 2 million characters.

Live MX lookups are **off** by default. Syntax errors and placeholder domains (`test.com`, `example.com`, …) are still cleared. A failed MX check never blanks a syntactically valid address; turn lookups on only if you want the `undeliverable` flag in `dedup_log.csv`:

```bash
LEAD_HYGIENE_MX_LOOKUP=1 python dedupe_cli.py path/to/leads.csv
```

After a run, check that the write-back still looks right:

```bash
.venv/bin/python -m pipeline.audit deduped.csv --source path/to/leads.csv --log dedup_log.csv
```

That reports row counts, leftover duplicate emails, leftover auto-merge pairs, and missing Ids (merge vs test-data drop).

A sample file lives at `tests/fixtures/lead_data_with_duplicates.csv`.

## Run the ADK agent

From the parent of this package:

```bash
adk web
```

Upload or paste a lead CSV. The agent calls `run_dedup_pipeline` once, saves `deduped.csv` and `dedup_log.csv` as artifacts, and summarizes critical changes, merges, and HITL flags. Follow-up questions about a person or Id use `lookup_lead` against the last run. Full current behavior is in [docs/AGENT.md](docs/AGENT.md).

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

Before changing a transformation, capture a baseline and declare which column the change may touch:

```bash
.venv/bin/python -m pipeline.shadow_diff capture leads.csv --out baseline.csv
.venv/bin/python -m pipeline.shadow_diff compare baseline.csv leads.csv --expect Phone
```

## Project layout

```text
agent.py                 # ADK router (Gemini); does not transform rows
dedupe_cli.py            # Local hygiene runner
enrich_cli.py            # Local Lusha enrich runner
pipeline/
  validate.py            # Quality flags, junk/test handling
  normalize.py           # Taxonomy and field formatting
  dedupe.py              # Identity-based merge
  invariants.py          # Production gate before write-back
  tools.py               # process_csv / ADK tools
  audit.py               # Post-run count / leftover-merge check
data/
  pipeline_config.json        # Allowlists, aliases, dummy values
  reference_taxonomy.md      # Title / company nicknames; extra dropdown synonyms
  salesforce_picklists.json  # Legal Industry / Country / State / LeadSource / Status values
docs/
  PIPELINE_SAFETY.md     # Module boundaries and invariant rules
```

Dropdown fields (Industry, Country, State, LeadSource, Status) use `data/salesforce_picklists.json` as the legal values; the taxonomy file only adds nicknames that the official labels would not catch. Without Salesforce, seed the mock from a Lead export:

```bash
.venv/bin/python -m pipeline.picklists seed path/to/leads.csv
```

That adds unique Status / LeadSource / Industry values from the file. Junk such as `missingQS` is skipped. Adding an allowlist entry is covered by the existing rule-based tests.

## Salesforce write-back

Import `deduped.csv` with the original column set. Use `dedup_log.csv` for merge provenance (`merged_from_ids`, match signals, confidence) and review flags (`missing_email`, `unformatted_phone`, `company_email_mismatch`, …).

This repo does not identify stale-but-valid data. Freshness needs Salesforce dates such as `LastModifiedDate` or `LastActivityDate` on the export; those are not in the current column set.
