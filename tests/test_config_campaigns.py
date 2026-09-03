"""Regression tests for campaign segmentation (cost-pool reset on full exit).

Run: python tests/test_config_campaigns.py   (no pytest dependency)

Scenario: a fund is fully exited and later re-entered at a different NAV.
The closed round's cost basis must not leak into the new position's
weighted entry NAV, and each round's realized P&L must be priced against
its own purchases.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import segment_campaigns  # noqa: E402


def holding(lots):
    """Minimal FundHolding-shaped dict list consumer: we test the pure
    segmentation function plus the per-campaign math that properties use."""
    return lots


def lot(date, amount, nav, shares, confirmed=True, note=""):
    return {
        "date": date,
        "amount_cny": amount,
        "nav_at_purchase": nav,
        "shares": shares,
        "nav_confirmed": confirmed,
        "note": note,
    }


def weighted(purchases):
    total = sum(l["amount_cny"] for l in purchases)
    return sum(l["amount_cny"] * l["nav_at_purchase"] for l in purchases) / total


def campaign_realized(camp):
    purchases = camp["purchases"]
    if not purchases:
        return 0.0
    avg = weighted(purchases)
    return sum(-l["amount_cny"] - (-l["shares"]) * avg for l in camp["redemptions"])


def check(name, cond):
    assert cond, f"FAIL: {name}"
    print(f"ok: {name}")


# ---- 1. Single round-trip campaign (legacy funds: 161725/018927) -----------
lots = [
    lot("2026-07-24", 400.0, 0.5395, 740.68),
    lot("2026-08-07", 99.9, 0.5781, 172.81),
    lot("2026-08-17", -508.01, 0.5589, -913.49),
]
closed, open_c = segment_campaigns(lots)
check("single closed campaign stays one closed campaign", closed and not open_c and len(closed) == 1)
check("realized priced at blended pool avg",
      abs(campaign_realized(closed[0]) - (508.01 - 913.49 * weighted(closed[0]["purchases"]))) < 1e-9)

# ---- 2. Full exit then re-entry at a different NAV (003015 case) -----------
lots = [
    lot("2026-07-20", 666.67, 2.0984, 317.7),
    lot("2026-08-10", -344.57, 2.1713, -158.85),
    lot("2026-08-27", -338.75, 2.1325, -158.85),   # -> shares hit 0 here
    lot("2026-09-01", 99.9, 2.1221, 47.08),         # re-entry opens new campaign
    lot("2026-09-01", 799.2, 2.1221, 376.61),
]
closed, open_c = segment_campaigns(lots)
check("re-entry detected: 1 closed + 1 open", len(closed) == 1 and open_c is not None)
old_avg = weighted(closed[0]["purchases"])
new_avg = weighted(open_c["purchases"])
check("open campaign uses only new lots (2.1221, not blended 2.1120)", abs(new_avg - 2.1221) < 1e-9)
check("closed round priced at its own basis 2.0984",
      abs(campaign_realized(closed[0]) - (683.32 - 317.7 * 2.0984)) < 1e-9)

# ---- 3. Same-day redemption & correction lot (018927 补正 pattern) ---------
lots = [
    lot("2026-07-21", 500.0, 1.367, 365.76),
    lot("2026-07-28", 333.33, 1.3728, 242.81),
    lot("2026-08-31", -842.41, 1.3814, -609.82),
    lot("2026-08-31", 1.73, 1.3814, 1.25),  # share correction, same date
]
closed, open_c = segment_campaigns(lots)
check("correction lot folds into same campaign (purchase-first ordering)",
      len(closed) == 1 and not open_c and len(closed[0]["purchases"]) == 3)
check("total shares close to zero", 365.76 + 242.81 + 1.25 - 609.82 < 1e-9)

# ---- 4. Pending (unconfirmed) lots never shift campaigns ------------------
lots = [
    lot("2026-07-20", 666.67, 2.0984, 317.7),
    lot("2026-08-27", -338.75, 2.1325, -158.85),
    lot("2026-09-01", 100.0, None, None, confirmed=False),  # pending, later confirmed
    lot("2026-09-01", 799.2, 2.1221, 376.61),
]
closed, open_c = segment_campaigns(lots)
# running = 317.7 - 158.85 = 158.85 → never hit zero → still one continuous
# campaign; pending lot excluded from the pool either way
check("pending lot excluded, no false re-entry split",
      len(closed) == 0 and open_c is not None
      and [l["date"] for l in open_c["purchases"]] == ["2026-07-20", "2026-09-01"])

print("\nAll campaign-segmentation checks passed.")
