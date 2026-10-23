#!/usr/bin/env python3
"""verify_pcap_bin.py 离线自检 —— 不需要雷达、不需要 DCA1000、不需要大文件

用小号合成数据复现 2026-10-23 的误判：
pcap 载荷经 lane 重排后切成 13 个 _Raw_N.bin（与 30 分钟行为段同样的片数），
分片编号到 10 以上时，字典序会把 _Raw_10 排到 _Raw_2 前面。

  1. 13 片、内容正确          → 必须通过（修复前：从第 3 片起整段不一致）
  2. 3 片、内容正确           → 通过（回归：不到 10 片的老路径不受影响）
  3. 13 片、改坏 _Raw_7 一个字节 → 必须不通过，首个差异落在被改的位置
  4. 13 片、缺 _Raw_5         → 必须拒绝运行（编号不连续）
  5. 文件名不符合 _Raw_<N>.bin → 必须拒绝运行
  6. sorted_bins 直接给出数字序 0..12

用法：
    python3 _test_verify_pcap_bin.py
"""

import array
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "verify_pcap_bin.py")
sys.path.insert(0, HERE)
import verify_pcap_bin as vpb

PKT = 1456
N_PKT = 130              # 共 130 包
SHARD_PKTS = 10          # 每片 10 包 → 13 片，与 30 分钟行为段片数相同
FRAME = PKT * 4


def payload(seq):
    """每包载荷各不相同，错位必然能被逐字节比出来"""
    a = array.array("h", [((seq * 7919 + i * 31) % 65521) - 32760
                          for i in range(PKT // 2)])
    return a.tobytes()


def wire_to_bin(buf):
    """(a,b,c,d) → (a,c,b,d)，与 vpb.reorder 独立实现，避免拿被测代码当标尺"""
    s = array.array("h")
    s.frombytes(buf)
    out = array.array("h", s)
    for k in range(0, len(s) - 3, 4):
        out[k + 1], out[k + 2] = s[k + 2], s[k + 1]
    return out.tobytes()


def make_session(root, shard_pkts=SHARD_PKTS):
    """写一个假段目录：一个 pcap + 若干 _Raw_N.bin，返回 (目录, 分片路径列表)"""
    d = tempfile.mkdtemp(dir=root)
    pcap = os.path.join(d, "cow_behavior_20261023_000000.pcap")
    stream = bytearray()
    cum = 0
    with open(pcap, "wb") as f:
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for seq in range(1, N_PKT + 1):
            p = payload(seq)
            udp = struct.pack("<I", seq) + cum.to_bytes(6, "little") + p
            frame = bytes(42) + udp
            f.write(struct.pack("<IIII", 0, 0, len(frame), len(frame)))
            f.write(frame)
            cum += len(p)
            stream += p
    binstream = wire_to_bin(bytes(stream))
    step = shard_pkts * PKT
    paths = []
    for i, off in enumerate(range(0, len(binstream), step)):
        p = os.path.join(d, f"cow_behavior_Raw_{i}.bin")
        with open(p, "wb") as f:
            f.write(binstream[off:off + step])
        paths.append(p)
    return d, paths


def run(d):
    r = subprocess.run([sys.executable, SCRIPT, d, "--frame-bytes", str(FRAME)],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return r.returncode, r.stdout.decode("utf-8", "replace")


def first_diff(out):
    m = re.search(r"首个位置 ([\d,]+)", out)
    return int(m.group(1).replace(",", "")) if m else None


def main():
    root = tempfile.mkdtemp(prefix="vpb_test_")
    fails = []

    def check(name, ok, detail=""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail and not ok else ""))
        if not ok:
            fails.append(name)

    try:
        # 1. 13 片正确数据必须通过
        d, paths = make_session(root)
        rc, out = run(d)
        check(f"1. {len(paths)} 片正确数据通过", rc == 0 and "逐字节完全一致" in out,
              out.strip().splitlines()[-2:] and " / ".join(out.strip().splitlines()[-3:]))

        # 演示旧行为：字典序确实是错的，确认本测试能复现原 bug
        lex = [int(re.search(r"_Raw_(\d+)", p).group(1)) for p in sorted(paths)]
        check("   (前提) 字典序在 13 片时错位", lex[:3] == [0, 1, 10], str(lex))

        # 6. sorted_bins 直接给数字序
        got = [int(re.search(r"_Raw_(\d+)", p).group(1)) for p in vpb.sorted_bins(d)]
        check("6. sorted_bins 返回 0..12", got == list(range(13)), str(got))

        # 3. 改坏 _Raw_7 的一个字节，必须报不一致且位置精确
        pos_in_shard = 1234
        with open(paths[7], "r+b") as f:
            f.seek(pos_in_shard)
            b = f.read(1)
            f.seek(pos_in_shard)
            f.write(bytes([b[0] ^ 0xFF]))
        rc, out = run(d)
        want = 7 * SHARD_PKTS * PKT + pos_in_shard
        fd = first_diff(out)
        check("3. 改坏 1 字节被查出且位置精确", rc != 0 and fd == want,
              f"rc={rc} 首差={fd} 期望={want}")

        # 4. 缺一片必须拒绝
        d4, paths4 = make_session(root)
        os.remove(paths4[5])
        rc, out = run(d4)
        check("4. 缺 _Raw_5 时拒绝运行", rc != 0 and "不连续" in out, out.strip()[-120:])

        # 5. 文件名不规范必须拒绝
        d5, paths5 = make_session(root)
        os.rename(paths5[3], os.path.join(d5, "cow_behavior_Raw_3_copy.bin"))
        rc, out = run(d5)
        check("5. 文件名不符合规则时拒绝运行", rc != 0 and "不符合" in out, out.strip()[-120:])

        # 2. 不到 10 片的老路径
        d2, paths2 = make_session(root, shard_pkts=50)
        rc, out = run(d2)
        check(f"2. {len(paths2)} 片正确数据通过（回归）", rc == 0 and "逐字节完全一致" in out,
              out.strip()[-160:])
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("=" * 50)
    if fails:
        print(f"失败 {len(fails)} 项: {fails}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
