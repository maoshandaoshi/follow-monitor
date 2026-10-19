# -*- coding: utf-8 -*-
"""启动前自检: 决定本次触发是否该启动盘中会话。

在 gate job 中运行(无并发组, ~15s 完成), 结果写入 $GITHUB_OUTPUT:
  start=true|false   -> session job 是否运行
  action=session|skip|keepalive

与旧版(内嵌在 session step 里的 heredoc)的区别:
  旧版没有 GH_TOKEN, gh run list 返回空 -> json 解析失败 -> 永远 fail-open,
  守卫形同虚设。本版本由 workflow 显式注入 GH_TOKEN, 并且 actions: read 权限。

任何异常都 fail-open (start=true): 查询失败时保守放行 —— 会话内部幂等
(挂单对账 + client_order_id), 重复启动最坏只是排队, 好过因误判丢掉监控。
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

REPO = "maoshandaoshi/follow-monitor"
WORKFLOW = "follow.yml"
KEEPALIVE_CRON = "0 6 * * 0"
COOLDOWN = timedelta(minutes=10)      # 上一轮结束后的静默期, 防重复启动
# 纽约时段窗(含盘前盘后)。8:45 起才启动: session.py 盘前等待封顶 45 分钟,
# 8:45 起跑恰好等到 9:30 开盘; 更早起跑只会等完退出, 白占 runner。
NY_OPEN, NY_CLOSE = (8, 45), (21, 0)


def out(key, value):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
        f.write("%s=%s\n" % (key, value))


def _ran_session(run_id) -> bool:
    """该 run 是否真正跑过 session job。

    只看 session job 的 conclusion: gate 秒退的 run (conclusion=skipped)
    不算真会话。查询失败按 True 处理(保守: 维持冷却, 防止 API 抖动时
    重启风暴)。
    """
    res = subprocess.run(
        ["gh", "api", "repos/%s/actions/runs/%s/jobs" % (REPO, run_id),
         "--jq", '[.jobs[] | select(.name=="session")][0].conclusion // "none"'],
        capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        return True
    return res.stdout.strip().strip('"') not in ("skipped", "none")


def decide():
    """返回 (start, action, reason)"""
    schedule = os.environ.get("SCHEDULE", "")
    if schedule == KEEPALIVE_CRON:
        return "false", "keepalive", "keepalive cron -> repo keepalive only"

    me = os.environ.get("GITHUB_RUN_ID", "")
    res = subprocess.run(
        ["gh", "run", "list", "--repo", REPO, "--workflow", WORKFLOW,
         "--limit", "25", "--json", "databaseId,status,updatedAt"],
        capture_output=True, text=True, timeout=30)
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError("gh run list rc=%s err=%s"
                           % (res.returncode, (res.stderr or "")[:150]))
    now = datetime.now(timezone.utc)
    for r in json.loads(res.stdout):
        if str(r["databaseId"]) == me:
            continue
        if r["status"] in ("in_progress", "queued"):
            return "false", "skip", "run %s is %s" % (r["databaseId"], r["status"])
        if r["status"] == "completed":
            t = datetime.fromisoformat(r["updatedAt"].replace("Z", "+00:00"))
            if now - t >= COOLDOWN:
                break          # 列表按时间倒序, 再老的不用看了
            # 冷却期只统计真正跑过 session 的 run。gate 秒退的按铃 run
            # (session job=skipped) 不参与冷却 —— 否则 5 分钟看门狗的每次
            # 按铃都会被"上一次按铃 5 分钟前刚结束"冷却, 形成永远无法
            # 启动会话的死锁 (2026-10-19 全天事故)
            if _ran_session(r["databaseId"]):
                return "false", "skip", "real session run %s finished %ds ago" % (
                    r["databaseId"], int((now - t).total_seconds()))
            # gate-only run: 继续看更早的
    ny = datetime.now(ZoneInfo("America/New_York"))
    # 时间窗只门控 cron: 手动 dispatch / push 部署是用户显式意图, 放行
    # (session.py 自己会判断盘前等待/收盘补轮/周末退出, 不会误交易)
    if schedule and not (NY_OPEN <= (ny.hour, ny.minute) < NY_CLOSE):
        return "false", "skip", "outside NY window (%s)" % ny.strftime("%H:%M %Z")
    return "true", "session", "no active session, NY %s" % ny.strftime("%H:%M %Z")


def main():
    try:
        start, action, reason = decide()
    except Exception as e:
        # fail-open: 会话内部幂等, 重复启动最坏排队, 好过因误判丢掉监控
        print("[GUARD] check failed (%s), proceeding anyway" % e)
        start, action, reason = "true", "session", "fail-open"
    if action == "skip":
        print("[GUARD] skip: %s" % reason)
    elif action == "keepalive":
        print("[GUARD] keepalive only")
    else:
        print("[GUARD] ok: %s -> starting" % reason)
    out("start", start)
    out("action", action)


if __name__ == "__main__":
    sys.exit(main())
