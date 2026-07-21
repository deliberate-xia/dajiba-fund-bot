"""
AKShare data fetcher: fund NAV, index data, trading calendar.
"""
import time
from datetime import date, timedelta

import akshare as ak
import pandas as pd


# ---------------------------------------------------------------------------
# Generic retry wrapper
# ---------------------------------------------------------------------------

def fetch_with_retry(func, max_retries: int = 3, delay: float = 2.0, **kwargs):
    """Call `func(**kwargs)` with retry + exponential backoff."""
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            return func(**kwargs)
        except Exception as e:
            last_err = e
            print(f"[Fetcher] Attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                time.sleep(delay ** attempt)
    raise last_err  # type: ignore


# ---------------------------------------------------------------------------
# Fund NAV
# ---------------------------------------------------------------------------

def _fetch_fund_nav_raw(fund_code: str) -> pd.DataFrame:
    """
    Fetch complete NAV history for a fund via AKShare.

    Uses ak.fund_open_fund_info_em() which returns ALL historical NAV data.
    Returns DataFrame with columns: date, nav, daily_return.
    """
    try:
        df = ak.fund_open_fund_info_em(
            symbol=fund_code,
            indicator="单位净值走势",
        )
    except Exception:
        # Fallback: try without indicator param
        df = ak.fund_open_fund_info_em(symbol=fund_code)

    if df is None or df.empty:
        raise ValueError(f"No NAV data returned for fund {fund_code}")

    # Normalise column names (AKShare may use Chinese column names)
    col_map = {}
    for col in df.columns:
        if "日期" in str(col) or "净值日期" in str(col) or col == "date":
            col_map[col] = "date"
        elif "单位净值" in str(col) or col == "nav":
            col_map[col] = "nav"
        elif "日增长" in str(col) or "增长率" in str(col) or col == "daily_return":
            col_map[col] = "daily_return"

    df = df.rename(columns=col_map)

    # Ensure we have required columns
    if "date" not in df.columns:
        df["date"] = df.iloc[:, 0]  # Assume first column is date
    if "nav" not in df.columns:
        df["nav"] = df.iloc[:, 1]   # Assume second column is NAV

    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["nav"] = pd.to_numeric(df["nav"], errors="coerce")

    if "daily_return" in df.columns:
        df["daily_return"] = pd.to_numeric(df["daily_return"], errors="coerce").fillna(0)
    else:
        # Compute daily return from NAV
        df = df.sort_values("date")
        df["daily_return"] = df["nav"].pct_change().fillna(0) * 100

    df = df.dropna(subset=["nav"]).sort_values("date").reset_index(drop=True)
    return df[["date", "nav", "daily_return"]]


def fetch_fund_nav_history(fund_code: str) -> pd.DataFrame:
    """Fetch fund NAV history with retry."""
    return fetch_with_retry(_fetch_fund_nav_raw, fund_code=fund_code)


# ---------------------------------------------------------------------------
# Index data
# ---------------------------------------------------------------------------

def _fetch_index_raw(index_code: str) -> pd.DataFrame:
    """
    Fetch daily index close prices.

    Supports:
      - CSI 300 ("000300") via ak.stock_zh_index_daily_em()
      - CSI Battery ("931719") via ak.stock_zh_index_hist_csindex()
      - Others: tries CSIndex format first, falls back to Eastmoney
    """
    if index_code == "000300":
        df = ak.stock_zh_index_daily_em(symbol="sh000300")
    elif index_code == "931719":
        df = ak.stock_zh_index_hist_csindex(symbol="931719")
    else:
        try:
            df = ak.stock_zh_index_hist_csindex(symbol=index_code)
        except Exception:
            df = ak.stock_zh_index_daily_em(symbol=f"sh{index_code}")

    if df is None or df.empty:
        raise ValueError(f"No index data returned for {index_code}")

    # Normalise columns
    col_map = {}
    for col in df.columns:
        cl = str(col).lower()
        if "date" in cl or "日期" in str(col):
            col_map[col] = "date"
        elif "close" in cl or "收盘" in str(col):
            col_map[col] = "close"

    df = df.rename(columns=col_map)

    if "date" not in df.columns:
        df["date"] = df.iloc[:, 0]
    if "close" not in df.columns:
        df["close"] = df.iloc[:, 4] if len(df.columns) > 4 else df.iloc[:, 1]

    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    return df[["date", "close"]]


def fetch_index_history(index_code: str) -> pd.DataFrame:
    """Fetch index history with retry."""
    return fetch_with_retry(_fetch_index_raw, index_code=index_code)


# ---------------------------------------------------------------------------
# Trading calendar
# ---------------------------------------------------------------------------

def _fetch_trade_calendar_raw() -> pd.DataFrame:
    """Fetch A-share trading calendar. Returns DataFrame with 'trade_date' column."""
    df = ak.tool_trade_date_hist_sina()
    df = df.rename(columns={df.columns[0]: "trade_date"})
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


def fetch_trade_calendar() -> list[date]:
    """Fetch trading calendar and return sorted list of dates."""
    df = fetch_with_retry(_fetch_trade_calendar_raw)
    return sorted(df["trade_date"].tolist())


# ---------------------------------------------------------------------------
# Latest NAV check (for pending confirmations)
# ---------------------------------------------------------------------------

def check_latest_nav(fund_code: str) -> dict | None:
    """
    Fetch only the most recent NAV record for a fund.
    Used to confirm pending purchase NAVs.

    Returns: {"date": date, "nav": float, "daily_return": float} or None.
    """
    try:
        df = fetch_fund_nav_history(fund_code)
        if df.empty:
            return None
        latest = df.iloc[-1]
        return {
            "date": latest["date"],
            "nav": float(latest["nav"]),
            "daily_return": float(latest.get("daily_return", 0)),
        }
    except Exception as e:
        print(f"[Fetcher] check_latest_nav({fund_code}) error: {e}")
        return None
