#!/usr/bin/env python3
"""遥控模块的设备识别自检 —— 不碰雷达、不碰 DCA1000，纯读设备清单

用途：确认接收器插上后，哪些 event 节点能报出开始键与停止键。
遥控器到货、或 dongle 拔插后 event 号变了，跑这个确认一遍。

    python3 _test_remote_devices.py
"""

import sys

import evdev
from evdev import ecodes

import remote_control as rc


def main():
    ctl = rc.RemoteController()
    devs = ctl.find_devices()

    print("=" * 66)
    print("  遥控输入设备自检")
    print("=" * 66)
    print(f"  开始键 KEY_{rc.START_KEY.upper()}   "
          f"停止键 KEY_{rc.STOP_KEY.upper()}")
    print()

    print("全部输入设备：")
    for path in sorted(evdev.list_devices()):
        try:
            d = evdev.InputDevice(path)
        except OSError as e:
            print(f"   {path:22s} 打不开: {e}")
            continue
        caps = d.capabilities().get(ecodes.EV_KEY, [])
        has_s = ctl.start_code in caps
        has_e = ctl.stop_code in caps
        mark = "  <== 会被监听" if (has_s and has_e) else ""
        print(f"   {path:22s} S={int(has_s)} E={int(has_e)} "
              f"keys={len(caps):4d}  {d.name}{mark}")
        d.close()

    print()
    print(f"结论：{len(devs)} 个设备能同时报出两个键")
    for path, d in sorted(devs.items()):
        print(f"   {path}  ->  {d.name}")
        d.close()

    if not devs:
        print()
        print("  没有找到可用设备。检查：")
        print("   1. 接收器插上了吗？ lsusb 应能看到 dongle")
        print("   2. 当前用户在 input 组吗？ id  应含 (input)")
        print("   3. 遥控器的键与 START_KEY/STOP_KEY 对得上吗？")
        print("      不确定的话跑： python3 -m evdev.evtest  按几下看键名")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
