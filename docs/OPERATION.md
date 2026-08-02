# 操作手册

从上电到取数的完整步骤。**每一步都有判据**，判据不达成就停在那里，
不要往下走 —— 否则多类问题混在一起无法排查。

## 一、硬件连接与跳线

| 项 | 设置 | 说明 |
|---|---|---|
| MMWAVEICBOOST SOP (P4/P5/P6) | **`001`**（mode 4） | 从 flash 运行。若在 `101`（mode 5，烧写模式），USB 照样枚举但雷达固件不启动，UART 全静默 |
| ISK 子板 S1 拨码 | OFF / ON / ON / OFF / OFF | DCA1000EVM 模式 |
| 雷达 USB | 接 Pi（供调试与串口） | XDS110 是 **USB 供电**，拔 5V 不会让它复位 |
| 雷达 5V 桶插座 | 接电源 | 这才是 6843 的供电 |
| DCA1000 网口 | 网线直连 Pi 的 eth0 | 普通直通线即可（千兆 auto-MDIX） |
| DCA1000 电源 | 5V | — |

**"断电重启雷达"指拔 5V 桶插头，等 5 秒，插回。** 只拔 USB 不会复位 6843。

## 二、开机后检查

```bash
# 1. 网口 IP（已写进 NetworkManager profile，开机自动配）
ip -4 addr show eth0        # 应为 192.168.33.30/24

# 2. 内核缓冲（已持久化到 /etc/sysctl.d/，开机自动生效）
sysctl net.core.rmem_max net.core.netdev_max_backlog
#   应为 268435456 和 5000

# 3. DCA1000 连通性 —— 不要用 ping，卡不响应 ICMP
cd ~/Ti_radar/run
export LD_LIBRARY_PATH=$PWD:$LD_LIBRARY_PATH
./DCA1000EVM_CLI_Control query_sys_status dca1000_behavior.json
#   应回 connected

# 4. 雷达是否在应答 ★ 最重要
python3 scripts/probe_radar.py
#   应回 12 行版本信息 + Done
```

第 2、3 步即使忘了也没关系 —— `capture_linux.py` 的第 0 步会自检并自动修正
内核缓冲。但**第 4 步必须做**：雷达不应答时下发 cfg 会得到全程静默，
那时任何结论都不可信（不是配置问题，是固件没跑）。

第 4 步返回 0 字节 → 见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md#雷达串口完全静默)。

## 三、采集

```bash
cd ~/mmwave-cow-capture
python3 scripts/capture_linux.py                    # 行为（默认）
python3 scripts/capture_linux.py --mode vitalsigns  # 生命体征
python3 scripts/capture_linux.py --loop 5           # 连续采 5 段
python3 scripts/capture_linux.py --yes              # 不等确认
python3 scripts/capture_linux.py --no-pcap          # 不抓 pcap（不推荐）
python3 scripts/capture_linux.py --no-verify        # 跳过 L3（现场省时间）
```

### ⚠ 切换波形必须先给雷达断电重启

生命体征和行为的 `channelCfg` 不同（`15 1 0` vs `15 7 0`），
而 mmw demo 只在首次 `sensorStart` 应用它。改了会触发 `debugAssert`，
串口回 `Exception: ./mss/mmw_cli.c, line 288.`

脚本会在**启动任何进程之前**拦住并提示，不会浪费一次采集。
断电后直接重跑即可（状态文件会自动更新）。

**同一份波形反复采集不需要断电。**

### 采集期间会经历

```
[0] 采集前自检     内核缓冲、落盘目录可写与余量、eth0 IP
[1] 配置 DCA1000   fpga → record
[2] 启动抓包       tcpdump，-B 按码率自动算
[3] 启动录制       start_record（必须 -q）
[4] 发送 cfg       31 条命令逐行下发，每条回 Done
[5] 等待           采集时长 + 10 秒封口缓冲
[6] 停止           stop_record（必然报 -4068，属正常）→ 停 tcpdump
[7] 整理           文件移入会话目录
[8] 判定           7 项判据 → GOOD / BAD / UNKNOWN
```

`stop_record` 报 `-4068 Timeout Error` 是 **TI 的设计缺陷，不是故障**，
数据此时已全部落盘。详见 [TECHNICAL.md](TECHNICAL.md#stop_record-必然超时)。

## 四、判断成功

### 三个层次

```bash
# 一眼看：目录名有没有 BAD_ 前缀
ls /mnt/pssd/Ti_radar_data/

# 一行看：总判定
grep "^verdict" <session_dir>/capture_meta.txt

# 逐项看
grep "^check\." <session_dir>/capture_meta.txt
```

### 全部通过的样子

```
verdict=GOOD
check.l0l1l2_pcap    = PASS   序号+卡自报+实收 全部相符
check.tcpdump_buffer = PASS   dropped by kernel = 0
check.kernel_udp_drops = PASS 增量 0
check.bin_total      = PASS   差 -128 B（末包对齐零头，正常）
check.l3_bytewise    = PASS   逐字节完全一致
check.rx_channels    = PASS   RX0=173 RX1=178 RX2=192 RX3=248
check.csv            = UNKNOWN（infinite 模式不产出，已放弃）
```

**csv 那项永远是 UNKNOWN，不影响结论。** 六项 PASS 即为成功。

### 批量筛选

```bash
# 所有会话的判定
grep -H "^verdict=" /mnt/pssd/Ti_radar_data/*/capture_meta.txt

# 只挑有问题的
grep -l "^verdict=BAD" /mnt/pssd/Ti_radar_data/*/capture_meta.txt

# 查某一项
grep -H "^check.rx_channels=" /mnt/pssd/Ti_radar_data/*/capture_meta.txt
```

## 五、现场检查清单（牛场用）

采集**前**：

- [ ] SOP 跳线 `001`，S1 拨码 OFF/ON/ON/OFF/OFF
- [ ] 5V 与 USB 都已连接，网线插好
- [ ] `probe_radar.py` 回 `Done`
- [ ] 落盘盘余量够（一段行为 4.4 GB，含 pcap）
- [ ] 若上一段是另一种波形 → **已断电重启**

采集**后**（每段都看）：

- [ ] `verdict=GOOD`，目录名无 `BAD_`
- [ ] `check.rx_channels=PASS` ← **四路都有信号,少一路数据废掉且不会报错**
- [ ] 记下牛号与备注（改脚本顶部 `COW_ID` / `NOTE`）

现场建议 `--no-verify`（省 40–80 秒/段），回实验室批量补跑 L3：

```bash
for d in /mnt/pssd/Ti_radar_data/*/; do
    python3 scripts/verify_pcap_bin.py "$d" --frame-bytes 737280
done
```

## 六、数据去向

```
<落盘目录>/Cow_<牛号>_<模式>_<起>_to_<止>/
    cow_behavior_Raw_0.bin        每片最大 1024 MiB，按 1456 边界切
    cow_behavior_Raw_1.bin
    cow_behavior_Raw_2.bin
    cow_behavior_<时间戳>.pcap    含每包序号与微秒时间戳
    cow_behavior_Raw_LogFile.csv  0 字节（已知，见技术文档）
    capture_meta.txt              判定 + 全部采集参数
```

**bin 和 pcap 都要保留** —— 验证期两份都是判据的一部分。
pcap 能转 bin，bin 无法还原 pcap，转换是不可逆的信息丢失。

改落盘位置：改 `config/dca1000_*.json` 的 `fileBasePath`。
**目录必须预先存在**，CLI 不会自动创建。
