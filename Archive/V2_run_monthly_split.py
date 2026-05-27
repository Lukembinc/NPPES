"""
NPPES Monthly Split V2 — automation for the cleaned per-state pipeline
======================================================================

This is the V2 sibling of run_monthly_split.py. It replaces the manual
two-path-edit ritual in 'V2 NPPES National Dataset CSV.ipynb' and produces
the same outputs the notebook produces:

  <monthly_folder>/Full Data/<STATE> NPPES Extract.csv   # cleaned, enriched
  <monthly_folder>/Dental/<STATE> NPPES Dental.csv       # dental subset

What the V2 cleaning step adds on top of v1:
  * ZIP codes (mailing + practice) trimmed to 5 digits
  * Phone/fax numbers (mailing, practice, authorized official) trimmed to 10 digits
  * County joined in from the MSA workbook via practice ZIP
  * Taxonomy codes 1-5 enriched with Grouping / Classification / Display Name
    (Code_N, Grouping_N, Class_N, Spec_N)
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
    python V2_run_monthly_split.py                       # auto-detect newest month
    python V2_run_monthly_split.py --folder /path/...    # specific monthly folder
    python V2_run_monthly_split.py --deactivated /path/...xlsx  # override report
    python V2_run_monthly_split.py --dry-run             # show plan, do nothing

Why this is faster than the v2 notebook
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

# Territories and non-state codes to skip (matches the v2 notebook)
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

# Phone/fax columns to trim to 10 digits (the v2 notebook does these five)
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
    """Apply all V2 notebook cleaning steps to one state's frame."""
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
         <Full Data>/<STATE> NPPES Extract.csv and <Dental>/<STATE> NPPES Dental.csv.
      3. Delete staging files.

    The two-step (stage then clean) is the trick that keeps memory bounded:
    we never hold the 11GB national file in RAM, and we never hold more than
    one state's filtered frame in RAM at a time when applying joins.
    """
    full_dir = monthly_folder / "Full Data"
    dental_dir = monthly_folder / "Dental"
    stage_dir = monthly_folder / "_stage_v2"
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
        cleaned.to_csv(full_paths[state], index=False)

        dental = select_dental(cleaned)
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
    print(f"  Dental:            {monthly_folder / 'Dental'}")

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
