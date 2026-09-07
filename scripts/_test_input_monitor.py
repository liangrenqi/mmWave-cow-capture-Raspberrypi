#!/usr/bin/env python3
"""全设备按键监视器 —— 排查"按了键但脚本收不到"

与 _test_remote_keys.py 的区别：**不做任何过滤**。
监听全部 /dev/input/event*，打印任何设备上的任何按键。
用来区分两件事：

    收得到事件 → 键盘链路正常，问题在按键映射或去抖参数
    收不到事件 → 按键根本没经过 Pi 的物理输入设备

后者最常见的原因不是故障，而是**按错了键盘**：
从 Windows 用 SSH 连上来、在本机键盘上打字，字符是走 SSH 通道进到
Pi 的 shell 的，终端会回显，但 Pi 的输入子系统里什么都没发生。
VNC 同理（XTEST 注入，不产生 evdev 事件）。
evdev 读的是内核输入子系统，所以**必须按插在 Pi 上的那个无线键盘**。

用法：
    python3 _test_input_monitor.py            # 监听 30 秒
    python3 _test_input_monitor.py 60         # 监听 60 秒
"""

import selectors
import sys
import time

import evdev
from evdev import ecodes


def main():
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0

    paths = sorted(evdev.list_devices())
    if not paths:
        print("没有任何 /dev/input/event* 可读。")
        print("检查当前用户是否在 input 组： id")
        return 1

    devs = {}
    print("=" * 70)
    print("  全设备按键监视器")
    print("=" * 70)
    print("  打开的设备：")
    for p in paths:
        try:
            d = evdev.InputDevice(p)
        except OSError as e:
            print(f"     {p:22s} 打不开: {e}")
            continue
        devs[p] = d
        nkeys = len(d.capabilities().get(ecodes.EV_KEY, []))
        print(f"     {p:22s} keys={nkeys:4d}  {d.name}")

    if not devs:
        return 1

    print()
    print(f"  现在开始监听 {duration:.0f} 秒。")
    print()
    print("  ★ 请【逐个】按遥控器上的按钮，每按一个停一下，")
    print("    并记住你按的顺序 —— 末尾会按顺序列出对应的键名。")
    print()
    print("  注意：HID 描述符声明的\"能报哪些键\"是能力上限，不等于实际发什么码。")
    print("  演示器类遥控常发 PAGEUP / PAGEDOWN / F5 / ESC / DOT，而不是字母键。")
    print("  所以必须实测，不能照 capabilities 猜。")
    print()
    print("  Ctrl-C 可提前结束。")
    print("=" * 70)
    print()

    sel = selectors.DefaultSelector()
    for d in devs.values():
        try:
            sel.register(d, selectors.EVENT_READ)
        except (OSError, ValueError):
            pass

    t0 = time.time()
    total = 0
    per_dev = {}
    seen_keys = {}          # 键名 -> 按下次数
    press_order = []        # 首次按下的顺序，用来对应"我按的第几个按钮"
    try:
        while time.time() - t0 < duration:
            for key, _ in sel.select(timeout=0.5):
                dev = key.fileobj
                try:
                    events = list(dev.read())
                except OSError:
                    continue
                for ev in events:
                    if ev.type != ecodes.EV_KEY:
                        continue
                    if ev.value not in (0, 1, 2):
                        continue
                    total += 1
                    node = dev.path.replace("/dev/input/", "")
                    per_dev[node] = per_dev.get(node, 0) + 1
                    kind = {0: "抬起", 1: "按下", 2: "重复"}[ev.value]
                    keyname = ecodes.KEY.get(ev.code) or ecodes.BTN.get(ev.code) \
                        or f"code={ev.code}"
                    # 一个键码可能有多个别名，evdev 此时返回 list 或 **tuple**
                    # （只判 list 会在 tuple 上抛 TypeError，实测踩到过）
                    if isinstance(keyname, (list, tuple)):
                        keyname = keyname[0]
                    keyname = str(keyname)
                    seen_keys.setdefault(keyname, 0)
                    if ev.value == 1:
                        seen_keys[keyname] += 1
                        if keyname not in press_order:
                            press_order.append(keyname)
                    t = time.time() - t0
                    print(f"  {t:7.3f}s  {node:8s} {keyname:16s} {kind}"
                          f"   (code={ev.code})   {dev.name}")
    except KeyboardInterrupt:
        print("\n  (提前结束)")
    finally:
        sel.close()
        for d in devs.values():
            try:
                d.close()
            except OSError:
                pass

    print()
    print("=" * 70)
    if total == 0:
        print("  收到 0 个按键事件。")
        print()
        print("  按可能性排序：")
        print("   1. **按的不是 Pi 上那个无线键盘** —— 若你是 SSH/VNC 进来的，")
        print("      在本机键盘上打字不会产生 Pi 的 evdev 事件（终端照样回显）。")
        print("      这是最常见的原因，也不是故障。")
        print("   2. 无线键盘没电 / 没开开关 / 与 dongle 掉了配对。")
        print("      换个位置按几下，或重新插拔 dongle。")
        print("   3. dongle 松动 —— lsusb 看看还在不在。")
    else:
        print(f"  收到 {total} 个按键事件，来自 {len(per_dev)} 个节点：")
        for node, n in sorted(per_dev.items()):
            print(f"     {node:8s} {n:5d} 个")
        if len(per_dev) > 1:
            print()
            print("  ⚠ 多个节点都在报按键 —— 同一次物理按下会被记多次。")
            print("    remote_control 的 MIN_GAP 去抖就是为这个准备的，")
            print("    具体间隔够不够，跑 _test_remote_keys.py 看汇总。")

        if press_order:
            print()
            print("  按下过的键（按首次出现顺序，即你按按钮的顺序）：")
            for i, k in enumerate(press_order, 1):
                print(f"     {i}. {k:24s} 共 {seen_keys[k]} 次")
            print()
            print("  把这份清单和你按的物理按钮对应起来，就能定 START_KEY /")
            print("  STOP_KEY。填进 remote_control.py 时去掉 KEY_ 前缀并小写，")
            print("  例如 KEY_PAGEDOWN -> \"pagedown\"。")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
