"""
NPPES Provider Lookup — Streamlit prototype (v3)
=================================================

What's new in this revision:
  * Updated for the V3 dental schema produced by V3_run_monthly_split.py.
    The dental files now have renamed/reordered columns; in particular:
      - "Provider Business Practice Location Address City Name"   -> "Practice Address City"
      - "Provider Business Practice Location Address State Name"  -> "Practice Address State"
      - "Provider Business Practice Location Address Postal Code" -> "Practice Address ZipCode"
      - "Spec_N" / "Grouping_N" / "Class_N" / "Code_N"            -> "Specialty N" / "Specialty Grouping N" / "Specialty Class N" / "Specialty Code N"
      - "County"                                                  -> "Practice Address County"
      - "Provider Enumeration Date"                               -> "Enumeration Date"
    The raw "Healthcare Provider Taxonomy Code_1..5" columns are dropped
    in the V3 dental output (already enriched into Specialty Code 1..5),
    so the old "taxonomy code fallback" filter is gone.
  * Top-of-page Dataset selector: "Full Data" vs "Dental"
      - Full Data  -> reads <monthly_folder>/Full Data/<STATE> NPPES Extract.csv  (V2 schema; Full Data CSV is unchanged in V3)
      - Dental     -> reads <monthly_folder>/Dental/<STATE> NPPES Dental.csv      (V3 schema)
  * Sidebar filters: city, ZIP (prefix match), name search,
    human-readable Specialty (Specialty 1..5).
  * If the V3 enrichment columns are present (Specialty 1, Specialty
    Grouping 1, Practice Address County, Deactivation Date, ...), they're
    surfaced so you can see the cleaned data.
  * Per-state files may be either Parquet (preferred — what the deployed
    Streamlit Cloud build ships) or CSV (local dev). The loader tries
    Parquet first and falls back to CSV automatically.

Note: this dashboard is fully V3 now. If you have older V2 parquet files
in the data/ folder, the City/ZIP columns won't be found and the page
will error. Re-run V3_convert_to_parquet.py to refresh them.

Data root resolution (in priority order):
  1. NPPES_ROOT env var, if set (e.g. local dev pointing at
     /Users/lukebincarousky/Downloads/NPPES).
  2. The repo's bundled `data/` folder (sibling of `dashboard/`). This is
     what Streamlit Cloud sees — it contains a Dental-only Parquet build.

Run locally (against your full /Users/.../Downloads/NPPES tree):

    cd "/Users/lukebincarousky/Downloads/NPPES/NPPES/dashboard"
    NPPES_ROOT="/Users/lukebincarousky/Downloads/NPPES" streamlit run app.py

Run locally (against the repo's bundled Dental Parquet data — same as cloud):

    cd "/Users/lukebincarousky/Downloads/NPPES/NPPES/dashboard"
    streamlit run app.py

Why the maxMessageSize bump in .streamlit/config.toml? Even with filters,
if you toggle "Show all matching" on a big state with no filters applied,
the WebSocket payload can exceed the default 200 MB cap. 1000 MB gives
plenty of headroom for prototyping. (This limitation goes away entirely
in Phase 4 when the FastAPI backend handles pagination.)

Known follow-ups (not in this iteration):
  * Click-into-NPI profile page.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Data root resolution. Env var wins so a local dev session can keep pointing
# at the full ~/Downloads/NPPES tree (with Full Data CSVs) without code changes.
# Default is the repo's bundled `data/` folder — this is what ships in the
# GitHub repo and what Streamlit Cloud uses (Dental-only, Parquet).
_REPO_DATA = Path(__file__).resolve().parent.parent / "data"
NPPES_ROOT = Path(os.environ.get("NPPES_ROOT") or _REPO_DATA)

MONTH_NAMES = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}
MONTHLY_FOLDER_RE = re.compile(
    r"^NPPES_Data_Dissemination_(?P<month>[A-Za-z]+)_(?P<year>\d{4})_V2$"
)

# Two recognized datasets, each living in its own subfolder with its own
# filename suffix. The Dental subset uses the V3 renamed schema; the Full
# Data CSVs still use the raw V2 column names (V3 left Full Data
# untouched). The dashboard is dental-by-default, so the V3 column names
# below drive the UI.
DATASETS: dict[str, dict[str, str]] = {
    "Full Data": {"subdir": "Full Data", "suffix": "NPPES Extract"},
    "Dental":    {"subdir": "Dental",    "suffix": "NPPES Dental"},
}

# Column name constants — V3 dental schema.
# Identity columns kept their original names in the rename step.
COL_NPI = "NPI"
COL_LAST = "Provider Last Name (Legal Name)"
COL_FIRST = "Provider First Name"
COL_ORG = "Provider Organization Name (Legal Business Name)"
# Practice address columns are renamed in V3.
COL_CITY = "Practice Address City"
COL_STATE = "Practice Address State"
COL_ZIP = "Practice Address ZipCode"

# Specialty columns (human-readable Display Name from the NUCC join).
SPEC_COLS = [f"Specialty {i}" for i in range(1, 6)]
# Paired Grouping columns at the same index — used to restrict the
# Specialty filter options to dental roles only.
GROUPING_COLS = [f"Specialty Grouping {i}" for i in range(1, 6)]

# V3 cleaning adds these on top of the base NPPES columns. Listed for
# documentation and so a future "show only enriched" toggle can use them.
V3_ENRICHED_COLS = [
    "Practice Address County",
    *GROUPING_COLS,
    *[f"Specialty Class {i}" for i in range(1, 6)],
    *SPEC_COLS,
    "Deactivation Date",
]

DEFAULT_PREVIEW_ROWS = 1000


# ---------------------------------------------------------------------------
# Data discovery
# ---------------------------------------------------------------------------
@st.cache_data
def find_latest_monthly_folder(root: Path) -> tuple[Path, str]:
    """Return (newest monthly folder path, label like 'May 2026').

    Picks the newest folder that has at least one recognized dataset
    subfolder (Full Data/ or Dental/). If none is found, falls back to the
    newest monthly folder regardless — the UI will then show a useful error.
    """
    monthly: list[tuple[datetime, Path, str]] = []
    monthly_with_v2: list[tuple[datetime, Path, str]] = []
    for dirpath, dirnames, _ in os.walk(root):
        for d in dirnames:
            m = MONTHLY_FOLDER_RE.match(d)
            if not m:
                continue
            month_num = MONTH_NAMES.get(m.group("month"))
            if month_num is None:
                continue
            folder = Path(dirpath) / d
            label = f"{m.group('month')} {m.group('year')}"
            key = datetime(int(m.group("year")), month_num, 1)
            monthly.append((key, folder, label))
            if any((folder / ds["subdir"]).exists() for ds in DATASETS.values()):
                monthly_with_v2.append((key, folder, label))

    chosen = monthly_with_v2 or monthly
    if not chosen:
        raise FileNotFoundError(
            f"No monthly NPPES_Data_Dissemination_<Month>_<Year>_V2 folders "
            f"found under {root}."
        )
    chosen.sort(key=lambda t: t[0])
    _, path, label = chosen[-1]
    return path, label


@st.cache_data
def list_available_datasets(monthly_folder_str: str) -> list[str]:
    """Return dataset names ('Full Data', 'Dental') whose subfolders exist."""
    monthly_folder = Path(monthly_folder_str)
    return [
        name for name, meta in DATASETS.items()
        if (monthly_folder / meta["subdir"]).exists()
    ]


@st.cache_data
def list_states(dataset_dir_str: str, suffix: str) -> list[str]:
    """List state codes inferred from filenames in a dataset subfolder.

    Looks for both '<STATE> <suffix>.parquet' (preferred) and
    '<STATE> <suffix>.csv' (local-dev fallback).
    """
    dataset_dir = Path(dataset_dir_str)
    out: set[str] = set()
    for ext in (".parquet", ".csv"):
        for p in dataset_dir.glob(f"* {suffix}{ext}"):
            # Filename is '<STATE> <suffix>.<ext>'; first token is the 2-letter state.
            first = p.name.split(" ")[0]
            if len(first) == 2 and first.isalpha():
                out.add(first)
    return sorted(out)


@st.cache_data
def load_state_csv(dataset_dir_str: str, suffix: str, state: str) -> pd.DataFrame:
    """Load and lightly normalize one state's per-provider file.

    Tries Parquet first (what the repo ships for the deployed build), then
    falls back to CSV (the local /Users/.../Downloads/NPPES tree).
    Cached per (dataset_dir, suffix, state) combination, so flipping
    Full Data <-> Dental for the same state doesn't trash the prior cache.
    """
    dataset_dir = Path(dataset_dir_str)
    parquet_path = dataset_dir / f"{state} {suffix}.parquet"
    csv_path = dataset_dir / f"{state} {suffix}.csv"

    if parquet_path.exists():
        # Parquet preserves string dtype; no low_memory/dtype kwargs needed.
        df = pd.read_parquet(parquet_path, engine="pyarrow")
    elif csv_path.exists():
        # Read everything as strings to avoid float coercion of ZIPs/phones/NPIs.
        df = pd.read_csv(csv_path, low_memory=False, dtype=str)
    else:
        raise FileNotFoundError(
            f"Neither {parquet_path.name} nor {csv_path.name} found in {dataset_dir}."
        )

    if COL_NPI in df.columns:
        df[COL_NPI] = df[COL_NPI].fillna("").astype(str).str.replace(r"\.0$", "", regex=True)

    if COL_ZIP in df.columns:
        df[COL_ZIP] = df[COL_ZIP].fillna("").astype(str).str.replace(r"\.0$", "", regex=True)

    return df


@st.cache_data
def unique_cities(df_index_key: str, cities_tuple: tuple[str, ...]) -> list[str]:
    return sorted({c for c in cities_tuple if c})


@st.cache_data
def unique_specialties(df_index_key: str, specs_tuple: tuple[str, ...]) -> list[str]:
    """Sorted unique human-readable specialties from the Specialty 1..5 columns."""
    return sorted({s for s in specs_tuple if s})


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------
def apply_filters(
    df: pd.DataFrame,
    city: str | None,
    zip_prefix: str,
    name_query: str,
    specialties: list[str],
) -> pd.DataFrame:
    """Apply sidebar filters to the loaded state DataFrame."""
    out = df

    if city:
        out = out[out[COL_CITY] == city]

    if zip_prefix:
        out = out[out[COL_ZIP].str.startswith(zip_prefix)]

    if name_query:
        q = name_query.strip().lower()
        cols_to_search = [c for c in (COL_LAST, COL_FIRST, COL_ORG) if c in out.columns]
        mask = pd.Series(False, index=out.index)
        for c in cols_to_search:
            mask = mask | out[c].fillna("").astype(str).str.lower().str.contains(q, regex=False)
        out = out[mask]

    if specialties:
        # Match if ANY of Specialty 1..5 equals one of the selected specialties.
        spec_cols_present = [c for c in SPEC_COLS if c in out.columns]
        if spec_cols_present:
            mask = pd.Series(False, index=out.index)
            for c in spec_cols_present:
                mask = mask | out[c].isin(specialties)
            out = out[mask]

    return out


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.set_page_config(page_title="NPPES Dental Provider Lookup", layout="wide")
st.title("NPPES Dental Provider Lookup")

# --- Resolve the monthly data source ----------------------------------------
try:
    monthly_folder, month_label = find_latest_monthly_folder(NPPES_ROOT)
except FileNotFoundError as e:
    st.error(str(e))
    st.stop()

# --- Dataset is fixed to Dental --------------------------------------------
# The deployed build only ships the Dental Parquet subset, and the cloud
# version is intentionally dental-only. We keep DATASETS as a config block
# (rather than inlining the path) so a future "Full Data" mode is a
# one-line revival of the radio above.
dataset_choice = "Dental"
dataset_meta = DATASETS[dataset_choice]
dataset_dir = monthly_folder / dataset_meta["subdir"]
suffix = dataset_meta["suffix"]

if not dataset_dir.exists():
    st.error(
        f"Found monthly folder '{monthly_folder.name}' but no Dental/ subfolder "
        f"inside it. Run V3_run_monthly_split.py (and V3_convert_to_parquet.py "
        f"for the Parquet build) first."
    )
    st.stop()

# --- Sidebar: state + filters -----------------------------------------------
st.sidebar.header("Filters")

states = list_states(str(dataset_dir), suffix)
if not states:
    st.warning(f"No per-state CSVs found in {dataset_dir}.")
    st.stop()

state = st.sidebar.selectbox(
    "State",
    states,
    index=states.index("MA") if "MA" in states else 0,
)

with st.spinner(f"Loading {state}…"):
    df = load_state_csv(str(dataset_dir), suffix, state)

# City: dropdown of unique values within the chosen state
city_options = unique_cities(
    f"{dataset_choice}:{state}",
    tuple(df[COL_CITY].fillna("").tolist()) if COL_CITY in df.columns else (),
)
city = st.sidebar.selectbox(
    "City",
    options=["(any)"] + city_options,
    index=0,
)
city_filter = None if city == "(any)" else city

# ZIP: free-text input, used as prefix
zip_prefix = st.sidebar.text_input(
    "ZIP code (or prefix, e.g. '021' for Boston area)",
    value="",
).strip()

# Name search: free-text, matches last/first/org name
name_query = st.sidebar.text_input(
    "Name (provider or organization)",
    value="",
).strip()

# Specialty: multiselect of unique human-readable specialties (Specialty 1..5)
# from the cleaned V3 data. In the Dental dataset, many providers also have
# non-dental taxonomies in Specialty 2..5 (e.g. "Student, Health Care" or
# other physician roles). We restrict the option list to specs whose aligned
# Specialty Grouping N == "Dental Providers", so the dropdown surfaces only
# dental roles (Dentist, Dental Hygienist, Orthodontics, ...).
spec_cols_present = [c for c in SPEC_COLS if c in df.columns]
if spec_cols_present:
    dental_specs: list[str] = []
    # Iterate by index so we can pair Specialty N with Specialty Grouping N
    # without string-replace gymnastics. SPEC_COLS and GROUPING_COLS are
    # parallel lists.
    for spec_col, group_col in zip(SPEC_COLS, GROUPING_COLS):
        if spec_col not in df.columns:
            continue
        if group_col in df.columns:
            dental_mask = df[group_col] == "Dental Providers"
            dental_specs.extend(df.loc[dental_mask, spec_col].fillna("").tolist())
        else:
            # No Grouping column to gate on — fall back to all values from this slot.
            dental_specs.extend(df[spec_col].fillna("").tolist())
    specialty_options = unique_specialties(
        f"{dataset_choice}:{state}:dental_only", tuple(dental_specs)
    )
    specialties = st.sidebar.multiselect(
        "Specialty (any match)",
        options=specialty_options,
        default=[],
        help="Dental specialties only (Specialty 1..5 values where the "
             "matching Specialty Grouping N is 'Dental Providers'). Match "
             "is any-of across the five Specialty N columns.",
    )
else:
    # Should not happen with V3 dental files — they always have Specialty 1..5.
    # If we land here, the data folder probably still has V2 parquets in it;
    # surface a clear error rather than silently rendering an unfiltered table.
    st.warning(
        "No Specialty 1..5 columns found in this dataset. This dashboard "
        "expects the V3 dental schema — re-run V3_run_monthly_split.py and "
        "V3_convert_to_parquet.py to refresh the data folder."
    )
    specialties = []

# --- Apply filters ----------------------------------------------------------
filtered = apply_filters(df, city_filter, zip_prefix, name_query, specialties)

# --- Result summary + table -------------------------------------------------
total = len(df)
match = len(filtered)
st.markdown(
    f"**{match:,}** of **{total:,}** providers match in **{state}** "
    f"({dataset_choice})."
)

show_all = st.checkbox(
    f"Show all {match:,} matching rows (default: top {DEFAULT_PREVIEW_ROWS:,})",
    value=False,
)

display_df = filtered if show_all else filtered.head(DEFAULT_PREVIEW_ROWS)

if show_all and match > 50_000:
    st.info(
        f"Rendering all {match:,} rows. If the page hangs or errors out, that's the "
        f"WebSocket message-size limit; either narrow your filters or, in Phase 4, "
        f"the FastAPI backend will paginate this server-side."
    )

st.dataframe(display_df, use_container_width=True, hide_index=True)
