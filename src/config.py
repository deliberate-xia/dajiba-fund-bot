"""
Configuration management: load/save holdings and preferences.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CostLot:
    """A single purchase transaction."""
    date: str                     # "2026-07-20"
    amount_cny: float             # 1000.00
    nav_at_purchase: float | None # 2.0984 (None if unconfirmed)
    shares: float | None          # 476.55 (None if unconfirmed)
    nav_confirmed: bool = True
    note: str = ""


@dataclass
class FundHolding:
    """Complete holding record for one fund."""
    fund_code: str
    fund_name: str
    fund_type: str                # "index_enhanced" | "sector_index" | ...
    volatility_profile: str       # "broad_market" | "sector" | "bond"
    benchmark_index: str
    benchmark_name: str
    cost_lots: list[dict] = field(default_factory=list)
    skip_tracking: bool = False

    @property
    def total_invested(self) -> float:
        """Net cash still committed: purchases minus redemptions (cash flow)."""
        return sum(lot["amount_cny"] for lot in self.cost_lots
                   if lot.get("nav_confirmed", False) and lot.get("amount_cny") is not None)

    @property
    def total_shares(self) -> float:
        return sum(lot["shares"] for lot in self.cost_lots
                   if lot.get("nav_confirmed", False) and lot.get("shares") is not None)

    @property
    def total_purchase_cost(self) -> float:
        """Total cash spent on purchases (redemptions excluded)."""
        return sum(lot["amount_cny"] for lot in self.cost_lots
                   if lot.get("nav_confirmed", False)
                   and (lot.get("amount_cny") or 0) > 0)

    @property
    def weighted_entry_nav(self) -> float | None:
        """Average NAV of purchase lots only (redemptions must NOT be mixed in —
        selling at a higher NAV would otherwise distort the cost basis downward)."""
        purchases = [l for l in self.cost_lots
                     if l.get("nav_confirmed", False)
                     and (l.get("amount_cny") or 0) > 0
                     and l.get("nav_at_purchase") is not None]
        if not purchases:
            return None
        total_amt = sum(l["amount_cny"] for l in purchases)
        if total_amt == 0:
            return None
        return sum(l["amount_cny"] * l["nav_at_purchase"] for l in purchases) / total_amt

    @property
    def realized_pnl(self) -> float:
        """Realized PnL from redemption lots: proceeds − cost of redeemed shares."""
        avg_nav = self.weighted_entry_nav
        if avg_nav is None or avg_nav <= 0:
            return 0.0
        realized = 0.0
        for l in self.cost_lots:
            if not l.get("nav_confirmed", False):
                continue
            shares = l.get("shares") or 0
            amount = l.get("amount_cny") or 0
            if shares < 0 and amount < 0:
                proceeds = -amount
                realized += proceeds - (-shares * avg_nav)
        return realized

    @property
    def has_pending_lots(self) -> bool:
        return any(not l.get("nav_confirmed", False) for l in self.cost_lots)


@dataclass
class UserPreferences:
    """User-configurable preferences."""
    pushplus_user_token: str = ""
    pushplus_topic_token: str = ""
    report_detail_level: str = "standard"
    extra_alert_drop_threshold: float = -0.05
    extra_alert_change_threshold: float = 0.03
    stop_loss_multipliers: dict = field(default_factory=lambda: {"broad_market": 2.5, "sector": 3.5})
    take_profit_multipliers: dict = field(default_factory=lambda: {"broad_market": 7.0, "sector": 6.0})
    hard_stop_loss_pct: dict = field(default_factory=lambda: {"broad_market": -0.08, "sector": -0.12})
    trend_score_thresholds: dict = field(default_factory=lambda: {"green": 65, "red": 35})
    timezone: str = "Asia/Shanghai"

    # ── 分批止盈 + 移动止盈 ──
    # Tighter ATR multipliers for trailing stop on the remaining position after
    # partial take-profit.  The remaining shares ride the trend; this stop is
    # what kicks you out when the trend finally reverses.
    trailing_stop_post_profit_multipliers: dict = field(
        default_factory=lambda: {"broad_market": 1.5, "sector": 2.0}
    )
    # Trend-strength → recommended sell ratio when take-profit triggers.
    # Higher trend score = sell less (let profits run).
    # Evaluated top-down: first matching min_trend_score wins.
    take_profit_sell_ratios: list = field(default_factory=lambda: [
        {"min_trend_score": 75, "sell_ratio": 0.30},
        {"min_trend_score": 65, "sell_ratio": 0.50},
        {"min_trend_score": 0,  "sell_ratio": 0.70},
    ])


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def _get_data_dir() -> Path:
    """Absolute path to data/ directory."""
    return Path(__file__).resolve().parent.parent / "data"


def load_holdings(path: Path | None = None) -> dict[str, FundHolding]:
    """Load holdings.json, return dict keyed by fund_code."""
    if path is None:
        path = _get_data_dir() / "holdings.json"
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    result = {}
    for code, obj in raw.items():
        result[code] = FundHolding(
            fund_code=obj.get("fund_code", code),
            fund_name=obj.get("fund_name", ""),
            fund_type=obj.get("fund_type", ""),
            volatility_profile=obj.get("volatility_profile", "broad_market"),
            benchmark_index=obj.get("benchmark_index", ""),
            benchmark_name=obj.get("benchmark_name", ""),
            cost_lots=obj.get("cost_lots", []),
            skip_tracking=obj.get("skip_tracking", False),
        )
    return result


def save_holdings(holdings: dict[str, FundHolding], path: Path | None = None) -> None:
    """Persist holdings back to JSON."""
    if path is None:
        path = _get_data_dir() / "holdings.json"
    raw = {}
    for code, h in holdings.items():
        raw[code] = {
            "fund_code": h.fund_code,
            "fund_name": h.fund_name,
            "fund_type": h.fund_type,
            "volatility_profile": h.volatility_profile,
            "benchmark_index": h.benchmark_index,
            "benchmark_name": h.benchmark_name,
            "cost_lots": h.cost_lots,
            "skip_tracking": h.skip_tracking,
        }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)


def load_preferences(path: Path | None = None) -> UserPreferences:
    """Load preferences.json, with optional local override."""
    if path is None:
        path = _get_data_dir() / "preferences.json"
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # Merge local override file if present (contains secrets, not committed to git)
    local_path = _get_data_dir() / "preferences.local.json"
    if local_path.exists():
        try:
            with open(local_path, "r", encoding="utf-8") as f:
                local_raw = json.load(f)
            raw.update(local_raw)
        except (json.JSONDecodeError, OSError):
            pass

    return UserPreferences(
        pushplus_user_token=raw.get("pushplus_user_token", ""),
        pushplus_topic_token=raw.get("pushplus_topic_token", ""),
        report_detail_level=raw.get("report_detail_level", "standard"),
        extra_alert_drop_threshold=raw.get("extra_alert_drop_threshold", -0.05),
        extra_alert_change_threshold=raw.get("extra_alert_change_threshold", 0.03),
        stop_loss_multipliers=raw.get("stop_loss_multipliers", {}),
        take_profit_multipliers=raw.get("take_profit_multipliers", {}),
        hard_stop_loss_pct=raw.get("hard_stop_loss_pct", {}),
        trend_score_thresholds=raw.get("trend_score_thresholds", {}),
        timezone=raw.get("timezone", "Asia/Shanghai"),
        trailing_stop_post_profit_multipliers=raw.get("trailing_stop_post_profit_multipliers", {}),
        take_profit_sell_ratios=raw.get("take_profit_sell_ratios", []),
    )
