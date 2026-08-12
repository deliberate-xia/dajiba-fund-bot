"""
Macro data fetcher: PBOC policy rates, LPR, RRR, money supply.
"""
from dataclasses import dataclass
from datetime import date, timedelta

import akshare as ak
import pandas as pd


@dataclass
class MacroSnapshot:
    """Current macro policy snapshot for the daily report."""
    lpr_1y: float = 0.0
    lpr_5y: float = 0.0
    lpr_date: str = ""              # Latest LPR announcement date
    lpr_1y_prev: float = 0.0        # Previous LPR (for change detection)
    lpr_5y_prev: float = 0.0
    lpr_change: str = ""            # "unchanged" | "cut" | "hike"
    rrr_large: float = 0.0          # Latest RRR for large banks
    rrr_date: str = ""
    m2_yoy: float = 0.0             # M2 YoY growth
    m2_date: str = ""
    social_finance_latest: float = 0.0  # Latest monthly (100M CNY)
    sf_date: str = ""
    next_lpr_date: str = ""         # Next expected LPR announcement


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def fetch_macro_snapshot() -> MacroSnapshot:
    """Fetch the latest macro policy indicators."""
    snap = MacroSnapshot()

    # ---- LPR ----
    try:
        lpr_df = ak.macro_china_lpr()
        if not lpr_df.empty:
            latest = lpr_df.iloc[-1]
            prev = lpr_df.iloc[-2] if len(lpr_df) >= 2 else latest
            snap.lpr_1y = float(latest["LPR1Y"])
            snap.lpr_5y = float(latest["LPR5Y"])
            snap.lpr_date = str(latest["TRADE_DATE"])[:10]
            snap.lpr_1y_prev = float(prev["LPR1Y"])
            snap.lpr_5y_prev = float(prev["LPR5Y"])

            if snap.lpr_1y < snap.lpr_1y_prev:
                snap.lpr_change = "cut"
            elif snap.lpr_1y > snap.lpr_1y_prev:
                snap.lpr_change = "hike"
            else:
                snap.lpr_change = "unchanged"
    except Exception:
        pass

    # ---- Next LPR date (20th of current/next month) ----
    today = date.today()
    this_month_20 = date(today.year, today.month, 20)
    if today > this_month_20:
        # Next month's 20th
        if today.month == 12:
            next_month = date(today.year + 1, 1, 20)
        else:
            next_month = date(today.year, today.month + 1, 20)
        snap.next_lpr_date = next_month.isoformat()
    else:
        snap.next_lpr_date = this_month_20.isoformat()

    # ---- RRR (filter stale: only use data from last 10 years) ----
    # NOTE: macro_china_reserve_requirement_ratio() returns rows in
    # DESCENDING order (newest first) — take iloc[0], not iloc[-1].
    try:
        rrr_df = ak.macro_china_reserve_requirement_ratio()
        if not rrr_df.empty:
            latest_rrr = rrr_df.iloc[0]
            cols = list(rrr_df.columns)
            date_col = cols[0]
            rrr_col = cols[3] if len(cols) > 3 else cols[2]
            rrr_date_str = str(latest_rrr[date_col])
            rrr_val = float(latest_rrr[rrr_col])
            if rrr_date_str >= "2018" and 5 < rrr_val < 20:
                snap.rrr_large = rrr_val
                snap.rrr_date = rrr_date_str
    except Exception:
        pass

    # ---- M2 (filter stale: only use data from last 6 months) ----
    # NOTE: macro_china_money_supply() returns rows in DESCENDING order
    # (newest first) — take iloc[0], not iloc[-1].
    try:
        m2_df = ak.macro_china_money_supply()
        if not m2_df.empty:
            latest_m2 = m2_df.iloc[0]
            m2_col = [c for c in m2_df.columns if "M2" in c and "同比" in c]
            date_col = m2_df.columns[0]
            if m2_col:
                m2_date_str = str(latest_m2[date_col])
                # AKShare sometimes returns very old data; only keep recent
                m2_val = float(latest_m2[m2_col[0]])
                if m2_date_str >= "2025" and 0 < m2_val < 30:
                    snap.m2_yoy = m2_val
                    snap.m2_date = m2_date_str
    except Exception:
        pass

    # ---- Social Financing (filter stale) ----
    try:
        sf_df = ak.macro_china_shrzgm()
        if not sf_df.empty:
            latest_sf = sf_df.iloc[-1]
            sf_col = sf_df.columns[1]  # 社会融资规模增量
            sf_date_str = str(latest_sf[sf_df.columns[0]])
            sf_val = float(latest_sf[sf_col])
            # Only keep if data is recent (2024+)
            if sf_date_str >= "2024" and abs(sf_val) < 100000:
                snap.social_finance_latest = sf_val
                snap.sf_date = sf_date_str
    except Exception:
        pass

    return snap


# ---------------------------------------------------------------------------
# Analysis text
# ---------------------------------------------------------------------------

def _fmt_month(raw: str) -> str:
    """Normalise API month strings ('202604', '2026年04月份') → '2026年4月'."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 6:
        return f"{digits[:4]}年{int(digits[4:6])}月"
    return raw


def build_macro_brief(snap: MacroSnapshot) -> str:
    """Generate a concise macro policy analysis in Chinese."""
    lines = [
        "## 🏛️ 宏观风向",
        "",
    ]
    has_any_data = False

    # LPR
    if snap.lpr_1y > 0:
        has_any_data = True
        change_icon = {"cut": "⬇️ 降息", "hike": "⬆️ 加息", "unchanged": "➡️ 持平"}
        icon = change_icon.get(snap.lpr_change, "➡️")
        lines.append(f"**LPR**（{snap.lpr_date}）：1年期 **{snap.lpr_1y}%**，5年期 **{snap.lpr_5y}%**（{icon}）")
        lines.append(f"> 下次报价：{snap.next_lpr_date}")
        lines.append("")

    # RRR（only if data is recent enough）
    if snap.rrr_large > 0:
        has_any_data = True
        lines.append(f"**存款准备金率**（{snap.rrr_date}）：大型机构 **{snap.rrr_large}%**")
        lines.append("")

    # M2（only if data is recent enough）
    if snap.m2_yoy > 0:
        has_any_data = True
        direction = "宽松" if snap.m2_yoy > 10 else "偏紧" if snap.m2_yoy < 8 else "中性"
        lines.append(f"**M2 同比**（{_fmt_month(snap.m2_date)}）：**{snap.m2_yoy}%**（{direction}）")
        lines.append("")

    # Social financing（only if data is recent enough）
    if snap.social_finance_latest != 0:
        has_any_data = True
        sf_billion = snap.social_finance_latest / 10000
        strength = "强劲" if sf_billion > 3 else "温和" if sf_billion > 1 else "偏弱"
        sf_label = _fmt_month(snap.sf_date)
        lines.append(f"**社会融资增量**（{sf_label}）：**{sf_billion:.2f} 万亿**（{strength}）")
        lines.append("")

    if not has_any_data:
        lines.append("> ⚠️ 宏观数据暂不可用，跳过分析")
        lines.extend(["", "---", ""])
        return "\n".join(lines)

    # Brief analysis
    lines.append("### 📝 简析")
    lines.append("")

    points = []

    # LPR trend
    if snap.lpr_change == "cut":
        points.append("- 🔥 LPR 下调，降息通道确认，利好股市尤其是成长板块（有色、电池）")
    elif snap.lpr_change == "unchanged" and snap.lpr_date:
        lpr_date = date.fromisoformat(snap.lpr_date)
        days_since = (date.today() - lpr_date).days
        if days_since > 60:
            points.append(f"- LPR 已连续持平 {days_since} 天，市场对降息预期升温，关注 {snap.next_lpr_date} 报价窗口")
        else:
            points.append(f"- 利率维持稳定，下次 LPR 报价 {snap.next_lpr_date}，关注是否调整")

    # M2 analysis (only if we have recent data)
    if snap.m2_yoy > 10:
        points.append("- 流动性格局偏宽，对权益资产形成支撑")
    elif snap.m2_yoy > 0 and snap.m2_yoy <= 8:
        points.append("- M2 增速偏低，关注央行是否加大投放力度")

    # Social finance (only if we have recent data)
    if snap.social_finance_latest > 40000:
        points.append("- 社融大超预期，实体经济融资需求旺盛，顺周期板块（有色、白酒）受益")
    elif snap.social_finance_latest > 0 and snap.social_finance_latest < 10000:
        points.append("- 社融偏弱，内需不足，消费板块需要更多耐心")

    # Key dates reminder
    if snap.next_lpr_date:
        nd = date.fromisoformat(snap.next_lpr_date)
        days_to = (nd - date.today()).days
        if 0 <= days_to <= 7:
            points.append(f"- ⚠️ 距下次 LPR 报价仅 {days_to} 天，政策敏感期，不宜重仓操作")

    # Portfolio-specific
    points.append("- 有色金属：对利率最敏感，降息/宽信用环境下弹性最大")
    points.append("- 白酒/消费：LPR 下调 → 房贷利率降 → 居民可支配收入改善 → 利好消费")

    if not points:
        points.append("- 宏观数据暂无重大变化，维持现有策略")

    lines.extend(points)
    lines.extend(["", "---", ""])
    return "\n".join(lines)
