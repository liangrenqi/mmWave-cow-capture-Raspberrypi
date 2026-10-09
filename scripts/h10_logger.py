#!/usr/bin/env python3
"""Polar H10 蓝牙直采：HR/RR + ECG + ACC，**只存原始字节，不在 Pi 上解析**

## 为什么只存原始字节

与 pcap 同一原则：解析可以重做，丢掉的字节回不来。Pi 上只做连接、下命令、
逐条通知照原样落盘；解析在 Windows 上用 `vitalsigns_analysis/h10_parse.py` 离线做。

## 每条通知记两个时钟

    <CLOCK_REALTIME ns> <CLOCK_MONOTONIC ns> <来源> <十六进制原始字节>

REALTIME 与 tcpdump 给 pcap 打的时间戳同源，是日后与雷达对齐的依据；
MONOTONIC 不受 NTP 调时影响，两者之差若跳变说明采集中被调过钟。
注意这是**Pi 收到通知的时刻**，不是传感器采样时刻：BLE 成批发送，
中间还隔着 BlueZ / D-Bus，延迟与抖动都未测，需第 3 步对时实验标定。

来源：HR（标准心率 0x2A37）、PMD（Polar PMD 数据）、CP_TX / CP_RX（PMD 控制点
写出与回应）、EV（连接事件）。

## 协议依据

`vitalsigns_analysis/polarofficial polar-ble-sdk master technical_documentation/
online_measurement.pdf`：PMD UUID（表 3）、控制点命令 1/2/3（表 4）、
测量类型 ECG=0 ACC=2（表 2）、设置类型与字节数（表 5）。
**文档没写**、按社区实现推断的两处：设置回应从第 5 字节起是
「类型 1B、个数 1B、值×个数」的列表；启动请求按同样格式每项只带一个值。
两者都会在 CP_RX 行留下原始字节，回应状态码非 0 时程序报错退出。

用法：
    python3 h10_logger.py --duration 300
    python3 h10_logger.py --address 24:AC:AC:11:CC:D4 --out /media/pi/SSD/h10
    # 随雷达分段采集（capture_linux.py 内部这样调用，见 h10_session.py）：
    python3 h10_logger.py --log-path <文件> --reconnect --remove-bond --parent-pid <PID>
Ctrl-C 或 SIGTERM 正常停止（会先发停止命令再断开）。

## 随雷达采集时加的四个开关（2026-09-30 用户逐点确认后加，默认全关 = 旧行为）

- `--reconnect`：断线（或首次没扫到）不退出，等 `--retry-gap` 秒后重扫重连，
  直到收到停止信号。全部重连都记在**同一个**日志文件里 —— 一段雷达对应一个 H10 文件。
- `--remove-bond`：每次建连前 `bluetoothctl remove <地址>`。已绑定连接的首次 PMD 启动
  固定晚约 30 s，删绑定现场配对只要 0.3 s（见 诊断输出/H10启动延迟_结论汇总.md）。
- `--parent-pid`：父进程（雷达采集）没了就自行停止，不留孤儿进程占着 H10。
- `--exit-grace`：收到停止后最多再等这么多秒，超时强制退出。实测结束时 HCI Disconnect
  可能 6 s 才完成，事件循环卡在等回应里；不兜底的话外面只能 SIGKILL，fsync 都做不了。

EV 事件（十六进制存，解码后是 ASCII）：
    connected              每次建连成功（第一条 = 首连，之后的 = 重连）
    disconnected           **意外**断开（采集中途）
    disconnected_at_end    结束时 Pi 主动断开 —— 不算断线（V1 教训：旧版两者都记 disconnected）
    disconnected_by_host   出错后 Pi 主动断开，随后会重连
    session_error <说明>   连接期间出错（命令无回应、状态码非 0 等）
    attempt <n>            第 n 次扫描+建连尝试
    bond_remove rc=<码> …  删绑定的结果
    scan_fail / parent_gone / stop_signal / exit_forced / end
"""

import argparse
import asyncio
import importlib.metadata
import os
import signal
import socket
import struct
import sys
import threading
import time

from bleak import BleakClient, BleakScanner

HR_MEAS = "00002a37-0000-1000-8000-00805f9b34fb"
PMD_CP = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"

ECG, ACC = 0, 2
# 表 5：设置类型 → 每个值的字节数（3 未用）
SETTING_SIZE = {0: 2, 1: 2, 2: 2, 4: 1, 5: 4}
SETTING_NAME = {0: "采样率Hz", 1: "分辨率bit", 2: "量程", 4: "通道数", 5: "换算系数"}


class RawLog:
    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8", buffering=1)   # 行缓冲，崩溃最多丢一行
        self.n = {}
        # 强制退出的兜底线程也要写一行 exit_forced，加锁防止两行交错
        self.lock = threading.Lock()

    def header(self, key, val):
        with self.lock:
            if not self.f.closed:
                self.f.write(f"# {key}={val}\n")

    def rec(self, src, data=b"", rt=None, mono=None):
        rt = time.time_ns() if rt is None else rt
        mono = time.monotonic_ns() if mono is None else mono
        with self.lock:
            if self.f.closed:
                return
            self.f.write(f"{rt} {mono} {src} {bytes(data).hex()}\n")
            self.n[src] = self.n.get(src, 0) + 1

    def close(self):
        with self.lock:
            if self.f.closed:
                return
            self.f.flush()
            os.fsync(self.f.fileno())
            self.f.close()


def parse_settings(resp):
    """设置回应 → {类型: [值…]}。格式见文件头「文档没写」一段。"""
    out, i = {}, 5
    while i + 2 <= len(resp):
        t, cnt = resp[i], resp[i + 1]
        size = SETTING_SIZE.get(t)
        if size is None or i + 2 + cnt * size > len(resp):
            break
        vals = []
        for j in range(cnt):
            b = resp[i + 2 + j * size:i + 2 + (j + 1) * size]
            vals.append(struct.unpack("<f", b)[0] if t == 5 else int.from_bytes(b, "little"))
        out[t] = vals
        i += 2 + cnt * size
    return out


def build_start(mtype, chosen):
    req = bytearray([0x02, mtype])
    for t in sorted(chosen):
        req += bytes([t, 1]) + int(chosen[t]).to_bytes(SETTING_SIZE[t], "little")
    return bytes(req)


async def cp_command(client, log, q, cmd, what, timeout=5):
    log.rec("CP_TX", cmd)
    await client.write_gatt_char(PMD_CP, cmd, response=True)
    try:
        resp = await asyncio.wait_for(q.get(), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"{what}：5 秒内控制点无回应")
    if len(resp) < 4 or resp[0] != 0xF0 or resp[1] != cmd[0] or resp[2] != cmd[1]:
        raise RuntimeError(f"{what}：回应格式不符 {resp.hex()}")
    if resp[3] != 0:
        raise RuntimeError(f"{what}：状态码 {resp[3]}（非 0 = 失败），回应 {resp.hex()}")
    return resp


def choose(settings, prefer):
    """每个设置类型选一个值：prefer 里有且设备支持就用它，否则取最大。只发 0/1/2 三类。"""
    chosen = {}
    for t in (0, 1, 2):
        if t in settings:
            p = prefer.get(t)
            chosen[t] = p if p in settings[t] else max(settings[t])
    return chosen


async def remove_bond(log, address):
    """`bluetoothctl remove <地址>`。失败只记日志不中断：设备本来就没绑定时它也报错（rc≠0）。"""
    try:
        p = await asyncio.create_subprocess_exec(
            "bluetoothctl", "remove", address,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=10)
        txt = " ".join(out.decode(errors="replace").split())[:120]
        log.rec("EV", f"bond_remove rc={p.returncode} {txt}".encode())
        print(f"  删绑定 rc={p.returncode} {txt}")
    except Exception as e:
        log.rec("EV", f"bond_remove_error {type(e).__name__}: {e}".encode()[:160])
        print(f"  删绑定失败（继续）：{e}")


async def main(args):
    if args.log_path:
        path = os.path.abspath(args.log_path)
    else:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(args.out, f"h10_{stamp}.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    log = RawLog(path)
    log.header("format", "realtime_ns monotonic_ns src hex")
    log.header("start_local", time.strftime("%Y-%m-%d %H:%M:%S %z"))
    log.header("host", socket.gethostname())
    log.header("argv", " ".join(sys.argv))
    log.header("bleak", importlib.metadata.version("bleak"))      # bleak 模块本身没有 __version__
    log.header("pid", os.getpid())
    print(f"输出 {path}", flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    finished = [False, 0]               # [收尾完成, 退出码]

    # ---- 退出兜底：stop 置位后 exit_grace 秒还没走完收尾，就强制退出 ----
    # 用线程计时器而不是 asyncio 超时：卡住时事件循环在等一个永远不来的回应，
    # 协程层面的超时不一定能把它拽出来；线程计时器不依赖事件循环。
    guard = [None]

    def force_exit():
        if finished[0]:
            # 收尾已完成、日志已 fsync，只是 asyncio.run 退出时卡在 bleak/D-Bus 的清理里
            sys.stdout.flush()
            os._exit(finished[1])
        log.rec("EV", f"exit_forced after {args.exit_grace:.0f}s".encode())
        log.header("counts", " ".join(f"{k}:{v}" for k, v in log.n.items()))
        log.close()
        print(f"!! 收尾 {args.exit_grace:.0f} 秒未完成，强制退出（数据已 fsync）", flush=True)
        os._exit(3)

    def request_stop(why):
        if not stop.is_set():
            log.rec("EV", why.encode())
            stop.set()
        if guard[0] is None and args.exit_grace > 0:
            guard[0] = threading.Timer(args.exit_grace, force_exit)
            guard[0].daemon = True
            guard[0].start()

    # 重复信号一律幂等：外面套了 `timeout`，它转发信号时可能对子进程和整个进程组各发一次，
    # 若把「第二次信号」当成「立刻退出」，一次正常停止就会被误判成强退
    def on_signal(signame):
        request_stop(f"stop_signal {signame}")

    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, on_signal, s.name)

    # ---- 看护父进程：雷达采集进程没了，H10 不能变成孤儿一直占着设备 ----
    # 查的是「那个 PID 还在不在」而不是 getppid()：中间隔着 `timeout`，直接父进程是它
    async def watch_parent():
        while not stop.is_set():
            try:
                os.kill(args.parent_pid, 0)
            except ProcessLookupError:
                request_stop(f"parent_gone pid={args.parent_pid}")
                return
            except PermissionError:     # 进程在、只是不归我们管 —— 算活着
                pass
            await asyncio.sleep(1)

    if args.parent_pid:
        loop.create_task(watch_parent())

    cp_q = asyncio.Queue()
    last_hr = [None]
    t_prog = time.monotonic()
    t_first = [None]                    # --duration 从首次启动流算起
    scan_dumped = [False]
    streams = [(ECG, "ECG", {}), (ACC, "ACC", {0: args.acc_rate, 2: args.acc_range})]

    def on_hr(_, data):
        log.rec("HR", data)
        flags = data[0]
        last_hr[0] = int.from_bytes(data[1:3], "little") if flags & 1 else data[1]

    def on_pmd(_, data):
        log.rec("PMD", data)

    def on_cp(_, data):
        log.rec("CP_RX", data)
        cp_q.put_nowait(bytes(data))

    async def wait_any(sec, *events):
        """等 stop 或给定事件之一，最多 sec 秒"""
        evs = (stop,) + events
        tasks = [asyncio.ensure_future(e.wait()) for e in evs]
        try:
            await asyncio.wait(tasks, timeout=sec, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()

    async def one_connection(dev, n_conn):
        """一次连接的完整生命周期。返回 "stop"（该结束了）/ "lost"（意外断开）/ "error"。"""
        st = {"connected": False, "closing": None}
        lost = asyncio.Event()

        # bleak 建连时遇 0x3e 会在内部自动重试，每次失败都回调 on_disc；
        # 连上之前的断开只记日志，否则后面某次重试连上了也会被当成断线
        def on_disc(_):
            if st["closing"] == "end":
                log.rec("EV", b"disconnected_at_end")
            elif st["closing"] == "error":
                log.rec("EV", b"disconnected_by_host")
            elif st["connected"]:
                log.rec("EV", b"disconnected")
                print("!! 连接断开", flush=True)
                lost.set()
            else:
                log.rec("EV", b"connect_attempt_failed")
                print("  建连未成，bleak 重试中…", flush=True)

        while not cp_q.empty():         # 上一次连接残留的控制点回应不能串到这次
            cp_q.get_nowait()

        async def start_streams(client):
            out = []
            for mtype, name, prefer in streams:
                if stop.is_set() or lost.is_set():
                    break
                resp = await cp_command(client, log, cp_q, bytes([0x01, mtype]), f"{name} 读设置")
                sts = parse_settings(resp)
                print(f"{name} 可选设置：" + "；".join(f"{SETTING_NAME[t]} {v}" for t, v in sts.items()))
                chosen = choose(sts, prefer)
                await cp_command(client, log, cp_q, build_start(mtype, chosen), f"{name} 启动")
                log.header(f"{name}_settings", " ".join(f"{t}:{v}" for t, v in chosen.items()))
                out.append((mtype, name))
                print(f"{name} 已启动：" + "；".join(f"{SETTING_NAME[t]} {v}" for t, v in chosen.items()),
                      flush=True)
            return out

        try:
            async with BleakClient(dev, disconnected_callback=on_disc,
                                   timeout=args.connect_timeout) as client:
                st["connected"] = True
                log.rec("EV", b"connected")
                log.header("mtu", getattr(client, "mtu_size", "?"))
                feat = await client.read_gatt_char(PMD_CP)
                log.rec("CP_FEAT", feat)
                print(f"已连接 {dev.address}（第 {n_conn} 次），MTU {getattr(client, 'mtu_size', '?')}，"
                      f"PMD 特性 {bytes(feat).hex()}", flush=True)

                try:
                    await client.start_notify(PMD_CP, on_cp)
                    await client.start_notify(PMD_DATA, on_pmd)
                    await client.start_notify(HR_MEAS, on_hr)

                    # 诊断用：连上并订阅后先等 N 秒再读设置、发启动命令，用来区分 PMD 约 30 s 的延迟
                    # 是从连接起算还是从启动命令起算（诊断输出/H10启动延迟_根因判据_测前写定.md 实验 C）
                    if args.start_delay > 0 and n_conn == 1:
                        log.rec("EV", b"start_delay_begin")
                        print(f"等待 {args.start_delay:.0f} 秒再发启动命令…")
                        await wait_any(args.start_delay, lost)
                        log.rec("EV", b"start_delay_end")

                    started = await start_streams(client)
                    if started and t_first[0] is None:
                        t_first[0] = time.monotonic()

                    # 诊断用：同一连接内停止 PMD 再重启，看第二次启动是否也要约 30 s
                    # （诊断输出/H10启动延迟_根因判据_测前写定.md 实验 E）。只在首次连接上做
                    if args.restart_after > 0 and started and n_conn == 1:
                        print(f"{args.restart_after:.0f} 秒后停止并重启 PMD…")
                        await wait_any(args.restart_after, lost)
                        if not stop.is_set() and not lost.is_set():
                            log.rec("EV", b"restart_begin")
                            for mtype, name in started:
                                await cp_command(client, log, cp_q, bytes([0x03, mtype]),
                                                 f"{name} 停止（重启前）")
                            print(f"已停止，等 {args.restart_gap:.0f} 秒再启动…")
                            await wait_any(args.restart_gap, lost)
                            started = await start_streams(client)
                            log.rec("EV", b"restart_end")

                    while not stop.is_set() and not lost.is_set():
                        await wait_any(10, lost)
                        el = time.monotonic() - (t_first[0] or t_prog)
                        print(f"{el:6.0f} s  HR 通知 {log.n.get('HR', 0)}  PMD 通知 {log.n.get('PMD', 0)}"
                              f"  心率 {last_hr[0]}", flush=True)
                        if args.duration and el >= args.duration:
                            request_stop("duration_reached")

                    if lost.is_set():
                        return "lost"

                    # 正常结束：先发停止命令，再由 async with 断开（记 disconnected_at_end）
                    st["closing"] = "end"
                    if client.is_connected:
                        for mtype, name in started:
                            try:
                                await cp_command(client, log, cp_q, bytes([0x03, mtype]), f"{name} 停止",
                                                 timeout=2)
                            except Exception as e:          # 停止失败不影响已落盘数据
                                print(f"{name} 停止命令失败：{e}")
                    return "stop"
                except Exception as e:
                    if lost.is_set():           # 断线导致的后续报错，按断线算
                        return "lost"
                    # 连着但出错（控制点无回应、状态码非 0…）：主动断开，交给外层决定是否重连
                    st["closing"] = "error"
                    log.rec("EV", f"session_error {type(e).__name__}: {e}".encode()[:200])
                    print(f"!! 连接期间出错：{e}", flush=True)
                    return "error"
        except Exception as e:
            if st["connected"]:
                # 已经连上后才抛出（多半是断开时 async with 的退出清理报错）
                return "lost" if lost.is_set() else ("stop" if st["closing"] == "end" else "error")
            log.rec("EV", f"connect_fail {type(e).__name__}: {e}".encode()[:200])
            print(f"!! 建连失败：{type(e).__name__}: {e}", flush=True)
            return "error"

    # ---- 主循环：扫描 → 建连 → 采集；--reconnect 时断了再来 ----
    attempt, n_conn, rc = 0, 0, 0
    while not stop.is_set():
        attempt += 1
        log.rec("EV", f"attempt {attempt}".encode())
        if args.remove_bond:
            await remove_bond(log, args.address)
            if stop.is_set():
                break

        # 按 MAC 地址找，不按名字：实测 H10 广播时可以不带名字（bluetoothctl 只显示地址），
        # find_device_by_name 会因此扫到了也判失败（2026-09-28 四次 scan_fail）
        print(f"扫描 {args.address}（{args.scan_timeout} 秒，第 {attempt} 次）…手机蓝牙必须先关闭",
              flush=True)
        try:
            dev = await BleakScanner.find_device_by_address(args.address, timeout=args.scan_timeout)
        except Exception as e:          # 适配器忙等，按没扫到处理
            log.rec("EV", f"scan_error {type(e).__name__}: {e}".encode()[:200])
            dev = None
        if stop.is_set():
            break
        if dev is None:
            log.rec("EV", b"scan_fail")
            if not scan_dumped[0]:
                # 顺带记下这次扫到的全部设备，下次失败能分清「没扫到」还是「地址不对」。只记一次
                scan_dumped[0] = True
                try:
                    seen = await BleakScanner.discover(timeout=5, return_adv=True)
                    for addr, (d, adv) in seen.items():
                        log.rec("SCAN", f"{addr} {adv.rssi} {adv.local_name or d.name or ''}".encode())
                    print(f"  没扫到；补扫 5 秒见到 {len(seen)} 个设备，已记入日志", flush=True)
                except Exception as e:
                    print(f"  补扫失败：{e}")
            if not args.reconnect:
                print(f"没找到 {args.address}：确认已佩戴（电极湿润）、手机蓝牙已关", flush=True)
                rc = 1
                break
            await wait_any(args.retry_gap)
            continue

        log.header("device", f"{dev.name} {dev.address}")
        n_conn += 1
        res = await one_connection(dev, n_conn)
        if res == "stop" or not args.reconnect:
            if res != "stop":
                rc = 2
            break
        print(f"  {args.retry_gap:.0f} 秒后重连…", flush=True)
        await wait_any(args.retry_gap)

    log.rec("EV", b"end")
    log.header("counts", " ".join(f"{k}:{v}" for k, v in log.n.items()))
    log.close()
    finished[1], finished[0] = rc, True
    print(f"结束。计数 {log.n}\n文件 {path}", flush=True)
    return rc


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", default="24:AC:AC:11:CC:D4", help="H10 的 MAC 地址（设备 ID 11CCD438 那条）")
    ap.add_argument("--out", default=os.path.expanduser("~/Ti_radar/data/h10"))
    ap.add_argument("--duration", type=float, default=0, help="秒；0 = 直到 Ctrl-C")
    ap.add_argument("--acc-rate", type=int, default=200, help="ACC 采样率，设备不支持则取最大")
    ap.add_argument("--acc-range", type=int, default=8, help="ACC 量程 g，与手机导出的 ±8 g 一致")
    ap.add_argument("--scan-timeout", type=float, default=20)
    ap.add_argument("--connect-timeout", type=float, default=30, help="秒；含 bleak 内部重试")
    ap.add_argument("--start-delay", type=float, default=0,
                    help="诊断用：连上并订阅后等待的秒数，再发启动命令；0 = 不等（默认行为不变）")
    ap.add_argument("--restart-after", type=float, default=0,
                    help="诊断用：首次启动后多少秒在同一连接内停止 PMD 再重启；0 = 不重启（默认行为不变）")
    ap.add_argument("--restart-gap", type=float, default=5, help="诊断用：重启时停止与再启动之间的秒数")
    # ---- 随雷达采集用（默认全关 = 旧行为）----
    ap.add_argument("--log-path", default=None,
                    help="直接指定日志文件路径（覆盖 --out 的自动命名）；分段采集时由 capture_linux 给出")
    ap.add_argument("--reconnect", action="store_true",
                    help="断线或没扫到时不退出，隔 --retry-gap 秒重扫重连，直到收到停止信号")
    ap.add_argument("--retry-gap", type=float, default=3, help="重连间隔秒")
    ap.add_argument("--remove-bond", action="store_true",
                    help="每次建连前 bluetoothctl remove 地址，绕开已绑定连接首次启动的 30 s 延迟")
    ap.add_argument("--parent-pid", type=int, default=0,
                    help="该 PID 的进程消失就自行停止；0 = 不看护")
    ap.add_argument("--exit-grace", type=float, default=12,
                    help="收到停止后最多等多少秒走完收尾，超时 fsync 后强制退出；0 = 不兜底")
    sys.exit(asyncio.run(main(ap.parse_args())))
