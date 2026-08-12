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
)
from src.analyzer import analyze_fund, compute_portfolio_summary
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
    holdings_changed = False
    is_first_run = (run_state.get("total_runs", 0) == 0)

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

        # Fetch benchmark index
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
            analyses.append(analysis)
            print(f"  Trend: {analysis.trend_light} ({analysis.trend_score}pts) | "
                  f"Signal: {analysis.signal_type} | "
                  f"NAV: {analysis.current_nav:.4f} ({analysis.daily_change_pct:+.2f}%)")

            # Check for extra alert conditions
            if abs(analysis.daily_change_pct) >= abs(prefs.extra_alert_change_threshold):
                alert_type = "drop" if analysis.daily_change_pct < 0 else "surge"
                alerts_to_send.append((analysis, alert_type))
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

    # ---- 10. Update run state ----
    run_state["last_run_date"] = today.isoformat()
    if ok:
        run_state["last_success_date"] = today.isoformat()
    run_state["total_runs"] = run_state.get("total_runs", 0) + 1
    if run_state.get("first_run_date") is None:
        run_state["first_run_date"] = today.isoformat()

    save_run_state(run_state, data_dir)

    print(f"\n{'='*50}")
    print(f"Run complete. Funds: {len(analyses)}, Alerts: {len(alerts_to_send)}, "
          f"Missed: {len(missed) if missed else 0}")
    print(f"{'='*50}\n")

    # Always exit 0 — graph data is saved; push failure is logged but not fatal


if __name__ == "__main__":
    main()
