# -*- coding: utf-8 -*-
"""
盈立持仓监控 — GitHub Actions / CI 版
========================================
每次运行(由 GitHub Actions 定时触发):
  1. OpenAPI 渠道登录(RSA 签名)获取 30 天 JWT
  2. 拉取盈立持仓明细 + 调仓流水(switch-history)
  3. 与仓库里 data/state.json 对比 -> 检测调仓
  4. 调仓发生 -> webhook 推送到手机(Server酱/企业微信/钉钉/Telegram 自动识别)
  5. 美股收盘后第一次运行 -> 推送当日持仓摘要
  6. 手动触发(workflow_dispatch) -> 随时推送完整持仓快照
  7. 有变化时更新 data/state.json (由 workflow 自动 commit 回仓库)

凭证全部来自环境变量(GitHub Secrets), 代码可公开, 不含任何密钥。

环境变量:
  USMART_PHONE        手机号         (必须)
  USMART_PASSWORD     登录密码       (必须)
  USMART_PUBLIC_KEY   RSA 公钥 base64 单行 (必须)
  USMART_PRIVATE_KEY  RSA 私钥 base64 单行 (必须, 可含换行自动剔除)
  PUSH_WEBHOOK        推送地址       (可选, 不填则仅打印到日志)
                      支持多个(英文逗号/分号/空格/换行分隔): Server酱 / 企业微信 /
                      钉钉 / Telegram / 飞书(webhook地址?secret=签名密钥)
  PUSH_WEBHOOK_FEISHU 飞书机器人地址 (可选, 与 PUSH_WEBHOOK 并存, 互不影响)
  MANUAL              1=跳过盘中判断, 强制执行并推送完整快照
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
from Crypto.Cipher import PKCS1_v1_5
from Crypto.Hash import MD5
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5 as Signer

NY_TZ = ZoneInfo("America/New_York")
DATA_DIR = os.environ.get("DATA_DIR", "data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

# ---------------- 非敏感配置 (可用环境变量覆盖) ----------------
OPENAPI_BASE = os.environ.get("OPENAPI_BASE", "https://open-jy.yxzq.com")
FOLLOW_BASE = os.environ.get("FOLLOW_BASE", "https://jy.yxzq.com")
CHANNEL = os.environ.get("USMART_CHANNEL", "1000671208")
AREA_CODE = os.environ.get("USMART_AREA", "86")
X_UID = os.environ.get("FOLLOW_X_UID", "1072154585190649856")
X_DEV_ID = os.environ.get("FOLLOW_X_DEV_ID", "66262f17e2932167")
INVEST_ID = os.environ.get("FOLLOW_INVEST_ID", "1517973810349703168")
STRATEGY_ID = os.environ.get("FOLLOW_STRATEGY_ID", "10756")

# ---------------- 凭证 (来自 GitHub Secrets) ----------------
PHONE = os.environ.get("USMART_PHONE", "").strip()
PASSWORD = os.environ.get("USMART_PASSWORD", "").strip()
PUBKEY = re.sub(r"\s+", "", os.environ.get("USMART_PUBLIC_KEY", ""))
PRIVKEY = re.sub(r"\s+", "", os.environ.get("USMART_PRIVATE_KEY", ""))
PUSH_WEBHOOK = os.environ.get("PUSH_WEBHOOK", "").strip()
MANUAL = os.environ.get("MANUAL", "").strip().lower() in ("1", "true", "yes")

SESSION = requests.Session()
# CI(Linux 海外机器)默认直连; 本地 Windows 测试设 USE_SYSTEM_PROXY=1 走系统代理/VPN
SESSION.trust_env = os.environ.get("USE_SYSTEM_PROXY", "").strip() == "1"

_PUB = RSA.import_key(base64.b64decode(PUBKEY)) if PUBKEY else None
_PRIV = RSA.import_key(base64.b64decode(PRIVKEY)) if PRIVKEY else None
TOKEN = ""


# ---------------- 登录 ----------------
def _rsa_encrypt(text: str) -> str:
    return base64.urlsafe_b64encode(
        PKCS1_v1_5.new(_PUB).encrypt(text.encode("utf-8"))).decode()


def _sign(body: str) -> str:
    return base64.b64encode(
        Signer.new(_PRIV).sign(MD5.new(body.encode("utf-8")))).decode()


def login() -> str:
    body = json.dumps({
        "phoneNumber": _rsa_encrypt(PHONE),
        "password": _rsa_encrypt(PASSWORD),
        "areaCode": AREA_CODE,
    })
    r = SESSION.post(OPENAPI_BASE + "/user-server/open-api/login",
                     data=body.encode("utf-8"),
                     headers={"Content-type": "application/json; charset=utf-8",
                              "X-Lang": "1", "X-Channel": CHANNEL,
                              "X-Sign": _sign(body)},
                     timeout=15)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 0:
        raise RuntimeError(f"登录失败 code={j.get('code')} msg={j.get('msg')}")
    return j["data"]["token"]


def _follow_headers() -> dict:
    return {
        "Host": "jy.yxzq.com",
        "Authorization": TOKEN,
        "X-Uid": X_UID, "X-Dev-Id": X_DEV_ID,
        "X-Type": "2", "X-Ver": "10.1.1", "X-Dt": "t1",
        "X-Net-Type": "n1", "X-Dev-Info": "h5", "X-Lang": "2",
        "Origin": "https://m-dl.yxzq.com",
        "Referer": "https://m-dl.yxzq.com/",
        "User-Agent": ("Mozilla/5.0 (Linux; Android 15; ABR-AL80 Build/V417IR; wv) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                       "Chrome/110.0.5481.154 Safari/537.36 "
                       "appVersion/10.1.1 softwareVersion/35 platform/yxzq-android "
                       "appId/com.usmart.stock nt/n1 channelId/yx"),
        "Accept": "application/json, text/plain, */*",
    }


def fetch_detail() -> dict:
    r = SESSION.get(FOLLOW_BASE + "/ams-center/api/v1/follow-invest-detail",
                    params={"strategyId": STRATEGY_ID, "investId": INVEST_ID},
                    headers=_follow_headers(), timeout=15)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 0:
        raise RuntimeError(f"持仓接口失败 code={j.get('code')} msg={j.get('msg')}")
    return j["data"]


def fetch_orders(limit=20) -> list:
    try:
        r = SESSION.get(FOLLOW_BASE + "/ams-center/api/v1/switch-history",
                        params={"businessType": "S", "investId": INVEST_ID,
                                "offset": 0, "limit": limit},
                        headers=_follow_headers(), timeout=15)
        r.raise_for_status()
        j = r.json()
        if j.get("code") != 0:
            return []
        data = j.get("data") or []
        if isinstance(data, dict):
            data = (data.get("list") or data.get("records")
                    or data.get("rows") or [])
        return data if isinstance(data, list) else []
    except Exception:
        return []


def build_result(d: dict) -> dict:
    total = float(d.get("currentMoney", 0))
    cash = float(d.get("cash", 0))
    stocks = []
    for p in d.get("position") or []:
        mv = float(p.get("marketValue", 0))
        qty = float(p.get("amount", 0))
        stocks.append({
            "ticker": p.get("stockCode", ""),
            "name": p.get("stockName", ""),
            "quantity": qty,
            "market_value": mv,
            "price": float(p.get("curPrice", 0)),
            "cost": float(p.get("costPrice", 0)),
            "pct_of_total": round(mv / total * 100, 2) if total else 0.0,
        })
    stocks.sort(key=lambda x: -x["market_value"])
    return {
        "ts": datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "strategy": d.get("name", ""),
        "currency": "USD" if d.get("currency") == 3 else str(d.get("currency")),
        "total_asset": total,
        "cash": cash,
        "cash_pct": round(cash / total * 100, 2) if total else 0.0,
        "securities_value": float(d.get("totalMarketValue", 0)),
        "total_profit": float(d.get("currentProfit", 0)),
        "stocks": stocks,
    }


# ---------------- 盘中判断 ----------------
def market_open(now_ny) -> bool:
    if now_ny.weekday() >= 5:
        return False
    hm = (now_ny.hour, now_ny.minute)
    # 含 16:00 整点: 收盘那一刻的补跑轮要能刷新 latest.json,
    # 否则对战日报用的是两分钟前的旧快照
    return (9, 30) <= hm <= (16, 0)


# ---------------- 状态与 diff ----------------
def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"positions": {}, "known_order_ids": [], "last_summary_date": ""}


def save_state(st: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


LATEST_FILE = os.path.join(DATA_DIR, "latest.json")


def write_latest(r: dict, rebalance: bool):
    """每轮写出当前目标权重+跟投总值, 供 alpaca_trade.py 镜像执行"""
    os.makedirs(DATA_DIR, exist_ok=True)
    payload = {
        "ts": r["ts"],
        "total_asset": r["total_asset"],
        "currency": r["currency"],
        "weights": {s["ticker"]: s["pct_of_total"]
                    for s in r["stocks"] if s["quantity"] > 0},
        "rebalance": rebalance,
    }
    with open(LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def diff_positions(prev: dict, cur: dict) -> list:
    """返回 (ticker, 原股数, 新股数) 列表; 股数变化才是真调仓(比例会随行情波动)"""
    out = []
    for t in sorted(set(prev) | set(cur)):
        old, new = prev.get(t), cur.get(t)
        old = 0.0 if old is None else float(old)
        new = 0.0 if new is None else float(new)
        if abs(old - new) < 1e-9:
            continue
        out.append((t, old, new))
    return out


def order_brief(o: dict) -> str:
    oid = o.get("orderId") or o.get("id") or "?"
    code = o.get("stockCode") or o.get("code") or ""
    name = o.get("stockName") or o.get("name") or code or "?"
    vol = o.get("volume") or o.get("orderVolume") or 0
    price = o.get("price") or o.get("dealPrice") or 0
    typ = o.get("type") or o.get("switchType") or ""
    typ_label = {"1": "买入", "2": "卖出", 1: "买入", 2: "卖出"}.get(typ, str(typ))
    try:
        price = float(price)
        vol = float(vol)
        ps = f"@ {price:,.2f}"
        vs = f"{vol:g} 股"
    except Exception:
        ps, vs = f"@ {price}", f"{vol} 股"
    bt = o.get("businessTime") or ""
    if isinstance(bt, (int, float)) and bt > 1e12:
        bt = datetime.fromtimestamp(bt / 1000).strftime("%m-%d %H:%M")
    elif isinstance(bt, (int, float)):
        bt = ""
    return f"{name}({code}) {typ_label} {vs} {ps} {bt} [order {oid}]"


# ---------------- 推送 ----------------
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
            print(f"[PUSH -> {host}] {r.status_code} {r.text[:120]}")
        except Exception as e:
            print(f"[PUSH-ERROR -> {host}] {type(e).__name__}: {e}")


def snapshot_text(r: dict, changes=None, new_orders=None) -> str:
    """完整版, 仅用于 Actions 控制台日志(不推送)"""
    lines = [f"[{r['ts']} NY] 持仓 {len([s for s in r['stocks'] if s['quantity'] > 0])} 只, "
             f"总资产 {r['total_asset']:,.2f} {r['currency']}"]
    for s in r["stocks"]:
        if s["quantity"] == 0:
            continue
        lines.append(f"{s['ticker']:<8} {s['quantity']:>6g} 股  "
                     f"{s['market_value']:>12,.2f}  ({s['pct_of_total']}%)")
    if changes:
        lines += ["", "== 调仓明细 =="] + [f"  {c[0]}: {c[1]:g} -> {c[2]:g} 股" for c in changes]
    if new_orders:
        lines += ["", "== 最新订单 =="] + [f"  {order_brief(o)}" for o in new_orders[:6]]
    return "\n".join(lines)


def push_snapshot_text(r: dict) -> str:
    """推送版: 只有 股票代码 比例"""
    lines = [f"{s['ticker']} {s['pct_of_total']}%" for s in r["stocks"] if s["quantity"] > 0]
    return "\n".join(lines) if lines else "(空仓)"


def push_alert_text(result: dict, changes: list, prev_pcts: dict) -> str:
    """推送版: 变动的股票 之前比例 -> 目前比例"""
    lines = []
    for t, _old_q, _new_q in changes:
        cur_pct = next((s["pct_of_total"] for s in result["stocks"] if s["ticker"] == t), 0)
        prev = prev_pcts.get(t, 0)
        lines.append(f"{t} {prev}% -> {cur_pct}%")
    return "\n".join(lines) if lines else push_snapshot_text(result)


# ---------------- 主流程 ----------------
def main():
    now_ny = datetime.now(NY_TZ)
    print(f"[TIME] NY: {now_ny:%Y-%m-%d %H:%M:%S %Z}  manual={MANUAL}")

    if not MANUAL and not market_open(now_ny):
        print("[SKIP] 非美股盘中时段, 退出 (手动触发可强制运行)")
        return

    if not (PHONE and PASSWORD and PUBKEY and PRIVKEY):
        print("[FATAL] 缺少 Secrets: USMART_PHONE / USMART_PASSWORD / "
              "USMART_PUBLIC_KEY / USMART_PRIVATE_KEY")
        sys.exit(1)

    global TOKEN
    TOKEN = login()
    print("[TOKEN] 登录成功")

    result = build_result(fetch_detail())
    orders = fetch_orders()
    state = load_state()
    baseline = json.dumps(state, sort_keys=True)

    cur_pos = {s["ticker"]: s["quantity"] for s in result["stocks"]
               if s["quantity"] > 0}
    cur_pcts = {s["ticker"]: s["pct_of_total"] for s in result["stocks"]
                if s["quantity"] > 0}
    changes = diff_positions(state.get("positions", {}), cur_pos)

    known = list(state.get("known_order_ids") or [])
    new_orders = [o for o in orders
                  if str(o.get("orderId") or o.get("id") or "?") not in known]

    print(snapshot_text(result, changes, new_orders))

    # --- 每轮写出最新快照给镜像交易器 ---
    write_latest(result, bool(changes or new_orders))

    # --- 推送逻辑: 只在真实调仓(盈立持仓股数变化)时推送;
    #     新订单流水只登记不推送(没成交不算调仓);
    #     收盘对战日报由 alpaca_trade.py 负责 ---
    if changes:
        push("调仓变动", push_alert_text(result, changes, state.get("pcts", {})))

    # --- 状态更新 (有实质变化才写, 避免每次运行都产生 commit) ---
    if changes or new_orders:
        state["positions"] = cur_pos
        state["pcts"] = cur_pcts
        state["ts"] = result["ts"]
    if new_orders:
        for o in new_orders:
            oid = str(o.get("orderId") or o.get("id") or "")
            if oid:
                known.append(oid)
        state["known_order_ids"] = known[-50:]
    if json.dumps(state, sort_keys=True) != baseline:
        save_state(state)
        print(f"[STATE] {STATE_FILE} updated (changes={bool(changes)}, "
              f"new_orders={len(new_orders)})")
    else:
        print("[STATE] no state change, skip commit")


if __name__ == "__main__":
    main()
