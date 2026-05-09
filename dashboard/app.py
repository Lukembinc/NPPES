"""
NPPES Provider Lookup — minimal Streamlit prototype (v1)
========================================================

This is the smallest possible version of the app:
  - Auto-detects the newest monthly States/ folder under /Users/lukebincarousky/Downloads/NPPES/
  - Lets the user pick a state from a dropdown
  - Shows that state's per-provider CSV as a table

Run it from a terminal:

    pip3 install streamlit pandas
    streamlit run /Users/lukebincarousky/Downloads/NPPES/NPPES/dashboard/app.py

Streamlit will open the app at http://localhost:8501.

Mental model reminder
---------------------
Every time you change a widget (e.g. select a different state), Streamlit
reruns this whole script top-to-bottom. The @st.cache_data decorator below
prevents the per-state CSV from being re-read on every rerun — it's read
once per state, then served from memory.
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


# ---------------------------------------------------------------------------
# Auto-detection (same logic as run_monthly_split.py)
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
            month_name = m.group("month")
            year = int(m.group("year"))
            month_num = MONTH_NAMES.get(month_name)
            if month_num is None:
                continue
            states_dir = Path(dirpath) / d / "States"
            if states_dir.exists():
                candidates.append(
                    (datetime(year, month_num, 1), states_dir, f"{month_name} {year}")
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
    """Return sorted list of state codes by scanning '<XX> NPPES Extract.csv' filenames."""
    states = []
    for p in states_dir.glob("* NPPES Extract.csv"):
        code = p.name.split(" ")[0]
        if len(code) == 2 and code.isalpha():
            states.append(code)
    return sorted(states)


@st.cache_data
def load_state_csv(states_dir_str: str, state: str) -> pd.DataFrame:
    """
    Load one state's per-provider CSV. Cached so the file is read once per state
    per session, even if the user interacts with other widgets.

    Note: states_dir_str is taken as a string (not a Path) because @st.cache_data
    hashes its arguments to decide cache hits, and strings hash cleanly.
    """
    path = Path(states_dir_str) / f"{state} NPPES Extract.csv"
    return pd.read_csv(path, low_memory=False, dtype={"NPI": str})


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.set_page_config(page_title="NPPES Provider Lookup", layout="wide")
st.title("NPPES Provider Lookup")

# Resolve which monthly extract we're using
try:
    states_dir, month_label = find_latest_states_dir(NPPES_ROOT)
except FileNotFoundError as e:
    st.error(str(e))
    st.stop()

st.caption(f"Data source: {month_label} extract — {states_dir}")

# Pick a state
states = list_states(states_dir)
if not states:
    st.warning("No per-state CSVs found in this States/ folder.")
    st.stop()

state = st.selectbox("State", states, index=states.index("MA") if "MA" in states else 0)

# Load and display
with st.spinner(f"Loading {state}…"):
    df = load_state_csv(str(states_dir), state)

st.write(f"**{len(df):,}** providers in {state}.")
st.dataframe(df, use_container_width=True)
