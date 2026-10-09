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
| 2.4G 遥控器接收器 | 任意 USB 口 | Genius 演示器，`27a7:2501`。枚举出 4 个 event 节点 |
| USB 喇叭 | 任意 USB 口 | ALSA 名 `Device`，**按名字选卡不按编号**（编号会变） |
| Polar H10 心率带 | 无线（BLE），无需接线 | 按 MAC `24:AC:AC:11:CC:D4` 连接（广播不带名字）。**必须佩戴、电极湿润**，干放不广播 |

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

# 5. WiFi 频段（采心率带时必查）
nmcli -t -f ACTIVE,SSID,FREQ dev wifi | grep ^yes
#   FREQ 应为 5xxx MHz；2.4 GHz（24xx）下 H10 建连大量失败
```

第 2、3 步即使忘了也没关系 —— `capture_linux.py` 的第 0 步会自检并自动修正
内核缓冲。但**第 4 步必须做**：雷达不应答时下发 cfg 会得到全程静默，
那时任何结论都不可信（不是配置问题，是固件没跑）。

第 4 步返回 0 字节 → 见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md#雷达串口完全静默)。
冷启动后见过一次零回显（DCA1000 同时亮 `LVDS_PATH_ERR` 红灯），
**重上 ICBOOST 的 5V 电**即恢复，只重上 DCA1000 无效。

第 5 步是 2026-09-28 实测：2.4 GHz WiFi 开着时 BLE 建连 15/15 失败（HCI 0x3e），
改 5 GHz 后正常。Pi 开机若连不上 5G，NetworkManager 会自动回落到 2.4G，
所以每次都要看，不能只看一次。

## 三、遥控采集（封盒后的主用方式）

**饲养员不需要碰电脑、不需要屏幕。** 上电即待命，全程只用遥控器 + 语音提示。

### 两层遥控，分工不要混

```
POWER × 2   →  启动 / 退出采集程序      （守护进程 remote_daemon.py 管）
ENTER × 2   →  采一段                   （采集程序 capture_linux.py 管）
ESC         →  紧急中止本段
```

`POWER` 管"程序开关"，`ENTER`/`ESC` 管"采集开关"。
所以饲养员只需记两件事：**开机听到"守护进程已启动"→ POWER 双击 → ENTER 双击采一段。**

### 完整流程与语音

| 操作 | 语音 | 说明 |
|---|---|---|
| 上电开机 | "守护进程已启动" | 守护进程自启完成（`daemon_ready.wav`），**什么命令都不用敲** |
| `POWER` × 2 | "采集程序启动" | 拉起采集程序 |
| `ENTER` × 2 | "采集命令下发" | **立刻响**，告诉你命令收到了 |
| （约 1–3 秒） | "心率带已连接" | H10 连上；45 秒仍未连上则播一次"心率带未连接" |
| （等 15–30 秒） | "采集开始" | 雷达真正开始发射 |
| （采集时长） | — | 此时 `ENTER` 被锁定，防误触 |
| 采完 | "通过" / "判废" | 7 项判据的结论 |
| （1.5 秒后） | "可以采集" | 回到待命，可采下一段 |
| `ESC`（采集中） | "已中止" | 归档到 `ABORT_` 目录，不跑判据 |
| `POWER` × 2（空闲时） | "采集程序退出" | 回到守护待命 |
| `POWER` × 2（采集中） | "采集进行中，请先按返回键中止" | **被拒绝**，先按 `ESC` |
| （采集中心率带掉线） | "心率带断开" | 自动重连，**雷达照常采集，本段不作废** |
| （重连成功） | "心率带已重连" | — |

**"采集命令下发"和"采集开始"之间那 15–30 秒是正常的** ——
要跑 preflight、拉起心率带并等它出第一包 ECG/ACC（最多 10 秒，实测 6–9 秒）、
配 FPGA、起 tcpdump、起 CLI_Record、下发 29 条 cfg、等 `sensorStart` 裁决。
听到第一句就说明按上了，**不要重复按**。

心率带的四条语音只是提示"去看一下心率带"，**都不影响雷达判定**。
心率带的情况只记在 `capture_meta.txt` 的 `h10=` 一行里，见下文第五节"心率带这段全不全"。

### 开机自启已装好，日常无需任何命令

```bash
systemctl is-active radar-remote      # 应回 active
systemctl is-enabled radar-remote     # 应回 enabled
journalctl -u radar-remote -f         # 看实时日志（排查时才用）
```

这三条都是**只读查看**，等于打开任务管理器看一眼，可以永远不用。
真正让它常驻的是 `systemctl enable`（已执行过，永久生效）。

### 为什么采集中不让退出程序

硬杀采集程序会留下两个后果：雷达**没收到 `sensorStop`，会继续发射到帧数跑完**；
游离文件留在落盘根目录，**下次采集被残留检查挡死**（封盒后没屏幕，
现象就是"遥控器坏了"）。
故按 `POWER` 退出前会先读采集程序的状态，**只有空闲时才放行**。
要中止当前这段请按 `ESC`。

### 音频文件

`~/mmwave-cow-capture/sounds/` 下 15 个 wav（采集 7 + 守护 4 + 心率带 4）。**文件名即内容**，
换成自己录的同名覆盖即可，代码不用改（16-bit PCM / 44.1 kHz / 单声道）。

```bash
python3 scripts/_test_audio.py            # 按实际顺序播一遍，验证喇叭
python3 scripts/_test_audio.py --gen      # 生成占位音（纯合成，无需录音）
```

采集程序用中高音区，守护进程用低音区，心率带用最高音区（1300–2000 Hz 短促音）——
刻意区分，听一下就知道是哪一层在说话。
注意 `daemon_ready.wav`（"守护进程已启动"，整台设备待命）与
`ready.wav`（"可以采集"，采集程序空闲）**语义不同**，录音措辞别都念"就绪"。
`--gen` 只生成缺失的文件，已录好的真人语音不会被覆盖（`--force` 才覆盖）。

### 遥控器按键与实际键码

**HID 声明的"能报哪些键"不等于按钮实际发什么码** ——
`event9` 的 capabilities 里有整套 163 个键，但按钮只发下面这些：

| 物理按钮 | 键码 | 节点 |
|---|---|---|
| 电源 | `KEY_POWER` | event7 |
| **确认** | **`KEY_ENTER`** | event9 |
| **返回** | **`KEY_ESC`** | event9 |
| 音量 ± | `KEY_VOLUMEUP/DOWN` | event8 |
| 方向键 | `KEY_UP/DOWN/LEFT/RIGHT` | event9 |

换遥控器后必须实测，不能照 capabilities 猜：

```bash
python3 scripts/_test_input_monitor.py    # 逐个按，看实际键名
python3 scripts/_test_remote_keys.py      # 验证双击/去抖/长按忽略
python3 scripts/remote_daemon.py --check  # 自检设备、logind、音频
```

### ⚠ POWER 键已被屏蔽系统关机功能

`HandlePowerKey` 的 systemd 默认值是 `poweroff`，而遥控器 POWER 键所在节点
带 udev 的 `power-switch` 标签 —— 不处理的话**按两下就把 Pi 关机，采集全废**。

已装 `/etc/systemd/logind.conf.d/99-radar-remote.conf` 置为 `ignore`。
**副作用：Pi 板载电源按钮短按也不再关机**（同样带那个标签）。
关机改用 `sudo poweroff`；桌面菜单的关机走 D-Bus，不受影响。

**断电前尽量先 `sudo poweroff`。** 直接拔电会让 exFAT 卷标记"未正常卸载"
（2026-10-09 已出现 2 次），积累下去有丢文件的风险。

## 四、手动采集（调试用）

```bash
cd ~/mmwave-cow-capture
python3 scripts/capture_linux.py                    # 行为（默认）
python3 scripts/capture_linux.py --mode vitalsigns  # 生命体征
python3 scripts/capture_linux.py --loop 5           # 连续采 5 段
python3 scripts/capture_linux.py --yes              # 不等确认
python3 scripts/capture_linux.py --no-pcap          # 不抓 pcap（不推荐）
python3 scripts/capture_linux.py --no-verify        # 跳过 L3（现场省时间）
python3 scripts/capture_linux.py --mode vitalsigns --remote   # 遥控模式
python3 scripts/capture_linux.py --remote --no-audio          # 遥控但静音
python3 scripts/capture_linux.py --no-h10           # 本次不采心率带
CAPTURE_H10=0 python3 scripts/capture_linux.py      # 同上，环境变量形式
```

心率带默认随段采集。启动时屏幕上"心率带H10"一行会显示开/关。
H10 不在身边或没佩戴时不必关：它连不上只会播一次"心率带未连接"，雷达照常采。
但那样每段都会多等 10 秒（等首个 PMD 超时），整轮不用时建议关掉。

**调试遥控功能时先停守护进程**，否则两个进程会抢同一个输入设备：

```bash
sudo systemctl stop radar-remote
# 调试完
sudo systemctl start radar-remote
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
    心率带         拉起 h10_logger（独立进程），等首个 PMD，最多 10 秒
[1] 配置 DCA1000   fpga → record
[2] 启动抓包       tcpdump，-B 按码率自动算
[3] 启动录制       start_record（必须 -q）
[4] 发送 cfg       31 条命令逐行下发，每条回 Done
[5] 等待           采集时长 + 10 秒封口缓冲
[6] 停止           给心率带发停止信号（不等）→ stop_record（必然报 -4068，属正常）→ 停 tcpdump
[7] 整理           文件移入会话目录，心率带日志一起移入
[8] 判定           7 项判据 → GOOD / BAD / UNKNOWN（心率带不参与）
```

自检失败的段不会拉起心率带，所以不会产生多余的心率带文件。

`stop_record` 报 `-4068 Timeout Error` 是 **TI 的设计缺陷，不是故障**，
数据此时已全部落盘。详见 [TECHNICAL.md](TECHNICAL.md#stop_record-必然超时)。

## 五、判断成功

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

### 心率带这段全不全

```bash
grep -E "^h10=|^h10_status|^h10_pmd_(lead|tail)_sec" <session_dir>/capture_meta.txt
```

| `h10=` | 含义 |
|---|---|
| `OK` | 全程连着，有 ECG/ACC |
| `PARTIAL（意外断开 n 次）` | 中途掉线过，已自动重连，掉线期间无数据 |
| `PARTIAL（只有 HR，无 ECG/ACC）` | 连上了但 PMD 流没起来 |
| `MISSING（H10 缺失）` | 整段没连上（没戴、电极干、WiFi 在 2.4G） |
| `MISSING（H10 进程未启动）` | logger 没拉起来，看 `h10_note` |
| `DISABLED` | 本次用 `--no-h10` 或 `CAPTURE_H10=0` 关了 |

`h10_pmd_lead_sec` / `h10_pmd_tail_sec` 是心率带数据覆盖雷达窗口的余量，
**都应 ≥ 0**（正常约 +7 / +10 秒）。负数表示雷达开头或结尾有一截没有心率带。

### 批量筛选

```bash
# 所有会话的判定
grep -H "^verdict=" /mnt/pssd/Ti_radar_data/*/capture_meta.txt

# 只挑有问题的
grep -l "^verdict=BAD" /mnt/pssd/Ti_radar_data/*/capture_meta.txt

# 查某一项
grep -H "^check.rx_channels=" /mnt/pssd/Ti_radar_data/*/capture_meta.txt
```

## 六、现场检查清单（牛场用）

采集**前**：

- [ ] SOP 跳线 `001`，S1 拨码 OFF/ON/ON/OFF/OFF
- [ ] 5V 与 USB 都已连接，网线插好
- [ ] 遥控器接收器与 USB 喇叭已插上
- [ ] **上电后听到"守护进程已启动"** ← 守护进程自启成功的唯一现场判据
- [ ] `probe_radar.py` 回 `Done`（封盒后做不了，改用上一条）
- [ ] 落盘盘余量够（一段行为 4.4 GB，含 pcap）
- [ ] 若上一段是另一种波形 → **已断电重启**
- [ ] H10 已佩戴、电极湿润
- [ ] Pi WiFi 在 5 GHz（开机热点校时后再确认一次）

开机后的**第一段**若听到"采集失败"，可能是移动 SSD 首次挂载时掉线
（2026-10-09 见过一次，疑供电，未坐实）。程序已回到待命、无残留，**直接再按一次 ENTER × 2**。

采集**后**（每段都看）：

- [ ] **听到"通过"而不是"判废"** ← 封盒后的主要判据
- [ ] 本段只听到一次"心率带已连接"，没有"断开"/"未连接"
- [ ] `verdict=GOOD`，目录名无 `BAD_`（回实验室核）
- [ ] `check.rx_channels=PASS` ← **四路都有信号,少一路数据废掉且不会报错**
- [ ] 记下牛号与备注（改脚本顶部 `COW_ID` / `NOTE`）

**封盒后没有屏幕，语音就是唯一的现场反馈。** 听到"判废"就该立刻重采，
而不是等回实验室才发现。

现场建议 `--no-verify`（省 40–80 秒/段），回实验室批量补跑 L3：

```bash
for d in /mnt/pssd/Ti_radar_data/*/; do
    python3 scripts/verify_pcap_bin.py "$d" --frame-bytes 737280
done
```

## 七、数据去向

```
<落盘目录>/Cow_<牛号>_<模式>_<起>_to_<止>/
    cow_behavior_Raw_0.bin        每片最大 1024 MiB，按 1456 边界切
    cow_behavior_Raw_1.bin
    cow_behavior_Raw_2.bin
    cow_behavior_<时间戳>.pcap    含每包序号与微秒时间戳
    cow_behavior_Raw_LogFile.csv  0 字节（已知，见技术文档）
    capture_meta.txt              判定 + 全部采集参数 + 心率带一节
    h10_<时间戳>.log              心率带原始字节 + 双时钟（离线解析）
    h10_<时间戳>.log.stdout.txt   心率带进程的终端输出，排查用

<落盘目录>/_h10_staging/          采集中心率带日志先写这里，段结束移走，平时应为空
<落盘目录>/H10_ORPHAN/            没有段目录可放的心率带日志（准备阶段就失败、上次崩溃遗留）
```

心率带日志在正常、`BAD_`、`ABORT_` 三种段目录里都有。
`H10_ORPHAN/` 里的文件不删，可以手动对照时间归档或丢弃。

**bin 和 pcap 都要保留** —— 验证期两份都是判据的一部分。
pcap 能转 bin，bin 无法还原 pcap，转换是不可逆的信息丢失。

改落盘位置：改 `config/dca1000_*.json` 的 `fileBasePath`。
**目录必须预先存在**，CLI 不会自动创建。
