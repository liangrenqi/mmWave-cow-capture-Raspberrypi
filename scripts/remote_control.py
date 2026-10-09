#!/usr/bin/env python3
"""
无线遥控采集控制 —— 按键监听 + 状态机 + 提示接口

用途：牛场饲养员不接触电脑，用 2.4G 遥控器（现阶段用 2.4G 无线键盘代替）
控制采集起停。被 capture_linux.py 的 --remote 模式调用，也可以单独跑
自测按键（见文件末尾的 __main__）。

设计上有三条不能改的前提，都踩过或实测过：

1. **必须用 evdev 直接读 /dev/input/event*，不能用 input() / 读 stdin。**
   封盒后没有终端、程序由 systemd 后台启动，无线键盘的按键不会进到一个
   没有终端焦点的进程。evdev 走的是内核输入子系统，与终端焦点无关 ——
   SSH 里跑、后台跑、没有 X 都收得到。

2. **不能硬编码 event 号。** 实测这块 2.4G dongle（RDMCTMZT）一个物理
   设备枚举出 5 个 HID 接口：

       event5  RDMCTMZT Wireless 2.4G Dongle
       event6  ... Mouse
       event7  ... System Control
       event8  ... Consumer Control
       event9  ... Keyboard

   字母键落在哪个接口不确定（不同厂商分派不同），且拔插后号会变。
   故按 capabilities 里有没有目标键来筛，并**同时监听所有符合的设备**，
   再定期重扫以支持热插拔。遥控器到货后如果是 HID 键盘类，同一条路，
   只需改 START_KEY / STOP_KEY 的键名。

3. **只认 EV_KEY value==1（按下），忽略 value==2（长按自动重复）。**
   否则按住开始键不放会被当成连击、直接触发采集，防误触就白做了。

权限：读 /dev/input/event* 需要在 input 组里。实测 pi 用户已在
（groups 含 102(input)），不需要 sudo。
"""

import os
import queue
import shutil
import subprocess
import sys
import threading
import time

try:
    import evdev
    from evdev import ecodes
except ImportError:                       # pragma: no cover
    evdev = None
    ecodes = None


# ============== 按键与时序参数 ==============
# 2026-09-03 换成 Genius 演示器遥控（USB ID 27a7:2501）实测的键码。
# **HID capabilities 声明的 163 个键是能力上限，不等于按钮实际发什么码** ——
# 必须实测。该遥控实测映射（见 _test_input_monitor.py 的输出）：
#   电源键   -> KEY_POWER       (event7 System Control)
#   音量±    -> KEY_VOLUMEUP/DOWN (event8 Consumer Control)
#   上下左右 -> KEY_UP/DOWN/LEFT/RIGHT (event9)
#   确认键   -> KEY_ENTER       (event9)  ← 选作开始
#   返回键   -> KEY_ESC         (event9)  ← 选作停止
# ENTER 与 ESC 同在 event9，不存在"两个键分散在不同接口"的问题。
START_KEY = "enter"      # 开始（需在 DOUBLE_WINDOW 内连按两次）
STOP_KEY = "esc"         # 紧急停止（单按即生效）

DOUBLE_WINDOW = 2.0      # 双击判定窗口（秒）
MIN_GAP = 0.05           # 两次按下的最小间隔，短于此视为抖动/重复上报
                         # 2026-09-02 实测定的值。原设 0.15 s，过度保守：
                         # 实测人为"快速连按"的间隔是 **209 ms**，离 150 ms
                         # 只差 59 ms —— 再按快一点第二次就会被当成抖动丢掉，
                         # 双击不成立且**没有任何提示**，现象是"按两下没反应"。
                         # 之所以敢降：同一次实测里 5 次按键**全部来自 event5**，
                         # event9 一个事件都没有 ⇒ 担心的跨节点重复并不存在。
                         # 真发生的话也是同一批 USB 传输、间隔在毫秒级，
                         # 50 ms 照样拦得住，而人手连按远在 50 ms 之上。
RESCAN_SEC = 5.0         # 重扫输入设备的周期（支持 dongle 热插拔）
# ============================================


# 状态机的五个状态。锁定语义：只有 IDLE 接受开始键。
IDLE = "IDLE"              # 空闲，等开始键
PREPARING = "PREPARING"    # 预检/配卡/发 cfg，约 10-20 秒
CAPTURING = "CAPTURING"    # 雷达在发射，数据在流
FINALIZING = "FINALIZING"  # 归档 + 判据，不可打断
ABORTING = "ABORTING"      # 中止收尾中

# 这些状态下按开始键一律忽略（这就是"锁住开始键直到落盘结束"）
_LOCKED_STATES = (PREPARING, CAPTURING, FINALIZING, ABORTING)


class Notifier:
    """提示接口 —— 现在只打日志，USB 蜂鸣器与 GPIO 状态灯各填一个后端

    为什么现在就抽象出来而不是等硬件到货：
    memory 里阶段 7 计划的 GPIO 状态灯（黄=配置中 / 绿闪=采集中 /
    绿常亮=判据通过 / 红常亮=判废 / 红快闪=出错中止）与这里的事件是
    同一套语义。做成事件驱动后，加灯只需再挂一个后端，状态机不用改第二遍。

    事件语义 —— 封盒后饲养员只能靠声音判断状态，所以每个状态迁移都有事件：

      ready       程序就绪，可以按开始键
      armed       开始键第一次按下，等第二次（DOUBLE_WINDOW 内）
      preparing   双击成立，开始准备
      start       **雷达真正开始发射**
      locked      锁定期间误按了开始键
      aborting    收到停止键，中止中
      finalizing  采集结束，正在归档与校验（不可打断）
      done_good   judged GOOD
      done_bad    judged BAD
      done_abort  中止完成
      error       出错

    preparing 与 start 必须分开：按下开始键后要跑 preflight、配 FPGA、
    起 tcpdump、起 CLI_Record、逐行发 cfg、等 sensorStart 裁决，
    实测**约 10-20 秒**才真正开始采集。只给一个提示音的话，饲养员会
    以为按了没反应而重复按。
    """

    EVENTS = ("ready", "armed", "preparing", "start", "locked", "aborting",
              "finalizing", "done_good", "done_bad", "done_abort", "error",
              # 心率带 H10（h10_session.py 发出，2026-09-30 用户要求断线/重连都要播报）
              "h10_connected", "h10_lost", "h10_reconnected", "h10_missing")

    def emit(self, event, detail=""):
        raise NotImplementedError


class LogNotifier(Notifier):
    """默认后端：打到 stdout。硬件到货前用这个，前台测试也看得见。

    只在关键状态节点播放音频,避免太频繁产生误导。
    """

    _TEXT = {
        "ready":      "就绪 —— 连按两次 [%s] 开始采集" % START_KEY.upper(),
        "armed":      "已按一次，%.0f 秒内再按一次生效" % DOUBLE_WINDOW,
        "preparing":  "已受理，准备中（约 10-20 秒）",
        "start":      "采集开始",
        # 文案要中性：开始键与停止键被拒都走这个事件，具体是哪个键、
        # 为什么被拒，由 detail 说明。原先写死"开始键已锁定"，
        # 停止键被拒时会打出"开始键已锁定 —— 停止键无效"这种自相矛盾的话。
        "locked":     "按键被忽略",
        "aborting":   "收到停止键，中止中",
        "finalizing": "采集结束，归档与校验中（不可打断）",
        "done_good":  "本段判定 GOOD",
        "done_bad":   "本段判定 BAD",
        "done_abort": "已中止，数据已归档",
        "error":      "出错",
        "h10_connected":   "心率带已连接",
        "h10_lost":        "心率带断开",
        "h10_reconnected": "心率带已重连",
        "h10_missing":     "心率带未连接",
    }

    # 音频文件映射 —— 只在关键节点播放,减少干扰
    # 录音建议:
    #   preparing.wav   - "采集命令下发" 或"命令已下发"
    #   ready.wav       - "就绪" 或短鸣,表示可以开始下一段
    #   start.wav       - "采集开始" 或"开始",雷达真正发射的时刻
    #   done_good.wav   - "通过" 或欢快短音
    #   done_bad.wav    - "判废" 或沉重长音
    #   done_abort.wav  - "已中止" 或"中止完成"
    #   error.wav       - "出错" 或急促告警音
    #
    # **preparing 必须有声**（2026-09-06 实测后加）：双击到 start 之间要跑
    # preflight、配 FPGA、起 tcpdump、起 CLI_Record、逐行发 29 条 cfg、
    # 等 sensorStart 裁决，实测 **10-20 秒**。这段全程无声时饲养员会以为
    # 没按上而反复按 —— 虽然锁定逻辑挡得住，但人会困惑。
    # 这也是当初把 armed/locked 等留作静默、却唯独要给 preparing 出声的原因：
    # 它标志"命令确实收到了，正在执行"，是这条链路上唯一的长等待。
    _AUDIO = {
        "preparing":  "preparing.wav",
        "ready":      "ready.wav",
        "start":      "start.wav",
        "done_good":  "done_good.wav",
        "done_bad":   "done_bad.wav",
        "done_abort": "done_abort.wav",
        "error":      "error.wav",
        # 心率带。录音建议：
        #   h10_connected.wav    - "心率带已连接"
        #   h10_lost.wav         - "心率带断开"
        #   h10_reconnected.wav  - "心率带已重连"
        #   h10_missing.wav      - "心率带未连接"（拉起 45 秒仍没连上，只播一次）
        # 断线时雷达照常采集，这几条只是告诉饲养员「去看看心率带」，不代表本段作废
        "h10_connected":   "h10_connected.wav",
        "h10_lost":        "h10_lost.wav",
        "h10_reconnected": "h10_reconnected.wav",
        "h10_missing":     "h10_missing.wav",
    }

    def emit(self, event, detail=""):
        text = self._TEXT.get(event, event)
        audio = self._AUDIO.get(event)
        line = f"  [遥控] {text}"
        if detail:
            line += f" —— {detail}"
        if audio:
            line += f"   (播放: {audio})"
        print(line, flush=True)


class AudioNotifier(Notifier):
    """USB 喇叭后端 —— 用 aplay 播放 wav，2026-09-03 硬件到货后实装

    ## 设备选择：必须按名字，不能按 card 号

    实测 `aplay -l` 的结果：

        card 0: vc4hdmi0 [vc4-hdmi-0]        ← HDMI，不是我们要的
        card 1: vc4hdmi1 [vc4-hdmi-1]        ← HDMI
        card 2: Device   [USB2.0 Device]     ← USB 喇叭

    默认设备（不给 -D）会落到 card 0 的 HDMI 上，**没有声音且不报错**。
    而 card 号会随插拔顺序变（HDMI 插不插、USB 口换一个都可能变），
    所以固定用 `plughw:CARD=Device` 按名字选。
    `plughw` 而非 `hw`：前者带自动重采样，wav 采样率与设备不完全匹配也能播；
    `hw` 要求完全一致，不匹配直接报错。

    ## 播放为什么必须放到后台线程

    `aplay` 是阻塞的，一段 1 秒的提示音就卡住主流程 1 秒。放在采集时序里
    会推迟 sensorStop 之类的关键动作，所以用独立线程播。

    但**不能每次 emit 都开一个新线程**：done_good 与 ready 只隔 1.5 秒
    （capture_linux._remote_loop 里特意加的间隔），两个 aplay 同时抢
    同一块 USB 声卡，第二个会失败。故用一个串行队列，一次只播一个。

    ## 失败绝不能影响采集

    喇叭掉线、wav 缺失、aplay 报错 —— 全部只打一行警告。
    提示音是辅助，采集才是正事。
    """

    # ALSA 设备名。实测 USB 喇叭在 /proc/asound/cards 里的名字是 "Device"
    # （完整描述 "Generic USB2.0 Device at usb-xhci-hcd.0-2, full speed"）。
    # 换了别的喇叭要重新看 `cat /proc/asound/cards` 里方括号中的名字。
    DEVICE = "plughw:CARD=Device"

    # wav 目录：默认取本文件同级的 ../sounds/（仓库布局），
    # 可用环境变量 CAPTURE_SOUNDS_DIR 覆盖
    DEFAULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               os.pardir, "sounds")

    def __init__(self, sounds_dir=None, device=None, fallback=None,
                 audio_map=None):
        self.sounds_dir = os.path.abspath(
            sounds_dir or os.environ.get("CAPTURE_SOUNDS_DIR")
            or self.DEFAULT_DIR)
        self.device = device or os.environ.get("CAPTURE_AUDIO_DEV") \
            or self.DEVICE
        self.fallback = fallback or LogNotifier()
        self.audio_map = audio_map or LogNotifier._AUDIO

        # 串行播放队列 + 单个工作线程
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._play_loop, daemon=True)
        self._worker.start()

        self.available = self._probe()

    def _probe(self):
        """启动时确认设备与 wav 都在，把问题一次说清楚而不是每次播放才报

        故意不做"设备不在就禁用"—— 喇叭可能是采集中途才插上的，
        每次播放时再试一次也无妨（失败只是一行警告）。
        """
        ok = True
        if shutil.which("aplay") is None:
            print("  [遥控] 找不到 aplay，语音提示不可用"
                  "（安装： sudo apt install alsa-utils）", flush=True)
            return False

        if not os.path.isdir(self.sounds_dir):
            print(f"  [遥控] 音频目录不存在: {self.sounds_dir}", flush=True)
            print("         语音提示不可用；建好目录并放入 wav 即可生效",
                  flush=True)
            return False

        missing = [fn for fn in sorted(set(self.audio_map.values()))
                   if not os.path.isfile(os.path.join(self.sounds_dir, fn))]
        if missing:
            print(f"  [遥控] 音频目录 {self.sounds_dir} 缺少 "
                  f"{len(missing)} 个文件: {' '.join(missing)}", flush=True)
            print("         缺失的那几个事件只打日志、不播声音，其余正常",
                  flush=True)
            ok = False        # 部分缺失仍然可用

        # 设备是否存在：读 /proc/asound/cards 比跑 aplay 快且无副作用
        want = self.device.split("CARD=")[-1] if "CARD=" in self.device else ""
        if want:
            try:
                with open("/proc/asound/cards") as f:
                    cards = f.read()
                if f"[{want:<15}]" not in cards and f"[{want}" not in cards:
                    print(f"  [遥控] ALSA 里找不到声卡 '{want}'，"
                          "语音提示可能无声", flush=True)
                    print(f"         当前声卡列表：\n{cards.rstrip()}",
                          flush=True)
                    ok = False
            except OSError:
                pass

        if ok:
            print(f"  [遥控] 语音提示已就绪：{self.sounds_dir} -> {self.device}",
                  flush=True)
        return True

    def _play_loop(self):
        """串行播放。队列里堆积时只保留最新的几个，不让提示音越积越多。"""
        while not self._stop.is_set():
            try:
                path = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if path is None:
                break
            try:
                # 给超时：喇叭异常时 aplay 可能挂住，不能让它拖住队列
                subprocess.run(["aplay", "-q", "-D", self.device, path],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, timeout=15)
            except subprocess.TimeoutExpired:
                print(f"  [遥控] 播放超时（已跳过）: {os.path.basename(path)}",
                      flush=True)
            except Exception as e:
                print(f"  [遥控] 播放失败（不影响采集）: {e}", flush=True)
            finally:
                self._queue.task_done()

    def emit(self, event, detail=""):
        # 日志照打 —— 语音是附加的，不替代终端输出
        self.fallback.emit(event, detail)

        fn = self.audio_map.get(event)
        if not fn:
            return
        path = os.path.join(self.sounds_dir, fn)
        if not os.path.isfile(path):
            return            # 缺文件在 _probe 已提示，这里静默跳过

        # 队列里已经堆了很多就丢掉最旧的，避免提示音滞后于实际状态
        while self._queue.qsize() > 3:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break
        self._queue.put(path)

    def close(self):
        self._stop.set()
        self._queue.put(None)
        if self._worker.is_alive():
            self._worker.join(timeout=2)


class BeeperNotifier(Notifier):
    """蜂鸣器后端骨架 —— 非 USB 声卡类硬件的备选实现

    我们实际用的是 USB 声卡喇叭，走 AudioNotifier。这个类留着是因为
    另外两类硬件的接法不同，将来换硬件时不必重新查：

    1. **USB HID 蜂鸣器**（`lsusb` 出现新设备、/dev/hidrawN 多一个）：
           open("/dev/hidrawN","wb").write(厂商定义的字节)
       协议看厂商文档，通常是几字节的开/关/频率。

    2. **PC speaker / evdev EV_SND 类**：
           dev.write(ecodes.EV_SND, ecodes.SND_TONE, 频率)
       只能出单音，靠长短组合编码状态。
    """

    def __init__(self, fallback=None):
        self.fallback = fallback or LogNotifier()
        self.available = False

    def _play(self, event):           # pragma: no cover - 未使用
        raise NotImplementedError("我们用 AudioNotifier（USB 声卡）")

    def emit(self, event, detail=""):
        if self.available:
            try:
                self._play(event)
            except Exception as e:
                print(f"  [遥控] 蜂鸣器播放失败（不影响采集）: {e}", flush=True)
        self.fallback.emit(event, detail)


class CompositeNotifier(Notifier):
    """把一个事件同时送给多个后端（声 + 灯 + 日志）

    单个后端抛异常不能影响采集 —— 提示只是提示，采集才是正事。
    """

    def __init__(self, *backends):
        self.backends = [b for b in backends if b is not None]

    def emit(self, event, detail=""):
        for b in self.backends:
            try:
                b.emit(event, detail)
            except Exception as e:
                print(f"  [遥控] 提示后端 {type(b).__name__} 出错（已忽略）: {e}",
                      flush=True)


def _key_code(name):
    """把 's' 这样的键名转成 evdev 键码"""
    key = f"KEY_{name.upper()}"
    code = ecodes.ecodes.get(key)
    if code is None:
        raise ValueError(f"无法识别的键名: {name}（应为 KEY_* 里的名字，如 s/e/f1）")
    return code


class RemoteController:
    """按键监听 + 状态机

    线程模型：监听跑在独立守护线程里，主流程（采集）是阻塞的。
    线程通过两个 Event 与主流程通信：

        _start_evt  双击开始键成立 → wait_for_start() 返回
        stop_evt    按下停止键     → 采集倒计时被打断

    stop_evt 在 PREPARING 阶段按下也会 set，主流程要到 CAPTURING 的倒计时
    才检查它 —— 这就实现了"准备阶段按停止键，等真正开始后立刻中止"。
    准备阶段只有 10-20 秒，中途中止要为 tcpdump / CLI_Record / 串口
    各写一条"起没起"的清理分支，而挂起执行可以复用同一条 abort 路径。
    """

    def __init__(self, notifier=None, start_key=START_KEY, stop_key=STOP_KEY,
                 double_window=DOUBLE_WINDOW, min_gap=MIN_GAP, grab=False,
                 state_file=None):
        if evdev is None:
            raise RuntimeError(
                "需要 python3-evdev。安装： sudo apt install python3-evdev\n"
                "（实测 Pi 上已装，版本 2.0.0）")

        self.notifier = notifier or LogNotifier()
        self.start_key, self.stop_key = start_key, stop_key
        self.start_code = _key_code(start_key)
        self.stop_code = _key_code(stop_key)
        self.double_window = double_window
        self.min_gap = min_gap
        self.grab = grab
        # 状态文件：每次状态迁移都写一行当前状态。
        # remote_daemon.py 靠它判断"能不能退出"—— 采集中必须拒绝退出，
        # 否则硬杀会留下没发 sensorStop 的雷达和卡住下次采集的游离文件。
        self.state_file = state_file

        self.state = IDLE
        self._start_evt = threading.Event()
        self.stop_evt = threading.Event()
        self._quit = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

        self._last_press = {}        # keycode -> 上次按下的时刻（去抖用）
        self._first_start_press = None   # 双击的第一次按下时刻
        self._devices = {}           # path -> InputDevice
        self._ready_announced = False    # 免得轮询式等待把 ready 刷满屏

    # ---------- 设备发现 ----------

    def find_devices(self):
        """挑出所有能同时报开始键与停止键的输入设备

        用 all() 而不是 any()：一个 dongle 会枚举出 Mouse /
        System Control / Consumer Control 等接口，它们各自只报少量键。
        要求两个键都在，才是真正的键盘接口，避免监听一堆收不到字母键的节点。
        """
        found = {}
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue                      # 权限不足或刚被拔掉
            try:
                caps = dev.capabilities().get(ecodes.EV_KEY, [])
                if self.start_code in caps and self.stop_code in caps:
                    found[path] = dev
                else:
                    dev.close()
            except OSError:
                try:
                    dev.close()
                except OSError:
                    pass
        return found

    def _refresh_devices(self, announce=False):
        """重扫设备，处理 dongle 拔插（event 号会变）"""
        current = self.find_devices()
        added = set(current) - set(self._devices)
        removed = set(self._devices) - set(current)

        for path in removed:
            dev = self._devices.pop(path, None)
            if dev is not None:
                try:
                    dev.close()
                except OSError:
                    pass
            print(f"  [遥控] 输入设备已移除: {path}", flush=True)

        for path in added:
            dev = current[path]
            self._devices[path] = dev
            if self.grab:
                # 独占：按键不再进入系统（终端里不会回显 s/e）。
                # 封盒后遥控器是专用的，独占更干净；前台调试时默认不开，
                # 免得把你正在用的键盘抢走。
                try:
                    dev.grab()
                except OSError as e:
                    print(f"  [遥控] 独占 {path} 失败（继续，非独占）: {e}",
                          flush=True)
            if announce:
                print(f"  [遥控] 新增输入设备: {dev.name}  ({path})", flush=True)

        # current 里没被收进 self._devices 的（已存在的重复对象）要关掉
        for path, dev in current.items():
            if self._devices.get(path) is not dev:
                try:
                    dev.close()
                except OSError:
                    pass
        return added, removed

    # ---------- 监听线程 ----------

    def _listen_loop(self):
        import selectors

        while not self._quit.is_set():
            self._refresh_devices(announce=True)
            if not self._devices:
                # 没有可用设备也不能退出 —— dongle 可能还没插上，
                # 或者刚被拔掉正在重新枚举。等下一轮重扫。
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
                                if ev.type == ecodes.EV_KEY and ev.value == 1:
                                    # value: 0=抬起 1=按下 2=长按重复
                                    # 只认 1，否则按住不放会被当成连击
                                    self._on_key_down(ev.code)
                        except OSError:
                            # 设备被拔掉，跳出去重扫
                            deadline = 0
                            break
            finally:
                sel.close()

    def _on_key_down(self, code):
        now = time.time()

        # 去抖：无线键盘偶发重复上报，短于 MIN_GAP 的第二次视为抖动。
        # 不加这条，单次误压可能被当成双击直接开采。
        last = self._last_press.get(code)
        if last is not None and now - last < self.min_gap:
            return
        self._last_press[code] = now

        if code == self.stop_code:
            self._on_stop_key()
        elif code == self.start_code:
            self._on_start_key(now)

    def _on_start_key(self, now):
        with self._lock:
            state = self.state

        if state in _LOCKED_STATES:
            # 这就是"一次采集开始后锁住开始键，防止误触"
            self.notifier.emit("locked",
                               f"开始键在 {state} 状态下无效（采集中，防误触）")
            self._first_start_press = None
            return

        first = self._first_start_press
        if first is not None and (now - first) <= self.double_window:
            self._first_start_press = None
            self._start_evt.set()
            return

        # 第一次按下：记时刻，等第二次
        self._first_start_press = now
        self.notifier.emit("armed")

    def _on_stop_key(self):
        with self._lock:
            state = self.state

        if state in (PREPARING, CAPTURING):
            if not self.stop_evt.is_set():
                self.stop_evt.set()
                extra = "（准备阶段，采集一开始就中止）" if state == PREPARING else ""
                self.notifier.emit("aborting", extra)
            return

        if state in (FINALIZING, ABORTING):
            # 收尾不可打断：这段时间在归档和逐字节校验，中断会毁掉一段
            # 已经采好的数据
            self.notifier.emit("locked", "停止键无效 —— 正在归档/校验，不可打断")
            return

        # IDLE 下按停止键：无事可停
        self.notifier.emit("locked", "停止键无效 —— 当前空闲，无采集可停止")

    # ---------- 对外接口 ----------

    def start(self):
        devs = self.find_devices()
        if not devs:
            print("  [遥控] 警告：当前没有发现能报 "
                  f"[{self.start_key.upper()}]/[{self.stop_key.upper()}] 的输入设备。")
            print("         接收器插上后会自动识别（每 "
                  f"{RESCAN_SEC:.0f} 秒重扫一次）。")
        else:
            print(f"  [遥控] 监听 {len(devs)} 个输入设备：")
            for path, d in sorted(devs.items()):
                print(f"         {d.name}  ({path})")
        for d in devs.values():
            try:
                d.close()
            except OSError:
                pass

        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
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

    def set_state(self, state):
        with self._lock:
            prev, self.state = self.state, state
        if state != IDLE:
            self._ready_announced = False
        # 写状态文件供守护进程读。
        # **只在状态真的变了才写**：_remote_loop 的 while 每秒都会
        # set_state(IDLE)（为了让 Ctrl-C 及时响应），无条件写就是每秒一次
        # SD 卡写入，长期待命纯属无谓磨损。
        # 相应地，守护进程不能再用"时间戳超过 N 秒就算陈旧"来判断 ——
        # IDLE 恰恰是可以持续几小时不变的状态，而它又是唯一允许退出的状态。
        # 改为写入 PID，由守护进程比对是不是当前子进程写的。
        if self.state_file and state != prev:
            try:
                tmp = f"{self.state_file}.tmp"
                with open(tmp, "w") as f:
                    f.write(f"{state}\n{int(time.time())}\n{os.getpid()}\n")
                os.replace(tmp, self.state_file)   # 原子替换，避免读到半行
            except OSError:
                pass

    def wait_for_start(self, timeout=None):
        """阻塞等"连按两次开始键"。返回 True 表示成立

        timeout 用于轮询式调用（让主循环能及时响应 Ctrl-C），所以
        "就绪"提示只在状态回到 IDLE 后播一次，不会被轮询刷满屏。

        成立后顺手清掉 stop_evt 与双击暂存 —— 上一段采集里按过的停止键
        不能带到下一段来。
        """
        if not self._ready_announced:
            self.notifier.emit("ready")
            self._ready_announced = True
        ok = self._start_evt.wait(timeout)
        if ok:
            self._start_evt.clear()
            self.stop_evt.clear()
            self._first_start_press = None
            self._ready_announced = False
            self.notifier.emit("preparing")
        return ok

    def stop_requested(self):
        return self.stop_evt.is_set()

    def reset_stop(self):
        self.stop_evt.clear()

    def sleep(self, seconds):
        """可被停止键打断的等待。返回 True 表示"被打断了"

        替代采集倒计时里的 time.sleep(1)。
        """
        return self.stop_evt.wait(seconds)


def _selftest():
    """单独跑这个文件时的按键自测 —— 不碰雷达、不碰 DCA1000

    用途：接收器插上后先跑这个，确认双击、锁定、停止键都识别正确，
    再去联调采集。用法：
        python3 remote_control.py              # 带语音
        python3 remote_control.py --no-audio   # 只打日志
    """
    use_audio = "--no-audio" not in sys.argv

    print("=" * 62)
    print("  遥控按键自测（不涉及雷达与采集）")
    print("=" * 62)
    print(f"  开始键 : 连按两次 [{START_KEY.upper()}]（{DOUBLE_WINDOW:.0f} 秒内）")
    print(f"  停止键 : [{STOP_KEY.upper()}]")
    print(f"  语音   : {'开' if use_audio else '关（--no-audio）'}")
    print("  Ctrl-C 退出")
    print("=" * 62)

    notifier = AudioNotifier() if use_audio else LogNotifier()
    rc = RemoteController(notifier=notifier).start()
    try:
        while True:
            if not rc.wait_for_start(timeout=1.0):
                continue

            # 模拟一次采集：PREPARING 3 秒 → CAPTURING 10 秒 → FINALIZING 3 秒
            rc.set_state(PREPARING)
            print("\n  >>> PREPARING（模拟 3 秒；此时按开始键应被锁定，"
                  "按停止键应挂起）")
            time.sleep(3)

            rc.set_state(CAPTURING)
            rc.notifier.emit("start")
            print("  >>> CAPTURING（模拟 10 秒；按停止键应立刻中止）")
            interrupted = False
            for r in range(10, 0, -1):
                sys.stdout.write(f"\r      剩余 {r:2d} 秒 ")
                sys.stdout.flush()
                if rc.sleep(1):
                    interrupted = True
                    break
            print()

            if interrupted:
                rc.set_state(ABORTING)
                print("  >>> ABORTING（模拟 3 秒；此时按键应全部无效）")
                time.sleep(3)
                rc.notifier.emit("done_abort")
            else:
                rc.set_state(FINALIZING)
                rc.notifier.emit("finalizing")
                print("  >>> FINALIZING（模拟 3 秒；此时按键应全部无效）")
                time.sleep(3)
                rc.notifier.emit("done_good")

            rc.set_state(IDLE)
            print()
    except KeyboardInterrupt:
        print("\n  退出")
    finally:
        rc.close()
        if hasattr(notifier, "close"):
            notifier.close()


if __name__ == "__main__":
    _selftest()
