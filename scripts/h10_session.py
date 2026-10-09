#!/usr/bin/env python3
"""每段雷达采集配一个独立的 H10 记录进程（方案乙，用户 2026-09-30 逐点确认）

## 用户的六条决定在这里怎么落地

1. **一段一个进程、一段一个文件**：capture() 过完自检后拉起 h10_logger.py，
   段结束（或中止）时停掉，日志移进该段的雷达数据目录（正常 / BAD_ / ABORT_ 三种都一样）。
2. **断线自动重连**：交给 logger 的 `--reconnect --remove-bond`（重连前删绑定，避开 30 s 延迟）。
   断线、重连各播一次语音。
3. **H10 故障不判废雷达段**：本模块所有公开方法都吞掉异常，只打印、不抛；
   结果只写进 meta 的 H10 一节，不进 checks、不影响 verdict。
4. **语音提示**：h10_connected / h10_lost / h10_reconnected / h10_missing 四个事件。
5–6. 判据 3（RR 对齐）与 V2 判定都在 Windows 侧离线做，不在这里。

## 「独立于雷达采集进程」具体指什么

- **另一个操作系统进程**，`start_new_session=True` 自成进程组：守护进程 killpg 雷达进程组时
  信号不会直接打到它，它的崩溃、卡死、BLE 异常也碰不到雷达进程的内存与时序。
- 雷达进程对它只做三件事：拉起（Popen，不等）、发 SIGINT（不等）、最后限时回收。
  最长阻塞 STOP_WAIT 秒，且与 stop_record（本来就要 7 秒）并行，实际几乎不增加收尾时间。
- 三重兜底防孤儿：logger 自己看护父 PID；外面套 `timeout -k`（最长时长 + 硬杀）；
  回收超时再 killpg SIGKILL。
- 语音事件来自**尾随日志文件**的 EV 行，不读子进程管道 —— 管道不读满了会反过来卡死 logger。
  logger 的终端输出写到同目录 `.stdout.txt`，一起归档，排查用。
"""

import glob
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))

H10_ADDRESS = os.environ.get("H10_ADDRESS", "24:AC:AC:11:CC:D4")
STAGING = "_h10_staging"        # 采集中 H10 日志先写这里（fileBasePath 下），段结束再移走
ORPHAN = "H10_ORPHAN"           # 雷达没产生数据目录时（准备阶段就失败），H10 日志归这里
EXIT_GRACE = 10                 # logger 收到停止后自己的收尾上限（秒），超时它 fsync 后自退
KILL_AFTER = 20                 # timeout -k：转发停止信号后再过这么久直接 SIGKILL
STOP_WAIT = 25                  # 雷达进程回收 H10 的最长等待；须 > KILL_AFTER
MISSING_AFTER = 45              # 拉起后这么久还没连上，播一次「心率带未连接」
MAX_MARGIN = 300                # timeout 总时长 = 段时长 + 缓冲 + 这么多秒
FIRST_PMD_WAIT = 10             # 配 DCA1000 之前最多等 H10 首个 PMD 这么久（秒），超时雷达照常开采。
                                # V2（10-09）实测拉起→首个 PMD 6.3–6.8 s，与 sensorStart 几乎同时，第 3 段晚 0.3 s


def _find_logger():
    p = os.environ.get("H10_LOGGER") or os.path.join(HERE, "h10_logger.py")
    return p if os.path.isfile(p) else None


class H10Session:
    """一段采集的 H10 记录。用法（capture_linux.py 里）：

        h10 = H10Session(base, notifier, enabled=True, max_sec=wait_sec)
        h10.start()                 # 自检通过后、配 DCA1000 之前
        h10.wait_first_pmd()        # 最多等 FIRST_PMD_WAIT 秒，让 H10 先出数据
        ...                         # 雷达采集
        h10.request_stop()          # [6] 停止时，不等
        info = h10.finish(session)  # [7] 整理时，移进段目录，返回 meta 用的字典
    """

    def __init__(self, base, notifier=None, enabled=True, max_sec=0):
        self.base = base
        self.notifier = notifier
        self.enabled = enabled
        self.max_sec = max_sec
        self.proc = None
        self.log_path = None
        self.out_path = None
        self.t_start = None
        self.status_note = ""
        self.result = None              # finish() 之后缓存，重复调用直接返回
        self.wait_note = ""             # wait_first_pmd() 的结果，写进 meta
        self._stopping = False
        self._mon = None
        self._mon_stop = threading.Event()
        self._lock = threading.Lock()
        # 尾随日志得到的计数
        self.st = dict(connects=0, lost=0, scan_fail=0, session_err=0, connect_fail=0,
                       hr=0, pmd=0, first_conn=None, first_hr=None, first_pmd=None,
                       last_pmd=None, exit_forced=False, bond_rc=[], parent_gone=False)

    # ---------------- 提示 ----------------
    def _say(self, event, detail=""):
        try:
            if self.notifier is not None:
                self.notifier.emit(event, detail)
            else:
                print(f"  [H10] {event} {detail}", flush=True)
        except Exception as e:
            print(f"  [H10] 提示失败（忽略）: {e}", flush=True)

    # ---------------- 启动 ----------------
    def start(self):
        try:
            self._start()
        except Exception as e:
            self.status_note = f"启动异常 {type(e).__name__}: {e}"
            print(f"  [H10] {self.status_note}（雷达照常采集）", flush=True)
            self.proc = None

    def _start(self):
        if not self.enabled:
            self.status_note = "已关闭（--no-h10 或 CAPTURE_H10=0）"
            return
        logger = _find_logger()
        if logger is None:
            self.status_note = f"找不到 h10_logger.py（应在 {HERE}）"
            print(f"  [H10] {self.status_note}，本段不采心率带", flush=True)
            return

        stage = os.path.join(self.base, STAGING)
        os.makedirs(stage, exist_ok=True)
        self._sweep_staging(stage)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = os.path.join(stage, f"h10_{stamp}.log")
        self.out_path = self.log_path + ".stdout.txt"
        total = int(self.max_sec + MAX_MARGIN) if self.max_sec else 0
        cmd = []
        if total and shutil.which("timeout"):
            cmd += ["timeout", "-k", str(KILL_AFTER), "-s", "INT", str(total)]
        cmd += [sys.executable, "-u", logger,
                "--address", H10_ADDRESS,
                "--log-path", self.log_path,
                "--reconnect", "--remove-bond",
                "--parent-pid", str(os.getpid()),
                "--exit-grace", str(EXIT_GRACE)]
        out = open(self.out_path, "w", encoding="utf-8")
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out,
                                         stderr=subprocess.STDOUT, start_new_session=True,
                                         cwd=HERE)
        finally:
            out.close()                 # 子进程已继承文件描述符，父进程这份关掉
        self.t_start = time.monotonic()
        print(f"  [H10] 记录进程已拉起 pid {self.proc.pid} → {os.path.basename(self.log_path)}",
              flush=True)
        self._mon = threading.Thread(target=self._monitor, daemon=True)
        self._mon.start()

    def wait_first_pmd(self, max_sec=FIRST_PMD_WAIT):
        """阻塞到尾随日志看到首个 PMD，或 max_sec 超时，或 logger 已退出。只等、不判，超时不抛。
        尾随线程 0.5 s 一轮，所以看到的时刻最多比实际晚约 0.5 s（只会让余量更大）。"""
        try:
            if self.proc is None:
                return None
            t = time.monotonic()
            while True:
                waited = time.monotonic() - t
                if self.st["first_pmd"]:
                    self.wait_note = f"{waited:.1f} s 后见到首个 PMD"
                    break
                if self.proc.poll() is not None:
                    self.wait_note = f"logger 已退出（rc {self.proc.returncode}），等了 {waited:.1f} s"
                    break
                if waited >= max_sec:
                    self.wait_note = f"{max_sec:.0f} s 未见 PMD，超时，雷达照常开采"
                    break
                time.sleep(0.2)
            print(f"  [H10] 等首个 PMD：{self.wait_note}", flush=True)
            return self.wait_note
        except Exception as e:
            self.wait_note = f"等待异常 {type(e).__name__}: {e}"
            print(f"  [H10] {self.wait_note}（雷达照常采集）", flush=True)
            return None

    def _sweep_staging(self, stage):
        """上次崩溃留在暂存目录的文件移到 H10_ORPHAN/，不删"""
        left = glob.glob(os.path.join(stage, "h10_*"))
        if not left:
            return
        dst = os.path.join(self.base, ORPHAN)
        os.makedirs(dst, exist_ok=True)
        for p in left:
            shutil.move(p, os.path.join(dst, os.path.basename(p)))
        print(f"  [H10] 暂存目录有 {len(left)} 个上次遗留文件，已移到 {ORPHAN}/", flush=True)

    # ---------------- 尾随日志 ----------------
    def _monitor(self):
        f, buf, warned_missing = None, "", False
        try:
            while not self._mon_stop.is_set():
                if f is None:
                    if os.path.isfile(self.log_path):
                        f = open(self.log_path, "r", encoding="utf-8", errors="replace")
                    else:
                        self._mon_stop.wait(0.5)
                        continue
                chunk = f.read()
                if chunk:
                    buf += chunk
                    *lines, buf = buf.split("\n")
                    for ln in lines:
                        self._on_line(ln)
                else:
                    if (not warned_missing and self.st["connects"] == 0 and not self._stopping
                            and time.monotonic() - self.t_start > MISSING_AFTER):
                        warned_missing = True
                        self._say("h10_missing", f"{MISSING_AFTER} 秒未连上，继续重试；雷达照常采集")
                    if self.proc is not None and self.proc.poll() is not None:
                        # 进程已退出：把剩下的读完就收工
                        rest = f.read()
                        for ln in (buf + rest).split("\n"):
                            self._on_line(ln)
                        buf = ""
                        break
                    self._mon_stop.wait(0.5)
            if f is not None:           # finish() 叫停时也把剩余行读完，计数才完整
                for ln in (buf + f.read()).split("\n"):
                    self._on_line(ln)
        except Exception as e:
            print(f"  [H10] 日志尾随出错（不影响记录本身）: {e}", flush=True)
        finally:
            if f is not None:
                f.close()

    def _on_line(self, ln):
        if not ln or ln.startswith("#"):
            return
        parts = ln.split()
        if len(parts) < 3:
            return
        src = parts[2]
        try:
            rt = int(parts[0])
        except ValueError:
            return
        s = self.st
        if src == "HR":
            s["hr"] += 1
            s["first_hr"] = s["first_hr"] or rt
            return
        if src == "PMD":
            s["pmd"] += 1
            s["first_pmd"] = s["first_pmd"] or rt
            s["last_pmd"] = rt
            return
        if src != "EV":
            return
        try:
            ev = bytes.fromhex(parts[3]).decode(errors="replace") if len(parts) > 3 else ""
        except ValueError:
            return
        if ev == "connected":
            s["connects"] += 1
            if s["connects"] == 1:
                s["first_conn"] = rt
                self._say("h10_connected")
            elif not self._stopping:
                self._say("h10_reconnected", f"第 {s['connects']} 次连接")
        elif ev == "disconnected":
            s["lost"] += 1
            if not self._stopping:
                self._say("h10_lost", "自动重连中；雷达照常采集")
        elif ev == "scan_fail":
            s["scan_fail"] += 1
        elif ev.startswith("session_error"):
            s["session_err"] += 1
        elif ev.startswith("connect_fail"):
            s["connect_fail"] += 1
        elif ev.startswith("exit_forced"):
            s["exit_forced"] = True
        elif ev.startswith("parent_gone"):
            s["parent_gone"] = True
        elif ev.startswith("bond_remove rc="):
            s["bond_rc"].append(ev.split()[1][3:])

    # ---------------- 停止 ----------------
    def request_stop(self):
        """发 SIGINT 给整个进程组（timeout + logger），立即返回"""
        try:
            self._stopping = True
            if self.proc is not None and self.proc.poll() is None:
                os.killpg(self.proc.pid, signal.SIGINT)
        except Exception as e:
            print(f"  [H10] 发停止信号失败（忽略）: {e}", flush=True)

    def _reap(self):
        if self.proc is None:
            return None, ""
        self.request_stop()
        how, killed = "正常退出", False
        try:
            rc = self.proc.wait(timeout=STOP_WAIT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except Exception:
                pass
            try:
                rc = self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rc = None
            how, killed = f"{STOP_WAIT} 秒未退出，已 SIGKILL", True
        # 只有不是自己杀的才归给 timeout：自己 killpg 后 rc 也是 -9，不能混记
        if not killed and rc is not None and rc in (124, 137, -9):
            how = f"被 timeout 终止（rc {rc}）"
        if self.st["exit_forced"]:
            how = f"logger 收尾超时自退（exit_forced，数据已 fsync）"
        return rc, how

    def finish(self, dest_dir):
        """停掉进程、把日志移进 dest_dir（None = 移进 H10_ORPHAN/），返回 meta 用的字典。可重复调用。"""
        with self._lock:
            if self.result is not None:
                return self.result
            try:
                self.result = self._finish(dest_dir)
            except Exception as e:
                self.result = dict(status="ERROR", note=f"收尾异常 {type(e).__name__}: {e}")
                print(f"  [H10] {self.result['note']}", flush=True)
            return self.result

    def _finish(self, dest_dir):
        if self.proc is None:
            return dict(status="DISABLED" if not self.enabled else "START_FAILED",
                        note=self.status_note)
        rc, how = self._reap()
        self._mon_stop.set()
        if self._mon is not None:
            self._mon.join(timeout=5)

        if dest_dir is None:
            dest_dir = os.path.join(self.base, ORPHAN)
        os.makedirs(dest_dir, exist_ok=True)
        moved = []
        for p in (self.log_path, self.out_path):
            if p and os.path.isfile(p):
                dst = os.path.join(dest_dir, os.path.basename(p))
                shutil.move(p, dst)
                moved.append(dst)
        log_dst = os.path.join(dest_dir, os.path.basename(self.log_path))
        size = os.path.getsize(log_dst) if os.path.isfile(log_dst) else 0

        s = self.st
        if s["connects"] == 0:
            status = "MISSING"
        elif s["lost"] > 0:
            status = "DROPOUT"
        elif s["pmd"] == 0:
            status = "NO_PMD"
        else:
            status = "OK"
        info = dict(status=status, file=os.path.basename(log_dst), bytes=size, rc=rc, exit=how,
                    connects=s["connects"], unexpected_disconnects=s["lost"],
                    scan_fails=s["scan_fail"], connect_fails=s["connect_fail"],
                    session_errors=s["session_err"], hr_notifications=s["hr"],
                    pmd_notifications=s["pmd"], bond_remove_rc=",".join(s["bond_rc"]),
                    first_connect_ns=s["first_conn"], first_hr_ns=s["first_hr"],
                    first_pmd_ns=s["first_pmd"], last_pmd_ns=s["last_pmd"],
                    parent_gone=s["parent_gone"], wait_first_pmd=self.wait_note, dest=dest_dir)
        print(f"  [H10] {status}：连接 {s['connects']} 次、意外断开 {s['lost']} 次、"
              f"HR {s['hr']} / PMD {s['pmd']} 条，{how} → {os.path.relpath(log_dst, self.base)}",
              flush=True)
        return info


def meta_lines(info, t0=None, t1=None):
    """capture_meta.txt 的 H10 一节。t0/t1 = 雷达采集窗口（datetime），用来写覆盖余量。"""
    out = ["", "# ----- 心率带 H10（独立进程，不参与判定；V2 判据离线做）-----"]
    if not info:
        out.append("h10_status=UNKNOWN")
        return out
    out.append(f"h10_status={info.get('status')}")
    if info.get("note"):
        out.append(f"h10_note={info['note']}")
    for k in ("file", "bytes", "exit", "rc", "connects", "unexpected_disconnects", "scan_fails",
              "connect_fails", "session_errors", "hr_notifications", "pmd_notifications",
              "bond_remove_rc", "parent_gone", "wait_first_pmd"):
        if k in info:
            out.append(f"h10_{k}={info[k]}")
    fp, lp = info.get("first_pmd_ns"), info.get("last_pmd_ns")
    if fp and t0 is not None:
        lead = t0.timestamp() - fp / 1e9
        out.append(f"h10_pmd_lead_sec={lead:+.1f}   # 正 = H10 首个 PMD 包早于雷达开始（按 Pi 到达时刻）")
    if lp and t1 is not None:
        tail = lp / 1e9 - t1.timestamp()
        out.append(f"h10_pmd_tail_sec={tail:+.1f}   # 正 = H10 末个 PMD 包晚于雷达结束")
    for k in ("first_connect_ns", "first_hr_ns", "first_pmd_ns", "last_pmd_ns"):
        if info.get(k):
            out.append(f"h10_{k}={info[k]}")
    return out


def summary_word(info):
    """判定一节里的一行：一眼看出心率带这段有没有、全不全"""
    if not info:
        return "UNKNOWN"
    st = info.get("status")
    return {"OK": "OK", "DISABLED": "DISABLED", "MISSING": "MISSING（H10 缺失）",
            "START_FAILED": "MISSING（H10 进程未启动）", "NO_PMD": "PARTIAL（只有 HR，无 ECG/ACC）",
            "DROPOUT": f"PARTIAL（意外断开 {info.get('unexpected_disconnects')} 次）"}.get(st, st)
