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

用法：
    python3 pcap_to_bin.py capture.pcap out.bin
    python3 pcap_to_bin.py capture.pcap out.bin --verify ref_Raw_0.bin
    python3 pcap_to_bin.py capture.pcap out.bin --no-reorder   # 保留网线原序
"""

import argparse
import array
import hashlib
import struct
import sys

PAYLOAD_OFF = 42          # Ethernet 14 + IP 20 + UDP 8
DCA_HDR = 10              # UINT32 seq + 6B cumulative byte count
FRAME_BYTES_DEFAULT = 131072


def read_pcap(path):
    """返回 (载荷字节, 序号列表, 是否有截断包)"""
    payload = bytearray()
    seqs = []
    truncated = False

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
                truncated = True     # snaplen 截断，数据不可用
                break
            udp = pkt[PAYLOAD_OFF:]
            if len(udp) < DCA_HDR:
                continue
            seqs.append(struct.unpack("<I", udp[:4])[0])
            payload += udp[DCA_HDR:]

    return bytes(payload), seqs, truncated


def reorder_lanes(data):
    """(a,b,c,d) → (a,c,b,d)，每 4 个 int16 一组

    2 lane LVDS 的交织顺序。尾部不足 4 个样本的原样保留。
    """
    s = array.array("h")
    s.frombytes(data)
    n = (len(s) // 4) * 4
    out = array.array("h", bytes(len(s) * 2))
    for i in range(0, n, 4):
        out[i] = s[i]
        out[i + 1] = s[i + 2]
        out[i + 2] = s[i + 1]
        out[i + 3] = s[i + 3]
    for i in range(n, len(s)):
        out[i] = s[i]
    return out.tobytes()


def report_gaps(seqs):
    if not seqs:
        return
    uniq = set(seqs)
    span = max(len(uniq), seqs[-1] - seqs[0] + 1)
    missing = span - len(uniq)
    dups = len(seqs) - len(uniq)
    oos = sum(1 for a, b in zip(seqs, seqs[1:]) if b <= a)
    print(f"包数     : {len(seqs):,}  序号 {seqs[0]} → {seqs[-1]}")
    print(f"丢失     : {missing}   重复: {dups}   乱序: {oos}")
    if missing:
        gaps = sorted(set(range(seqs[0], seqs[-1] + 1)) - uniq)
        print(f"丢失序号(前 20): {gaps[:20]}")
        print("注意：丢包会使后续样本整体错位，按帧边界定位并丢弃受影响的帧")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pcap")
    ap.add_argument("out")
    ap.add_argument("--verify", metavar="REF.bin",
                    help="与 CLI_Record 的 bin 逐字节比对")
    ap.add_argument("--no-reorder", action="store_true",
                    help="不做 lane 重排，保留网线原始顺序")
    ap.add_argument("--frame-bytes", type=int, default=FRAME_BYTES_DEFAULT,
                    help=f"每帧字节数，默认 {FRAME_BYTES_DEFAULT}（妊娠配置）")
    args = ap.parse_args()

    payload, seqs, truncated = read_pcap(args.pcap)
    if truncated:
        sys.exit("发现截断包（caplen != wirelen）—— 抓包时漏了 -s 0，数据不可用")

    report_gaps(seqs)

    data = payload if args.no_reorder else reorder_lanes(payload)
    with open(args.out, "wb") as f:
        f.write(data)

    print(f"\n输出     : {args.out}")
    print(f"大小     : {len(data):,} B")
    frames = len(data) / args.frame_bytes
    print(f"帧数     : {frames:.4f}"
          + ("" if frames == int(frames) else "  ← 非整数，尾部有不完整帧"))
    print(f"重排     : {'否（网线原序）' if args.no_reorder else '是 (a,b,c,d)→(a,c,b,d)'}")

    if args.verify:
        ref = open(args.verify, "rb").read()
        same = data == ref
        print(f"\nmd5 输出 : {hashlib.md5(data).hexdigest()}")
        print(f"md5 参照 : {hashlib.md5(ref).hexdigest()}")
        if same:
            print(">>> 逐字节完全一致 <<<")
        else:
            print(f"不一致：输出 {len(data):,} B，参照 {len(ref):,} B")
            n = min(len(data), len(ref))
            diff = [i for i in range(n) if data[i] != ref[i]]
            if diff:
                print(f"差异字节数 {len(diff):,}，首个位置 {diff[0]}")
                print("若差异很多且从头就有，检查 --no-reorder 是否用反了")
            sys.exit(1)


if __name__ == "__main__":
    main()
