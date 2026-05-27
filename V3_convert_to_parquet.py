"""
V3 Convert to Parquet — convert cleaned CSVs to Parquet for the dashboard
=========================================================================

This script automates the conversion of the V3 dental CSV files (produced
by V3_run_monthly_split.py) into Parquet format. Parquet files are
significantly smaller and faster to load in the Streamlit dashboard.

The script:
  1. Finds the latest monthly NPPES folder.
  2. Reads all CSVs in the <monthly_folder>/Dental/ directory.
  3. Writes them as Parquet files to the repository's data directory.

What's different from V2_convert_to_parquet.py
----------------------------------------------
The dental CSVs now use the V3 renamed schema (see V3_run_monthly_split.py
and the V3 notebook). The string-typed columns that previously had long
"Provider Business ... Postal Code" names are now "Practice Address
ZipCode" and "Business Address ZipCode", so DTYPE_MAP has been updated
to match.

Heads-up on the dashboard
-------------------------
The output Parquet path is unchanged from V2 (REPO_DATA_ROOT/<monthly
folder>/Dental/), so the new V3 parquets will overwrite the V2 ones in
the same directory. Because the column names are different, the Streamlit
dashboard will need to be updated to reference the new schema:
  - "Provider Business Practice Location Address Postal Code"  ->  "Practice Address ZipCode"
  - "Provider Business Mailing Address Postal Code"            ->  "Business Address ZipCode"
  - "Code_1" / "Grouping_1" / "Class_1" / "Spec_1"             ->  "Specialty Code 1" / "Specialty Grouping 1" / "Specialty Class 1" / "Specialty 1"
  (and likewise for 2..5)
  - "Provider Enumeration Date"                                ->  "Enumeration Date"
  - "County"                                                   ->  "Practice Address County"
  - License Number / License State columns similarly shortened
  - The long "Provider Business Practice Location Address ..." and
    "Provider Business Mailing Address ..." columns are now "Practice
    Address ..." and "Business Address ...".

Usage
-----
    python V3_convert_to_parquet.py
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
NPPES_ROOT = Path("/Users/lukebincarousky/Downloads/NPPES")
REPO_DATA_ROOT = NPPES_ROOT / "NPPES" / "data"

MONTH_NAMES = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}

MONTHLY_FOLDER_RE = re.compile(
    r"^NPPES_Data_Dissemination_(?P<month>[A-Za-z]+)_(?P<year>\d{4})_V2$"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def find_latest_monthly_folder(root: Path) -> Path:
    """Walk `root` looking for monthly NPPES folders; return the newest."""
    candidates: list[tuple[datetime, Path]] = []
    # Only look in the top-level NPPES_ROOT to avoid deep traversal
    for d in os.listdir(root):
        if os.path.isdir(root / d):
            m = MONTHLY_FOLDER_RE.match(d)
            if not m:
                continue
            month_name = m.group("month")
            year = int(m.group("year"))
            month_num = MONTH_NAMES.get(month_name)
            if month_num is None:
                continue
            candidates.append((datetime(year, month_num, 1), root / d))

    if not candidates:
        raise FileNotFoundError(
            f"No monthly NPPES_Data_Dissemination_<Month>_<Year>_V2 folders "
            f"found under {root}."
        )
    candidates.sort(key=lambda t: t[0])
    return candidates[-1][1]


def convert_csv_to_parquet():
    try:
        source_monthly_folder = find_latest_monthly_folder(NPPES_ROOT)
        folder_name = source_monthly_folder.name

        source_dental_dir = source_monthly_folder / "Dental"
        target_dental_dir = REPO_DATA_ROOT / folder_name / "Dental"

        if not source_dental_dir.exists():
            print(f"Error: Source directory does not exist: {source_dental_dir}")
            return

        print(f"Source: {source_dental_dir}")
        print(f"Target: {target_dental_dir}")

        # Ensure target directory exists
        target_dental_dir.mkdir(parents=True, exist_ok=True)

        csv_files = list(source_dental_dir.glob("*.csv"))
        if not csv_files:
            print(f"No CSV files found in {source_dental_dir}")
            return

        print(f"Found {len(csv_files)} CSV files to convert.")

        # Explicitly cast columns that must be strings to avoid numeric inference
        # (which leads to AttributeErrors in the dashboard and dropped leading
        # zeros). Names below match the V3 dental schema produced by
        # V3_run_monthly_split.py — NOT the raw NPPES column names.
        DTYPE_MAP = {
            "NPI": str,
            "Practice Address ZipCode": str,
            "Business Address ZipCode": str,
            # Phone/fax columns are already trimmed to 10-char strings upstream,
            # but force them to str here too so pandas doesn't re-infer them as
            # floats when a CSV happens to be entirely numeric for a column.
            "Practice Address Telephone": str,
            "Practice Address Fax": str,
            "Business Address Telephone": str,
            "Business Address Fax": str,
            "Authorized Official Telephone Number": str,
            # License Numbers can contain leading zeros and mixed formats — keep
            # as strings so the dashboard doesn't have to defensively cast.
            "License Number 1": str,
            "License Number 2": str,
            "License Number 3": str,
            "License Number 4": str,
            "License Number 5": str,
            # EIN can have leading zeros / placeholder values.
            "Employer Identification Number (EIN)": str,
            # TIN similarly.
            "Parent Organization TIN": str,
        }

        for csv_path in csv_files:
            parquet_name = csv_path.with_suffix(".parquet").name
            target_path = target_dental_dir / parquet_name

            print(f"Converting {csv_path.name} -> {parquet_name}...", end="", flush=True)

            # Read CSV with explicit string types for critical columns.
            # Using a dtype map with columns that may not exist in every CSV
            # is fine: pandas only enforces the dtype for columns it sees.
            df = pd.read_csv(csv_path, low_memory=False, dtype=DTYPE_MAP)
            df.to_parquet(target_path, index=False, engine="pyarrow")

            print(" Done.")

        print("\nConversion complete!")
        print(f"Parquet files are ready in: {target_dental_dir}")

    except Exception as e:
        print(f"An error occurred: {e}")


if __name__ == "__main__":
    convert_csv_to_parquet()
