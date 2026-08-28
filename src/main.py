"""
Main orchestrator: fetch → analyze → report → push → save.

Entry point for GitHub Actions workflow.
"""
import os
import sys
import time
from datetime import date
from pathlib import Path

# Fix Unicode output on Windows (GBK terminal → UTF-8)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Ensure src/ is importable when running as `python src/main.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.config import load_holdings, load_preferences, save_holdings, FundHolding
from src.trade_cal import TradingCalendar
from src.fetcher import (
    fetch_fund_nav_history,
    fetch_index_history,
    fetch_trade_calendar,
)
from src.store import (
    load_nav_history,
    save_nav_history,
    merge_new_nav_data,
    load_index_history,
    save_index_history,
    load_run_state,
    save_run_state,
    detect_missed_runs,
    load_add_signal_state,
    save_add_signal_state,
    load_signal_state,
    save_signal_state,
    load_dip_buy_state,
    save_dip_buy_state,
    _default_add_signal_state,
)
from src.analyzer import (
    analyze_fund,
    compute_portfolio_summary,
    compute_reversal_signals,
    update_add_signal_state,
    compute_dip_buy_status,
)
from src.macro import fetch_macro_snapshot, build_macro_brief
from src.reporter import build_daily_report, build_extra_alert
from src.pusher import send_report, send_alert


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_data_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data"


def _confirm_pending_lots(holding: FundHolding, nav_df) -> bool:
    """
    Confirm any pending cost lots using the fund's NAV history.

    The confirmation NAV is the NAV on the lot's actual confirmation date:
      - orders submitted after 15:00 confirm at the NEXT trading day's NAV
      - orders submitted before 15:00 confirm at the same day's NAV

    (Using "the latest NAV available" instead would silently confirm lots at
    a wrong price whenever the bot misses a few runs.)
    Returns True if any lot was confirmed (holding mutated in-place).
    """
    if nav_df is None or nav_df.empty or not holding.has_pending_lots:
        return False

    changed = False
    nav_dates = nav_df["date"].tolist()

    for lot in holding.cost_lots:
        if lot.get("nav_confirmed", True):
            continue

        lot_date_str = lot.get("date")
        if not lot_date_str:
            continue
        try:
            lot_date = date.fromisoformat(lot_date_str)
        except (ValueError, TypeError):
            continue

        note = lot.get("note", "")
        after_cutoff = "15:00后" in note or "15:00 后" in note
        strict = after_cutoff

        confirm_nav = None
        confirm_date = None
        for d in nav_dates:
            if d > lot_date or (not strict and d >= lot_date):
                row = nav_df[nav_df["date"] == d].iloc[0]
                confirm_nav = float(row["nav"])
                confirm_date = d
                break

        if confirm_nav is None:
            print(f"[{holding.fund_code}] Pending lot {lot_date_str}: "
                  f"confirmation NAV not available yet")
            continue

        lot["nav_at_purchase"] = confirm_nav
        if lot.get("amount_cny") is not None:
            # Purchase lot: calculate shares from amount
            lot["shares"] = round(lot["amount_cny"] / confirm_nav, 2)
        elif lot.get("shares") is not None:
            # Redemption lot: calculate amount from shares (will be negative)
            lot["amount_cny"] = round(lot["shares"] * confirm_nav, 2)
        lot["nav_confirmed"] = True
        changed = True
        print(f"[{holding.fund_code}] ✅ Lot {lot_date_str} confirmed: "
              f"NAV {confirm_nav} on {confirm_date}")

    if changed and not holding.has_pending_lots:
        holding.skip_tracking = False
        print(f"[{holding.fund_code}] All lots confirmed — tracking enabled.")

    return changed


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def _collect_weekly_data(
    holdings: dict,
    analyses: list,
    today: date,
) -> dict:
    """
    Collect weekly operations and performance for Friday review.
    """
    from datetime import timedelta

    # Find this week's Monday
    monday = today - timedelta(days=today.weekday())

    # ---- Detect operations this week ----
    operations = []
    for code, h in holdings.items():
        for lot in h.cost_lots:
            lot_date_str = lot.get("date", "")
            if not lot_date_str:
                continue
            try:
                lot_date = date.fromisoformat(lot_date_str)
            except (ValueError, TypeError):
                continue
            if lot_date < monday or lot_date > today:
                continue

            # Determine action type from note
            note = lot.get("note", "")
            amount = lot.get("amount_cny", 0)
            if "转入" in note:
                action = "加仓（转入）"
            elif "赎回" in note:
                action = "减仓"
            else:
                action = "建仓/加仓"

            operations.append({
                "date": lot_date_str,
                "fund_name": h.fund_name,
                "action": action,
                "amount": amount,
                "evaluation": _evaluate_operation(h, lot, lot_date, note),
            })

    # ---- Per-fund weekly performance ----
    fund_weekly = []
    for a in analyses:
        if a is None or a.signal_type == "pending":
            continue
        weekly_ret = a.return_7d  # Close enough for a ~5-day trading week
        # Build per-fund comment
        if a.trend_light == "green":
            comment = "趋势向好，继续持有"
        elif a.trend_light == "yellow":
            comment = "震荡整理，持仓观望"
        else:
            comment = "趋势偏弱，密切关注"
        if a.signal_urgency == "alert":
            comment = "⚠️ " + comment

        fund_weekly.append({
            "fund_name": a.fund_name,
            "weekly_return_pct": weekly_ret,
            "cumulative_pnl_pct": a.cumulative_return_pct,
            "trend_light": a.trend_light,
            "comment": comment,
        })

    # ---- Good moves & concerns ----
    good_moves = []
    concerns = []

    # Good: held through green-light funds, added to winners
    green_funds = [a.fund_name for a in analyses if a and a.trend_light == "green"]
    if green_funds:
        good_moves.append(f"{'、'.join(green_funds)} 趋势偏多，继续持有是正确的")
    if not operations:
        good_moves.append("本周无频繁操作，保持纪律")

    # Concerns
    red_funds = [a.fund_name for a in analyses if a and a.trend_light == "red"]
    for fn in red_funds:
        concerns.append(f"{fn} 亮红灯（偏空），上周如有加仓需谨慎，下周重点观察是否继续走弱")
    if len(operations) >= 3:
        concerns.append(f"本周操作 {len(operations)} 笔，交易频率偏高，基金不适合频繁进出")

    # Check for early sales
    for op in operations:
        if "减仓" in op["action"]:
            concerns.append(f"{op['fund_name']} 本周有减仓操作，确认是否基于明确的策略信号")

    # ---- Next week ----
    next_week_notes = []
    for a in analyses:
        if a is None or a.signal_type == "pending":
            continue
        if a.signal_type == "add":
            next_week_notes.append(f"📌 {a.fund_name} 发出加仓信号，下周可关注入场时机")
        elif a.signal_urgency == "warning" or a.signal_urgency == "alert":
            next_week_notes.append(f"⚠️ {a.fund_name} 需谨慎，{a.signal_message[:20]}...")
    if not next_week_notes:
        next_week_notes.append("📌 无特别信号，维持现有持仓，静待趋势明朗")

    # ---- Total weekly PnL ----
    total_pnl_pct = sum(fw["weekly_return_pct"] for fw in fund_weekly) / len(fund_weekly) if fund_weekly else 0.0

    return {
        "week_label": f"{monday.month}/{monday.day}-{today.month}/{today.day}",
        "operations": operations,
        "fund_weekly": fund_weekly,
        "good_moves": good_moves,
        "concerns": concerns,
        "next_week_notes": next_week_notes,
        "total_weekly_pnl_pct": total_pnl_pct,
    }


def _evaluate_operation(holding, lot, lot_date: date, note: str) -> str:
    """Generate a brief evaluation of a single operation."""
    if not lot.get("nav_confirmed", False):
        return "⏳ 待确认净值"
    if "转入" in note:
        return "集中持仓，方向明确"
    if "赎回" in note:
        return "主动调仓，需后续验证"
    if "建仓" in note:
        return "首次建仓，入场时机合理"
    return "正常操作"


# ---------------------------------------------------------------------------
# Right-side add-position (右侧加仓)
# ---------------------------------------------------------------------------

_REVERSAL_SIGNAL_NAMES = {
    "s_ma5": "站上5日线+拐头",
    "s_cross": "均线金叉",
    "s_macd": "MACD金叉",
}


def _process_reversal_signal(
    holding,
    nav_df,
    analysis,
    add_state: dict,
    reversal_cfg: dict,
    today: date,
) -> list[tuple[str, str]]:
    """
    计算右侧加仓信号、推进状态机，返回需要即时推送的 (标题, 内容) 列表。
    状态写入 add_state[fund_code]；reporter 需要的信息写入 analysis.reversal_info。
    """
    nav_series = nav_df.set_index("date")["nav"]
    if not isinstance(nav_series.index, pd.DatetimeIndex):
        nav_series.index = pd.to_datetime(nav_series.index)
    if nav_series.empty:
        return []

    code = holding.fund_code
    prev_state = add_state.get(code, _default_add_signal_state(code))
    sig = compute_reversal_signals(nav_series, reversal_cfg)
    new_state, events = update_add_signal_state(
        prev_state, sig, today.isoformat(), float(nav_series.iloc[-1]), reversal_cfg,
    )
    add_state[code] = new_state

    # 命中信号的中文名（供日报展示）
    hit_names = [name for key, name in _REVERSAL_SIGNAL_NAMES.items()
                 if getattr(sig, key, False)]

    tiers = list(reversal_cfg.get("tiers", [0.20, 0.10, 0.05]))
    ratio = tiers[new_state["tier"] - 1] if new_state["tier"] >= 1 else 0.0
    current_value = holding.total_shares * float(nav_series.iloc[-1])
    # 清仓后重新入场（份额=0）：没有"现有仓位"可加，
    # 建议金额改用基准建仓金额（reentry_base_amount），按档位比例递减。
    if holding.total_shares > 0:
        suggest_amount = current_value * ratio
        is_reentry = False
    else:
        base = float(reversal_cfg.get("reentry_base_amount", 200.0))
        suggest_amount = base * ratio
        is_reentry = True

    analysis.reversal_info = {
        "status": new_state["status"],
        "tier": new_state["tier"],
        "max_tiers": len(tiers),
        "signal_since": new_state.get("signal_since", ""),
        "signals_hit": hit_names,
        "drawdown_pct": sig.drawdown_pct,
        "max_dd_pct": sig.max_dd_pct,
        "recent_low": new_state.get("recent_low", 0.0),
        "tier_ratio": ratio,
        "suggest_amount": suggest_amount,
        "is_reentry": is_reentry,
    }

    sig_high_lookback = reversal_cfg.get("high_lookback", 20)
    pushes = []
    for ev in events:
        if ev["type"] == "tier_triggered":
            tier = ev["tier"]
            pct = int(tiers[tier - 1] * 100)
            signals_str = "、".join(hit_names) if hit_names else "止跌转涨"
            if tier == 1:
                title = f"📥 右侧加仓信号：{holding.fund_name}"
                content = "\n".join([
                    f"{holding.fund_code} 近{sig_high_lookback}日最深回撤 {sig.max_dd_pct:+.1f}%，"
                    "出现止跌转涨确认：",
                    f"✅ {signals_str}",
                    "",
                    f"建议{'重新建仓' if is_reentry else '加仓现有仓位的'} "
                    f"**{pct}%**（≈ ¥{suggest_amount:,.0f}），右侧递减第 1 档。",
                    f"🛡️ 本轮低点 {new_state['recent_low']:.4f}，跌破则信号作废。",
                ])
            else:
                title = f"📥 右侧加仓第 {tier} 档确认：{holding.fund_name}"
                content = "\n".join([
                    "继续上行中的再次确认：",
                    f"✅ {signals_str}",
                    "",
                    f"建议{'再追加' if not is_reentry else '再加'} "
                    f"**{pct}%**（≈ ¥{suggest_amount:,.0f}），右侧递减第 {tier} 档。",
                    f"🛡️ 本轮低点 {new_state['recent_low']:.4f} 仍有效，跌破则信号作废。",
                ])
            pushes.append((title, content))
        elif ev["type"] == "invalidated":
            title = f"⚠️ 右侧加仓信号作废：{holding.fund_name}"
            content = "\n".join([
                f"{holding.fund_code} 跌破本轮低点 {prev_state.get('recent_low', 0):.4f}，",
                "原定加仓计划取消，等下一次止跌确认后再考虑。切勿盲目抄底。",
            ])
            pushes.append((title, content))
    return pushes


def main():
    data_dir = _get_data_dir()
    today = date.today()
    print(f"\n{'='*50}")
    print(f"大基吧 · 基金日报 — {today.isoformat()}")
    print(f"{'='*50}\n")

    # ---- 1. Trading calendar ----
    cal = TradingCalendar(data_dir / "trade_calendar.json")
    try:
        trade_dates = fetch_trade_calendar()
        cal.update_cache(trade_dates)
        print(f"[Calendar] Loaded {len(trade_dates)} trading days")
    except Exception as e:
        print(f"[Calendar] WARNING: Could not fetch trading calendar: {e}")
        print("[Calendar] Falling back to cached data (may be stale)")

    if not cal.is_trading_day(today):
        print(f"[Calendar] {today} is NOT a trading day. Exiting gracefully.")
        return

    # ---- 2. Load state ----
    holdings = load_holdings()
    prefs = load_preferences()
    run_state = load_run_state(data_dir)
    add_state = load_add_signal_state(data_dir)  # 右侧加仓信号状态
    signal_state = load_signal_state(data_dir)   # 相同信号推送冷却状态
    dip_state = load_dip_buy_state(data_dir)     # 跌多买多档位状态

    # Override tokens from env vars if available (GitHub Secrets)
    user_token = os.environ.get("PUSHPLUS_USER_TOKEN", prefs.pushplus_user_token)
    topic_token = os.environ.get("PUSHPLUS_TOPIC_TOKEN", prefs.pushplus_topic_token)

    # ---- 3. (Pending lot confirmation happens per-fund after NAV fetch) ----

    # ---- 4. Detect missed runs ----
    missed = detect_missed_runs(
        run_state.get("last_success_date"),
        today,
        cal.is_trading_day,
    )
    if missed:
        print(f"[Main] Detected {len(missed)} missed trading days: {missed}")

    # ---- 5. Fetch & analyze each fund ----
    analyses = []
    alerts_to_send = []
    reversal_pushes = []  # 右侧加仓即时推送 (title, content)
    dip_buy_pushes = []   # 跌多买多档位触发推送 (title, content)
    holdings_changed = False
    is_first_run = (run_state.get("total_runs", 0) == 0)
    reversal_cfg = prefs.reversal_add or {}
    reversal_profiles = reversal_cfg.get("profiles", ["sector"])
    dip_cfg = prefs.dip_buy or {}
    dip_enabled = dip_cfg.get("enabled", True)
    dip_codes = dip_cfg.get("codes") or []
    dip_tiers = dip_cfg.get("tiers", [])
    dip_per_code_tiers = dip_cfg.get("per_code_tiers", {}) or {}

    for fund_code, holding in holdings.items():
        print(f"\n--- [{fund_code}] {holding.fund_name} ---")

        if holding.skip_tracking:
            print(f"  Skipping — NAV not yet confirmed.")
            # Create a minimal pending analysis
            from src.analyzer import FundAnalysis
            pa = FundAnalysis(
                fund_code=fund_code,
                fund_name=holding.fund_name,
                signal_type="pending",
                signal_message="等待净值确认",
            )
            analyses.append(pa)
            continue

        # Fetch NAV
        try:
            fetched_nav = fetch_fund_nav_history(fund_code)
            print(f"  Fetched {len(fetched_nav)} NAV records")
        except Exception as e:
            print(f"  ERROR fetching NAV: {e}")
            from src.analyzer import FundAnalysis
            fa = FundAnalysis(
                fund_code=fund_code,
                fund_name=holding.fund_name,
                data_quality="stale",
                trend_explanation=f"数据获取失败: {e}",
                signal_type="hold",
                signal_message="今日数据获取异常，请稍后手动重试。显示最近可用数据。",
            )
            analyses.append(fa)
            continue

        # Merge with stored history
        stored_nav = load_nav_history(fund_code, data_dir)
        merged_nav, new_points = merge_new_nav_data(stored_nav, fetched_nav)

        if new_points == 0 and not is_first_run:
            print(f"  No new NAV data (latest: {merged_nav['date'].iloc[-1] if not merged_nav.empty else 'N/A'})")

        save_nav_history(fund_code, merged_nav, data_dir)
        print(f"  Saved {len(merged_nav)} total NAV records ({new_points} new)")

        # Confirm any pending cost lots using the exact confirmation-date NAV
        if _confirm_pending_lots(holding, merged_nav):
            holdings_changed = True

        # Fetch benchmark index (海外 QDII 无 A 股基准指数，benchmark_index 为空则跳过)
        benchmark_df = pd.DataFrame()
        if holding.benchmark_index:
            try:
                benchmark_df = fetch_index_history(holding.benchmark_index)
                print(f"  Fetched {len(benchmark_df)} benchmark records ({holding.benchmark_name})")
            except Exception as e:
                print(f"  WARNING: Benchmark fetch failed ({holding.benchmark_name}): {e}")
                benchmark_df = load_index_history(holding.benchmark_index, data_dir)
                print(f"  Using {len(benchmark_df)} cached benchmark records")
        if not benchmark_df.empty:
            save_index_history(holding.benchmark_index, benchmark_df, data_dir)

        # Analyze
        try:
            analysis = analyze_fund(holding, merged_nav, benchmark_df, prefs)
            # 已清仓（份额=0）的基金不再报止损/减仓类信号——
            # 持仓都没有了，催止损毫无意义；右侧加仓信号不受影响，另行提醒。
            if holding.total_shares <= 0 and analysis.signal_type in ("stop", "reduce", "watch"):
                analysis.signal_type = "hold"
                analysis.signal_urgency = "normal"
                analysis.signal_message = (
                    "已清仓，等待重新入场机会。右侧止跌确认信号出现时会另行提醒。"
                )
            analyses.append(analysis)
            print(f"  Trend: {analysis.trend_light} ({analysis.trend_score}pts) | "
                  f"Signal: {analysis.signal_type} | "
                  f"NAV: {analysis.current_nav:.4f} ({analysis.daily_change_pct:+.2f}%)")

            # Check for extra alert conditions
            if abs(analysis.daily_change_pct) >= abs(prefs.extra_alert_change_threshold):
                alert_type = "drop" if analysis.daily_change_pct < 0 else "surge"
                alerts_to_send.append((analysis, alert_type))

            # Right-side add-position signal（行业基金止跌转涨确认）
            if (reversal_cfg.get("enabled", True)
                    and holding.volatility_profile in reversal_profiles):
                reversal_pushes.extend(
                    _process_reversal_signal(
                        holding, merged_nav, analysis, add_state, reversal_cfg, today,
                    )
                )

            # 跌多买多：净值回撤每跨过一个新档位 → 即时推送买入提醒。
            # 一轮回撤内各档位只触发一次；净值创新高后整轮重置。
            # 默认只对 no_exit（长期持有）基金生效，避免与趋势止损策略冲突；
            # 配置了 codes 时只对列出的代码生效；per_code_tiers 可单独指定某基金的档位。
            fund_tiers = dip_per_code_tiers.get(fund_code) or dip_tiers
            dip_eligible = ((fund_code in dip_codes) if dip_codes else holding.no_exit) \
                or fund_code in dip_per_code_tiers
            if dip_enabled and dip_eligible and fund_tiers:
                nav_db = merged_nav.set_index("date")["nav"]
                if not isinstance(nav_db.index, pd.DatetimeIndex):
                    nav_db.index = pd.to_datetime(nav_db.index)
                status = compute_dip_buy_status(nav_db, {**dip_cfg, "tiers": fund_tiers})
                analysis.dip_buy_info = dict(status)
                st = dip_state.get(fund_code, {"max_tier": 0})
                old_max = int(st.get("max_tier", 0))
                new_max = 0
                for i, t in enumerate(fund_tiers, start=1):
                    if i > old_max and status["drawdown_pct"] <= t.get("dd_pct", 0):
                        new_max = i
                if new_max > 0:
                    st = {"max_tier": new_max, "last_trigger_date": today.isoformat()}
                    dip_state[fund_code] = st
                    analysis.dip_buy_info["just_triggered"] = new_max
                    tier = fund_tiers[new_max - 1]
                    left = fund_tiers[new_max:]
                    title = f"📉 买入提醒：{holding.fund_name} 回撤 {status['drawdown_pct']:.1f}%"
                    content = "\n".join([
                        f"{fund_code} 净值距60日高点已回撤 **{status['drawdown_pct']:.1f}%**"
                        f"（高点 {status['high_ref']:.4f}，当前净值 {analysis.current_nav:.4f}）",
                        "",
                        f"按「跌多买多」计划，触发第 {new_max}/{len(fund_tiers)} 档："
                        f"买入 **¥{tier['amount']:,}**",
                        "",
                    ])
                    if left:
                        nxt = left[0]
                        content += (f"下一档：回撤达 {abs(nxt['dd_pct']):.0f}% 时买入 "
                                    f"¥{nxt['amount']:,}")
                    else:
                        content += "全部档位已触发，耐心持有等待反弹；若继续深跌，评估是否追加计划外资金。"
                    content += ("\n\n⚠️ QDII 净值 T+2 披露，提醒以最新净值为准；"
                                "实际买入可在行情下跌当日自行择时执行。")
                    dip_buy_pushes.append((title, content))
                elif status["drawdown_pct"] >= 0 and old_max > 0:
                    # 净值创新高 → 本轮结束，档位重置
                    dip_state[fund_code] = {"max_tier": 0,
                                            "last_trigger_date": st.get("last_trigger_date", "")}
                # 报告展示：当前已触发档位 + 下一档
                cur_max = int(dip_state.get(fund_code, {}).get("max_tier", 0))
                analysis.dip_buy_info["fired_count"] = cur_max
                analysis.dip_buy_info["tier_count"] = len(fund_tiers)
                analysis.dip_buy_info["next_tier"] = (
                    fund_tiers[cur_max] if cur_max < len(fund_tiers) else None
                )
        except Exception as e:
            print(f"  ERROR in analysis: {e}")
            import traceback
            traceback.print_exc()
            from src.analyzer import FundAnalysis
            fa = FundAnalysis(
                fund_code=fund_code,
                fund_name=holding.fund_name,
                data_quality="stale",
                trend_explanation=f"分析计算异常: {e}",
                signal_type="hold",
                signal_message="今日分析异常，请稍后手动重试。",
            )
            analyses.append(fa)

    # Persist holdings if any pending lots were confirmed
    if holdings_changed:
        save_holdings(holdings)
        print("[Main] Holdings updated with confirmed NAVs")

    # ---- 5.5 相同信号推送冷却 ----
    # 告警/警告类信号（止损/减仓/关注/止盈）在冷却期内折叠为一行摘要，
    # 不每天重复同一份催促；信号变化或冷却期结束后恢复完整详情。
    quiet_funds = {}
    cooldown = int(prefs.signal_cooldown_days or 3)
    for a in analyses:
        if a is None or a.signal_urgency not in ("warning", "alert"):
            continue
        code = a.fund_code
        prev = signal_state.get(code)
        if not prev or prev.get("signal_type") != a.signal_type:
            continue  # 首次出现或信号已变化 → 完整推送
        try:
            days_since = cal.trading_days_between(
                date.fromisoformat(prev.get("last_push")), today)
        except (ValueError, TypeError):
            continue
        if days_since <= cooldown:
            a.signal_since = prev.get("since") or prev.get("last_push") or today.isoformat()
            quiet_funds[code] = prev
    if quiet_funds:
        print(f"[Main] Signal cooldown: {len(quiet_funds)} fund(s) quiet "
              f"({', '.join(quiet_funds)})")

    # ---- 6. Portfolio summary ----
    portfolio = compute_portfolio_summary(analyses)
    print(f"\n[Portfolio] Active: {portfolio.get('active_count', 0)}, "
          f"Pending: {portfolio.get('pending_count', 0)}")

    # ---- 7. Build report ----
    is_friday = cal.is_friday(today)
    week_data = _collect_weekly_data(holdings, analyses, today) if is_friday else None

    # Fetch macro data
    macro_brief = ""
    try:
        macro_snap = fetch_macro_snapshot()
        macro_brief = build_macro_brief(macro_snap)
        print(f"[Macro] LPR 1Y={macro_snap.lpr_1y}%, 5Y={macro_snap.lpr_5y}%, "
              f"M2={macro_snap.m2_yoy}%")
    except Exception as e:
        print(f"[Macro] WARNING: Could not fetch macro data: {e}")

    report = build_daily_report(
        today, analyses, holdings, portfolio,
        is_friday=is_friday,
        missed_days=missed,
        bot_name="大基吧",
        week_data=week_data,
        macro_brief=macro_brief,
        quiet_funds=quiet_funds,
    )

    # ---- 8. Push daily report (single push to avoid rate limiting) ----
    # Extra alerts are incorporated into the main report when applicable
    date_str = f"{today.year}年{today.month:02d}月{today.day:02d}日"
    title = f"大基吧日报 - {date_str}"
    ok = send_report(user_token, title, report, template="markdown")
    time.sleep(2)  # Respect rate limit between pushes

    if not ok:
        print("WARNING: Failed to send report via PushPlus (data saved, will retry on next run)")
        run_state["failed_runs"] = run_state.get("failed_runs", 0) + 1
    else:
        print("Report pushed successfully!")
        run_state["failed_runs"] = 0

    # ---- 9. Right-side add-position immediate pushes (tier trigger / invalidate) ----
    for push_title, push_content in reversal_pushes:
        print(f"[Reversal] Push: {push_title}")
        send_alert(user_token, push_title, push_content)
        time.sleep(2)  # Respect rate limit between pushes

    # ---- 9.5 跌多买多档位触发推送 ----
    for push_title, push_content in dip_buy_pushes:
        print(f"[DipBuy] Push: {push_title}")
        send_alert(user_token, push_title, push_content)
        time.sleep(2)  # Respect rate limit between pushes

    # ---- 9.6 月末定投兜底提醒 ----
    # 逢跌买多（dip_buy）优先触发买入；若整月未触发，本月最后一个交易日
    # （下一交易日若在下月）推一条兜底提醒，避免错过月度积累。
    dca_cfg = prefs.monthly_dca or {}
    if dca_cfg.get("enabled", False):
        nxt_td = cal.next_trading_day(today)
        is_last_td = (nxt_td is None or nxt_td.month != today.month)
        if is_last_td:
            total = float(dca_cfg.get("amount_cny", 0))
            allocs = dca_cfg.get("allocations", [])
            if total > 0 and allocs:
                lines = [f"📅 月末兜底：本月 {allocs[0].get('fund_name', '')} 的 ¥{total:,.0f}",
                         "如本月已触发 -3% 跌买并买入，请忽略本条。", ""]
                for al in allocs:
                    amt = total * float(al.get("weight", 0))
                    lines.append(f"否则：今天是本月最后一个交易日，"
                                 f"15:00 前买入 ¥{amt:,.0f}（{al.get('fund_code', '')}）")
                lines += [
                    "",
                    "规则：跌买优先触发，月末兜底；金额固定，不择时不追加。",
                ]
                send_alert(user_token, "📅 月末定投兜底提醒", "\n".join(lines))
                print(f"[DCA] Month-end fallback pushed (¥{total:,.0f})")
                time.sleep(2)  # Respect rate limit between pushes

    # ---- 10. Update run state ----
    run_state["last_run_date"] = today.isoformat()
    if ok:
        run_state["last_success_date"] = today.isoformat()
    run_state["total_runs"] = run_state.get("total_runs", 0) + 1
    if run_state.get("first_run_date") is None:
        run_state["first_run_date"] = today.isoformat()

    # 信号推送状态：仅在推送成功时更新；冷却期内的基金保持原状，
    # 这样"上次完整推送日"始终以用户真正收到的报告为准。
    if ok:
        today_iso = today.isoformat()
        for a in analyses:
            if a is None or a.fund_code in quiet_funds:
                continue
            code = a.fund_code
            prev = signal_state.get(code) or {}
            same_signal = prev.get("signal_type") == a.signal_type
            signal_state[code] = {
                "signal_type": a.signal_type,
                "since": prev.get("since", today_iso) if same_signal else today_iso,
                "last_push": today_iso,
            }
        save_signal_state(signal_state, data_dir)

    save_run_state(run_state, data_dir)
    save_add_signal_state(add_state, data_dir)
    save_dip_buy_state(dip_state, data_dir)

    print(f"\n{'='*50}")
    print(f"Run complete. Funds: {len(analyses)}, Alerts: {len(alerts_to_send)}, "
          f"Missed: {len(missed) if missed else 0}")
    print(f"{'='*50}\n")

    # Always exit 0 — graph data is saved; push failure is logged but not fatal


if __name__ == "__main__":
    main()
