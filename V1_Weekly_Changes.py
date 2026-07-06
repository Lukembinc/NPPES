"""
NPPES Weekly Changes V1 — reconcile weekly npidata_pfile diffs into the
per-state Dental extracts
=========================================================================

CMS publishes a *weekly* incremental file (``npidata_pfile_<start>-<end>.csv``)
inside each ``NPPES_Data_Dissemination_<MMDDYY>_<MMDDYY>_Weekly_V2`` folder.
Each weekly file contains one row per NPI that was **added or changed** during
that week, in the *raw* 330-column NPPES layout (the same layout as the monthly
cumulative ``npidata_pfile``, plus deactivation / reactivation columns).

The per-state Dental extracts under

    <monthly_folder>/Dental/<STATE> NPPES Dental.csv

are produced by ``V3_run_monthly_split.py`` from the monthly cumulative file and
are in the cleaned, renamed **V3 dental schema**. Those CSVs are what
``V3_convert_to_parquet.py`` turns into the parquet the Streamlit dashboard
reads.

This script keeps those extracts current *between* monthly refreshes: it takes
each weekly file, runs it through the **exact same V3 cleaning pipeline**
(ZIP/phone trimming, county join, taxonomy enrichment, dental selection, and the
V3 rename/reorder), and reconciles the result into the extracts.

Reconciliation model (see "Design decisions" below)
---------------------------------------------------
For every NPI that appears in the weekly files (the "touched" set) we take the
**latest** weekly record that mentions it, then:

  * DEACTIVATED  (NPI Deactivation Date set, no later reactivation)
        -> remove the NPI from the extracts entirely.
  * ACTIVE but NO LONGER DENTAL (no taxonomy grouping == "Dental Providers")
        -> remove the NPI from the extracts.
  * ACTIVE and DENTAL
        -> upsert: the NPI's row(s) are written into the extract for its
           *current* practice-location state, replacing any prior copy — which
           may live in a different state file if the provider moved.

The mechanism is uniform and order-independent within a run: we (1) drop every
touched NPI from every state file, then (2) insert the current active-dental
rows into their correct state file. Because a provider can legitimately appear
in an extract more than once (the V3 pipeline concatenates one row per dental
taxonomy grouping without de-duping), removal is keyed on NPI and re-insertion
reproduces the same multiplicity via ``select_dental``.

Design decisions (confirmed with the user)
-------------------------------------------
  1. Output target : update the extract CSVs **in place**, after taking a
     timestamped backup of the whole Dental folder (``--no-backup`` to skip).
  2. Deactivations : deactivated NPIs are **removed** from the extracts.
  3. Scope         : **full reconcile** — upsert dental rows, remove NPIs that
     are no longer dental, and move rows to the correct state file when a
     provider's practice state changes.
  4. Weekly scope  : **auto-detect all** ``..._Weekly_V2`` folders and apply
     them oldest -> newest so the newest record for each NPI wins.

Reuse of V3 logic
-----------------
The cleaning is not re-implemented here; this script imports
``V3_run_monthly_split`` and calls its ``clean_state_df``, ``select_dental`` and
``clean_rename_cols`` functions (and its reference-data loaders) so the weekly
rows are cleaned *identically* to the monthly rows. If the V3 schema changes,
this script follows automatically.

Usage
-----
    python V1_Weekly_Changes.py                 # auto-detect newest month + all weeklies
    python V1_Weekly_Changes.py --dry-run       # report the plan, change nothing
    python V1_Weekly_Changes.py --no-backup     # skip the Dental-folder backup
    python V1_Weekly_Changes.py --weekly-folder /path/to/one_Weekly_V2   # just one week
    python V1_Weekly_Changes.py --monthly-folder /path/...   # target a specific month
    # (test/advanced) point reference files + extracts somewhere else:
    python V1_Weekly_Changes.py --nppes-root /tmp/x --dental-dir /tmp/x/Dental

After a run, re-run ``V3_convert_to_parquet.py`` to refresh the dashboard parquet.

Tested with: pandas >= 1.5, Python >= 3.9
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Configuration — defaults that match the monthly pipeline
# ---------------------------------------------------------------------------
NPPES_ROOT = Path("/Users/lukebincarousky/Downloads/NPPES")

MONTH_NAMES = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}

# Monthly folder: NPPES_Data_Dissemination_<MonthName>_<Year>_V2 (not the weekly ones)
MONTHLY_FOLDER_RE = re.compile(
    r"^NPPES_Data_Dissemination_(?P<month>[A-Za-z]+)_(?P<year>\d{4})_V2$"
)

# Weekly folder: NPPES_Data_Dissemination_<MMDDYY>_<MMDDYY>_Weekly_V2
WEEKLY_FOLDER_RE = re.compile(
    r"^NPPES_Data_Dissemination_(?P<start>\d{6})_(?P<end>\d{6})_Weekly_V2$"
)

# Extract filename: "<STATE> NPPES Dental.csv"
DENTAL_FILE_RE = re.compile(r"^(?P<state>[A-Z]{2}) NPPES Dental\.csv$")

# Raw NPPES columns needed for deactivation logic that are NOT in the V3 data
# dictionary (the monthly pipeline gets deactivations from a separate report).
DEACT_DATE_COL = "NPI Deactivation Date"
REACT_DATE_COL = "NPI Reactivation Date"

# The V3 dental schema's identity + routing columns (post-rename).
NPI_COL = "NPI"
DENTAL_STATE_COL = "Practice Address State"  # renamed practice-location state


# ---------------------------------------------------------------------------
# Import the V3 monthly pipeline so cleaning is identical
# ---------------------------------------------------------------------------
def _load_v3_module(this_dir: Path):
    """Import V3_run_monthly_split.py that sits next to this file.

    Importing is side-effect free (its argparse/main is guarded by
    ``if __name__ == '__main__'``), so this only brings in the constants and
    the cleaning/loader functions we reuse.
    """
    v3_path = this_dir / "V3_run_monthly_split.py"
    if not v3_path.exists():
        raise FileNotFoundError(
            f"Could not find V3_run_monthly_split.py next to this script "
            f"(looked in {this_dir}). It is required for the cleaning logic."
        )
    spec = importlib.util.spec_from_file_location("V3_run_monthly_split", v3_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["V3_run_monthly_split"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------
def find_latest_monthly_folder(root: Path) -> Path:
    candidates: list[tuple[datetime, Path]] = []
    for d in os.listdir(root):
        if not os.path.isdir(root / d):
            continue
        m = MONTHLY_FOLDER_RE.match(d)
        if not m:
            continue
        month_num = MONTH_NAMES.get(m.group("month"))
        if month_num is None:
            continue
        candidates.append((datetime(int(m.group("year")), month_num, 1), root / d))
    if not candidates:
        raise FileNotFoundError(
            f"No monthly NPPES_Data_Dissemination_<Month>_<Year>_V2 folders found under {root}."
        )
    candidates.sort(key=lambda t: t[0])
    return candidates[-1][1]


def _parse_mmddyy(s: str) -> datetime:
    return datetime.strptime(s, "%m%d%y")


def find_weekly_folders(root: Path) -> list[tuple[datetime, Path]]:
    """Return (end_date, folder) for every weekly folder under root, sorted oldest->newest."""
    out: list[tuple[datetime, Path]] = []
    for d in os.listdir(root):
        if not os.path.isdir(root / d):
            continue
        m = WEEKLY_FOLDER_RE.match(d)
        if not m:
            continue
        try:
            end = _parse_mmddyy(m.group("end"))
        except ValueError:
            continue
        out.append((end, root / d))
    out.sort(key=lambda t: t[0])
    return out


def find_weekly_pfile(weekly_folder: Path) -> Path:
    matches = [
        p for p in weekly_folder.glob("npidata_pfile_*.csv")
        if not p.name.endswith("_fileheader.csv")
    ]
    if not matches:
        raise FileNotFoundError(
            f"No npidata_pfile_*.csv (excluding _fileheader.csv) found in {weekly_folder}."
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"Ambiguous npidata_pfile candidates in {weekly_folder}: {[p.name for p in matches]}"
        )
    return matches[0]


def discover_state_files(dental_dir: Path) -> dict[str, Path]:
    """Map STATE -> extract path for every '<ST> NPPES Dental.csv' in the folder."""
    mapping: dict[str, Path] = {}
    for p in sorted(dental_dir.glob("*.csv")):
        m = DENTAL_FILE_RE.match(p.name)
        if m:
            mapping[m.group("state")] = p
    return mapping


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _read_extract(path: Path) -> pd.DataFrame:
    """Read an existing extract as all-string, preserving blanks and leading zeros."""
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False)


def _value_to_str(v) -> str:
    """Render a value the way a clean CSV cell should look (no 'nan', no '1234.0')."""
    if v is None:
        return ""
    if isinstance(v, float):
        if np.isnan(v):
            return ""
        if v.is_integer():
            return str(int(v))
        return str(v)
    s = str(v)
    return "" if s.lower() == "nan" else s


def _frame_to_str(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce an entire frame to clean strings so it round-trips like the extracts."""
    return df.applymap(_value_to_str)


def _is_deactivated(deact: str, react: str) -> bool:
    """A row is deactivated if it has a deactivation date and no *later* reactivation."""
    deact = (deact or "").strip()
    react = (react or "").strip()
    if not deact:
        return False
    if not react:
        return True
    # Both present: reactivation wins only if it's on/after the deactivation date.
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            d = datetime.strptime(deact, fmt)
            break
        except ValueError:
            d = None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            r = datetime.strptime(react, fmt)
            break
        except ValueError:
            r = None
    if d is None or r is None:
        # Can't parse -> treat presence of a reactivation date as "active".
        return False
    return r < d


# ---------------------------------------------------------------------------
# Core reconciliation
# ---------------------------------------------------------------------------
def clean_weekly_to_dental(
    weekly_pfile: Path,
    cols: list[str],
    msa_geo: pd.DataFrame,
    spec_dict: pd.DataFrame,
    v3,
    chunksize: int = 100_000,
) -> tuple[pd.DataFrame, set[str]]:
    """Clean one weekly file and return (active_dental_df, touched_npis).

    * active_dental_df : V3-schema dental rows for providers that are active
      (not deactivated) in this weekly file. May contain an NPI more than once
      (one row per dental taxonomy grouping), matching the monthly pipeline.
    * touched_npis     : every NPI present in the weekly file (active OR
      deactivated, dental OR not) — these are the rows whose prior extract
      copies are now stale and must be dropped before re-insertion.
    """
    # Only request columns that actually exist in this file's header.
    header = pd.read_csv(weekly_pfile, nrows=0)
    file_cols = set(header.columns)
    read_cols = [c for c in cols if c in file_cols]
    for extra in (DEACT_DATE_COL, REACT_DATE_COL):
        if extra in file_cols and extra not in read_cols:
            read_cols.append(extra)

    # An empty "deactivated report" frame: passed to clean_state_df so it still
    # adds the 'Deactivation Date' schema column, but leaves it blank for these
    # (active) providers. Deactivated providers are handled separately and removed.
    empty_deact = pd.DataFrame({"NPI": pd.Series(dtype=str),
                                "Deactivation Date": pd.Series(dtype=object)})

    touched: set[str] = set()
    active_frames: list[pd.DataFrame] = []

    for chunk in pd.read_csv(
        weekly_pfile, usecols=read_cols, dtype={NPI_COL: str},
        low_memory=False, chunksize=chunksize,
    ):
        chunk[NPI_COL] = chunk[NPI_COL].astype(str)
        touched.update(chunk[NPI_COL].tolist())

        deact = chunk.get(DEACT_DATE_COL, pd.Series([""] * len(chunk), index=chunk.index))
        react = chunk.get(REACT_DATE_COL, pd.Series([""] * len(chunk), index=chunk.index))
        deactivated_mask = [
            _is_deactivated(d, r) for d, r in zip(deact.astype(str), react.astype(str))
        ]
        active = chunk.loc[[not x for x in deactivated_mask]]
        if active.empty:
            continue

        # Feed only the V3 data-dict columns into the shared cleaning function.
        active_dd = active[[c for c in cols if c in active.columns]].copy()
        cleaned = v3.clean_state_df(active_dd, msa_geo, spec_dict, empty_deact)
        dental = v3.select_dental(cleaned)
        if dental.empty:
            continue
        dental = v3.clean_rename_cols(dental)
        dental[NPI_COL] = dental[NPI_COL].astype(str)
        active_frames.append(dental)

    if active_frames:
        active_dental = pd.concat(active_frames, ignore_index=True)
    else:
        # Empty frame with the right schema so downstream code is uniform.
        active_dental = v3.clean_rename_cols(
            v3.select_dental(pd.DataFrame(columns=cols))
        )
    return active_dental, touched


def reconcile(
    weekly_pfiles: list[Path],
    dental_dir: Path,
    cols: list[str],
    msa_geo: pd.DataFrame,
    spec_dict: pd.DataFrame,
    v3,
    dry_run: bool = False,
    chunksize: int = 100_000,
) -> pd.DataFrame:
    """Apply weekly files (already oldest->newest) to the extracts in dental_dir.

    Returns a per-NPI action report DataFrame.
    """
    state_files = discover_state_files(dental_dir)
    if not state_files:
        raise FileNotFoundError(f"No '<ST> NPPES Dental.csv' files found in {dental_dir}.")
    valid_states = set(state_files)

    # 1) Fold all weeklies into: latest active-dental rows per NPI + global touched set.
    latest_rows: dict[str, pd.DataFrame] = {}   # NPI -> its dental row(s) from the newest week
    touched_all: set[str] = set()
    for pfile in weekly_pfiles:
        print(f"  reading {pfile.parent.name}/{pfile.name} ...", flush=True)
        active_dental, touched = clean_weekly_to_dental(
            pfile, cols, msa_geo, spec_dict, v3, chunksize=chunksize
        )
        touched_all.update(touched)
        # Newer week supersedes older: clear prior rows for every NPI this week touched,
        # then set rows for the ones that are active-dental this week.
        for npi in touched:
            latest_rows.pop(npi, None)
        if not active_dental.empty:
            for npi, grp in active_dental.groupby(NPI_COL, sort=False):
                latest_rows[npi] = grp

    final_rows = (
        pd.concat(latest_rows.values(), ignore_index=True)
        if latest_rows else active_dental.iloc[0:0]
    )
    if not final_rows.empty:
        final_rows[DENTAL_STATE_COL] = final_rows[DENTAL_STATE_COL].astype(str)

    # NPIs that end up active-dental, grouped by their CURRENT practice state.
    final_by_state: dict[str, pd.DataFrame] = {}
    skipped_state_rows = 0
    if not final_rows.empty:
        for state, grp in final_rows.groupby(DENTAL_STATE_COL, sort=False):
            if state in valid_states:
                final_by_state[state] = grp
            else:
                skipped_state_rows += len(grp)

    # 2) Build "before" location map for reporting: NPI -> set(states it was in).
    before_states: dict[str, set[str]] = {}
    extract_frames: dict[str, pd.DataFrame] = {}
    for state, path in state_files.items():
        df = _read_extract(path)
        extract_frames[state] = df
        if NPI_COL in df.columns:
            present = set(df.loc[df[NPI_COL].isin(touched_all), NPI_COL].tolist())
            for npi in present:
                before_states.setdefault(npi, set()).add(state)

    # 3) Rewrite each state file: drop touched NPIs, append this state's final rows.
    summary = {"files_changed": 0, "rows_removed": 0, "rows_added": 0, "skipped_state_rows": skipped_state_rows}
    for state, path in state_files.items():
        df = extract_frames[state]
        target_cols = list(df.columns)

        before_n = len(df)
        kept = df[~df[NPI_COL].isin(touched_all)] if NPI_COL in df.columns else df
        removed_n = before_n - len(kept)

        add_df = final_by_state.get(state)
        if add_df is not None and not add_df.empty:
            add_out = _frame_to_str(add_df.reindex(columns=target_cols))
            new_df = pd.concat([kept, add_out], ignore_index=True)
            added_n = len(add_out)
        else:
            new_df = kept.reset_index(drop=True)
            added_n = 0

        if removed_n == 0 and added_n == 0:
            continue  # untouched file

        summary["files_changed"] += 1
        summary["rows_removed"] += removed_n
        summary["rows_added"] += added_n
        if not dry_run:
            new_df.to_csv(path, index=False)
        print(f"    {state}: -{removed_n} / +{added_n} rows"
              + ("  (dry-run)" if dry_run else ""))

    # 4) Build per-NPI action report.
    after_states: dict[str, set[str]] = {}
    if not final_rows.empty:
        for npi, grp in final_rows.groupby(NPI_COL, sort=False):
            after_states[npi] = set(
                s for s in grp[DENTAL_STATE_COL].tolist() if s in valid_states
            )

    report_rows = []
    for npi in sorted(touched_all):
        was = before_states.get(npi, set())
        now = after_states.get(npi, set())
        if now and not was:
            action = "added"
        elif now and was and now != was:
            action = "moved_state"
        elif now and was:
            action = "updated"
        elif was and not now:
            action = "removed"   # deactivated or no longer dental
        else:
            action = "no_change"  # touched but never in / entering the dental extracts
        report_rows.append({
            "NPI": npi,
            "action": action,
            "before_states": ";".join(sorted(was)),
            "after_states": ";".join(sorted(now)),
        })
    report = pd.DataFrame(report_rows, columns=["NPI", "action", "before_states", "after_states"])

    print("\nSummary:")
    print(f"  touched NPIs:        {len(touched_all):,}")
    print(f"  files changed:       {summary['files_changed']}")
    print(f"  rows removed:        {summary['rows_removed']:,}")
    print(f"  rows added:          {summary['rows_added']:,}")
    if summary["skipped_state_rows"]:
        print(f"  rows skipped (state not in extract set): {summary['skipped_state_rows']:,}")
    counts = report["action"].value_counts().to_dict()
    print(f"  actions:             {counts}")
    return report


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------
def backup_dental_dir(dental_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = dental_dir.parent / f"{dental_dir.name}_backup_{stamp}"
    shutil.copytree(dental_dir, dest)
    return dest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--nppes-root", type=Path, default=NPPES_ROOT,
                        help="Root that holds the monthly/weekly folders and the NPPES repo.")
    parser.add_argument("--monthly-folder", type=Path, default=None,
                        help="Monthly folder whose Dental/ extracts are updated (default: newest).")
    parser.add_argument("--dental-dir", type=Path, default=None,
                        help="Directory of '<ST> NPPES Dental.csv' extracts (default: <monthly>/Dental).")
    parser.add_argument("--weekly-root", type=Path, default=None,
                        help="Where the '..._Weekly_V2' folders live (default: --nppes-root).")
    parser.add_argument("--weekly-folder", type=Path, default=None,
                        help="Process only this one weekly folder instead of auto-detecting all.")
    parser.add_argument("--no-backup", action="store_true",
                        help="Skip backing up the Dental folder before writing.")
    parser.add_argument("--keep-backup", action="store_true",
                        help="Keep the Dental-folder backup after a successful run "
                             "(by default it is deleted once reconciliation completes).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report the plan and counts without writing any files.")
    parser.add_argument("--report", type=Path, default=None,
                        help="Where to write the per-NPI action report CSV "
                             "(default: alongside the Dental folder).")
    parser.add_argument("--chunksize", type=int, default=100_000,
                        help="Rows per pandas chunk when reading weekly files.")
    args = parser.parse_args()

    this_dir = Path(__file__).resolve().parent
    v3 = _load_v3_module(this_dir)

    nppes_root = args.nppes_root
    weekly_root = args.weekly_root or nppes_root

    # Reference files (same locations the V3 pipeline uses, rooted at --nppes-root).
    data_dict_path = nppes_root / "NPPES" / "Dictionaries" / "main_nppes_data_dict.csv"
    geo_msa_workbook = nppes_root / "NPPES" / "Geographic Data" / "US State County City Zip MSA workbook.xlsx"
    taxonomy_workbook = nppes_root / "NPPES" / "Dictionaries" / "nucc_taxonomy_250.xlsx"
    for p in (data_dict_path, geo_msa_workbook, taxonomy_workbook):
        if not p.exists():
            print(f"ERROR: missing reference file {p}", file=sys.stderr)
            return 1

    # Locate the extracts.
    monthly_folder = args.monthly_folder or find_latest_monthly_folder(nppes_root)
    dental_dir = args.dental_dir or (monthly_folder / "Dental")
    if not dental_dir.is_dir():
        print(f"ERROR: Dental directory not found: {dental_dir}", file=sys.stderr)
        return 1

    # Locate the weekly file(s).
    if args.weekly_folder:
        weekly_pfiles = [find_weekly_pfile(args.weekly_folder)]
    else:
        weekly_pfiles = [find_weekly_pfile(f) for _, f in find_weekly_folders(weekly_root)]
    if not weekly_pfiles:
        print(f"ERROR: no weekly folders found under {weekly_root}", file=sys.stderr)
        return 1

    print(f"NPPES root:        {nppes_root}")
    print(f"Monthly folder:    {monthly_folder}")
    print(f"Dental extracts:   {dental_dir}")
    print(f"Weekly files ({len(weekly_pfiles)}), oldest -> newest:")
    for p in weekly_pfiles:
        print(f"    {p.parent.name}/{p.name}")

    # Load reference data once (reusing V3's loaders).
    cols = v3.load_columns(data_dict_path)
    msa_geo = v3.load_msa_zip_to_county(geo_msa_workbook)
    spec_dict = v3.load_taxonomy_lookup(taxonomy_workbook)

    # Backup before mutating.
    if not args.dry_run and not args.no_backup:
        dest = backup_dental_dir(dental_dir)
        print(f"Backed up Dental folder -> {dest}")
    elif args.dry_run:
        print("--dry-run: no backup, no files will be written.")

    print("\nReconciling...")
    report = reconcile(
        weekly_pfiles=weekly_pfiles,
        dental_dir=dental_dir,
        cols=cols,
        msa_geo=msa_geo,
        spec_dict=spec_dict,
        v3=v3,
        dry_run=args.dry_run,
        chunksize=args.chunksize,
    )

    report_path = args.report or (
        dental_dir.parent / f"V1_Weekly_Changes_report_{datetime.now():%Y%m%d_%H%M%S}.csv"
    )
    if not args.dry_run:
        report.to_csv(report_path, index=False)
        print(f"\nPer-NPI report written to: {report_path}")
    else:
        print(f"\n--dry-run: report not written (would be {report_path}).")

    print("\nDone. Re-run V3_convert_to_parquet.py to refresh the dashboard parquet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
