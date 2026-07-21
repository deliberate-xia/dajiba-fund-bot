"""
State persistence: read/write NAV history, index history, run state.
"""
import json
from datetime import date
from pathlib import Path

import pandas as pd


# ---------------------------------------------------------------------------
# NAV history
# ---------------------------------------------------------------------------

def load_nav_history(fund_code: str, data_dir: Path) -> pd.DataFrame:
    """Load NAV history from data/nav_history/{fund_code}.json."""
    file_path = data_dir / "nav_history" / f"{fund_code}.json"
    if not file_path.exists():
        return pd.DataFrame(columns=["date", "nav", "daily_return"])
    with open(file_path, "r", encoding="utf-8") as f:
        records = json.load(f)
    if not records:
        return pd.DataFrame(columns=["date", "nav", "daily_return"])
    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["nav"] = pd.to_numeric(df["nav"])
    df["daily_return"] = pd.to_numeric(df.get("daily_return", 0))
    return df.sort_values("date").reset_index(drop=True)


def save_nav_history(fund_code: str, df: pd.DataFrame, data_dir: Path) -> None:
    """Save NAV history, deduplicating by date."""
    nav_dir = data_dir / "nav_history"
    nav_dir.mkdir(parents=True, exist_ok=True)
    df = df.drop_duplicates(subset=["date"]).sort_values("date")
    records = []
    for _, row in df.iterrows():
        records.append({
            "date": str(row["date"]),
            "nav": float(row["nav"]),
            "daily_return": float(row.get("daily_return", 0)),
        })
    with open(nav_dir / f"{fund_code}.json", "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def merge_new_nav_data(
    existing: pd.DataFrame,
    fetched: pd.DataFrame,
) -> tuple[pd.DataFrame, int]:
    """
    Merge newly fetched NAV data with existing history.
    Returns (merged_df, new_point_count).
    """
    if existing.empty:
        return fetched, len(fetched)

    existing_dates = set(existing["date"])
    new_rows = fetched[~fetched["date"].isin(existing_dates)]

    if new_rows.empty:
        return existing, 0

    merged = pd.concat([existing, new_rows], ignore_index=True)
    merged = merged.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
    return merged, len(new_rows)


# ---------------------------------------------------------------------------
# Index history
# ---------------------------------------------------------------------------

def load_index_history(index_code: str, data_dir: Path) -> pd.DataFrame:
    """Load index history from data/index_history/{index_code}.json."""
    file_path = data_dir / "index_history" / f"{index_code}.json"
    if not file_path.exists():
        return pd.DataFrame(columns=["date", "close"])
    with open(file_path, "r", encoding="utf-8") as f:
        records = json.load(f)
    if not records:
        return pd.DataFrame(columns=["date", "close"])
    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["close"] = pd.to_numeric(df["close"])
    return df.sort_values("date").reset_index(drop=True)


def save_index_history(index_code: str, df: pd.DataFrame, data_dir: Path) -> None:
    """Save index history, deduplicating by date."""
    idx_dir = data_dir / "index_history"
    idx_dir.mkdir(parents=True, exist_ok=True)
    df = df.drop_duplicates(subset=["date"]).sort_values("date")
    records = []
    for _, row in df.iterrows():
        records.append({
            "date": str(row["date"]),
            "close": float(row["close"]),
        })
    with open(idx_dir / f"{index_code}.json", "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------

def load_run_state(data_dir: Path) -> dict:
    """Load run_state.json."""
    file_path = data_dir / "run_state.json"
    if not file_path.exists():
        return _default_run_state()
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_run_state(state: dict, data_dir: Path) -> None:
    """Save run_state.json."""
    with open(data_dir / "run_state.json", "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _default_run_state() -> dict:
    return {
        "last_run_date": None,
        "last_success_date": None,
        "first_run_date": None,
        "total_runs": 0,
        "failed_runs": 0,
        "notes": [],
    }


def detect_missed_runs(
    last_success_str: str | None,
    today: date,
    is_trading_day_fn,
) -> list[date]:
    """Return list of trading days missed between last success and today."""
    if last_success_str is None:
        return []
    try:
        last_success = date.fromisoformat(last_success_str)
    except (ValueError, TypeError):
        return []

    if last_success >= today:
        return []

    missed = []
    cursor = last_success + __import__("datetime").timedelta(days=1)
    while cursor < today:
        if is_trading_day_fn(cursor):
            missed.append(cursor)
        cursor += __import__("datetime").timedelta(days=1)
    return missed
