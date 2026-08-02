#!/usr/bin/env python3
"""L3 完整形式：pcap → bin 逐字节比对（流式，不吃内存）

与阶段 5 已跑过的 L3 的区别 —— 那次只比了**文件大小**：
    L3 bin = 786,432,000 B = 6000 × 131,072 精确
**大小相等几乎不能说明任何事**：CLI_Record 会用零填充补掉丢失的包，
第一阶段实测丢 4863 包后文件大小仍精确等于期望值。
所以"大小对"这个判据对丢包基本免疫，逐字节才是真的 L3。

为什么可以流式做（而不必把 786 MB 全读进内存）：
    每包载荷 1456 B = 728 个 int16 = 182 个完整四元组，1456 % 8 == 0。
    **包边界与 4 元组边界严格对齐**，所以"逐包重排"与"整体重排"数学等价，
    可以边读边比。（memory 里"1456 不是 8 的倍数、包边界会落在四元组中间"
    这条记错了，1456 = 8 × 182。）

同时复算 L0（序号连续性）和 L2（卡自报累计字节），使本脚本自成判据、
不依赖采集时 capture_linux.py 的输出。

用法：
    python3 verify_pcap_bin.py <session_dir>
    python3 verify_pcap_bin.py <session_dir> --frame-bytes 786432
"""

import argparse
import array
import glob
import hashlib
import os
import struct
import sys
import time

PAYLOAD_OFF = 42        # Ethernet 14 + IP 20 + UDP 8
DCA_HDR = 10            # UINT32 序号 + 6 字节累计已发送字节数
PKT_PAYLOAD = 1456
CHUNK = PKT_PAYLOAD * 2000      # 2,912,000 B，是 8 的倍数


def reorder(buf):
    """(a,b,c,d) → (a,c,b,d)，每 4 个 int16 一组

    用 array 的扩展切片赋值，比逐元素快约 50 倍。
    实测与逐元素参照实现结果完全相同。
    len(buf) 必须是 8 的倍数（调用方保证）。
    """
    s = array.array("h")
    s.frombytes(buf)
    out = array.array("h", bytes(len(s) * 2))
    out[0::4] = s[0::4]
    out[1::4] = s[2::4]
    out[2::4] = s[1::4]
    out[3::4] = s[3::4]
    return out.tobytes()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("--frame-bytes", type=int, default=131072)
    args = ap.parse_args()

    d = args.session
    pcaps = sorted(glob.glob(os.path.join(d, "*.pcap")))
    bins = sorted(glob.glob(os.path.join(d, "*_Raw_*.bin")))
    if not pcaps:
        sys.exit(f"[ERROR] {d} 下没有 .pcap")
    if not bins:
        sys.exit(f"[ERROR] {d} 下没有 _Raw_*.bin")

    pcap_path = pcaps[0]
    bin_total = sum(os.path.getsize(b) for b in bins)
    print(f"pcap : {os.path.basename(pcap_path)}  "
          f"{os.path.getsize(pcap_path):,} B")
    for b in bins:
        print(f"bin  : {os.path.basename(b)}  {os.path.getsize(b):,} B")
    print(f"每帧 : {args.frame_bytes:,} B")
    print("-" * 66)

    # bin 侧当作一条连续字节流读（CLI_Record 切分只是文件边界，内容连续）
    bin_iter = iter(bins)
    bf = open(next(bin_iter), "rb")
    bin_done = False

    def bin_read(n):
        """跨文件读 n 字节

        CLI_Record 按 maxRecFileSize_MB 切分（行为配置 2.2 GB → 3 个 bin），
        内容是连续的，这里当一条字节流读。
        bin_done 标志是必需的：全部读完后 bf 已 close，若不记状态，
        下一次调用会对已关闭的文件 read 而抛 ValueError。
        """
        nonlocal bf, bin_done
        if bin_done:
            return b""
        out = b""
        while len(out) < n:
            piece = bf.read(n - len(out))
            if piece:
                out += piece
                continue
            bf.close()
            try:
                bf = open(next(bin_iter), "rb")
            except StopIteration:
                bin_done = True
                break
        return out

    md5_conv = hashlib.md5()
    md5_bin = hashlib.md5()
    buf = bytearray()
    payload_total = 0
    compared = 0
    first_diff = None
    diff_bytes = 0

    seqs_n = 0
    seq_first = seq_last = None
    seq_min = seq_max = None
    dups = oos = 0
    seen_gapcheck = 0
    prev_seq = None
    cum_first = None
    cum_last = None
    last_payload_len = 0
    truncated = False

    def flush(final=False):
        """把 buf 里 8 的倍数长度的部分重排、与 bin 比对"""
        nonlocal buf, compared, first_diff, diff_bytes
        n = len(buf) if final else (len(buf) // 8) * 8
        if not final:
            n = (n // 8) * 8
        if n == 0:
            return
        chunk = bytes(buf[:n])
        del buf[:n]
        conv = reorder(chunk) if n % 8 == 0 else chunk
        md5_conv.update(conv)
        ref = bin_read(len(conv))
        md5_bin.update(ref)
        m = min(len(conv), len(ref))
        if conv[:m] != ref[:m]:
            for i in range(m):
                if conv[i] != ref[i]:
                    if first_diff is None:
                        first_diff = compared + i
                    diff_bytes += 1
        compared += m

    t0 = time.time()
    with open(pcap_path, "rb") as f:
        gh = f.read(24)
        if len(gh) < 24:
            sys.exit("pcap 文件头不完整")
        magic, = struct.unpack("<I", gh[:4])
        if magic not in (0xA1B2C3D4, 0xA1B23C4D):
            sys.exit(f"不认识的 pcap magic {magic:#x}")
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
                truncated = True
                break

            udp = pkt[PAYLOAD_OFF:]
            if len(udp) < DCA_HDR:
                continue

            seq, = struct.unpack("<I", udp[:4])
            cum = int.from_bytes(udp[4:10], "little")
            payload = udp[DCA_HDR:]

            seqs_n += 1
            if seq_first is None:
                seq_first, seq_min, seq_max = seq, seq, seq
                cum_first = cum
            else:
                if seq <= prev_seq:
                    oos += 1
                seq_min = min(seq_min, seq)
                seq_max = max(seq_max, seq)
            prev_seq = seq
            seq_last = seq
            cum_last = cum
            last_payload_len = len(payload)

            payload_total += len(payload)
            buf += payload
            if len(buf) >= CHUNK:
                flush()

    flush(final=True)
    # bin 可能比 pcap 载荷短（末包不满），把剩余 bin 也计入 md5
    while True:
        tail = bin_read(1 << 20)
        if not tail:
            break
        md5_bin.update(tail)
    if not bf.closed:
        bf.close()
    dt = time.time() - t0

    if truncated:
        sys.exit("发现截断包（caplen != wirelen）—— 抓包漏了 -s 0，数据不可用")

    span = seq_max - seq_min + 1 if seq_first is not None else 0
    missing = span - seqs_n

    print(f"L0 序号     : {seqs_n:,} 包  {seq_min} → {seq_max}  "
          f"丢失 {missing}  乱序 {oos}")
    print(f"L2 卡自报   : 首包累计 {cum_first:,} B（应为 0）  "
          f"末包累计+载荷 = {cum_last + last_payload_len:,} B")
    print(f"   实收载荷 : {payload_total:,} B  "
          f"差 {cum_last + last_payload_len - payload_total:,} B")
    print("-" * 66)
    print(f"L3 比对     : {compared:,} B  用时 {dt:.1f} s "
          f"({compared / dt / 1e6:.1f} MB/s)")
    print(f"   md5 转换 : {md5_conv.hexdigest()}")
    print(f"   md5 参照 : {md5_bin.hexdigest()}")

    if payload_total != bin_total:
        print(f"   注：pcap 载荷 {payload_total:,} B vs bin {bin_total:,} B，"
              f"差 {payload_total - bin_total:,} B（末包不满属正常）")

    ok = first_diff is None and diff_bytes == 0
    print("=" * 66)
    if ok and compared == bin_total:
        print(">>> L3 完整形式通过：逐字节完全一致 <<<")
    elif ok:
        print(f">>> 已比对的 {compared:,} B 完全一致，"
              f"但覆盖不全（bin 共 {bin_total:,} B）<<<")
    else:
        frame = first_diff // args.frame_bytes
        off_in = first_diff % args.frame_bytes
        print(f"!!! 不一致：差异 {diff_bytes:,} B，首个位置 {first_diff:,}")
        print(f"    = 第 {frame} 帧内偏移 {off_in:,}")
        print("    差异从头就有且量大 → 检查重排方向；"
              "集中在某处 → 按帧丢弃受影响帧")
    print("=" * 66)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
