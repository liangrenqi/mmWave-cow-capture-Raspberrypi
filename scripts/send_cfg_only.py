#!/usr/bin/env python3
"""阶段 5.5：只下发 cfg，验证 sensorStart 能否通过

**完全不碰 DCA1000** —— 本测试只关心雷达芯片是否接受这份配置。
串口逐行发 cfg，看 sensorStart 回 Done 还是 Error，然后立刻 sensorStop。

为什么行为 cfg 可能失败（源码依据，SDK 03.06.02.00）：
  mmw demo 在 sensorStart 时要为点云检测流水线在**雷达片上 L3 RAM**
  分配两块内存，与 DCA1000 的缓冲无关：
    radarCube = numRangeBins × numDopplerChirps × numVirtualAnt × 4 B
                datapath/dpc/objectdetection/objdetdsp/src/objectdetection.c:1793
    detMatrix = numRangeBins × numDopplerBins × 2 B
                同上 :1826
  L3 总量 768 KB (0xC0000)，见 xwr68xx_mmw_demo_dss.map:19 / _mss.map:18
  两块的对齐都是 2 字节（cfarcaprocdsp.h:77 + objectdetection.c:99/108），
  所以下面的算式是精确的，没有隐藏填充。
  硬约束：numDopplerChirps 必须是 4 的倍数
          datapath/dpc/dpu/dopplerproc/src/dopplerprocdsp.c:703

预期报错路径（同样是读源码确认的）：
  MSS 侧 objdetrangehwa 先从 L3 池分配 radarCube；128 loops 时它正好占满
  768 KB，于是传给 DSP 的剩余池大小为 0（mss_main.c:2288）
  → DSP 侧 detMatrix 分配失败 → ENOMEM__L3_RAM_DET_MATRIX
  → MmwDemo_dataPathConfig 返回负值
  → MmwDemo_configSensor 返回（mss_main.c:3544）
  → CLISensorStart 返回 -1
  → CLI 框架打印 "Error -1" 到**串口**（utils/cli/src/cli.c:203）
  精确的 heap 用量 printf 走 CCS 的 JTAG 控制台，串口上看不到。

⚠ 陷阱：sensorStart 失败后 sensorState 停在 OPENED。此后若下发
  **channelCfg 不同**的配置（例如回到妊娠的 `channelCfg 15 1 0`），
  mmw_cli.c 里 CLISensorStart 的 memcmp 会不匹配并触发 debugAssert，
  串口打 "Exception: ..."，且必须**重启雷达**才能恢复
  （源码注释原话 "the board needs to be reboot"）。
  同一份 channelCfg 反复重试是安全的。

用法：
    python3 send_cfg_only.py --cfg cow_behavior.cfg --dry-run       # 只算不发
    python3 send_cfg_only.py --cfg cow_behavior.cfg                 # 试验 1：原样
    python3 send_cfg_only.py --cfg cow_behavior.cfg --set-loops 120 # 试验 2
--set-loops 只改内存里的那一行，**不写回文件**。
"""

import argparse
import os
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("[ERROR] 需要 pyserial: pip3 install pyserial")

RADAR_PORT = "/dev/ttyACM0"
RADAR_BAUDRATE = 115200
L3_TOTAL = 0xC0000              # 786,432 B


def pow2roundup(x):
    """mathutils.c:119 的等价实现"""
    p = 1
    while p < x:
        p *= 2
    return p


def parse_cfg(lines):
    """从 cfg 文本行解析出算 L3 需要的字段"""
    out = {"tx_count": 0}
    for line in lines:
        p = line.split()
        if not p:
            continue
        if p[0] == "frameCfg":
            # frameCfg <chirpStart> <chirpEnd> <loops> <frames> <period_ms> ...
            out.update(loops=int(p[3]), frames=int(p[4]), period_ms=float(p[5]))
        elif p[0] == "profileCfg":
            out["samples"] = int(p[10])
        elif p[0] == "channelCfg":
            out["rx_count"] = bin(int(p[1])).count("1")
            out["tx_mask"] = int(p[2])
        elif p[0] == "chirpCfg":
            out["tx_count"] += 1
    return out


def predict_l3(fc):
    """按 mmw demo 的分配逻辑算 L3 占用。返回 (dict, 是否放得下, 违规原因列表)"""
    n_tx = max(fc["tx_count"], 1)
    n_rx = fc["rx_count"]
    loops = fc["loops"]

    rb = pow2roundup(fc["samples"])
    dop_chirps = (n_tx * loops) // n_tx          # == loops
    dop_bins = max(pow2roundup(dop_chirps), 16)  # mss_main.c:2099 把下限钳到 16
    n_va = n_tx * n_rx

    cube = rb * dop_chirps * n_va * 4            # cmplx16ReIm_t = 4 B
    det = rb * dop_bins * 2                      # uint16_t
    total = cube + det

    problems = []
    if total > L3_TOTAL:
        problems.append(f"L3 超限 {total - L3_TOTAL:,} B（需 {total:,} / 有 {L3_TOTAL:,}）")
    if dop_chirps % 4 != 0:
        problems.append(f"numDopplerChirps={dop_chirps} 不是 4 的倍数"
                        "（dopplerprocdsp.c:703 会拒绝）")

    info = dict(numRangeBins=rb, numDopplerChirps=dop_chirps,
                numDopplerBins=dop_bins, numVirtualAnt=n_va,
                cube=cube, det=det, total=total,
                frame_bytes=fc["samples"] * n_rx * 2 * 2 * loops * n_tx)
    return info, not problems, problems


def report_prediction(fc, info, ok, problems):
    print("=" * 62)
    print(f"配置        : {fc['samples']} samples / {fc['tx_count']} TX / "
          f"{fc['rx_count']} RX / {fc['loops']} loops / "
          f"{fc['frames']} 帧 @ {fc['period_ms']:.0f} ms")
    print(f"派生        : numRangeBins={info['numRangeBins']}  "
          f"numDopplerChirps={info['numDopplerChirps']}  "
          f"numDopplerBins={info['numDopplerBins']}  "
          f"虚拟天线={info['numVirtualAnt']}")
    print("-" * 62)
    print(f"radarCube   : {info['cube']:>9,} B")
    print(f"detMatrix   : {info['det']:>9,} B")
    print(f"合计        : {info['total']:>9,} B   /  L3 可用 {L3_TOTAL:,} B "
          f"({100.0 * info['total'] / L3_TOTAL:.1f}%)")
    print(f"每帧数据量  : {info['frame_bytes']:>9,} B   "
          f"码率 {info['frame_bytes'] / (fc['period_ms'] / 1000.0) / 1e6:.2f} MB/s")
    print("-" * 62)
    if ok:
        print(f"预测        : sensorStart 应当通过（L3 余 "
              f"{L3_TOTAL - info['total']:,} B）")
    else:
        print("预测        : sensorStart 应当失败")
        for p in problems:
            print(f"              - {p}")
        print("              串口预期回 'Error -1'（cli.c:203）")
    print("=" * 62)


def drain(ser, first_wait, quiet_gap=0.4):
    """读完一条命令的回显。还有数据就继续等，避免截断多行响应。"""
    time.sleep(first_wait)
    lines, deadline = [], time.time() + quiet_gap
    while time.time() < deadline:
        if ser.in_waiting:
            chunk = ser.read(ser.in_waiting).decode(errors="replace")
            lines.extend(l.strip() for l in chunk.splitlines() if l.strip())
            deadline = time.time() + quiet_gap
        else:
            time.sleep(0.05)
    return lines


def drain_until_verdict(ser, timeout):
    """等 sensorStart 的最终裁决：Done / Error / 超时无响应

    sensorStart 不像别的命令那样立刻返回：它要真的分配 L3 内存、配 CBUFF、
    启动硬件流水线。而且失败时**会死锁**（实测 + 源码确认）：
        MSS  MmwDemo_DPM_ioctl_blocking → Semaphore_pend(BIOS_WAIT_FOREVER)
             mss_main.c:1735            ← 无超时
        DSS  DPC 里 detMatrix 分配失败 → 返回 ENOMEM
        MSS  reportFxn 只在 DPM_Report_IOCTL 分支 post 信号量（:2631），
             DPC 报错不走那个分支 ⇒ 信号量永远没人 post
    所以 CLI 任务永久卡住，"Error -1" 那行代码根本执行不到。
    必须靠超时来判定，且判定后**雷达需断电重启**才能再用。
    """
    lines, t0 = [], time.time()
    while time.time() - t0 < timeout:
        if ser.in_waiting:
            chunk = ser.read(ser.in_waiting).decode(errors="replace")
            for l in (x.strip() for x in chunk.splitlines()):
                if not l:
                    continue
                lines.append(l)
                low = l.lower()
                if low.startswith("done"):
                    return lines, "done"
                if low.startswith("error") or "exception:" in low:
                    return lines, "error"
        else:
            time.sleep(0.1)
    return lines, "timeout"


def is_real_error(line, cmd):
    """判断这行回显是不是"我们刚发的这条命令"出错了

    与 capture_linux.py 同一套判据：'?`???' is not recognized 报的是缓冲
    噪声；Debug/calibration status 是正常启动信息；Ignored 是提示。
    """
    low = line.lower()
    if "exception:" in low:
        return True
    if "not recognized" in low:
        q = line.split("'")
        subject = q[1].strip() if len(q) >= 2 else ""
        return subject in (cmd, cmd.split()[0])
    if "error" in low:
        if low.startswith("debug:") or "calibration status" in low:
            return False
        return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", required=True)
    ap.add_argument("--port", default=RADAR_PORT)
    ap.add_argument("--set-loops", type=int,
                    help="改内存里 frameCfg 的 loops，不写回文件")
    ap.add_argument("--dry-run", action="store_true", help="只算 L3，不发串口")
    ap.add_argument("--start-timeout", type=float, default=10.0,
                    help="等 sensorStart 裁决的秒数，默认 10（失败时会死锁）")
    ap.add_argument("--keep-running", action="store_true",
                    help="成功后不发 sensorStop（默认发，避免雷达一直流数据）")
    args = ap.parse_args()

    # 相对路径先找 ../config/（仓库布局），再找脚本同目录（扁平布局）
    if os.path.isabs(args.cfg):
        path = args.cfg
    else:
        _here = os.path.dirname(os.path.abspath(__file__))
        for _d in (os.path.join(os.path.dirname(_here), "config"), _here, "."):
            _p = os.path.join(_d, args.cfg)
            if os.path.isfile(_p):
                path = _p
                break
        else:
            sys.exit(f"[ERROR] 找不到 {args.cfg}")
    with open(path, encoding="utf-8") as f:
        cmds = [ln.strip() for ln in f
                if ln.strip() and not ln.strip().startswith("%")]

    if args.set_loops is not None:
        for i, c in enumerate(cmds):
            if c.startswith("frameCfg"):
                p = c.split()
                old = p[3]
                p[3] = str(args.set_loops)
                cmds[i] = " ".join(p)
                print(f"[改] frameCfg loops {old} -> {args.set_loops}（仅内存）")
                print(f"     {cmds[i]}")
                break
        else:
            sys.exit("[ERROR] cfg 里没有 frameCfg 行")

    fc = parse_cfg(cmds)
    missing = [k for k in ("samples", "loops", "rx_count", "frames") if k not in fc]
    if missing:
        sys.exit(f"[ERROR] cfg 缺少字段: {missing}")

    info, ok, problems = predict_l3(fc)
    report_prediction(fc, info, ok, problems)

    if args.dry_run:
        return 0

    print(f"\n打开 {args.port} @ {RADAR_BAUDRATE} ...", end=" ", flush=True)
    try:
        ser = serial.Serial(port=args.port, baudrate=RADAR_BAUDRATE,
                            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                            stopbits=serial.STOPBITS_ONE, timeout=2)
    except serial.SerialException as e:
        print(f"[ERROR] {e}")
        return 2
    print("[OK]")

    # 清掉上电启动横幅和残留，否则会被当成第一条命令的假报错
    ser.reset_input_buffer()
    time.sleep(0.3)
    if ser.in_waiting:
        junk = ser.read(ser.in_waiting).decode(errors="replace")
        n = len(junk.strip().splitlines())
        if n:
            print(f"丢弃开口前 {n} 行积压内容（启动横幅/噪声）")
        ser.reset_input_buffer()

    critical = {"sensorStop", "flushCfg", "sensorStart"}
    errors, started = [], False
    start_state = None          # done / error / timeout
    replies_seen = 0            # 前 30 条里有多少条真的回了话

    print(f"\n发送 {len(cmds)} 条命令")
    for i, cmd in enumerate(cmds, 1):
        ser.write((cmd + "\n").encode())
        label = cmd if len(cmd) <= 46 else cmd[:43] + "..."
        print(f"  [{i:2d}/{len(cmds)}] {label}")

        if cmd == "sensorStart":
            lines, start_state = drain_until_verdict(ser, args.start_timeout)
            for line in lines:
                print(f"           <- {line}")
                if is_real_error(line, cmd):
                    errors.append(f"{cmd} -> {line}")
            started = (start_state == "done")
            if start_state == "timeout":
                print(f"           [超时] {args.start_timeout:.0f} 秒内既无 Done "
                      "也无 Error")
        else:
            lines = drain(ser, 0.5 if cmd in critical else 0.05)
            if lines:
                replies_seen += 1
            for line in lines:
                print(f"           <- {line}")
                if is_real_error(line, cmd):
                    errors.append(f"{cmd} -> {line}")

    n_pre = len(cmds) - 1
    print("\n" + "=" * 62)
    print(f"前 {n_pre} 条回显   : {replies_seen}/{n_pre} 条有响应")

    # 三态判定。只有"雷达确实在应答"时，sensorStart 的结果才构成 L3 判据。
    if replies_seen == 0:
        print("结果        : 串口不通 —— 雷达一条都没回")
        print("              **本次测不到 L3**，与配置无关。"
              "检查 SOP 跳线(mode 4 = 001)、5V 供电、断电重启")
        verdict, conclusive = 2, False
    elif started:
        print("结果        : sensorStart 通过（收到 Done）")
        verdict, conclusive = 0, True
    elif start_state == "error":
        print("结果        : sensorStart 被明确拒绝（回了 Error）")
        verdict, conclusive = 1, True
    else:
        print("结果        : sensorStart 无响应 —— 挂死")
        print("              这是 L3 超限的**预期表现**，不是串口问题：")
        print("              前面的命令都正常回话，只有 sensorStart 卡住。")
        print("              机制：DPC 分配失败 → 信号量无人 post →")
        print("              MSS 卡在 Semaphore_pend(BIOS_WAIT_FOREVER)")
        print("              （mss_main.c:1735 / :2631）")
        print("              ⚠ 雷达现在处于死锁，**再测之前必须断电重启**")
        verdict, conclusive = 1, True

    if conclusive:
        # 预测的是"能否通过"，实测的是 started；只有判据成立时才比较
        match = (started == ok)
        print(f"预测符合    : {'是' if match else '否 ← 需重新分析'}"
              f"   (预测 {'通过' if ok else '失败'} / "
              f"实测 {'通过' if started else '失败'})")
    else:
        print("预测符合    : 无法判定（串口不通，本次结果不能用于验证 L3）")
    print("=" * 62)

    if started and not args.keep_running:
        print("\n发 sensorStop 收尾（DCA1000 未在录，无数据落盘）")
        ser.write(b"sensorStop\n")
        for line in drain(ser, 1.0):
            print(f"  <- {line}")

    ser.close()

    if not started:
        print("\n⚠ 下次下发前请给雷达断电重启（拔 5V，等 5 秒，插回）。")
        if start_state == "timeout" and replies_seen:
            print("  原因一：CLI 任务卡在 Semaphore_pend，"
                  "此后任何命令都不会被处理（含 sensorStop）。")
        print("  原因二：sensorState 停在 OPENED，若下发 channelCfg 不同的配置"
              "（如妊娠的 15 1 0），\n"
              "          CLISensorStart 的 memcmp 会触发 debugAssert。"
              "同一份 channelCfg 重试是安全的。")
    return verdict


if __name__ == "__main__":
    sys.exit(main())
