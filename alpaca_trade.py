# -*- coding: utf-8 -*-
"""
Alpaca 镜像跟单执行器 (paper)
=============================
配合 follow_action.py (监控盈立持仓) 使用:
  monitor 每轮写 data/latest.json (目标权重 + 跟投总值 + 是否发生调仓)
  本脚本读取后, 在 Alpaca 模拟账户上镜像执行, 并记录"对战收益率"。

执行策略 (被动阶梯限价, 全部客观规则无预测):
  调仓信号到达后:
    0- 5 分钟  买入挂 bid   / 卖出挂 ask    (被动排队, 争取最优价)
    5-10 分钟  买入挂 mid   / 卖出挂 mid    (中间价)
    10-15 分钟 买入挂 ask   / 卖出挂 bid    (主动成交)
    15 分钟后   跟随最新盘口追价, 封顶 = 到达价 +0.6% (买) / -0.6% (卖)
    45 分钟后   封顶放宽到 ±1.5% (保证跟单完成, 优先不脱队)
  在途挂单**每轮重新定价**: 价格移动超过 0.1% 就撤旧单挂新单(阶梯推进),
  只有"撤单结果不明"才暂停 —— 旧版一律跳过在途单, 挂单永远停在初始价
  (2026-10-16 NEM 买单 115.21 挂了一下午没重定价, 收盘零成交)。

流动性护栏 (避免在成交差的票上付出过大冲击成本):
  - 盘口价差 > 3% 的票本轮跳过
  - 订单金额 > 该股 30 日均成交额(ADV) 10% 的票跳过并推送告警(每票每天一次)
  - 不做空: 卖出量以实际持仓为上限

收益率对战:
  - **起点 = 第一次"可执行的"镜像调仓那一轮**(盘中、非手动), 同一轮同一时刻
    取 盈立 total_asset 与 Alpaca equity 作为双方基准 —— 起点这一刻两边
    收益率必然都是 0.00%; 收盘/周末/手动查询不会误启基线。
  - 之后 收益率 = 当前数额 / 起点数额 - 1, 每日收盘推送对比 + 执行滑点统计
  - 滑点 = (到达价-成交价)/到达价 (买) —— 正值 = 买得比信号到达时便宜

环境变量:
  ALPACA_KEY_ID / ALPACA_SECRET   Alpaca paper 凭证 (必须)
  PUSH_WEBHOOK                   推送地址 (可选, 多个用 英文逗号/分号/换行 分隔)
  PUSH_WEBHOOK_FEISHU            飞书机器人地址 (可选, 与 PUSH_WEBHOOK 并存)
  MANUAL=1                       手动模式: 只报告状态, 不交易

推送约定(用户明确要求): 只有两类消息会推到手机 ——
  ① 调仓(由 follow_action.py 检测盈立持仓变化后推送"股票代码+比例")
  ② 收盘对战日报(本脚本 20:00 NY 盘后时段结束后推送, 含未完成挂单提示)
流动性告警/镜像执行细节一律只打日志, 不推送。

时段(2026-10-22 扩展): 监控与镜像覆盖美股完整时段 4:00-20:00 NY,
  盘前/盘后的调仓同样跟单(extended hours 限价单)。免费行情是 IEX 源,
  IEX 自身只有 8:00-9:30 / 16:00-17:00 延长时段, 之外报价陈旧就跳过下单。
"""
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs
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
STALE_REFRESH_MIN = 60.0    # 信号超过 60 分钟视为陈旧: 到达价刷新为当前中间价,
                            # 否则追价封顶钉在陈旧价格上(周末/会话中断后
                            # 买单永远成交不了)。每 60 分钟最多刷新一次。

SESSION = requests.Session()

def _hdr():
    return {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}


def market_open(now_ny) -> bool:
    """可执行窗口 = 美股完整时段(盘前 4:00 - 盘后 20:00 NY)。

    2026-10-22 前只在 9:30-16:00 交易: 盈立盘前/夜盘的调仓信号虽然能被
    follow_action 发现(现已 4:00-20:00 全时段检测), 镜像却要等到开盘才动,
    错过盘前跳空(用户反馈"夜盘和盘前没有跟单")。现在直接按 extended hours
    下单(limit + TIF day), 由盘口护栏决定能不能真的成交。
    """
    if now_ny.weekday() >= 5:
        return False
    t = now_ny.hour * 60 + now_ny.minute
    return 4 * 60 <= t < 20 * 60


def regular_hours(now_ny) -> bool:
    """常规时段 9:30-16:00(用于判断盘口是否来自连续竞价)"""
    if now_ny.weekday() >= 5:
        return False
    t = now_ny.hour * 60 + now_ny.minute
    return 9 * 60 + 30 <= t < 16 * 60


def webhook_urls() -> list:
    """收集所有推送地址, 支持多渠道并存。

    来源1: PUSH_WEBHOOK            (可用 英文逗号/分号/空格/换行 分隔多个地址)
    来源2: PUSH_WEBHOOK_FEISHU     (飞书机器人专用, 与上面互不影响)
    """
    blobs = [PUSH_WEBHOOK, os.environ.get("PUSH_WEBHOOK_FEISHU", "").strip()]
    urls = []
    for blob in blobs:
        if not blob:
            continue
        for u in re.split(r"[,;\s]+", blob):
            u = u.strip()
            if u and u not in urls:
                urls.append(u)
    return urls


def _push_one(url: str, title: str, text: str):
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
    elif "open.feishu.cn" in url:  # 飞书自定义机器人
        # 签名校验模式: 地址填 webhook地址?secret=你的签名密钥
        payload = {"msg_type": "text", "content": {"text": f"{title}\n{text}"}}
        q = parse_qs(urlparse(url).query)
        if q.get("secret"):
            ts = str(int(time.time()))
            sign = base64.b64encode(hmac.new(
                f"{ts}\n{q['secret'][0]}".encode(), b"",
                hashlib.sha256).digest()).decode()
            payload["timestamp"] = ts
            payload["sign"] = sign
        r = SESSION.post(url.split("?")[0], json=payload, timeout=15)
    else:
        r = SESSION.post(url, json={"title": title, "content": text}, timeout=15)
    return r


def push(title: str, text: str):
    urls = webhook_urls()
    if not urls:
        print(f"[PUSH-DISABLED] {title}\n{text}")
        return
    for url in urls:
        host = urlparse(url).netloc or url[:30]
        try:
            r = _push_one(url, title, text)
            print(f"[PUSH -> {host}] {r.status_code} {r.text[:100]}")
        except Exception as e:
            print(f"[PUSH-ERROR -> {host}] {type(e).__name__}: {e}")


# ---------------- Alpaca API ----------------
def api_get(path, params=None):
    r = SESSION.get(f"{BASE}{path}", headers=_hdr(), params=params, timeout=15)
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
    """查询未完成挂单。返回 list; **查询失败返回 None**(与空列表 [] 严格区分)。
    2026-10-15 事故: 旧版 except 静默返回 [], execute() 把'查询失败'当成'没有挂单',
    每 90 秒重复挂单, INTC/ITB/MSTR 买单各成交 5-6 次, CRL 超持 124 股。
    """
    last = None
    for attempt in range(3):
        try:
            r = SESSION.get(f"{BASE}/orders?status=open&limit=50&nested=false",
                            headers=_hdr(), timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            print(f"[OO-ERR] attempt {attempt + 1}/3: {type(e).__name__}: {e}")
            time.sleep(1.0)
    print(f"[OO-FAIL] open_orders 查询失败({last}), 返回 None -> 调用方必须熔断")
    return None


def order_status(oid: str):
    """查单个订单状态; 失败返回 None"""
    try:
        return api_get(f"/orders/{oid}")
    except Exception as e:
        print(f"[OO-ERR] order_status {oid[:8]}: {type(e).__name__}: {e}")
        return None


def cancel_by_id(oid: str) -> str:
    """按 ID 撤单。返回 'ok'(已撤或本来就没有) / 'gone'(404 已不存在) / 'fail'"""
    try:
        r = SESSION.delete(f"{BASE}/orders/{oid}", headers=_hdr(), timeout=15)
        if r.status_code in (200, 204, 404, 422):
            return "ok"
        print(f"[WARN] cancel_by_id {oid[:8]}: {r.status_code} {r.text[:80]}")
        return "fail"
    except Exception as e:
        print(f"[WARN] cancel_by_id {oid[:8]}: {e}")
        return "fail"


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
        "time_in_force": "day", "extended_hours": True,
        "client_order_id": f"m{sym[:10]}{int(time.time()*1000)}",
    })
    if r.status_code in (200, 201):
        o = r.json()
        print(f"[ORDER] {side} {qty} {sym} @ {px:.2f} id={o.get('id','')[:8]}")
        return o
    print(f"[ORDER-REJECT] {side} {qty} {sym} @ {px:.2f}: "
          f"{r.status_code} {r.text[:120]}")
    return None


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
        # 无真实盘口时用最新价同时充当买卖价。
        # 旧版 bid=min(lt,dc) ask=max(lt,dc) 会**伪造**一个 |最新价-收盘价|
        # 的价差: 行情波动大时直接超过 MAX_SPREAD, 把整只票锁死
        # (2026-10-16 MSTR/TEM 清理卖盘被伪价差挡了半小时)。
        ref = lt or dc
        if bid <= 0:
            bid = min(ask, ref) if ask > 0 else ref
        if ask <= 0:
            ask = max(bid, ref) if bid > 0 else ref
        if bid <= 0 or ask <= 0 or ask < bid:
            return {"ok": False}
        return {"ok": True, "bid": bid, "ask": ask, "mid": (bid + ask) / 2,
                "t": str((s.get("latestTrade") or {}).get("t") or "")}
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
                st = json.load(f)
            st.setdefault("pending", {})     # 本地挂单登记(防重复挂单)
            st.setdefault("seen_fills", [])  # 已统计的成交单 id(防重复累计)
            return st
        except Exception:
            pass
    return {"targets": {}, "arrival": {}, "arrival_ts": {}, "desired_qty": {},
            "target_ts": None, "target_src_ts": None, "race": None,
            "pending": {}, "seen_fills": [],
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


def cancel_and_confirm(sym: str, wait_s: float = 4.0) -> str:
    """撤掉 sym 全部挂单并**确认生效**后再返回。
    返回:
      'cancelled'  挂单已全部撤掉, 可以安全挂新单
      'filled'     有单在撤销前/撤销中成交(部分或全部), 需下轮重新对账
      'failed'     超时仍挂着, 保守起见本轮不挂新单
    竞态背景: 旧单挂在盘口排队, 撤单请求到达时它可能刚好成交,
    此时 Alpaca 撤单返回 404/422, 若忽略结果直接挂新单就会双重成交
    (2026-10-13 PDD 因此被买 3 次: 83.50/83.57/83.67 各 228 股)。
    """
    mine = [o for o in (open_orders() or []) if o.get("symbol") == sym]
    if not mine:
        return "cancelled"
    cancel_symbol(sym)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        left = [o for o in (open_orders() or []) if o.get("symbol") == sym]
        if not left:
            return "cancelled"
        if any(float(o.get("filled_qty") or 0) > 0 for o in left):
            return "filled"
        time.sleep(0.5)
    for o in (open_orders() or []):
        if o.get("symbol") == sym and float(o.get("filled_qty") or 0) > 0:
            return "filled"
    return "failed"


def _order_one(st: dict, sym: str, desired: int, held: dict, oo: dict,
               now_ny, equity: float):
    """把 sym 对齐到 desired 股。含撤单确认 + 挂单前复查, 保证不会重复下单。"""
    # 撤单结果不明的在途单: 宁可这轮不动, 也不冒叠单的风险
    if (st.get("pending") or {}).get(sym, {}).get("stuck"):
        print(f"[PENDING] {sym} 在途单状态不明, 本轮不挂新单")
        return
    cur = held.get(sym, 0)
    remaining = desired - cur
    if remaining == 0:
        if sym in oo:
            res = cancel_and_confirm(sym)
            print(f"[CANCEL] {sym} 已到位({cur}), 撤残余挂单 -> {res}")
        return

    q = quote_of(sym)
    if not q["ok"]:
        print(f"[SKIP] {sym}: 无有效报价")
        return
    # 盘前/盘后行情来自 IEX 免费源, IEX 自身只有 8:00-9:30 / 16:00-17:00
    # 的延长时段; 之外拿到的是**陈旧盘口**, 拿它挂单等于盲飞(限价挂在
    # 早已不存在的价位上)。常规时段不受影响 —— 那时 latestTrade 也可能
    # 因冷门票超过 10 分钟没成交, 但那是连续竞价的真实状态。
    if not regular_hours(now_ny) and q.get("t"):
        try:
            ts = re.sub(r"\.(\d{6})\d+", r".\1",
                        str(q["t"]).replace("Z", "+00:00"))
            lt = datetime.fromisoformat(ts)
            age_min = (now_ny - lt.astimezone(NY_TZ)).total_seconds() / 60
            if age_min > 10:
                print(f"[SKIP] {sym}: 盘前/盘后报价陈旧 {age_min:.0f} 分钟"
                      f"(IEX 无成交), 本轮不下单")
                return
        except Exception:
            pass
    mid, spread = q["mid"], (q["ask"] - q["bid"]) / max(q["mid"], 1e-9)
    if spread > MAX_SPREAD:
        _warn_once(st, sym, f"{sym} 盘口价差 {spread*100:.1f}% 过大"
                            f"(bid={q['bid']:.2f} ask={q['ask']:.2f}), 本轮跳过")
        return

    side = "buy" if remaining > 0 else "sell"
    qty = abs(remaining)
    if side == "sell":
        qty = min(qty, max(cur, 0))    # 不做空
        if qty == 0:
            return
    notional = qty * q["mid"]

    adv = adv_dollars(sym)
    if adv > 0 and notional > adv * MAX_ADV_PART:
        _warn_once(st, sym, f"{sym} 订单金额 ${notional:,.0f} 超过其30日均"
                            f"成交额的10% (${adv:,.0f}), 跳过")
        return

    # ---- 到达价: 信号陈旧(周末/会话中断)时按当前中间价刷新, 防止追价封顶
    #      钉在几天前的价格上导致买单永远成交不了。每 60 分钟最多刷新一次。
    ats = st.setdefault("arrival_ts", {})
    now_ts = now_ny.timestamp()
    el = elapsed_min(now_ny, st)
    arrival = st["arrival"].get(sym)
    if not arrival or arrival <= 0:
        arrival = q["mid"]
        st["arrival"][sym] = arrival
        ats[sym] = now_ts
    elif (el > STALE_REFRESH_MIN
          and now_ts - float(ats.get(sym) or 0) > STALE_REFRESH_MIN * 60):
        print(f"[REFRESH] {sym} 信号已 {el:.0f} 分钟, 到达价 "
              f"{arrival:.2f} -> {q['mid']:.2f}")
        arrival = q["mid"]
        st["arrival"][sym] = arrival
        ats[sym] = now_ts
    px = price_for(side, q, arrival, el)
    if px <= 0:
        return

    # ---- 购买力预检(避免 403 刷屏与无效重试) ----
    if side == "buy":
        cash = st.get("_cash")
        if cash is not None and qty * px > cash * 0.98:
            _warn_once(st, sym, f"{sym} 买入 ${qty * px:,.0f} 超过可用现金 "
                                f"${cash:,.0f}, 本轮跳过")
            return

    exist = oo.get(sym)
    if exist and exist.get("side") == side:
        old_px = float(exist.get("limit_price") or 0)
        if old_px > 0 and abs(old_px - px) / old_px < REPLACE_EPS:
            print(f"[KEEP] {sym} 挂单 {old_px} 仍有效")
            return
    if exist:
        res = cancel_and_confirm(sym)
        if res != "cancelled":
            # 旧单成交中/撤不掉: 绝不叠加新单, 交给下一轮重新对账
            print(f"[EXEC] {sym} 旧单撤销结果={res}, 本轮不挂新单")
            return

    # ---- 挂单前最终复查: 撤单窗口内行情/持仓可能已变 ----
    held_now = positions_map()
    oo_now = open_orders()
    if oo_now is None:
        print(f"[EXEC-ABORT] {sym} 复查时 open_orders 查询失败, 本轮不挂新单")
        return
    oo_syms = {o["symbol"] for o in oo_now}
    if sym in oo_syms:
        print(f"[EXEC] {sym} 撤单后仍有挂单存在, 本轮跳过")
        return
    cur_now = held_now.get(sym, 0)
    remaining_now = desired - cur_now
    if remaining_now == 0:
        print(f"[EXEC] {sym} 撤单期间已对齐({cur_now}), 无需再挂")
        return
    if (remaining_now > 0) != (remaining > 0):
        print(f"[EXEC] {sym} 撤单期间方向反转({remaining:+d}->{remaining_now:+d}), "
              f"本轮跳过")
        return
    qty = min(abs(remaining_now),
              max(cur_now, 0) if side == "sell" else abs(remaining_now))
    if qty <= 0:
        return
    o = place_limit(sym, side, qty, px)
    print(f"[EXEC] {sym} 目标{desired} 现持{cur_now} 余{remaining_now:+d}"
          f" {side} {qty} @ {px} (arrival={arrival:.2f})")

    # ---- 本地登记 + 挂单后成交确认(防重复挂单的最后防线) ----
    if not o:
        return
    oid = o.get("id")
    st["pending"][sym] = {"id": oid, "side": side, "qty": qty,
                          "px": px, "ts": f"{now_ny:%F %H:%M:%S}"}
    # 挂单后 2.5 秒确认状态: 若已成交, 下一轮对账会看到新持仓, 不会重复挂
    time.sleep(2.5)
    stt = order_status(oid)
    if stt and stt.get("status") == "filled":
        fq = float(stt.get("filled_qty") or 0)
        print(f"[ORDER-FILLED] {sym} {side} {fq:g} @ {stt.get('filled_avg_price')} "
              f"(挂单后立即成交)")
        st["pending"].pop(sym, None)


def elapsed_min(now_ny, st: dict) -> float:
    try:
        target_dt = datetime.fromisoformat(st["target_ts"])
        return (now_ny - target_dt).total_seconds() / 60
    except Exception:
        return 0.0


def _reconcile_pending(st: dict):
    """对账本地登记的挂单(pending)。

    每轮调用。逻辑:
    1. Alpaca open 挂单 ∪ 本地 pending = 已知挂单宇宙
    2. 本地 pending 里 Alpaca 查不到的: 按 ID 显式撤单(撤成功/404/已成交都算清除)
    3. 本地 pending 里 Alpaca 还挂着的: 保留, 由 _order_one 的阶梯重挂处理
       (价格移动超 0.1% 撤旧挂新, 否则 KEEP —— 在途单不再是"冻结"理由)
    4. 撤单结果不明(网络失败等)的登记 stuck 标记: 该票本轮不挂新单,
       下一轮重试, 避免旧单可能还活着时叠单
    """
    oo = open_orders()
    if oo is None:
        return None                      # 查询失败: 调用方熔断
    # status=open 查询返回的即未完结集合(new/accepted/held/partially_filled 等),
    # 不再按 status 二次过滤, 否则 partially_filled 的单会被漏掉
    remote_ids = {o.get("id") for o in oo}
    for sym in list(st["pending"]):
        p = st["pending"][sym]
        oid = p.get("id")
        if oid in remote_ids:
            p.pop("stuck", None)         # 远端可见, 解除暂停标记
            continue                     # 远端还挂着, 正常
        # 远端查不到: 可能已成交/已撤/查询曾抽风。按 ID 显式撤一次兜底
        res = cancel_by_id(oid)
        stt = order_status(oid) if res == "ok" else None
        stt_status = (stt or {}).get("status")
        if stt_status == "filled":
            print(f"[RECONCILE] {sym} 挂单 {oid[:8]} 已成交, 清除登记")
            st["pending"].pop(sym, None)
        elif res == "ok":
            print(f"[RECONCILE] {sym} 挂单 {oid[:8]} 已撤销, 清除登记")
            st["pending"].pop(sym, None)
        else:
            print(f"[RECONCILE] {sym} 挂单 {oid[:8]} 状态不明({stt_status}), "
                  f"标记 stuck, 本轮不挂新单")
            p["stuck"] = True
    return oo


def execute(st: dict, now_ny, equity: float):
    targets = st["targets"]
    if not targets:
        return

    # ---- fail-closed: 挂单状态查不到就整体熔断, 绝不盲飞 ----
    oo_raw = _reconcile_pending(st)
    if oo_raw is None:
        print("[EXEC-ABORT] open_orders 查询失败, 本轮不执行任何订单(fail-closed)")
        return
    oo = {o["symbol"]: o for o in oo_raw}

    held = positions_map()
    st["_cash"] = float((account() or {}).get("cash") or 0)
    print(f"[EXEC] elapsed={elapsed_min(now_ny, st):.0f}min "
          f"targets={len(targets)} held={len(held)} open_orders={len(oo)} "
          f"pending={len(st['pending'])} cash={st['_cash']:,.2f}")

    # 待处理清单 = 目标内股票 + 已持仓但不在目标里的股票(跟投清仓了的, 必须同步清仓)
    work = {sym: st["desired_qty"].get(sym, 0) for sym in targets}
    for sym in held:
        if sym not in targets:
            work[sym] = 0
            print(f"[CLEANUP] {sym} 不在跟投目标中, 计划清仓 {held[sym]} 股")

    for sym in sorted(work):
        # 只有"撤单结果不明"的在途单才暂停挂新单; 正常在途单交给 _order_one
        # 走阶梯重挂。旧版一律跳过 -> 挂单永远停在初始价 (2026-10-16 NEM 买单
        # 115.21 挂了一下午没重定价, 收盘零成交, 5.6 万美金现金干看着)。
        p = st["pending"].get(sym) or {}
        if p.get("stuck"):
            print(f"[PENDING] {sym} 在途单状态不明({p.get('side')} "
                  f"{p.get('qty')}@{p.get('px')}), 本轮跳过")
            continue
        _order_one(st, sym, work[sym], held, oo, now_ny, equity)


def _warn_once(st: dict, sym: str, msg: str):
    key = f"{sym}:{datetime.now(NY_TZ):%F}"
    if st["warned"].get(key):
        return
    st["warned"][key] = True
    st["warned"] = {k: v for k, v in st["warned"].items()
                    if k.split(":")[-1] >= f"{datetime.now(NY_TZ):%F}"}
    # 只记日志不推送: 用户约定只有"调仓"和"日报"两类消息(2026-10-17)
    print(f"[LIQ-WARN] {msg}")


def collect_fills(st: dict, now_ny):
    """统计成交滑点: 与到达价比较。按订单 id 去重(旧版每轮全量重复累计,
    2026-10-15 一天虚增 1194 笔)。

    2026-10-22 审计修正: 历史 exec 计数 5798 笔 vs 真实成交 141 笔,
    污染全部来自 10-15 去重修复之前; 修复后每日新增与 API 完全一致。
    此处再做两道加固:
      - after 走 params 传参(旧版拼进 URL 未编码冒号, 有被忽略的风险)
      - seen_fills 保持**有序列表**从头部淘汰(旧版 set->list 随机顺序
        截断, 会把未超期的 id 挤出去导致重复计数)
    """
    after = st.get("last_fill_check")
    try:
        params = {"status": "closed", "limit": 100, "nested": "false"}
        if after:
            params["after"] = after
        seen_list = list(st.get("seen_fills") or [])
        seen = set(seen_list)
        for o in api_get("/orders", params):
            if o.get("status") != "filled" or not str(o.get("client_order_id", "")).startswith("m"):
                continue
            oid = o.get("id")
            if oid in seen:
                continue
            fq, fpx = float(o.get("filled_qty") or 0), float(o.get("filled_avg_price") or 0)
            if fq <= 0 or fpx <= 0:
                continue
            sym, side = o["symbol"], o["side"]
            arrival = st["arrival"].get(sym)
            notional = fq * fpx
            ex = st["exec"]
            ex["fills"] += 1
            seen.add(oid)
            seen_list.append(oid)
            if arrival:
                slip = ((arrival - fpx) if side == "buy" else (fpx - arrival)) / arrival
                if side == "buy":
                    ex["buy_notional"] += notional
                    ex["buy_slip"] += slip * notional
                else:
                    ex["sell_notional"] += notional
                    ex["sell_slip"] += slip * notional
            print(f"[FILL] {side} {fq:g} {sym} @ {fpx} (arrival={arrival})")
        # 有序保留最近 2000 个 id(从头部淘汰最老的), 防 state 无限膨胀
        st["seen_fills"] = seen_list[-2000:]
    except Exception as e:
        print(f"[FILLS-ERR] {e}")
    st["last_fill_check"] = f"{now_ny.astimezone(ZoneInfo('UTC')):%F}T00:00:00Z"


# ---------------- 报告 ----------------
def remote_last_report() -> str:
    """读**远端仓库** trader.json 的 last_report, 推送日报前做幂等校验。

    背景(2026-10-21 双日报事故): 16:00 的会话发完日报后 commit 尚未落地,
    16:01 新会话按旧检出启动, 本地 last_report 仍是昨天 -> 再发一次。
    本地状态追不上多 runner 并发, 只有远端仓库是唯一事实源。
    查询失败返回 "" (fail-open: 宁可重发, 不能漏发)。
    """
    tok = os.environ.get("GH_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not (tok and repo):
        return ""
    try:
        r = SESSION.get(
            f"https://api.github.com/repos/{repo}/contents/data/trader.json",
            headers={"Authorization": f"Bearer {tok}",
                     "Accept": "application/vnd.github.raw",
                     "User-Agent": "follow-monitor"},
            params={"ref": "main"}, timeout=15)
        if r.status_code != 200:
            print(f"[GH] 远端 trader.json 查询 {r.status_code}, 按未发送处理")
            return ""
        return (json.loads(r.text).get("last_report") or "")
    except Exception as e:
        print(f"[GH] 远端 last_report 查询失败({type(e).__name__}: {e}), "
              f"按未发送处理")
        return ""



def race_lines(st: dict, latest: dict, equity: float) -> list:
    """对战对比: 起点数额 -> 当前数额 -> 收益率 (两边都从同一个时刻起算)

    盈立快照缺失时的兜底(2026-10-22): latest.json 是会话本机文件,
    新 runner 里没有; 收盘补轮在 16:00 之后 follow_action 又不刷新。
    旧版此时 fr 按 0.00% 算, "领先"行把 Alpaca 的全部收益当成领先幅度,
    严重误导。现在回退到 trader.json 里缓存的最近一次快照(标注时间),
    连缓存都没有就不显示"领先"行 —— 缺一边数据时不做比较。
    """
    r = st.get("race")
    if not r:
        return ["(对战未开始: 等第一次可执行的镜像调仓)"]
    fb = float(r.get("follow_base") or 0)
    ab = float(r.get("alpaca_base") or 0)
    cur_f = float(latest.get("total_asset") or 0)
    note = ""
    if cur_f <= 0:
        fl = st.get("follow_last") or {}
        cur_f = float(fl.get("asset") or 0)
        if cur_f > 0:
            note = f" (快照 {fl.get('ts') or ' earlier'})"
    fr = (cur_f / fb - 1) if (fb > 0 and cur_f > 0) else None
    ar = (equity / ab - 1) if (ab > 0 and equity > 0) else None
    out = [f"盈立 {fb:,.0f} → {cur_f:,.0f}  {fr*100:+.2f}%{note}"
           if fr is not None else f"盈立 起点 {fb:,.0f} (暂无快照)",
           f"Alpaca  {ab:,.0f} → {equity:,.0f}  {ar*100:+.2f}%"
           if ar is not None else f"Alpaca 起点 {ab:,.0f} (无权益)"]
    if fr is not None and ar is not None:
        lead = ar - fr
        out.append(f"{'Alpaca' if lead >= 0 else '盈立'} 领先 "
                   f"{abs(lead)*100:.2f} pt   起点 {r.get('t0', '')}")
    else:
        out.append(f"起点 {r.get('t0', '')} (缺一侧数据, 本轮不比)")
    return out


def pending_lines(st: dict) -> list:
    """日报附注: 还没跟完的挂单。跟单没做完要能一眼看到"""
    p = st.get("pending") or {}
    if not p:
        return []
    out = ["未完成挂单:"]
    for sym, o in sorted(p.items()):
        out.append(f"  {sym} {o.get('side')} {o.get('qty')}@{o.get('px')}")
    return out


def gap_lines(st: dict, held: dict) -> list:
    """日报附注: 目标股数还没买齐的(跟单进度)。
    2026-10-16 的教训: NEM 目标 241 股实际 0 股, 收盘日报里完全看不出来,
    用户只能自己发现"没跟上"。短提 >=2 股且 >=2% 才显示, 避免噪音。"""
    out = []
    for sym, want in sorted((st.get("desired_qty") or {}).items()):
        have = int(held.get(sym, 0))
        short = want - have
        if short >= 2 and short >= want * 0.02:
            out.append(f"  {sym} {have}/{want} 股未完成")
    for sym, have in sorted(held.items()):
        if sym not in (st.get("targets") or {}):
            out.append(f"  {sym} 持仓 {have} 股待清(不在盈立目标中)")
    return (["跟单进度:"] + out) if out else []


def exec_lines(st: dict) -> list:
    ex = st["exec"]
    if not ex["fills"]:
        return ["执行: 暂无成交"]
    def avg(n, s):
        return (s / n * 100) if n else 0.0
    return [f"执行(本次对战): {ex['fills']} 笔成交",
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
    # 缓存最近一次盈立快照到 trader.json: latest.json 是会话本机文件
    # (gitignore), 新 runner/收盘补轮拿不到, 日报用它兜底(标注快照时间)
    if float(latest.get("total_asset") or 0) > 0:
        st["follow_last"] = {"asset": float(latest["total_asset"]),
                             "ts": str(latest.get("ts") or "")}
    acct = account()
    equity = float(acct.get("portfolio_value") or 0)
    held = positions_map()
    print(f"[ACCT] equity={equity:,.2f} cash={float(acct.get('cash') or 0):,.2f} "
          f"positions={len(held)}")

    # 1) 新调仓信号 -> 固定目标 (用信号时刻的权重与权益, 防止行情漂移导致的反复交易)
    sig_text = None
    if (latest.get("rebalance")
            and latest.get("ts") != st.get("target_src_ts")
            and latest.get("weights")):
        weights = {k: float(v) for k, v in latest["weights"].items()}
        st["targets"] = weights
        st["target_ts"] = f"{now_ny.isoformat()}"
        st["target_src_ts"] = latest.get("ts")
        st["desired_qty"] = {}
        st["arrival"] = {}
        st["arrival_ts"] = {}
        # 新目标到来: 撤销全部在途挂单(旧目标作废), 按新目标重新对账
        for sym, p in list(st["pending"].items()):
            res = cancel_by_id(p.get("id", ""))
            print(f"[RETARGET] 撤旧挂单 {sym} {p.get('side')} {p.get('qty')} "
                  f"-> {res}")
            st["pending"].pop(sym, None)
        for sym, w in sorted(weights.items()):
            q = quote_of(sym)
            px = q.get("mid") if q.get("ok") else 0
            st["arrival"][sym] = px
            st["desired_qty"][sym] = int(equity * w / 100 / px) if px > 0 else 0
        sig_text = ("\n".join([f"{s} {w}%" for s, w in sorted(weights.items())]
                              or ["(空仓)"])
                    + f"\n目标权益 ${equity:,.0f}")
        print(f"[TARGET] {st['desired_qty']}")

    # 2) 执行 (仅盘中且非手动)
    if st["targets"] and open_now and not MANUAL:
        execute(st, now_ny, equity)

    # 3) 对战起点: **只在"新调仓信号到达且可执行"那一轮**播种 —— 同一轮、
    #    同一时刻取双方数额, 起点这一刻两边必然都是 0.00%。
    #    旧版只要求 target_src_ts 存在: race 被清空后, 任意盘中轮次都会
    #    拿着几天前的旧信号立刻误播种 (2026-10-19 15:53 收盘前 7 分钟
    #    误起点, 起点时刻毫无调仓语义)。sig_text 非空 = 本轮确有新信号。
    seeded = False
    if sig_text and open_now and not MANUAL and not st.get("race"):
        st["race"] = {"t0": f"{now_ny:%F %H:%M} NY",
                      "follow_base": float(latest.get("total_asset") or 0),
                      "alpaca_base": equity,
                      "follow_src": str(latest.get("ts") or "")}
        seeded = True
        print(f"[RACE] 对战起点: 盈立 {st['race']['follow_base']:,.2f}  "
              f"Alpaca {st['race']['alpaca_base']:,.2f}  (双方均为 0.00%)")

    # 4) 新调仓信号: 只记日志不推送。
    #    推送由 follow_action.py 的"调仓变动"负责(检测到盈立持仓变化那一刻),
    #    两边都推会造成同一调仓两条消息。用户约定: 只有调仓和日报两类推送。
    if sig_text:
        tail = ("\n" + "\n".join(race_lines(st, latest, equity))) if seeded else ""
        print("[MIRROR] 开始镜像调仓\n" + sig_text + tail)

    # 5) 成交统计
    collect_fills(st, now_ny)

    # 6) 对战记录与推送
    today = f"{now_ny:%F}"
    if MANUAL:
        push("Alpaca 状态",
             "\n".join(alpaca_pos_lines(held) + race_lines(st, latest, equity)
                       + gap_lines(st, held) + pending_lines(st)
                       + exec_lines(st)))
    elif (st.get("race") and now_ny.hour >= 20
          and st.get("last_report") != today):
        # 日报时间 16:00 -> 20:00: 盘后时段(16:00-20:00)也在跟单了,
        # 报告必须覆盖夜盘的调仓与成交, 否则夜盘动作要等第二天才看得到。
        # 收盘后先对账一次: 把收盘时已过期/已成交的挂单登记清掉,
        # 日报里的"未完成挂单"才是真实状态
        _reconcile_pending(st)
        # 幂等校验: 本地 last_report 追不上多 runner 并发(16:00 会话 commit
        # 未落地, 16:01 新会话按旧检出启动又发一次 —— 2026-10-21 双日报),
        # 推前必须问远端仓库这个唯一事实源
        if remote_last_report() == today:
            print("[REPORT] 远端今日日报已发, 跳过(防多 runner 重复推送)")
            st["last_report"] = today
        else:
            st["last_report"] = today
            push("对战日报",
                 "\n".join(race_lines(st, latest, equity) + gap_lines(st, held)
                           + pending_lines(st) + exec_lines(st)))

    save_state(st)
    print("[DONE]")


if __name__ == "__main__":
    main()
