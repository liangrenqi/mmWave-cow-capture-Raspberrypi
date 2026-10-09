# mmwave-cow-capture

树莓派 5 上的 IWR6843 + DCA1000EVM 原始 ADC 采集系统。
用于牛只行为识别与生命体征（呼吸/心率）的毫米波雷达数据采集，
并随段同步采集 Polar H10 心率带作为心跳真值。

从 Windows + mmWave Studio 的手工流程，移植成 Linux 下全自动、
带完整性判据的无人值守采集。目标场景是**电池供电、全封装的无线设备**
（牛场不允许拉电线、雷达距牛 0.5 m）。

## 硬件

| 部件 | 型号 |
|---|---|
| 雷达 | IWR6843ISK（60–64 GHz，3 TX / 4 RX） |
| 载板 | MMWAVEICBOOST |
| 采集卡 | DCA1000EVM（FPGA 2.9） |
| 主机 | Raspberry Pi 5 (2 GB)，Debian 12 bookworm，kernel 6.6.31 |
| 存储 | USB 移动固态硬盘（exFAT，实测 247 MB/s） |
| 心率带 | Polar H10（BLE，HR + ECG 130 Hz + ACC 200 Hz） |
| 遥控 | Genius 2.4G 演示器 + USB 喇叭（语音提示） |

雷达固件：xWR68xx MMW Demo **03.06.02.00**（SDK 03.06.02.00），常驻 flash 上电自启。

## 数据通路

```
IWR6843 ──LVDS──> DCA1000EVM ──千兆以太网/UDP 4098──> Pi 5
   ↑                                                   ├─ CLI_Record ─> .bin
   └── UART /dev/ttyACM0 逐行下发 .cfg                   └─ tcpdump    ─> .pcap

Polar H10 ──BLE──> Pi 5 ── h10_logger（每段一个独立进程）─> h10_*.log
```

**bin 和 pcap 双份都存**，这是完整性判据 L3 成立的前提：
CLI_Record 会用零填充补掉丢失的包，使文件大小无法反映丢包
（实测丢 4863 包后大小仍精确等于期望值）；pcap 保留每包序号与微秒时间戳，
可定位到帧、可分层区分卡侧/主机侧丢包。

H10 通知的到达时刻与 pcap 包时刻用同一个 Pi 时钟，这是把心率带接到 Pi 上的唯一理由
（手机 App 导出的时间轴只有秒级，无法与雷达做亚秒同步）。
**心率带的任何故障都不判废雷达段**，只记在 meta 里。

## 两种波形

| | 生命体征 | 行为识别 |
|---|---|---|
| 采样点/chirp | 256 | 128 |
| TX / RX | 1 / 4 | 3 (TDM) / 4 |
| loops | 32 | **120** |
| 帧周期 | 50 ms (20 Hz) | 100 ms |
| 每帧字节 | 131,072 | 737,280 |
| 码率 | 2.62 MB/s | 7.37 MB/s |
| 5 分钟数据量 | 750 MB | 2.2 GB |

行为配置的 loops 是 **120 而非 128**：128 会让雷达片上 L3 RAM 溢出，
`sensorStart` 直接死锁。详见 [docs/TECHNICAL.md](docs/TECHNICAL.md#片上-l3-ram-限制)。

## 快速开始

仓库**自带已编译的 ARM64 二进制**（`bin/`，含 SIGBUS 补丁），
clone 下来就能跑，不需要自己编译 TI 源码。

```bash
git clone <repo> && cd mmwave-cow-capture
chmod +x run.sh check.sh

./check.sh          # 环境自检：逐项告诉你还缺什么（不改系统）
./run.sh            # 采集（行为波形，含 7 项判据）
```

首次使用需做两件事（`check.sh` 会提示）：

```bash
# 1. 内核参数与 udev 规则
sudo cp config/99-dca1000-capture.conf /etc/sysctl.d/
sudo sysctl -p /etc/sysctl.d/99-dca1000-capture.conf
sudo cp config/99-ti-radar.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=tty

# 2. 改落盘位置（改成你的路径，目录必须先建好，CLI 不会自动创建）
#    编辑 config/dca1000_behavior.json 和 dca1000_vitalsigns.json
#    的 fileBasePath 两处
```

常用参数：

```bash
./run.sh --mode vitalsigns   # 换生命体征波形（切换前必须给雷达断电重启）
./run.sh --no-verify         # 跳过 L3 逐字节比对，省 40-80 秒（现场推荐）
./run.sh --loop 5            # 连续采 5 段
./run.sh --yes               # 不等确认直接开始
./run.sh --no-h10            # 本次不采心率带（或 CAPTURE_H10=0）
python3 scripts/probe_radar.py            # 单独确认雷达在应答
python3 scripts/verify_pcap_bin.py <dir> --frame-bytes 737280   # 补跑 L3
```

采心率带时 **Pi 的 WiFi 必须在 5 GHz**（2.4 GHz 下 BLE 建连大量失败），
且 H10 必须佩戴、电极湿润。详见 [docs/SETUP.md](docs/SETUP.md#76-心率带-h10)。

采集结束后看判定：

```bash
grep "^verdict" <session_dir>/capture_meta.txt
# verdict=GOOD

grep "^h10=" <session_dir>/capture_meta.txt
# h10=OK

# 批量筛出有问题的
grep -l "^verdict=BAD" /mnt/pssd/Ti_radar_data/*/capture_meta.txt
```

判废的会话目录会加 `BAD_` 前缀，**数据保留不删**。

封盒后的遥控采集（上电自启、POWER 启停程序、ENTER 采一段、ESC 中止）
见 [docs/OPERATION.md](docs/OPERATION.md#三遥控采集封盒后的主用方式)。

## 目录结构

```
run.sh              采集入口（设好环境变量再调 scripts/capture_linux.py）
check.sh            环境自检，clone 后先跑这个
bin/                已编译的 ARM64 二进制（CLI_Control / CLI_Record / libRF_API.so）
scripts/            采集与验证脚本
config/             波形 cfg、DCA1000 json、系统配置（sysctl / udev / systemd unit）
sounds/             语音提示 wav（15 个，文件名即内容）
dca1000_src/        已打补丁的 TI CLI 源码（想自己编译时用）
patches/            对 TI 原始源码的三处补丁及说明
docs/               文档
```

`bin/` 里的二进制是在 Pi 5 / Debian 12 / aarch64 上编译的，
已包含 `patches/` 的全部修改。若要自行编译见 [docs/SETUP.md](docs/SETUP.md)。

## 脚本

| 文件 | 用途 |
|---|---|
| `capture_linux.py` | 采集主脚本。自检 → 拉起心率带 → 配置 DCA1000 → 抓包 → 下发 cfg → 收尾 → 7 项判据 |
| `h10_session.py` | 每段一个 H10 进程：拉起、等首个 PMD、停止、日志归档、写 meta。异常全吞，不进判据 |
| `h10_logger.py` | H10 BLE 记录：只存原始字节 + 双时钟；断线重连、建连前删绑定 |
| `remote_daemon.py` | 开机自启的守护进程：POWER × 2 启停采集程序 |
| `remote_control.py` | 采集程序内的遥控状态机与语音后端（ENTER × 2 采一段、ESC 中止） |
| `probe_radar.py` | 雷达存活探针。0 字节即固件没跑，此时任何 cfg 结果都不可信 |
| `verify_pcap_bin.py` | L3 逐字节比对（pcap 重排后 ≡ bin），流式、跨分片 |
| `check_quality.py` | RX 通道死活检测。四层判据唯一测不出的静默故障 |
| `send_cfg_only.py` | 只发 cfg 不碰采集卡，用于验证 `sensorStart`。含 L3 占用预测 |
| `pcap_to_bin.py` | pcap → bin 转换（含 lane 重排），封盒后纯 pcap 方案的后处理 |
| `_test_*.py` | 自检脚本：音频、按键、输入设备、H10 会话（离线假 logger） |

## 文档

| 文档 | 内容 |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | 从零部署：编译 CLI、网络、内核参数、遥控与心率带、分阶段验证判据 |
| [docs/OPERATION.md](docs/OPERATION.md) | 操作手册：开机到取数的完整步骤、语音含义、现场检查清单 |
| [docs/TECHNICAL.md](docs/TECHNICAL.md) | 技术文档：数据格式、判据体系、参数推导、遥控、心率带、源码依据 |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | 异常说明：每种故障的症状、根因、处置 |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | 迄今所有修改及其依据 |
| [patches/](patches/) | TI CLI 源码的 ARM64 移植补丁（3 处，修 SIGBUS） |

## 依赖

```bash
sudo apt install python3-serial tcpdump
# 遥控与语音
sudo apt install python3-evdev alsa-utils
# 心率带
sudo apt install python3-dbus-fast bluez
pip install --user bleak==3.0.2
```

不需要 numpy —— `array.array` 的扩展切片赋值已足够快
（3.93 亿个 int16 重排约 6 秒）。

## 已验证状态

- 阶段 0–6 全部通过（2026-08-02）。最近一次行为满测：
  3000 帧 / 300 秒 / 7.33 MB/s / 2.2 GB，**六项判据零失败**
  （第七项 csv 在 `infinite` 模式下不产出，已知并放弃）。
- 长时采集：2026-08-06 连跑 5 段共 45 分钟 / 7.08 GB 全部 GOOD。
- 遥控 + 语音 + 开机自启：2026-09-06 实测通过。
- 心率带 H10 并入：2026-10-09 V2 重做 / V2-R 断线重连 / V3 冷启动 systemd + 中止，全部通过，
  雷达七项判据不受影响。

剩余：功耗与温度实测、GPIO 状态灯、封盒；
开机后 SSD 首次挂载掉线（疑供电）与冷启动雷达零回显两个现场问题待处置。

## 许可与来源

- 本仓库的脚本与文档：自研。
- `patches/` 是对 TI DCA1000EVM CLI 源码的修改，以 **patch 形式**提供，
  不含 TI 原始源码。使用前需自行从 TI mmWave Studio 获取
  `ReferenceCode/DCA1000/SourceCode` 并应用补丁。
- TI 官方文档（DCA1000EVM 用户指南、mmWave SDK 用户指南等）未包含在本仓库，
  请从 TI 官网获取。
