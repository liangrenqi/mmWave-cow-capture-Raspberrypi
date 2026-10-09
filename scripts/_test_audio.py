#!/usr/bin/env python3
"""语音提示自检与占位音频生成 —— 不碰雷达、不碰 DCA1000

三件事：
  1. 列出 ALSA 声卡，确认 USB 喇叭的名字（用于 -D plughw:CARD=<名字>）
  2. 逐个播放 sounds/ 下的 wav，确认能出声、音量合适
  3. `--gen` 生成占位 wav（纯合成音，无需录音设备），
     让你在录真人语音之前就能把整条链路跑通

用法：
    python3 _test_audio.py                 # 列设备 + 播放现有 wav
    python3 _test_audio.py --gen           # 先生成缺失的占位 wav 再播
    python3 _test_audio.py --gen --force   # 覆盖重新生成全部占位 wav
    python3 _test_audio.py --dev plughw:CARD=Device

占位音的设计：不同事件用不同的音高与节奏，闭着眼也能分辨 ——
  ready       中音单响
  start       低到高的上滑音（"开始了"）
  done_good   三声上行（愉快）
  done_bad    两声下行（不妙）
  done_abort  一长一短
  error       快速三连低音
  h10_*       心率带四个事件，统一用最高音区（连接 / 断开 / 重连 / 未连接）

生成的 wav 是 16-bit PCM / 44.1 kHz / 单声道，aplay 直接支持。
录真人语音时用同样格式覆盖同名文件即可，代码不用改。
"""

import argparse
import math
import os
import struct
import subprocess
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import remote_control as rc

RATE = 44100


def tone(freq, ms, vol=0.35, fade_ms=8):
    """生成一段正弦音的样本列表

    两端加淡入淡出：直接切断会有"啪"的爆音（波形突变），
    8 ms 的斜坡足以消除，听感上仍是干脆的短音。
    """
    n = int(RATE * ms / 1000)
    fade = max(1, int(RATE * fade_ms / 1000))
    out = []
    for i in range(n):
        a = vol
        if i < fade:
            a *= i / fade
        elif i > n - fade:
            a *= (n - i) / fade
        out.append(a * math.sin(2 * math.pi * freq * i / RATE))
    return out


def sweep(f0, f1, ms, vol=0.35, fade_ms=8):
    """频率线性变化的滑音（用相位累加，直接算 sin(2πft) 会在频率变化时跳相）"""
    n = int(RATE * ms / 1000)
    fade = max(1, int(RATE * fade_ms / 1000))
    out, phase = [], 0.0
    for i in range(n):
        f = f0 + (f1 - f0) * i / max(1, n - 1)
        phase += 2 * math.pi * f / RATE
        a = vol
        if i < fade:
            a *= i / fade
        elif i > n - fade:
            a *= (n - i) / fade
        out.append(a * math.sin(phase))
    return out


def silence(ms):
    return [0.0] * int(RATE * ms / 1000)


# 占位音设计：音高与节奏各不相同，不看屏幕也能分辨
PLACEHOLDER = {
    # 两声同音短促 = "收到了" 的应答感，与 start 的上滑音明显不同。
    # 双击后立刻响，告诉饲养员命令已下发、不要再按。
    "preparing.wav":  lambda: (tone(760, 110) + silence(70)
                               + tone(760, 110)),
    "ready.wav":      lambda: tone(880, 180),
    "start.wav":      lambda: sweep(440, 990, 350),
    "done_good.wav":  lambda: (tone(660, 120) + silence(60)
                               + tone(880, 120) + silence(60)
                               + tone(1170, 200)),
    "done_bad.wav":   lambda: (tone(440, 300) + silence(80)
                               + tone(330, 450)),
    "done_abort.wav": lambda: (tone(520, 450) + silence(80)
                               + tone(700, 160)),
    "error.wav":      lambda: (tone(300, 130) + silence(50)
                               + tone(300, 130) + silence(50)
                               + tone(300, 130)),

    # ---- 守护进程用（remote_daemon.py）----
    # 与采集程序那套刻意区分开：程序级事件用**低音区**，
    # 采集级事件用中高音区。听一下就知道现在是"程序开关"还是"采集开关"。
    "daemon_ready.wav": lambda: (tone(392, 150) + silence(70)
                                 + tone(392, 150)),
    "daemon_start.wav": lambda: sweep(262, 523, 420),
    "daemon_exit.wav":  lambda: sweep(523, 262, 420),
    "daemon_busy.wav":  lambda: (tone(233, 100) + silence(45)
                                 + tone(233, 100) + silence(45)
                                 + tone(233, 100) + silence(45)
                                 + tone(233, 100)),

    # ---- 心率带 H10（h10_session.py）----
    # 用**最高音区**（1300–2000 Hz 的短促音），与上面两套都区分开：
    # 一听是"尖的"就知道说的是心率带，不是雷达。
    "h10_connected.wav":   lambda: (tone(1320, 90) + silence(50)
                                    + tone(1760, 140)),
    "h10_lost.wav":        lambda: (sweep(1980, 1180, 260) + silence(90)
                                    + sweep(1980, 1180, 260)),
    "h10_reconnected.wav": lambda: (sweep(1180, 1980, 220) + silence(60)
                                    + tone(1760, 90) + silence(40)
                                    + tone(1760, 90)),
    "h10_missing.wav":     lambda: (tone(1480, 80) + silence(70)
                                    + tone(1480, 80) + silence(70)
                                    + tone(1480, 80) + silence(70)
                                    + tone(1100, 260)),
}


def write_wav(path, samples):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)               # 16-bit
        w.setframerate(RATE)
        w.writeframes(b"".join(
            struct.pack("<h", max(-32767, min(32767, int(s * 32767))))
            for s in samples))


def list_cards():
    print("=" * 68)
    print("  ALSA 声卡")
    print("=" * 68)
    try:
        with open("/proc/asound/cards") as f:
            print(f.read().rstrip())
    except OSError as e:
        print(f"  读不到 /proc/asound/cards: {e}")
    print()
    print("  方括号里的名字就是 -D plughw:CARD=<名字> 要填的。")
    print("  **不要用 card 号** —— 插拔顺序变了它会变，名字不会。")
    print()
    r = subprocess.run(["aplay", "-l"], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
    print(r.stdout.rstrip())
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gen", action="store_true", help="生成缺失的占位 wav")
    ap.add_argument("--force", action="store_true",
                    help="配合 --gen：覆盖已存在的文件")
    ap.add_argument("--dev", default=None, help="ALSA 设备，默认按名字选 USB 喇叭")
    ap.add_argument("--dir", default=None, help="wav 目录，默认 ../sounds/")
    ap.add_argument("--no-play", action="store_true", help="只生成不播放")
    args = ap.parse_args()

    list_cards()

    sounds_dir = os.path.abspath(args.dir or rc.AudioNotifier.DEFAULT_DIR)
    device = args.dev or rc.AudioNotifier.DEVICE
    print("=" * 68)
    print(f"  音频目录 : {sounds_dir}")
    print(f"  播放设备 : {device}")
    print("=" * 68)
    print()

    if args.gen:
        os.makedirs(sounds_dir, exist_ok=True)
        for fn, maker in PLACEHOLDER.items():
            path = os.path.join(sounds_dir, fn)
            if os.path.isfile(path) and not args.force:
                print(f"  跳过（已存在）: {fn}")
                continue
            write_wav(path, maker())
            print(f"  已生成: {fn}  ({os.path.getsize(path):,} B)")
        print()

    # 按事件顺序播放，顺便验证 AudioNotifier 用的映射与实际文件一致
    def play_group(title, pairs):
        """播一组事件->文件，返回缺失的文件名列表"""
        print(f"  --- {title} ---")
        gone = []
        for event, fn in pairs:
            if not fn:
                continue
            path = os.path.join(sounds_dir, fn)
            if not os.path.isfile(path):
                print(f"     {event:13s} {fn:20s} **缺失**")
                gone.append(fn)
                continue
            size = os.path.getsize(path)
            print(f"     {event:13s} {fn:20s} {size:>9,} B",
                  end="", flush=True)
            if args.no_play:
                print()
                continue
            r = subprocess.run(["aplay", "-q", "-D", device, path],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, text=True)
            if r.returncode == 0:
                print("   [播放 OK]")
            else:
                print(f"   [播放失败] {(r.stderr or '').strip()[:80]}")
        return gone

    print("  逐个播放（顺序即实际出现顺序）：")
    print()

    # 采集程序的事件（capture_linux.py --remote）
    cap_map = rc.LogNotifier._AUDIO
    missing = play_group(
        "采集程序 capture_linux.py --remote（中高音区）",
        [(e, cap_map.get(e)) for e in
         ("preparing", "start", "done_good", "done_bad",
          "done_abort", "error", "ready")])

    print()
    missing += play_group(
        "心率带 H10（最高音区，与雷达区分）",
        [(e, cap_map.get(e)) for e in
         ("h10_connected", "h10_lost", "h10_reconnected", "h10_missing")])

    # 守护进程的事件（remote_daemon.py）。import 放这里而不是文件顶部：
    # 只有这一处用得到，且它会 import evdev，没插遥控器时不该拖累纯音频测试。
    print()
    try:
        import remote_daemon
        dmn_map = remote_daemon.DaemonNotifier._AUDIO
        missing += play_group(
            "守护进程 remote_daemon.py（低音区，与上面区分）",
            [(e, dmn_map.get(e)) for e in
             ("daemon_ready", "app_starting", "app_exiting", "busy")])
    except Exception as e:
        print(f"  --- 守护进程音频（跳过：{e}）---")

    missing = sorted(set(missing))
    print()
    if missing:
        print(f"  缺 {len(missing)} 个文件：{' '.join(missing)}")
        print("  跑 `python3 _test_audio.py --gen` 生成占位音，")
        print("  或直接放入同名的真人语音 wav。")
    else:
        print(f"  {len(PLACEHOLDER)} 个文件齐全。")
    print()
    print("  录真人语音时的格式：16-bit PCM / 44.1 kHz / 单声道")
    print("  （其它采样率也能播 —— 用的是 plughw 会自动重采样，"
          "但同格式最稳）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
