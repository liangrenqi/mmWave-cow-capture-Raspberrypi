#!/usr/bin/env python3
"""遥控守护进程 —— 开机常驻，用 POWER 键双击启停采集程序

角色分工（两层遥控，别混）：

    remote_daemon.py   （本文件，开机自启，永不退出）
        POWER × 2  ->  启动 capture_linux.py --remote
        POWER × 2  ->  退出 capture_linux.py（仅在它空闲时）

    capture_linux.py --remote  （被本文件拉起的子进程）
        ENTER × 2  ->  开始一段采集
        ESC        ->  紧急中止本段

所以 POWER 管"程序开关"，ENTER/ESC 管"采集开关"。封盒后饲养员只需记
两件事：POWER 双击开机待命，ENTER 双击采一段。

## 三个必须这样做的理由

**1. POWER 键在 event7，与 ENTER/ESC 的 event9 不是同一个节点。**
实测 Genius 遥控的键分布：
    event7  System Control     KEY_POWER / KEY_SLEEP / KEY_WAKEUP
    event8  Consumer Control   音量键等 155 个
    event9  键盘接口           ENTER / ESC / 方向键等 163 个
RemoteController 筛的是"能报 ENTER 且能报 ESC"，event7 匹配不上，
故本文件自己找设备、只认 KEY_POWER。

**2. ★ POWER 键默认会让 systemd 关机。**
`HandlePowerKey=poweroff` 是 systemd 默认值，且实测 event0/7/8/9
四个节点全部带 udev 的 `power-switch` 标签，都在 logind 监听范围内。
手动测试时没关机是因为桌面会话挂了一把 `handle-power-key block` 锁
（`gtk-nop`）—— **那把锁靠不住**：封盒后若改命令行启动、或本服务在桌面
之前起来，按两下 POWER 就直接关机，采集全废。

必须装这个 drop-in（不改原文件）：

    /etc/systemd/logind.conf.d/99-radar-remote.conf
        [Login]
        HandlePowerKey=ignore
        HandlePowerKeyLongPress=ignore

代价：Pi 的 POWER 键不再关机（含板载电源键），关机改用 `sudo poweroff`。
对封盒设备来说这正是想要的 —— 遥控器不该能关机。
本文件启动时会自检这条配置，缺了就大声警告。

**3. 采集进行中拒绝退出。**
硬杀子进程会留下两个后果：雷达没收到 sensorStop 会继续发射到帧数跑完；
游离文件留在 fileBasePath 根目录，**下次启动被残留检查挡死**
（封盒后没有屏幕，现象就是"遥控器坏了"）。
故按退出时先读子进程写的状态文件，只有 IDLE 才放行，否则播"忙"提示音。
要中止当前采集请先按 ESC。

用法：
    python3 remote_daemon.py              # 前台跑，调试用
    python3 remote_daemon.py --check      # 只自检环境，不进主循环
    systemctl start radar-remote          # 装成服务后
"""

import argparse
import os
import selectors
import signal
import subprocess
import sys
import threading
import time

try:
    import evdev
    from evdev import ecodes
except ImportError:
    sys.exit("[ERROR] 需要 python3-evdev： sudo apt install python3-evdev")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import remote_control as rcmod


# ============== 参数 ==============
POWER_KEY = "power"        # 启停程序用的键（遥控器电源键）
DOUBLE_WINDOW = 2.0        # 双击窗口，与 remote_control 一致
MIN_GAP = 0.05             # 去抖，与 remote_control 一致（实测定的值）
RESCAN_SEC = 5.0           # 重扫输入设备周期（支持 dongle 热插拔）

CAPTURE_SCRIPT = os.path.join(HERE, "capture_linux.py")
CAPTURE_ARGS = ["--mode", "vitalsigns", "--remote"]

# 子进程状态文件：capture_linux.py 每次状态迁移都写它
STATE_FILE = os.path.join(HERE, ".remote_state")

# 优雅退出的等待上限。子进程要发 sensorStop、等在途数据、停 tcpdump、
# 归档 —— 实测中止流程约 10 秒，给足余量。
GRACEFUL_WAIT = 45.0

LOGIND_DROPIN = "/etc/systemd/logind.conf.d/99-radar-remote.conf"
# ==================================


class DaemonNotifier:
    """守护进程自己的语音提示 —— 与采集程序用同一个喇叭、不同的音频文件

    单独一套文件（daemon_*.wav）而不复用采集程序的：
    "程序启动了"和"采集开始了"是两件事，混用会让饲养员分不清现在
    到底是待命还是在采。
    """

    _TEXT = {
        "daemon_ready":  "守护进程就绪 —— 双击 [POWER] 启动采集程序",
        "app_starting":  "采集程序启动中",
        "app_exiting":   "采集程序退出中",
        "app_exited":    "采集程序已退出，回到待命",
        "busy":          "采集进行中，无法退出 —— 请先按 [ESC] 中止",
        "app_died":      "采集程序意外退出",
    }

    _AUDIO = {
        "daemon_ready": "daemon_ready.wav",
        "app_starting": "daemon_start.wav",
        "app_exiting":  "daemon_exit.wav",
        "busy":         "daemon_busy.wav",
        "app_died":     "error.wav",        # 复用采集程序的出错音
    }

    def __init__(self, use_audio=True):
        self.audio = None
        if use_audio:
            # 复用 AudioNotifier 的设备探测、串行播放队列与容错逻辑，
            # 只把事件->文件的映射换成守护进程这一套
            self.audio = rcmod.AudioNotifier(audio_map=self._AUDIO)

    def emit(self, event, detail=""):
        text = self._TEXT.get(event, event)
        line = f"  [守护] {text}"
        if detail:
            line += f" —— {detail}"
        print(line, flush=True)
        if self.audio is not None:
            fn = self._AUDIO.get(event)
            if fn:
                # 直接走播放队列，不再让 AudioNotifier 打一遍日志
                path = os.path.join(self.audio.sounds_dir, fn)
                if os.path.isfile(path):
                    self.audio._queue.put(path)

    def close(self):
        if self.audio is not None:
            self.audio.close()


def check_logind():
    """确认 POWER 键不会触发关机 —— 这是上自启前的硬性前提

    返回 True 表示安全。不安全时**不阻止启动**（可能是有意为之），
    但要把风险和修法说清楚。
    """
    if os.path.isfile(LOGIND_DROPIN):
        try:
            with open(LOGIND_DROPIN) as f:
                body = f.read()
            if "HandlePowerKey=ignore" in body.replace(" ", ""):
                return True
        except OSError:
            pass

    # 也接受直接改了主配置的情形
    try:
        with open("/etc/systemd/logind.conf") as f:
            for line in f:
                s = line.strip().replace(" ", "")
                if s.startswith("HandlePowerKey=") and s.endswith("ignore"):
                    return True
    except OSError:
        pass

    print("=" * 66, flush=True)
    print("  ⚠ 警告：POWER 键可能触发系统关机", flush=True)
    print("=" * 66, flush=True)
    print("  systemd 的 HandlePowerKey 默认值是 poweroff，而遥控器的", flush=True)
    print("  POWER 键所在节点带 udev 的 power-switch 标签，在 logind", flush=True)
    print("  的监听范围内。桌面会话虽然会挂一把 handle-power-key 锁，", flush=True)
    print("  但那把锁在命令行启动或桌面崩溃后就没了 —— 届时按两下", flush=True)
    print("  POWER 会直接关机，正在进行的采集全废。", flush=True)
    print("", flush=True)
    print("  修法（新建 drop-in，不动原配置）：", flush=True)
    print(f"    sudo mkdir -p {os.path.dirname(LOGIND_DROPIN)}", flush=True)
    print(f"    sudo tee {LOGIND_DROPIN} <<'EOF'", flush=True)
    print("    [Login]", flush=True)
    print("    HandlePowerKey=ignore", flush=True)
    print("    HandlePowerKeyLongPress=ignore", flush=True)
    print("    EOF", flush=True)
    print("    sudo systemctl restart systemd-logind", flush=True)
    print("", flush=True)
    print("  副作用：Pi 的 POWER 键不再关机（含板载电源键），", flush=True)
    print("  关机改用 sudo poweroff。封盒设备正需如此。", flush=True)
    print("=" * 66, flush=True)
    return False


def read_child_state(expect_pid=None):
    """读子进程写的状态。读不到、或不是当前子进程写的，都返回 None

    **不能用"时间戳超过 N 秒就算陈旧"来判断**（这是我第一版的错）：
      - IDLE 可以持续几小时不变（待命），而它恰恰是唯一允许退出的状态，
        按时间判陈旧会导致待命久了反而永远退不出去
      - CAPTURING 在 45 分钟采集里也是 2700 秒不变

    真正要防的是"上次运行留下的陈旧文件"，而那用 PID 比对更准：
    文件里记的 PID 与当前子进程不符 ⇒ 是遗留文件，不可信。
    时间戳只留作日志排查用，不参与判定。
    """
    try:
        with open(STATE_FILE) as f:
            lines = f.read().split("\n")
        state = lines[0].strip()
        pid = int(lines[2].strip()) if len(lines) > 2 and lines[2].strip() else 0
    except (OSError, ValueError, IndexError):
        return None
    if not state:
        return None
    # PID 不符 = 上次运行遗留的文件。expect_pid 为 None 时不校验
    # （--check 之类的场景只想看看文件内容）。
    if expect_pid is not None and pid and pid != expect_pid:
        return None
    return state


class PowerWatcher:
    """只监听 POWER 键的双击

    与 RemoteController 的按键逻辑刻意保持一致（去抖 MIN_GAP、
    双击窗口、只认 value==1 忽略长按重复），但设备筛选不同：
    这里找的是"能报 KEY_POWER"的节点，而不是"能报 ENTER 且 ESC"。
    """

    def __init__(self, on_double, grab=True):
        self.code = rcmod._key_code(POWER_KEY)
        self.on_double = on_double
        self.grab = grab
        self._quit = threading.Event()
        self._devices = {}
        self._last_press = None
        self._first_press = None
        self._thread = None
        self.rejected = []          # find_devices 排除掉的节点及原因

    def find_devices(self):
        """找出"只报 POWER、不报采集键"的节点

        ★ 筛选必须比"能报 KEY_POWER"严格得多，否则会踩两个坑。
        实测 `--check` 的结果，5 个节点都声明了 KEY_POWER：

            event1  vc4-hdmi-0                            ← HDMI，与遥控无关
            event3  vc4-hdmi-1                            ← HDMI
            event7  Genius ... System Control  (3 键)      ← 真正报 POWER 的
            event8  Genius ... Consumer Control (155 键)   ← 采集程序在用
            event9  Genius Wireless Device      (163 键)   ← ★ ENTER/ESC 在这

        **坑一（致命）**：event9 是 ENTER/ESC 所在的节点。本类默认 grab()
        独占，一旦独占了 event9，capture_linux.py 就再也收不到 ENTER/ESC，
        遥控采集彻底失灵 —— 而且不报错，表现为"按了没反应"。

        **坑二**：HDMI 节点声明 KEY_POWER 是 CEC 相关，独占它没意义。

        故三条排除规则：
          1. 排除能报采集键（ENTER 或 ESC）的节点 —— 那是子进程的地盘
          2. 排除 pwr_button（板载电源键，不该用来启停采集程序）
          3. 排除 vc4-hdmi（HDMI CEC）

        剩下的正好是 event7，与实测"POWER 只在 event7 报事件"一致。
        """
        start_code = rcmod._key_code(rcmod.START_KEY)
        stop_code = rcmod._key_code(rcmod.STOP_KEY)
        found, rejected = {}, []

        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue

            def drop(reason):
                rejected.append((path, dev.name, reason))
                try:
                    dev.close()
                except OSError:
                    pass

            try:
                caps = dev.capabilities().get(ecodes.EV_KEY, [])
            except OSError:
                drop("读不到 capabilities")
                continue

            if self.code not in caps:
                drop("不报 POWER")
                continue

            low = dev.name.lower()
            if "pwr_button" in low:
                drop("板载电源键")
                continue
            if "hdmi" in low:
                drop("HDMI CEC")
                continue
            # ★ 关键一条：这个节点是采集程序用的，绝不能独占
            if start_code in caps or stop_code in caps:
                drop(f"同时报 {rcmod.START_KEY}/{rcmod.STOP_KEY}，"
                     "是采集程序的设备")
                continue

            found[path] = dev

        self.rejected = rejected
        return found

    def _refresh(self, announce=False):
        current = self.find_devices()
        for path in set(self._devices) - set(current):
            dev = self._devices.pop(path, None)
            if dev is not None:
                try:
                    dev.close()
                except OSError:
                    pass
            print(f"  [守护] POWER 设备已移除: {path}", flush=True)

        for path in set(current) - set(self._devices):
            dev = current[path]
            self._devices[path] = dev
            if self.grab:
                # 独占很重要：不独占时 POWER 键仍会传给 logind/桌面。
                # 即使配了 HandlePowerKey=ignore，桌面环境也可能自己弹
                # 关机对话框。独占后按键只到我们这里。
                try:
                    dev.grab()
                except OSError as e:
                    print(f"  [守护] 独占 {path} 失败（继续）: {e}", flush=True)
            if announce:
                print(f"  [守护] 监听 POWER: {dev.name}  ({path})", flush=True)

        for path, dev in current.items():
            if self._devices.get(path) is not dev:
                try:
                    dev.close()
                except OSError:
                    pass

    def _loop(self):
        while not self._quit.is_set():
            self._refresh(announce=True)
            if not self._devices:
                self._quit.wait(RESCAN_SEC)
                continue

            sel = selectors.DefaultSelector()
            for dev in self._devices.values():
                try:
                    sel.register(dev, selectors.EVENT_READ)
                except (OSError, ValueError):
                    pass

            deadline = time.time() + RESCAN_SEC
            try:
                while not self._quit.is_set() and time.time() < deadline:
                    for key, _ in sel.select(timeout=0.3):
                        dev = key.fileobj
                        try:
                            for ev in dev.read():
                                # 只认按下；value==2 是长按自动重复，
                                # 按住不放不该被当成双击
                                if (ev.type == ecodes.EV_KEY
                                        and ev.code == self.code
                                        and ev.value == 1):
                                    self._on_press()
                        except OSError:
                            deadline = 0
                            break
            finally:
                sel.close()

    def _on_press(self):
        now = time.time()
        if self._last_press is not None and now - self._last_press < MIN_GAP:
            return                      # 抖动/重复上报
        self._last_press = now

        if self._first_press is not None and now - self._first_press <= DOUBLE_WINDOW:
            self._first_press = None
            self.on_double()
        else:
            self._first_press = now
            print(f"  [守护] POWER 已按一次，{DOUBLE_WINDOW:.0f} 秒内再按一次"
                  "生效", flush=True)

    def start(self):
        devs = self.find_devices()
        if not devs:
            print("  [守护] 警告：没找到能报 POWER 的输入设备，"
                  f"每 {RESCAN_SEC:.0f} 秒重扫", flush=True)
        for d in devs.values():
            try:
                d.close()
            except OSError:
                pass
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def close(self):
        self._quit.set()
        if self._thread:
            self._thread.join(timeout=2)
        for dev in self._devices.values():
            try:
                if self.grab:
                    dev.ungrab()
            except OSError:
                pass
            try:
                dev.close()
            except OSError:
                pass
        self._devices.clear()


class Daemon:
    def __init__(self, args):
        self.args = args
        self.notifier = DaemonNotifier(use_audio=not args.no_audio)
        self.child = None
        self._lock = threading.Lock()
        self._quit = threading.Event()

    # ---------- 子进程管理 ----------

    def child_running(self):
        return self.child is not None and self.child.poll() is None

    def start_child(self):
        if self.child_running():
            print("  [守护] 采集程序已在运行", flush=True)
            return

        # 清掉可能残留的旧状态文件，免得刚启动就读到上次的 CAPTURING
        try:
            os.remove(STATE_FILE)
        except OSError:
            pass

        argv = [sys.executable, CAPTURE_SCRIPT] + CAPTURE_ARGS \
            + ["--state-file", STATE_FILE]
        if self.args.no_audio:
            argv.append("--no-audio")
        # 默认让子进程独占 ENTER/ESC 所在设备。
        # **这不是可选的美化项，而是必需的**：evdev 读取事件**不消费**它 ——
        # 内核输入子系统会把同一次按键同时投递给 evdev 客户端**和**常规
        # 键盘处理器（即控制台/tty）。所以不独占时遥控器仍是一个普通键盘：
        #   ENTER 会在当前活动控制台上"回车"（tty1 上正跑着 bash，
        #         等于往 root/pi 的 shell 里敲回车，执行掉行内残留内容）
        #   ESC   会被编辑器、菜单当成转义键
        # 实测 tty1 上确有 login+bash 在跑，活动 VT 是 7。
        # 封盒后无人看管，这种误输入没人能发现。
        if not self.args.no_grab_capture:
            argv.append("--grab")

        self.notifier.emit("app_starting")
        try:
            # start_new_session：给子进程独立的进程组，这样我们能用
            # os.killpg 把它连同它起的 tcpdump/CLI 一起收拾干净。
            # 但也意味着它收不到我们终端的 Ctrl-C，得显式转发信号。
            self.child = subprocess.Popen(argv, cwd=HERE,
                                          start_new_session=True)
            print(f"  [守护] 已启动 PID {self.child.pid}: "
                  f"{' '.join(os.path.basename(a) for a in argv[:2])} "
                  f"{' '.join(CAPTURE_ARGS)}", flush=True)
        except Exception as e:
            self.child = None
            self.notifier.emit("app_died", f"启动失败: {e}")

    def stop_child(self):
        """优雅退出子进程 —— 只在它空闲时调用（调用方已检查过状态）"""
        if not self.child_running():
            return

        self.notifier.emit("app_exiting")
        # SIGINT 等价于 Ctrl-C：capture_linux 的 _remote_loop 捕获它并
        # 正常收尾（rc.close() + notifier.close()）。
        # 用 killpg 而不是 child.send_signal：子进程组里可能还有
        # tcpdump 等，一起通知到。
        try:
            os.killpg(os.getpgid(self.child.pid), signal.SIGINT)
        except (OSError, ProcessLookupError):
            pass

        t0 = time.time()
        while time.time() - t0 < GRACEFUL_WAIT:
            if self.child.poll() is not None:
                break
            time.sleep(0.3)
        else:
            print(f"  [守护] {GRACEFUL_WAIT:.0f} 秒内没退出，升级到 SIGTERM",
                  flush=True)
            try:
                os.killpg(os.getpgid(self.child.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            time.sleep(3)
            if self.child.poll() is None:
                print("  [守护] 仍未退出，SIGKILL", flush=True)
                try:
                    os.killpg(os.getpgid(self.child.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass

        rc = self.child.poll()
        self.child = None
        try:
            os.remove(STATE_FILE)
        except OSError:
            pass
        self.notifier.emit("app_exited", f"退出码 {rc}")

    # ---------- POWER 双击的处理 ----------

    def on_power_double(self):
        # 串行化：双击回调来自监听线程，start/stop 都不是原子的
        with self._lock:
            if not self.child_running():
                self.start_child()
                return

            # 采集程序在跑 —— 只有它空闲时才允许退出
            state = read_child_state(expect_pid=self.child.pid)
            if state is None:
                # 读不到状态：可能刚启动还没写、也可能文件坏了。
                # 保守起见按"可能在采集"处理，让用户先按 ESC。
                # 这是有意的不对称：误拒的代价是多按一次，
                # 误允的代价是毁掉一段采集 + 卡住下次启动。
                print("  [守护] 读不到采集程序状态，保守拒绝退出", flush=True)
                self.notifier.emit("busy", "状态未知")
                return

            if state == rcmod.IDLE:
                self.stop_child()
            else:
                print(f"  [守护] 当前状态 {state}，拒绝退出", flush=True)
                self.notifier.emit("busy", f"当前 {state}")

    # ---------- 主循环 ----------

    def run(self):
        print("=" * 66, flush=True)
        print("  遥控守护进程", flush=True)
        print("=" * 66, flush=True)
        print(f"  POWER × 2 : 启动 / 退出采集程序", flush=True)
        print(f"  采集程序内 ENTER × 2 = 采一段，ESC = 中止本段", flush=True)
        print(f"  采集进行中按 POWER 会被拒绝（先按 ESC 中止）", flush=True)
        print(f"  子进程    : {CAPTURE_SCRIPT} {' '.join(CAPTURE_ARGS)}",
              flush=True)
        print(f"  状态文件  : {STATE_FILE}", flush=True)
        print("=" * 66, flush=True)

        check_logind()

        watcher = PowerWatcher(self.on_power_double,
                               grab=not self.args.no_grab).start()
        self.notifier.emit("daemon_ready")

        # 收到 SIGTERM（systemctl stop）时也要优雅收尾
        def _term(signum, frame):
            print(f"\n  [守护] 收到信号 {signum}，收尾中", flush=True)
            self._quit.set()
        signal.signal(signal.SIGTERM, _term)

        try:
            while not self._quit.is_set():
                # 子进程意外退出（崩溃、或它自己 Ctrl-C 退出）要察觉到，
                # 否则守护进程会以为它还在、按 POWER 变成"拒绝退出"
                with self._lock:
                    if self.child is not None and self.child.poll() is not None:
                        rc = self.child.poll()
                        self.child = None
                        try:
                            os.remove(STATE_FILE)
                        except OSError:
                            pass
                        if rc == 0:
                            self.notifier.emit("app_exited", "自行退出")
                        else:
                            self.notifier.emit("app_died", f"退出码 {rc}")
                self._quit.wait(1.0)
        except KeyboardInterrupt:
            print("\n  [守护] Ctrl-C，退出", flush=True)
        finally:
            with self._lock:
                if self.child_running():
                    print("  [守护] 守护进程退出，先收尾子进程", flush=True)
                    # 守护自己要退了，此时无论子进程在干什么都得让它优雅结束
                    self.notifier.emit("app_exiting", "守护进程退出")
                    try:
                        os.killpg(os.getpgid(self.child.pid), signal.SIGINT)
                        self.child.wait(timeout=GRACEFUL_WAIT)
                    except Exception:
                        try:
                            os.killpg(os.getpgid(self.child.pid),
                                      signal.SIGKILL)
                        except Exception:
                            pass
            watcher.close()
            # 给最后一句语音留时间播完再关播放线程
            time.sleep(1.5)
            self.notifier.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="只自检环境（设备、logind、音频），不进主循环")
    ap.add_argument("--no-audio", action="store_true",
                    help="不播语音（也会传给子进程）")
    ap.add_argument("--no-grab", action="store_true",
                    help="不独占 POWER 键。默认独占 —— 不独占时按键仍会"
                         "传给 logind/桌面，可能弹关机对话框")
    ap.add_argument("--no-grab-capture", action="store_true",
                    help="不让子进程独占 ENTER/ESC。默认独占 —— "
                         "不独占时遥控器同时是普通键盘，ENTER 会往活动"
                         "控制台的 shell 里敲回车（实测 tty1 上有 bash）")
    args = ap.parse_args()

    if args.check:
        print("=" * 66)
        print("  守护进程环境自检")
        print("=" * 66)
        ok = True

        w = PowerWatcher(lambda: None, grab=False)
        devs = w.find_devices()
        print(f"\n[1] POWER 键设备: {len(devs)} 个")
        for p, d in sorted(devs.items()):
            print(f"    ✓ {p}  {d.name}")
            d.close()
        if w.rejected:
            print("    已排除：")
            for p, name, why in sorted(w.rejected):
                print(f"      {p:22s} {name:42s} {why}")
        if not devs:
            print("    ✗ 没找到 —— 遥控器插上了吗？")
            ok = False
        elif len(devs) > 1:
            print(f"    ⚠ 有 {len(devs)} 个节点会被独占，正常应只有 1 个"
                  "（System Control）")

        print(f"\n[2] 采集脚本: {CAPTURE_SCRIPT}")
        if os.path.isfile(CAPTURE_SCRIPT):
            print("    ✓ 存在")
        else:
            print("    ✗ 不存在")
            ok = False

        print("\n[3] logind 电源键设置")
        if check_logind():
            print("    ✓ HandlePowerKey=ignore 已配置，POWER 不会关机")
        else:
            ok = False

        print("\n[4] 音频")
        if args.no_audio:
            print("    (--no-audio，跳过)")
        else:
            n = DaemonNotifier(use_audio=True)
            missing = [fn for fn in sorted(set(DaemonNotifier._AUDIO.values()))
                       if n.audio is None
                       or not os.path.isfile(os.path.join(n.audio.sounds_dir, fn))]
            if missing:
                print(f"    缺少: {' '.join(missing)}")
                print("    跑 python3 _test_audio.py --gen 生成占位音")
                ok = False
            else:
                print("    ✓ 守护进程用的音频齐全")
            n.close()

        print()
        print("=" * 66)
        print("  自检" + ("通过" if ok else "未通过（见上）"))
        print("=" * 66)
        return 0 if ok else 1

    Daemon(args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
