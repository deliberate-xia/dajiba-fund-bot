"""
Markdown report builder: generates the daily fund report in PushPlus-compatible Markdown.
"""
from datetime import date
from typing import Optional


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_pct(value: float, signed: bool = True) -> str:
    """Format a percentage: 0.0523 → '+5.23%' or '5.23%'."""
    if signed:
        return f"{value:+.2f}%"
    return f"{value:.2f}%"


def fmt_cny(value: float, signed: bool = True) -> str:
    """Format CNY: 1234.56 → '+¥1,234.56'."""
    if signed:
        sign = "+" if value >= 0 else ""
        return f"{sign}¥{abs(value):,.2f}"
    return f"¥{value:,.2f}"


def _traffic_icon(light: str) -> str:
    return {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(light, "⚪")


def _signal_label(signal_type: str) -> str:
    labels = {
        "add": "可加仓",
        "hold": "持有观望",
        "reduce": "建议减仓",
        "watch": "密切关注",
        "stop": "🔴 止损",
        "profit": "🎉 止盈",
        "pending": "⏳ 等待数据",
    }
    return labels.get(signal_type, signal_type)


def _weekday_cn(d: date) -> str:
    """Return Chinese weekday name."""
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return names[d.weekday()]


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------

def _build_header(d: date, bot_name: str, missed_days: list | None) -> str:
    lines = [
        f"🤖 **{bot_name} · 每日投资简报**",
        f"📅 {d.year}年{d.month:02d}月{d.day:02d}日 {_weekday_cn(d)}",
        "",
        "---",
        "",
    ]
    if missed_days:
        days_str = "、".join(md.strftime("%m/%d") for md in missed_days)
        lines.extend([
            "### ⚠️ 运行提醒",
            f"上次成功推送后有 {len(missed_days)} 个交易日漏跑（{days_str}），数据已自动补齐。",
            "",
            "---",
            "",
        ])
    return "\n".join(lines)


def _build_portfolio_overview(analyses: list, portfolio: dict, holdings: dict) -> str:
    lines = [
        "## 📊 持仓总览",
        "",
        "| 基金 | 净值 | 日涨跌 | 持有收益 | 信号 |",
        "|------|------|--------|----------|------|",
    ]

    total_invested = 0.0
    total_value = 0.0

    for a in analyses:
        if a is None:
            continue

        holding = holdings.get(a.fund_code)
        if holding is None:
            continue

        if a.signal_type == "pending":
            nav_str = "⏳ 待确认"
            change_str = "—"
            pnl_str = "—"
            signal_str = "⏳ 等待"
        else:
            nav_str = f"{a.current_nav:.4f}"
            change_str = fmt_pct(a.daily_change_pct)
            shares = holding.total_shares
            if shares > 0:
                mkt_value = a.current_nav * shares
                invested = holding.total_invested
                pnl = mkt_value - invested
                pnl_pct = (mkt_value / invested - 1) * 100 if invested > 0 else 0
                pnl_str = f"{fmt_cny(pnl)} ({fmt_pct(pnl_pct)})"
                total_value += mkt_value
                total_invested += invested
            else:
                pnl_str = "—"
            signal_str = f"{_traffic_icon(a.trend_light)} {_signal_label(a.signal_type)}"

        lines.append(
            f"| {a.fund_code} {a.fund_name} "
            f"| {nav_str} | {change_str} | {pnl_str} | {signal_str} |"
        )

    lines.append("")
    if total_invested > 0:
        total_pnl = total_value - total_invested
        total_pnl_pct = (total_value / total_invested - 1) * 100
        lines.append(
            f"💰 总市值：{fmt_cny(total_value, signed=False)} "
            f"| 总投入：{fmt_cny(total_invested, signed=False)} "
            f"| 总盈亏：**{fmt_cny(total_pnl)} ({fmt_pct(total_pnl_pct)})**"
        )
    else:
        lines.append("💰 总市值：待确认 | 总投入：¥1,500.00")

    lines.extend(["", "---", ""])
    return "\n".join(lines)


def _build_fund_detail(a, holding) -> str:
    """Build the per-fund detail section."""
    if a is None:
        return ""

    if a.signal_type == "pending":
        pending_note = ""
        if holding and holding.cost_lots:
            unconfirmed = [l for l in holding.cost_lots if not l.get("nav_confirmed", False)]
            if unconfirmed:
                lot = unconfirmed[0]
                pending_note = f"该基金于 {lot.get('date', '近期')} 申购，预计确认净值后将自动纳入分析。"
        return "\n".join([
            f"## 🔍 {a.fund_code} {a.fund_name}",
            "",
            "⏳ **等待净值确认**",
            "",
            pending_note,
            "",
            "---",
            "",
        ])

    icon = _traffic_icon(a.trend_light)
    lines = [
        f"## 🔍 {a.fund_code} {a.fund_name}",
        "",
        f"### {icon} 趋势信号：{'偏多' if a.trend_light == 'green' else '偏空' if a.trend_light == 'red' else '震荡'}",
        "",
        "| 指标 | 数值 |",
        "|------|------|",
        f"| 当前净值 | **{a.current_nav:.4f}** |",
        f"| 日涨跌 | **{fmt_pct(a.daily_change_pct)}** |",
    ]

    if holding:
        shares = holding.total_shares
        if shares > 0:
            lines.append(f"| 持有份额 | {shares:,.2f} 份 |")
            lines.append(f"| 成本净值 | {a.entry_nav_weighted:.4f} |")
            mkt_value = a.current_nav * shares
            invested = holding.total_invested
            pnl = mkt_value - invested
            pnl_pct = (mkt_value / invested - 1) * 100
            lines.append(f"| 持仓盈亏 | **{fmt_cny(pnl)} ({fmt_pct(pnl_pct)})** |")

    lines.extend(["", "### 📈 阶段表现", ""])

    bm_name = holding.benchmark_name if holding else "基准"
    lines.append(f"| 周期 | 基金 | {bm_name} | 超额 |")
    lines.append("|------|------|---------|------|")

    for period, f_ret, b_ret in [
        ("近7日", a.return_7d, a.benchmark_return_7d),
        ("近30日", a.return_30d, a.benchmark_return_30d),
        ("近90日", a.return_90d, a.benchmark_return_90d),
    ]:
        if abs(f_ret) < 0.001 and abs(b_ret) < 0.001:
            continue
        delta = f_ret - b_ret
        icon_s = "✅" if delta >= 0 else "❌"
        lines.append(f"| {period} | {fmt_pct(f_ret)} | {fmt_pct(b_ret)} | {icon_s} {fmt_pct(delta)} |")

    lines.extend([
        "",
        "### ⚙️ 风控状态",
        "",
        f"- 🛡️ 止损线：**{a.effective_stop:.4f}**（距当前 {fmt_pct(a.stop_distance_pct)}）",
        f"- 🎯 止盈线：**{a.take_profit_price:.4f}**（还需上涨 {fmt_pct(a.profit_distance_pct)}）",
    ])

    if a.stop_distance_pct > 10:
        lines.append("- ✅ 安全区间运行，未触发风控")
    elif a.stop_distance_pct > 3:
        lines.append("- ⚠️ 正常区间，但建议关注止损距离")
    else:
        lines.append("- 🔴 接近止损线，请密切关注！")

    lines.extend([
        "",
        "### 💡 趋势解读",
        "",
        f"当前净值位于20日均线（{a.ma_20:.4f}）**{'上方' if a.nav_vs_ma20_pct > 0 else '下方'}**。{a.trend_explanation}。",
        "",
        f"**{a.beats_benchmark_text}**" if a.beats_benchmark_text else "",
        "",
        "### 🎯 操作建议",
        "",
        f"> {a.signal_message}",
        "",
    ])

    # Extra: idle cash timing tip
    if a.signal_type == "add" and a.trend_light == "green":
        lines.append("💡 **闲钱提示**：当前处于相对低位且趋势向好，如有闲钱可考虑小额加仓。")
        lines.append("")

    lines.extend(["---", ""])
    return "\n".join(lines)


def _build_footer(bot_name: str) -> str:
    lines = [
        f"> ⚠️ 本报告由{bot_name}自动生成，仅供参考，不构成投资建议。",
        "> 基金投资有风险，过往业绩不预示未来表现。请根据自身风险承受能力独立决策。",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Weekly review (Friday only)
# ---------------------------------------------------------------------------

def _build_weekly_review(week_data: dict) -> str:
    """
    Build the weekly operation review section.

    week_data expected keys:
      - week_label: str (e.g. "7/20-7/24")
      - operations: list[dict] with keys: date, fund_name, action, amount, evaluation
      - fund_weekly: list[dict] with keys: fund_name, weekly_return_pct, cumulative_pnl_pct, trend_light, comment
      - good_moves: list[str]
      - concerns: list[str]
      - next_week_notes: list[str]
      - total_weekly_pnl_pct: float
    """
    operations = week_data.get("operations", [])
    fund_weekly = week_data.get("fund_weekly", [])
    good_moves = week_data.get("good_moves", [])
    concerns = week_data.get("concerns", [])
    next_week_notes = week_data.get("next_week_notes", [])
    week_label = week_data.get("week_label", "")
    total_pnl = week_data.get("total_weekly_pnl_pct", 0.0)

    lines = [
        f"## 📋 本周操作复盘（{week_label}）",
        "",
    ]

    # ---- Portfolio weekly P&L ----
    pnl_emoji = "📈" if total_pnl >= 0 else "📉"
    lines.append(f"{pnl_emoji} **组合本周盈亏：{total_pnl:+.2f}%**")
    lines.append("")

    # ---- Operations review ----
    if operations:
        lines.extend([
            "### 🔄 操作明细",
            "",
            "| 日期 | 基金 | 操作 | 金额 | 评价 |",
            "|------|------|------|------|------|",
        ])
        for op in operations:
            lines.append(
                f"| {op['date']} | {op['fund_name']} | {op['action']} "
                f"| ¥{op['amount']:,.0f} | {op['evaluation']} |"
            )
        lines.append("")
    else:
        lines.extend([
            "### 🔄 操作明细",
            "",
            "本周无操作，静观其变也是一种策略。",
            "",
        ])

    # ---- Per-fund weekly performance ----
    lines.extend([
        "### 📊 各基金本周表现",
        "",
        "| 基金 | 本周涨跌 | 累计盈亏 | 趋势 | 点评 |",
        "|------|----------|----------|------|------|",
    ])
    for fw in fund_weekly:
        icon = _traffic_icon(fw.get("trend_light", "yellow"))
        lines.append(
            f"| {fw['fund_name']} | {fmt_pct(fw.get('weekly_return_pct', 0))} "
            f"| {fmt_pct(fw.get('cumulative_pnl_pct', 0))} "
            f"| {icon} | {fw.get('comment', '')} |"
        )
    lines.append("")

    # ---- Overall evaluation ----
    lines.append("### 🧠 总体评价")
    lines.append("")

    if good_moves:
        lines.append("**✅ 正确的：**")
        for g in good_moves:
            lines.append(f"- {g}")
        lines.append("")

    if concerns:
        lines.append("**⚠️ 需要注意的：**")
        for c in concerns:
            lines.append(f"- {c}")
        lines.append("")

    # ---- Next week ----
    if next_week_notes:
        lines.append("### 📅 下周关注")
        lines.append("")
        for n in next_week_notes:
            lines.append(f"- {n}")
        lines.append("")

    lines.extend(["---", ""])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main report builder
# ---------------------------------------------------------------------------

def build_daily_report(
    report_date: date,
    analyses: list,
    holdings: dict,
    portfolio: dict,
    is_friday: bool = False,
    missed_days: list[date] | None = None,
    bot_name: str = "大基吧",
    week_data: dict | None = None,
) -> str:
    """
    Generate the complete Markdown daily report.

    If is_friday and week_data is provided, includes a weekly review section
    with operation evaluation and forward-looking notes.
    """
    sections = [
        _build_header(report_date, bot_name, missed_days),
        _build_portfolio_overview(analyses, portfolio, holdings),
    ]

    # Weekly review goes after portfolio overview but before per-fund details
    if is_friday and week_data:
        sections.append(_build_weekly_review(week_data))

    for a in analyses:
        holding_ = holdings.get(a.fund_code) if a else None
        sections.append(_build_fund_detail(a, holding_))

    # Footer: weekend note only if weekly review wasn't included
    if is_friday and not week_data:
        lines = ["## 📅 周末展望", "", "周末为非交易日，下周一晚间将发送下一份日报。", "", "---", ""]
        sections.append("\n".join(lines))

    sections.append(_build_footer(bot_name))
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Extra alert
# ---------------------------------------------------------------------------

def build_extra_alert(
    fund_code: str,
    fund_name: str,
    change_pct: float,
    alert_type: str,   # "drop" or "surge"
) -> str:
    """
    Build a short alert message for extreme daily moves.
    """
    if alert_type == "drop":
        emoji = "⚠️"
        desc = f"单日大跌 {fmt_pct(change_pct)}"
        advice = (
            "建议：\n"
            "1. 不要恐慌性赎回，先确认是否有重大利空\n"
            "2. 检查是否触及止损线\n"
            "3. 关注晚间是否有相关消息\n"
            "4. 明日开盘后根据市场情况决策"
        )
    else:
        emoji = "📊"
        desc = f"单日波动 {fmt_pct(change_pct)}"
        advice = "建议：关注波动原因，评估是否为短期情绪影响。"

    return "\n".join([
        f"{emoji} **紧急提醒：基金异动**",
        "",
        f"**{fund_code} {fund_name}**",
        f"今日变动：{desc}",
        "",
        advice,
        "",
        "--- 大基吧 紧急推送 ---",
    ])
