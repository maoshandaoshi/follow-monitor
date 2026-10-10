# -*- coding: utf-8 -*-
"""
Alpaca 镜像跟单执行器 (paper)
=============================
配合 follow_action.py (监控跟投易) 使用:
  monitor 每轮写 data/latest.json (目标权重 + 跟投总值 + 是否发生调仓)
  本脚本读取后, 在 Alpaca 模拟账户上镜像执行, 并记录"对战收益率"。

执行策略 (被动阶梯限价, 全部客观规则无预测):
  调仓信号到达后:
    0- 5 分钟  买入挂 bid   / 卖出挂 ask    (被动排队, 争取最优价)
    5-10 分钟  买入挂 mid   / 卖出挂 mid    (中间价)
    10-15 分钟 买入挂 ask   / 卖出挂 bid    (主动成交)
    15 分钟后   跟随最新盘口追价, 封顶 = 到达价 +0.6% (买) / -0.6% (卖)
    45 分钟后   封顶放宽到 ±1.5% (保证跟单完成, 优先不脱队)
  每 5 分钟一轮 (由 GitHub Actions cron 驱动), 未成交自动重挂新价格。

流动性护栏 (避免在成交差的票上付出过大冲击成本):
  - 盘口价差 > 3% 的票本轮跳过
  - 订单金额 > 该股 30 日均成交额(ADV) 10% 的票跳过并推送告警(每票每天一次)
  - 不做空: 卖出量以实际持仓为上限

收益率对战 (从 Alpaca 第一次调仓开始计时):
  - 跟投易基准 = 当轮 total_asset; Alpaca 基准 = 当时 equity
  - 每日收盘推送双方收益率对比 + 执行滑点统计
  - 滑点 = (到达价-成交价)/到达价 (买) —— 正值 = 买得比信号到达时便宜

环境变量:
  ALPACA_KEY_ID / ALPACA_SECRET   Alpaca paper 凭证 (必须)
  PUSH_WEBHOOK                   推送地址 (可选)
  MANUAL=1                       手动模式: 只报告状态, 不交易
"""
import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

NY_TZ = ZoneInfo("America/New_York")
DATA_DIR = os.environ.get("DATA_DIR", "data")
LATEST_FILE = os.path.join(DATA_DIR, "latest.json")   # monitor 写入
STATE_FILE = os.path.join(DATA_DIR, "trader.json")    # 本脚本状态(由 workflow commit 回仓)

ALPACA_KEY = os.environ.get("ALPACA_KEY_ID", "").strip()
ALPACA_SECRET = os.environ.get("ALPACA_SECRET", "").strip()
PUSH_WEBHOOK = os.environ.get("PUSH_WEBHOOK", "").strip()
MANUAL = os.environ.get("MANUAL", "").strip().lower() in ("1", "true", "yes")

BASE = "https://paper-api.alpaca.markets/v2"
DATA = "https://data.alpaca.markets/v2"

# ---------------- 执行参数 (客观规则, 可调) ----------------
EP1_MIN, EP2_MIN, EP3_MIN = 5.0, 10.0, 15.0   # 阶梯切换时刻(分钟)
CHASE_CAP = 0.006          # 追价封顶: 到达价 ±0.6%
DESPERATE_MIN = 45.0       # 超过此分钟数进入"必须完成"模式
DESPERATE_CAP = 0.015      # 疯狂上限 ±1.5%
MAX_SPREAD = 0.03          # 盘口价差 > 3% 跳过
MAX_ADV_PART = 0.10         # 订单金额 > ADV 10% 跳过
REPLACE_EPS = 0.001         # 现有挂单价格与新价差 <0.1% 时不重挂(保队列)

SESSION = requests.Session()

def _hdr():
    return {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}


def market_open(now_ny) -> bool:
    if now_ny.weekday() >= 5:
        return False
    t = now_ny.hour * 60 + now_ny.minute
    return 9 * 60 + 30 <= t < 16 * 60


def push(title: str, text: str):
    if not PUSH_WEBHOOK:
        print(f"[PUSH-DISABLED] {title}\n{text}")
        return
    try:
        url = PUSH_WEBHOOK
        if "sctapi.ftqq.com" in url:  # Server酱
            r = SESSION.post(url, json={"title": title, "desp": text}, timeout=15)
        elif "qyapi.weixin.qq.com" in url:  # 企业微信群机器人
            r = SESSION.post(url, json={"msgtype": "markdown",
                                        "markdown": {"content": f"**{title}**\n{text}"}},
                             timeout=15)
        elif "oapi.dingtalk.com" in url:  # 钉钉群机器人
            r = SESSION.post(url, json={"msgtype": "markdown",
                                       "markdown": {"title": title, "text": text}},
                             timeout=15)
        elif "api.telegram.org" in url:  # Telegram Bot
            chat = os.environ.get("TG_CHAT_ID", "")
            r = SESSION.post(url, json={"chat_id": chat,
                                        "text": f"{title}\n{text}"}, timeout=15)
        else:
            r = SESSION.post(url, json={"title": title, "content": text}, timeout=15)
        print(f"[PUSH] {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"[PUSH-ERROR] {type(e).__name__}: {e}")


# ---------------- Alpaca API ----------------
def api_get(path):
    r = SESSION.get(f"{BASE}{path}", headers=_hdr(), timeout=15)
    r.raise_for_status()
    return r.json()


def account():
    return api_get("/account")


def positions_map():
    out = {}
    for p in api_get("/positions"):
        try:
            out[p["symbol"]] = int(float(p["qty"]))
        except Exception:
            pass
    return out


def open_orders():
    try:
        return api_get("/orders?status=open&limit=50&nested=false")
    except Exception:
        return []


def cancel_symbol(sym):
    try:
        r = SESSION.delete(f"{BASE}/orders", headers=_hdr(),
                            params={"symbol": sym}, timeout=15)
        # 207 = 已无该票挂单
        if r.status_code not in (200, 204, 207):
            print(f"[WARN] cancel {sym}: {r.status_code} {r.text[:80]}")
    except Exception as e:
        print(f"[WARN] cancel {sym}: {e}")


def place_limit(sym, side, qty, px):
    r = SESSION.post(f"{BASE}/orders", headers=_hdr(), timeout=15, json={
        "symbol": sym, "qty": str(int(qty)), "side": side,
        "type": "limit", "limit_price": f"{px:.2f}",
        "time_in_force": "day", "extended_hours": False,
        "client_order_id": f"m{sym[:10]}{int(time.time()*1000)}",
    })
    if r.status_code in (200, 201):
        o = r.json()
        print(f"[ORDER] {side} {qty} {sym} @ {px:.2f} id={o.get('id','')[:8]}")
        return True
    print(f"[ORDER-REJECT] {side} {qty} {sym} @ {px:.2f}: "
          f"{r.status_code} {r.text[:120]}")
    return False


def quote_of(sym) -> dict:
    """返回 {bid, ask, mid, ok}"""
    try:
        r = SESSION.get(f"{DATA}/stocks/{sym}/snapshot",
                        headers=_hdr(), params={"feed": "iex"}, timeout=15)
        if r.status_code != 200:
            return {"ok": False}
        s = r.json()
        q = s.get("latestQuote") or {}
        bid = float(q.get("bp") or 0)
        ask = float(q.get("ap") or 0)
        lt = float((s.get("latestTrade") or {}).get("p") or 0)
        dc = float((s.get("dailyBar") or {}).get("c") or 0)
        if bid <= 0:
            bid = min(lt, dc) if lt > 0 and dc > 0 else (lt or dc)
        if ask <= 0:
            ask = max(lt, dc) if lt > 0 and dc > 0 else (lt or dc)
        if bid <= 0 or ask <= 0 or ask < bid:
            return {"ok": False}
        return {"ok": True, "bid": bid, "ask": ask, "mid": (bid + ask) / 2}
    except Exception as e:
        print(f"[QUOTE-ERR] {sym}: {e}")
        return {"ok": False}


def adv_dollars(sym) -> float:
    """30 日均日成交额 (美元)"""
    try:
        r = SESSION.get(f"{DATA}/stocks/{sym}/bars", headers=_hdr(),
                        params={"timeframe": "1Day", "limit": 30, "feed": "iex"},
                        timeout=15)
        if r.status_code != 200:
            return 0.0
        bars = r.json().get("bars") or []
        if not bars:
            return 0.0
        return sum(float(b.get("c", 0)) * float(b.get("v", 0)) for b in bars) / len(bars)
    except Exception:
        return 0.0


# ---------------- 状态 ----------------
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"targets": {}, "arrival": {}, "desired_qty": {},
            "target_ts": None, "target_src_ts": None, "race": None,
            "exec": {"fills": 0, "buy_notional": 0, "buy_slip": 0,
                     "sell_notional": 0, "sell_slip": 0},
            "warned": {}, "last_report": "", "last_fill_check": None}


def save_state(st):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def read_latest() -> dict:
    if os.path.exists(LATEST_FILE):
        try:
            with open(LATEST_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


# ---------------- 执行引擎 ----------------
def price_for(side: str, q: dict, arrival: float, elapsed_min: float) -> float:
    """被动阶梯: 返回本挂单限价"""
    if side == "buy":
        if elapsed_min < EP1_MIN:
            px = q["bid"]
        elif elapsed_min < EP2_MIN:
            px = q["mid"]
        else:
            px = q["ask"]
        cap = arrival * (1 + (DESPERATE_CAP if elapsed_min >= DESPERATE_MIN
                              else CHASE_CAP))
        px = min(px, q["ask"] * 1.0005)   # 不显著越过 ask
        px = min(px, cap)                 # 追价封顶
    else:
        if elapsed_min < EP1_MIN:
            px = q["ask"]
        elif elapsed_min < EP2_MIN:
            px = q["mid"]
        else:
            px = q["bid"]
        floor = arrival * (1 - (DESPERATE_CAP if elapsed_min >= DESPERATE_MIN
                                else CHASE_CAP))
        px = max(px, q["bid"] * 0.9995)
        px = max(px, floor)
    return round(px, 2)


def execute(st: dict, now_ny, equity: float):
    targets = st["targets"]
    if not targets:
        return
    target_dt = datetime.fromisoformat(st["target_ts"])
    elapsed = (now_ny - target_dt).total_seconds() / 60
    held = positions_map()
    oo = {o["symbol"]: o for o in open_orders() if o.get("status") == "open"}
    print(f"[EXEC] elapsed={elapsed:.0f}min targets={len(targets)} "
          f"open_orders={len(oo)}")

    for sym, w in sorted(targets.items()):
        desired = st["desired_qty"].get(sym, 0)
        if desired <= 0:
            continue
        cur = held.get(sym, 0)
        remaining = desired - cur
        if remaining == 0:
            if sym in oo:
                cancel_symbol(sym)     # 已到位, 撤掉残余挂单
            continue

        q = quote_of(sym)
        if not q["ok"]:
            print(f"[SKIP] {sym}: 无有效报价")
            continue
        mid, spread = q["mid"], (q["ask"] - q["bid"]) / max(q["mid"], 1e-9)
        if spread > MAX_SPREAD:
            _warn_once(st, sym, f"{sym} 盘口价差 {spread*100:.1f}% 过大, 本轮跳过")
            continue

        side = "buy" if remaining > 0 else "sell"
        qty = abs(remaining)
        if side == "sell":
            qty = min(qty, max(cur, 0))    # 不做空
            if qty == 0:
                continue
        notional = qty * q["mid"]

        adv = adv_dollars(sym)
        if adv > 0 and notional > adv * MAX_ADV_PART:
            _warn_once(st, sym, f"{sym} 订单金额 ${notional:,.0f} 超过其30日均"
                                f"成交额的10% (${adv:,.0f}), 跳过")
            continue

        arrival = st["arrival"].get(sym) or q["mid"]
        st["arrival"].setdefault(sym, q["mid"])
        px = price_for(side, q, arrival, elapsed)
        if px <= 0:
            continue

        exist = oo.get(sym)
        if exist and exist.get("side") == side:
            old_px = float(exist.get("limit_price") or 0)
            if old_px > 0 and abs(old_px - px) / old_px < REPLACE_EPS:
                print(f"[KEEP] {sym} 挂单 {old_px} 仍有效")
                continue
        if exist:
            cancel_symbol(sym)
        place_limit(sym, side, qty, px)
        print(f"[EXEC] {sym} w={w}% 目标{desired} 现持{cur} 余{remaining:+d}"
              f" {side} {qty} @ {px} (arrival={arrival:.2f} elapsed={elapsed:.0f}m)")


def _warn_once(st: dict, sym: str, msg: str):
    key = f"{sym}:{datetime.now(NY_TZ):%F}"
    if st["warned"].get(key):
        return
    st["warned"][key] = True
    st["warned"] = {k: v for k, v in st["warned"].items()
                    if k.split(":")[-1] >= f"{datetime.now(NY_TZ):%F}"}
    print(f"[LIQ-WARN] {msg}")
    push("流动性告警", msg)


def collect_fills(st: dict, now_ny):
    """统计成交滑点: 与到达价比较"""
    after = st.get("last_fill_check")
    try:
        path = f"/orders?status=closed&limit=100&nested=false" + \
               (f"&after={after}" if after else "")
        for o in api_get(path):
            if o.get("status") != "filled" or not str(o.get("client_order_id", "")).startswith("m"):
                continue
            fq, fpx = float(o.get("filled_qty") or 0), float(o.get("filled_avg_price") or 0)
            if fq <= 0 or fpx <= 0:
                continue
            sym, side = o["symbol"], o["side"]
            arrival = st["arrival"].get(sym)
            notional = fq * fpx
            ex = st["exec"]
            ex["fills"] += 1
            if arrival:
                slip = ((arrival - fpx) if side == "buy" else (fpx - arrival)) / arrival
                if side == "buy":
                    ex["buy_notional"] += notional
                    ex["buy_slip"] += slip * notional
                else:
                    ex["sell_notional"] += notional
                    ex["sell_slip"] += slip * notional
            print(f"[FILL] {side} {fq:g} {sym} @ {fpx} (arrival={arrival})")
    except Exception as e:
        print(f"[FILLS-ERR] {e}")
    st["last_fill_check"] = f"{now_ny.astimezone(ZoneInfo('UTC')):%F}T00:00:00Z"


# ---------------- 报告 ----------------
def race_lines(st: dict, latest: dict, equity: float) -> list:
    if not st.get("race"):
        return ["(对战未开始: 等 Alpaca 第一次镜像调仓)"]
    r = st["race"]
    fr = latest.get("total_asset", 0) / r["follow_base"] - 1 if r["follow_base"] else 0
    ar = equity / r["alpaca_base"] - 1 if r["alpaca_base"] else 0
    lead = ar - fr
    return [f"跟投易 {fr*100:+.2f}%  vs  Alpaca {ar*100:+.2f}%",
            f"{'Alpaca 领先' if lead >= 0 else '跟投易领先'} {abs(lead)*100:.2f} pt",
            f"计时起点: {r['t0']}"]


def exec_lines(st: dict) -> list:
    ex = st["exec"]
    if not ex["fills"]:
        return ["执行: 暂无成交"]
    def avg(n, s):
        return (s / n * 100) if n else 0.0
    return [f"执行: {ex['fills']} 笔成交",
            f"买入滑点 {avg(ex['buy_notional'], ex['buy_slip']):+.3f}% "
            f"(正=比信号到达价便宜)  卖出滑点 "
            f"{avg(ex['sell_notional'], ex['sell_slip']):+.3f}% (正=卖得更贵)"]


def alpaca_pos_lines(held: dict) -> list:
    if not held:
        return ["Alpaca 持仓: (空)"]
    return ["Alpaca 持仓:"] + [f"{s} {q}" for s, q in sorted(held.items())]


# ---------------- 主流程 ----------------
def main():
    now_ny = datetime.now(NY_TZ)
    print(f"[TIME] NY: {now_ny:%Y-%m-%d %H:%M}  manual={MANUAL}")

    if not (ALPACA_KEY and ALPACA_SECRET):
        print("[SKIP] 未配置 ALPACA_KEY_ID / ALPACA_SECRET")
        return

    open_now = market_open(now_ny)
    latest = read_latest()
    st = load_state()
    acct = account()
    equity = float(acct.get("portfolio_value") or 0)
    held = positions_map()
    print(f"[ACCT] equity={equity:,.2f} cash={float(acct.get('cash') or 0):,.2f} "
          f"positions={len(held)}")

    # 1) 新调仓信号 -> 固定目标 (用信号时刻的权重与权益, 防止行情漂移导致的反复交易)
    if (latest.get("rebalance")
            and latest.get("ts") != st.get("target_src_ts")
            and latest.get("weights")):
        weights = {k: float(v) for k, v in latest["weights"].items()}
        st["targets"] = weights
        st["target_ts"] = f"{now_ny.isoformat()}"
        st["target_src_ts"] = latest.get("ts")
        st["desired_qty"] = {}
        st["arrival"] = {}
        for sym, w in sorted(weights.items()):
            q = quote_of(sym)
            px = q.get("mid") if q.get("ok") else 0
            st["arrival"][sym] = px
            st["desired_qty"][sym] = int(equity * w / 100 / px) if px > 0 else 0
        if not st.get("race"):
            st["race"] = {"t0": f"{now_ny:%F %H:%M} NY",
                          "follow_base": latest.get("total_asset") or 0,
                          "alpaca_base": equity}
            print(f"[RACE] 对战开始: follow_base="
                  f"{st['race']['follow_base']:,.0f} alpaca_base={equity:,.0f}")
        push("开始镜像调仓",
             "\n".join([f"{s} {w}%" for s, w in sorted(weights.items())]
                      or ["(空仓)"]) +
             f"\n目标权益 ${equity:,.0f}")
        print(f"[TARGET] {st['desired_qty']}")

    # 2) 执行 (仅盘中且非手动)
    if st["targets"] and open_now and not MANUAL:
        execute(st, now_ny, equity)

    # 3) 成交统计
    collect_fills(st, now_ny)

    # 4) 对战记录与推送
    today = f"{now_ny:%F}"
    if MANUAL:
        push("Alpaca 状态",
             "\n".join(alpaca_pos_lines(held) + race_lines(st, latest, equity)
                       + exec_lines(st)))
    elif (st.get("race") and now_ny.hour >= 16
          and st.get("last_report") != today):
        st["last_report"] = today
        push("对战日报",
             "\n".join(race_lines(st, latest, equity) + exec_lines(st)))

    save_state(st)
    print("[DONE]")


if __name__ == "__main__":
    main()
