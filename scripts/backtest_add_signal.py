"""
回测右侧加仓信号：用本地历史净值数据模拟状态机，统计信号质量。

用法：
    python scripts/backtest_add_signal.py [--start 2024-06-01] [--fund 018927]

统计口径：
  - 信号数：第1档触发次数、推进到2/3档的次数、作废次数
  - 质量：每次第1档触发后，模拟加仓 20% 仓位，看 T+5/T+10/T+20 个交易日
    的收益（只统计到信号作废为止的收益，避免"死扛"偏差）
"""
import json
import sys
from pathlib import Path

# Fix Unicode output on Windows (GBK terminal → UTF-8)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analyzer import compute_reversal_signals, update_add_signal_state
from src.config import UserPreferences


def load_nav(fund_code: str) -> pd.DataFrame:
    path = ROOT / "data" / "nav_history" / f"{fund_code}.json"
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)
    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["nav"] = pd.to_numeric(df["nav"])
    return df.sort_values("date").reset_index(drop=True)


def backtest_fund(fund_code: str, fund_name: str, cfg: dict,
                  start: str) -> dict:
    df = load_nav(fund_code)
    nav_series = df.set_index("date")["nav"]
    nav_series.index = pd.to_datetime(nav_series.index)
    start_ts = pd.Timestamp(start)

    state = {"status": "idle", "tier": 0, "signal_since": "",
             "reference_nav": 0.0, "recent_low": 0.0}
    events = []
    first_signals = []   # 第1档触发时的 (日期, 当时净值)

    for i in range(35, len(nav_series)):  # MACD 预热 35 根
        date_ts = nav_series.index[i]
        if date_ts < start_ts:
            continue
        date_str = date_ts.date().isoformat()
        nav = float(nav_series.iloc[i])
        window = nav_series.iloc[:i + 1]

        sig = compute_reversal_signals(window, cfg)
        state, evs = update_add_signal_state(state, sig, date_str, nav, cfg)
        for ev in evs:
            events.append({"date": date_str, **ev, "nav": nav,
                           "drawdown": sig.drawdown_pct,
                           "max_dd": sig.max_dd_pct,
                           "hit_count": sig.hit_count})
            if ev["type"] == "tier_triggered" and ev["tier"] == 1:
                first_signals.append(i)

    # 触发后收益统计（T+5/T+10/T+20，止于作废日）
    results = []
    n_total = len(nav_series)
    for idx in first_signals:
        entry_nav = float(nav_series.iloc[idx])
        row = {"date": nav_series.index[idx].date().isoformat(),
               "entry": entry_nav}
        for horizon in (5, 10, 20):
            target = idx + horizon
            if target < n_total:
                fwd = (float(nav_series.iloc[target]) / entry_nav - 1) * 100
            else:
                fwd = None
            row[f"t{horizon}"] = fwd
        results.append(row)

    dfr = pd.DataFrame(results)
    summary = {
        "fund": f"{fund_code} {fund_name}",
        "n_first": len(first_signals),
        "n_tier2": sum(1 for e in events if e["type"] == "tier_triggered" and e["tier"] == 2),
        "n_tier3": sum(1 for e in events if e["type"] == "tier_triggered" and e["tier"] == 3),
        "n_invalidated": sum(1 for e in events if e["type"] == "invalidated"),
    }
    for horizon in ("t5", "t10", "t20"):
        col = dfr[horizon].dropna() if not dfr.empty else pd.Series(dtype=float)
        if len(col):
            summary[f"{horizon}_mean"] = col.mean()
            summary[f"{horizon}_winrate"] = (col > 0).mean() * 100
        else:
            summary[f"{horizon}_mean"] = None
            summary[f"{horizon}_winrate"] = None

    # 作废后的走势：作废日往前看 10 日平均（信号作废是否避免了继续下跌）
    invalidation_fwd = []
    for e in events:
        if e["type"] != "invalidated":
            continue
        # 找到该日 index
        idx = nav_series.index.searchsorted(pd.Timestamp(e["date"]))
        if idx + 10 < n_total:
            fwd = (float(nav_series.iloc[idx + 10]) / e["nav"] - 1) * 100
            invalidation_fwd.append(fwd)
    if invalidation_fwd:
        import statistics
        summary["invalide_after_10d_mean"] = statistics.mean(invalidation_fwd)
    else:
        summary["invalide_after_10d_mean"] = None

    return {"summary": summary, "events": events, "results": dfr}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="回测右侧加仓信号")
    parser.add_argument("--start", default="2024-06-01", help="回测起始日期")
    parser.add_argument("--fund", default="", help="只回测指定基金代码")
    args = parser.parse_args()

    cfg = UserPreferences().reversal_add
    cfg.update(json.loads(open(ROOT / "data" / "preferences.json", encoding="utf-8").read())
               .get("reversal_add", {}))
    print(f"回测配置: 高点{cfg['high_lookback']}日 回撤{cfg['max_drawdown_pct']}% "
          f"最少{cfg['min_signals']}个信号 档位{cfg['tiers']} 起止 {args.start}")
    print()

    funds = {
        "018927": "南方中证电池主题指数C",
        "161725": "招商中证白酒指数A",
        "017140": "华宝中证有色金属ETF联接A",
    }
    if args.fund:
        funds = {args.fund: funds.get(args.fund, args.fund)}

    for code, name in funds.items():
        r = backtest_fund(code, name, cfg, args.start)
        s = r["summary"]
        print(f"📊 {s['fund']}")
        print(f"   第1档触发 {s['n_first']} 次 | 推进2档 {s['n_tier2']} 次 | "
              f"推进3档 {s['n_tier3']} 次 | 作废 {s['n_invalidated']} 次")
        cells = []
        for horizon in ("t5", "t10", "t20"):
            mean, win = s.get(horizon + "_mean"), s.get(horizon + "_winrate")
            if mean is None:
                cells.append(horizon + " —")
            else:
                cells.append(f"{horizon} {mean:+.2f}% (胜率{win:.0f}%)")
        print("   触发后收益：" + " | ".join(cells))
        if s.get("invalide_after_10d_mean") is not None:
            print(f"   作废后10日平均 {s['invalide_after_10d_mean']:+.2f}%（作废是否躲过下跌）")
        if not r["results"].empty:
            print(r["results"].to_string(index=False))
        print()


if __name__ == "__main__":
    main()
