#!/usr/bin/env python3
"""RX 通道死活检测 —— 四层判据覆盖不到的那个缺口

## 为什么需要这一项

L0/L1/L2/L3 验的**全是字节流的完整性**：序号连续、字节数精确、bin≡pcap。
若某个 RX 的射频前端坏了、那一路恒为零或恒定，**四层判据全部通过**：
字节数照样精确、序号照样连续、bin 与 pcap 照样逐字节一致。

而行为识别要靠 3TX×4RX = 12 元虚拟阵列估角度，少一个 RX 结果就全错，
且是**静默错误** —— 回实验室处理时才发现，那趟牛场就白跑了。
所以这个检查必须**现场做**，它是"要不要立刻重采"的判据。

它**不判**"数据是不是真实回波"（那由人和场景保证，软件判不了：
幅度小可能是室内无强反射体，也可能是增益偏低，光看数字分不出）。
只判**通道是不是死的** —— 属于硬件故障检测。

## bin 里的通道布局（实测 + 文档双证）

`adcbufCfg -1 0 1 1 1` 第 4 个字段 ChanInterleave = 1 = **非交织**
（`refer/mmwave_sdk_user_guide.txt:993-998`，且 68xx 只支持 1）。
非交织 ⇒ 一个 chirp 内 4 个 RX 各占一段连续区域：

    [RX0: numAdcSamples 个复数][RX1][RX2][RX3]
    每 RX 段 = samples × 2(I/Q) × 2 B

实测印证（行为数据 400 chirp）：按此布局各通道 mean|x| 为
214 / 227 / 251 / 326，**区分度明显**；而若按"每复数轮询交织"解读，
四路变成 256/258/252/252 —— 几乎相同，是把四路混匀了的假象。
故布局 A 正确。

注意：读的是 CLI_Record 落盘后的 bin（已做 lane 重排），
不是 pcap 网线原序 —— 重排规则见 pcap_to_bin.py。

用法：
    python3 check_quality.py <session_dir> --cfg cow_behavior.cfg
"""

import argparse
import array
import glob
import os
import sys

# 判废阈值。依据阶段 4 实测底噪：幅度峰值 1690（满量程 5.2%）、
# 零值率 0.1289%、I/Q 同时为零 0.000214%。
# 死通道的特征是与其它通道差一个数量级，不是"绝对值多小"，
# 所以主判据用**相对比值**，绝对阈值只兜底防"全部通道都死"。
MIN_ABS_MEAN = 5.0       # 平均幅度低于此值视为该通道无信号
MIN_REL_RATIO = 0.15     # 某通道均值 < 通道中位数 × 此值 ⇒ 异常
MAX_ZERO_RATE = 5.0      # 单通道零值率上限（%）；一个丢包=728连续零


def pow2roundup(x):
    p = 1
    while p < x:
        p *= 2
    return p


def parse_cfg(path):
    out = {"tx_count": 0}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("%"):
                continue
            p = line.split()
            if p[0] == "frameCfg":
                out.update(loops=int(p[3]), frames=int(p[4]))
            elif p[0] == "profileCfg":
                out["samples"] = int(p[10])
            elif p[0] == "channelCfg":
                out["rx_count"] = bin(int(p[1])).count("1")
            elif p[0] == "chirpCfg":
                out["tx_count"] += 1
            elif p[0] == "adcbufCfg":
                out["chan_interleave"] = int(p[4])
    return out


def check_rx(bins, samples, rx_count, max_chirps=4000):
    """按 RX 分别统计幅度与零值率

    抽样而非全量：4000 个 chirp 已是百万级样本，统计上足够，
    且现场要快（全量读 2.2 GB 要几十秒，抽样 1 秒内完成）。
    从每个 bin 文件的多个位置取，避免只看开头。
    """
    per_rx = samples * 2                    # 每 RX 段的 int16 个数
    chirp_ints = per_rx * rx_count
    chirp_bytes = chirp_ints * 2

    acc = [{"sum": 0, "n": 0, "zero": 0, "peak": 0} for _ in range(rx_count)]
    got = 0

    for path in bins:
        size = os.path.getsize(path)
        n_chirp_total = size // chirp_bytes
        if n_chirp_total == 0:
            continue
        # 从 8 个等距位置各取一段，覆盖整个文件
        spots = 8
        per_spot = max(1, min(max_chirps // (spots * len(bins)),
                              n_chirp_total // spots or 1))
        with open(path, "rb") as f:
            for s in range(spots):
                off = (n_chirp_total * s // spots) * chirp_bytes
                f.seek(off)
                raw = f.read(chirp_bytes * per_spot)
                if len(raw) < chirp_bytes:
                    continue
                a = array.array("h")
                a.frombytes(raw[:(len(raw) // 2) * 2])
                n_ch = len(a) // chirp_ints
                for c in range(n_ch):
                    base = c * chirp_ints
                    for rx in range(rx_count):
                        seg = a[base + rx * per_rx: base + (rx + 1) * per_rx]
                        d = acc[rx]
                        for v in seg:
                            av = v if v >= 0 else -v
                            d["sum"] += av
                            if av > d["peak"]:
                                d["peak"] = av
                            if v == 0:
                                d["zero"] += 1
                        d["n"] += len(seg)
                    got += 1
    return acc, got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("--cfg", required=True)
    args = ap.parse_args()

    if os.path.isabs(args.cfg):
        cfg_path = args.cfg
    else:
        _here = os.path.dirname(os.path.abspath(__file__))
        for _d in (os.path.join(os.path.dirname(_here), "config"), _here, "."):
            _p = os.path.join(_d, args.cfg)
            if os.path.isfile(_p):
                cfg_path = _p
                break
        else:
            print(f"[ERROR] 找不到 {args.cfg}")
            return 2
    fc = parse_cfg(cfg_path)
    bins = sorted(glob.glob(os.path.join(args.session, "*_Raw_*.bin")))
    if not bins:
        print("[ERROR] 没有 _Raw_*.bin")
        return 2

    if fc.get("chan_interleave") != 1:
        print(f"[WARN] adcbufCfg ChanInterleave={fc.get('chan_interleave')}，"
              "本脚本的通道布局假设非交织(=1)，结果不可信")

    acc, got = check_rx(bins, fc["samples"], fc["rx_count"])
    if not got:
        print("[ERROR] 没读到完整 chirp")
        return 2

    means = [(d["sum"] / d["n"]) if d["n"] else 0.0 for d in acc]
    ordered = sorted(means)
    mid = ordered[len(ordered) // 2] if ordered else 0.0

    print(f"RX 通道检测（抽样 {got:,} 个 chirp，"
          f"每通道 {acc[0]['n']:,} 个 int16）")
    print(f"{'':<6}{'平均幅度':>10}{'峰值':>8}{'零值率':>9}   判定")
    problems = []
    for rx, d in enumerate(acc):
        m = means[rx]
        zr = 100.0 * d["zero"] / d["n"] if d["n"] else 0.0
        flags = []
        if m < MIN_ABS_MEAN:
            flags.append("幅度近零")
        if mid > 0 and m < mid * MIN_REL_RATIO:
            flags.append(f"仅为中位数的{100.0 * m / mid:.0f}%")
        if zr > MAX_ZERO_RATE:
            flags.append(f"零值率过高")
        verdict = "OK" if not flags else "**异常** " + " ".join(flags)
        if flags:
            problems.append(f"RX{rx}: {' '.join(flags)}")
        print(f"RX{rx:<5}{m:>10.1f}{d['peak']:>8}{zr:>8.3f}%   {verdict}")

    # 满量程占用：只作参考，不判废 —— 幅度小可能是场景问题也可能是增益，
    # 软件分不出，交给人判断
    peak_all = max(d["peak"] for d in acc)
    print(f"\n满量程占用  : {100.0 * peak_all / 32767:.1f}%"
          f"（峰值 {peak_all} / 32767）")
    if peak_all < 200:
        print("  [注意] 峰值极低，确认雷达是否对着目标、天线是否遮挡")

    print("=" * 60)
    if problems:
        print("!!! RX 通道异常 —— 数据不可用于角度估计，建议立即重采")
        for p in problems:
            print(f"    {p}")
        return 1
    print(">>> 4 个 RX 通道均有信号 <<<")
    return 0


if __name__ == "__main__":
    sys.exit(main())
