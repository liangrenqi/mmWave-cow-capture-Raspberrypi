# 从零部署

在一台新的 Raspberry Pi 5 上把采集链路搭起来。
**每阶段判据不达成就停在那里** —— 否则多类问题混在一起无法排查。

环境：Debian 12 bookworm / kernel 6.6.31 / Python 3.11.2 / tcpdump 4.99.3。

---

## 0. 依赖

```bash
sudo apt install python3-serial tcpdump build-essential dos2unix
sudo usermod -aG dialout $USER      # 串口权限，需重新登录
```

不需要 numpy（`array.array` 已足够快，3.93 亿 int16 重排约 6 秒）。

---

## 1. 编译 TI CLI

TI 的二进制不入库（有 license），需自行获取源码并打补丁。

源码来自 mmWave Studio：`ReferenceCode/DCA1000/SourceCode`

```bash
cd <SourceCode 根目录>
dos2unix makefile                    # makefile 是 CRLF，否则 missing separator
patch -p1 < <repo>/patches/Common_Osal_Utils_osal.h.patch
patch -p1 < <repo>/patches/Common_rf_api_internal.h.patch
patch -p1 < <repo>/patches/RF_API_recorddatarecv.h.patch
make                                 # 必须在 SourceCode 根目录跑
```

**必须在根目录跑 make** —— RF_API 的规则不带 INCFLAGS，
靠源文件里的 `../Common/...` 相对路径。

补丁修的是什么、为什么 ARM64 上必须打，见 [patches/README.md](../patches/README.md)。

**判据**：产出三个文件，且**每个二进制都实际运行过**：

```bash
export LD_LIBRARY_PATH=$PWD:$LD_LIBRARY_PATH   # 用户指南写的 $pwd 是笔误
./DCA1000EVM_CLI_Control --version
./DCA1000EVM_CLI_Record  --version              # ← 这个也要跑！
```

**"产出三个文件"不是充分判据** —— 曾经冒烟测试只跑了 `CLI_Control`，
而 SIGBUS 缺陷全在 `CLI_Record` 身上。跨架构移植时判据必须是"都跑过"。

告诉采集脚本 CLI 在哪（若不在 `~/Ti_radar/run`）：

```bash
export DCA1000_CLI_DIR=/path/to/SourceCode/Release
```

---

## 2. 网络（点对点直连）

网线直连 Pi 与 DCA1000，普通直通线即可（千兆 auto-MDIX）。

```bash
ip link                             # 先查网卡名！Pi 5 用 macb 驱动，可能叫 end0
```

若不是 `eth0`，改 `scripts/capture_linux.py` 的 `ETH_IF`。

固定 IP（写进 NetworkManager，开机自动配）：

```bash
sudo nmcli connection modify "Wired connection 1" \
    ipv4.method manual ipv4.addresses 192.168.33.30/24
sudo nmcli connection up "Wired connection 1"
```

**不配网关、不开 DHCP** —— 点对点专线。
IP 分配：FPGA `192.168.33.180` / 主机 `192.168.33.30`，
配置口 4096 / 数据口 4098（`DCA1000EVM.txt:655-657`）。

**判据**（不要用 ping —— **DCA1000 不响应 ICMP**）：

```bash
cd <CLI 目录> && export LD_LIBRARY_PATH=$PWD:$LD_LIBRARY_PATH
./DCA1000EVM_CLI_Control query_sys_status <repo>/config/dca1000_behavior.json
# 应回 connected
```

---

## 3. 内核参数与 udev

```bash
sudo cp config/99-dca1000-capture.conf /etc/sysctl.d/
sudo sysctl -p /etc/sysctl.d/99-dca1000-capture.conf

sudo cp config/99-ti-radar.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=tty
```

两者分别解决什么：

- **sysctl**：CLI 请求 `SO_RCVBUF = 0x7FFFFFFF`（约 2 GB），
  内核**静默截断到 `rmem_max`**。默认 208 KB 在 7.4 MB/s 下只够 29 ms。
  忘了配不会报错，只会静默丢包 —— 所以 `capture_linux.py` 也会自检并自动修正。
- **udev**：让 ModemManager 别碰雷达串口。它会独占端口约 10 秒
  并往 CLI 灌 AT/QCDM 字节。**牛场设备上电自启必踩这个竞态。**

**判据**：

```bash
sysctl net.core.rmem_max net.core.netdev_max_backlog
#   268435456 / 5000

# 拔插雷达 USB 后立刻轮询，端口应始终空闲
sudo fuser -v /dev/ttyACM0          # 应无输出
```

---

## 4. 落盘位置

```bash
sudo mkdir -p /mnt/pssd/Ti_radar_data     # 或你的路径
```

改 `config/dca1000_*.json` 两份的 `fileBasePath`。
**目录必须预先存在，CLI 不会自动创建。**

exFAT 的注意事项：tcpdump 降权后无法写入，脚本已用 `-Z` 处理。
**若盘只在 Pi 上用，建议格 ext4** —— 有日志、掉电更安全、且不需要 `-Z`。

**判据**：

```bash
python3 -c "
import importlib.util as u, os
s=u.spec_from_file_location('c','scripts/capture_linux.py')
m=u.module_from_spec(s); s.loader.exec_module(m)
jc=m.load_json_cfg(); fc=m.parse_cfg(m.PROFILE_CFG)
print(m.preflight(fc['frame_bytes']*1000.0/fc['period_ms'],
      base_path=jc['base_path'], need_bytes=fc['frames']*fc['frame_bytes']))
"
# 全部 [OK] 且最后一行 True
```

---

## 5. 雷达侧

烧固件（在 Windows 上用 UniFlash 做）：
`mmwave_sdk_03_06_02_00-LTS/packages/ti/demo/xwr68xx/mmw/xwr68xx_mmw_demo.bin`

跳线：

| 项 | 烧写 | 运行 |
|---|---|---|
| SOP (P4/P5/P6) | mode 5 = `101` | **mode 4 = `001`** |

ISK 子板 S1 拨码（DCA1000EVM 模式）：OFF / ON / ON / OFF / OFF

固件常驻 flash、上电自启 —— 这正是无线自主设备需要的
（对比 Studio 的 SOP mode 2 每次下载到 RAM）。

**判据**：

```bash
python3 scripts/probe_radar.py
# 应回 12 行版本信息 + Done
```

0 字节 → SOP 跳线不在 `001`，或 5V 没接。
注意 **XDS110 是 USB 供电**，所以"USB 枚举正常"完全不能说明 6843 有电。

---

## 6. 分阶段验证（按顺序，不要跳）

| 阶段 | 内容 | 判据 |
|---|---|---|
| 1 | 编译 CLI | 三个二进制都实际运行过 |
| 2 | 链路 | `query_sys_status` → connected |
| 3 | 串口 | `probe_radar.py` → Done |
| 4 | 短采集（200 帧） | bin = 200 × 每帧字节，精确 |
| 5 | 生命体征满测（6000 帧） | 六项判据 PASS |
| 5.5 | 行为 cfg 能否 sensorStart | `send_cfg_only.py` 31 条全 Done |
| 6 | 行为满测（3000 帧） | 六项判据 PASS |
| 7 | 功耗与温度 | 待做 |

阶段 4 用短配置先跑通链路：把 cfg 的 `frameCfg` 第 4 字段和 json 的
`framesToCapture` 都改成 200（**两数必须相等**，脚本会校验）。

阶段 5.5 单独存在的理由：行为配置是 3 天线 120 loops，
与曾经卡住的配置同量级，可能走不到测带宽那步就先卡在 `sensorStart`。
先用 `send_cfg_only.py`（不碰采集卡）验证，省时且干净。

---

## 7. 常见部署问题

| 症状 | 原因 |
|---|---|
| `make` 报 missing separator | makefile 是 CRLF，先 `dos2unix` |
| 编译过但 CLI_Record 一跑就崩 | 补丁没打全（SIGBUS） |
| 命令全"成功"但没数据、灯不闪 | `start_record` 缺 `-q` |
| ping DCA1000 不通 | 正常，卡不响应 ICMP |
| 串口 permission denied | 用户不在 `dialout` 组，且需重新登录 |
| `Errno 16 busy` | ModemManager 抢了，见第 3 步 |
| pcap 只有 24 字节 | exFAT + tcpdump 降权，需 `-Z` |
| 采集报"落盘目录不存在" | CLI 不自动创建，先 mkdir |

更多见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。
