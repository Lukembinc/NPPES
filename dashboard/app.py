"""
NPPES Provider Lookup — Streamlit prototype (v2)
=================================================

What's new vs. v1:
  * Sidebar filters: city, ZIP (prefix match), name search, taxonomy code(s)
  * Top-N preview (default 1,000 rows) with a "Show all matching" toggle
  * Result count: matching / total
  * NPI and ZIP are kept as strings so leading zeros and 9-digit ZIPs survive

Run it from /Users/lukebincarousky/Downloads/NPPES/NPPES/dashboard:

    streamlit run app.py --server.maxMessageSize=1000

Or if you cd into that folder first, `.streamlit/config.toml` next to this file
applies the same setting automatically:

    cd "/Users/lukebincarousky/Downloads/NPPES/NPPES/dashboard"
    streamlit run app.py

Why the maxMessageSize bump? Even with filters, if you toggle "Show all matching"
on a big state with no filters applied, the WebSocket payload can exceed the
default 200 MB cap. 1000 MB gives plenty of headroom for prototyping. (This
limitation goes away entirely in Phase 4 when the FastAPI backend handles
pagination.)

Known follow-ups (not in this iteration):
  * Taxonomy codes are shown as raw NUCC codes (e.g. '1223G0001X'). We can
    swap in human-readable specialty labels by joining against the public
    NUCC taxonomy CSV — small lift, called out below with a TODO.
  * County filter requires joining ZIP → county via your existing
    Geographic Data/ZIP_Locale_Detail.xls workbook. Easy to add next.
  * Click-into-NPI profile page is the next step after filters feel right.
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
NPPES_ROOT = Path("/Users/lukebincarousky/Downloads/NPPES")

MONTH_NAMES = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}
MONTHLY_FOLDER_RE = re.compile(
    r"^NPPES_Data_Dissemination_(?P<month>[A-Za-z]+)_(?P<year>\d{4})_V2$"
)

# Column name constants — long strings, defined once for clarity
COL_NPI = "NPI"
COL_LAST = "Provider Last Name (Legal Name)"
COL_FIRST = "Provider First Name"
COL_ORG = "Provider Organization Name (Legal Business Name)"
COL_CITY = "Provider Business Practice Location Address City Name"
COL_STATE = "Provider Business Practice Location Address State Name"
COL_ZIP = "Provider Business Practice Location Address Postal Code"
TAXONOMY_COLS = [f"Healthcare Provider Taxonomy Code_{i}" for i in range(1, 6)]

DEFAULT_PREVIEW_ROWS = 1000


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------
@st.cache_data
def find_latest_states_dir(root: Path) -> tuple[Path, str]:
    """Return (path to newest States/ folder, label like 'April 2026')."""
    candidates: list[tuple[datetime, Path, str]] = []
    for dirpath, dirnames, _ in os.walk(root):
        for d in dirnames:
            m = MONTHLY_FOLDER_RE.match(d)
            if not m:
                continue
            month_num = MONTH_NAMES.get(m.group("month"))
            if month_num is None:
                continue
            states_dir = Path(dirpath) / d / "States"
            if states_dir.exists():
                candidates.append(
                    (datetime(int(m.group("year")), month_num, 1), states_dir, f"{m.group('month')} {m.group('year')}")
                )
    if not candidates:
        raise FileNotFoundError(
            f"No 'States/' subfolder found inside any monthly NPPES folder under {root}. "
            f"Did you run run_monthly_split.py yet?"
        )
    candidates.sort(key=lambda t: t[0])
    _, path, label = candidates[-1]
    return path, label


@st.cache_data
def list_states(states_dir: Path) -> list[str]:
    """Return sorted list of state codes from filenames in the States/ folder."""
    return sorted(
        p.name.split(" ")[0]
        for p in states_dir.glob("* NPPES Extract.csv")
        if len(p.name.split(" ")[0]) == 2 and p.name.split(" ")[0].isalpha()
    )


@st.cache_data
def load_state_csv(states_dir_str: str, state: str) -> pd.DataFrame:
    """
    Load and lightly normalize one state's per-provider CSV.

    Cached: read once per (states_dir, state) per session.
    """
    path = Path(states_dir_str) / f"{state} NPPES Extract.csv"
    df = pd.read_csv(path, low_memory=False, dtype=str)  # all strings = no float coercion of ZIPs/phones

    # NPI as a clean string (no trailing .0 ever appears since we read as str, but be defensive)
    if COL_NPI in df.columns:
        df[COL_NPI] = df[COL_NPI].fillna("").str.replace(r"\.0$", "", regex=True)

    # ZIP as string, drop any trailing ".0" that snuck in from a prior float pass
    if COL_ZIP in df.columns:
        df[COL_ZIP] = df[COL_ZIP].fillna("").str.replace(r"\.0$", "", regex=True)

    return df


@st.cache_data
def unique_cities(df_index_key: str, cities_tuple: tuple[str, ...]) -> list[str]:
    """
    Return sorted unique cities for the dropdown. Wrapped in a cache so we don't
    re-sort 600k rows every rerun. df_index_key is just for cache-busting per state.
    """
    return sorted({c for c in cities_tuple if c})


@st.cache_data
def unique_taxonomies(df_index_key: str, codes_tuple: tuple[str, ...]) -> list[str]:
    return sorted({c for c in codes_tuple if c})


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------
def apply_filters(
    df: pd.DataFrame,
    city: str | None,
    zip_prefix: str,
    name_query: str,
    taxonomy_codes: list[str],
) -> pd.DataFrame:
    """Apply sidebar filters to the loaded state DataFrame."""
    out = df

    if city:
        out = out[out[COL_CITY] == city]

    if zip_prefix:
        out = out[out[COL_ZIP].str.startswith(zip_prefix)]

    if name_query:
        q = name_query.strip().lower()
        # Match across last/first/org names (case-insensitive substring)
        cols_to_search = [c for c in (COL_LAST, COL_FIRST, COL_ORG) if c in out.columns]
        mask = pd.Series(False, index=out.index)
        for c in cols_to_search:
            mask = mask | out[c].fillna("").str.lower().str.contains(q, regex=False)
        out = out[mask]

    if taxonomy_codes:
        # Match if ANY of taxonomy_1..5 is in the selected list
        tax_cols_present = [c for c in TAXONOMY_COLS if c in out.columns]
        if tax_cols_present:
            mask = pd.Series(False, index=out.index)
            for c in tax_cols_present:
                mask = mask | out[c].isin(taxonomy_codes)
            out = out[mask]

    return out


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.set_page_config(page_title="NPPES Provider Lookup", layout="wide")
st.title("NPPES Provider Lookup")

# --- Resolve the data source ------------------------------------------------
try:
    states_dir, month_label = find_latest_states_dir(NPPES_ROOT)
except FileNotFoundError as e:
    st.error(str(e))
    st.stop()

st.caption(f"Data source: {month_label} extract — {states_dir}")

# --- Sidebar: state + filters ----------------------------------------------
st.sidebar.header("Filters")

states = list_states(states_dir)
if not states:
    st.warning("No per-state CSVs found in this States/ folder.")
    st.stop()

state = st.sidebar.selectbox(
    "State",
    states,
    index=states.index("MA") if "MA" in states else 0,
)

with st.spinner(f"Loading {state}…"):
    df = load_state_csv(str(states_dir), state)

# City: dropdown of unique values within the chosen state
city_options = unique_cities(state, tuple(df[COL_CITY].fillna("").tolist()) if COL_CITY in df.columns else ())
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

# Taxonomy: multiselect of unique codes present in this state
# TODO: join against the NUCC taxonomy CSV to show 'Dentist (1223G0001X)' instead of raw codes.
all_tax_codes: list[str] = []
for c in TAXONOMY_COLS:
    if c in df.columns:
        all_tax_codes.extend(df[c].fillna("").tolist())
taxonomy_options = unique_taxonomies(state, tuple(all_tax_codes))
taxonomy_codes = st.sidebar.multiselect(
    "Taxonomy codes (any match)",
    options=taxonomy_options,
    default=[],
    help="NUCC provider taxonomy codes. We can swap these for human-readable specialty labels next.",
)

# --- Apply filters ----------------------------------------------------------
filtered = apply_filters(df, city_filter, zip_prefix, name_query, taxonomy_codes)

# --- Result summary + table -------------------------------------------------
total = len(df)
match = len(filtered)
st.markdown(f"**{match:,}** of **{total:,}** providers match in {state}.")

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
