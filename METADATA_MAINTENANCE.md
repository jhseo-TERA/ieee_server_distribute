# Metadata backfill and field repairs

Weekly collection and historical backfill have different reports. The weekly
job continues to cover its configured current/previous years. Historical runs
use `scripts/backfill_metadata.py` and a dedicated output directory; they do not
overwrite `logs/metadata_updates/latest_*.json`.

## Before changing stored metadata

Create a Git checkpoint and run `scripts/backup_mysql.ps1 -NoPrune` for a private
local database backup. Keep the dump, raw publisher responses and personal
state snapshots out of Git. `scripts/metadata_integrity.py` captures original
paper identities, PDF paths/availability, legacy favorites and all paper foreign
key relationships for before/after comparison.

## Historical IEEE collection

```powershell
.venv\Scripts\python.exe scripts\backfill_metadata.py --output outputs\my_backfill --sources TCAS-I,JSSC --apply
```

Omit `--apply` to collect and validate only. `--years 2011` selects pilot years
within the reviewed ranges. The default scope is the 17 sources and ranges in
`RANGES`; it does not extend into pre-2006 literature or add new conferences.

Each source/year saves raw Crossref response files, source configuration hash,
candidate hash, known existing DOI/document anchors, exclusions and application
records. Validated candidates are imported without another network collection.
Existing rows are preserved verbatim. A different document with the same DOI,
a different DOI on an existing document, or conflicting publication ownership
is held for review. A matching title never authorizes a merge.

Status meanings:

- `completed`: selected eligible candidates were processed and known anchors
  were accounted for.
- `confirmed_absence`: an evidenced publication-schedule exception applies.
- `completed_with_gaps`: valid candidates were processed but known anchors need
  explanation.
- `completed_with_conflicts`: valid candidates were processed; identity or DOI
  conflicts remain in a separate file.
- `unresolved`: collection/validation/application failed. This is not a verified
  zero-paper edition.

Completed units are skipped on rerun. Unresolved/held units remain pending unless
`--retry-unresolved` is specified. Candidate and configuration hashes must match
before a cached conflict batch can be retried. Historical application records
are retained and inserted counts accumulate across retries.
Use `--revalidate` after changing collection rules to recollect selected units,
including previously completed ones. Previous unit reports are saved under
`history/`; the original raw responses and committed application records remain.

The default collector requires authors. A reviewed publisher record with no
author metadata may be inserted with a null author only through the explicit
`import_new_records(..., allow_missing_authors=True)` maintenance path. Review
these records separately to avoid importing covers, advertisements and indexes.

## DOI and archive field repairs

`scripts/metadata_enrichment.py` consumes an explicit JSON plan. Each entry has
`id`, `article_number`, `expected`, `new`, and `evidence`; null-only DOI fills also
set `blank_doi_only: true`. Plan evidence must establish identity through an
exact publisher document ID, or the local source file's key/title/source and
one unambiguous DOI. Never generate an IEEE DOI from a numeric document ID.

```powershell
.venv\Scripts\python.exe scripts\metadata_enrichment.py --plan outputs\reviewed_plan.json --output outputs\repair_preview
.venv\Scripts\python.exe scripts\metadata_enrichment.py --plan outputs\reviewed_plan.json --output outputs\repair_apply --apply
```

The first batch has at most 200 rows; subsequent batches have at most 2,000.
All metadata writers share an advisory lock. Current values are locked and
compared with the plan before changing the whitelisted bibliographic fields.
Nonblank DOI values are preserved by null-only plans, ambiguous DOI owners are
held, and paper IDs, document keys, source systems, PDFs and favorites cannot
be changed by field-repair plans. Per-batch before/after values are saved before
the transaction and marked committed only after verification. Reapplying a
successful plan is a no-op; a crash after commit can be reconciled from actual
values and the saved prepared batch.

`scripts/validate_nature_doi_candidates.py` verifies Nature candidates against
the exact registered DOI, normalized title and venue. Legacy `ncomms` keys are
supported. `scripts/recover_archive_metadata.py` builds archive proposals from
saved official metadata and PDF evidence without writing to the database.

Unclassified records and genuine/undetermined DOI blanks may remain. Keep
Optica multiple-DOI aliases pending until a canonical DOI is established; local
citation lookup excludes these ambiguous keys instead of selecting the last
workbook value. Publisher removal notices are recorded as exceptions, not
treated as a successfully recovered article.
If exact publisher metadata later establishes a removed document's bibliography,
its title, authors and source can be repaired while retaining evidence that the
local PDF is a removal notice. This does not restore the original article or
imply scientific misconduct; preserve the publisher's stated reason separately.

## Verification and recovery

Run the metadata unit tests and opt-in MySQL tests; the latter use
connection-local temporary tables, not the persistent papers table.

```powershell
$env:METADATA_MYSQL_TESTS='1'
.venv\Scripts\python.exe -m unittest tests.test_metadata_enrichment tests.test_metadata_backfill tests.test_metadata_mysql
```

After application, compare integrity snapshots, verify every accepted candidate
against its stored key/DOI, and test title search and existing PDF delivery.
`scripts/verify_metadata_enrichment.py --run outputs/my_backfill_run --output
outputs/my_backfill_run/final_verification` also replays committed insert and
field-repair audits, checks every inserted bibliographic field and every repaired
field against the current database, and rejects uncommitted audit records.
For audits without commit timestamps, replay order uses the files' final write
times; preserve those times when copying an audit directory. The verification
report records the exact ordering used.
Updating DOI fields alone does not add DOI matching to the general web search
or DOI export to Zotero/RIS; those are separate features.

For field-level recovery, build a reverse plan from a committed batch's `new`
values as the expected state and `old` values as the proposed state, then review
it before applying. This prevents overwriting intervening edits. Inserted rows
have their assigned IDs recorded; do not delete them if users or research tables
have subsequently referenced them. A full database restore is a last resort,
not the default response to an individual metadata correction.
