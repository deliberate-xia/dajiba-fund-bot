"""
Trading calendar: detect trading days with local caching.
"""
import json
from datetime import date, timedelta
from pathlib import Path


class TradingCalendar:
    """Manages A-share trading calendar with 7-day cache."""

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self._dates: set[date] | None = None

    def _ensure_loaded(self) -> None:
        """Load from cache file."""
        if self._dates is not None:
            return
        if not self.cache_path.exists():
            self._dates = set()
            return
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            dates_raw = raw.get("trade_dates", [])
            self._dates = {
                date.fromisoformat(d) for d in dates_raw
            }
        except (json.JSONDecodeError, KeyError, ValueError):
            self._dates = set()

    def update_cache(self, date_list: list[date]) -> None:
        """Overwrite cache with a fresh list of trading dates."""
        self._dates = set(date_list)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump({
                "trade_dates": [d.isoformat() for d in sorted(date_list)],
                "updated": date.today().isoformat(),
            }, f, ensure_ascii=False, indent=2)

    def is_trading_day(self, d: date | None = None) -> bool:
        """Check if given date (default: today) is a trading day."""
        if d is None:
            d = date.today()
        self._ensure_loaded()
        return d in self._dates

    def last_trading_day(self, d: date | None = None) -> date | None:
        """Get the most recent trading day on or before given date."""
        if d is None:
            d = date.today()
        self._ensure_loaded()
        if not self._dates:
            return None
        candidates = sorted(td for td in self._dates if td <= d)
        return candidates[-1] if candidates else None

    def previous_trading_day(self, d: date) -> date | None:
        """Get the immediately preceding trading day."""
        self._ensure_loaded()
        if not self._dates:
            return None
        candidates = sorted(td for td in self._dates if td < d)
        return candidates[-1] if candidates else None

    def is_friday(self, d: date | None = None) -> bool:
        """Check if given date falls on a Friday."""
        if d is None:
            d = date.today()
        return d.weekday() == 4

    def trading_days_between(self, start: date, end: date) -> int:
        """Count trading days between two dates (inclusive)."""
        self._ensure_loaded()
        if isinstance(start, str):
            start = date.fromisoformat(start)
        if isinstance(end, str):
            end = date.fromisoformat(end)
        return sum(1 for d in self._dates if start <= d <= end)
