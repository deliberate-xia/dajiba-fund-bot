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
    no_exit: bool = False        # True: 长期持有策略，不产生止损/止盈卖出信号

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
        """Average NAV of the OPEN campaign's purchase lots only (see
        segment_campaigns). A fully exited round must NOT leak its cost basis
        into a re-entered position — e.g. 003015 exited 8/27 @2.0984, re-bought
        9/1 @2.1221; pooling both would shift the entry NAV used for
        stop/take-profit lines by ~0.5%. Falls back to all purchase lots when
        the fund is fully closed, keeping legacy single-campaign semantics."""
        closed, open_c = segment_campaigns(self.cost_lots)
        if open_c is not None:
            purchases = open_c["purchases"]
        else:
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
    def position_entry_date(self) -> str | None:
        """Earliest purchase date of the OPEN campaign; None when fully closed
        (callers fall back to the legacy min-confirmed-lot date)."""
        _, open_c = segment_campaigns(self.cost_lots)
        if open_c is None or not open_c["purchases"]:
            return None
        return min(l["date"] for l in open_c["purchases"])

    @property
    def realized_pnl(self) -> float:
        """Realized PnL: proceeds − cost, priced per campaign (see
        segment_campaigns). Each redemption is measured against its own
        campaign's average purchase NAV, so a closed round keeps its own
        result even after the fund is re-entered at a different NAV."""
        realized = 0.0
        closed, open_c = segment_campaigns(self.cost_lots)
        for camp in closed + ([open_c] if open_c is not None else []):
            purchases = camp["purchases"]
            if not purchases:
                continue
            total_amt = sum(l["amount_cny"] for l in purchases)
            if total_amt <= 0:
                continue
            avg = sum(l["amount_cny"] * l["nav_at_purchase"] for l in purchases) / total_amt
            for l in camp["redemptions"]:
                shares = l.get("shares") or 0
                amount = l.get("amount_cny") or 0
                if shares < 0 and amount < 0:
                    realized += -amount - (-shares * avg)
        return realized

    @property
    def has_pending_lots(self) -> bool:
        return any(not l.get("nav_confirmed", False) for l in self.cost_lots)


def segment_campaigns(cost_lots: list[dict]) -> tuple[list[dict], dict | None]:
    """Split confirmed lots into closed round-trip campaigns plus the open one.

    A fund can be fully exited and later re-entered (003015: exited 8/27,
    re-bought 9/1). Without segmentation the old round's purchase lots keep
    pooling with the new position, distorting entry NAV and realized P&L.

    Lots are consumed in (date, purchase-first, file order) so same-day
    corrections/re-purchases land before same-day redemptions. Every
    confirmed purchase that happens while cumulative confirmed shares sit at
    zero starts a new campaign. Returns (closed_campaigns, open_campaign_or_None);
    each campaign = {"purchases": [...], "redemptions": [...]}.
    """
    ordered = sorted(
        enumerate(cost_lots),
        key=lambda e: (e[1].get("date", ""), 1 if (e[1].get("shares") or 0) < 0 else 0, e[0]),
    )
    campaigns: list[dict] = []
    current: dict = {"purchases": [], "redemptions": []}
    running = 0.0
    for _, lot in ordered:
        if not lot.get("nav_confirmed", False):
            continue
        shares = lot.get("shares")
        if shares is None:
            continue
        if shares > 0:
            if running <= 1e-9 and (current["purchases"] or current["redemptions"]):
                campaigns.append(current)
                current = {"purchases": [], "redemptions": []}
            current["purchases"].append(lot)
            running += shares
        else:
            current["redemptions"].append(lot)
            running += shares  # negative
    if current["purchases"] or current["redemptions"]:
        campaigns.append(current)
    if running > 1e-9 and campaigns:
        return campaigns[:-1], campaigns[-1]
    return campaigns, None


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
    # 距止损线 % 以内视为"接近止损"并发警告（收窄后正常波动不再频繁催）
    near_stop_band_pct: float = 1.0
    # 相同信号（如连续止损）多少交易日后才再次完整推送详情；
    # 期间只显示一行"信号未变"摘要，避免每天重复同一份止损催促。
    signal_cooldown_days: int = 3
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

    # ── 右侧加仓（止跌转涨确认后提示加仓）──
    # 仅对配置的 volatility_profile（默认行业基金 sector）生效：
    #   1. 前置条件：近 high_lookback 日内出现过 ≥ max_drawdown_pct 的
    #      深跌（从近期低点计算的最大回撤），即"刚从深跌中走出"
    #   2. 止跌信号组合确认：站上5日线+拐头 / 均线金叉 / MACD金叉，
    #      至少 min_signals 个同时成立才触发
    #   3. 右侧递减式加仓：tiers 定义各档位比例（相对当前仓位）
    #   4. 跌破本轮低点（触发日往前 low_lookback 日最低收盘）→ 信号作废
    reversal_add: dict = field(default_factory=lambda: {
        "enabled": True,
        "profiles": ["sector"],
        "high_lookback": 20,       # 回撤高点的回溯天数
        "max_drawdown_pct": -10.0, # 回撤阈值（%）
        "min_signals": 2,          # 组合确认最少信号数
        "ma_hold_days": 2,         # 站上5日线的连续天数
        "tiers": [0.20, 0.10, 0.05],
        "low_lookback": 20,        # 本轮低点的回溯天数（不含当日）
        "reentry_base_amount": 200.0,  # 清仓后重新入场的基准建仓金额（份额为0时使用）
    })

    # ── 跌多买多（左侧下跌加仓计划）──
    # 净值距 lookback_days 日内高点的回撤每跨过一个档位，推送一次买入提醒；
    # 每个档位一轮回只提醒一次，净值创新高后整轮重置。
    # 仅对 no_exit（长期持有，如 QDII）基金生效，与趋势止损策略互不干扰。
    dip_buy: dict = field(default_factory=lambda: {
        "enabled": True,
        "codes": [],               # 空列表 = 所有 no_exit 基金；否则只对列出的代码生效
        "lookback_days": 60,       # 回撤参考高点回溯天数
        "tiers": [
            {"dd_pct": -5,  "amount": 200},
            {"dd_pct": -10, "amount": 400},
            {"dd_pct": -15, "amount": 600},
            {"dd_pct": -20, "amount": 800},
        ],
    })

    # ── 每月定投提醒（长期复利现金流引擎）──
    # 每月第一个交易日下午推送一次定投提醒，金额按 allocations 权重拆分到各基金。
    # 机械执行，不看涨跌；金额随收入增长直接在 preferences.json 调整。
    monthly_dca: dict = field(default_factory=lambda: {
        "enabled": True,
        "amount_cny": 500,
        "allocations": [
            {"fund_code": "040046", "fund_name": "华安纳斯达克100指数A", "weight": 0.25},
            {"fund_code": "050025", "fund_name": "博时标普500ETF联接A", "weight": 0.25},
            {"fund_code": "003015", "fund_name": "中金沪深300指数增强A", "weight": 0.50},
        ],
    })


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
            no_exit=obj.get("no_exit", False),
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
            "no_exit": h.no_exit,
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
        near_stop_band_pct=raw.get("near_stop_band_pct", 1.0),
        signal_cooldown_days=raw.get("signal_cooldown_days", 3),
        trend_score_thresholds=raw.get("trend_score_thresholds", {}),
        timezone=raw.get("timezone", "Asia/Shanghai"),
        trailing_stop_post_profit_multipliers=raw.get("trailing_stop_post_profit_multipliers", {}),
        take_profit_sell_ratios=raw.get("take_profit_sell_ratios", []),
        reversal_add=raw.get("reversal_add", {}),
        dip_buy=raw.get("dip_buy", {}),
        monthly_dca=raw.get("monthly_dca", {}),
    )
