# Multi-Source Data Prep Workflow (prototype)

Cleanses and merges five extracts with mismatched schemas into one SQLite database. It resolves
duplicate customer keys, runs strict validation, and writes output whose bytes are reproducible:
the same input, or the same rows in a different order, gives the same fingerprint.

**All data is synthetic.** `generate/make_sources.py` writes five files that disagree on column
names, key formats, date formats, money formats and file formats. Nothing here came from a real
system.

## Results (measured)

Run: `python3 prep/workflow.py --data data --out out` (report in `results/run.json`, `out/report.json`).

| Source | Format | Rows in | Rows out | What the prep did |
|---|---|---|---|---|
| CRM customers | CSV | 2,100 | 2,000 | 100 stale duplicates dropped, latest `Last Updated` kept; 652 day-first dates parsed |
| Core accounts | CSV | 3,000 | 3,000 | 649 account numbers zero-padded to 8 digits; 600 customer refs trimmed; 2,772 money strings with thousands separators parsed to cents |
| POS transactions | JSON lines | 20,000 | 20,000 | 9,809 branch codes upper-cased; epoch seconds converted to UTC timestamps |
| Branch reference | CSV | 40 | 40 | branch codes upper-cased |
| Complaints | TSV | 300 | 300 | 300 customer keys normalised; 203 severities lower-cased |

- **Strict validation.** Every table passes: primary key unique, row count reconciles to rows in
  minus duplicates dropped, and all four foreign keys have zero orphans. 0 rows rejected.
- **Build time.** 0.67 s for all five sources on the sandbox, including validation and SQLite write.
- **Determinism.** Two runs on the same input give the same fingerprint. A run on the same
  rows in shuffled order (rewritten files, same formats) gives the same fingerprint. Both are
  tested in `tests/test_prep.py`.
- **Fingerprint.** `1b02739697fd524ed8c2098375d508d91c0f084b552be654a8bc16c3891e7d66`
  (SHA-256 over each table's canonical CSV, tables sorted by name).
- **Tests.** 10 pass, in about 5 s.

## How it works

1. **Config** (`prep/config.json`). One entry per source: file and format, the column rename map
   from source names to canonical names, the normalisation kind for each column, the primary key,
   and which column wins when a key repeats. Foreign keys are listed separately. Adding a source
   means adding a config entry, not changing code.
2. **Normalise** (`prep/workflow.py`, `norm_value`). Each value is handled by its declared kind:
   - `cust`: `C-000123`, `c-000123 `, `123` and ` 123 ` all become the integer 123.
   - `acct`: digits padded to 8 characters.
   - `code`, `lower`, `text`: stripped and case-folded where declared.
   - `date`: ISO or day-first (`%d/%m/%Y`). Impossible dates such as 31/02 are rejected.
   - `money`: `1,234.50` becomes integer cents via `Decimal`, so there is no float rounding.
   - `epoch`: seconds since the Unix epoch become UTC ISO timestamps.
   Counts of changed values go into the report, so you can see what the prep actually touched.
3. **Reject.** Any value that fails its kind rejects the run, with the first raw row indices.
   This is strict: there is no quarantine table.
4. **Resolve duplicates.** Where the config sets `keep_latest_by`, one row per key is kept:
   the one with the latest date. Ties are broken by content, so the result does not depend on
   input order.
5. **Validate.** Primary key unique, row count reconciles, foreign keys have no orphans. Any
   failure raises `ValidationError` before anything is written, so a failed run leaves no
   `prep.db`.
6. **Write and fingerprint.** Tables are sorted by primary key, written to SQLite with a unique
   index on each key, and fingerprinted.

## Run it

Requires Python 3.11 or later.

```
pip install -r requirements.txt
python3 generate/make_sources.py --out data     # synthetic extracts
python3 prep/workflow.py --data data --out out  # cleanse, validate, write out/prep.db
python3 -m pytest -q tests/                     # 10 tests
```

## Tests

- Normalisation: customer keys, account numbers, dates (ISO, day-first, impossible), money.
- Duplicates: 2,100 CRM rows become 2,000 customers, and none of the stale `old…@` emails survive.
- Determinism: rerun gives the same fingerprint; shuffled input gives the same fingerprint.
- Strict validation: an unparseable key aborts the run and writes no database; an orphan
  reference aborts; a missing source column aborts.

## Limitations and things not verified

- **Synthetic data.** The mismatches are the ones this generator writes. Real extracts will have
  other quirks: encodings, quoted newlines, time zones on dates, and fields that change meaning.
- **Only one duplicate rule.** Duplicates are resolved only for the customer table, by latest
  date. The other tables must be unique, or the run aborts.
- **Strict means all-or-nothing.** One bad row stops the whole run. That suits a batch that must
  be exact, but a production pipeline might prefer a quarantine table with an alert.
- **Normalisation is the mapping, not a fuzzy match.** Keys are matched only after the declared
  normalisation. Two customers with different keys that refer to the same person are not merged.
- **Single machine.** Timings are local (2 vCPU sandbox, 0.67 s). No scale test beyond 20,000
  transactions.
- **Shuffle test rewrites files.** It shuffles rows of each source and keeps the format, so it
  checks order independence, not arbitrary reformatting.
- **CI not executed.** `.github/workflows/prep.yml` was written and its YAML parsed. It was not run
  on GitHub Actions. Docker was not used.

## Files

- `generate/make_sources.py`: synthetic extracts
- `prep/workflow.py`, `prep/config.json`: pipeline and per-source mapping
- `tests/test_prep.py`: 10 tests
- `results/run.json`: measured run report (fingerprint, counts, stats)
- `data/`: generated extracts (regenerate with `generate/make_sources.py`)
- `.github/workflows/prep.yml` (not executed)
- `requirements.txt`
