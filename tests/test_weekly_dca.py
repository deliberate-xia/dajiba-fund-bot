"""Regression tests for weekly-DCA auto booking & pending confirmation.

Run: python -X utf8 tests/test_weekly_dca.py   (no pytest dependency)

Scenario: the user runs fixed ¥10 weekday DCA plans (040046 Mon-Wed,
017641 Wed-Fri, fee ¥0.01) and no longer reports confirmations. The bot must
auto-book each executed order at the ORDER DAY's NAV once that day's NAV row
arrives (QDII rows lag 1-2 trading days), and never back-fill the
pre-ledger period covered by a market-value snapshot lot.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from src.config import FundHolding  # noqa: E402
from src.main import _book_weekly_dca, _confirm_pending_lots  # noqa: E402

DCA_CFG = {
    "enabled": True,
    "amount_cny": 10.0,
    "fee_cny": 0.01,
    "plans": [{"fund_code": "017641", "weekdays": [2, 3, 4]}],  # Wed-Fri
}


def holding(lots):
    return FundHolding(
        fund_code="017641", fund_name="t", fund_type="qdii_index",
        volatility_profile="broad_market", benchmark_index="",
        benchmark_name="", cost_lots=lots,
    )


def mkdf(rows):
    # Real pipeline merges akshare rows (datetime.date) with cached json —
    # dates end up as date objects, so _confirm_pending_lots compares correctly.
    df = pd.DataFrame({"date": [pd.to_datetime(d).date() for d, _ in rows],
                       "nav": [n for _, n in rows]})
    return df


def lot(date_, amount, nav=None, shares=None, confirmed=True):
    return {
        "date": date_, "amount_cny": amount, "nav_at_purchase": nav,
        "shares": shares, "nav_confirmed": confirmed, "note": "",
    }


def check(name, cond):
    assert cond, f"FAIL: {name}"
    print(f"ok: {name}")


# ---- 1. Book scheduled weekdays at order-day NAV, net of fee --------------
# Snapshot lot 9/2 (ledger floor); nav rows exist for Wed 9/3 & Fri 9/4
# (Mon 9/7 is not in the plan). Today = 9/8 (Tue).
h = holding([lot("2026-09-02", 49.71, 1.6965, 29.30)])
rows = [("2026-09-03", 1.7000), ("2026-09-04", 1.7050), ("2026-09-07", 1.7100)]
lines = _book_weekly_dca(h, mkdf(rows), DCA_CFG, today=date(2026, 9, 8))
check("books Wed 9/3 and Fri 9/4 (Mon skipped)", len(lines) == 2)
b = next(l for l in h.cost_lots if l["date"] == "2026-09-03")
check("net amount 9.99 (fee 0.01)", b["amount_cny"] == 9.99)
check("shares = 9.99/nav rounded 2dp", b["shares"] == round(9.99 / 1.7, 2))
check("lot date = order day, NAV = that day's row", b["nav_at_purchase"] == 1.7)
check("idempotent second run books nothing", _book_weekly_dca(h, mkdf(rows), DCA_CFG, today=date(2026, 9, 8)) == [])

# ---- 2. Never back-fill before the snapshot floor --------------------------
h2 = holding([lot("2026-09-02", 49.71, 1.6965, 29.30)])
rows2 = [("2026-08-28", 1.6900), ("2026-08-31", 1.6950), ("2026-09-01", 1.6980)]
check("pre-floor weekdays not booked (covered by snapshot)",
      _book_weekly_dca(h2, mkdf(rows2), DCA_CFG, today=date(2026, 9, 2)) == [])

# ---- 3. Disabled plan / fund not in plan books nothing ---------------------
h3 = holding([lot("2026-09-02", 49.71, 1.6965, 29.30)])
off = dict(DCA_CFG, enabled=False)
check("enabled=false stops booking", _book_weekly_dca(h3, mkdf(rows), off, today=date(2026, 9, 8)) == [])
check("fund not in plan stops booking",
      _book_weekly_dca(h3, mkdf(rows), {"enabled": True, "plans": [{"fund_code": "999999", "weekdays": [0]}]},
                       today=date(2026, 9, 8)) == [])

# ---- 4. Pending lot auto-confirms at the first NAV row >= order date ------
# 017641's transitional 9/2 pending (net 9.99): dated-9/2 row arrives late.
h4 = holding([lot("2026-09-02", 49.71, 1.6965, 29.30),
              lot("2026-09-02", 9.99, confirmed=False)])
df4 = mkdf([("2026-09-01", 1.6900), ("2026-09-02", 1.7000)])
check("pending confirmed once its dated NAV lands",
      _confirm_pending_lots(h4, df4) is True)
p = next(l for l in h4.cost_lots if l["amount_cny"] == 9.99)
check("confirmed at dated-9/2 NAV", p["nav_confirmed"] and p["nav_at_purchase"] == 1.7
      and p["shares"] == round(9.99 / 1.7, 2))

# ---- 5. Booked lot does not duplicate a same-day pending -------------------
h5 = holding([lot("2026-09-02", 49.71, 1.6965, 29.30),
              lot("2026-09-03", 9.99, confirmed=False)])  # pending on 9/3
check("existing pending date suppresses booking",
      _book_weekly_dca(h5, mkdf(rows), DCA_CFG, today=date(2026, 9, 8)) == ["2026-09-04 定投自动落账 ¥9.99 @净值1.7050 = 5.86份"])

print("\nAll weekly-DCA checks passed.")
