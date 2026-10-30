# -*- coding: utf-8 -*-
"""
盈立行情探针 (诊断用, 不参与交易)
====================================
目的: 确认盈立 OpenAPI 的 /quotes-openservice/api/v1/realtime 能否在
      盘前/盘后拿到可用报价 (美股代码 usTSLA), 以及返回的时间戳精度/新鲜度。
      若可用, alpaca_trade 在 IEX 无盘口的时段改用它定价。

跑法: CI 里 python quote_probe.py  (需要 USMART_* 环境变量)
"""
import base64
import json
import os
import time
from datetime import datetime

import requests
from Crypto.Hash import MD5
from Crypto.Signature import PKCS1_v1_5 as Signer

import follow_action as FA

QUOTE_HOST = os.environ.get("USMART_QUOTE_HOST", "https://open-hz.yxzq.com:8443")
SYMBOLS = os.environ.get("PROBE_SYMBOLS", "TSLA,FAZ,SOXL,NVDA").split(",")


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


def main():
    FA.TOKEN = FA.login()
    print(f"[PROBE] login ok, quote_host={QUOTE_HOST}")
    secu = [f"us{s.strip().upper()}" for s in SYMBOLS if s.strip()]
    print(f"[PROBE] secuIds={secu}")
    for api, params in (
        ("/quotes-openservice/api/v1/realtime", {"secuIds": secu}),
        ("/quotes-openservice/api/v1/marketstate", {"market": "us"}),
        ("/quotes-openservice/api/v1/orderbook", {"secuId": secu[0]}),
    ):
        try:
            code, text = quote_post(api, params)
        except Exception as e:
            print(f"[PROBE] {api} EXC {type(e).__name__}: {e}")
            continue
        print(f"[PROBE] {api} http={code} len={len(text)}")
        print("[PROBE] raw:", text[:1500].replace("\n", " "))
    print(f"[PROBE] local utc now = {datetime.utcnow():%F %T}")


if __name__ == "__main__":
    main()
