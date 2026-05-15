"""
V2 Convert to Parquet — convert cleaned CSVs to Parquet for the dashboard
=========================================================================

This script automates the conversion of cleaned CSV files (produced by 
V2_run_monthly_split.py) into Parquet format. Parquet files are significantly
smaller and faster to load in the Streamlit dashboard.

The script:
  1. Finds the latest monthly NPPES folder.
  2. Reads all CSVs in the <monthly_folder>/Dental/ directory.
  3. Writes them as Parquet files to the repository's data directory.

Usage
-----
    python V2_convert_to_parquet.py
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
        # (which leads to AttributeErrors in the dashboard and dropped leading zeros).
        DTYPE_MAP = {
            "NPI": str,
            "Provider Business Practice Location Address Postal Code": str,
            "Provider Business Mailing Address Postal Code": str,
        }

        for csv_path in csv_files:
            parquet_name = csv_path.with_suffix(".parquet").name
            target_path = target_dental_dir / parquet_name
            
            print(f"Converting {csv_path.name} -> {parquet_name}...", end="", flush=True)
            
            # Read CSV with explicit string types for critical columns
            df = pd.read_csv(csv_path, low_memory=False, dtype=DTYPE_MAP)
            df.to_parquet(target_path, index=False, engine='pyarrow')
            
            print(" Done.")

        print("\nConversion complete!")
        print(f"Parquet files are ready in: {target_dental_dir}")

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    convert_csv_to_parquet()
