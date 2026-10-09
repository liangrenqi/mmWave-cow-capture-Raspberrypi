#!/usr/bin/env python3
"""h10_session.py 离线自检 —— 不需要 H10、不需要蓝牙、不碰雷达

用一个**假 logger**（本文件运行时写到临时目录）代替 h10_logger.py，
按场景往日志里写 EV/HR/PMD 行，验证 H10Session 自己的逻辑：

  1. dropout  连上 → 意外断开 → 重连：语音三连、status=DROPOUT、日志移进段目录
  2. ok       正常一段：只播「已连接」，结束时的 disconnected_at_end 不算断线
  3. missing  一直没扫到：播一次「未连接」、status=MISSING、finish(None) 归 H10_ORPHAN/
  4. hang     收到停止不退出：限时后 SIGKILL，finish 照样返回、文件照样移走
  5. disabled / 找不到 logger：不起进程、不抛异常
  6. 暂存目录遗留文件 → 下次 start 时移到 H10_ORPHAN/
  7. finish 可重复调用、提示器抛异常不外泄

测不到的（必须上 Pi 用真 H10 测）：bleak/BlueZ 行为、删绑定权限、真实重连。

Windows 上没有 os.killpg：用垫片把「发 SIGINT」换成写停止文件、「SIGKILL」换成 proc.kill()，
信号传递这一环在 Windows 上**没测**；Pi 上跑本脚本走真信号 + GNU timeout。

用法：
    python3 _test_h10_session.py
"""

import os
import shutil
import signal
import sys
import tempfile
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import h10_session as hs

FAKE_LOGGER = r'''
import argparse, os, signal, sys, time
ap = argparse.ArgumentParser()
ap.add_argument("--log-path"); ap.add_argument("--address")
ap.add_argument("--parent-pid"); ap.add_argument("--exit-grace")
ap.add_argument("--reconnect", action="store_true"); ap.add_argument("--remove-bond", action="store_true")
a, _ = ap.parse_known_args()
scn = os.environ.get("FAKE_H10_SCENARIO", "ok")
stopfile = a.log_path + ".stop"
stop = [False]
def on_sig(*_): stop[0] = True
signal.signal(signal.SIGINT, on_sig)
f = open(a.log_path, "w", encoding="utf-8", buffering=1)
def ev(s): rec("EV", s.encode().hex())
def rec(src, hx=""): f.write(f"{time.time_ns()} {time.monotonic_ns()} {src} {hx}\n")
f.write("# format=realtime_ns monotonic_ns src hex\n")
def stopped():
    return stop[0] or os.path.exists(stopfile)
ev("attempt 1"); ev("bond_remove rc=0 Device has been removed")
if scn == "missing":
    while not stopped():
        ev("scan_fail"); time.sleep(0.3)
    ev("stop_signal SIGINT"); ev("end"); f.close(); sys.exit(0)
ev("connected")
time.sleep(float(os.environ.get("FAKE_PMD_DELAY", "0")))   # 模拟 连上→首个 PMD 的延迟
t0, dropped = time.time(), False
while True:
    if scn == "hang":
        rec("PMD", "00"); time.sleep(0.2); continue      # 无视停止
    if stopped():
        break
    rec("HR", "0048"); rec("PMD", "00aabb")
    if scn == "dropout" and not dropped and time.time() - t0 > 1.0:
        dropped = True
        ev("disconnected"); ev("attempt 2"); ev("bond_remove rc=1 not available"); ev("connected")
    time.sleep(0.1)
ev("stop_signal SIGINT"); ev("disconnected_at_end"); ev("end")
f.write("# counts=...\n"); f.close()
'''


class RecNotifier:
    def __init__(self, explode=False):
        self.events = []
        self.explode = explode

    def emit(self, event, detail=""):
        self.events.append(event)
        if self.explode:
            raise RuntimeError("喇叭坏了（测试）")


FAILS = []


def check(cond, what):
    print(f"   [{'OK ' if cond else 'FAIL'}] {what}")
    if not cond:
        FAILS.append(what)


def install_windows_shim():
    """Windows 无 killpg：SIGINT → 写停止文件，SIGKILL → proc.kill()"""
    if hasattr(os, "killpg"):
        return False
    live = {}
    orig_start = hs.H10Session._start

    def _start(self):
        orig_start(self)
        if self.proc is not None:
            live[self.proc.pid] = self

    def killpg(pid, sig):
        s = live[pid]
        if sig == signal.SIGINT:
            open(s.log_path + ".stop", "w").close()
        else:
            s.proc.kill()

    hs.H10Session._start = _start
    os.killpg = killpg
    signal.SIGKILL = getattr(signal, "SIGKILL", 9)
    hs.shutil.which = lambda _: None    # Windows 的 timeout.exe 不是 GNU timeout
    return True


def run_case(tmp, name, scenario, run_sec, dest="session", **kw):
    print(f"\n== {name} ==")
    base = os.path.join(tmp, name)
    os.makedirs(base)
    os.environ["FAKE_H10_SCENARIO"] = scenario
    n = RecNotifier(explode=kw.get("explode", False))
    s = hs.H10Session(base, n, enabled=True, max_sec=60)
    s.start()
    time.sleep(run_sec)
    t_stop = datetime.now()
    s.request_stop()
    sess = os.path.join(base, "Cow_X_session") if dest == "session" else None
    if sess:
        os.makedirs(sess)
    t = time.monotonic()
    info = s.finish(sess)
    took = time.monotonic() - t
    again = s.finish(sess)
    check(again is info, "finish 重复调用返回同一结果")
    stage = os.path.join(base, hs.STAGING)
    # .stop 是 Windows 垫片自己的停止文件，不是被测代码产生的
    check(not [p for p in os.listdir(stage) if not p.endswith(".stop")], "暂存目录已清空")
    where = sess or os.path.join(base, hs.ORPHAN)
    got = sorted(os.listdir(where))
    check(any(f.endswith(".log") for f in got) and any(f.endswith(".stdout.txt") for f in got),
          f"日志与 stdout 已移到 {os.path.basename(where)}/：{got}")
    meta = hs.meta_lines(info, t_stop - timedelta(seconds=run_sec), t_stop)
    print("   meta:", " | ".join(m for m in meta if m and not m.startswith("# ")))
    print("   语音:", n.events, f" 收尾耗时 {took:.1f} s")
    return info, n.events, meta, took


def main():
    shim = install_windows_shim()
    print("平台垫片:", "Windows（信号传递未测）" if shim else "无（真信号 + timeout）")
    tmp = tempfile.mkdtemp(prefix="h10sess_")
    fake = os.path.join(tmp, "fake_logger.py")
    with open(fake, "w", encoding="utf-8") as f:
        f.write(FAKE_LOGGER)
    os.environ["H10_LOGGER"] = fake
    try:
        info, ev, meta, _ = run_case(tmp, "dropout", "dropout", 2.5)
        check(info["status"] == "DROPOUT", f"status=DROPOUT（实得 {info['status']}）")
        check(ev == ["h10_connected", "h10_lost", "h10_reconnected"], "语音 已连接→断开→已重连")
        check(info["connects"] == 2 and info["unexpected_disconnects"] == 1, "连接 2 次、意外断开 1 次")
        check(info["bond_remove_rc"] == "0,1", f"删绑定 rc 记录 0,1（实得 {info['bond_remove_rc']}）")
        check(hs.summary_word(info).startswith("PARTIAL"), "summary=PARTIAL")

        info, ev, meta, _ = run_case(tmp, "ok", "ok", 2.0)
        check(info["status"] == "OK", f"status=OK（实得 {info['status']}）")
        check(ev == ["h10_connected"], "只播「已连接」，disconnected_at_end 不算断线")
        check(info["pmd_notifications"] > 5 and info["hr_notifications"] > 5, "HR/PMD 计数 > 0")
        check(any(m.startswith("h10_pmd_lead_sec=") for m in meta)
              and any(m.startswith("h10_pmd_tail_sec=") for m in meta), "meta 含覆盖余量 lead/tail")

        hs.MISSING_AFTER = 1
        info, ev, meta, _ = run_case(tmp, "missing", "missing", 2.5, dest=None)
        hs.MISSING_AFTER = 45
        check(info["status"] == "MISSING", f"status=MISSING（实得 {info['status']}）")
        check(ev == ["h10_missing"], "「未连接」只播一次")
        check(info["scan_fails"] >= 3, "scan_fail 有计数")

        hs.STOP_WAIT = 2
        info, ev, meta, took = run_case(tmp, "hang", "hang", 1.0)
        hs.STOP_WAIT = 25
        check("SIGKILL" in (info.get("exit") or ""), f"卡死 → SIGKILL（exit={info.get('exit')}）")
        check(took < 10, "回收有上限")

        info, ev, meta, _ = run_case(tmp, "explode", "ok", 1.5, explode=True)
        check(info["status"] == "OK", "提示器抛异常不影响记录与收尾")

        print("\n== disabled / 找不到 logger ==")
        s = hs.H10Session(os.path.join(tmp, "dis"), None, enabled=False, max_sec=60)
        s.start()
        check(s.finish(None)["status"] == "DISABLED" and s.proc is None, "关闭时不起进程")
        os.environ["H10_LOGGER"] = os.path.join(tmp, "nope.py")
        hs.HERE = tmp                   # 回退路径也找不到
        s = hs.H10Session(os.path.join(tmp, "nolog"), None, enabled=True, max_sec=60)
        s.start()
        r = s.finish(None)
        check(r["status"] == "START_FAILED", f"找不到 logger → START_FAILED（{r.get('note')}）")
        check(hs.summary_word(r) == "MISSING（H10 进程未启动）", "summary 写明 H10 缺失")
        os.environ["H10_LOGGER"] = fake

        print("\n== wait_first_pmd ==")
        # a) PMD 延迟 1.5 s 到来：应在约 1.5–2.5 s 返回（尾随 0.5 s 一轮），且之后 lead ≥ 0
        os.environ["FAKE_H10_SCENARIO"], os.environ["FAKE_PMD_DELAY"] = "ok", "1.5"
        s = hs.H10Session(os.path.join(tmp, "wait_ok"), None, enabled=True, max_sec=60)
        s.start()
        t = time.monotonic(); s.wait_first_pmd(max_sec=10); took = time.monotonic() - t
        t0 = datetime.now()             # 相当于雷达 t0：等完之后才开始
        check(s.st["first_pmd"] is not None and 1.0 <= took <= 3.5,
              f"PMD 到了就返回（{took:.1f} s，{s.wait_note}）")
        r = s.finish(os.path.join(tmp, "wait_ok", "seg"))
        lead = [m for m in hs.meta_lines(r, t0) if m.startswith("h10_pmd_lead_sec=")]
        check(lead and float(lead[0].split("=")[1].split()[0]) >= 0, f"等完再开采 → lead ≥ 0（{lead}）")
        check(any(m.startswith("h10_wait_first_pmd=") for m in hs.meta_lines(r, t0)), "meta 记下等待结果")
        os.environ["FAKE_PMD_DELAY"] = "0"
        # b) 一直连不上：到 max_sec 超时返回，不抛
        os.environ["FAKE_H10_SCENARIO"] = "missing"
        s = hs.H10Session(os.path.join(tmp, "wait_to"), None, enabled=True, max_sec=60)
        s.start()
        t = time.monotonic(); s.wait_first_pmd(max_sec=2); took = time.monotonic() - t
        check(1.9 <= took <= 3.0 and "超时" in s.wait_note, f"连不上 → 超时返回（{took:.1f} s，{s.wait_note}）")
        s.finish(None)
        # c) 关闭 / 没起进程：立即返回
        s = hs.H10Session(os.path.join(tmp, "wait_dis"), None, enabled=False, max_sec=60)
        s.start()
        t = time.monotonic(); s.wait_first_pmd(max_sec=5); took = time.monotonic() - t
        check(took < 0.1, f"关闭时不等（{took:.2f} s）")
        s.finish(None)

        print("\n== 暂存遗留 ==")
        base = os.path.join(tmp, "sweep")
        os.makedirs(os.path.join(base, hs.STAGING))
        open(os.path.join(base, hs.STAGING, "h10_19990101_000000.log"), "w").close()
        os.environ["FAKE_H10_SCENARIO"] = "ok"
        s = hs.H10Session(base, None, enabled=True, max_sec=60)
        s.start()
        check(os.path.isfile(os.path.join(base, hs.ORPHAN, "h10_19990101_000000.log")),
              "遗留文件移到 H10_ORPHAN/")
        s.finish(os.path.join(base, "seg"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "；".join(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
