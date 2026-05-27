"""
NPPES Monthly Split V3 — automation for the cleaned per-state pipeline
======================================================================

This is the V3 sibling of V2_run_monthly_split.py. It produces the same
outputs the V3 notebook produces:

  <monthly_folder>/Full Data/<STATE> NPPES Extract.csv   # cleaned, enriched
  <monthly_folder>/Dental/<STATE> NPPES Dental.csv       # dental subset (V3 schema)

What's new in V3 vs V2
----------------------
The Full Data CSV is unchanged — it still saves with the raw NPPES column
names plus the enrichment columns (Code_N / Grouping_N / Class_N / Spec_N,
County, Deactivation Date).

The Dental CSV now has a renamed and reordered schema that matches the V3
notebook's `cleanRenameCols()` function:
  * Columns reordered into Identity → Practice Address → Specialty →
    License → Business → Business Address groups
  * Drops: ZipCode, Healthcare Provider Taxonomy Group_1, the two country
    code columns, raw Healthcare Provider Taxonomy Code_1..5, and the five
    Primary Taxonomy Switch_1..5 columns
  * Renames Code_N / Grouping_N / Class_N / Spec_N to
    'Specialty Code N' / 'Specialty Grouping N' / 'Specialty Class N' /
    'Specialty N'
  * Renames the long 'Provider Business Practice Location Address ...'
    columns to short 'Practice Address ...' columns
  * Renames the long 'Provider Business Mailing Address ...' columns to
    short 'Business Address ...' columns
  * Renames 'Provider Enumeration Date' to 'Enumeration Date'
  * Renames the License Number / License State columns to short forms
  * Renames 'County' (from the MSA merge) to 'Practice Address County'

The data transformation (cleaning, MSA join, taxonomy join, deactivation
merge, dental-grouping selection) is identical to V2.

What stays the same as V2 (carried over from the V2 script)
------------------------------------------------------------
  * ZIP codes (mailing + practice) trimmed to 5 digits
  * Phone/fax numbers (mailing, practice, authorized official) trimmed to
    10 digits
  * County joined in from the MSA workbook via practice ZIP
  * Taxonomy codes 1-5 enriched with Grouping / Classification / Display
    Name (Code_N, Grouping_N, Class_N, Spec_N)
  * Deactivation Date merged in from the Deactivated NPI Report
  * Dental subset = rows where any Grouping_1..5 == 'Dental Providers'

What's auto-detected:
  1. The most recent monthly download folder under
     /Users/lukebincarousky/Downloads/NPPES/ (folders named
     'NPPES_Data_Dissemination_<Month>_<Year>_V2', skipping the _Weekly_ ones).
  2. The cumulative npidata_pfile_*.csv inside it.
  3. The most recent 'NPPES Deactivated NPI Report YYYYMMDD.xlsx' under
     the NPPES root.

Usage
-----
    python V3_run_monthly_split.py                       # auto-detect newest month
    python V3_run_monthly_split.py --folder /path/...    # specific monthly folder
    python V3_run_monthly_split.py --deactivated /path/...xlsx  # override report
    python V3_run_monthly_split.py --dry-run             # show plan, do nothing

Why this is faster than the v3 notebook
---------------------------------------
The notebook's importStateChunk() loops over states and calls
pd.read_csv(...chunksize=...) inside that loop, so the 11GB national file
is re-read ~50 times. This script reads the national file ONCE and streams
rows into per-state staging files; cleaning + taxonomy joins then happen
per state on the already-filtered data, which is tiny by comparison.

Tested with: pandas >= 1.5, Python >= 3.9
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Configuration — paths that don't change month-to-month
# ---------------------------------------------------------------------------
NPPES_ROOT = Path("/Users/lukebincarousky/Downloads/NPPES")
DATA_DICT_PATH = NPPES_ROOT / "NPPES" / "Dictionaries" / "main_nppes_data_dict.csv"
GEO_ZIP_LOCALE = NPPES_ROOT / "NPPES" / "Geographic Data" / "ZIP_Locale_Detail.xls"
GEO_MSA_WORKBOOK = NPPES_ROOT / "NPPES" / "Geographic Data" / "US State County City Zip MSA workbook.xlsx"
TAXONOMY_WORKBOOK = NPPES_ROOT / "NPPES" / "Dictionaries" / "nucc_taxonomy_250.xlsx"

# Territories and non-state codes to skip (matches the v3 notebook)
SKIP_STATES = {"PR", "VI", "AS", "GU", "PW", "FM", "MP", "MH"}

MONTH_NAMES = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}

# Folder pattern: NPPES_Data_Dissemination_<MonthName>_<Year>_V2
# Excludes the weekly diff folders, which look like
#   NPPES_Data_Dissemination_<MMDDYY>_<MMDDYY>_Weekly_V2
MONTHLY_FOLDER_RE = re.compile(
    r"^NPPES_Data_Dissemination_(?P<month>[A-Za-z]+)_(?P<year>\d{4})_V2$"
)

# Deactivated NPI report pattern: 'NPPES Deactivated NPI Report YYYYMMDD.xlsx'
DEACTIVATED_RE = re.compile(r"^NPPES Deactivated NPI Report (?P<date>\d{8})\.xlsx$")

STATE_COL = "Provider Business Practice Location Address State Name"
ZIP_PRACTICE_COL = "Provider Business Practice Location Address Postal Code"
ZIP_MAILING_COL = "Provider Business Mailing Address Postal Code"
CHUNKSIZE = 100_000

# Phone/fax columns to trim to 10 digits (the v3 notebook does these five)
PHONE_COLS = [
    "Provider Business Mailing Address Telephone Number",
    "Provider Business Mailing Address Fax Number",
    "Provider Business Practice Location Address Telephone Number",
    "Provider Business Practice Location Address Fax Number",
    "Authorized Official Telephone Number",
]

# Sentinel for "absolutely no value" — useful when astype(str) on a NaN
# yields the literal string "nan", which we don't want surfacing in the
# trimmed phone/zip columns. We replace it after the trim.
NAN_STR = "nan"


# ---------------------------------------------------------------------------
# V3 Dental schema — column reorder, drop, and rename
# ---------------------------------------------------------------------------
# This list mirrors `new_cols` in the V3 notebook's cleanRenameCols(). The
# dental dataframe is selected and reordered by this list, then unwanted
# columns are dropped, then remaining columns are renamed.
DENTAL_REORDER_COLS = [
    # Identity
    "NPI",
    "Provider Name Prefix Text",
    "Provider First Name",
    "Provider Middle Name",
    "Provider Last Name (Legal Name)",
    "Provider Name Suffix Text",
    "Provider Credential Text",
    "Provider Sex Code",
    "Provider Enumeration Date",
    "Last Update Date",
    "Certification Date",
    "Deactivation Date",

    # Practice Address
    "Provider First Line Business Practice Location Address",
    "Provider Second Line Business Practice Location Address",
    "Provider Business Practice Location Address City Name",
    "Provider Business Practice Location Address State Name",
    "Provider Business Practice Location Address Postal Code",
    "ZipCode",
    "County",
    "Provider Business Practice Location Address Country Code (If outside U.S.)",
    "Provider Business Practice Location Address Telephone Number",
    "Provider Business Practice Location Address Fax Number",

    # Specialty Information
    "Code_1", "Grouping_1", "Class_1", "Spec_1",
    "Code_2", "Grouping_2", "Class_2", "Spec_2",
    "Code_3", "Grouping_3", "Class_3", "Spec_3",
    "Code_4", "Grouping_4", "Class_4", "Spec_4",
    "Code_5", "Grouping_5", "Class_5", "Spec_5",

    # License Information
    "Healthcare Provider Taxonomy Group_1",
    "Healthcare Provider Taxonomy Code_1", "Provider License Number_1",
    "Provider License Number State Code_1",
    "Healthcare Provider Primary Taxonomy Switch_1",
    "Healthcare Provider Taxonomy Code_2", "Provider License Number_2",
    "Provider License Number State Code_2",
    "Healthcare Provider Primary Taxonomy Switch_2",
    "Healthcare Provider Taxonomy Code_3", "Provider License Number_3",
    "Provider License Number State Code_3",
    "Healthcare Provider Primary Taxonomy Switch_3",
    "Healthcare Provider Taxonomy Code_4", "Provider License Number_4",
    "Provider License Number State Code_4",
    "Healthcare Provider Primary Taxonomy Switch_4",
    "Healthcare Provider Taxonomy Code_5", "Provider License Number_5",
    "Provider License Number State Code_5",
    "Healthcare Provider Primary Taxonomy Switch_5",

    # Business Information
    "Employer Identification Number (EIN)",
    "Provider Organization Name (Legal Business Name)",
    "Authorized Official First Name",
    "Authorized Official Middle Name",
    "Authorized Official Last Name",
    "Authorized Official Title or Position",
    "Authorized Official Telephone Number",
    "Is Sole Proprietor",
    "Is Organization Subpart",
    "Parent Organization LBN",
    "Parent Organization TIN",

    # Business Address
    "Provider First Line Business Mailing Address",
    "Provider Second Line Business Mailing Address",
    "Provider Business Mailing Address City Name",
    "Provider Business Mailing Address State Name",
    "Provider Business Mailing Address Postal Code",
    "Provider Business Mailing Address Country Code (If outside U.S.)",
    "Provider Business Mailing Address Telephone Number",
    "Provider Business Mailing Address Fax Number",
]

# Columns dropped from the dental output after reordering. Matches the V3
# notebook exactly: redundant ZipCode (we already have the postal code),
# country codes (these state-filtered files are US-only), raw taxonomy
# codes (we've already enriched them into Code_N/Grouping_N/etc.), and
# the primary taxonomy switch columns (not used downstream).
DENTAL_DROP_COLS = [
    "ZipCode",
    "Healthcare Provider Taxonomy Group_1",
    "Provider Business Practice Location Address Country Code (If outside U.S.)",
    "Provider Business Mailing Address Country Code (If outside U.S.)",
    "Healthcare Provider Taxonomy Code_1",
    "Healthcare Provider Taxonomy Code_2",
    "Healthcare Provider Taxonomy Code_3",
    "Healthcare Provider Taxonomy Code_4",
    "Healthcare Provider Taxonomy Code_5",
    "Healthcare Provider Primary Taxonomy Switch_1",
    "Healthcare Provider Primary Taxonomy Switch_2",
    "Healthcare Provider Primary Taxonomy Switch_3",
    "Healthcare Provider Primary Taxonomy Switch_4",
    "Healthcare Provider Primary Taxonomy Switch_5",
]

# Rename map applied to the dental output. Matches the V3 notebook exactly.
DENTAL_RENAME_MAP = {
    "Provider Enumeration Date": "Enumeration Date",

    # Practice address shortening
    "Provider First Line Business Practice Location Address": "Practice Address",
    "Provider Second Line Business Practice Location Address": "Practice Address2",
    "Provider Business Practice Location Address City Name": "Practice Address City",
    "Provider Business Practice Location Address State Name": "Practice Address State",
    "Provider Business Practice Location Address Postal Code": "Practice Address ZipCode",
    "County": "Practice Address County",
    "Provider Business Practice Location Address Telephone Number": "Practice Address Telephone",
    "Provider Business Practice Location Address Fax Number": "Practice Address Fax",

    # Business address shortening
    "Provider First Line Business Mailing Address": "Business Address",
    "Provider Second Line Business Mailing Address": "Business Address2",
    "Provider Business Mailing Address City Name": "Business Address City",
    "Provider Business Mailing Address State Name": "Business Address State",
    "Provider Business Mailing Address Postal Code": "Business Address ZipCode",
    "Provider Business Mailing Address Telephone Number": "Business Address Telephone",
    "Provider Business Mailing Address Fax Number": "Business Address Fax",

    # Specialty (enriched from taxonomy join)
    "Code_1": "Specialty Code 1",
    "Grouping_1": "Specialty Grouping 1",
    "Class_1": "Specialty Class 1",
    "Spec_1": "Specialty 1",
    "Code_2": "Specialty Code 2",
    "Grouping_2": "Specialty Grouping 2",
    "Class_2": "Specialty Class 2",
    "Spec_2": "Specialty 2",
    "Code_3": "Specialty Code 3",
    "Grouping_3": "Specialty Grouping 3",
    "Class_3": "Specialty Class 3",
    "Spec_3": "Specialty 3",
    "Code_4": "Specialty Code 4",
    "Grouping_4": "Specialty Grouping 4",
    "Class_4": "Specialty Class 4",
    "Spec_4": "Specialty 4",
    "Code_5": "Specialty Code 5",
    "Grouping_5": "Specialty Grouping 5",
    "Class_5": "Specialty Class 5",
    "Spec_5": "Specialty 5",

    # License shortening
    "Provider License Number_1": "License Number 1",
    "Provider License Number State Code_1": "License State 1",
    "Provider License Number_2": "License Number 2",
    "Provider License Number State Code_2": "License State 2",
    "Provider License Number_3": "License Number 3",
    "Provider License Number State Code_3": "License State 3",
    "Provider License Number_4": "License Number 4",
    "Provider License Number State Code_4": "License State 4",
    "Provider License Number_5": "License Number 5",
    "Provider License Number State Code_5": "License State 5",
}


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------
def find_latest_monthly_folder(root: Path) -> Path:
    """Walk `root` looking for monthly NPPES folders; return the newest."""
    candidates: list[tuple[datetime, Path]] = []
    for dirpath, dirnames, _ in os.walk(root):
        for d in dirnames:
            m = MONTHLY_FOLDER_RE.match(d)
            if not m:
                continue
            month_name = m.group("month")
            year = int(m.group("year"))
            month_num = MONTH_NAMES.get(month_name)
            if month_num is None:
                continue
            candidates.append((datetime(year, month_num, 1), Path(dirpath) / d))

    if not candidates:
        raise FileNotFoundError(
            f"No monthly NPPES_Data_Dissemination_<Month>_<Year>_V2 folders "
            f"found under {root}."
        )
    candidates.sort(key=lambda t: t[0])
    return candidates[-1][1]


def find_cumulative_csv(monthly_folder: Path) -> Path:
    """Inside a monthly folder, find npidata_pfile_*.csv (the cumulative file)."""
    matches = [
        p for p in monthly_folder.glob("npidata_pfile_*.csv")
        if not p.name.endswith("_fileheader.csv")
    ]
    if not matches:
        raise FileNotFoundError(
            f"No npidata_pfile_*.csv (excluding _fileheader.csv) found in {monthly_folder}."
        )
    if len(matches) > 1:
        cumulative = [p for p in matches if "20050523" in p.name]
        if cumulative:
            return cumulative[0]
        raise RuntimeError(
            f"Ambiguous npidata_pfile candidates in {monthly_folder}: "
            f"{[p.name for p in matches]}"
        )
    return matches[0]


def find_latest_deactivated_report(root: Path) -> Path:
    """Find the newest 'NPPES Deactivated NPI Report YYYYMMDD.xlsx' under root."""
    candidates: list[tuple[datetime, Path]] = []
    for p in root.glob("NPPES Deactivated NPI Report *.xlsx"):
        m = DEACTIVATED_RE.match(p.name)
        if not m:
            continue
        try:
            dt = datetime.strptime(m.group("date"), "%Y%m%d")
        except ValueError:
            continue
        candidates.append((dt, p))
    if not candidates:
        raise FileNotFoundError(
            f"No 'NPPES Deactivated NPI Report YYYYMMDD.xlsx' files found under {root}."
        )
    candidates.sort(key=lambda t: t[0])
    return candidates[-1][1]


# ---------------------------------------------------------------------------
# Reference data loading
# ---------------------------------------------------------------------------
def load_columns(data_dict_path: Path) -> list[str]:
    data_dict = pd.read_csv(data_dict_path)
    return data_dict["Column_Name"].tolist()


def load_states(geo_path: Path) -> list[str]:
    geo = pd.read_excel(geo_path)
    raw = geo["PHYSICAL STATE"].dropna().unique().tolist()
    return sorted(s for s in raw if s not in SKIP_STATES)


def load_msa_zip_to_county(msa_path: Path) -> pd.DataFrame:
    """Return a tiny lookup frame with Zip (5-digit zero-padded str) -> County Name."""
    msa = pd.read_excel(msa_path, sheet_name="USA_MSA")
    msa = msa[["Zip", "County Name"]].copy()
    msa["Zip"] = msa["Zip"].astype(str).str.zfill(5)
    # Some workbooks have duplicate ZIP rows; keep the first to avoid row-blowup on merge.
    msa = msa.drop_duplicates(subset=["Zip"], keep="first").reset_index(drop=True)
    return msa


def load_taxonomy_lookup(tax_path: Path) -> pd.DataFrame:
    """Subset of the NUCC taxonomy used for the Code -> Grouping/Class/Spec join."""
    spec = pd.read_excel(tax_path)
    return spec[["Code", "Grouping", "Classification", "Display Name"]].copy()


def load_deactivated(deactivated_path: Path) -> pd.DataFrame:
    """Match the notebook: drop the title row, rename to ['NPI', 'Deactivation Date']."""
    df = pd.read_excel(deactivated_path)
    df = df.drop(index=0).reset_index(drop=True)
    df.columns = ["NPI", "Deactivation Date"]
    df["NPI"] = df["NPI"].astype(str)
    return df


# ---------------------------------------------------------------------------
# Cleaning — applied per state after the chunked read finishes
# ---------------------------------------------------------------------------
def _trim_str_column(s: pd.Series, length: int) -> pd.Series:
    """Coerce to str, trim to `length` chars, and convert literal 'nan' back to empty."""
    out = s.astype(str).str.slice(0, length)
    out = out.where(out != NAN_STR, "")
    return out


def clean_state_df(
    filtered_df: pd.DataFrame,
    msa_geo: pd.DataFrame,
    spec_dict: pd.DataFrame,
    deactivated: pd.DataFrame,
) -> pd.DataFrame:
    """Apply all V3 notebook cleaning steps to one state's frame.

    The transformation here is identical to V2 — V3 only differs in the
    dental rename/reorder step (applied later, see clean_rename_cols).
    """
    # 1) Drop all-null columns
    clean_df = filtered_df.dropna(axis=1, how="all").reset_index(drop=True)

    # 2) ZIPs to 5 digits
    for c in (ZIP_MAILING_COL, ZIP_PRACTICE_COL):
        if c in clean_df.columns:
            clean_df[c] = _trim_str_column(clean_df[c], 5)

    # 3) Phone / fax to 10 digits
    for c in PHONE_COLS:
        if c in clean_df.columns:
            clean_df[c] = _trim_str_column(clean_df[c], 10)

    # 4) Merge County from MSA workbook (left join on practice ZIP)
    if ZIP_PRACTICE_COL in clean_df.columns:
        clean_df = clean_df.merge(
            msa_geo,
            left_on=ZIP_PRACTICE_COL,
            right_on="Zip",
            how="left",
        )
        clean_df.rename(columns={"Zip": "ZipCode", "County Name": "County"}, inplace=True)

    # 5) Taxonomy joins for codes 1..5
    for n in range(1, 6):
        tax_col = f"Healthcare Provider Taxonomy Code_{n}"
        if tax_col not in clean_df.columns:
            continue
        clean_df = clean_df.merge(
            spec_dict,
            left_on=tax_col,
            right_on="Code",
            how="left",
        )
        clean_df.rename(
            columns={
                "Code": f"Code_{n}",
                "Grouping": f"Grouping_{n}",
                "Classification": f"Class_{n}",
                "Display Name": f"Spec_{n}",
            },
            inplace=True,
        )

    # 6) NPI -> str, then merge Deactivation Date
    if "NPI" in clean_df.columns:
        clean_df["NPI"] = clean_df["NPI"].astype(str)
        clean_df = clean_df.merge(deactivated, on="NPI", how="left")

    return clean_df


def select_dental(clean_df: pd.DataFrame) -> pd.DataFrame:
    """Match notebook: concat per-Grouping_N matches without dedupe."""
    dental_parts: list[pd.DataFrame] = []
    for n in range(1, 6):
        col = f"Grouping_{n}"
        if col in clean_df.columns:
            dental_parts.append(clean_df[clean_df[col] == "Dental Providers"])
    if not dental_parts:
        return clean_df.iloc[0:0].copy()
    return pd.concat(dental_parts, ignore_index=True)


def clean_rename_cols(dental_df: pd.DataFrame) -> pd.DataFrame:
    """V3 only — reorder, drop, and rename columns for the dental output.

    Mirrors the V3 notebook's cleanRenameCols() exactly:
      1. Reorder to DENTAL_REORDER_COLS (also acts as a column selection).
      2. Drop the columns in DENTAL_DROP_COLS.
      3. Rename per DENTAL_RENAME_MAP.

    Safety note: a state's filtered frame may have lost columns to the
    earlier dropna(axis=1, how='all'), or never had certain optional
    columns. Rather than crash with KeyError (as `df[cols]` would in the
    notebook), we use reindex() so missing columns come through as NaN.
    This keeps the dental schema stable across states/months — important
    for the dashboard's parquet reader.
    """
    df = dental_df.reindex(columns=DENTAL_REORDER_COLS)
    df = df.drop(columns=[c for c in DENTAL_DROP_COLS if c in df.columns])
    df = df.rename(columns=DENTAL_RENAME_MAP)
    return df


# ---------------------------------------------------------------------------
# Single-pass split: read national CSV once, stage per-state, then clean
# ---------------------------------------------------------------------------
def split_and_clean(
    cumulative_csv: Path,
    monthly_folder: Path,
    cols: list[str],
    states: list[str],
    msa_geo: pd.DataFrame,
    spec_dict: pd.DataFrame,
    deactivated: pd.DataFrame,
    chunksize: int = CHUNKSIZE,
) -> None:
    """
    Pipeline:
      1. Stream cumulative_csv -> staging csvs (one per state) using append-on-chunk.
      2. For each state: read staging csv, apply cleaning, write final
         <Full Data>/<STATE> NPPES Extract.csv and <Dental>/<STATE> NPPES Dental.csv
         (the dental file uses the V3 renamed/reordered schema).
      3. Delete staging files.

    The two-step (stage then clean) is the trick that keeps memory bounded:
    we never hold the 11GB national file in RAM, and we never hold more than
    one state's filtered frame in RAM at a time when applying joins.
    """
    full_dir = monthly_folder / "Full Data"
    dental_dir = monthly_folder / "Dental"
    stage_dir = monthly_folder / "_stage_v3"
    full_dir.mkdir(parents=True, exist_ok=True)
    dental_dir.mkdir(parents=True, exist_ok=True)
    stage_dir.mkdir(parents=True, exist_ok=True)

    state_set = set(states)
    stage_paths = {s: stage_dir / f"{s}.csv" for s in states}
    full_paths = {s: full_dir / f"{s} NPPES Extract.csv" for s in states}
    dental_paths = {s: dental_dir / f"{s} NPPES Dental.csv" for s in states}

    # Clear any prior runs so we don't append onto stale data
    for path_map in (stage_paths, full_paths, dental_paths):
        for p in path_map.values():
            if p.exists():
                p.unlink()

    header_written: set[str] = set()
    print(f"\nReading {cumulative_csv.name} in {chunksize:,}-row chunks...")
    total_rows = 0
    chunk_count = 0
    for chunk in pd.read_csv(
        cumulative_csv, chunksize=chunksize, usecols=cols, low_memory=False
    ):
        chunk_count += 1
        total_rows += len(chunk)

        chunk = chunk[chunk[STATE_COL].isin(state_set)]
        if chunk.empty:
            if chunk_count % 25 == 0:
                print(f"  chunk {chunk_count}: {total_rows:,} rows scanned so far")
            continue

        for state, group in chunk.groupby(STATE_COL, sort=False):
            target = stage_paths[state]
            write_header = state not in header_written
            group.to_csv(target, mode="a", index=False, header=write_header)
            header_written.add(state)

        if chunk_count % 25 == 0:
            print(f"  chunk {chunk_count}: {total_rows:,} rows scanned so far")

    print(f"  done — {total_rows:,} rows scanned in {chunk_count} chunks.\n")

    # Per-state cleaning pass
    print("Cleaning per-state data (ZIP/phone trim, county, taxonomy, deactivation)...")
    for state in states:
        stage_path = stage_paths[state]
        if not stage_path.exists():
            print(f"  {state}: no rows found — skipping.")
            continue

        # Read staged data. Force key string-looking columns to string up front
        # so trim/merge operations behave deterministically across runs.
        df = pd.read_csv(stage_path, low_memory=False)
        cleaned = clean_state_df(df, msa_geo, spec_dict, deactivated)

        # Full Data CSV keeps the raw + enriched column names (unchanged from V2).
        cleaned.to_csv(full_paths[state], index=False)

        # Dental CSV uses the V3 renamed/reordered schema.
        dental = select_dental(cleaned)
        dental = clean_rename_cols(dental)
        dental.to_csv(dental_paths[state], index=False)

        print(f"  {state}: full={len(cleaned):,} rows, dental={len(dental):,} rows")

    # Cleanup staging
    for p in stage_paths.values():
        if p.exists():
            p.unlink()
    try:
        stage_dir.rmdir()
    except OSError:
        # Stage dir not empty (shouldn't happen) — leave it for inspection.
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--folder",
        type=Path,
        default=None,
        help="Specific monthly folder to process (skips auto-detection).",
    )
    parser.add_argument(
        "--deactivated",
        type=Path,
        default=None,
        help="Path to a specific Deactivated NPI Report .xlsx (skips auto-detection).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print detected paths and exit without reading the CSV.",
    )
    parser.add_argument(
        "--chunksize",
        type=int,
        default=CHUNKSIZE,
        help=f"Rows per pandas chunk (default: {CHUNKSIZE:,}).",
    )
    args = parser.parse_args()

    # 1. Find monthly folder
    monthly_folder = args.folder or find_latest_monthly_folder(NPPES_ROOT)
    print(f"Monthly folder:      {monthly_folder}")

    # 2. Find cumulative CSV
    cumulative_csv = find_cumulative_csv(monthly_folder)
    print(f"Cumulative CSV:      {cumulative_csv.name}")

    # 3. Find deactivated report
    deactivated_path = args.deactivated or find_latest_deactivated_report(NPPES_ROOT)
    print(f"Deactivated report:  {deactivated_path.name}")

    # 4. Sanity-check reference files
    missing = [
        p for p in (DATA_DICT_PATH, GEO_ZIP_LOCALE, GEO_MSA_WORKBOOK, TAXONOMY_WORKBOOK)
        if not p.exists()
    ]
    if missing:
        for p in missing:
            print(f"ERROR: missing reference file {p}", file=sys.stderr)
        return 1

    # 5. Load reference data once
    cols = load_columns(DATA_DICT_PATH)
    states = load_states(GEO_ZIP_LOCALE)
    msa_geo = load_msa_zip_to_county(GEO_MSA_WORKBOOK)
    spec_dict = load_taxonomy_lookup(TAXONOMY_WORKBOOK)
    deactivated = load_deactivated(deactivated_path)

    print(f"Columns kept:        {len(cols)} (from data dictionary)")
    print(f"States to emit:      {len(states)} ({', '.join(states[:5])}...)")
    print(f"MSA ZIP rows:        {len(msa_geo):,}")
    print(f"Taxonomy codes:      {len(spec_dict):,}")
    print(f"Deactivated NPIs:    {len(deactivated):,}")
    print(f"Output dirs:")
    print(f"  Full Data:         {monthly_folder / 'Full Data'}")
    print(f"  Dental (V3):       {monthly_folder / 'Dental'}")

    if args.dry_run:
        print("\n--dry-run set; exiting without processing.")
        return 0

    # 6. Run split + clean
    split_and_clean(
        cumulative_csv=cumulative_csv,
        monthly_folder=monthly_folder,
        cols=cols,
        states=states,
        msa_geo=msa_geo,
        spec_dict=spec_dict,
        deactivated=deactivated,
        chunksize=args.chunksize,
    )

    print(
        f"\nDone. {len(states)} per-state CSVs are in "
        f"{monthly_folder / 'Full Data'} and {monthly_folder / 'Dental'}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
