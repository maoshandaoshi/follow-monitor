# -*- coding: utf-8 -*-
"""
盈立行情探针 (诊断用, 不参与交易)
====================================
目的: 判断在 IEX 免费源没有盘口的时段(4:00-8:00 / 17:00-20:00 NY),
      盈立 OpenAPI 能否提供可用的盘前/盘后报价。三条通道都探:
        ① REST /realtime   最近一笔(含盘前盘后?)快照
        ② REST /tick       逐笔成交(看时间戳是否落在延长时段)
        ③ WS   rt.us.XXX   实时推送(看是否推盘前成交)

结论直接决定 alpaca_trade 的定价源: 若 WS 有延长时段数据 -> 用它;
若只有收盘价 -> 只能给"检测"用, 不能定价。

跑法: CI 里 python quote_probe.py  (需要 USMART_* 环境变量)
"""
import base64
import json
import os
import threading
import time
from datetime import datetime

import requests
from Crypto.Hash import MD5
from Crypto.Signature import PKCS1_v1_5 as Signer

import follow_action as FA

QUOTE_HOST = os.environ.get("USMART_QUOTE_HOST", "https://open-hz.yxzq.com:8443")
WS_HOST = os.environ.get("USMART_WS_HOST", "wss://open-hz.yxzq.com:8443/wss/v1")
WS_ORIGIN = os.environ.get("USMART_WS_ORIGIN", "https://open-hz.yxzq.com")
SYMBOLS = [s.strip().upper() for s in
           os.environ.get("PROBE_SYMBOLS", "TSLA,FAZ").split(",") if s.strip()]
WS_SECONDS = int(os.environ.get("PROBE_WS_SECONDS", "45"))


def _sign_urlsafe(content: str) -> str:
    return base64.urlsafe_b64encode(
        Signer.new(FA._PRIV).sign(MD5.new(content.encode("utf-8")))).decode()


def quote_post(api: str, params: dict):
    body = json.dumps(params)
    now = time.time()
    hdr = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": FA.TOKEN,
        "X-Channel": FA.CHANNEL,
        "X-Lang": "1",
        "X-Request-Id": str(int(now * 10 ** 6)) + "000",
        "X-Time": str(int(now)),
    }
    hdr["X-Sign"] = _sign_urlsafe(
        hdr["Authorization"] + hdr["X-Channel"] + hdr["X-Lang"]
        + hdr["X-Request-Id"] + hdr["X-Time"] + body)
    r = requests.post(QUOTE_HOST + api, data=body.encode("utf-8"),
                      headers=hdr, timeout=15)
    return r.status_code, r.text


def probe_rest(secu):
    code, text = quote_post("/quotes-openservice/api/v1/realtime",
                            {"secuIds": secu})
    print(f"[PROBE] realtime http={code}")
    try:
        j = json.loads(text)
        rec = (j.get("data") or {}).get("list") or []
        if rec:
            r0 = rec[0]
            print("[PROBE] realtime keys:", ",".join(sorted(r0.keys())))
            keep = {k: r0.get(k) for k in
                    ("symbol", "latestPrice", "latestTime", "close", "open",
                     "preClose", "high", "low", "bidPrice", "askPrice",
                     "bidSize", "askSize", "trdStatus", "volume")}
            print("[PROBE] realtime rec0:", json.dumps(keep, ensure_ascii=False))
            ext = [k for k in r0 if any(t in k.lower() for t in
                                        ("pre", "after", "ext", "night", "post"))]
            print("[PROBE] realtime ext-like fields:",
                  {k: r0[k] for k in ext})
        else:
            print("[PROBE] realtime empty:", text[:300])
    except Exception as e:
        print("[PROBE] realtime parse fail", e, text[:300])

    code, text = quote_post("/quotes-openservice/api/v1/tick",
                            {"secuId": secu[0], "tradeTime": 0, "seq": 0,
                             "count": 5, "sortDirection": 2})
    print(f"[PROBE] tick http={code} raw={text[:600]}")

    code, text = quote_post("/quotes-openservice/api/v1/marketstate",
                            {"market": "us"})
    print(f"[PROBE] marketstate http={code} raw={text[:200]}")


def probe_ws(secu):
    try:
        import websocket
    except Exception as e:
        print("[PROBE] websocket-client 不可用:", e)
        return
    got = {"n": 0}
    stop = threading.Event()

    def on_open(ws):
        ws.send(json.dumps({"op": "auth", "accessToken": FA.TOKEN}))
        print("[PROBE] ws opened, auth sent")

    def on_message(ws, msg):
        try:
            recv = json.loads(msg)
        except Exception:
            print("[PROBE] ws raw:", msg[:200])
            return
        op = recv.get("op")
        if op == "auth":
            print("[PROBE] ws auth reply:", recv.get("code"), recv.get("msg"))
            if recv.get("code") == 0:
                topics = [f"rt.us.{s}" for s in SYMBOLS] + \
                         [f"ob.us.{SYMBOLS[0]}"]
                ws.send(json.dumps({"op": "sub", "topiclist": topics}))
                print("[PROBE] ws sub sent:", topics)
        elif op == "ping":
            ws.send(json.dumps({"op": "pong"}))
        elif op == "update":
            got["n"] += 1
            data = ""
            try:
                data = base64.b64decode(recv.get("data") or "").decode("utf-8")
            except Exception:
                data = str(recv.get("data"))[:120]
            print(f"[PROBE] ws #{got['n']} topic={recv.get('topic')} "
                  f"data={data[:400]}")
        elif op == "offline":
            print("[PROBE] ws force offline")
            stop.set()
        else:
            print("[PROBE] ws other:", msg[:200])

    def on_error(ws, err):
        print("[PROBE] ws error:", type(err).__name__, err)

    def on_close(ws, *a):
        print("[PROBE] ws closed")
        stop.set()

    ws = websocket.WebSocketApp(WS_HOST, on_open=on_open,
                                on_message=on_message, on_error=on_error,
                                on_close=on_close)
    t = threading.Thread(target=lambda: ws.run_forever(origin=WS_ORIGIN),
                         daemon=True)
    t.start()
    stop.wait(WS_SECONDS)
    try:
        ws.close()
    except Exception:
        pass
    print(f"[PROBE] ws total updates in {WS_SECONDS}s: {got['n']}")
    if got["n"] == 0:
        print("[PROBE] 结论: WS 未推送延长时段数据(盘前无成交推送)")


def main():
    FA.TOKEN = FA.login()
    print(f"[PROBE] login ok, quote_host={QUOTE_HOST} ws={WS_HOST}")
    secu = [f"us{s}" for s in SYMBOLS]
    print(f"[PROBE] utc now={datetime.utcnow():%F %T}")
    probe_rest(secu)
    probe_ws(secu)


if __name__ == "__main__":
    main()
