#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全天候长会话 (GitHub Actions 专用)

背景: GitHub 的 schedule cron 不可靠(延迟/漏跑很常见), 靠 cron 准点没戏。
方案: 外部看门狗(腾讯云 SCF 每 5 分钟) / cron 只负责"启动会话",
      启动后本进程在云端连续轮询, 每 INTERVAL 秒跑一轮
      follow_action.py (监控盈立持仓) + alpaca_trade.py (镜像交易),
      直到超出监控窗(盘前 4:00 - 盘后 20:00 NY)或达到时长上限,
      期间定期把状态 commit 回仓库。

监控窗覆盖盘前/盘后: 盈立的调仓可能发生在夜盘(pre-market)/盘后
(after-hours), 旧版只在 9:30-16:00 轮询 -> 这些调仓既不被检测也不被跟单。
单次会话 5h(留在 GitHub 6h 硬上限内), 由守卫接力, 空窗最长约 10 分钟;
20:00 之后的会话补跑一轮收盘日报(覆盖盘后动作)后退出。

环境变量:
  LOOP_INTERVAL   每轮间隔秒数 (默认 90: 盈立调仓 -> 推送/镜像延迟 <=90s)
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
INTERVAL = int(os.environ.get("LOOP_INTERVAL", "90"))
# 盘前/盘后用更慢的节奏: 一是不用每 90 秒登录一次盈立(16 小时下来
# 600+ 次登录, 有被风控的风险), 二是延长时段调仓概率本来就低。
INTERVAL_EXT = int(os.environ.get("LOOP_INTERVAL_EXT", "180"))
MAX_SECONDS = float(os.environ.get("LOOP_MAX_HOURS", "5")) * 3600
MAX_ITER = int(os.environ.get("LOOP_MAX_ITER", "0"))
# 监控窗 = 美股完整时段(盘前 4:00 起 / 盘后到 20:00 NY)。
# 旧版只在 9:30-16:00 轮询: 盈立在盘前/夜盘的调仓既不会被检测,
# 也不会被镜像跟单(2026-10 下旬用户反馈)。现在全天候轮询,
# 具体能不能交易由 alpaca_trade.py 的盘口护栏决定。
MON_START = dtime(4, 0)
MON_END = dtime(20, 0)


def monitor_open(dt: datetime) -> bool:
    if dt.weekday() >= 5:
        return False
    return MON_START <= dt.time() < MON_END


def interval_now(dt: datetime) -> int:
    """常规时段 90s(调仓延迟 <=90s); 盘前/盘后 180s(降低登录频率)"""
    return INTERVAL if dtime(9, 30) <= dt.time() < dtime(16, 0) else INTERVAL_EXT


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

        if not monitor_open(now):
            if now.weekday() >= 5:
                print(f"[SESSION] 周末休市, 退出 ({now:%F %H:%M} NY)")
                break
            if now.time() < MON_START:
                # 凌晨(20:00 之后到次日 4:00): 没有盘口也没有调仓可跟,
                # 直接退出交给守卫 —— 守卫启动窗就是 4:00-20:10, 这里只是
                # fail-open 误启动时的兜底
                print(f"[SESSION] 未到盘前监控时段(4:00 NY), 退出 "
                      f"({now:%F %H:%M} NY)")
                break
            # 已过盘后时段(工作日 20:00 之后): 补跑最后一轮 —— 让
            # "收盘对战日报"发得出去(含盘后 16:00-20:00 的调仓与成交)。
            # 交易脚本内部按日期去重 last_report, 同一天不会重复推送。
            print(f"[SESSION] 已过盘后时段, 补跑一轮收盘日报后退出 "
                  f"({now:%F %H:%M} NY)")
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

        slp = interval_now(datetime.now(NY))
        nxt = datetime.now(NY) + timedelta(seconds=slp)
        print(f"[SESSION] 第 {rnd} 轮完成, {slp}s 后继续 "
              f"(下次 {nxt:%H:%M:%S} NY)", flush=True)
        time.sleep(slp)


if __name__ == "__main__":
    main()
