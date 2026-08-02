#!/usr/bin/env python3
"""雷达串口存活探针 —— 只读，不改任何状态

用途：在下发 cfg 之前确认雷达真的在应答。
判据：`version` 应回 "Platform : xWR68xx ... Done"。
0 字节 = 固件没在跑或 UART 不通，此时任何 cfg 下发结果都不可信。

顺带打印 USB runtime power 状态：若 ttyACM 的父设备被 autosuspend
挂起，串口会静默但 USB 仍在枚举列表里 —— 与"固件死了"现象相同、
成因完全不同（前者重启 Pi 或写 power/control=on 即恢复，后者要断电）。
"""

import os
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("[ERROR] 需要 pyserial")

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"
SYSDEV = "/sys/bus/usb/devices/3-1"


def usb_power_state():
    out = []
    for rel in ("power/control", "power/runtime_status",
                "power/autosuspend_delay_ms", "power/runtime_suspended_time"):
        p = os.path.join(SYSDEV, rel)
        try:
            with open(p) as f:
                out.append(f"{rel.split('/')[-1]}={f.read().strip()}")
        except OSError:
            pass
    for iface in ("3-1:1.0", "3-1:1.3"):
        p = f"/sys/bus/usb/devices/{iface}/power/runtime_status"
        try:
            with open(p) as f:
                out.append(f"{iface}={f.read().strip()}")
        except OSError:
            pass
    return "  ".join(out)


def main():
    print(f"USB power : {usb_power_state()}")
    try:
        s = serial.Serial(PORT, 115200, timeout=2)
    except serial.SerialException as e:
        print(f"[FAIL] 打不开 {PORT}: {e}")
        return 2

    # 先看陈旧缓冲：雷达崩溃前打的东西可能还在
    time.sleep(0.5)
    if s.in_waiting:
        stale = s.read(s.in_waiting).decode(errors="replace")
        n = len([l for l in stale.splitlines() if l.strip()])
        print(f"陈旧缓冲  : {n} 行（上电横幅或崩溃信息）")
        for line in (l.strip() for l in stale.splitlines()):
            if line:
                print(f"            <- {line}")

    s.reset_input_buffer()
    s.write(b"version\n")

    # 等到出现 Done 或超时
    lines, t0 = [], time.time()
    while time.time() - t0 < 4.0:
        if s.in_waiting:
            for l in (x.strip() for x in
                      s.read(s.in_waiting).decode(errors="replace").splitlines()):
                if l:
                    lines.append(l)
        elif any(l.lower().startswith("done") for l in lines):
            break
        else:
            time.sleep(0.1)
    s.close()

    print(f"version   : {len(lines)} 行")
    for l in lines:
        print(f"            <- {l}")

    alive = any(l.lower().startswith("done") for l in lines)
    print("=" * 56)
    if alive:
        print(">>> 雷达在应答，可以下发 cfg <<<")
        return 0
    print("!!! 雷达无应答 —— 此时下发 cfg 的结果不可信")
    print("    区分两种成因：")
    print("      USB runtime_status=suspended → autosuspend 问题，")
    print("        sudo sh -c 'echo on > /sys/bus/usb/devices/3-1/power/control'")
    print("      USB 正常但仍静默 → 固件侧，需拔 5V 断电重启")
    return 1


if __name__ == "__main__":
    sys.exit(main())
