"""
Strategy engine: ATR, moving averages, trend scoring, signal generation.

All functions are pure — they operate on DataFrames and return values.
No side effects, no file I/O.
"""
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Analysis result
# ---------------------------------------------------------------------------

@dataclass
class FundAnalysis:
    """Complete analysis output for one fund."""
    fund_code: str
    fund_name: str

    # Current state
    current_nav: float = 0.0
    prev_nav: float = 0.0
    daily_change_pct: float = 0.0
    nav_date: str = ""

    # ATR
    atr_20: float = 0.0
    atr_20_pct: float = 0.0

    # Moving averages
    ma_20: float = 0.0
    ma_60: float = 0.0
    nav_vs_ma20_pct: float = 0.0
    nav_vs_ma60_pct: float = 0.0

    # Stop-loss / take-profit
    entry_nav_weighted: float = 0.0
    highest_nav_since_entry: float = 0.0
    dynamic_stop: float = 0.0
    hard_stop: float = 0.0
    effective_stop: float = 0.0
    take_profit_price: float = 0.0
    stop_distance_pct: float = 0.0
    profit_distance_pct: float = 0.0

    # ── 分批止盈 + 移动止盈 ──
    profit_tier: int = 0             # 0=none, 1=nearing, 2=triggered
    sell_ratio: float = 0.50         # recommended sell % when profit triggers (trend-adjusted)
    profit_strategy_reason: str = "" # why this sell ratio (trend-based)
    trailing_stop_post_profit: float = 0.0  # trailing stop for remaining position post-profit

    # Trend score
    trend_score: int = 0
    trend_light: str = "yellow"    # "green" | "yellow" | "red"
    trend_explanation: str = ""

    # Performance
    return_7d: float = 0.0
    return_30d: float = 0.0
    return_90d: float = 0.0
    cumulative_return_pct: float = 0.0

    # Benchmark comparison
    benchmark_return_7d: float = 0.0
    benchmark_return_30d: float = 0.0
    benchmark_return_90d: float = 0.0
    relative_strength_20d: float = 0.0
    beats_benchmark_text: str = ""

    # Signal
    signal_type: str = "hold"       # "add" | "hold" | "reduce" | "watch" | "stop" | "profit" | "pending"
    signal_message: str = ""
    signal_urgency: str = "normal"  # "normal" | "warning" | "alert"

    # Data quality
    data_quality: str = "ok"        # "ok" | "insufficient" | "stale"


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------

def compute_moving_averages(nav_series: pd.Series) -> dict:
    """
    Compute MA20, MA60, and distance percentages.
    Returns dict with keys: ma_20, ma_60, nav_vs_ma20_pct, nav_vs_ma60_pct.
    """
    n = len(nav_series)
    current = nav_series.iloc[-1]

    ma20 = float(nav_series.tail(20).mean()) if n >= 20 else float(nav_series.mean())
    ma60 = float(nav_series.tail(min(n, 60)).mean()) if n >= 10 else float(nav_series.mean())
    # If < 60 data points, we still compute what we can but note it's approximate

    return {
        "ma_20": ma20,
        "ma_60": ma60,
        "nav_vs_ma20_pct": (current / ma20 - 1) * 100,
        "nav_vs_ma60_pct": (current / ma60 - 1) * 100,
    }


# ---------------------------------------------------------------------------
# ATR (fund-adapted)
# ---------------------------------------------------------------------------

def compute_fund_atr(nav_series: pd.Series, period: int = 20) -> tuple[float, float]:
    """
    Fund-adapted ATR using absolute daily NAV changes.
    Returns (atr_value, atr_as_pct_of_nav).
    """
    if len(nav_series) < 2:
        return 0.0, 0.0

    daily_range = nav_series.diff().abs()
    atr = float(daily_range.ewm(span=period, adjust=False).mean().iloc[-1])
    current_nav = float(nav_series.iloc[-1])
    atr_pct = (atr / current_nav * 100) if current_nav > 0 else 0.0

    return atr, atr_pct


# ---------------------------------------------------------------------------
# Stop-loss & take-profit
# ---------------------------------------------------------------------------

def compute_stop_loss_levels(
    nav_series: pd.Series,
    entry_nav: float,
    entry_date,
    atr: float,
    volatility_profile: str,
    stop_multipliers: dict,
    profit_multipliers: dict,
    hard_stop_pct_map: dict,
) -> dict:
    """
    Compute dynamic stop-loss and take-profit levels.

    highest_nav_since_entry is computed only from the user's entry date onward,
    not from the fund's entire history.

    Equity profiles (broad_market / sector): ATR-based trailing stop +
    ATR%-based take-profit target.
    Bond profile: fixed percentage lines (ATR is meaningless for bond funds —
    a tiny ATR would place the stop right under the price and scream
    "near stop-loss" every single day).

    Returns dict with:
        dynamic_stop, hard_stop, effective_stop,
        take_profit_price, stop_distance_pct, profit_distance_pct,
        highest_nav_since_entry.
    """
    current_nav = float(nav_series.iloc[-1])

    # highest NAV since the user's entry (not all-time)
    entry_ts = pd.Timestamp(entry_date)
    nav_since_entry = nav_series[nav_series.index >= entry_ts]
    if len(nav_since_entry) == 0:
        nav_since_entry = nav_series
    highest_nav = float(nav_since_entry.max())

    stop_mult = stop_multipliers.get(volatility_profile, 2.5)
    profit_mult = profit_multipliers.get(volatility_profile, 5.0)
    hard_pct = hard_stop_pct_map.get(volatility_profile, -0.08)

    # Fallback ATR: if ATR is 0 (insufficient data), use 2% of current NAV
    effective_atr = atr if atr > 0 else current_nav * 0.02

    if volatility_profile == "bond":
        # Fixed percentage lines: bond funds move in fractions of a percent.
        # stop: -5%, take-profit: +8% relative to entry.
        hard_stop = entry_nav * (1 + hard_stop_pct_map.get("bond", -0.05))
        dynamic_stop = hard_stop
        take_profit_price = entry_nav * (1 + 0.08)
    else:
        # Dynamic trailing stop
        dynamic_stop = highest_nav - (effective_atr * stop_mult)

        # Hard stop
        hard_stop = entry_nav * (1 + hard_pct)  # hard_pct is negative

        # Take-profit: entry * (1 + ATR% as decimal * multiplier)
        atr_pct_decimal = effective_atr / entry_nav if entry_nav > 0 else 0.02
        take_profit_price = entry_nav * (1 + atr_pct_decimal * profit_mult)

    # Effective = more conservative (higher) of the two, but never above the
    # take-profit line (a stop above the profit target would make the two
    # lines cross and render the display nonsense).
    effective_stop = min(max(dynamic_stop, hard_stop), take_profit_price)

    # Distance to stop/profit
    stop_distance_pct = (current_nav / effective_stop - 1) * 100 if effective_stop > 0 else 100
    profit_distance_pct = (current_nav / take_profit_price - 1) * 100 if take_profit_price > 0 else 0

    # Only trigger stop if we have enough data (at least 5 trading days)
    # and the stop distance is genuinely negative
    has_enough_data = len(nav_since_entry) >= 3

    return {
        "dynamic_stop": dynamic_stop,
        "hard_stop": hard_stop,
        "effective_stop": effective_stop,
        "take_profit_price": take_profit_price,
        "stop_distance_pct": stop_distance_pct,
        "profit_distance_pct": profit_distance_pct,
        "highest_nav_since_entry": highest_nav,
        "stop_triggered": has_enough_data and current_nav <= effective_stop,
        "profit_triggered": current_nav >= take_profit_price,
        # Near-stop band must sit INSIDE the ATR trailing cushion, otherwise a
        # trailing stop (2.5×ATR below the high) makes "near stop" fire every
        # single day while the fund is at its highs.
        "near_stop": has_enough_data and 0 < stop_distance_pct <= 2,
        "near_profit": -3 <= profit_distance_pct < 0,
    }


# ---------------------------------------------------------------------------
# Tiered take-profit strategy (trend-adjusted + trailing stop)
# ---------------------------------------------------------------------------

def compute_profit_strategy(
    nav_series: pd.Series,
    entry_nav: float,
    entry_date,
    atr: float,
    volatility_profile: str,
    stop_info: dict,
    trend_score: int,
    preferences,          # UserPreferences
) -> dict:
    """
    Compute tiered take-profit strategy.

    When take-profit triggers:
      1. Sell ratio depends on trend strength (stronger trend → sell less)
      2. Remaining position switches to a tighter trailing stop instead of a
         fixed take-profit target — lets profits run while protecting gains.

    Returns:
        profit_tier: 0=normal, 1=nearing-profit, 2=profit-triggered
        sell_ratio: recommended sell percentage (0.30-0.70)
        trailing_stop_post_profit: trailing stop price for remaining shares
    """
    # ── Determine profit tier ──
    if stop_info.get("profit_triggered"):
        profit_tier = 2
    elif stop_info.get("near_profit"):
        profit_tier = 1
    else:
        profit_tier = 0

    # ── Trend-adjusted sell ratio ──
    sell_ratio = 0.50  # default fallback
    sell_reason = ""
    for tier in preferences.take_profit_sell_ratios:
        if trend_score >= tier["min_trend_score"]:
            sell_ratio = tier["sell_ratio"]
            break

    if trend_score >= 75:
        sell_reason = "趋势强劲，小比例止盈让利润奔跑"
    elif trend_score >= 65:
        sell_reason = "趋势良好，标准比例止盈锁利"
    else:
        sell_reason = "趋势偏弱，大比例止盈落袋为安"

    # ── Trailing stop for remaining position ──
    post_mult = preferences.trailing_stop_post_profit_multipliers.get(
        volatility_profile, 1.5
    )
    current_nav = float(nav_series.iloc[-1])
    effective_atr = atr if atr > 0 else current_nav * 0.02
    highest = stop_info["highest_nav_since_entry"]

    # Trailing stop: highest since entry minus tighter ATR multiple.
    # Floor at entry_nav — once partial profit is taken, remaining position
    # should never turn into a loss.
    trailing_stop = max(
        highest - effective_atr * post_mult,
        entry_nav
    )

    return {
        "profit_tier": profit_tier,
        "sell_ratio": sell_ratio,
        "sell_reason": sell_reason,
        "trailing_stop_post_profit": trailing_stop,
        "trailing_stop_distance_pct": (current_nav / trailing_stop - 1) * 100
            if trailing_stop > 0 else 100,
    }


# ---------------------------------------------------------------------------
# Trend scoring (4 dimensions → 0-100)
# ---------------------------------------------------------------------------

def compute_trend_score(
    nav_series: pd.Series,
    benchmark_series: pd.Series | None,
    thresholds: dict,
) -> dict:
    """
    Four-dimension trend scoring.

    Returns: {total_score, light, ma_score, momentum_score, rs_score, vol_score, explanation}.
    """
    n = len(nav_series)
    ma = compute_moving_averages(nav_series)

    # ---- Dimension 1: MA Alignment (0-30) ----
    if n < 20:
        ma_score = 15  # Insufficient data
    else:
        nav = nav_series.iloc[-1]
        ma20, ma60 = ma["ma_20"], ma["ma_60"]
        if nav > ma20 > ma60:
            ma_score = 30  # Bullish alignment
        elif nav > ma20 and ma20 < ma60:
            ma_score = 20  # Recovering
        elif nav < ma20 < ma60:
            ma_score = 5   # Bearish
        elif nav < ma20 and ma20 > ma60:
            ma_score = 10  # Pulling back
        else:
            ma_score = 15  # Indecisive

    # ---- Dimension 2: Momentum (0-30) ----
    momentum_score = 15  # Default
    if n >= 7:
        ret_7d = (nav_series.iloc[-1] / nav_series.iloc[-min(n, 8)] - 1) * 100
    else:
        ret_7d = 0
    if n >= 30:
        ret_30d = (nav_series.iloc[-1] / nav_series.iloc[-min(n, 31)] - 1) * 100
    else:
        ret_30d = ret_7d  # Fallback

    if ret_7d > 3 and ret_30d > 5:
        momentum_score = 30
    elif ret_7d > 0 and ret_30d > 0:
        momentum_score = 22
    elif (ret_7d > 0) != (ret_30d > 0):
        momentum_score = 15  # Mixed signals
    elif ret_7d < 0 and ret_30d < 0:
        momentum_score = 8
    elif ret_7d < -3 and ret_30d < -5:
        momentum_score = 0

    # ---- Dimension 3: Relative Strength vs Benchmark (0-25) ----
    rs_score = 15  # Default (no benchmark)
    if benchmark_series is not None and len(benchmark_series) >= 7 and n >= 7:
        try:
            bm_ret_7d = (benchmark_series.iloc[-1] / benchmark_series.iloc[-min(len(benchmark_series), 8)] - 1) * 100
            bm_ret_20d = (benchmark_series.iloc[-1] / benchmark_series.iloc[-min(len(benchmark_series), 21)] - 1) * 100 \
                if len(benchmark_series) >= 21 else bm_ret_7d
        except (IndexError, ZeroDivisionError):
            bm_ret_7d = bm_ret_20d = 0

        # Align dates: compute fund returns over same lookback
        f_ret_7d = (nav_series.iloc[-1] / nav_series.iloc[-min(n, 8)] - 1) * 100
        f_ret_20d = (nav_series.iloc[-1] / nav_series.iloc[-min(n, 21)] - 1) * 100 if n >= 21 else f_ret_7d

        beats = 0
        if f_ret_7d > bm_ret_7d:
            beats += 1
        if f_ret_20d > bm_ret_20d:
            beats += 1

        # Also check 60d if available
        if n >= 60 and len(benchmark_series) >= 60:
            bm_ret_60d = (benchmark_series.iloc[-1] / benchmark_series.iloc[-61] - 1) * 100
            f_ret_60d = (nav_series.iloc[-1] / nav_series.iloc[-61] - 1) * 100
            if f_ret_60d > bm_ret_60d:
                beats += 1
            rs_score = {3: 25, 2: 18, 1: 12}.get(beats, 5)
        else:
            rs_score = {2: 25, 1: 18, 0: 10}.get(beats, 12)

    # ---- Dimension 4: Volatility Context (0-15) ----
    vol_score = 8  # Default
    if n >= 22:
        atr, _ = compute_fund_atr(nav_series, period=20)
        atr_prev, _ = compute_fund_atr(nav_series.iloc[:-2], period=20) if n > 22 else (atr, 0)
        atr_contracting = atr < atr_prev
        price_rising = nav_series.iloc[-1] > nav_series.iloc[-6] if n >= 6 else False

        if atr_contracting and price_rising:
            vol_score = 15
        elif atr_contracting and not price_rising:
            vol_score = 8
        elif not atr_contracting and price_rising:
            vol_score = 10
        else:
            vol_score = 3

    # ---- Composite ----
    total = ma_score + momentum_score + rs_score + vol_score
    green_threshold = thresholds.get("green", 65)
    red_threshold = thresholds.get("red", 35)

    if total >= green_threshold:
        light = "green"
    elif total < red_threshold:
        light = "red"
    else:
        light = "yellow"

    # Explanation
    parts = []
    if light == "green":
        parts.append("趋势向好")
    elif light == "red":
        parts.append("趋势偏弱")
    else:
        parts.append("趋势震荡")

    if n < 20:
        parts.append("（数据积累中，分析基于有限数据）")

    if momentum_score >= 22:
        parts.append("，短期动能较强")
    elif momentum_score <= 8:
        parts.append("，短期动能不足")

    explanation = "".join(parts)

    return {
        "total_score": total,
        "light": light,
        "ma_score": ma_score,
        "momentum_score": momentum_score,
        "rs_score": rs_score,
        "vol_score": vol_score,
        "explanation": explanation,
    }


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------

def generate_signal(
    analysis: "FundAnalysis",
    stop_info: dict,
    trend: dict,
    ma_info: dict,
) -> dict:
    """
    Decision matrix → signal type and Chinese message.

    Returns: {signal_type, signal_message, signal_urgency}.
    """
    light = trend["light"]
    nav_vs_ma20 = ma_info["nav_vs_ma20_pct"]

    # Check stop/profit triggers first
    if stop_info.get("profit_triggered"):
        sell_pct = int(analysis.sell_ratio * 100)
        keep_pct = 100 - sell_pct
        ts = analysis.trailing_stop_post_profit
        # Compute distance from current NAV to trailing stop
        ts_dist_pct = (analysis.current_nav / ts - 1) * 100 if ts > 0 else 0
        return {
            "signal_type": "profit",
            "signal_message": (
                f"🎉 已触及止盈线（{analysis.take_profit_price:.4f}）！\n\n"
                f"**分批止盈策略**：{analysis.profit_strategy_reason}\n"
                f"- 📤 建议卖出 **{sell_pct}%** 锁定利润\n"
                f"- 📥 保留 **{keep_pct}%** 仓位继续持有\n"
                f"- 🛡️ 剩余仓位移动止盈线：**{ts:.4f}**"
                f"（距当前 {ts_dist_pct:+.1f}%）\n"
                f"- 如净值跌破移动止盈线，则清仓剩余部分，本轮交易结束"
            ),
            "signal_urgency": "alert",
        }

    if stop_info.get("stop_triggered"):
        return {
            "signal_type": "stop",
            "signal_message": "🔴 已触发止损线！建议按纪律执行赎回，保存本金等待下一次机会。市场永远有机会，留得青山在。",
            "signal_urgency": "alert",
        }

    if stop_info.get("near_stop"):
        return {
            "signal_type": "reduce",
            "signal_message": "⚠️ 净值已接近止损位（距止损仅2%以内）。如继续下跌建议减仓控制风险，保护本金是第一位的。",
            "signal_urgency": "warning",
        }

    if stop_info.get("near_profit"):
        sell_pct = int(analysis.sell_ratio * 100)
        ts = analysis.trailing_stop_post_profit
        ts_dist_pct = (analysis.current_nav / ts - 1) * 100 if ts > 0 else 0
        return {
            "signal_type": "reduce",
            "signal_message": (
                f"已接近止盈目标位（{analysis.take_profit_price:.4f}），距离触发还需上涨 "
                f"{abs(analysis.profit_distance_pct):.1f}%。\n\n"
                f"**建议提前准备**：触发后卖出 {sell_pct}%（{analysis.profit_strategy_reason}），"
                f"剩余仓位移动止盈线 {ts:.4f}。"
            ),
            "signal_urgency": "warning",
        }

    # Trend-based signals
    if light == "green":
        if abs(nav_vs_ma20) < 3:
            return {
                "signal_type": "add",
                "signal_message": "趋势向好，净值接近均线支撑位，是相对舒适的加仓区间。可考虑适当加仓，建议分批买入、控制单次加仓不超过现有仓位的30%。",
                "signal_urgency": "normal",
            }
        elif nav_vs_ma20 > 5:
            return {
                "signal_type": "hold",
                "signal_message": "趋势向上但净值已偏离均线较远（>5%），追高有一定风险。建议持有现有仓位等待回踩均线时再加仓。",
                "signal_urgency": "normal",
            }
        else:
            return {
                "signal_type": "hold",
                "signal_message": "趋势向好，建议继续持有。如有回调至均线附近，可视为加仓机会。",
                "signal_urgency": "normal",
            }

    elif light == "yellow":
        return {
            "signal_type": "hold",
            "signal_message": "趋势尚不明朗，市场处于震荡整理阶段。建议持仓观望，不买不卖，等待更明确的趋势信号出现后再做决策。",
            "signal_urgency": "normal",
        }

    else:  # red
        if abs(nav_vs_ma20) > 3:
            return {
                "signal_type": "watch",
                "signal_message": "趋势偏弱，净值在均线下方运行。虽然尚未触发止损，但建议密切关注后续走势，暂不加仓。如继续走弱请做好减仓准备。",
                "signal_urgency": "warning",
            }
        else:
            return {
                "signal_type": "watch",
                "signal_message": "趋势偏弱但距均线不远，可能处于筑底阶段。建议继续观察，不要恐慌性赎回，等待方向明确。",
                "signal_urgency": "normal",
            }


# ---------------------------------------------------------------------------
# Master analysis function
# ---------------------------------------------------------------------------

def analyze_fund(
    holding,          # FundHolding
    nav_df: pd.DataFrame,
    benchmark_df: pd.DataFrame,
    preferences,      # UserPreferences
) -> FundAnalysis:
    """
    Run all computations for one fund; return complete FundAnalysis.
    """
    analysis = FundAnalysis(
        fund_code=holding.fund_code,
        fund_name=holding.fund_name,
    )

    nav_series = nav_df.set_index("date")["nav"]
    # Ensure DatetimeIndex for proper comparison
    if not isinstance(nav_series.index, pd.DatetimeIndex):
        nav_series.index = pd.to_datetime(nav_series.index)

    if nav_series.empty:
        analysis.data_quality = "insufficient"
        analysis.trend_explanation = "暂无足够数据进行分析"
        analysis.signal_type = "pending"
        analysis.signal_message = "数据积累中，请等待更多交易日后查看分析。"
        return analysis

    n = len(nav_series)

    # ---- Current state ----
    analysis.current_nav = float(nav_series.iloc[-1])
    analysis.nav_date = str(nav_df["date"].iloc[-1])
    if n >= 2:
        analysis.prev_nav = float(nav_series.iloc[-2])
        analysis.daily_change_pct = (analysis.current_nav / analysis.prev_nav - 1) * 100

    # ---- Moving averages ----
    ma_info = compute_moving_averages(nav_series)
    analysis.ma_20 = ma_info["ma_20"]
    analysis.ma_60 = ma_info["ma_60"]
    analysis.nav_vs_ma20_pct = ma_info["nav_vs_ma20_pct"]
    analysis.nav_vs_ma60_pct = ma_info["nav_vs_ma60_pct"]

    # ---- ATR ----
    atr, atr_pct = compute_fund_atr(nav_series)
    analysis.atr_20 = atr
    analysis.atr_20_pct = atr_pct

    # ---- Entry NAV ----
    entry_nav = holding.weighted_entry_nav
    if entry_nav is None or entry_nav <= 0:
        # Fallback: use first available NAV
        entry_nav = float(nav_series.iloc[0])
    analysis.entry_nav_weighted = entry_nav

    # ---- Stop-loss ----
    # Get earliest confirmed cost lot date as entry date
    entry_date = min(
        (lot["date"] for lot in holding.cost_lots if lot.get("nav_confirmed", False)),
        default=str(nav_series.index[0] if hasattr(nav_series, "index") else nav_df["date"].iloc[0]),
    )

    stop_info = compute_stop_loss_levels(
        nav_series,
        entry_nav,
        entry_date,
        atr,
        holding.volatility_profile,
        preferences.stop_loss_multipliers,
        preferences.take_profit_multipliers,
        preferences.hard_stop_loss_pct,
    )
    analysis.dynamic_stop = stop_info["dynamic_stop"]
    analysis.hard_stop = stop_info["hard_stop"]
    analysis.effective_stop = stop_info["effective_stop"]
    analysis.take_profit_price = stop_info["take_profit_price"]
    analysis.stop_distance_pct = stop_info["stop_distance_pct"]
    analysis.profit_distance_pct = stop_info["profit_distance_pct"]
    analysis.highest_nav_since_entry = stop_info["highest_nav_since_entry"]

    # ---- Benchmark ----
    # Guard against misaligned series: comparing the fund's 2026 returns
    # against a benchmark frozen in 2024 produces garbage "超额" numbers.
    bm_close = None
    if not benchmark_df.empty:
        try:
            fund_last = pd.to_datetime(nav_df["date"].iloc[-1])
            bm_last = pd.to_datetime(benchmark_df["date"].iloc[-1])
            if (fund_last - bm_last).days > 10:
                print(f"  WARNING: benchmark {holding.benchmark_name} is stale "
                      f"(latest {bm_last.date()}), skipping benchmark comparison")
            else:
                bm_close = benchmark_df.set_index("date")["close"]
        except (IndexError, ValueError) as e:
            print(f"  WARNING: benchmark alignment failed: {e}")

    # ---- Trend score ----
    trend = compute_trend_score(nav_series, bm_close, preferences.trend_score_thresholds)
    analysis.trend_score = trend["total_score"]
    analysis.trend_light = trend["light"]
    analysis.trend_explanation = trend["explanation"]

    # ---- Performance ----
    if n >= 7:
        analysis.return_7d = (nav_series.iloc[-1] / nav_series.iloc[-min(n, 8)] - 1) * 100
    if n >= 30:
        analysis.return_30d = (nav_series.iloc[-1] / nav_series.iloc[-min(n, 31)] - 1) * 100
    if n >= 90:
        analysis.return_90d = (nav_series.iloc[-1] / nav_series.iloc[-min(n, 91)] - 1) * 100

    analysis.cumulative_return_pct = (analysis.current_nav / entry_nav - 1) * 100

    # ---- Benchmark returns ----
    if bm_close is not None and len(bm_close) >= 7:
        analysis.benchmark_return_7d = (bm_close.iloc[-1] / bm_close.iloc[-min(len(bm_close), 8)] - 1) * 100
        analysis.benchmark_return_30d = (bm_close.iloc[-1] / bm_close.iloc[-min(len(bm_close), 31)] - 1) * 100 \
            if len(bm_close) >= 31 else analysis.benchmark_return_7d
        analysis.benchmark_return_90d = (bm_close.iloc[-1] / bm_close.iloc[-min(len(bm_close), 91)] - 1) * 100 \
            if len(bm_close) >= 91 else analysis.benchmark_return_30d

    analysis.relative_strength_20d = analysis.return_30d - analysis.benchmark_return_30d

    # ---- Beats benchmark text ----
    parts = []
    for period, fund_ret, bm_ret in [
        ("近7日", analysis.return_7d, analysis.benchmark_return_7d),
        ("近30日", analysis.return_30d, analysis.benchmark_return_30d),
        ("近90日", analysis.return_90d, analysis.benchmark_return_90d),
    ]:
        if abs(fund_ret) < 0.001 and abs(bm_ret) < 0.001:
            continue
        delta = fund_ret - bm_ret
        if delta > 0:
            parts.append(f"✅ {period}跑赢 +{delta:.2f}%")
        else:
            parts.append(f"❌ {period}跑输 {delta:.2f}%")
    analysis.beats_benchmark_text = " | ".join(parts) if parts else "数据不足"

    # ---- Tiered take-profit strategy ----
    profit_strategy = compute_profit_strategy(
        nav_series,
        entry_nav,
        entry_date,
        atr,
        holding.volatility_profile,
        stop_info,
        trend["total_score"],
        preferences,
    )
    analysis.profit_tier = profit_strategy["profit_tier"]
    analysis.sell_ratio = profit_strategy["sell_ratio"]
    analysis.profit_strategy_reason = profit_strategy["sell_reason"]
    analysis.trailing_stop_post_profit = profit_strategy["trailing_stop_post_profit"]

    # ---- Signal ----
    signal = generate_signal(analysis, stop_info, trend, ma_info)
    analysis.signal_type = signal["signal_type"]
    analysis.signal_message = signal["signal_message"]
    analysis.signal_urgency = signal["signal_urgency"]

    # ---- Data quality ----
    if n < 7:
        analysis.data_quality = "insufficient"
    elif n < 20:
        analysis.data_quality = "limited"
    else:
        analysis.data_quality = "ok"

    return analysis


# ---------------------------------------------------------------------------
# Portfolio summary
# ---------------------------------------------------------------------------

def compute_portfolio_summary(analyses: list[FundAnalysis]) -> dict:
    """
    Aggregate across all holdings.
    Returns: {total_invested, total_market_value, total_pnl, total_pnl_pct,
              best_performer, worst_performer, active_count, pending_count}.
    """
    total_invested = 0.0
    total_value = 0.0
    active = []
    pending_count = 0

    for a in analyses:
        if a.signal_type == "pending":
            pending_count += 1
            continue

        # Estimate invested from entry nav and shares
        if a.entry_nav_weighted > 0 and a.current_nav > 0:
            # We need shares count — use cost lots from the original data
            pass
        active.append(a)

    # Values are computed in reporter since it has access to holdings
    result = {
        "total_invested": total_invested,
        "total_market_value": total_value,
        "total_pnl": total_value - total_invested,
        "total_pnl_pct": ((total_value / total_invested - 1) * 100) if total_invested > 0 else 0,
        "active_count": len(active),
        "pending_count": pending_count,
        "has_alerts": any(a.signal_urgency == "alert" for a in active),
        "has_warnings": any(a.signal_urgency == "warning" for a in active),
    }

    # Best / worst
    if active:
        by_return = sorted(active, key=lambda x: x.cumulative_return_pct, reverse=True)
        result["best_performer"] = f"{by_return[0].fund_name} ({by_return[0].cumulative_return_pct:+.2f}%)"
        result["worst_performer"] = f"{by_return[-1].fund_name} ({by_return[-1].cumulative_return_pct:+.2f}%)"

    return result
