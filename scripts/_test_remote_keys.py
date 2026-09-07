#!/usr/bin/env python3
"""按键原始事件记录 —— 验证去抖与双击判定是否真的成立

**为什么必须做这个测试**：实测这块 2.4G dongle 有两个节点都能报字母键
（event5 "Wireless 2.4G Dongle" 与 event9 "... Keyboard"）。若一次物理
按下被两个节点各报一次，而两次上报的间隔**大于** remote_control.MIN_GAP
（150 ms），那么"按一次"会被判成"双击" —— 开始键的防误触就完全失效了，
而且这种失效是静默的：看起来"按一下就开始采集了，挺好用"，实际上口袋里
压一下也会开采。

所以这里直接打出每个事件的**来源节点与相对时刻**，并同步显示
remote_control 的判定结果，两者对照。

用法（在 Pi 上跑，然后按遥控器/无线键盘上的键）：

    python3 _test_remote_keys.py

注意：evdev 不依赖终端焦点，所以从 Windows 用 SSH 跑这个、在 Pi 上按
无线键盘，事件照样收得到 —— 这正是封盒后需要的性质，顺带一并验证了。
Ctrl-C 结束并打印汇总。
"""

import selectors
import sys
import time
from collections import defaultdict

import evdev
from evdev import ecodes

import remote_control as rc


def main():
    ctl = rc.RemoteController()
    devs = ctl.find_devices()
    if not devs:
        print("没有找到能报出 S/E 的设备，先跑 _test_remote_devices.py")
        return 1

    print("=" * 70)
    print("  按键原始事件记录")
    print("=" * 70)
    print(f"  监听 {len(devs)} 个节点：")
    for p, d in sorted(devs.items()):
        print(f"     {p}  {d.name}")
    print()
    print(f"  去抖窗口 MIN_GAP      = {rc.MIN_GAP * 1000:.0f} ms")
    print(f"  双击窗口 DOUBLE_WINDOW = {rc.DOUBLE_WINDOW:.1f} s")
    print()
    print("  请依次测试，每步之间停一下：")
    print("     1. 单按一次 S      —— 应只出现 [armed]，**不应**触发开始")
    print("     2. 快速连按两次 S  —— 应出现 [armed] 然后 [双击成立]")
    print("     3. 按住 S 不放两秒 —— 长按重复(value=2)应被全部忽略")
    print("     4. 单按一次 E      —— 应出现 [停止键]")
    print()
    print("  Ctrl-C 结束并打印汇总")
    print("=" * 70)
    print()

    sel = selectors.DefaultSelector()
    for d in devs.values():
        sel.register(d, selectors.EVENT_READ)

    t_start = time.time()
    # 统计：每个键的原始按下次数、被去抖丢掉的次数、跨节点重复的间隔
    raw_counts = defaultdict(int)
    dropped = defaultdict(int)
    gaps = []
    last_by_code = {}
    last_node = {}
    released_since = {}
    first_press_at = None
    doubles = 0
    stops = 0

    try:
        while True:
            for key, _ in sel.select(timeout=0.5):
                dev = key.fileobj
                node = dev.path.replace("/dev/input/", "")
                try:
                    events = list(dev.read())
                except OSError:
                    continue
                for ev in events:
                    if ev.type != ecodes.EV_KEY:
                        continue
                    if ev.code not in (ctl.start_code, ctl.stop_code):
                        continue

                    name = "S" if ev.code == ctl.start_code else "E"
                    t = time.time() - t_start
                    kind = {0: "抬起", 1: "按下", 2: "长按重复"}.get(ev.value,
                                                                  str(ev.value))

                    if ev.value != 1:
                        # 只打不算 —— value=2 被忽略正是设计要点之一。
                        # value==0（抬起）要记下来：两次按下之间有没有抬起，
                        # 是区分"人为连按"与"跨节点重复"的决定性证据。
                        if ev.value == 0:
                            released_since[ev.code] = True
                        print(f"  {t:7.3f}s  {node:8s} {name}  {kind}"
                              + ("   <- 已忽略（长按重复）" if ev.value == 2 else ""))
                        continue

                    raw_counts[name] += 1
                    note = ""

                    # 复刻 remote_control._on_key_down 的去抖判定
                    last = last_by_code.get(ev.code)
                    if last is not None:
                        gap = t - last
                        same_node = (last_node.get(ev.code) == node)
                        released = released_since.get(ev.code, False)
                        if gap < rc.MIN_GAP:
                            dropped[name] += 1
                            gaps.append((name, gap, node, "被去抖丢弃",
                                         same_node, released))
                            print(f"  {t:7.3f}s  {node:8s} {name}  按下"
                                  f"   <- 距上次 {gap * 1000:.0f} ms，"
                                  f"小于 {rc.MIN_GAP * 1000:.0f} ms，"
                                  "**已去抖丢弃**")
                            continue
                        gaps.append((name, gap, node, "已接受",
                                     same_node, released))
                        note = f"（距上次 {gap * 1000:.0f} ms）"
                    last_by_code[ev.code] = t
                    last_node[ev.code] = node
                    released_since[ev.code] = False

                    if ev.code == ctl.stop_code:
                        stops += 1
                        print(f"  {t:7.3f}s  {node:8s} E  按下 {note}"
                              "   <- [停止键] 会触发中止")
                        continue

                    # 开始键的双击判定
                    if first_press_at is not None and \
                            (t - first_press_at) <= rc.DOUBLE_WINDOW:
                        doubles += 1
                        dt = t - first_press_at
                        first_press_at = None
                        print(f"  {t:7.3f}s  {node:8s} S  按下 {note}"
                              f"   <- [双击成立] 间隔 {dt * 1000:.0f} ms，"
                              "会开始采集")
                    else:
                        first_press_at = t
                        print(f"  {t:7.3f}s  {node:8s} S  按下 {note}"
                              f"   <- [armed] 等第二次（{rc.DOUBLE_WINDOW:.0f} s 内）")
    except KeyboardInterrupt:
        pass
    finally:
        sel.close()
        for d in devs.values():
            try:
                d.close()
            except OSError:
                pass

    print()
    print("=" * 70)
    print("  汇总")
    print("=" * 70)
    for name in ("S", "E"):
        if raw_counts[name]:
            print(f"  {name}: 原始按下 {raw_counts[name]} 次，"
                  f"其中 {dropped[name]} 次被去抖丢弃，"
                  f"有效 {raw_counts[name] - dropped[name]} 次")
    print(f"  双击成立 {doubles} 次；停止键 {stops} 次")

    if gaps:
        print()
        print("  相邻同键按下的间隔：")
        print("  （判断依据不能只看间隔 —— 还要看是否同一节点、中间有没有抬起。")
        print("    跨节点重复的特征是：不同节点 + 中间没有抬起 + 间隔在毫秒级）")
        suspects = []
        for name, g, node, verdict, same_node, released in gaps:
            bits = []
            bits.append("同节点" if same_node else "**跨节点**")
            bits.append("中间有抬起" if released else "**中间无抬起**")
            # 真正可疑的只有"跨节点 + 无抬起"，光是间隔短不算
            suspicious = (not same_node) and (not released)
            if suspicious and verdict == "已接受":
                suspects.append((name, g, node))
            flag = "  ⚠ 疑似跨节点重复" if suspicious else ""
            print(f"     {name}  {g * 1000:7.0f} ms  {node:8s} {verdict}"
                  f"  [{', '.join(bits)}]{flag}")

        human = [g for _, g, _, v, sn, rel in gaps
                 if v == "已接受" and sn and rel]
        print()
        if suspects:
            print("  ⚠ 检测到疑似跨节点重复（不同节点、中间无抬起）：")
            for name, g, node in suspects:
                print(f"     {name} 在 {node} 上距上次仅 {g * 1000:.0f} ms")
            print(f"    把 MIN_GAP 调到该间隔之上，或只监听一个节点。")
        else:
            print("  ✓ 没有跨节点重复 —— 所有被接受的按下都是同节点、"
                  "且中间有抬起，")
            print("    即全部是真实的人为按键。")

        if human:
            fastest = min(human) * 1000
            print()
            print(f"  人为连按的最快间隔 = {fastest:.0f} ms"
                  f"（去抖窗口 {rc.MIN_GAP * 1000:.0f} ms）")
            if fastest < rc.MIN_GAP * 1000 * 1.5:
                print("  ⚠ 余量不足：按得再快一点，第二次就会被去抖丢掉，")
                print("    双击不成立且没有提示。建议调小 MIN_GAP。")
            else:
                print(f"  ✓ 余量 {fastest - rc.MIN_GAP * 1000:.0f} ms，充足")
    return 0


if __name__ == "__main__":
    sys.exit(main())
