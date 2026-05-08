"""
NPPES Monthly Split — Phase 1 automation
========================================

Replaces the manual two-path-edit ritual in 'NPPES National Dataset CSV.ipynb'.

What it does, end to end:
  1. Auto-detects the most recent monthly download folder under
     /Users/lukebincarousky/Downloads/NPPES/ (folders named
     'NPPES_Data_Dissemination_<Month>_<Year>_V2', skipping the _Weekly_ ones).
  2. Finds the cumulative npidata_pfile_*.csv inside it.
  3. Reads it ONCE, routing each chunk's rows to per-state CSVs in a
     'States/' subfolder of that month's directory.
  4. Drops all-null columns per state file (matching v1 behavior).

Usage
-----
    python run_monthly_split.py                       # auto-detect newest month
    python run_monthly_split.py --folder /path/...    # use a specific monthly folder
    python run_monthly_split.py --dry-run             # show what would run, do nothing

Why this is faster than the v1 notebook
---------------------------------------
The v1 notebook calls the chunked reader inside a per-state loop, so the 11GB
national file is read ~50 times. This script reads it once and partitions
rows by state on the fly, then post-processes each per-state CSV to drop
all-null columns. The pd.concat-in-loop O(n^2) issue is also avoided.

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
GEO_WORKBOOK = NPPES_ROOT / "NPPES" / "Geographic Data" / "ZIP_Locale_Detail.xls"

# Territories and non-state codes to skip (matches the v1 notebook)
SKIP_STATES = {"PR", "VI", "AS", "GU", "PW", "FM", "MP", "MH"}

# Month-name -> month-number lookup for parsing folder names
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

STATE_COL = "Provider Business Practice Location Address State Name"
CHUNKSIZE = 100_000


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
                continue  # weird month spelling — skip
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
        # Pick the one starting with 20050523 (NPPES launch date) if present —
        # that's the cumulative monthly file, not a weekly diff.
        cumulative = [p for p in matches if "20050523" in p.name]
        if cumulative:
            return cumulative[0]
        raise RuntimeError(
            f"Ambiguous npidata_pfile candidates in {monthly_folder}: "
            f"{[p.name for p in matches]}"
        )
    return matches[0]


# ---------------------------------------------------------------------------
# Per-state split, single-pass
# ---------------------------------------------------------------------------
def load_columns(data_dict_path: Path) -> list[str]:
    data_dict = pd.read_csv(data_dict_path)
    return data_dict["Column_Name"].tolist()


def load_states(geo_path: Path) -> list[str]:
    geo = pd.read_excel(geo_path)
    raw = geo["PHYSICAL STATE"].dropna().unique().tolist()
    return sorted(s for s in raw if s not in SKIP_STATES)


def split_national_csv(
    cumulative_csv: Path,
    output_dir: Path,
    cols: list[str],
    states: list[str],
    chunksize: int = CHUNKSIZE,
) -> None:
    """
    Read `cumulative_csv` once and stream rows into per-state CSVs in `output_dir`.

    Strategy
    --------
    First chunk written to a state's file: write with header.
    Subsequent chunks: append without header.
    After all chunks, re-read each per-state file to drop all-null columns
    (matches v1 behavior, but done once per state instead of accumulating in RAM).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    state_set = set(states)

    # Track which state files have already been written (so we know whether to write the header)
    header_written: set[str] = set()
    out_paths = {s: output_dir / f"{s} NPPES Extract.csv" for s in states}

    # Remove any prior runs so we don't append onto stale data
    for p in out_paths.values():
        if p.exists():
            p.unlink()

    print(f"\nReading {cumulative_csv.name} in {chunksize:,}-row chunks...")
    total_rows = 0
    chunk_count = 0
    for chunk in pd.read_csv(
        cumulative_csv, chunksize=chunksize, usecols=cols, low_memory=False
    ):
        chunk_count += 1
        total_rows += len(chunk)

        # Only consider rows whose state is in our target list
        chunk = chunk[chunk[STATE_COL].isin(state_set)]
        if chunk.empty:
            if chunk_count % 25 == 0:
                print(f"  chunk {chunk_count}: {total_rows:,} rows scanned so far")
            continue

        for state, group in chunk.groupby(STATE_COL, sort=False):
            target = out_paths[state]
            write_header = state not in header_written
            group.to_csv(target, mode="a", index=False, header=write_header)
            header_written.add(state)

        if chunk_count % 25 == 0:
            print(f"  chunk {chunk_count}: {total_rows:,} rows scanned so far")

    print(f"  done — {total_rows:,} rows scanned in {chunk_count} chunks.\n")

    # Post-process: drop all-null columns per state file (v1 parity)
    print("Post-processing per-state files (dropping all-null columns)...")
    for state in states:
        path = out_paths[state]
        if not path.exists():
            print(f"  {state}: no rows found — skipping.")
            continue
        df = pd.read_csv(path, low_memory=False)
        cleaned = df.dropna(axis=1, how="all").reset_index(drop=True)
        cleaned.to_csv(path, index=False)
        print(f"  {state}: {len(cleaned):,} rows, {len(cleaned.columns)} columns")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--folder",
        type=Path,
        default=None,
        help="Specific monthly folder to process (skips auto-detection).",
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
    print(f"Monthly folder: {monthly_folder}")

    # 2. Find cumulative CSV
    cumulative_csv = find_cumulative_csv(monthly_folder)
    print(f"Cumulative CSV: {cumulative_csv.name}")

    # 3. Resolve other inputs
    if not DATA_DICT_PATH.exists():
        print(f"ERROR: data dictionary missing at {DATA_DICT_PATH}", file=sys.stderr)
        return 1
    if not GEO_WORKBOOK.exists():
        print(f"ERROR: geographic workbook missing at {GEO_WORKBOOK}", file=sys.stderr)
        return 1

    cols = load_columns(DATA_DICT_PATH)
    states = load_states(GEO_WORKBOOK)
    output_dir = monthly_folder / "States"

    print(f"Output dir:     {output_dir}")
    print(f"Columns kept:   {len(cols)} (from data dictionary)")
    print(f"States to emit: {len(states)} ({', '.join(states[:5])}...)")

    if args.dry_run:
        print("\n--dry-run set; exiting without processing.")
        return 0

    # 4. Run the split
    split_national_csv(cumulative_csv, output_dir, cols, states, chunksize=args.chunksize)

    print(f"\nDone. {len(states)} per-state CSVs are in {output_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
