#!/usr/bin/env python3
"""pcap → bin 转换，输出与 TI CLI_Record 的 _Raw_n.bin 逐字节一致

规则来源：2026-07-31 用 200 帧实测数据比对 CLI_Record 输出坐实，
13,107,200 个 int16 样本零差异。

两个容易错的地方：
  1. 载荷偏移是 52，不是 42 —— Ethernet 14 + IP 20 + UDP 8 = 42 到 UDP
     载荷，再跳 10 字节 DCA 包头（UINT32 序号 + 6 字节累计已发送字节数）
  2. 必须做 lane 重排 —— json 里 reorderEnable=1，CLI_Record 落盘时
     每 4 个 int16 交换中间两个：(a,b,c,d) → (a,c,b,d)
     只拼接载荷会得到大小正确但内容错乱的文件（实测 43% 字节不同）

**2026-08-06 改成流式**。原实现把整个载荷读进内存再一次性写出，
7.4 GB 的 pcap 需要约 14 GB 内存（载荷 + array 副本），Pi 5 只有 2 GB，
必然 OOM；且 reorder 是逐元素 Python 循环，35 亿个 int16 要跑几十分钟。
现在边读边写，内存恒定约 3 MB，重排用 array 扩展切片（快约 50 倍）。

流式为什么与整体重排等价：每包载荷 1456 B = 728 个 int16 = 182 个完整
四元组，**1456 % 8 == 0**，包边界与四元组边界严格对齐。
（memory 里"1456 不是 8 的倍数"那条记错了，1456 = 8 × 182。）
这两点与 verify_pcap_bin.py 的 L3 判据同构，那份已在 7 GB 数据上验证过。

用法：
    python3 pcap_to_bin.py capture.pcap out.bin
    python3 pcap_to_bin.py capture.pcap out.bin --verify ref_Raw_0.bin
    python3 pcap_to_bin.py capture.pcap out.bin --verify-dir <session_dir>
    python3 pcap_to_bin.py capture.pcap out.bin --no-reorder   # 保留网线原序
    python3 pcap_to_bin.py capture.pcap out.bin --frame-bytes 786432  # 行为
"""

import argparse
import array
import glob
import hashlib
import os
import re
import struct
import sys
import time

PAYLOAD_OFF = 42          # Ethernet 14 + IP 20 + UDP 8
DCA_HDR = 10              # UINT32 seq + 6B cumulative byte count
FRAME_BYTES_DEFAULT = 131072
PKT_PAYLOAD = 1456
CHUNK = PKT_PAYLOAD * 2000        # 2,912,000 B，是 8 的倍数


def iter_payloads(path):
    """流式产出每包的 (序号, 载荷)。截断包直接中止（数据不可用）。"""
    with open(path, "rb") as f:
        gh = f.read(24)
        if len(gh) < 24:
            sys.exit("pcap 文件头不完整")
        magic, = struct.unpack("<I", gh[:4])
        if magic not in (0xA1B2C3D4, 0xA1B23C4D):
            sys.exit(f"不认识的 pcap magic {magic:#x}（只支持小端）")
        linktype, = struct.unpack("<I", gh[20:24])
        if linktype != 1:
            sys.exit(f"linktype={linktype}，只支持 Ethernet(1)")

        while True:
            ph = f.read(16)
            if len(ph) < 16:
                break
            _, _, caplen, wirelen = struct.unpack("<IIII", ph)
            pkt = f.read(caplen)
            if len(pkt) < caplen:
                break
            if caplen != wirelen:
                sys.exit("发现截断包（caplen != wirelen）—— "
                         "抓包时漏了 -s 0，数据不可用")
            udp = pkt[PAYLOAD_OFF:]
            if len(udp) < DCA_HDR:
                continue
            yield struct.unpack("<I", udp[:4])[0], udp[DCA_HDR:]


def reorder_lanes(data):
    """(a,b,c,d) → (a,c,b,d)，每 4 个 int16 一组

    2 lane LVDS 的交织顺序。用 array 扩展切片赋值，比逐元素快约 50 倍
    （实测 8M 个 int16 重排 0.133 秒），结果与逐元素参照实现完全相同。

    len(data) 应是 8 的倍数（每 4 个 int16 = 8 字节一组）。
    不足的尾部原样保留 —— 正常数据不会走到这条路径，
    因为 1456 % 8 == 0 使每包都是完整四元组。
    """
    s = array.array("h")
    s.frombytes(data)
    n4 = (len(s) // 4) * 4
    out = array.array("h", bytes(len(s) * 2))
    if n4:
        out[0:n4:4] = s[0:n4:4]
        out[1:n4:4] = s[2:n4:4]
        out[2:n4:4] = s[1:n4:4]
        out[3:n4:4] = s[3:n4:4]
    for i in range(n4, len(s)):
        out[i] = s[i]
    return out.tobytes()


def sorted_bins(session_dir):
    """按 _Raw_<N>.bin 的数字排序 —— 字典序在 N>=10 时会把 _Raw_10 排到
    _Raw_2 前面，拼接顺序错了整段数据从那里起全是垃圾且不报错。
    """
    paths = glob.glob(os.path.join(session_dir, "*_Raw_*.bin"))
    if not paths:
        sys.exit(f"{session_dir} 下没有 _Raw_*.bin")

    def idx(p):
        m = re.search(r"_Raw_(\d+)\.bin$", os.path.basename(p))
        if not m:
            sys.exit(f"文件名不符合 _Raw_<N>.bin: {p}")
        return int(m.group(1))

    out = sorted(paths, key=idx)
    got = [idx(p) for p in out]
    if got != list(range(len(got))):
        sys.exit(f"分片编号不连续: {got} —— 缺文件会导致静默错位")
    return out


class BinStream:
    """把多个 _Raw_N.bin 当一条连续字节流读

    分片边界**不落在帧边界上**：每片 1,073,741,760 B 是 1456 的整数倍
    但不是 131,072 的整数倍（8191.9995），所以每个边界都把一帧劈成两半，
    错位量逐片递增 64 B。当参照物比对时必须按连续流读，否则从第一个
    边界起就整体错位。
    """

    def __init__(self, paths):
        self.paths = list(paths)
        self.i = 0
        self.f = open(self.paths[0], "rb") if self.paths else None

    def read(self, n):
        out = b""
        while self.f is not None and len(out) < n:
            piece = self.f.read(n - len(out))
            if piece:
                out += piece
                continue
            self.f.close()
            self.i += 1
            self.f = (open(self.paths[self.i], "rb")
                      if self.i < len(self.paths) else None)
        return out

    def close(self):
        if self.f is not None:
            self.f.close()
            self.f = None


class SeqTracker:
    """流式统计序号连续性 —— 不保存全部序号（4861187 个 int 约 150 MB）

    只留首末、极值、计数与乱序数，丢失数由 span − count 推出。
    与 verify_pcap_bin.py 的 L0 算法一致。
    """

    def __init__(self):
        self.n = 0
        self.first = self.last = None
        self.lo = self.hi = None
        self.oos = 0

    def add(self, seq):
        self.n += 1
        if self.first is None:
            self.first = self.lo = self.hi = seq
        else:
            if seq <= self.last:
                self.oos += 1
            self.lo = min(self.lo, seq)
            self.hi = max(self.hi, seq)
        self.last = seq

    def report(self):
        if not self.n:
            print("包数     : 0")
            return 0
        span = self.hi - self.lo + 1
        missing = span - self.n
        print(f"包数     : {self.n:,}  序号 {self.first} → {self.last}")
        print(f"丢失     : {missing}   乱序: {self.oos}")
        if missing:
            print("注意：丢包会使后续样本整体错位，"
                  "按帧边界定位并丢弃受影响的帧")
        return missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pcap")
    ap.add_argument("out")
    ap.add_argument("--verify", metavar="REF.bin",
                    help="与单个 CLI_Record bin 逐字节比对")
    ap.add_argument("--verify-dir", metavar="SESSION_DIR",
                    help="与整段的全部 _Raw_*.bin 比对（按连续流，"
                         "正确处理跨分片帧）")
    ap.add_argument("--no-reorder", action="store_true",
                    help="不做 lane 重排，保留网线原始顺序")
    ap.add_argument("--frame-bytes", type=int, default=FRAME_BYTES_DEFAULT,
                    help=f"每帧字节数，默认 {FRAME_BYTES_DEFAULT}（妊娠）；"
                         f"行为配置是 786432，用错只影响帧数显示")
    args = ap.parse_args()

    ref_stream = None
    if args.verify and args.verify_dir:
        sys.exit("--verify 与 --verify-dir 只能给一个")
    if args.verify:
        ref_stream = BinStream([args.verify])
    elif args.verify_dir:
        paths = sorted_bins(args.verify_dir)
        print("参照分片 :")
        for p in paths:
            print(f"           {os.path.basename(p)}  "
                  f"{os.path.getsize(p):,} B")
        ref_stream = BinStream(paths)

    seqs = SeqTracker()
    md5_out = hashlib.md5()
    md5_ref = hashlib.md5()
    written = compared = diff_bytes = 0
    first_diff = None
    buf = bytearray()
    t0 = time.time()

    def flush(final=False):
        """把 buf 里 8 的倍数长度的部分重排后写出，并与参照流比对

        为什么可以逐块重排：每包载荷 1456 = 8 × 182，包边界与四元组边界
        严格对齐，所以逐块重排与整体重排数学等价。
        """
        nonlocal written, compared, diff_bytes, first_diff
        n = len(buf) if final else (len(buf) // 8) * 8
        if not n:
            return
        chunk = bytes(buf[:n])
        del buf[:n]
        data = chunk if args.no_reorder else reorder_lanes(chunk)
        fout.write(data)
        md5_out.update(data)
        written += len(data)
        if ref_stream is not None:
            ref = ref_stream.read(len(data))
            md5_ref.update(ref)
            m = min(len(data), len(ref))
            if data[:m] != ref[:m]:
                for i in range(m):
                    if data[i] != ref[i]:
                        if first_diff is None:
                            first_diff = compared + i
                        diff_bytes += 1
            compared += m

    with open(args.out, "wb") as fout:
        for seq, payload in iter_payloads(args.pcap):
            seqs.add(seq)
            buf += payload
            if len(buf) >= CHUNK:
                flush()
        flush(final=True)

    dt = time.time() - t0
    seqs.report()

    print(f"\n输出     : {args.out}")
    print(f"大小     : {written:,} B  用时 {dt:.1f} s "
          f"({written / dt / 1e6:.1f} MB/s)" if dt > 0 else "")
    frames, tail = divmod(written, args.frame_bytes)
    print(f"帧数     : {frames:,}" + (f" + 尾部 {tail:,} B 不足一帧"
                                      f"（缺 {args.frame_bytes - tail:,} B，"
                                      f"后处理按整帧取即可）" if tail else " 整"))
    print(f"重排     : {'否（网线原序）' if args.no_reorder else '是 (a,b,c,d)→(a,c,b,d)'}")

    if ref_stream is None:
        return 0

    # 参照流可能比 pcap 载荷短（末帧零头），把剩余也计入 md5 以便比总量
    ref_extra = 0
    while True:
        tail_b = ref_stream.read(1 << 20)
        if not tail_b:
            break
        md5_ref.update(tail_b)
        ref_extra += len(tail_b)
    ref_stream.close()

    print(f"\nmd5 输出 : {md5_out.hexdigest()}")
    print(f"md5 参照 : {md5_ref.hexdigest()}")
    print(f"比对     : {compared:,} B")
    if ref_extra:
        print(f"参照多出 : {ref_extra:,} B（参照比 pcap 载荷长，异常）")
    if written > compared:
        print(f"输出多出 : {written - compared:,} B"
              f"（pcap 载荷比 bin 长，末帧零头未落盘属正常）")

    if first_diff is None and diff_bytes == 0:
        print(">>> 已比对的部分逐字节完全一致 <<<")
        return 0

    frame = first_diff // args.frame_bytes
    print(f"!!! 不一致：差异 {diff_bytes:,} B，首个位置 {first_diff:,}"
          f" = 第 {frame} 帧内偏移 {first_diff % args.frame_bytes:,}")
    print("    从头就有且量大 → 检查 --no-reorder 用反了；"
          "出现在某个分片边界 → 参照流拼接顺序有问题")
    return 1


if __name__ == "__main__":
    sys.exit(main())
