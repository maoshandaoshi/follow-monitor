#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘中长会话 (GitHub Actions 专用)

背景: GitHub 的 schedule cron 不可靠(延迟/漏跑很常见), 每 5 分钟一次靠不住。
方案: cron 只负责"启动会话", 启动后本进程在云端连续轮询, 每 INTERVAL 秒
      跑一轮 follow_action.py (监控跟投易) + alpaca_trade.py (镜像交易),
      直到美股收盘或达到时长上限, 期间定期把状态 commit 回仓库。

启动窗口(UTC): 13:30 (9:30 EDT 开盘) / 15:35 (盘中备份) / 18:30 (接续到收盘)
每段最长 LOOP_MAX_HOURS 小时, 13:30 段 + 18:30 段恰好首尾相接覆盖全天。

环境变量:
  LOOP_INTERVAL   每轮间隔秒数 (默认 300)
  LOOP_MAX_HOURS  单次会话最长小时数 (默认 5, GitHub 单任务上限 6h)
  LOOP_MAX_ITER   仅跑 N 轮后退出 (测试用)
"""
import os
import subprocess
import sys
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
INTERVAL = int(os.environ.get("LOOP_INTERVAL", "300"))
MAX_SECONDS = float(os.environ.get("LOOP_MAX_HOURS", "5")) * 3600
MAX_ITER = int(os.environ.get("LOOP_MAX_ITER", "0"))
OPEN = dtime(9, 30)
CLOSE = dtime(16, 0)


def market_open(dt: datetime) -> bool:
    if dt.weekday() >= 5:
        return False
    return OPEN <= dt.time() < CLOSE


def run_step(script: str):
    env = dict(os.environ)
    env.pop("MANUAL", None)          # 会话内一律按自动模式(可下单)
    print(f"\n===== [{datetime.now(NY):%F %H:%M:%S} NY] {script} =====", flush=True)
    try:
        subprocess.run([sys.executable, script], env=env, timeout=600)
    except subprocess.TimeoutExpired:
        print(f"[WARN] {script} 超时 600s, 跳过本轮")
    except Exception as e:
        print(f"[WARN] {script} 异常: {type(e).__name__}: {e}")


def _git(*args) -> int:
    return subprocess.run(["git", *args], check=False).returncode


def commit_state(round_no: int):
    """把 data/ 下的状态提交回仓库, 保证异常中断也不丢状态"""
    _git("config", "user.name", "follow-bot")
    _git("config", "user.email", "actions@users.noreply.github.com")
    _git("add", "-A", "data")
    if _git("diff", "--cached", "--quiet") == 0:
        return
    _git("commit", "-m", f"auto: session snapshot (round {round_no})")
    for attempt in range(3):
        if _git("push") == 0:
            print("[GIT] 状态已提交回仓库")
            return
        # 远端有新提交(例如刚推过代码)会让 push 被拒 —— 先合并再重试,
        # 否则本会话后续所有轮次的状态都会滞留不上去。
        print(f"[GIT] push 被拒(第 {attempt+1} 次), 与远端合并后重试 ...")
        if _git("pull", "--rebase", "--autostash") != 0:
            _git("rebase", "--abort")
            # 冲突时以会话本机状态为准(它是唯一在交易的进程)
            _git("pull", "--no-rebase", "--no-edit", "-X", "ours")
        time.sleep(3)
    print("[GIT] 状态仍未能推送, 将在会话结束时重试")


def main():
    t0 = time.time()
    rnd = 0
    print(f"[SESSION] 启动 {datetime.now(NY):%F %H:%M:%S} NY  "
          f"interval={INTERVAL}s max={MAX_SECONDS/3600:.1f}h")

    while True:
        now = datetime.now(NY)

        if not market_open(now):
            if now.weekday() >= 5:
                print(f"[SESSION] 周末休市, 退出 ({now:%F %H:%M} NY)")
                break
            if now.time() < OPEN:
                wait = min((datetime.combine(now.date(), OPEN, NY) - now).total_seconds(),
                           3600)
                if rnd == 0 and wait > 5:
                    print(f"[SESSION] 尚未开盘, 等待 {wait/60:.0f} 分钟至 9:30 NY")
                    time.sleep(wait)
                    continue
                print("[SESSION] 等待后仍未开盘, 退出")
                break
            # 已收盘(工作日): 补跑最后一轮 —— 让"收盘对战日报"发得出去。
            # (交易脚本内部按日期去重 last_report, 同一天不会重复推送)
            print(f"[SESSION] 已收盘, 补跑一轮收盘日报后退出 ({now:%F %H:%M} NY)")
            rnd += 1
            run_step("follow_action.py")
            run_step("alpaca_trade.py")
            commit_state(rnd)
            break

        rnd += 1
        run_step("follow_action.py")
        run_step("alpaca_trade.py")
        commit_state(rnd)

        if MAX_ITER and rnd >= MAX_ITER:
            print(f"[SESSION] 达到测试轮数 {MAX_ITER}, 退出")
            break
        if time.time() - t0 > MAX_SECONDS:
            print(f"[SESSION] 达到时长上限 {MAX_SECONDS/3600:.1f}h, 退出")
            break

        nxt = datetime.now(NY) + timedelta(seconds=INTERVAL)
        print(f"[SESSION] 第 {rnd} 轮完成, 下次 {nxt:%H:%M:%S} NY", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
