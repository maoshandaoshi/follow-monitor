# -*- coding: utf-8 -*-
"""report_lines 单元回归(全 mock, 无网络, 无推送)。

口径(2026-11-05 用户定稿): 持仓表涨跌 = 持仓盈亏(现价/成本-1),
成本优先盈立快照(latest.costs), 兜底 Alpaca avg_entry_price。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import alpaca_trade as A  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def base_state():
    return {
        "race": {"t0": "2026-10-21 16:00 NY", "follow_base": 148776.86,
                 "alpaca_base": 90545.05},
        "targets": {"KWEB": 25.95, "ISRG": 19.73, "GDX": 16.95,
                    "ABNB": 16.02, "TAN": 15.28, "YINN": 5.0},
        "follow_last": {},
    }


def mock_pos(pairs):
    """pairs: {sym: (current_price, avg_entry)}"""
    A.positions_px = lambda: {s: px for s, (px, _) in pairs.items()}
    A.api_get = lambda path, params=None: (
        [{"symbol": s, "qty": "100", "current_price": str(px),
          "avg_entry_price": str(avg)} for s, (px, avg) in pairs.items()]
        if path == "/positions" else [])


# ---- 用例 1: 正常 —— 盈立成本/现价优先 ----
st = base_state()
latest = {"total_asset": 141287.09,
          "costs": {"KWEB": 23.45, "ISRG": 478.77},
          "prices": {"KWEB": 24.98, "ISRG": 478.99}}
mock_pos({"KWEB": (24.89, 23.469), "ISRG": (478.85, 481.868),
          "GDX": (86.68, 82.331), "ABNB": (175.49, 177.86),
          "TAN": (46.18, 44.326), "YINN": (26.36, 25.561)})
lines = A.report_lines(st, latest, 85013.81)
kweb = [x for x in lines if x.startswith("KWEB")][0]
isrg = [x for x in lines if x.startswith("ISRG")][0]
gdx = [x for x in lines if x.startswith("GDX")][0]
check("KWEB 用盈立成本", kweb.split()[-1] == "+6.5%", kweb)   # 24.98/23.45
check("ISRG 用盈立成本", isrg.split()[-1] == "+0.0%", isrg)   # 478.99/478.77
check("GDX 缺盈立成本兜底 Alpaca avg", gdx.split()[-1] == "+5.3%", gdx)  # 86.68/82.331
check("总收益行存在", any(x.startswith("盈立 -5.03%") for x in lines), str(lines[:2]))
check("领先行存在", any("领先" in x for x in lines))

# ---- 用例 2: latest 无 costs/prices(旧 latest.json / 新 runner) ----
st = base_state()
lines = A.report_lines(st, {"total_asset": 141287.09}, 85013.81)
yinn = [x for x in lines if x.startswith("YINN")][0]
check("全缺成本兜底 Alpaca avg", yinn.split()[-1] == "+3.1%", yinn)  # 26.36/25.561

# ---- 用例 3: 快照兜底 note ----
st = base_state()
st["follow_last"] = {"asset": 140848.48, "ts": "2026-11-04 20:00:28"}
lines = A.report_lines(st, {}, 85013.81)
check("快照兜底标注", any("(快照 2026-11-04 20:00:28)" in x for x in lines), str(lines[:2]))

# ---- 用例 4: targets 缺失 -> 按持仓市值现算比例 ----
st = base_state()
st["targets"] = {}
pairs = {"KWEB": (24.98, 23.45), "GDX": (86.68, 82.25)}
mock_pos(pairs)
# equity=1000: KWEB 市值 2498 -> 250%? 换成可控数字
A.positions_px = lambda: {"KWEB": 50.0, "GDX": 50.0}
A.api_get = lambda path, params=None: (
    [{"symbol": "KWEB", "qty": "10", "current_price": "50",
      "avg_entry_price": "45"},
     {"symbol": "GDX", "qty": "10", "current_price": "50",
      "avg_entry_price": "45"}] if path == "/positions" else [])
lines = A.report_lines(st, {"total_asset": 0}, 1000.0)
kweb = [x for x in lines if x.startswith("KWEB")][0]
check("缺 targets 现算比例 50%", kweb.split()[1] == "50%", kweb)
check("缺 targets 盈亏用 avg", kweb.split()[-1] == "+11.1%", kweb)  # 50/45

# ---- 用例 5: 双缺不崩 ----
st = base_state()
st["race"] = {}
lines = A.report_lines(st, {}, 0.0)
check("双缺不崩", lines == ["(缺双方数据, 本轮不比)"], str(lines))

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} -> {FAILS}")
    sys.exit(1)
print("ALL PASS")
