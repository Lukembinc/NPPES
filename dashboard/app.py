"""
NPPES Provider Lookup — Streamlit prototype (v2)
=================================================

What's new in this revision:
  * Top-of-page Dataset selector: "Full Data" vs "Dental"
      - Full Data  -> reads <monthly_folder>/Full Data/<STATE> NPPES Extract.csv
      - Dental     -> reads <monthly_folder>/Dental/<STATE> NPPES Dental.csv
  * Sidebar filters: city, ZIP (prefix match), name search,
    human-readable Specialty (Spec_1..5) — falls back to raw taxonomy
    codes only when Spec_* columns aren't present.
  * If the V2 enrichment columns are present (Spec_1, Grouping_1, County,
    Deactivation Date, ...), they're surfaced so you can see the cleaned data.
  * Per-state files may be either Parquet (preferred — what the deployed
    Streamlit Cloud build ships) or CSV (local dev). The loader tries
    Parquet first and falls back to CSV automatically.

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
  * Specialty (Spec_1..5) multiselect as a more human filter than raw taxonomy codes.
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

# Two recognized V2 datasets, each living in its own subfolder with its own
# filename suffix. Keep these together so adding a third (e.g. "Physicians")
# later is a one-line addition.
DATASETS: dict[str, dict[str, str]] = {
    "Full Data": {"subdir": "Full Data", "suffix": "NPPES Extract"},
    "Dental":    {"subdir": "Dental",    "suffix": "NPPES Dental"},
}

# Column name constants — long strings, defined once for clarity
COL_NPI = "NPI"
COL_LAST = "Provider Last Name (Legal Name)"
COL_FIRST = "Provider First Name"
COL_ORG = "Provider Organization Name (Legal Business Name)"
COL_CITY = "Provider Business Practice Location Address City Name"
COL_STATE = "Provider Business Practice Location Address State Name"
COL_ZIP = "Provider Business Practice Location Address Postal Code"
TAXONOMY_COLS = [f"Healthcare Provider Taxonomy Code_{i}" for i in range(1, 6)]
SPEC_COLS = [f"Spec_{i}" for i in range(1, 6)]

# V2 cleaning adds these. They may not all be present in older folders.
V2_ENRICHED_COLS = [
    "County",
    *[f"Grouping_{i}" for i in range(1, 6)],
    *[f"Class_{i}" for i in range(1, 6)],
    *[f"Spec_{i}" for i in range(1, 6)],
    "Deactivation Date",
]

DEFAULT_PREVIEW_ROWS = 1000


# ---------------------------------------------------------------------------
# Data discovery
# ---------------------------------------------------------------------------
@st.cache_data
def find_latest_monthly_folder(root: Path) -> tuple[Path, str]:
    """Return (newest monthly folder path, label like 'May 2026').

    Picks the newest folder that has at least one recognized V2 dataset
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
def unique_taxonomies(df_index_key: str, codes_tuple: tuple[str, ...]) -> list[str]:
    return sorted({c for c in codes_tuple if c})


@st.cache_data
def unique_specialties(df_index_key: str, specs_tuple: tuple[str, ...]) -> list[str]:
    """Sorted unique human-readable specialties from the Spec_1..5 columns."""
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
        # Match if ANY of Spec_1..5 equals one of the selected specialties.
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
        f"inside it. Run V2_run_monthly_split.py (or the Parquet conversion step) first."
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

# Specialty: multiselect of unique human-readable specialties (Spec_1..5)
# from the cleaned V2 data. In the Dental dataset, many providers also have
# non-dental taxonomies in Spec_2..5 (e.g. "Student, Health Care" or other
# physician roles). We restrict the option list to specs whose aligned
# Grouping_N == "Dental Providers", so the dropdown surfaces only dental
# roles (Dentist, Dental Hygienist, Orthodontics, ...).
spec_cols_present = [c for c in SPEC_COLS if c in df.columns]
if spec_cols_present:
    dental_specs: list[str] = []
    for c in spec_cols_present:
        # The Grouping column at the same index N is the gate.
        g = c.replace("Spec_", "Grouping_")
        if g in df.columns:
            dental_mask = df[g] == "Dental Providers"
            dental_specs.extend(df.loc[dental_mask, c].fillna("").tolist())
        else:
            # No Grouping_N to gate on — fall back to all values from this slot.
            dental_specs.extend(df[c].fillna("").tolist())
    specialty_options = unique_specialties(
        f"{dataset_choice}:{state}:dental_only", tuple(dental_specs)
    )
    specialties = st.sidebar.multiselect(
        "Specialty (any match)",
        options=specialty_options,
        default=[],
        help="Dental specialties only (Spec_1..5 values where the matching "
             "Grouping_N is 'Dental Providers'). Match is any-of across the "
             "five Spec_N columns.",
    )
else:
    # Older folders without V2 enrichment — keep raw taxonomy code filter as fallback.
    all_tax_codes: list[str] = []
    for c in TAXONOMY_COLS:
        if c in df.columns:
            all_tax_codes.extend(df[c].fillna("").tolist())
    taxonomy_options = unique_taxonomies(f"{dataset_choice}:{state}", tuple(all_tax_codes))
    specialties = st.sidebar.multiselect(
        "Taxonomy codes (any match)",
        options=taxonomy_options,
        default=[],
        help="Spec_1..5 columns not found in this dataset, "
             "falling back to raw NUCC taxonomy codes.",
    )
    # apply_filters expects Spec_* names; with no Spec_* columns it will
    # short-circuit, so we instead inline a taxonomy-code match here.
    if specialties:
        tax_cols_present = [c for c in TAXONOMY_COLS if c in df.columns]
        if tax_cols_present:
            tmask = pd.Series(False, index=df.index)
            for c in tax_cols_present:
                tmask = tmask | df[c].isin(specialties)
            df = df[tmask]
        specialties = []  # already applied above

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
