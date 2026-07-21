"""
Main orchestrator: fetch → analyze → report → push → save.

Entry point for GitHub Actions workflow.
"""
import os
import sys
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
    check_latest_nav,
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
from src.reporter import build_daily_report, build_extra_alert
from src.pusher import send_report, send_alert


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_data_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data"


def _check_pending_confirmations(
    holdings: dict[str, FundHolding],
    data_dir: Path,
) -> bool:
    """
    Check if any pending cost lots now have confirmed NAVs.
    Returns True if any lot was confirmed (holdings mutated in-place).
    """
    changed = False
    today = date.today()

    for code, holding in holdings.items():
        if not holding.skip_tracking:
            continue

        for lot in holding.cost_lots:
            if lot.get("nav_confirmed", True):
                continue

            print(f"[{code}] Checking pending confirmation for lot {lot.get('date')}...")
            latest = check_latest_nav(code)
            if latest is None:
                print(f"[{code}]   NAV not yet available")
                continue

            # The lot date is the purchase submission date.
            # After 15:00 cutoff, NAV is set on the NEXT trading day.
            # We check if the latest NAV date is >= the expected confirmation date.
            lot_date = date.fromisoformat(lot["date"]) if lot.get("date") else None
            if lot_date and latest["date"] >= lot_date:
                lot["nav_at_purchase"] = latest["nav"]
                lot["shares"] = round(lot["amount_cny"] / latest["nav"], 2)
                lot["nav_confirmed"] = True
                print(f"[{code}]   ✅ NAV confirmed: {latest['nav']} on {latest['date']}")

        # If all lots confirmed, enable tracking
        if not holding.has_pending_lots:
            holding.skip_tracking = False
            print(f"[{code}] All lots confirmed — tracking enabled.")

        changed = True

    return changed


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

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

    # ---- 3. Check pending confirmations ----
    changed = _check_pending_confirmations(holdings, data_dir)
    if changed:
        save_holdings(holdings)
        print("[Main] Holdings updated with confirmed NAVs")

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

    # ---- 6. Portfolio summary ----
    portfolio = compute_portfolio_summary(analyses)
    print(f"\n[Portfolio] Active: {portfolio.get('active_count', 0)}, "
          f"Pending: {portfolio.get('pending_count', 0)}")

    # ---- 7. Build report ----
    is_friday = cal.is_friday(today)
    report = build_daily_report(
        today, analyses, holdings, portfolio,
        is_friday=is_friday,
        missed_days=missed,
        bot_name="大基吧",
    )

    # ---- 8. Push extra alerts first ----
    for analysis, alert_type in alerts_to_send:
        alert_msg = build_extra_alert(
            analysis.fund_code, analysis.fund_name,
            analysis.daily_change_pct, alert_type,
        )
        title = f"基金异动提醒 - {analysis.fund_name}"
        ok = send_alert(user_token, title, alert_msg, topic_token=topic_token)
        if ok:
            print(f"[Alert] Sent: {title}")
        else:
            print(f"[Alert] FAILED: {title}")

    # ---- 9. Push daily report ----
    date_str = f"{today.year}年{today.month:02d}月{today.day:02d}日"
    title = f"大基吧日报 - {date_str}"
    ok = send_report(user_token, title, report, topic_token=topic_token, template="markdown")

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
