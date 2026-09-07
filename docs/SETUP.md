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

遥控采集额外需要 evdev 与 ALSA 工具（不用遥控功能可跳过）：

```bash
sudo apt install python3-evdev alsa-utils
sudo usermod -aG input,audio $USER  # 读 /dev/input/event*、用 USB 喇叭
```

**装 evdev 优先用 apt 而不是 `pip install --user`。**
pip 用户级安装会落到 `~/.local/lib/python3.11/site-packages/`，
只有该用户可见 —— 若 systemd unit 里写了别的 `User=`，会
`ModuleNotFoundError` 且服务无限重启（本项目实测踩过，见 CHANGELOG）。
本机当前就是 pip 装的，故 unit 必须 `User=pi`。

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
| 6.5 | 遥控 + 语音 + 开机自启 | 见第 7 步，四项实测 |
| 7 | 功耗与温度 | 待做 |

阶段 4 用短配置先跑通链路：把 cfg 的 `frameCfg` 第 4 字段和 json 的
`framesToCapture` 都改成 200（**两数必须相等**，脚本会校验）。

阶段 5.5 单独存在的理由：行为配置是 3 天线 120 loops，
与曾经卡住的配置同量级，可能走不到测带宽那步就先卡在 `sensorStart`。
先用 `send_cfg_only.py`（不碰采集卡）验证，省时且干净。

---

## 7. 遥控采集与开机自启

封盒后饲养员不碰电脑，全程遥控器 + 语音。**装之前先跑通阶段 1-6。**

### 7.1 ⚠ 先屏蔽 POWER 键的关机功能（这一步不能跳）

`HandlePowerKey` 的 systemd 默认值是 **`poweroff`**，而系统自带 udev 规则
（`/usr/lib/udev/rules.d/70-power-switch.rules`）给**所有** `ID_INPUT_KEY=1`
的输入设备打 `power-switch` 标签，不区分来源。
实测遥控器的 event7 与板载 event0 **都在 logind 监听范围内** ——
不处理的话按两下 POWER 就把 Pi 关机，正在进行的采集全废。

```bash
sudo mkdir -p /etc/systemd/logind.conf.d
sudo tee /etc/systemd/logind.conf.d/99-radar-remote.conf <<'EOF'
[Login]
HandlePowerKey=ignore
HandlePowerKeyLongPress=ignore
EOF
```

**判据**（需重启后才生效，`systemd-logind` 实测 `CanReload=no`）：

```bash
sudo systemd-analyze cat-config systemd/logind.conf | grep HandlePowerKey
#   应为 ignore
```

别指望桌面会话那把 `handle-power-key block` 锁 —— 它在命令行启动、
桌面崩溃、或本服务比桌面先起来时都不存在。

**副作用**：Pi 板载电源按钮短按也不再关机，改用 `sudo poweroff`。
桌面菜单的关机走 D-Bus，不受影响。

### 7.2 测出遥控器按钮的实际键码

**HID 声明的"能报哪些键"不等于按钮实际发什么码。**
本项目用的 Genius 演示器（`27a7:2501`），其 event9 的 capabilities 里有
整套 163 个键（含 KEY_S / KEY_E / 甚至 KEY_POWER），但按钮只发几个。
照 capabilities 猜的后果是"按遍所有按钮都没反应"且不报错。

```bash
python3 scripts/_test_input_monitor.py      # 逐个按，看实际键名与来源节点
python3 scripts/_test_remote_keys.py        # 验证双击、去抖、长按忽略
```

本项目实测结果（换遥控器需重测）：

| 物理按钮 | 键码 | 节点 | 用途 |
|---|---|---|---|
| 电源 | `KEY_POWER` | event7 | 启停采集程序 |
| 确认 | `KEY_ENTER` | event9 | 开始采集（双击） |
| 返回 | `KEY_ESC` | event9 | 紧急中止 |

键位改动处：`scripts/remote_control.py` 的 `START_KEY` / `STOP_KEY`，
`scripts/remote_daemon.py` 的 `POWER_KEY`（填 `KEY_` 之后的部分，小写）。

### 7.3 音频

```bash
cat /proc/asound/cards        # 方括号里的名字才是 CARD= 要填的
python3 scripts/_test_audio.py --gen        # 生成 11 个占位音（纯合成）
python3 scripts/_test_audio.py              # 按实际顺序播一遍，验证喇叭
```

**设备必须按名字选不按编号** —— 代码固定 `plughw:CARD=Device`。
不给 `-D` 会落到 card 0 的 HDMI 上，**没声音且不报错**；
而 card 号随插拔顺序变（HDMI 插不插、换 USB 口都可能变），名字不变。
换了别的喇叭改 `remote_control.AudioNotifier.DEVICE`。

wav 放 `sounds/`，**文件名即内容**，换真人语音同名覆盖即可，
代码不用改（16-bit PCM / 44.1 kHz / 单声道）。

### 7.4 装服务

```bash
sudo cp config/radar-remote.service /etc/systemd/system/
sudo systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/radar-remote.service   # ★ 必做
sudo systemctl enable radar-remote        # 只需一次，永久生效
sudo reboot                               # 顺带让 7.1 的 logind 配置生效
```

`systemd-analyze verify` 那步别省 —— **放错段的键会被静默忽略**。
本项目实测把 `StartLimitIntervalSec` 写进 `[Service]`（它属于 `[Unit]`），
结果重启限流失效、崩溃时无限重启，就是靠这条命令发现的。

**unit 里 `User=pi` 不能改成 root**，两个独立理由：

1. evdev 是 pip 用户级安装，root 看不见 → `ModuleNotFoundError`，
   实测服务崩溃重启 30 次
2. ★ `capture_linux.py` 用 `SUDO_USER or USER or "pi"` 决定 `tcpdump -Z`
   的降权目标。以 root 直接运行 ⇒ `-Z root`，而 exFAT 挂载按 `uid=1000`
   固定，实测 **写出 0 个包、24 字节空 pcap，退出码仍是 0**

权限够用（已逐条验证）：脚本对需提权的三件事单独调 sudo
（`sysctl` / `tcpdump` / `kill`），`sudo -n` 非交互免密全部可用；
`pi` 在 `input(102)` 读 event、`audio(29)` 用喇叭、`dialout(20)` 开串口。
unit 必须显式写 `SupplementaryGroups=input audio dialout …`，
否则 systemd 只给主组，恰好丢掉这三个。

### 7.5 判据（四项，全部实测）

重启后**不敲任何命令**：

- [ ] 听到"设备就绪" ← 自启成功
- [ ] `POWER` × 2 → "采集程序启动" → `ENTER` × 2 → "采集命令下发" →
      （10-20 秒）→ "采集开始" → 采完 → "通过"
- [ ] 采集中按 `ENTER` 被锁定（防误触）、按 `POWER` 被拒绝（播"忙"）
- [ ] 采集中按 `ESC` → 中止并归档到 `ABORT_` 目录，
      **之后立刻还能重新开始**（验证游离文件没卡住下次采集）

```bash
systemctl is-active radar-remote        # active
python3 scripts/remote_daemon.py --check   # 设备 + logind + 音频一次查完
```

最后那项判据最关键：原 Ctrl-C 路径不归档，游离文件会留在落盘根目录
导致下次采集被残留检查挡死 —— 封盒后没屏幕，现象就是"遥控器坏了"。

---

## 8. 常见部署问题

| 症状 | 原因 |
|---|---|
| `make` 报 missing separator | makefile 是 CRLF，先 `dos2unix` |
| 编译过但 CLI_Record 一跑就崩 | 补丁没打全（SIGBUS） |
| 命令全"成功"但没数据、灯不闪 | `start_record` 缺 `-q` |
| ping DCA1000 不通 | 正常，卡不响应 ICMP |
| 串口 permission denied | 用户不在 `dialout` 组，且需重新登录 |
| `Errno 16 busy` | ModemManager 抢了，见第 3 步 |
| pcap 只有 24 字节 | exFAT + tcpdump 降权，需 `-Z`；服务跑成 root 也会 |
| 采集报"落盘目录不存在" | CLI 不自动创建，先 mkdir |
| 按遥控器没反应 | 键码没实测、守护进程没跑、或两进程抢设备 |
| 按键在终端里回显字符 | 没 grab 独占（evdev 读事件不消费它） |
| 按两下 POWER 关机了 | 7.1 没做 |
| 服务无限重启 | `User=root` 找不到 evdev，或限流键放错段 |
| 没声音 | 落到 HDMI 上了，须 `plughw:CARD=<名字>` |

更多见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。
