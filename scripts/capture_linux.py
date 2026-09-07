#!/usr/bin/env python3
"""
IWR6843 + DCA1000 采集脚本（Linux / 树莓派 5）

移植自 Windows 版 pointCloudwithAdc_bat/auto_capture.py，时序保持一致：
    fpga -> record -> start_record(非阻塞) -> 串口发 cfg -> 等待 -> stop_record

相比 Windows 原版的改动：
  1. 串口 COM13 -> /dev/ttyACM0，CLI 去掉 .exe 并设 LD_LIBRARY_PATH
  2. 文件前缀改为从 JSON 的 filePrefix 自动读取（原版硬编码，与 json 不一致）
  3. 新增 tcpdump 并行抓包 —— bin 会零填充掩盖丢包，pcap 保留包序号可定位
  4. 新增 csv 丢包判废 + BAD_ 前缀（沿用第一阶段 Lua 脚本的规则）
  5. 新增采集前后的内核丢包计数差值，用于区分卡侧丢包与主机侧丢包

用法：
    python3 capture_linux.py                  # 单次采集
    python3 capture_linux.py --no-pcap        # 不抓 pcap
    python3 capture_linux.py --loop 5         # 连续采 5 段
    python3 capture_linux.py --remote         # 遥控模式（2.4G 遥控器/键盘起停）
"""

import argparse
import glob
import json
import os
import re
import shutil

import signal
import struct
import subprocess
import threading
import sys
import time
from datetime import datetime, timedelta

try:
    import serial
except ImportError:
    sys.exit("[ERROR] 需要 pyserial: pip3 install pyserial")

# 遥控模块是可选的：不加 --remote 时完全不需要 evdev，
# 原有的命令行用法与部署不受影响。
try:
    import remote_control
except ImportError:
    remote_control = None


HERE = os.path.dirname(os.path.abspath(__file__))

# 两种目录布局都支持：
#   扁平（~/Ti_radar/run/）：脚本、cfg/json、CLI 二进制全在一起
#   仓库（mmwave-cow-capture/）：scripts/ 与 config/ 分开，
#                                CLI 二进制不入库（有 license），另行指定
# CFG_DIR  ：cfg 与 json 的位置
# CLI_DIR  ：DCA1000EVM_CLI_* 与 libRF_API.so 的位置，可用 DCA1000_CLI_DIR 覆盖
_parent_cfg = os.path.join(os.path.dirname(HERE), "config")
CFG_DIR = _parent_cfg if os.path.isdir(_parent_cfg) else HERE

CLI_DIR = os.environ.get("DCA1000_CLI_DIR", "")
if not CLI_DIR:
    # 探测顺序有讲究：先找仓库自带的 ../bin/，再找脚本同目录，
    # 最后才回落到 ~/Ti_radar/run。若把 ~/Ti_radar/run 排在前面，
    # 本机（有历史目录）用的是那份、别人 clone 后用的是 bin/ ——
    # 两边跑的不是同一个二进制，本机测过也不代表别人能跑。
    _cands = [
        os.path.join(os.path.dirname(HERE), "bin"),   # 仓库布局
        HERE,                                         # 扁平布局
        os.path.expanduser("~/Ti_radar/run"),         # 历史位置，兜底
    ]
    for _c in _cands:
        if os.path.isfile(os.path.join(_c, "DCA1000EVM_CLI_Control")):
            CLI_DIR = _c
            break
    else:
        CLI_DIR = HERE

# ============== 配置 ==============
CLI_CONTROL = os.path.join(CLI_DIR, "DCA1000EVM_CLI_Control")
MODE = os.environ.get("CAPTURE_CFG_MODE", "behavior")   # behavior | vitalsigns
JSON_CFG = os.path.join(CFG_DIR, f"dca1000_{MODE}.json")
PROFILE_CFG = os.path.join(CFG_DIR, f"cow_{MODE}.cfg")

RADAR_PORT = "/dev/ttyACM0"      # 配置口（ACM1 是数据口）
RADAR_BAUDRATE = 115200

ETH_IF = "eth0"
DATA_PORT = 4098                 # DCA1000EVM.txt:656
PCAP_USER = os.environ.get("SUDO_USER") or os.environ.get("USER") or "pi"

BUFFER_SEC = 10                  # 雷达停止后 DCA1000 封口缓冲
SENSOR_START_TIMEOUT = 12        # 等 sensorStart 裁决的秒数（失败时会死锁）
ABORT_SETTLE_SEC = 3             # 中止时给 DCA1000 吐完在途数据的时间
                                 # （比 BUFFER_SEC 短：中止的数据本就不指望用）

# mmwavelink 的字段宽度上限。两者都是 rlUInt16_t/文档明写的范围，
# 超了不报错、静默回绕（见 check_consistency 的注释）。
MAX_FRAMES = 65535               # rl_sensor.h:963  "Valid Range 0 to 65535"
MAX_LOOPS = 255                  # rl_sensor.h:958  "valid range = 1 to 255"

# 现场标注
COW_ID = "UNKNOWN"
CAPTURE_MODE = "vitalsigns"
NOTE = ""
# ==================================


def data_files(base, prefix):
    """列出 DCA1000 产出的数据文件

    只按数据文件的后缀匹配，不用裸 glob(f"{prefix}*") —— 那样会把
    同名的配置文件也算进去（filePrefix 是 cow_vitalsigns，
    而 cfg 就叫 cow_vitalsigns.cfg）。
    """
    pats = (f"{prefix}*_Raw_*.bin", f"{prefix}*.csv", f"{prefix}*.pcap")
    out = []
    for pat in pats:
        out.extend(glob.glob(os.path.join(base, pat)))
    return out


def load_json_cfg():
    """从 JSON 读 fileBasePath / filePrefix / framesToCapture

    不硬编码这三个值：Windows 原版硬编码 DCA1000_FILE_PREFIX="cow_capture"
    与 json 里的 filePrefix 不一致，会导致采完文件搬不走。
    """
    with open(JSON_CFG, encoding="utf-8") as f:
        cfg = json.load(f)["DCA1000Config"]
    cap = cfg["captureConfig"]
    return {
        "base_path": cap["fileBasePath"],
        "prefix": cap["filePrefix"],
        "frames": int(cap["framesToCapture"]),
        "packet_delay_us": cfg.get("packetDelay_us"),
    }


def parse_cfg(profile_path):
    """从 cfg 解析波形参数，并算出每帧字节数

    每帧字节 = samples × RX数 × 2(I/Q) × 2B × loops × TX数
    妊娠：256 × 4 × 2 × 2 × 32 × 1 = 131,072 B
    行为：128 × 4 × 2 × 2 × 128 × 3 = 786,432 B
    两者都与第一阶段实测记录精确吻合。
    """
    out = {"tx_count": 0}
    with open(profile_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("%"):
                continue
            p = line.split()
            if p[0] == "frameCfg":
                # frameCfg <chirpStart> <chirpEnd> <loops> <frames> <period_ms> ...
                out.update(frames=int(p[4]), period_ms=int(p[5]), loops=int(p[3]))
            elif p[0] == "profileCfg":
                out["samples"] = int(p[10])
            elif p[0] == "channelCfg":
                # 第 1 个字段是 rxChannelEn 位掩码，第 2 个是 txChannelEn
                out["rx_count"] = bin(int(p[1])).count("1")
                out["rx_mask"] = int(p[1])
                out["tx_mask"] = int(p[2])
            elif p[0] == "chirpCfg":
                out["tx_count"] += 1

    need = ("frames", "period_ms", "loops", "samples", "rx_count")
    missing = [k for k in need if k not in out]
    if missing:
        raise ValueError(f"{profile_path} 缺少字段: {missing}")

    out["frame_bytes"] = (out["samples"] * out["rx_count"] * 2 * 2
                          * out["loops"] * max(out["tx_count"], 1))
    return out


def check_consistency(jc, fc):
    """json 的 framesToCapture 必须等于 cfg 的 frameCfg 第 4 字段

    两者不等时 DCA1000 与雷达对采集长度的认知不一致：
    偏小则数据截断，偏大则 CLI 等到超时。

    随后查 mmwavelink 的字段宽度。这两条都是**静默失败**：
    雷达照常回 Done、采集照常开始，只是长度不是你要的那个值。
    """
    if jc["frames"] != fc["frames"]:
        sys.exit(
            f"[ERROR] 帧数不一致，拒绝采集\n"
            f"  {os.path.basename(JSON_CFG)} framesToCapture = {jc['frames']}\n"
            f"  {os.path.basename(PROFILE_CFG)} frameCfg[4]   = {fc['frames']}\n"
            f"  这两个数必须相等。"
        )
    check_field_limits(fc)


def check_field_limits(fc):
    """frameCfg 的帧数与 loops 必须装得进 mmwavelink 的 16 位/8 位字段

    2026-08-06 的实害：cow_vitalsigns.cfg 写 72000 帧（想采 60 分钟），
    雷达静默回绕成 72000-65536 = **6464 帧**，5.4 分钟就自己停了。
    全链路无一处报错 —— cfg 逐行 Done、sensorStart 通过、判据没跑
    （因为脚本还在等 60 分钟的倒计时），等了 11 分钟才发现。
    bin 847,249,408 B = 131072 × 6464 整，pcap 末包是 96 B 零头
    （雷达自停的特征；被 kill 末包会是满 1456）。

    帧数 0 在 mmwavelink 里合法（= 无穷帧，收到 Frame Stop 才停），
    但**本脚本不支持**：收尾段只发 stop_record、不发 sensorStop，
    雷达会一直跑到下次采集开头那句 sensorStop；且 check.bin_total
    与 L1 都拿 frames × frame_bytes 当期望值，帧数为 0 时无期望值可比。
    要用无穷模式得先改这三处，别靠这里放行。
    """
    frames, loops = fc["frames"], fc["loops"]
    period_ms = fc["period_ms"]
    name = os.path.basename(PROFILE_CFG)

    if frames > MAX_FRAMES:
        max_min = MAX_FRAMES * period_ms / 60000.0
        sys.exit(
            f"[ERROR] 帧数超出 16 位上限，拒绝采集\n"
            f"  {name} frameCfg[4] = {frames}，上限 {MAX_FRAMES}\n"
            f"  numFrames 是 rlUInt16_t（rl_sensor.h:963，用户指南也写"
            f" 0 to 65535）\n"
            f"  真正下发的会是 {frames % (MAX_FRAMES + 1)} 帧，且不会有任何报错。\n"
            f"  本波形 @ {period_ms} ms/帧 单段最长 {MAX_FRAMES} 帧"
            f" = {max_min:.1f} 分钟。\n"
            f"  要采更久请分段连采（json 的 framesToCapture 同步改）。"
        )

    if frames == 0:
        sys.exit(
            f"[ERROR] 帧数为 0（无穷帧），本脚本不支持，拒绝采集\n"
            f"  {name} frameCfg[4] = 0\n"
            f"  收尾只发 stop_record 不发 sensorStop，雷达不会停；\n"
            f"  且 check.bin_total 与 L1 判据没有期望值可比。\n"
            f"  详见 check_field_limits 的注释。"
        )

    if not 1 <= loops <= MAX_LOOPS:
        sys.exit(
            f"[ERROR] loops 超出有效范围，拒绝采集\n"
            f"  {name} frameCfg[3] = {loops}，有效范围 1-{MAX_LOOPS}\n"
            f"  numLoops 的文档范围见 rl_sensor.h:958。同样是静默失败。"
        )


def cli(cmd, desc, wait=True, timeout=60, quiet=False):
    """调一条 DCA1000 CLI 命令

    wait=False 用于 start_record —— 它会常驻前台等数据流，必须非阻塞启动。

    quiet=True 给 start_record 加 -q，这是无图形环境的必需项：
    CLI_Control 不自己收数据，而是起一个 CLI_Record 子进程。非 -q 模式下
    它用 `gnome-terminal -x ./DCA1000EVM_CLI_Record ...` 启动
    （cli_control_main.cpp:1660），Pi OS 没有 gnome-terminal，于是
    system() 静默失败、退出码仍是 0，表现为"命令成功但没有数据"。
    -q 走的是 `./DCA1000EVM_CLI_Record ... -q &` 后台分支（同文件 1657 行）。
    """
    env = dict(os.environ, LD_LIBRARY_PATH=f"{CLI_DIR}:{os.environ.get('LD_LIBRARY_PATH', '')}")
    argv = [CLI_CONTROL, cmd, JSON_CFG] + (["-q"] if quiet else [])
    print(f"  -> {desc} ...", end=" ", flush=True)
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, cwd=CLI_DIR,
        )
    except FileNotFoundError:
        sys.exit(f"\n[ERROR] 找不到 {CLI_CONTROL}")

    if not wait:
        time.sleep(1)          # 与 Windows 原版一致：给 CLI 一秒把端口绑好
        print("[LAUNCHED]")
        return 0, proc

    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        print("[TIMEOUT]")
        return -1, None

    ok = proc.returncode == 0 and "Success" in (out or "") + (err or "")
    print("[OK]" if ok else f"[WARN rc={proc.returncode}]")
    for s in (out, err):
        if s and s.strip():
            for line in s.strip().splitlines():
                print(f"       {line.strip()}")
    return proc.returncode, None


# 状态文件写脚本目录（仓库布局下 scripts/ 可写；已在 .gitignore 里）
CHCFG_STATE = os.path.join(HERE, ".last_chcfg")


def check_chcfg(fc):
    """切换波形时拦下来 —— chCfg 改了必须先给雷达断电重启

    mmw demo 只在**首次** sensorStart 时应用 openCfg 里的 chCfg /
    lowPowerMode / adcCfg。之后若下发不同的值，sensorStart 会直接
    debugAssert：

        mss/mmw_cli.c:285-289
          if (memcmp(&gMmwMssMCB.cfg.openCfg.chCfg, &openCfg.chCfg,
                     sizeof(rlChanCfg_t)) != 0)
              MmwDemo_debugAssert(0);      ← line 288

        TI 的注释原话："the board needs to be reboot for the new
        configuration to be applied."

    sensorStop + flushCfg **不够** —— 它们只让 sensorState 回到 STOPPED，
    不回到 INIT。所以行为(channelCfg 15 7 0, 3 TX) 与
    妊娠(channelCfg 15 1 0, 1 TX) 之间切换必须断电。

    2026-08-02 实测撞上过：串口回 "Exception: ./mss/mmw_cli.c, line 288."
    那时 tcpdump 和 CLI_Record 都已启动，还得走一遍收尾。故这个检查放在
    最前面 —— 在起任何进程之前。
    """
    cur = f"{fc['rx_mask']} {fc['tx_mask']}"
    try:
        with open(CHCFG_STATE) as f:
            prev = f.read().strip()
    except OSError:
        prev = None

    if prev and prev != cur:
        print(f"\n  [ERROR] channelCfg 变了，雷达需要断电重启后才能采集")
        print(f"     上次 channelCfg : {prev} 0")
        print(f"     本次 channelCfg : {cur} 0")
        print("     mmw demo 只在首次 sensorStart 应用 chCfg，改了会触发")
        print("     debugAssert（mmw_cli.c:288），串口回 'Exception:'。")
        print("     sensorStop / flushCfg 都不够 —— 必须拔 5V 断电，等 5 秒，插回。")
        print(f"     断电后删掉状态文件或直接重跑即可：rm {CHCFG_STATE}")
        return False

    try:
        with open(CHCFG_STATE, "w") as f:
            f.write(cur)
    except OSError:
        pass
    return True


def run_l3_verify(session, frame_bytes):
    """跑 verify_pcap_bin.py 做 L3 逐字节比对，返回 (returncode, 摘要)"""
    script = os.path.join(HERE, "verify_pcap_bin.py")
    if not os.path.isfile(script):
        return None, f"找不到 {os.path.basename(script)}"
    r = subprocess.run([sys.executable, script, session,
                        "--frame-bytes", str(frame_bytes)],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True)
    out = r.stdout or ""
    key = [l.strip() for l in out.splitlines()
           if "L3 比对" in l or "逐字节" in l or "不一致" in l]
    note = " | ".join(key) if key else out.strip().splitlines()[-1:][0] if out.strip() else "无输出"
    return r.returncode, note[:200]


def run_rx_check(session):
    """跑 check_quality.py 的 RX 通道检测，返回 (returncode, 摘要)"""
    script = os.path.join(HERE, "check_quality.py")
    if not os.path.isfile(script):
        return None, f"找不到 {os.path.basename(script)}"
    r = subprocess.run([sys.executable, script, session, "--cfg", PROFILE_CFG],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True)
    out = r.stdout or ""
    # 只取 "RX<数字>" 的数据行，不能用 startswith("RX") ——
    # 那会把标题行 "RX 通道检测（抽样 ...）" 也当成数据
    nums, bad = [], []
    for line in (l.strip() for l in out.splitlines()):
        mm = re.match(r"^RX(\d+)\s+([\d.]+)", line)
        if mm:
            nums.append(f"RX{mm.group(1)}={mm.group(2)}")
            if "异常" in line:
                bad.append(line.split("**异常**")[-1].strip()
                           or f"RX{mm.group(1)} 异常")
    joined = " ".join(nums)
    if r.returncode == 0:
        note = f"4 路均有信号  {joined}"
    else:
        # 失败时也带上各通道数值，便于事后判断严重程度
        detail = "; ".join(f"RX{i} {b}" for i, b in enumerate(bad)) if bad else ""
        problems = [l.strip() for l in out.splitlines()
                    if l.strip().startswith("RX") and "异常" in l]
        if problems:
            detail = " | ".join(p.split("**异常**")[-1].strip()
                                + f"({p.split()[0]})" for p in problems)
        note = f"通道异常 {detail or '(见脚本输出)'}  {joined}"
    return r.returncode, note[:200]


def _read_sysctl(key):
    path = "/proc/sys/" + key.replace(".", "/")
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def preflight(rate_bps, base_path=None, need_bytes=None):
    """采集前自检并自动修正内核缓冲 —— 原先靠人工跑 prepare.sh

    为什么必须自动化：`sysctl -w` 不持久，重启即失效。忘记跑 prepare.sh
    不会报错，只会**静默降级**——2026-08-02 09:48 那次行为采集就是这样，
    rmem_max 掉回默认 212992（208 KB），netdev_max_backlog 掉回 1000，
    结果丢了 22,510 个包。静默失败比参数不够优危险得多，所以移进代码里。

    rmem_max 为什么要这么大（读源码确认，非推测）：CLI 在
    RF_API/rf_api.cpp:701 对数据口设 SO_RCVBUF = 0x7FFFFFFF（约 2 GB），
    内核会**静默截断到 rmem_max，不报错**。默认 208 KB 在 7.4 MB/s 下
    只够缓冲 29 毫秒。

    netdev_max_backlog 是协议栈入口队列，在 UDP/AF_PACKET 分流**之前**，
    所以它同时影响 CLI_Record 和 tcpdump 两条路径。
    """
    want = {
        "net.core.rmem_max": 268435456,          # 256 MB
        "net.core.netdev_max_backlog": 5000,
    }
    print("\n[0] 采集前自检")
    all_ok = True

    for key, target in want.items():
        cur = _read_sysctl(key)
        if cur is None:
            print(f"  [WARN] 读不到 {key}，跳过")
            continue
        if cur >= target:
            print(f"  -> {key} = {cur:,} [OK]")
            continue
        print(f"  -> {key} = {cur:,} 偏小（需 {target:,}），修正中 ...",
              end=" ", flush=True)
        r = subprocess.run(["sudo", "sysctl", "-w", f"{key}={target}"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           text=True)
        now = _read_sysctl(key)
        if r.returncode == 0 and now is not None and now >= target:
            print(f"[OK] → {now:,}")
        else:
            print(f"[FAIL] 仍为 {now}")
            print(f"       手动执行： sudo sysctl -w {key}={target}")
            all_ok = False

    # 缓冲够撑多久：这是判断参数是否足够的唯一有意义的量
    rmem = _read_sysctl("net.core.rmem_max")
    if rmem and rate_bps:
        print(f"  -> UDP 缓冲可吸收约 {rmem / rate_bps:.1f} 秒的数据流"
              f"（码率 {rate_bps / 1e6:.2f} MB/s）")

    # 落盘目录：CLI 不自动创建（阶段 3 踩过），且 tcpdump 降权后要能写。
    # exFAT 上 tcpdump 用户无写权限，靠 -Z PCAP_USER 解决，这里顺带验证。
    if base_path:
        if not os.path.isdir(base_path):
            print(f"  [ERROR] 落盘目录不存在: {base_path}")
            print("       CLI_Record 不会自动创建，先 mkdir -p")
            all_ok = False
        else:
            probe = os.path.join(base_path, ".w_probe")
            try:
                with open(probe, "wb") as f:
                    f.write(b"x")
                os.remove(probe)
                st = os.statvfs(base_path)
                free_gb = st.f_bavail * st.f_frsize / 1e9
                fstype = subprocess.run(
                    ["findmnt", "-n", "-o", "FSTYPE", "--target", base_path],
                    stdout=subprocess.PIPE, text=True).stdout.strip()
                need_gb = (need_bytes or 0) * 2 / 1e9   # bin + pcap 约两份
                note = f"{base_path} 可写，{fstype or '?'}，剩余 {free_gb:.1f} GB"
                if need_gb and free_gb < need_gb:
                    print(f"  [ERROR] {note} —— 本次约需 {need_gb:.1f} GB，不足")
                    all_ok = False
                else:
                    print(f"  -> {note}"
                          + (f"（本次约需 {need_gb:.1f} GB）" if need_gb else "")
                          + " [OK]")
            except OSError as e:
                print(f"  [ERROR] 落盘目录不可写: {base_path} ({e})")
                all_ok = False

    # eth0 有没有 IP —— 没有的话 CLI 连不上卡，但报错很不直观
    r = subprocess.run(["ip", "-4", "addr", "show", ETH_IF],
                       stdout=subprocess.PIPE, text=True)
    if "inet " in r.stdout:
        ip = [t for t in r.stdout.split() if t.count(".") == 3][0]
        print(f"  -> {ETH_IF} {ip} [OK]")
    else:
        print(f"  [ERROR] {ETH_IF} 没有 IPv4 地址")
        print(f"       sudo nmcli connection up \"Wired connection 1\"")
        all_ok = False

    return all_ok


def udp_drops():
    """读 /proc/net/udp 里数据口的 drops 计数，读不到返回 None

    这是**环节 4（主机内核缓冲溢出）的直接证据** —— 由内核维护，
    记录"socket 接收缓冲满了，丢弃了多少个数据报"。
    与 csv / pcap 的丢包数不同：后者只说丢了多少，不说丢在哪一层；
    这个计数 > 0 就是内核缓冲溢出的铁证（对应 rmem_max 不够）。

    注意：**必须在采集期间读** —— socket 只在 CLI_Record 运行时存在，
    采集结束后那一行就消失了，事后读永远是 None。
    """
    hex_port = f"{DATA_PORT:04X}"
    try:
        with open("/proc/net/udp") as f:
            next(f, None)                  # 跳过表头行（sl local_address ...）
            for line in f:
                cols = line.split()
                if len(cols) < 13:
                    continue
                local = cols[1].split(":")  # 形如 1E21A8C0:1002
                if len(local) == 2 and local[1].upper() == hex_port:
                    return int(cols[12])
    except (OSError, ValueError):
        pass
    return None


class DropMonitor:
    """采集期间后台轮询内核 drops 计数

    原实现是采集前后各读一次取差值，但**采集结束后 socket 已关闭**，
    /proc/net/udp 里那一行消失，事后读永远拿不到值 ——
    两次实测都只打印"读不到"，即该措施完全无效。
    改为在采集期间轮询，记录首个可读值与最大值。
    """

    def __init__(self, interval=0.5):
        self.interval = interval
        self.first = None
        self.peak = None
        self.samples = 0
        self._stop = threading.Event()
        self._thread = None

    def _loop(self):
        while not self._stop.is_set():
            v = udp_drops()
            if v is not None:
                self.samples += 1
                if self.first is None:
                    self.first = v
                self.peak = v if self.peak is None else max(self.peak, v)
            self._stop.wait(self.interval)

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def report(self):
        if self.first is None:
            return "内核 UDP drops: 采集期间未读到 socket（CLI_Record 可能没起来）"
        delta = self.peak - self.first
        s = (f"内核 UDP drops: 起始 {self.first} 峰值 {self.peak} "
             f"增量 {delta}（采样 {self.samples} 次）")
        if delta:
            s += "  ← 内核缓冲溢出！调大 net.core.rmem_max"
        return s

    @property
    def delta(self):
        if self.first is None:
            return None
        return self.peak - self.first


def is_real_error(line, cmd):
    """判断串口回显是不是"我们发的这条命令"出错了

    不能只搜 error / not recognized：
      - `'?`???...' is not recognized` 报的是缓冲里的噪声，不是我们的命令
      - `Init Calibration Status = 0x1ffe` 带 Debug 字样，是正常启动信息
      - `Ignored: Sensor is already stopped` 是提示，不是错误
    判据：出错的那行必须提到我们刚发的命令名（TI 的 CLI 会把命令原文括在
    单引号里回显），否则视为无关噪声。
    """
    low = line.lower()
    if "not recognized" in low:
        # 形如 '<原文>' is not recognized as a CLI command
        quoted = line.split("'")
        subject = quoted[1] if len(quoted) >= 2 else ""
        return subject.strip() == cmd.split()[0] or subject.strip() == cmd
    if "error" in low:
        if low.startswith("debug:") or "calibration status" in low:
            return False
        return True
    return False


def record_proc_running():
    """CLI_Record 是否真的在跑

    不能只看 CLI_Control 的退出码 —— 它起子进程失败时仍返回 0。
    用 -f 全命令行匹配：进程名超过 15 字符，pgrep -x 匹配不到。
    """
    r = subprocess.run(["pgrep", "-f", "DCA1000EVM_CLI_Record start_record"],
                       stdout=subprocess.PIPE, text=True)
    return bool(r.stdout.strip())


def wait_for_csv(base, prefix, timeout=15.0):
    """杀 CLI_Record 之前，等它把 csv 汇总写完

    csv 一直是 0 字节的根因（读源码确认，不是 TI 不产出）：

        CLI_Record 进程内：
          StartRecordData → WriteRecordSettingsInLogFile()   rf_api.cpp:1634
                            → fopen("..._LogFile.csv","w")   ← 只建文件，0 字节
          采集完成，卡发 STS_REC_COMPLETED
                            → cli_record_main.cpp:325 "Record is completed"
                            → StopRecordProc_Callback()      :326
                            → StopRecordProc_Thread() → StopRecordData()  :177
                            → WriteInlineProcSummaryInLogFile()  rf_api.cpp:1882/1895
                            → fprintf 统计 + fclose()            :2396,2398

    **汇总由 CLI_Record 自己写**，而我们在 stop_record 报 -4068 后立刻
    SIGTERM/SIGKILL 掉它，它还没走到 :1882 ⇒ 文件停在 0 字节。
    SIGKILL 连 stdio 缓冲都不 flush。Windows 上有 csv，是因为那边
    stop_record 能正常完成，CLI_Record 自己走完这条路再退出。

    为什么不必等满 90 秒：STS_REC_COMPLETED 触发的收尾（:326）与接收线程
    那个 90 秒 recvfrom 超时是**两条独立路径**。实测 CLI 日志里
    "Record is completed" 出现在我们发 stop_record 之前 9 秒，说明那时
    CLI_Record 已在走收尾。所以只要别急着杀，csv 几秒内就该出来。

    有界等待：超时就按原方式杀，不会卡住流程。csv 提供的
    out-of-sequence / zero-filled 计数已被 pcap 判据覆盖且更强
    （能定位到帧、能分层区分卡侧/主机侧），不值得为它牺牲 90 秒。
    """
    paths = glob.glob(os.path.join(base, f"{prefix}*LogFile.csv"))
    if not paths:
        return None
    csv_path = paths[0]
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if os.path.getsize(csv_path) > 0:
                dt = time.time() - t0
                print(f"  -> csv 汇总已写出（等待 {dt:.1f} 秒，"
                      f"{os.path.getsize(csv_path):,} B）")
                return dt
        except OSError:
            pass
        if not record_proc_running():
            # 进程自己退了，再看一眼文件
            try:
                if os.path.getsize(csv_path) > 0:
                    print(f"  -> csv 汇总已写出（CLI_Record 自行退出）")
                    return time.time() - t0
            except OSError:
                pass
            break
        time.sleep(0.3)
    print(f"  [WARN] 等了 {timeout:.0f} 秒 csv 仍为空 —— "
          "pcap 判据不受影响，继续")
    return None


def kill_record_proc(timeout=6.0):
    """终止残留的 CLI_Record 并等它真的消失

    为什么每次都会残留（读源码确认）：
      - `stop_record` 通过 UDP 发到本机 4096 通知 CLI_Record，然后轮询
        共享内存等它回报状态，**只等 CLI_CMD_TIMEOUT_DURATION = 7000 ms**
        （Common/globals.h:181）
      - 而 CLI_Record 的接收线程阻塞在 recvfrom 上，超时是
        SOCKET_THREAD_TIMEOUT_DURATION_SEC = 90 秒
        （Common/rf_api_internal.h:135 = CAPTURE_TIMEOUT_DURATION_SEC 80 + 10）
      - 雷达停止出流后没有新包，CLI_Record 要等到 90 秒超时才醒来；
        stop_record 7 秒就放弃、报 -4068，并 DestroyShm 删掉共享内存 ——
        CLI_Record 醒来后连状态都无处回写，就一直挂着

    即"停止等待 7 秒 < 接收线程唤醒 90 秒"是 TI 的设计缺陷，
    数据流正常结束时必然触发。数据此时已全部落盘，直接终止是安全的。
    """
    pids = subprocess.run(["pgrep", "-f", "DCA1000EVM_CLI_Record"],
                          stdout=subprocess.PIPE, text=True).stdout.split()
    if not pids:
        return 0

    for sig in ("-TERM", "-KILL"):
        subprocess.run(["sudo", "kill", sig] + pids, stderr=subprocess.DEVNULL)
        deadline = time.time() + timeout / 2
        while time.time() < deadline:
            if not record_proc_running():
                print(f"  -> 已终止 {len(pids)} 个残留 CLI_Record")
                return len(pids)
            time.sleep(0.2)
    print(f"  [WARN] {len(pids)} 个 CLI_Record 仍未退出: {' '.join(pids)}")
    return len(pids)


def cleanup_shm():
    """清掉僵死的 System V 共享内存段

    CLI 用共享内存标记"录制进程在跑"（clishm_<配置口>）。CLI_Record 崩溃或
    被 kill -9 后这个段会留下，nattch=0 但 key 还在，下次 start_record 会
    误报 "Stop the already running process."。
    """
    r = subprocess.run(["ipcs", "-m"], stdout=subprocess.PIPE, text=True)
    removed = 0
    for line in r.stdout.splitlines():
        cols = line.split()
        # key shmid owner perms bytes nattch
        if len(cols) >= 6 and cols[0] == "0xffffffff" and cols[5] == "0":
            if subprocess.run(["ipcrm", "-m", cols[1]],
                              stderr=subprocess.DEVNULL).returncode == 0:
                removed += 1
    if removed:
        print(f"  -> 清掉 {removed} 个僵死共享内存段")
    return removed


def send_cfg(profile_path):
    """逐行把 cfg 发到雷达配置口"""
    print(f"  -> 打开 {RADAR_PORT} @ {RADAR_BAUDRATE} ...", end=" ", flush=True)
    try:
        ser = serial.Serial(
            port=RADAR_PORT, baudrate=RADAR_BAUDRATE,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE, timeout=2,
        )
    except serial.SerialException as e:
        print(f"[ERROR] {e}")
        return False, [], False
    print("[OK]")

    # 清空开口前积压的输入：可能是上次运行的残留、上电启动横幅
    # （xWR68xx MMW Demo 03.06.02.00 ...），或线上噪声。
    # 不清的话，这些内容会在第一条命令处被当作一行读出，
    # 表现为 "'?`???...' is not recognized as a CLI command" 之类的假报错。
    ser.reset_input_buffer()
    time.sleep(0.3)
    if ser.in_waiting:
        junk = ser.read(ser.in_waiting).decode(errors="replace")
        n = len(junk.strip().splitlines())
        if n:
            print(f"  -> 丢弃开口前的 {n} 行积压内容（启动横幅/噪声）")
        ser.reset_input_buffer()

    with open(profile_path, encoding="utf-8") as f:
        cmds = [ln.strip() for ln in f
                if ln.strip() and not ln.strip().startswith("%")]

    print(f"  -> 发送 {len(cmds)} 条命令")
    critical = {"sensorStop", "flushCfg"}
    errors = []
    sensor_started = False
    replies_seen = 0

    for i, cmd in enumerate(cmds, 1):
        ser.write((cmd + "\n").encode())
        label = cmd if len(cmd) <= 42 else cmd[:39] + "..."
        print(f"     [{i:2d}/{len(cmds)}] {label}")

        if cmd == "sensorStart":
            # sensorStart 不能只等固定时长：它要真的分配 L3 内存、配 CBUFF、
            # 启动硬件流水线，实测成功时要**好几秒**才回 Done。
            # 原实现只等 0.5 秒，会把成功的采集误判成失败并中止。
            # 失败时还会**死锁**（源码确认，SDK 03.06.02.00）：
            #   MSS  MmwDemo_DPM_ioctl_blocking → Semaphore_pend(BIOS_WAIT_FOREVER)
            #        mss_main.c:1735  ← 无超时
            #   DSS  DPC 分配失败（如 L3 装不下 radarCube+detMatrix）→ 返回 ENOMEM
            #   MSS  reportFxn 只在 DPM_Report_IOCTL 分支 post 信号量（:2631），
            #        DPC 报错不走那分支 ⇒ 永远没人 post ⇒ CLI 任务永久阻塞
            # 所以"Error -1"那行代码根本执行不到，必须靠超时判定。
            lines, t0 = [], time.time()
            verdict = "timeout"
            while time.time() - t0 < SENSOR_START_TIMEOUT:
                if ser.in_waiting:
                    chunk = ser.read(ser.in_waiting).decode(errors="replace")
                    for l in (x.strip() for x in chunk.splitlines()):
                        if not l:
                            continue
                        lines.append(l)
                        print(f"               <- {l}")
                        if is_real_error(l, cmd):
                            errors.append(f"{cmd} -> {l}")
                        low = l.lower()
                        if low.startswith("done"):
                            verdict = "done"
                        elif low.startswith("error") or "exception:" in low:
                            verdict = "error"
                    if verdict != "timeout":
                        break
                else:
                    time.sleep(0.1)
            sensor_started = (verdict == "done")
            if verdict == "timeout":
                print(f"               [超时] {SENSOR_START_TIMEOUT:.0f} 秒内"
                      "既无 Done 也无 Error")
                if replies_seen:
                    print("               前面命令都正常回话 ⇒ 雷达侧挂死"
                          "（很可能是片上 L3 装不下，见上方注释）")
                    print("               ⚠ 需断电重启雷达才能恢复")
                else:
                    print("               前面命令也全无回显 ⇒ 串口不通，"
                          "非配置问题")
            continue

        time.sleep(0.5 if cmd in critical else 0.05)

        if ser.in_waiting:
            resp = ser.read(ser.in_waiting).decode(errors="replace").strip()
            lines = [l.strip() for l in resp.splitlines() if l.strip()]
            if lines:
                replies_seen += 1
            for line in lines:
                print(f"               <- {line}")
                if is_real_error(line, cmd):
                    errors.append(f"{cmd} -> {line}")

    ser.close()

    if errors:
        print(f"  [{'WARN' if sensor_started else 'ERROR'}] "
              f"雷达返回 {len(errors)} 条错误:")
        for e in errors:
            print(f"     {e}")
    if not sensor_started:
        print("  [ERROR] 没有收到 sensorStart 的 Done —— 雷达未启动")
    return (not errors) and sensor_started, errors, sensor_started


def send_sensor_stop(timeout=3.0):
    """单独发一条 sensorStop —— 提前中止时唯一能真正停下雷达的手段

    为什么必须单独发：send_cfg() 发完最后一条就 close() 了串口，收尾段
    一条串口命令都没有。原先的 Ctrl-C 路径只发 stop_record，而那是对
    **DCA1000** 说的 —— 雷达完全不知情，会按 cfg 里的帧数继续发射到
    跑完为止。妊娠 54000 帧那种配置，第 1 分钟中止的话雷达还要空转
    44 分钟，真正让它停下来的是**下一次采集开头 cfg 里的那句 sensorStop**。
    check_field_limits 的注释里早就写明了这个行为（"收尾只发 stop_record
    不发 sensorStop，雷达不会停"），只是此前没有提前中止的需求。

    这段空转还会全部计进阶段 7 要测的功耗里。

    失败必须明确报出来，不能静默略过：串口打不开（ModemManager 抢占、
    USB 重新枚举中）意味着雷达停不下来，这是需要人工拔 5V 的情形。
    """
    print("  -> 发送 sensorStop 到雷达 ...", end=" ", flush=True)
    try:
        ser = serial.Serial(port=RADAR_PORT, baudrate=RADAR_BAUDRATE,
                            timeout=1)
    except serial.SerialException as e:
        print(f"[FAIL] {e}")
        print("     ⚠ 雷达没有收到停止命令，会继续发射到帧数跑完为止。")
        print("       若要立刻停：拔掉雷达 5V 电源。")
        return False, str(e)

    try:
        ser.reset_input_buffer()
        ser.write(b"sensorStop\n")
        lines, t0 = [], time.time()
        while time.time() - t0 < timeout:
            if ser.in_waiting:
                chunk = ser.read(ser.in_waiting).decode(errors="replace")
                for l in (x.strip() for x in chunk.splitlines()):
                    if l:
                        lines.append(l)
                low = " ".join(lines).lower()
                # "Done" 是正常应答；已经停了的话回 "Ignored: Sensor is
                # already stopped"，那同样算达成目的
                if "done" in low or "already stopped" in low:
                    break
            else:
                time.sleep(0.05)
    finally:
        try:
            ser.close()
        except Exception:
            pass

    reply = " | ".join(lines) if lines else "(无回显)"
    low = reply.lower()
    ok = ("done" in low) or ("already stopped" in low)
    print("[OK]" if ok else "[WARN]")
    if lines:
        for l in lines:
            print(f"       <- {l}")
    if not ok:
        print(f"     ⚠ {timeout:.0f} 秒内没等到 Done —— 雷达可能仍在发射。")
    return ok, reply


def abort_capture(jc, fc, pcap_proc, rec_proc, dropmon, t0, reason,
                  notifier=None, settle_sec=ABORT_SETTLE_SEC,
                  use_pcap=True, serial_errs=None):
    """中止收尾 —— 停止键与 Ctrl-C 走这同一条路

    与正常收尾（_run 的 [6][7][8]）的三处区别：
      1. **先发 sensorStop** —— 正常路径不需要，雷达跑完帧数会自己停
      2. 只等 settle_sec 秒而不是 BUFFER_SEC，中止的数据本就不指望用
      3. 归档到 ABORT_ 前缀目录，**不跑判据**（不跑 L3、不跑 RX 检测）

    **归档这一步是本函数存在的首要理由。** 原先的 Ctrl-C 路径不归档，
    _Raw_*.bin / .pcap / .csv 全留在 fileBasePath 根目录变成游离文件，
    于是**下一次采集会被 capture() 里的残留检查直接挡回并 return False**。
    牛场里这意味着：饲养员按了停止键，之后按开始键永远无声失败 ——
    而封盒后没有屏幕能看到那条报错，现象就是"遥控器坏了"。
    2026-08-06 帧数回绕那次实测踩到过这个（当时的记录：
    "手动 Ctrl-C 收场，留下三个游离文件没归档"）。

    数据**移走归档而不是删除**，沿用"判废也只加前缀、数据保留不删"
    的既有规则。中止前那段数据有时仍然可用，删了不可逆。
    """
    base, prefix = jc["base_path"], jc["prefix"]
    if notifier:
        notifier.emit("aborting", reason)

    print("\n[中止] " + reason)

    # 先停 dropmon：stop_record 会关掉 socket，之后 /proc/net/udp 读不到
    if dropmon is not None:
        dropmon.stop()

    # ① 让雷达真的停下来。放在最前面 —— 每晚一秒就多空转一秒。
    stop_ok, stop_reply = send_sensor_stop()

    # ② 给 DCA1000 一点时间把在途数据推完再关记录，避免 pcap 断在半包
    print(f"  -> 等待 {settle_sec:.0f} 秒让在途数据落地 ...")
    time.sleep(settle_sec)

    # ③ 之后与正常收尾同序：stop_record → tcpdump → 清进程
    cli("stop_record", "stop_record")
    tcpdump_stats = stop_tcpdump(pcap_proc) if use_pcap else ""
    kill_record_proc()
    if rec_proc is not None and rec_proc.poll() is None:
        rec_proc.terminate()
    cleanup_shm()

    # ④ 归档。t0 为 None 表示还没进入采集（准备阶段就中止了），
    #    用当前时刻命名，目录名里不写"到几点"。
    t_ref = t0 or datetime.now()
    name = (f"ABORT_Cow_{COW_ID}_{CAPTURE_MODE}_{t_ref:%Y%m%d_%H%M%S}")
    session = os.path.join(base, name)

    print("\n[中止归档]")
    moved = []
    for p in data_files(base, prefix):
        if os.path.isfile(p):
            os.makedirs(session, exist_ok=True)
            shutil.move(p, os.path.join(session, os.path.basename(p)))
            moved.append(os.path.basename(p))

    if not moved:
        # 准备阶段就中止、雷达还没出流时是正常的
        print("  -> 没有产生任何数据文件（中止得早），无需归档")
        if notifier:
            notifier.emit("done_abort", "无数据文件")
        return None

    for fn in sorted(moved):
        size = os.path.getsize(os.path.join(session, fn))
        print(f"     {fn}  {size:,} B")

    bins = sorted(glob.glob(os.path.join(session, "*_Raw_*.bin")))
    total = sum(os.path.getsize(b) for b in bins)
    got_frames = total // fc["frame_bytes"] if fc["frame_bytes"] else 0
    planned = fc["frames"]
    elapsed = (datetime.now() - t0).total_seconds() if t0 else 0.0

    print(f"  -> 实际完整帧 {got_frames:,} / 预定 {planned:,}"
          f"（{got_frames / planned * 100:.1f}%），bin {total:,} B")

    # ⑤ 精简 meta。verdict 仍放最前，机器 grep "^verdict=" 的约定不变；
    #    ABORTED 是新增的第四种取值（原有 GOOD / BAD / UNKNOWN）。
    with open(os.path.join(session, "capture_meta.txt"), "w",
              encoding="utf-8") as f:
        f.write("# ===== 判定 =====\n")
        f.write("verdict=ABORTED\n")
        f.write(f"verdict_reason=人工中止: {reason}\n")
        f.write("checks_total=0\nchecks_passed=0\nchecks_failed=0\n")
        f.write("checks_unknown=0\n")
        f.write("# 中止的数据不跑判据（L3 逐字节与 RX 通道检测均未执行）。\n")
        f.write("# 要事后补判： verify_pcap_bin.py <本目录> --frame-bytes "
                f"{fc['frame_bytes']}\n")

        f.write("\n# ----- 中止信息 -----\n")
        f.write("stop_reason=manual_abort\n")
        f.write(f"abort_detail={reason}\n")
        f.write(f"sensor_stop_sent={'yes' if stop_ok else 'FAILED'}\n")
        f.write(f"sensor_stop_reply={stop_reply}\n")
        f.write(f"frames_planned={planned}\n")
        f.write(f"frames_captured={got_frames}\n")
        f.write(f"elapsed_sec={elapsed:.1f}\n")
        f.write(f"settle_sec={settle_sec}\n")

        f.write("\n# ----- 采集参数 -----\n")
        f.write(f"cow_id={COW_ID}\nmode={CAPTURE_MODE}\nnote={NOTE}\n")
        f.write(f"cfg_file={os.path.basename(PROFILE_CFG)}\n")
        f.write(f"json_file={os.path.basename(JSON_CFG)}\n")
        f.write(f"frames={fc['frames']}\nperiod_ms={fc['period_ms']}\n")
        f.write(f"loops={fc['loops']}\n")
        f.write(f"samples={fc['samples']}\ntx_count={fc['tx_count']}\n")
        f.write(f"rx_count={fc['rx_count']}\n")
        f.write(f"channel_cfg={fc['rx_mask']} {fc['tx_mask']} 0\n")
        f.write(f"frame_bytes={fc['frame_bytes']}\n")
        f.write(f"bin_total_bytes={total}\nbin_files={len(bins)}\n")
        if t0:
            f.write(f"start={t0:%Y-%m-%d %H:%M:%S}\n")
        f.write(f"aborted_at={datetime.now():%Y-%m-%d %H:%M:%S}\n")
        f.write(f"pcap={'yes' if use_pcap else 'no'}\n")
        f.write(f"host={os.uname().nodename}\n")
        if tcpdump_stats:
            f.write(f"tcpdump_stats={tcpdump_stats}\n")
        if serial_errs:
            f.write(f"serial_errors={len(serial_errs)}\n")
            for e in serial_errs:
                f.write(f"  {e}\n")

    print(f"\n  数据: {session}")
    if not stop_ok:
        print("  ⚠ sensorStop 未确认成功 —— 下次采集前确认雷达已停"
              "（或拔 5V 重上电）")
    if notifier:
        notifier.emit("done_abort",
                      f"{got_frames}/{planned} 帧")
    print("=" * 62)
    return session



def tcpdump_buf_kb(rate_bps, seconds=8.0, floor_kb=8192):
    """按码率算 -B（单位 KB），而不是拍一个固定数

    -B 是 tcpdump 自己的 AF_PACKET 捕获缓冲，与 rmem_max / netdev_max_backlog
    是**三套独立的缓冲**：
        网卡 → [netdev_max_backlog]  内核协议栈入口（两路共用）
                 ├→ UDP socket → [rmem_max] → CLI_Record → bin
                 └→ AF_PACKET  → [-B 这个]  → tcpdump    → pcap

    溢出机制是生产/消费速率不匹配：内核按码率往缓冲里放，tcpdump 取出写盘。
    只要 tcpdump 被调度延迟（CPU 被 CLI_Record 抢、写盘阻塞、页缓存回写），
    缓冲就积压，积压超过 -B 就丢包。所以 -B 的意义是
    **"能吸收多少秒的调度抖动"**，必须随码率缩放。

    实测依据：
      妊娠 2.61 MB/s + -B 8192(8 MB) → 约 3.1 秒余量 → dropped 0（阶段 5）
      行为 7.23 MB/s + -B 8192(8 MB) → 约 1.1 秒余量 → dropped 22,510（08-02）
    取 8 秒余量：行为配置约 59 MB，Pi 有 2 GB 内存，可承受。
    """
    if not rate_bps:
        return floor_kb
    return max(floor_kb, int(rate_bps * seconds / 1024))


def start_tcpdump(pcap_path, buf_kb=8192):
    """并行抓包

    -s 0 必须给：默认 snaplen 会截断载荷，数据全废。
    不开 --immediate-mode：每包立刻刷盘，高速流下反而丢包。
    """
    # -qtn 抑制协议解析与反向 DNS，省 CPU（取自 mmwave-capture-std）
    #
    # -Z pi 是 exFAT 落盘的必需项。tcpdump 出于安全会把自己降权到
    # `tcpdump` 用户（/etc/passwd 里 shell 是 nologin）。而 exFAT 没有
    # Unix 所有权概念，整个挂载按 uid=1000(pi) 固定，`tcpdump` 用户在上面
    # **无写权限** —— 症状是只报一句 "Couldn't change ownership of savefile"，
    # 退出码仍为 0，pcap 停在 24 字节（只有文件头、零个包）。
    #
    # 2026-08-02 实测对照（同一测试，各发 500 个 UDP 包）：
    #   microSD ext4，不加 -Z  → 342 包
    #   exFAT，  不加 -Z       → **0 包**，24 字节
    #   exFAT，  -Z root       → **0 包**（exFAT 挂载 uid=1000，root 也不是所有者）
    #   exFAT，  -Z pi         → **500 包**，729 KB，0 dropped
    # 故必须显式 -Z pi，且 root 无效。
    cmd = ["sudo", "tcpdump", "-i", ETH_IF, "-s", "0", "-B", str(buf_kb), "-qtn",
           "-Z", PCAP_USER, "-w", pcap_path, f"udp port {DATA_PORT}"]
    print(f"  -> tcpdump -> {os.path.basename(pcap_path)} ...", end=" ", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    time.sleep(1.5)            # 让它把 BPF 装好再放数据进来
    if proc.poll() is not None:
        print(f"[FAIL] {proc.stderr.read().strip()}")
        return None
    print("[OK]")
    return proc


def stop_tcpdump(proc):
    """停止抓包，返回它自报的 captured/dropped 统计

    必须先 SIGUSR2 再 SIGINT：-B 8192 给了 8 MB 内核缓冲，
    SIGINT 到达时里面可能还压着几 MB 没落盘的包，直接停就丢了。
    SIGUSR2 让 tcpdump 先把缓冲刷到文件。
    （做法取自 mmwave-capture-std 的 radardca.py:stop_tcpdump_capture）

    退出时 tcpdump 往 stderr 打
    `N packets captured / N packets received by filter / M packets dropped by kernel`。
    **dropped 是环节 5（用户态处理不及）的直接证据** ——
    与 /proc/net/udp 的 drops 不同：那个是 UDP socket 缓冲溢出（环节 4），
    这个是 AF_PACKET 抓包缓冲溢出，说明 tcpdump 自己跟不上。
    """
    if proc is None:
        return ""
    print("  -> 停止 tcpdump（先刷缓冲）...", end=" ", flush=True)
    subprocess.run(["sudo", "kill", "-USR2", str(proc.pid)], stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    subprocess.run(["sudo", "kill", "-INT", str(proc.pid)], stderr=subprocess.DEVNULL)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        subprocess.run(["sudo", "kill", "-9", str(proc.pid)], stderr=subprocess.DEVNULL)
    print("[OK]")

    try:
        err = proc.stderr.read() if proc.stderr else ""
    except (OSError, ValueError):
        err = ""
    stats = " ".join(l.strip() for l in err.splitlines()
                     if "packets" in l and ("captured" in l or "dropped" in l
                                            or "received" in l))
    if "dropped by kernel" in stats:
        m = re.search(r"(\d+)\s+packets dropped by kernel", stats)
        if m and int(m.group(1)) > 0:
            stats += "  ← tcpdump 抓包缓冲溢出，调大 -B"
    return stats


def check_csv(session_dir):
    """读 CLI 的 csv 判丢包

    判据是 csv 的计数，不是文件大小 —— 第一阶段已实测 DCA1000 会用
    零填充补齐缺失包，丢 4863 包后文件大小仍精确等于期望值。
    """
    csvs = glob.glob(os.path.join(session_dir, "*LogFile.csv"))
    if not csvs:
        return None, "没有找到 csv，无法判定丢包"

    text = open(csvs[0], encoding="utf-8", errors="replace").read()
    if not text.strip():
        return None, (f"{os.path.basename(csvs[0])} 为空 —— "
                      "CLI_Record 未写出汇总（见 wait_for_csv 注释）")

    # 必须锚定 TI 的确切字段名（rf_api.cpp:2358-2380）：
    #   "Out of sequence count - %llu"
    #   "Out of sequence seen from %u to %u"      ← 是偏移，不是计数
    #   "Number of zero filled packets - %llu"
    #   "Number of zero filled bytes - %llu"      ← 是字节数，不是包数
    # 原先用宽松的 r"[Oo]ut of [Ss]equence\D*(\d+)" 会把 "seen from" 的
    # 起始偏移也算成丢包数，把 zero filled bytes 也加进 zero-filled 包数。
    # 实测：真值 oos=12 / zf=4863 被算成 4108 / 7085391。
    oos_m = re.findall(r"Out of sequence count\s*-\s*(\d+)", text)
    zf_m = re.findall(r"Number of zero filled packets\s*-\s*(\d+)", text)
    zfb_m = re.findall(r"Number of zero filled bytes\s*-\s*(\d+)", text)
    rcv_m = re.findall(r"Number of received packets\s*-\s*(\d+)", text)

    oos = sum(int(n) for n in oos_m) if oos_m else None
    zf = sum(int(n) for n in zf_m) if zf_m else None
    zfb = sum(int(n) for n in zfb_m) if zfb_m else None
    rcv = sum(int(n) for n in rcv_m) if rcv_m else None

    if oos is None and zf is None:
        return None, f"csv 中未匹配到丢包字段，请人工查看 {os.path.basename(csvs[0])}"

    parts = [f"out-of-sequence={oos}", f"zero-filled-packets={zf}"]
    if zfb is not None:
        parts.append(f"zero-filled-bytes={zfb}")
    if rcv is not None:
        parts.append(f"received={rcv}")
    return (oos or 0) + (zf or 0) == 0, " ".join(parts)


PCAP_PAYLOAD_OFF = 42      # Ethernet 14 + IP 20 + UDP 8
DCA_HDR_LEN = 10           # UINT32 序号 + 6 字节累计已发送字节数


def check_pcap(session_dir, expect_bytes=None):
    """三层证据判完整性

    单看"bin 与 pcap 一致"不足以证明没丢数据 —— 两者共享同一个上游
    （内核网络栈），包在到达内核前就丢了的话两边都没有，比对依然完美。
    故分三层，缺一不可：

      L1 配置→卡 : 卡自报发送量 == 帧数 × 每帧字节  （查雷达少发帧/卡少收 LVDS）
      L2 卡→主机 : 卡自报发送量 == 主机实收载荷量    （查网线丢包/内核缓冲溢出）
      L3 主机内部: bin ≡ pcap 逐字节（由 pcap_to_bin.py --verify 做）

    L2 的关键是每包偏移 4 起的 6 字节"累计已发送字节数"——
    该值来自卡侧，不受主机侧任何环节影响。由此可纯软件区分：
      卡自报 == 实收 但 < 应产生  → 卡侧丢包（LVDS 溢出/DDR3 满/FPGA 过快）
      卡自报 >  实收              → 主机侧丢包（内核缓冲/用户态/磁盘）
    这一点封盒后尤其重要 —— 面板 LED 看不见了。
    """
    paths = glob.glob(os.path.join(session_dir, "*.pcap"))
    if not paths:
        return None

    seqs = []
    payload_total = 0
    first_ts = last_ts = None
    truncated = False
    first_cum = None
    last_cum = last_len = 0

    with open(paths[0], "rb") as f:
        if len(f.read(24)) < 24:
            return {"clean": None, "detail": "pcap 为空"}
        while True:
            ph = f.read(16)
            if len(ph) < 16:
                break
            ts_s, ts_us, caplen, wirelen = struct.unpack("<IIII", ph)
            pkt = f.read(caplen)
            if len(pkt) < caplen:
                break
            if caplen != wirelen:      # snaplen 截断 → 数据不可用
                truncated = True
                break
            udp = pkt[PCAP_PAYLOAD_OFF:]
            if len(udp) < DCA_HDR_LEN:
                continue
            seqs.append(struct.unpack("<I", udp[:4])[0])
            # 6 字节小端补成 8 字节
            cum = struct.unpack("<Q", udp[4:DCA_HDR_LEN] + b"\x00\x00")[0]
            if first_cum is None:
                first_cum = cum
            last_cum, last_len = cum, len(udp) - DCA_HDR_LEN
            payload_total += len(udp) - DCA_HDR_LEN
            ts = ts_s + ts_us / 1e6
            first_ts = ts if first_ts is None else first_ts
            last_ts = ts

    if truncated:
        return {"clean": False, "detail": "包被截断（snaplen），数据不可用 —— 检查 -s 0"}
    if not seqs:
        return {"clean": None, "detail": "pcap 里没有数据包"}

    span = max(len(set(seqs)), seqs[-1] - seqs[0] + 1)
    missing = span - len(set(seqs))
    dups = len(seqs) - len(set(seqs))
    oos = sum(1 for a, b in zip(seqs, seqs[1:]) if b <= a)
    rate = payload_total / (last_ts - first_ts) / 1e6 if last_ts > first_ts else 0

    # 累计字节数与包序号**不随 start_record 归零** —— 它们是 FPGA 上电后
    # 持续累加的。所以必须取**增量**，不能直接用末包的累计值。
    #
    # 2026-08-02 实测踩到：上次采集结束在序号 1519121 / 累计 2,211,840,000，
    # 本次就从 1519122 / 2,211,840,000 接着数。旧代码把末包累计值
    # （4,423,680,000 = 两次之和）当成本次总量，于是
    #   L2 报"主机侧丢包 2,211,840,000 B"、L1 报"卡侧丢包"、
    #   首包累计非 0 报"开头漏了包" —— 三条全是同一个 bug 的三种表现，
    # 而实际按增量算 L1/L2 都精确为 0，那次采集是成功的。
    #
    # 前几次没暴露，是因为那几次都在 Pi 重启 / FPGA 重新配置后跑，
    # 计数器恰好从 0 开始。
    card_sent = (last_cum + last_len) - first_cum   # 本次采集卡自报发送量
    l2_gap = card_sent - payload_total       # >0 = 主机侧丢
    l1_gap = (expect_bytes - card_sent) if expect_bytes else None

    lines = [f"L0 {len(seqs)} 包 序号 {seqs[0]}→{seqs[-1]}, "
             f"丢失 {missing} 重复 {dups} 乱序 {oos}, {rate:.2f} MB/s"]
    if first_cum:
        lines.append(f"   （首包累计 {first_cum:,} B —— FPGA 上电后的历史累计，"
                     "已按增量扣除）")
    lines.append(f"L2 卡自报增量 {card_sent:,} B vs 实收 {payload_total:,} B, "
                 f"差 {l2_gap:,} B"
                 + ("" if l2_gap == 0 else "  ← 主机侧丢包"))
    if expect_bytes:
        lines.append(f"L1 应产生 {expect_bytes:,} B vs 卡自报增量 {card_sent:,} B, "
                     f"差 {l1_gap:,} B"
                     + ("" if l1_gap == 0 else "  ← 卡侧丢包或雷达少发帧"))

    # 不再把 first_cum != 0 当异常 —— 那是 FPGA 上电后的历史累计，
    # 只有第一次采集才为 0。真正的判据是增量三者相符。
    clean = (missing == 0 and dups == 0 and oos == 0
             and l2_gap == 0
             and (l1_gap == 0 if expect_bytes else True))
    return {"clean": clean, "detail": " | ".join(lines),
            "lines": lines, "payload": payload_total,
            "card_sent": card_sent, "l1_gap": l1_gap, "l2_gap": l2_gap}


def capture(idx, jc, fc, use_pcap, run_l3=True, remote=None, notifier=None):
    base = jc["base_path"]
    prefix = jc["prefix"]
    dur = fc["frames"] * fc["period_ms"] // 1000
    wait_sec = dur + BUFFER_SEC
    t_setup = {"base": base, "prefix": prefix, "dur": dur, "wait_sec": wait_sec}

    print("=" * 62)
    print(f"  采集 #{idx}   {fc['frames']} 帧 @ {fc['period_ms']} ms = {dur} 秒")
    print("=" * 62)

    os.makedirs(base, exist_ok=True)

    # 内核缓冲自检 + 自动修正。放在这里而不是靠人工跑 prepare.sh：
    # sysctl -w 不持久，重启即失，忘记跑不报错、只静默降级成丢包。
    if not preflight(fc["frame_bytes"] * 1000.0 / fc["period_ms"],
                     base_path=base,
                     need_bytes=fc["frames"] * fc["frame_bytes"]):
        print("\n  [ERROR] 自检未通过，先解决上面的问题再采集")
        return False

    if not check_chcfg(fc):
        return False

    # 残留检查：沿用第一阶段规则，有残留就中止，避免两次数据混在一起
    leftover = [p for p in data_files(base, prefix) if os.path.isfile(p)]
    if leftover:
        print(f"  [ERROR] {base} 下有 {len(leftover)} 个 {prefix}* 残留文件：")
        for p in leftover[:5]:
            print(f"     {os.path.basename(p)}")
        print("  先处理掉再采，避免两次数据混在一个目录。")
        return False

    # 上次的 CLI_Record 必然残留（stop_record 的 7 秒超时 < 接收线程 90 秒
    # 唤醒周期，见 kill_record_proc 注释），这里自动清掉而不是要求人工处理
    if record_proc_running():
        print("  -> 发现上次残留的 CLI_Record，清理中")
        kill_record_proc()
        if record_proc_running():
            print("  [ERROR] 清理失败，手动执行： pkill -9 -f DCA1000EVM_CLI_Record")
            return False
    cleanup_shm()

    print("\n[1] 配置 DCA1000")
    if cli("fpga", "FPGA 配置")[0] != 0:
        print("  [WARN] FPGA 配置返回非 0，继续")
    if cli("record", "录制参数")[0] != 0:
        print("  [ERROR] 录制参数配置失败")
        return False

    pcap_proc = None
    rec_proc = None
    dropmon = None
    try:
        if use_pcap:
            print("\n[2] 启动抓包")
            ts_pre = datetime.now().strftime("%Y%m%d_%H%M%S")
            pcap_path = os.path.join(base, f"{prefix}_{ts_pre}.pcap")
            buf_kb = tcpdump_buf_kb(fc["frame_bytes"] * 1000.0 / fc["period_ms"])
            print(f"  -> -B {buf_kb:,} KB（{buf_kb / 1024:.0f} MB，约 8 秒余量）")
            pcap_proc = start_tcpdump(pcap_path, buf_kb)
            if pcap_proc is None:
                print("  [ERROR] tcpdump 启动失败。用 --no-pcap 可跳过")
                return False

        print("\n[3] 启动录制")
        _, rec_proc = cli("start_record", "start_record", wait=False, quiet=True)
        time.sleep(1.5)
        if not record_proc_running():
            print("  [ERROR] CLI_Record 没有启动起来。")
            print("  常见原因：僵死共享内存段（跑 cleanup_shm() 或 ipcrm -m <id>）")
            return False
        print("  -> CLI_Record 已在监听数据口 [OK]")

        # socket 已存在，现在才能读到 drops —— 必须在采集期间轮询
        dropmon = DropMonitor()
        dropmon.start()
        try:
            return _run(idx, jc, fc, use_pcap, pcap_proc, rec_proc, dropmon, t_setup,
                        run_l3, remote, notifier)
        finally:
            dropmon.stop()
    except (Exception, KeyboardInterrupt) as e:
        # tcpdump 一旦起来就必须回收，否则异常退出会留下后台进程继续写 pcap。
        # 走 abort_capture 而不是各自清理：它会发 sensorStop（否则雷达继续
        # 空转）并把游离文件归档（否则下次采集被残留检查挡死）。
        # 连 KeyboardInterrupt 一起捕获：准备阶段（起 tcpdump / 发 cfg）按
        # Ctrl-C 同样会留下 pcap 文件和运行中的进程，光靠 _run 里那个
        # 倒计时的 except 覆盖不到这一段。
        why = "Ctrl-C" if isinstance(e, KeyboardInterrupt) \
            else f"异常中止: {type(e).__name__}: {e}"
        abort_capture(jc, fc, pcap_proc, rec_proc, dropmon, None, why,
                      notifier=notifier, use_pcap=use_pcap)
        raise


def _run(idx, jc, fc, use_pcap, pcap_proc, rec_proc, dropmon, t_setup,
         run_l3=True, remote=None, notifier=None):
    base, prefix = t_setup["base"], t_setup["prefix"]
    dur, wait_sec = t_setup["dur"], t_setup["wait_sec"]

    # start_record 后 30 秒内必须让雷达出流，否则 CLI 超时
    # （mmwave_sdk_user_guide.txt:720）
    print("\n[4] 发送 cfg 到雷达")
    ok, errs, started = send_cfg(PROFILE_CFG)

    # 关键区分：sensorStart 成功了吗？
    # 若已启动，雷达就在发射、数据已在流 —— 此时中止会把一次好数据扔掉。
    # 前面命令的报错照样记进 meta，让人事后判断，但不掐断采集。
    if not ok and not started:
        print("  [ERROR] cfg 发送失败且 sensorStart 未成功，收尾")
        # 走 abort_capture：此时 tcpdump 已经建出 pcap 文件，不归档的话
        # 它会留在 fileBasePath 根目录，把下一次采集的残留检查挡死。
        abort_capture(jc, fc, pcap_proc, rec_proc, dropmon, None,
                      "cfg 发送失败且 sensorStart 未成功",
                      notifier=notifier, use_pcap=use_pcap, serial_errs=errs)
        return False
    if not ok and started:
        print(f"  [WARN] 有 {len(errs)} 条命令报错，但 sensorStart 已成功 ——")
        print("         数据已在流，继续采集。报错详情会写进 capture_meta.txt")

    t0 = datetime.now()
    print(f"\n  ** 采集开始 {t0:%Y-%m-%d %H:%M:%S}")
    # 到这里雷达才真正在发射。提示音放这里而不是按键那一刻 —— 中间隔着
    # preflight/配卡/起进程/发 cfg，实测约 10-20 秒。
    if remote is not None:
        remote.set_state(remote_control.CAPTURING)
    if notifier:
        notifier.emit("start", f"{fc['frames']} 帧 / {dur} 秒")

    print(f"\n[5] 等待 {wait_sec} 秒")
    aborted = None
    try:
        for r in range(wait_sec, 0, -1):
            sys.stdout.write(f"\r  剩余 {r:4d} 秒 ")
            sys.stdout.flush()
            if remote is not None:
                # 可被停止键打断的等待。stop_evt 在**准备阶段**按下也会 set，
                # 所以第一次循环就可能命中 —— 那正是"准备阶段按了停止键，
                # 等采集真正开始后立刻中止"的实现方式。
                if remote.sleep(1):
                    aborted = "饲养员按下停止键"
                    break
            else:
                time.sleep(1)
    except KeyboardInterrupt:
        aborted = "Ctrl-C"

    if aborted:
        print()
        if remote is not None:
            remote.set_state(remote_control.ABORTING)
        abort_capture(jc, fc, pcap_proc, rec_proc, dropmon, t0, aborted,
                      notifier=notifier, use_pcap=use_pcap, serial_errs=errs)
        return False
    print("\r  等待结束        ")

    print("\n[6] 停止")
    # 从这里开始不可打断：正在归档与逐字节校验，中断会毁掉一段已采好的数据
    if remote is not None:
        remote.set_state(remote_control.FINALIZING)
    if notifier:
        notifier.emit("finalizing")
    # 先停 dropmon：stop_record 会关闭 socket，那之后 /proc/net/udp 就读不到了
    dropmon.stop()

    # stop_record 必然报 -4068 超时（7 秒 < CLI_Record 的 90 秒唤醒周期），
    # 这是 TI 的设计缺陷、不是我们的错。数据此时已落盘，照常收尾。
    rc, _ = cli("stop_record", "stop_record")
    if rc != 0:
        print("     （-4068 超时属已知现象，见 kill_record_proc 注释，数据不受影响）")
    tcpdump_stats = stop_tcpdump(pcap_proc)

    # 杀之前先给 CLI_Record 机会把 csv 汇总写完 —— 汇总在它的进程里写，
    # 直接 SIGKILL 会让 csv 永远停在 0 字节。只在正常收尾路径上等，
    # 异常路径不等（那时数据本就有问题，没必要多花时间）。
    wait_for_csv(base, prefix)

    # 主动清掉 CLI_Record，否则它挂到 90 秒超时、下次采集被它挡住
    kill_record_proc()
    if rec_proc and rec_proc.poll() is None:
        rec_proc.terminate()
    cleanup_shm()

    # 目录名标注雷达实际采集窗口，不含封口缓冲
    t1 = t0 + timedelta(seconds=dur)
    name = f"Cow_{COW_ID}_{CAPTURE_MODE}_{t0:%Y%m%d_%H%M%S}_to_{t1:%H%M%S}"
    session = os.path.join(base, name)
    os.makedirs(session, exist_ok=True)

    print("\n[7] 整理")
    moved = []
    for p in data_files(base, prefix):
        if os.path.isfile(p):
            shutil.move(p, os.path.join(session, os.path.basename(p)))
            moved.append(os.path.basename(p))
    if not moved:
        print("  [ERROR] 没有找到任何数据文件")
        return False
    for fn in sorted(moved):
        size = os.path.getsize(os.path.join(session, fn))
        print(f"     {fn}  {size:,} B")

    bins = sorted(glob.glob(os.path.join(session, "*_Raw_*.bin")))
    total = sum(os.path.getsize(b) for b in bins)

    print("\n[8] 判定")
    # 逐项记录，最后统一裁决并写进 meta。
    # 原实现的三个缺口（2026-08-02 审计发现）：
    #   1. tcpdump dropped > 0 只打印字符串，不参与判废
    #      → 08-02 09:48 丢了 22,510 包，靠 L2 缺口连带才判 BAD；
    #        若哪天只有 tcpdump 丢而 L2 恰好对上，就会误判 GOOD
    #   2. L3 逐字节比对根本没跑（注释说"由 pcap_to_bin.py 做"，但从不调用）
    #      → meta 里的"L3 精确"其实只比了文件大小，而文件大小对丢包免疫
    #        （DCA1000 会零填充补齐，实测丢 4863 包后大小仍精确）
    #   3. bin 总量不符只打印提示，不判废
    #      → 08-01 那次 bin 只有 19.9 MB（应 2.2 GB），差 99% 也没据此判废
    checks = []          # (名称, True/False/None, 说明)

    def record(name, ok, note):
        checks.append((name, ok, note))
        mark = {True: "[OK]  ", False: "[FAIL]", None: "[??]  "}[ok]
        print(f"  {mark} {name}: {note}")
        return ok

    csv_clean, detail = check_csv(session)
    record("csv", csv_clean, detail)

    # pcap 判丢包。csv 常因 stop_record 超时而为空，pcap 更可靠：
    # 它给出每包序号，能算出丢失/重复/乱序，而不只是总数
    pcap_verdict = ""
    if use_pcap:
        pv = check_pcap(session, expect_bytes=fc["frames"] * fc["frame_bytes"])
        if pv:
            pcap_verdict = pv["detail"]
            for ln in pv.get("lines", [pv["detail"]]):
                print(f"         {ln}")
            record("L0/L1/L2 pcap", pv["clean"],
                   "序号+卡自报+实收 全部相符" if pv["clean"]
                   else pv["detail"][:120])

    # 缺口 1：tcpdump 自报丢包，现在参与判废
    if tcpdump_stats:
        m = re.search(r"(\d+)\s+packets dropped by kernel", tcpdump_stats)
        n_drop = int(m.group(1)) if m else None
        record("tcpdump 缓冲", (n_drop == 0) if n_drop is not None else None,
               f"dropped by kernel = {n_drop}"
               + ("" if n_drop == 0 else "  ← 调大 -B") if n_drop is not None
               else "未拿到统计")

    record("内核 UDP drops", (dropmon.delta == 0) if dropmon.delta is not None
           else None, dropmon.report())

    # 缺口 3：bin 总量现在参与判废。
    # 注意 bin 可能比"帧数×每帧"略少：DCA1000 末包按 1456 对齐，
    # 多出的零头不构成完整帧，CLI_Record 不落盘（实测差 128 B）。
    # 故允许"少于一个包"的差额，超出即判废。
    expect_total = fc["frames"] * fc["frame_bytes"]
    diff = total - expect_total
    record("bin 总量", abs(diff) < 1456,
           f"{total:,} B / 应 {expect_total:,} B  差 {diff:,} B"
           + ("（末包对齐零头，正常）" if 0 > diff > -1456 else ""))

    # 缺口 2：L3 逐字节比对，现在真的跑。
    # 放在数据全部落盘之后，不影响采集与写盘；纯读文件。
    # 行为 2.2 GB 约 79 秒、妊娠 750 MB 约 24 秒，故给 --no-verify 可关。
    if use_pcap and run_l3:
        print("  -> L3 逐字节比对中（bin 2.2 GB 约 80 秒）...")
        rc, l3_note = run_l3_verify(session, fc["frame_bytes"])
        record("L3 bin≡pcap", rc == 0, l3_note)
    elif use_pcap:
        record("L3 bin≡pcap", None, "已跳过（--no-verify）")

    # RX 通道死活。四层判据全过也测不出某路 RX 恒零 —— 而角度估计要靠
    # 12 元虚拟阵列，少一路结果全错且不报错。抽样检测，约 1 秒。
    rc, rx_note = run_rx_check(session)
    record("RX 通道", rc == 0 if rc is not None else None, rx_note)

    fails = [c[0] for c in checks if c[1] is False]
    unknowns = [c[0] for c in checks if c[1] is None]
    if fails:
        verdict, reason = "BAD", "判据失败: " + ", ".join(fails)
    elif len(unknowns) == len(checks):
        verdict, reason = "UNKNOWN", "所有判据均无法判定"
    else:
        verdict = "GOOD"
        passed = [c[0] for c in checks if c[1] is True]
        reason = "通过: " + ", ".join(passed)
        if unknowns:
            reason += "；未判定: " + ", ".join(unknowns)

    print()
    if verdict == "BAD":
        bad = os.path.join(base, "BAD_" + name)
        os.rename(session, bad)
        session = bad
        print(f"  [BAD] {reason}")
        print("        已加 BAD_ 前缀（数据保留，未删除）")
        if notifier:
            notifier.emit("done_bad", "判据失败: " + ", ".join(fails))
    elif verdict == "UNKNOWN":
        print(f"  [UNKNOWN] {reason}")
        if notifier:
            notifier.emit("done_bad", "所有判据均无法判定")
    else:
        print(f"  [GOOD] {reason}")
        if notifier:
            notifier.emit("done_good", f"{fc['frames']} 帧 / {total:,} B")
    clean = False if verdict == "BAD" else (None if verdict == "UNKNOWN" else True)

    # UTF-8：第一阶段用 ASCII 是为了避开 Studio 输出窗口的 GBK 解码，
    # Linux 下没这个约束，用 ASCII 反而把中文判定结论写成问号
    with open(os.path.join(session, "capture_meta.txt"), "w",
              encoding="utf-8") as f:
        # ---- 总判定放最前：人一眼能看到，机器 grep "^verdict=" 即可 ----
        f.write("# ===== 判定 =====\n")
        f.write(f"verdict={verdict}\n")
        f.write(f"verdict_reason={reason}\n")
        f.write(f"checks_total={len(checks)}\n")
        f.write(f"checks_passed={sum(1 for c in checks if c[1] is True)}\n")
        f.write(f"checks_failed={sum(1 for c in checks if c[1] is False)}\n")
        f.write(f"checks_unknown={sum(1 for c in checks if c[1] is None)}\n")

        # 每项判据一行，机器可 grep "^check\." 逐项取值
        f.write("\n# ----- 逐项判据（PASS/FAIL/UNKNOWN + 说明）-----\n")
        slug = {"csv": "csv", "L0/L1/L2 pcap": "l0l1l2_pcap",
                "tcpdump 缓冲": "tcpdump_buffer",
                "内核 UDP drops": "kernel_udp_drops",
                "bin 总量": "bin_total", "L3 bin≡pcap": "l3_bytewise",
                "RX 通道": "rx_channels"}
        for cname, cok, cnote in checks:
            key = slug.get(cname, cname.replace(" ", "_"))
            state = {True: "PASS", False: "FAIL", None: "UNKNOWN"}[cok]
            f.write(f"check.{key}={state}\n")
            f.write(f"check.{key}.detail={cnote}\n")

        f.write("\n# ----- 采集参数 -----\n")
        f.write(f"cow_id={COW_ID}\nmode={CAPTURE_MODE}\nnote={NOTE}\n")
        f.write(f"cfg_file={os.path.basename(PROFILE_CFG)}\n")
        f.write(f"json_file={os.path.basename(JSON_CFG)}\n")
        f.write(f"frames={fc['frames']}\nperiod_ms={fc['period_ms']}\n")
        f.write(f"loops={fc['loops']}\nduration_sec={dur}\n")
        f.write(f"samples={fc['samples']}\ntx_count={fc['tx_count']}\n")
        f.write(f"rx_count={fc['rx_count']}\n")
        f.write(f"channel_cfg={fc['rx_mask']} {fc['tx_mask']} 0\n")
        f.write(f"frame_bytes={fc['frame_bytes']}\n")
        f.write(f"expect_total_bytes={expect_total}\n")
        f.write(f"data_rate_MBps={fc['frame_bytes'] / fc['period_ms'] / 1000:.3f}\n")
        f.write(f"start={t0:%Y-%m-%d %H:%M:%S}\nend={t1:%Y-%m-%d %H:%M:%S}\n")
        f.write(f"bin_total_bytes={total}\nbin_files={len(bins)}\n")
        f.write(f"packet_delay_us={jc['packet_delay_us']}\n")
        f.write(f"pcap={'yes' if use_pcap else 'no'}\n")
        f.write(f"host={os.uname().nodename}\n")

        f.write("\n# ----- 原始判据输出（排查用）-----\n")
        f.write(f"csv_verdict={detail}\n")
        if pcap_verdict:
            f.write(f"pcap_verdict={pcap_verdict}\n")
        f.write(f"kernel_udp_drops_delta={dropmon.delta}\n")
        if tcpdump_stats:
            f.write(f"tcpdump_stats={tcpdump_stats}\n")
        if errs:
            f.write(f"serial_errors={len(errs)}\n")
            for e in errs:
                f.write(f"  {e}\n")

    print(f"\n  数据: {session}")
    print("=" * 62)
    return clean is not False


def _remote_loop(args, jc, fc, run_l3):
    """遥控模式主循环：等按键 → 采一段 → 回到等待

    与 --loop N 的区别：不预设段数。采完（或中止后）回到 IDLE 继续等
    下一次按键，饲养员想采几段就采几段。

    Ctrl-C 的两重语义：
      等待按键时按 → 退出程序（本函数的 except 捕获）
      采集进行中按 → 中止当前这一段并归档，程序继续等下一次按键
                     （由 _run 的倒计时与 capture 的 except 处理）
    """
    if remote_control is None:
        sys.exit("[ERROR] --remote 需要 remote_control.py（与本脚本同目录）"
                 "和 python3-evdev\n"
                 "        安装： sudo apt install python3-evdev")

    # 语音提示：优先用 USB 喇叭（AudioNotifier），它内部会 fallback 到
    # LogNotifier，所以终端输出不会因为接了喇叭而消失。
    # --no-audio 只打日志不出声（夜里调试、或喇叭没插时用）。
    if args.no_audio:
        notifier = remote_control.LogNotifier()
    else:
        notifier = remote_control.AudioNotifier()
    try:
        rc = remote_control.RemoteController(notifier=notifier, grab=args.grab,
                                            state_file=args.state_file)
    except (RuntimeError, ValueError) as e:
        sys.exit(f"[ERROR] 遥控初始化失败: {e}")

    print("\n" + "=" * 62)
    print("  遥控模式")
    print(f"  开始 : 连按两次 [{remote_control.START_KEY.upper()}]"
          f"（{remote_control.DOUBLE_WINDOW:.0f} 秒内）")
    print(f"  停止 : [{remote_control.STOP_KEY.upper()}]（紧急中止，"
          "数据归档到 ABORT_ 目录、不跑判据）")
    print("  采集开始后开始键锁定，直到归档与校验全部结束才解锁")
    print("  Ctrl-C 退出程序")
    print("=" * 62)
    rc.start()

    n = 0
    try:
        while True:
            rc.set_state(remote_control.IDLE)
            # 让上一段的结束音效（done_good/done_bad/done_abort/error）
            # 有时间播完再发 ready —— 否则两个音频背靠背，done_* 会被吞掉
            # 或叠在一起。这个停顿也充当明确的段间分界，语义上合理。
            if n > 0:
                time.sleep(1.5)
            # 分段等待而不是无限阻塞 —— 让 Ctrl-C 能及时退出
            if not rc.wait_for_start(timeout=1.0):
                continue

            n += 1
            rc.set_state(remote_control.PREPARING)
            try:
                capture(n, jc, fc, not args.no_pcap, run_l3,
                        remote=rc, notifier=notifier)
            except KeyboardInterrupt:
                # 采集中的 Ctrl-C：capture 已经走完 abort_capture 归档，
                # 这里只是不让它冒泡到外层把程序也结束掉
                print("\n  本段已中止，回到待命状态")
            except Exception as e:
                notifier.emit("error", f"{type(e).__name__}: {e}")
                print(f"\n  [ERROR] 本段采集异常: {e}")
            print()
    except KeyboardInterrupt:
        print("\n  退出遥控模式")
    finally:
        rc.close()
        # 关掉播放线程，否则解释器要等它超时才退出
        if hasattr(notifier, "close"):
            notifier.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-pcap", action="store_true", help="不并行抓 pcap")
    ap.add_argument("--loop", type=int, default=1, help="连续采集段数")
    ap.add_argument("--yes", action="store_true", help="不等确认直接开始")
    ap.add_argument("--mode", choices=("behavior", "vitalsigns"),
                    help="选波形，默认 behavior。避免改常量被覆盖")
    ap.add_argument("--no-verify", action="store_true",
                    help="跳过 L3 逐字节比对（行为约 80 秒 / 妊娠约 24 秒）。"
                         "RX 通道检测不受影响，它只需约 1 秒")
    ap.add_argument("--verify", action="store_true",
                    help="强制开启 L3（覆盖 CAPTURE_L3 环境变量）")
    ap.add_argument("--remote", action="store_true",
                    help="遥控模式：用 2.4G 遥控器/键盘控制起停，"
                         "不预设段数，采完回到待命")
    ap.add_argument("--grab", action="store_true",
                    help="遥控模式下独占输入设备（按键不再进入系统，"
                         "终端里不回显）。封盒后建议开，调试时默认关")
    ap.add_argument("--no-audio", action="store_true",
                    help="遥控模式下不播语音提示，只打日志"
                         "（喇叭没插、或夜里调试时用）")
    ap.add_argument("--state-file", default=None,
                    help="把遥控状态机的当前状态写到该文件，供 "
                         "remote_daemon.py 判断能否退出。由守护进程传入，"
                         "手动运行时不必给")
    args = ap.parse_args()

    # 用 --mode 覆盖默认值。原先靠改 JSON_CFG/PROFILE_CFG 两个常量切换波形，
    # 那样改动会被下一次 scp 覆盖（2026-08-02 就这样丢过一次，导致对着
    # 记住了行为 chCfg 的雷达发妊娠 cfg，撞上 mmw_cli.c:288 的 debugAssert）。
    global JSON_CFG, PROFILE_CFG
    if args.mode:
        JSON_CFG = os.path.join(CFG_DIR, f"dca1000_{args.mode}.json")
        PROFILE_CFG = os.path.join(CFG_DIR, f"cow_{args.mode}.cfg")

    jc = load_json_cfg()
    fc = parse_cfg(PROFILE_CFG)
    check_consistency(jc, fc)

    dur = fc["frames"] * fc["period_ms"] // 1000
    print("=" * 62)
    print("  IWR6843 + DCA1000 采集（Linux）")
    print("=" * 62)
    print(f"  串口      : {RADAR_PORT} @ {RADAR_BAUDRATE}")
    print(f"  cfg       : {os.path.basename(PROFILE_CFG)}")
    print(f"  json      : {os.path.basename(JSON_CFG)}")
    print(f"  落盘      : {jc['base_path']}")
    print(f"  前缀      : {jc['prefix']}")
    print(f"  波形      : {fc['frames']} 帧 @ {fc['period_ms']} ms, "
          f"{fc['samples']} smp / {fc['tx_count']} TX / {fc['rx_count']} RX / "
          f"{fc['loops']} loops")
    print(f"  每帧/总量 : {fc['frame_bytes']:,} B / "
          f"{fc['frames'] * fc['frame_bytes']:,} B")
    print(f"  时长      : {dur} 秒 + {BUFFER_SEC} 秒缓冲")
    # L3 开关三种途径，优先级：--verify > --no-verify > CAPTURE_L3 环境变量 > 默认开
    # 环境变量让"整轮连续采集都不验"只需设一次，不必每条命令都加参数：
    #   export CAPTURE_L3=0     后续所有采集都跳过 L3
    env_l3 = os.environ.get("CAPTURE_L3", "").strip().lower()
    if args.verify:
        run_l3, l3_src = True, "--verify"
    elif args.no_verify:
        run_l3, l3_src = False, "--no-verify"
    elif env_l3 in ("0", "no", "off", "false"):
        run_l3, l3_src = False, "CAPTURE_L3 环境变量"
    else:
        run_l3, l3_src = True, "默认"

    est = fc["frames"] * fc["frame_bytes"] / 28e6      # 实测约 28 MB/s
    print(f"  pcap      : {'否' if args.no_pcap else '是'}")
    print(f"  L3 比对   : {'开' if run_l3 else '关'}（{l3_src}）"
          + (f"，预计约 {est:.0f} 秒" if run_l3 and not args.no_pcap else ""))
    print(f"  牛号/模式 : {COW_ID} / {CAPTURE_MODE}")
    print(f"  遥控      : {'开' if args.remote else '关'}"
          + ("（独占输入设备）" if args.remote and args.grab else "")
          + ("（无语音）" if args.remote and args.no_audio else ""))
    print("=" * 62)

    # 遥控模式不预设段数，由按键驱动，故与 --loop / --yes 都不相干
    if args.remote:
        if args.loop != 1:
            print("  [提示] --remote 下 --loop 无效 —— 不预设段数，"
                  "采完回到待命等下一次按键")
        return _remote_loop(args, jc, fc, run_l3)

    if not args.yes:
        if input("\n开始？(Enter 继续 / q 退出) ").strip().lower() == "q":
            return

    for i in range(1, args.loop + 1):
        print()
        capture(i, jc, fc, not args.no_pcap, run_l3)
        if i < args.loop:
            time.sleep(3)


if __name__ == "__main__":
    main()
