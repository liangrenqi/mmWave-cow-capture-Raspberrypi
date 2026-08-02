# 异常说明

按**症状**查。每条给出症状、根因、处置，以及是否影响数据。

这些故障有个共同特点值得先说：**大多数是静默的** ——
退出码正常、不报错，但数据已经废了。这就是判据体系存在的理由。

---

## 一、雷达侧

### 雷达串口完全静默

**症状**：`probe_radar.py` 回 0 字节；或下发 31 条命令**一条回显都没有**
（连必回 `Done` 的 `sensorStop` 也没有）。

**判据**：这是"串口不通"，不是配置问题。**此时任何 cfg 下发结果都不可信。**

按可能性排查：

1. **SOP 跳线不在 mode 4 (`001`)**。若在 `101`（烧写模式），
   XDS110 照样枚举、USB 一切正常，但 6843 不从 flash 启动固件 → UART 全静默。
   **这是最常见的原因。**
2. **5V 没接或没接好**。XDS110 是 **USB 供电**，
   所以"USB 枚举正常"完全不能说明 6843 有电。看板上电源 LED。
3. **雷达处于死锁**（见下一条）。断电重启。
4. 端口被占（见 ModemManager 一条）。`sudo fuser -v /dev/ttyACM0` 查。

**区分手段**：`probe_radar.py` 会打印 USB runtime power 状态。
若 `runtime_status=suspended` 是 autosuspend 问题；若显示 `active` 但仍静默，
就是固件侧。

**注意**：软件层的 USB 复位（`ioctl` reset、`deauthorize`、
`modprobe -r cdc_acm`）**都救不回来** —— 实测全部无效，因为它们只做协议层
复位，端口始终带电。只有真的断 5V 才能复位 6843。

---

### sensorStart 挂死（前 30 条正常）

**症状**：前 30 条命令都回 `Done`，第 31 条 `sensorStart` 之后
**既无 `Done` 也无 `Error`**，永久无响应。可能先打一句
`Debug: Init Calibration Status = 0x1ffe`（那是正常启动信息）。

**根因**：雷达**片上 L3 RAM** 装不下 radarCube + detMatrix。
不是采集卡缓存不够。典型触发：行为配置用了 128 loops。

失败时是**死锁**而非报错：MSS 卡在 `Semaphore_pend(BIOS_WAIT_FOREVER)`，
因为 DPC 报错不走 `DPM_Report_IOCTL` 分支、信号量永远没人 post。
详见 [TECHNICAL.md](TECHNICAL.md#片上-l3-ram-限制)。

**处置**：
1. **必须断电重启雷达** —— 死锁后连 `sensorStop` 都不再被处理
2. 行为配置用 **120 loops**（`frameCfg` 第 3 字段）
3. 想验证某个配置能否 `sensorStart`，用
   `send_cfg_only.py --cfg xxx.cfg --dry-run` 先算 L3 占用，再实发

**注意 `--dry-run` 只是算式预测**，实发才算实测。

---

### Exception: ./mss/mmw_cli.c, line 288.

**症状**：31 条命令都正常，`sensorStart` 回这一行。

**根因**：`channelCfg` 与上次 `sensorStart` 时不同。mmw demo 只在**首次**
`sensorStart` 应用 `chCfg`，改了就 `debugAssert`。
典型触发：在生命体征（`15 1 0`）和行为（`15 7 0`）之间切换。

**处置**：**拔 5V 断电，等 5 秒，插回。** `sensorStop`/`flushCfg` 都不够。

`capture_linux.py` 的 `check_chcfg()` 现在会在启动任何进程之前拦住并提示，
所以正常不会再撞上。若手工用 `send_cfg_only.py` 仍可能遇到。

---

### 串口开头有大段乱码或"is not recognized"

**症状**：第一条命令的回显里夹着数遍
`xWR68xx MMW Demo 03.06.02.00` 横幅，或
`'?`???...' is not recognized as a CLI command`。

**根因**：雷达上电/复位后的启动信息堆在串口缓冲里被一起读出；
或 ModemManager 探测时灌进去的 AT/QCDM 字节。

**影响**：无害。脚本开口前会 `reset_input_buffer()` 清掉，
且 `is_real_error()` 只在报错行提到我们刚发的命令名时才算真错。

---

## 二、Pi 侧环境

### 串口被占：Errno 16 Device or resource busy

**症状**：`[4] 发送 cfg` 步骤报
`[Errno 16] could not open port /dev/ttyACM0: Device or resource busy`。

**根因**：**ModemManager** 把每个新串口当调制解调器探测，
独占端口约 10 秒，并往雷达 CLI 里灌 AT/QCDM 指令
（日志：`[ttyACM0/probe] failed to parse QCDM version info command result: -5`）。
USB 一重新枚举它就抢 —— 所以断电重启后立刻跑脚本必踩。

**处置**：已由 `config/99-ti-radar.rules` 修好（让 MM 忽略这个 VID:PID）。
若规则丢了：

```bash
sudo cp config/99-ti-radar.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=tty
```

**验证方式不能只看 `udevadm test`** ——
要真的 USB 重新枚举后轮询端口占用才算实测。

---

### tcpdump 写出 24 字节空 pcap（exFAT）★ 最危险

**症状**：pcap 只有 **24 字节**（只有文件头、零个包），
tcpdump 只报一句 `Couldn't change ownership of savefile`，
**退出码仍是 0**。采完 5 分钟才发现全废。

**根因**：tcpdump 出于安全降权到 `tcpdump` 用户（shell 是 nologin），
而 exFAT 没有 Unix 所有权，整个挂载按 `uid=1000` 固定，该用户**无写权限**。

实测对照（各发 500 个 UDP 包）：

| 环境 | 结果 |
|---|---|
| microSD ext4，不加 `-Z` | 342 包 |
| exFAT，不加 `-Z` | **0 包**，24 B |
| exFAT，**`-Z root`** | **0 包** ← 挂载 `uid=1000`，root 也不是所有者 |
| exFAT，**`-Z pi`** | **500 包**，0 dropped |

**处置**：已加 `-Z PCAP_USER`（取自 `SUDO_USER`/`USER`）。
`-Z root` 无效这点反直觉，别踩。

---

### 采集丢包但看不出原因

**先看是哪个计数器报的** —— 四个计数器分属不同层：

| 计数器 | 含义 | 处置 |
|---|---|---|
| `check.kernel_udp_drops` FAIL | UDP socket 缓冲溢出（环节 4） | 调大 `net.core.rmem_max` |
| `check.tcpdump_buffer` FAIL | tcpdump 的 AF_PACKET 缓冲溢出（环节 5） | 调大 `-B` |
| L1 缺口（卡自报 < 应产生） | **卡侧**丢包（环节 1–3） | 调 `packetDelay_us`；看面板 LED |
| L2 缺口（卡自报 > 实收） | **主机侧**丢包（环节 4–6） | 见上两行 |

**`tcpdump_buffer` FAIL 而其它全过 ⇒ bin 是完好的**，只是 pcap 丢了。
实际发生过：丢 22,510 包（1.5%），而 `kernel_udp_drops = 0`、
bin 完整落盘。那次判 BAD 是 pcap 拖累的。

`-B` 已改成按码率自动算（8 秒余量），正常不会再溢出。

---

### 重启后丢包，之前明明是好的

**根因**：`sysctl -w` 不持久，重启即失效。
`rmem_max` 掉回 212992（208 KB，**差 1260 倍**）、
`netdev_max_backlog` 掉回 1000，而**不会有任何提示** ——
静默降级成丢包。曾因此丢 22,510 个包。

**处置**：已双保险 ——
`config/99-dca1000-capture.conf` 开机生效 +
`capture_linux.py` 的 `preflight()` 每次采集自检并**自动修正**。

若自检报 `[FAIL]`，手工执行提示里给的 `sysctl -w` 命令。

---

## 三、采集卡侧

### stop_record 报 -4068 Timeout Error

**症状**：每次采集收尾都报
`Stop Record command : Timeout Error! Couldnt read the record process status. [error -4068]`

**这是正常现象，不是故障。** TI 的设计缺陷：`stop_record` 只等 7 秒，
而 CLI_Record 的接收线程超时是 90 秒。数据流正常结束时**必然触发**。

**数据此时已全部落盘，不受影响。** 脚本会自动清掉残留的 CLI_Record 和共享内存。

---

### CLI_Record 没有启动起来

**症状**：`[3] 启动录制` 报 `CLI_Record 没有启动起来`。

**根因**：
1. 僵死共享内存段（上次异常退出留下）
2. `start_record` 忘了 `-q`（脚本已固定加上）
3. 上次的 CLI_Record 还占着 4096/4098

**处置**：脚本会自动 `cleanup_shm()` 和 `kill_record_proc()`。
若仍不行：

```bash
pkill -9 -f DCA1000EVM_CLI_Record
ipcs -m | grep 0xffffffff        # 找 key 为 0xffffffff、nattch=0 的段
ipcrm -m <shmid>
```

---

### bin 只有很小一部分（如 19.9 MB 而应 2.2 GB）

**症状**：pcap 完整（序号连续、L2 相符），但 bin 只有开头一小段，
且 bin 的 mtime 停在采集刚开始。

**根因**：CLI_Record 残留 / 共享内存脏，落盘几秒后就断了。
特征是**卡自报比应产生多出的量恰好等于 bin 大小** ——
上一轮残留进程使计数从上次位置续算。

**处置**：清残留后重采（脚本现在会在采集前自动清）。

---

### 首包累计值不为 0

**这不是异常。** 累计字节数和包序号是 FPGA 上电后持续累加的，
只有第一次采集才从 0 开始。判据已改成取**增量**。

若看到旧版本报"开头漏了包"，那是判据 bug，不是数据问题。

---

## 四、数据质量

### check.rx_channels FAIL

**症状**：某路 RX 幅度接近零或远低于其它三路。

**根因**：该 RX 射频前端故障，或天线连接问题。

**严重性高**：行为识别靠 12 元虚拟阵列估角度，**少一路结果全错**。
而 L0–L3 四层判据**全部会通过** —— 字节数照样精确、序号照样连续、
bin≡pcap 照样一致。这是唯一测不出的静默故障，所以单独做了这项检测。

**处置**：**立刻重采**。若重采仍然如此，是硬件问题，
检查天线连接与 `channelCfg` 的 rxChannelEn 掩码（应为 15 = 四路全开）。

---

### 满量程占用只有 5%

**不是故障，也判不了。** 实测行为与生命体征数据峰值都在
1685–1924 / 32767 ≈ 5–6%。

可能是室内无强反射体（很可能），也可能是增益配置偏低 ——
**光看数字分不出来**。要区分只能对着已知目标测：
金属板放 1 米，看距离维 FFT 峰值是否落在正确的 bin。

脚本只在峰值 < 200 时提示"确认雷达是否对着目标、天线是否遮挡"。

---

### csv 恒为 0 字节

**已知，已放弃，不影响判定。** `check.csv` 永远是 `UNKNOWN`。

根因是汇总由 CLI_Record 自己的进程写，而 `captureStopMode: "infinite"` 下
那条路径不被触发。详见 [TECHNICAL.md](TECHNICAL.md#csv-为什么恒为-0-字节)。

csv 那两个计数（out-of-sequence、zero-filled）已被 pcap 判据完全覆盖且更强 ——
pcap 能定位到帧、能分层区分卡侧/主机侧。

---

### L3 比对不一致

**症状**：`check.l3_bytewise` FAIL，报差异字节数与首个位置。

**排查顺序**：
1. **差异从头就有且量大（约 43%）** → lane 重排方向反了。
   检查 JSON 的 `reorderEnable` 是否为 1
2. **差异集中在某处** → 按帧边界定位，丢弃受影响的帧保住其余
3. **pcap 有截断包**（`caplen != wirelen`）→ 抓包漏了 `-s 0`，数据全废

pcap 载荷比 bin 多 **128 B** 属正常（末包对齐零头，CLI_Record 不落盘）。

---

## 五、快速诊断命令

```bash
# 雷达在应答吗
python3 scripts/probe_radar.py

# 端口被谁占了
sudo fuser -v /dev/ttyACM0

# 内核缓冲对不对
sysctl net.core.rmem_max net.core.netdev_max_backlog

# 采集卡通不通（不要用 ping，卡不响应 ICMP）
cd ~/Ti_radar/run && export LD_LIBRARY_PATH=$PWD:$LD_LIBRARY_PATH
./DCA1000EVM_CLI_Control query_sys_status dca1000_behavior.json

# 有残留进程吗
pgrep -af DCA1000EVM_CLI_Record

# 僵死共享内存
ipcs -m | grep 0xffffffff

# USB 供电/枚举
lsusb -d 0451:bef3
dmesg | grep -iE "ttyACM|over.?current" | tail

# 温度与节流
vcgencmd measure_temp && vcgencmd get_throttled     # 0x0 = 正常

# 补跑 L3
python3 scripts/verify_pcap_bin.py <session_dir> --frame-bytes 737280

# 单独查 RX 通道
python3 scripts/check_quality.py <session_dir> --cfg config/cow_behavior.cfg
```

---

## 六、几条容易误判的经验

| 现象 | 不是故障 |
|---|---|
| `stop_record` 报 `-4068` | TI 设计缺陷，数据已落盘 |
| bin 比期望少 128 B | 末包对齐零头 |
| 首包累计值不为 0 | FPGA 上电后的历史累计 |
| csv 为 0 字节 | infinite 模式不产出 |
| ping DCA1000 不通 | 卡不响应 ICMP，用 `query_sys_status` |
| 采集后 `/proc/net/udp` 读不到 | socket 已关闭，必须采集期间轮询 |
| DCA1000 灯采完后不闪 | 雷达已停止出流，封口缓冲期无数据 |
| 满量程只用 5% | 可能是场景，软件判不了 |
