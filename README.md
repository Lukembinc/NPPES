# NPPES Provider Data Pipeline

Tooling for working with the CMS **NPPES / NPI Registry bulk data dissemination files** — the monthly national provider download, not the lookup API.

The national file is ~11 GB and 330 columns wide, which makes it awkward to explore directly. This repo turns it into cleaned, enriched, per-state extracts, keeps them current between monthly releases using the weekly change files, and provides a Streamlit dashboard for browsing the result.

> **Data provenance.** Everything here is built from publicly available CMS data ([NPPES Data Dissemination](https://download.cms.gov/nppes/NPI_Files.html)), supplemented with public NUCC taxonomy and USPS/MSA reference files. This is a personal project. It is not affiliated with, endorsed by, or built using data from any employer.

---

## What it does

```
CMS monthly national file (11 GB, 330 cols)
        │
        ├─ V3_run_monthly_split.py ──► <month>/Full Data/<ST> NPPES Extract.csv   (cleaned + enriched, raw column names)
        │                          └─► <month>/Dental/<ST> NPPES Dental.csv       (dental subset, V3 schema)
        │
        ├─ V3_convert_to_parquet.py ─► data/<month>/Dental/<ST> NPPES Dental.parquet
        │
        └─ V1_Weekly_Changes.py ─────► reconciles CMS weekly change files into the extracts between monthly releases

                                       dashboard/app.py reads the Parquet build
```

**Cleaning and enrichment applied to every extract:**

- ZIP codes (practice + mailing) trimmed to 5 digits
- Phone and fax numbers (practice, mailing, authorized official) trimmed to 10 digits
- County joined in from the MSA workbook via practice ZIP
- Taxonomy codes 1–5 enriched with NUCC grouping, classification, and display name
- Deactivation dates merged in from the CMS Deactivated NPI Report
- Dental subset selected where any taxonomy grouping is `Dental Providers`

## Repo map

| Path | What it is |
|---|---|
| `V3_run_monthly_split.py` | Main pipeline. Splits the national file into cleaned per-state extracts. |
| `V3_convert_to_parquet.py` | Converts the dental CSVs to Parquet for the dashboard. |
| `V1_Weekly_Changes.py` | Applies CMS weekly change files to keep extracts current mid-month. |
| `V3 NPPES National Dataset CSV.ipynb` | Notebook the V3 pipeline was developed from. |
| `Data Preview.ipynb` | Scratch notebook for loading a single state/scope and eyeballing it. |
| `dashboard/app.py` | Streamlit provider lookup UI. |
| `data/` | Dental-only Parquet build, per state, by month. Currently May / June / July 2026. |
| `Dictionaries/` | Column selection dictionary, NUCC taxonomy reference (v25.0). |
| `Geographic Data/` | ZIP → county / MSA crosswalks. |
| `Archive/` | Superseded V2 scripts and notebooks, kept for reference. |

The `data/` folder ships 51 files per month (50 states + DC). Raw dissemination dumps and the Full Data CSV split are gitignored — only the dental Parquet build is tracked.

## Running it

**Dashboard, against the bundled data:**

```bash
pip install -r requirements.txt
cd dashboard
streamlit run app.py
```

**Dashboard, against a local full NPPES tree:**

```bash
NPPES_ROOT=/path/to/NPPES streamlit run app.py
```

The loader resolves its data root from `NPPES_ROOT` if set, otherwise falls back to the repo's bundled `data/` folder. It reads Parquet first and falls back to CSV.

**Rebuilding the extracts** (requires the raw CMS download locally):

```bash
python V3_run_monthly_split.py --dry-run     # show the plan
python V3_run_monthly_split.py               # auto-detect newest month
python V3_convert_to_parquet.py              # then refresh the Parquet build
```

## Design decisions worth noting

**Single-pass read of the national file.** The original notebook looped over states and called `read_csv(chunksize=...)` inside the loop, re-reading the 11 GB file roughly 50 times. `V3_run_monthly_split.py` reads it once and streams rows into per-state staging files; cleaning and taxonomy joins then run per state against already-filtered data. This is the single largest performance difference between the notebook and the script.

**Weekly reconciliation is order-independent.** CMS weekly files contain one row per NPI added or changed that week. Rather than applying diffs sequentially, the script collects the set of touched NPIs, drops all of them from every state file, then re-inserts current active-dental rows into the correct state file. This handles the three cases uniformly — deactivated (removed), no longer dental (removed), and moved to a different state (lands in the right file) — without depending on the order weeks are applied. Weekly folders are auto-detected and applied oldest to newest so the most recent record for each NPI wins.

**Providers can legitimately appear more than once.** The pipeline concatenates one row per dental taxonomy grouping without de-duplicating. Removal during reconciliation is keyed on NPI alone, and re-insertion reproduces the same multiplicity, so the invariant holds across runs.

**Separate Full Data and Dental schemas.** Full Data keeps the raw NPPES column names so it stays comparable to the source. The dental subset is renamed and reordered into Identity → Practice Address → Specialty → License → Business groups, since that's what the dashboard and ad-hoc analysis actually read.

## Current state and limitations

This is a personal working repo, and it reads like one in places:

- Paths to the raw CMS download are hardcoded to a local machine in the pipeline scripts and notebooks. They need editing (or `--folder`) to run elsewhere.
- Script names carry `V1` / `V2` / `V3` version prefixes rather than using git history. `Archive/` holds the superseded versions.
- The dashboard is prototype-grade. Filtering happens in-process against the full state file, which is why `maxMessageSize` is bumped in `.streamlit/config.toml`.
- No test suite.
- The bundled Parquet build covers dental providers only. Rebuilding other specialties requires the raw download.

## Data notes

NPPES is a self-reported registry. Practice addresses, taxonomies, and contact details are maintained by providers themselves and go stale. Deactivation dates come from a separate CMS report merged in during the pipeline run, so an NPI's presence in an extract reflects the state of that report at build time, not real-time status.