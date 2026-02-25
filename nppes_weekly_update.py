"""
NPPES Weekly Update Script
==========================
Purpose:
    Merges a weekly NPPES data file into the large master CSV (~10GB).
    The weekly file contains:
        - New NPI records (INSERT)
        - Updated NPI records (UPDATE/UPSERT)

Strategy (memory-efficient):
    1. Load the weekly file entirely into memory (~30K rows ≈ small).
    2. Stream through the master CSV in chunks, writing each chunk to a
       temporary output file — replacing any row whose NPI matches a
       weekly record with the weekly version.
    3. Append any weekly records whose NPIs were NOT found in the master
       file (i.e., brand-new providers).
    4. Replace the master CSV with the updated file.
    5. Optionally re-run the state-split step for affected states only.

Usage:
    python nppes_weekly_update.py \
        --master  /path/to/npidata_pfile_20050523-20260208.csv \
        --weekly  /path/to/npidata_pfile_20260202-20260208.csv \
        --output  /path/to/npidata_pfile_20050523-20260215.csv   # optional
        --states-dir /path/to/States/                             # optional
        --data-dict /path/to/main_nppes_data_dict.csv            # optional

    If --output is omitted the master file is updated in-place.
    If --states-dir is provided the per-state CSVs for affected states
    are regenerated automatically after the merge.
"""

import argparse
import os
import sys
import shutil
import tempfile
import time
from pathlib import Path

import pandas as pd
import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
NPI_COL = "NPI"           # Primary key column
CHUNKSIZE = 100_000       # Rows per chunk when streaming the master file
STATE_COL = "Provider Business Practice Location Address State Name"

TERRITORIES_TO_EXCLUDE = {"PR", "VI", "AS", "GU", "PW", "FM", "MP", "MH"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_weekly(weekly_path: str) -> tuple[pd.DataFrame, dict]:
    """
    Load the weekly file into a dict keyed by NPI for O(1) lookup.
    Returns (weekly_df, weekly_lookup_dict).
    """
    print(f"[1/5] Loading weekly file: {weekly_path}")
    weekly_df = pd.read_csv(weekly_path, low_memory=False, dtype={NPI_COL: str})

    # Normalise NPI to string (strip whitespace / leading zeros issues)
    weekly_df[NPI_COL] = weekly_df[NPI_COL].astype(str).str.strip()

    weekly_dict = {row[NPI_COL]: row for _, row in weekly_df.iterrows()}
    print(f"    Loaded {len(weekly_df):,} weekly records "
          f"({len(weekly_dict):,} unique NPIs).")
    return weekly_df, weekly_dict


def merge_master(master_path: str,
                 weekly_dict: dict,
                 tmp_path: str,
                 cols: list | None = None) -> tuple[set, set]:
    """
    Stream through the master CSV, writing an updated version to tmp_path.

    Returns:
        updated_npis  – NPIs that existed and were replaced
        remaining_npis – weekly NPIs NOT found in master (need appending)
    """
    print(f"[2/5] Streaming master file: {master_path}")
    print(f"      Writing updated records to: {tmp_path}")

    updated_npis: set = set()
    rows_processed = 0
    first_chunk = True
    remaining_npis = set(weekly_dict.keys())

    reader = pd.read_csv(
        master_path,
        chunksize=CHUNKSIZE,
        low_memory=False,
        dtype={NPI_COL: str},
        usecols=cols,
    )

    with open(tmp_path, "w", newline="", encoding="utf-8") as out_fh:
        for chunk in reader:
            chunk[NPI_COL] = chunk[NPI_COL].astype(str).str.strip()

            # Replace rows whose NPI appears in the weekly file
            mask = chunk[NPI_COL].isin(weekly_dict)
            if mask.any():
                hits = chunk.loc[mask, NPI_COL].tolist()
                for npi in hits:
                    updated_npis.add(npi)
                    remaining_npis.discard(npi)
                    # Overwrite with weekly version
                    weekly_row = weekly_dict[npi]
                    for col in chunk.columns:
                        if col in weekly_row.index:
                            chunk.loc[chunk[NPI_COL] == npi, col] = weekly_row[col]

            chunk.to_csv(out_fh, index=False, header=first_chunk)
            first_chunk = False
            rows_processed += len(chunk)

            if rows_processed % 1_000_000 == 0:
                print(f"      ... processed {rows_processed:,} master rows")

    print(f"    Done. {rows_processed:,} master rows processed.")
    print(f"    Updated {len(updated_npis):,} existing NPI records.")
    return updated_npis, remaining_npis


def append_new_records(tmp_path: str,
                       weekly_dict: dict,
                       new_npis: set,
                       master_columns: list) -> int:
    """
    Append brand-new NPIs (not previously in master) to tmp_path.
    Returns the count of rows appended.
    """
    if not new_npis:
        print("[3/5] No new NPIs to append.")
        return 0

    print(f"[3/5] Appending {len(new_npis):,} new NPI records to master...")
    new_rows = pd.DataFrame(
        [weekly_dict[npi] for npi in new_npis]
    )

    # Align columns to master schema (fill missing cols with NaN)
    new_rows = new_rows.reindex(columns=master_columns)

    with open(tmp_path, "a", newline="", encoding="utf-8") as out_fh:
        new_rows.to_csv(out_fh, index=False, header=False)

    print(f"    Appended {len(new_rows):,} new rows.")
    return len(new_rows)


def replace_master(master_path: str, tmp_path: str, output_path: str | None):
    """
    Move the temp file to either the master path (in-place) or output_path.
    """
    destination = output_path if output_path else master_path
    print(f"[4/5] Replacing master file at: {destination}")
    shutil.move(tmp_path, destination)
    print(f"    Master file updated.")
    return destination


def regenerate_state_files(master_path: str,
                           states_dir: str,
                           affected_states: set,
                           cols: list | None = None):
    """
    Re-generate per-state CSVs for only the affected states.
    This avoids processing all 50 states when only a handful changed.
    """
    if not affected_states:
        print("[5/5] No state files need regenerating.")
        return

    valid_states = affected_states - TERRITORIES_TO_EXCLUDE - {None, "nan", "NaN"}
    if not valid_states:
        print("[5/5] All affected states are territories – skipping.")
        return

    print(f"[5/5] Regenerating state files for: {sorted(valid_states)}")
    states_path = Path(states_dir)
    states_path.mkdir(parents=True, exist_ok=True)

    # Build a dict: state -> filtered DataFrame
    state_data: dict[str, pd.DataFrame] = {s: pd.DataFrame() for s in valid_states}

    reader = pd.read_csv(
        master_path,
        chunksize=CHUNKSIZE,
        low_memory=False,
        dtype={NPI_COL: str},
        usecols=cols,
    )

    rows_read = 0
    for chunk in reader:
        for state in valid_states:
            filtered = chunk[chunk[STATE_COL] == state]
            if not filtered.empty:
                state_data[state] = pd.concat([state_data[state], filtered])
        rows_read += len(chunk)
        if rows_read % 1_000_000 == 0:
            print(f"    ... read {rows_read:,} rows for state regeneration")

    for state, df in state_data.items():
        if df.empty:
            print(f"    WARNING: No rows found for state '{state}' – skipping.")
            continue
        df = df.dropna(axis=1, how="all").reset_index(drop=True)
        out_file = states_path / f"{state} NPPES Extract.csv"
        df.to_csv(out_file, index=False)
        print(f"    Saved {len(df):,} rows → {out_file.name}")

    print("    State file regeneration complete.")


def get_affected_states(weekly_dict: dict) -> set:
    """Extract the unique states from weekly records."""
    states: set = set()
    for row in weekly_dict.values():
        if STATE_COL in row.index:
            val = row[STATE_COL]
            if pd.notna(val):
                states.add(str(val).strip())
    return states


def get_master_columns(master_path: str) -> list:
    """Read only the header row of the master file to get column names."""
    header_df = pd.read_csv(master_path, nrows=0)
    return list(header_df.columns)


def load_data_dict_cols(dict_path: str) -> list | None:
    """
    If a data dictionary CSV is provided, return the column subset to use.
    Returns None if the file is not provided or cannot be read.
    """
    if not dict_path or not os.path.exists(dict_path):
        return None
    try:
        dd = pd.read_csv(dict_path)
        cols = dd["Column_Name"].tolist()
        print(f"    Using {len(cols)} columns from data dictionary.")
        return cols
    except Exception as exc:
        print(f"    WARNING: Could not load data dict ({exc}). Using all columns.")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Merge NPPES weekly CSV into the master 10GB CSV."
    )
    parser.add_argument("--master", required=True,
                        help="Path to the master NPPES CSV file (~10GB).")
    parser.add_argument("--weekly", required=True,
                        help="Path to the weekly NPPES CSV file (~30K rows).")
    parser.add_argument("--output", default=None,
                        help="Output path. Defaults to in-place update of --master.")
    parser.add_argument("--states-dir", default=None,
                        help="Directory containing per-state CSVs to regenerate.")
    parser.add_argument("--data-dict", default=None,
                        help="Path to main_nppes_data_dict.csv for column filtering.")
    args = parser.parse_args()

    start = time.time()
    print("=" * 60)
    print("NPPES Weekly Update Script")
    print("=" * 60)

    # Optional: restrict to data-dict columns
    cols = load_data_dict_cols(args.data_dict)

    # Step 1 – Load weekly file
    weekly_df, weekly_dict = load_weekly(args.weekly)

    # Determine which states are affected (for optional state regen)
    affected_states = get_affected_states(weekly_dict)
    print(f"    Affected states: {sorted(affected_states)}")

    # Get master column schema before streaming
    master_columns = get_master_columns(args.master)

    # Step 2 – Stream master + update
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".csv", dir=os.path.dirname(args.master))
    os.close(tmp_fd)

    try:
        updated_npis, new_npis = merge_master(
            args.master, weekly_dict, tmp_path, cols=cols
        )

        # Step 3 – Append truly new records
        append_new_records(tmp_path, weekly_dict, new_npis, master_columns)

        # Step 4 – Replace master
        final_path = replace_master(args.master, tmp_path, args.output)

        # Step 5 – Optionally regenerate affected state files
        if args.states_dir:
            regenerate_state_files(
                final_path, args.states_dir, affected_states, cols=cols
            )

    except Exception as exc:
        # Clean up temp file on failure
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        print(f"\nERROR: {exc}")
        sys.exit(1)

    elapsed = time.time() - start
    print("=" * 60)
    print(f"Update complete in {elapsed:.1f}s")
    print(f"  Updated : {len(updated_npis):,} existing records")
    print(f"  Inserted: {len(new_npis):,} new records")
    print("=" * 60)


if __name__ == "__main__":
    main()
