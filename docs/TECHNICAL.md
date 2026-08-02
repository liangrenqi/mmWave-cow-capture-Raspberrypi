# 技术文档

结论尽量给出源码或文档依据。**推算与实测分开标注** —— 雷达参数错了通常
不报错、只是结果不对，事后极难发现。

源码路径（TI mmWave Studio）：
`ReferenceCode/DCA1000/SourceCode`，以下简称 CLI 源码。
SDK 路径：`mmwave_sdk_03_06_02_00-LTS/packages/ti`。

---

## 一、数据格式

### DCA1000 的 UDP 包结构

从 CLI 源码 `RF_API/recorddatarecv.cpp` 确认：

```
偏移 0  : UINT32   包序号
偏移 4  : 6 字节   累计已发送字节数
偏移 10 : 1456 B   ADC 载荷
合计 UDP 载荷 1466 字节
```

pcap 里剥到 ADC 载荷的偏移是 **52**：以太网 14 + IP 20 + UDP 8 = 42 到 UDP 载荷，
再跳 10 字节 DCA 包头。实测 linktype=1、ethertype=0x0800、IHL=20、UDP 长度 1474。

**1456 = 8 × 182**，即每包 728 个 int16 = 整 182 组四元组。
**包边界与四元组边界严格对齐**，所以逐包流式重排与整体重排数学等价 ——
这是 `verify_pcap_bin.py` 能流式处理而不吃内存的依据
（已实测：29 MB 数据用逐元素参照实现与切片实现按五种块大小各算一遍，全部一致）。

### 每帧字节数

```
每帧 = samples × RX数 × 2(I/Q) × 2 B × loops × TX数
生命体征 = 256 × 4 × 2 × 2 × 32  × 1 = 131,072
行为     = 128 × 4 × 2 × 2 × 120 × 3 = 737,280
```

脚本从 cfg 自动算（`profileCfg[10]` / `channelCfg[1]` 位掩码 / `frameCfg[3]` /
chirpCfg 条数），不硬编码。

### lane 重排：网线序 ≠ bin 序

JSON 里 `reorderEnable: 1`，CLI_Record 落盘时按 lane 重排：

```
网线上（pcap 载荷）  (a, b, c, d)
CLI_Record 写的 bin  (a, c, b, d)      每 4 个 int16 交换中间两个
```

**证据强度**：1 段 200 帧数据、13,107,200 个 int16 逐个比对零差异；
参照物是 TI CLI_Record 自己的输出，属独立实现。
后又在 3.93 亿样本（2.2 GB，跨 54 万个包边界）上复验通过。

自写 pcap→bin 转换**必须复现这个重排**，只拼接载荷会得到大小正确但
内容错乱的文件（实测 43% 字节不同）。

### RX 通道在 bin 里的布局

`adcbufCfg -1 0 1 1 1` 第 4 字段 `ChanInterleave = 1` = **非交织**
（`mmwave_sdk_user_guide.txt:993-998`，且 68xx 只支持 1）：

```
一个 chirp 内：[RX0: samples 个复数][RX1][RX2][RX3]
每 RX 段 = samples × 2(I/Q) × 2 B
```

实测印证（行为数据 400 chirp）：按此布局四通道 mean|x| = 214/227/251/326
**区分度明显**；若按"每复数轮询交织"解读则是 256/258/252/252，几乎相同 ——
那是把四路混匀的假象。**布局搞错会让检查静默地测错东西。**

### 文件分片

`maxRecFileSize_MB: 1024`，行为配置 2.2 GB 切成 3 片。
每片截到 **1456 的整数倍**边界，不跨包切断
（实测 1,073,741,760 / 1456 = 737,460.0 整数）。

bin 总量可能比"帧数 × 每帧字节"少 **128 B**：DCA1000 末包按 1456 对齐，
多出的零头不构成完整帧，CLI_Record 不落盘。属正常，判据留了 1456 B 容差。

---

## 二、完整性判据体系

### 为什么需要分层

**"bin 与 pcap 逐字节一致"不足以证明没丢数据。** 两者共享同一个上游
（内核网络栈）：CLI_Record 走 UDP socket，tcpdump 走 AF_PACKET。
包在到达内核之前就丢了的话，两边都没有它，比对依然完美一致。

| 层 | 判据 | 排除的环节 |
|---|---|---|
| **L0** | 包序号连续性 | 丢失/重复/乱序，可定位到帧 |
| **L1** 配置→卡 | 卡自报发送量 == 帧数 × 每帧字节 | 雷达少发帧、卡少收 LVDS |
| **L2** 卡→主机 | 卡自报发送量 == 主机实收载荷量 | 网线丢包、内核缓冲溢出 |
| **L3** 主机内部 | bin ≡ pcap 逐字节 | 写盘错误、重排实现错误 |

### L2 的关键：累计已发送字节数

每包偏移 4 起的 6 字节是 DCA1000 自己记的"我已发送多少字节"，
**该值来自卡侧，不受主机侧任何环节影响**。由此可**纯软件区分**：

- 卡自报 == 实收，但 < 应产生 → **卡侧丢包**
- 卡自报 > 实收 → **主机侧丢包**

封盒后看不见面板 LED，这是唯一替代手段。

### ⚠ 累计值与序号不随 start_record 归零

**它们是 FPGA 上电后持续累加的。** 上次采集结束在序号 1519121 /
累计 2,211,840,000，下次就从 1519122 / 2,211,840,000 接着数。

所以 L1/L2 必须取**增量**：

```
本次卡自报 = (末包累计 + 末包载荷) − 首包累计
```

同理"首包累计应为 0"**只对 FPGA 上电后第一次采集成立**，不能当判据。
（这曾导致误判：把两次累计之和当本次总量，报"主机侧丢包 2.2 GB"，
而实际增量精确为 0。）

### 七项判据

| 判据 | 数据来源 | 判 BAD 条件 |
|---|---|---|
| `l0l1l2_pcap` | pcap 逐包解析 | 丢失/重复/乱序 ≠ 0，或 L1/L2 增量不符 |
| `tcpdump_buffer` | tcpdump stderr | `dropped by kernel` > 0 |
| `kernel_udp_drops` | `/proc/net/udp` 第 13 列 | 采集期间增量 > 0 |
| `bin_total` | 文件大小 | 与期望差 ≥ 1456 B |
| `l3_bytewise` | `verify_pcap_bin.py` | 逐字节不一致 |
| `rx_channels` | `check_quality.py` | 任一 RX 幅度 < 中位数 15% |
| `csv` | CLI 的 LogFile.csv | — （`infinite` 模式不产出，恒 UNKNOWN） |

**内核 UDP drops 必须采集期间轮询** —— socket 只在 CLI_Record 运行时存在，
采集后 `/proc/net/udp` 那一行就消失，事后读永远是 None。
由 `DropMonitor` 每 0.5 秒轮询。

**文件大小不能当主判据**：CLI_Record 会零填充补掉丢失的包，
实测丢 4863 包后大小仍精确等于期望值。

### RX 通道检测为什么必要

L0–L3 验的**全是字节流完整性**。若某个 RX 射频前端坏了、那一路恒零，
**四层判据全部通过**。而行为识别靠 3TX×4RX = 12 元虚拟阵列估角度，
**少一路结果全错且不报错**。

判据用**相对比值**（低于通道中位数 15%）而非绝对阈值，因为死通道的特征是
与其它通道差一个数量级。已做反向测试：人为把 RX2 置零，正确报出异常。

它**不判**"是不是真实回波" —— 那由人和场景保证。满量程占用只作参考
（实测 5.1–5.9%，可能是室内无强反射体，也可能是增益偏低，光看数字分不出）。

---

## 三、三套缓冲是独立的

```
网卡 → [netdev_max_backlog]  内核协议栈入口（两路共用）
         ├→ UDP socket → [rmem_max]    → CLI_Record → bin
         └→ AF_PACKET  → [tcpdump -B]  → tcpdump    → pcap
```

### rmem_max

CLI 在 `RF_API/rf_api.cpp:701` 对数据口设 `SO_RCVBUF = SOCK_RECV_BUF_SIZE`
（`RF_API/defines.h:135` = `0x7FFFFFFF`，约 2 GB）。
**内核会静默截断到 `rmem_max`，不报错。** 默认 208 KB 在 7.4 MB/s 下
只够缓冲 29 毫秒。设为 256 MB。

### netdev_max_backlog

协议栈入口队列，在 UDP/AF_PACKET 分流**之前**，
所以同时影响 CLI_Record 和 tcpdump。默认 1000 个包在 5100 包/秒下只够 196 ms。
设为 5000。

### tcpdump -B（单位 KB）

意义是**能吸收多少秒的调度抖动**，必须随码率缩放。实测：

| 配置 | 码率 | -B | 余量 | 结果 |
|---|---|---|---|---|
| 生命体征 | 2.61 MB/s | 8 MB | 3.2 秒 | dropped **0** |
| 行为 | 7.23 MB/s | 8 MB | 1.1 秒 | dropped **22,510**（1.5%） |
| 行为 | 7.37 MB/s | **56 MB** | **8.0 秒** | dropped **0** |

`tcpdump_buf_kb()` 按 8 秒余量自动算。这也说明"pcap 并行开销可承受"
这个结论**只在生命体征码率下成立**，不能外推。

---

## 四、片上 L3 RAM 限制

**行为配置的 loops 必须是 120，不能是 128。** 这不是经验值，是两个约束夹出来的。

mmw demo 在 `sensorStart` 时为**点云检测流水线**分配两块内存，
都在 6843 片上 768 KB 的 L3 里。我们不用点云输出，
但 mmw demo 无条件分配，cfg 里没有开关能关掉
（`guiMonitor` 只控制 UART 吐什么，不影响分配）。

```
radarCube = numRangeBins × numDopplerChirps × numVirtualAnt × 4 B
            datapath/dpc/objectdetection/objdetdsp/src/objectdetection.c:1793
detMatrix = numRangeBins × numDopplerBins × 2 B                    同上 :1826
L3 总量   = 0xC0000 = 786,432 B
            demo/xwr68xx/mmw/xwr68xx_mmw_demo_dss.map:19（及 _mss.map:18）
两块对齐都是 2 B（cfarcaprocdsp.h:77 + objectdetection.c:99/108）⇒ 算式精确
```

派生量（`demo/utils/mmwdemo_rfparser.c`）：
`numRangeBins = pow2roundup(samples)` :884、
`numDopplerChirps = 总chirp数 / TX数` :887、
`numVirtualAntennas = TX × RX` :445。

硬约束：`numDopplerChirps` 必须是 **4 的倍数**
（`datapath/dpc/dpu/dopplerproc/src/dopplerprocdsp.c:703`）。

| loops | radarCube | +detMatrix | vs 786,432 | 4 的倍数 |
|---|---|---|---|---|
| **128** | 786,432 | 32,768 | **超 32,768** | 是 |
| 124 | 761,856 | 32,768 | 仍超 | 是 |
| 122 | 749,568 | 32,768 | 够 | **否 ✗** |
| **120** | 737,280 | 32,768 | **够，余 16,384** | 是 |
| 生命体征 32 | 131,072 | 16,384 | 18.8% | 是 |

128 loops 时 radar cube **正好吃满整个 L3**，缺的就是那个检测矩阵。

**实测**：128 → 前 30 条 Done，`sensorStart` 挂死；120 → 31 条全部 Done。
单变量对照，其余 30 条命令逐字相同。

**samples 128→96 完全无效** —— cube 用 `pow2roundup(samples)`，
96 和 128 都 round up 到 128；要到 64 才省内存，但那把带宽和距离分辨率砍半。

120 loops 的代价（**推算，未实测验证**）：多普勒积累 −6.25%，
速度分辨率 0.042 → 0.045 m/s，积累 SNR 约 −0.28 dB。
**距离分辨率、带宽、最大距离、最大速度、TDM 顺序全不变。**

### 失败时是死锁，不是返回错误码

```
MSS  MmwDemo_DPM_ioctl_blocking → Semaphore_pend(BIOS_WAIT_FOREVER)
     demo/xwr68xx/mmw/mss/mss_main.c:1735   ← 无超时
DSS  DPC detMatrix 分配失败 → ENOMEM
MSS  reportFxn 只在 DPM_Report_IOCTL 分支 post 信号量  mss_main.c:2631
     DPC 报错不走那分支 ⇒ 永远没人 post ⇒ CLI 任务永久阻塞
```

所以 `Error -1` 那行代码根本执行不到。**死锁后连 `sensorStop` 都不再被处理，
必须断电重启。** 精确的 heap 用量 printf 走 CCS 的 JTAG 控制台，串口看不到。

---

## 五、chCfg 只在首次 sensorStart 生效

```c
mss/mmw_cli.c:285-289
  if (memcmp(&gMmwMssMCB.cfg.openCfg.chCfg, &openCfg.chCfg,
             sizeof(rlChanCfg_t)) != 0)
      MmwDemo_debugAssert(0);      // ← line 288
```

TI 注释原话："the board needs to be reboot for the new configuration
to be applied."

`sensorStop` + `flushCfg` **不够** —— 只让 `sensorState` 回到 `STOPPED`，
不回 `INIT`。故行为（`channelCfg 15 7 0`）与生命体征（`15 1 0`）之间
切换必须断电。`check_chcfg()` 在启动任何进程前拦住。

---

## 六、TI CLI 的三个缺陷（已处置）

### start_record 缺 -q 就静默失败

`CLI_Control/cli_control_main.cpp:1656-1661`：

```c
if (gbCliQuietMode)
    "./DCA1000EVM_CLI_Record start_record %s -q &"              // 后台，可用
else
    "gnome-terminal -x ./DCA1000EVM_CLI_Record start_record %s"  // 需图形终端
```

Pi OS 无 gnome-terminal → `system()` 失败但**退出码仍为 0**。
症状：命令全"成功"、灯不闪、无数据。**必须加 `-q`**，
并用 `pgrep -f`（进程名超 15 字符，`-x` 匹配不到）确认真的起来了。

### pragma pack 污染导致 SIGBUS

`#pragma pack(1)` 开了不关（`Common/globals.h:63`、
`Common/rf_api_internal.h:95`），经 include 链污染到 `cUdpDataReceiver`，
实测 `alignof(cUdpDataReceiver) == 1`。类内嵌 `pthread_cond_t`/`pthread_mutex_t`
被压到未对齐地址；x86 容忍未对齐原子操作，**aarch64 glibc 直接 SIGBUS**。

三处修改见 `patches/`。**协议结构体一律不动** —— `rf_api.h` 里走网线的结构
必须逐字节紧凑（0xA55A 头 / 命令码 / 0xEEAA 尾），加对齐会插填充
使 FPGA 解析错乱且不报错。

判据：`alignof` 从 1 变 8；gdb 下五线程全起不崩；`ss -ulnp | grep 4098` 有监听。

### stop_record 必然超时

```
CLI_CMD_TIMEOUT_DURATION           = 7000 ms  (Common/globals.h:181)
SOCKET_THREAD_TIMEOUT_DURATION_SEC = 90 s     (Common/rf_api_internal.h:135)
```

`stop_record` 轮询共享内存等 CLI_Record 回报状态，**只等 7 秒**；
而 CLI_Record 的接收线程阻塞在 `recvfrom` 上，超时是 **90 秒**。
雷达停止出流后没有新包，CLI_Record 要 90 秒才醒来 ——
`stop_record` 7 秒就放弃、报 `-4068`，并 `DestroyShm()` 删掉共享内存，
CLI_Record 醒来后状态无处回写，一直挂着占住 4096/4098。

**7 秒 < 90 秒 ⇒ 数据流正常结束时必然触发。** 数据此时已全部落盘，
直接终止进程是安全的。`kill_record_proc()` + `cleanup_shm()` 自动处理。

### csv 为什么恒为 0 字节

汇总由 **CLI_Record 自己的进程**写：

```
StartRecordData → WriteRecordSettingsInLogFile()      rf_api.cpp:1634
                  → fopen(..._LogFile.csv, "w")       ← 只建空文件
采集完成 → STS_REC_COMPLETED → StopRecordProc_Callback()  cli_record_main.cpp:326
        → StopRecordData() :177
        → WriteInlineProcSummaryInLogFile()           rf_api.cpp:1882/1895
        → fprintf 统计 + fclose()                     :2396,2398
```

我们在 `-4068` 后立刻 SIGKILL 掉它，它还没走到 `:1882`。
Windows 上有 csv，正是因为那边 `stop_record` 能正常完成。

**但 `captureStopMode: "infinite"` 下这条路径根本不触发** ——
卡不判定"完成"、不发 `STS_REC_COMPLETED`。唯一替代是等 90 秒超时，
代价太大而收益（两个计数）已被 pcap 判据覆盖且更强。**故放弃 csv。**

---

## 七、配置要点

### 三处相比 6 月旧 cfg 的关键改动

- `lvdsStreamCfg -1 0 1 0`（原 `-1 1 1 1`）：关 HSI header，
  产出纯 ADC 的 `_Raw_n.bin`。原设置会产出 `_hdr_0ADC_n.bin`，
  使"每包 1456 字节无 padding"等既有结论失效
- JSON `dataLoggingMode: "raw"`（原 `"multi"`）：**必须与上一条配对**
  （`mmwave_sdk_user_guide.txt:655-658`）
- JSON `captureStopMode: "infinite"`：`"frames"` 会报 `-4064`
  "valid only in raw mode"，但代码 `cli_control_main.cpp:805` 实际判的是
  `!= MULTI_MODE` —— **TI 的提示文字与实现相反**，frames 只能配 multi，
  而 multi 会产出 `_hdr_` 文件使既有结论失效

### 改采集时长

必须同时改两处且两数相等（脚本会校验）：
cfg 的 `frameCfg` 第 4 字段、JSON 的 `framesToCapture`。

**不改帧周期** —— 生命体征 50 ms = 20 Hz 慢时间采样率是呼吸心率提取的基础。

### 行为 cfg 的 TDM 顺序

三条 `chirpCfg` 的 txEnable 掩码是 **1 / 4 / 2**，即 **TX0 → TX2 → TX1**。
组虚拟阵列必须按此顺序，**错了不报错只出错结果**。

### 丢包的六个环节

依据 `DCA1000EVM.txt` Table 17：

1. LVDS 缓冲溢出 → `EEPROM_RD_FAIL_LED`
2. DDR3 满 → `DDR_FULL_LED`
3. 以太网发送过快 → `FPGA_ERR_LED`，调 `packetDelay_us`（当前 25 μs ≈ 325 Mbps）
4. 主机内核缓冲溢出 ← 调 `rmem_max` / `netdev_max_backlog`
5. 用户态处理不及 ← tcpdump 的 `-B`
6. 磁盘写入跟不上

环节 1–3 属卡侧、4–6 属主机侧，可由 L1/L2 的累计字节字段区分。

---

## 八、性能实测

| 项 | microSD (ext4) | SSD (exFAT, USB) |
|---|---|---|
| 顺序写（绕过缓存） | 21 MB/s 稳定 | 247 MB/s |
| L3 逐字节比对 2.2 GB | 79.9 s (27.7 MB/s) | **36.7 s (60.3 MB/s)** |
| 容量 | 剩 18 GB（3 段） | 932 GB（约 200 段） |

需求：bin 7.37 + pcap 7.72 = **15.1 MB/s**。**microSD 本来也够**（余量 40%），
两次采集失败都与介质无关。SSD 的真实价值是容量与验证速度。

RX 通道检测：**0.85 秒**（抽样约 4000 chirp，每通道百万级样本），故永远开启。

**exFAT 风险（未验证，仅提示）**：无日志，牛场电池供电下异常掉电
可能损坏文件系统。若盘只在 Pi 上用，格 ext4 更安全，且不再需要 `-Z`。
