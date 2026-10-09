# 修改记录

从 Windows + mmWave Studio 手工流程移植到 Pi 5 自动采集的全部改动。
按时间倒序。每条尽量给出**依据**（源码行号或实测数据），
并区分**实测**与**推算**。

---

## 2026-10-09

### 并入 Polar H10 心率带（已实测通过，commit bd6bd34）

人体实验与牛场都要逐拍心跳真值（牛场也给牛戴 H10）。手机 App 导出的时间轴
各自从 0 起算、只有秒级绝对时刻，无法与雷达做亚秒同步，所以改由 Pi 直接收 H10：
通知到达时刻与 pcap 包时刻用同一个 `CLOCK_REALTIME`。

**用户 09-30 定的原则：心率带必须独立于雷达采集进程。** 落成：

- 新增 `scripts/h10_session.py`：每段一个 H10 进程。自检通过后用
  `timeout -k 20 -s INT` 拉起 `h10_logger.py`，`start_new_session` 自成进程组；
  段末 SIGINT 不等，`finish` 时把日志移进该段目录（正常 / `BAD_` / `ABORT_` 都移），
  没有段目录归 `H10_ORPHAN/`
- **H10 的任何故障都不进 checks、不影响 verdict**，只在 meta 写 `h10=` 一行与 `h10_*` 一节
- 雷达开采前**等首个 PMD，最多 10 秒**（`FIRST_PMD_WAIT`）。依据：V2 首轮实测
  H10 拉起到首个 PMD 6.3–6.8 s，与 `sensorStart` 撞在一起，覆盖余量 −0.3 ~ +0.5 s；
  等之后稳定在 +6.9 ~ +7.0 s
- 关闭：`--no-h10` 或 `CAPTURE_H10=0`
- `capture_linux.py` 的雷达部分原样拆成 `_capture_radar()`，
  **采集时序（fpga→record→start_record→cfg→stop）一字未动**

新入库 `scripts/h10_logger.py`（只存原始字节 + 双时钟，解析离线做），加四个开关，默认全关 = 旧行为：

- `--reconnect`：断线不退出，重扫重连，全部记在同一个文件
- `--remove-bond`：每次建连前 `bluetoothctl remove`。**已绑定连接上的首次 PMD 启动
  固定晚约 30 s**（09-28–29 实测 22 次无例外），删绑定后 0.3 s。机理在 H10 固件，不再深挖
- `--parent-pid`：父进程没了自行停止，防孤儿
- `--exit-grace`：收到停止后的收尾上限，超时 fsync 后自退
- EV 改名：结束时 Pi 主动断开记 `disconnected_at_end`，`disconnected` 只表示**意外**断开
  （V1 教训：旧版两者都记 `disconnected`，"意外断开 = 0"的判据字面上满足不了）

语音新增 4 个：`h10_connected` / `h10_lost` / `h10_reconnected` / `h10_missing`，
用最高音区与雷达提示区分（`sounds/` 现共 15 个 wav）。断线时雷达照常采集。

新增 `scripts/_test_h10_session.py`：假 logger 离线自检 7 个场景，Pi 上走真信号全过。

### 验证（2026-10-09，iPhone 热点 5 GHz，判据测前写定、测后不改）

| 轮次 | 内容 | 结果 |
|---|---|---|
| V2 首轮 | 3 段 × 5 min | **不过**：V2-1 判据写错（csv 本来就是 UNKNOWN）；V2-6 覆盖余量 −0.3 s → 改代码"等首个 PMD" |
| V2 重做 | 3 段 × 5 min，条件同首轮 | V2-1..9 全过，雷达七项判据零影响 |
| V2-R | `bluetoothctl disconnect` 后重连、再走出范围 | 断开到重连后首帧 8.1 s / ≤ 18.5 s，删绑定 rc=0，语音正确 |
| V3 | 冷启动 → systemd → 一段 ESC 中止 + 一段正常 → POWER 退出 | 7 项全过，ABORT 段 H10 日志正确归档，退出后无残留 |

### ★ 前提：Pi WiFi 必须在 5 GHz

09-28 实测：2.4 GHz WiFi 开着时 BLE 建连 15/15 失败（HCI 0x3e），改 5 GHz 后正常。
10-08 测试 2 不过就是因为 Pi 开机时 5G 连不上、静默回落到 2.4G（建连失败 373 次）；
10-09 换 5G 重跑全过（失败 2 次）。

### 修掉的分类 bug

自己 killpg 之后 rc 也是 −9，被误记成"被 timeout 终止"。已加 killed 标志区分（Pi 上实测发现）。

### 配置

`cow_vitalsigns.cfg` 与 `dca1000_vitalsigns.json` 帧数改回 600（30 s），两处一致，供下一轮实验 1。

### 验证期间暴露、与 H10 无关的两个现场问题（未解决）

1. **开机后第一段 SSD 首次挂载掉线**：采集程序首次访问 `/mnt/pssd` 触发 automount，
   同一秒 SSD 在 USB 3 上掉线，`os.makedirs` 报 `Errno 19`，播"采集失败"；再按一次就好。
   嫌疑是供电（`throttled=0x50000`、每段开始都报欠压、`usb_max_current_enable=0`），
   但掉线那一秒没有欠压记录，**未坐实**。exFAT"未正常卸载"出现 2 次（直接拔电所致）。
2. **Pi 冷启动后雷达串口零回显**（见过 1 次）：29 条命令无回显，DCA1000 亮 `LVDS_PATH_ERR`。
   只重上 DCA1000 无效，**重上 ICBOOST 5V 电**恢复。根因（上电时序）未查。

文档同步：OPERATION / SETUP / TECHNICAL（新增第十节）/ TROUBLESHOOTING（新增第六节）/ README。
`daemon_ready.wav` 的实际内容是"守护进程已启动"，文档里原写的"设备就绪"一并改掉。

---

## 2026-09-06

### 遥控采集 + 语音提示 + 开机自启（已实测通过）

封盒后饲养员不碰电脑、不需要屏幕：上电即待命，全程遥控器 + 语音。

**两层遥控**（分工不要混）：

```
remote_daemon.py（开机自启，永不退出，独占 event7）
    POWER × 2  →  启动 / 退出 capture_linux.py --remote

capture_linux.py --remote（子进程，独占 event8/9）
    ENTER × 2  →  采一段
    ESC        →  紧急中止本段
```

新增 `scripts/remote_control.py`（按键状态机 + 音频后端）、
`scripts/remote_daemon.py`（守护进程）、`config/radar-remote.service`、
四个自检脚本、`sounds/` 下 11 个 wav。
`capture_linux.py` 改 8 处，**采集时序（fpga→record→start_record→cfg→stop）
一字未动** —— 七项判据的基础不能碰。

### ★ 修掉原 Ctrl-C 路径的两个真 bug

这两条在加遥控之前就存在，是"提前中止"这个需求把它们暴露出来的。

**1. 不发 `sensorStop`，雷达根本不停。**
原路径只发 `stop_record`，而那是对 **DCA1000** 说的 —— 雷达完全不知情，
按 cfg 帧数继续发射到跑完。妊娠 54000 帧那种配置，第 1 分钟中止的话
雷达还要**空转 44 分钟**，真正停它的是下次采集开头 cfg 里那句 `sensorStop`。
这段空转还会全部计进阶段 7 要测的功耗。
（`check_field_limits` 的注释里早写明了这个行为，只是此前没有中止需求。）

**2. 不归档，游离文件卡死下次采集 ★ 更严重。**
`_Raw_*.bin` / `.pcap` / `.csv` 留在落盘根目录 → **下次采集被残留检查
挡回并 return False**。牛场里就是"饲养员按了停止，之后按开始永远无声失败"，
而封盒后没屏幕看不到那条报错。
2026-08-06 帧数回绕那次实测踩到过（当时记录："手动 Ctrl-C 收场，
留下三个游离文件没归档"）。

现在 **Ctrl-C 与 ESC 走同一个 `abort_capture()`**：
发 `sensorStop` → 等 3 秒 → `stop_record` → 停 tcpdump → 清进程 →
归档到 `ABORT_Cow_*` → 写 `verdict=ABORTED` 的精简 meta（不跑 L3/RX）。
`capture()` 的 except 也从 `except Exception` 扩到含 `KeyboardInterrupt`
—— 准备阶段（起 tcpdump / 发 cfg）按 Ctrl-C 同样会留下文件和进程，
`_run` 里那个倒计时的 except 覆盖不到。

**数据是移走归档不是删除**，沿用"判废也只加前缀、数据保留不删"的既有规则。

### ★ `User=root` 会静默毁掉 pcap（unit 的最大坑）

第一版 unit 写了 `User=root`，两条都栽：

1. **evdev 装在 pi 的用户目录里，root 看不见**
   （`/home/pi/.local/lib/python3.11/site-packages/evdev/`，
   是 pip 用户级安装，`dpkg -l python3-evdev` 查不到）。
   实测服务崩溃重启 **30 次**，日志全是 `ModuleNotFoundError`。
2. **`PCAP_USER` 会解析成 root** —— `capture_linux.py:90` 是
   `SUDO_USER or USER or "pi"`，以 root 直接运行时没有 `SUDO_USER`、
   `USER=root` ⇒ `tcpdump -Z root`。而 exFAT 挂载按 `uid=1000` 固定，
   实测 **`-Z root` 写出 0 个包、24 字节空 pcap，退出码仍是 0**
   —— 采完 45 分钟才发现全废。

改成 `User=pi` + `SupplementaryGroups=input audio dialout …`
（不写附加组的话 systemd 只给主组，恰好丢掉这三个）。
权限够用已逐条验证：脚本里用到的三处 sudo（`sysctl`/`tcpdump`/`kill`）
`sudo -n` 非交互免密全部 OK。

### `StartLimitIntervalSec` 属于 `[Unit]` 不属于 `[Service]`

放错段会被**静默忽略**（`systemd-analyze verify` 报
`Unknown key ... ignoring`）⇒ 重启限流失效，崩溃时无限重启。
修正后验证被识别：`StartLimitIntervalUSec=2min` / `StartLimitBurst=5`。
**养成习惯：装 unit 后必跑 `systemd-analyze verify`。**

### ⚠ POWER 键默认会让 systemd 关机

`HandlePowerKey` 的 systemd 默认值是 `poweroff`，而系统自带 udev 规则
（`/usr/lib/udev/rules.d/70-power-switch.rules`）给**所有** `ID_INPUT_KEY=1`
的设备打 `power-switch` 标签 —— 实测 event0/1/3/7/8/9 **六个节点全部**
在 logind 监听范围内。

手动测试时没关机是**侥幸**：靠桌面会话挂的一把
`handle-power-key block` 锁（`gtk-nop`）挡着。那把锁在命令行启动、
桌面崩溃、或本服务比桌面先起来时都不存在。

已装 `/etc/systemd/logind.conf.d/99-radar-remote.conf` 置 `ignore`。
**副作用：Pi 板载电源按钮短按也不再关机**，关机改用 `sudo poweroff`。
`systemd-logind` 不支持 reload（实测 `CanReload=no`），需重启生效。

### ★ evdev 读事件**不消费**它 —— 必须 grab() 独占

内核会把同一次按键**同时**投递给 evdev 客户端**和**常规键盘处理器。
不独占时遥控器仍是普通键盘：
- 现象：按开始键，**SSH 终端里冒出 `ss` 字符**
- 真实风险：`ENTER` 会在活动控制台上"回车"，而实测 **tty1 上正跑着
  `login` + `bash`** ⇒ 等于往那个 shell 里敲回车。封盒后无人能发现。

已改为**默认独占**（`--no-grab` / `--no-grab-capture` 可关）。

### 设备筛选：capabilities ≠ 实际按键

`event9` 的 capabilities 里**有 KEY_S、KEY_E、甚至 KEY_POWER** ——
整套 163 键都在。那只是 HID 描述符声明的**能力上限**。
照它猜键位的后果是"按遍所有按钮都没反应"且不报错。

同一坑更危险的一面：守护进程原本筛"能报 KEY_POWER"的节点，结果
**event1/3（HDMI CEC）、event8、event9 全部命中**，而它会 grab 独占 ——
**一旦独占 event9，采集程序就再也收不到 ENTER/ESC**。
现有三条排除规则：排除能报 ENTER/ESC 的、排除 `pwr_button`、排除 `hdmi`。
修好后只剩 event7，与实测一致。

### 状态文件判据用 PID 比对，不能用时间戳

守护进程靠采集程序写的状态文件决定"能不能退出"（只有 IDLE 放行）。
第一版用"时间戳超 300 秒即陈旧"，**是错的**：
**IDLE 可以持续几小时不变，而它恰恰是唯一允许退出的状态** ⇒
待命久了反而永远退不出去；CAPTURING 在 45 分钟采集里也是 2700 秒不变。
改为文件里记 PID，与当前子进程不符才算遗留。

`set_state()` 同时改成**仅状态变化时才写盘** —— `_remote_loop` 每秒
`set_state(IDLE)` 一次（为让 Ctrl-C 及时响应），无条件写就是每秒一次
SD 卡写入，长期待命纯属无谓磨损。

### 按键参数：MIN_GAP 150 → 50 ms

实测人为"快速连按"的间隔是 **209 ms**，离原设的 150 ms 只差 59 ms
—— 再快一点第二次就被当成抖动丢掉，**双击不成立且没有任何提示**，
现象是"按两下没反应"。
敢降的依据：同一次实测里 5 次按键**全部来自 event5**（旧键盘）、
另一节点零事件 ⇒ 跨节点重复并不存在。真发生也是同一批 USB 传输、
间隔在毫秒级，50 ms 照样拦得住，而人手连按远在其上。

区分"跨节点重复"与"人为连按"不能只看间隔，要同时看三样：
**是否同一节点、中间有没有抬起、间隔量级**。

### 语音：USB 声卡按名字选，两处必需的时序间隔

固定 `plughw:CARD=Device`（名字取自 `/proc/asound/cards` 方括号内）。
不给 `-D` 会落到 card 0 的 HDMI 上，**没声音且不报错**；
card 号随插拔顺序变，名字不变；`plughw` 而非 `hw` 以便自动重采样。

- **`preparing` 必须有声**（"采集命令下发"）：双击到 `start` 之间要跑
  preflight、配 FPGA、起进程、发 29 条 cfg、等 `sensorStart`，实测
  **10–20 秒**。用户实测反馈"中间没语音会让人以为没按上而继续按"。
- **`done_*` 与 `ready` 之间加 1.5 秒**：原本只隔三行 print（< 0.1 s），
  换成真播放器后 `done_good` 会被吞掉或与 `ready` 叠音。

播放实现三个约束：独立线程串行播（`aplay` 阻塞，会卡住采集时序；
两个 aplay 抢同一声卡第二个会失败）、队列超 3 个丢最旧（免得提示音
滞后于状态）、失败只打警告（喇叭掉线/wav 缺失都不影响采集）。

---

## 2026-08-02

### 阶段 6 通过：行为满测 2.2 GB 六项判据零失败

3000 帧 / 300 秒 / 7.33 MB/s / bin 2,211,839,872 B（切 3 片）。
在 SSD 上完成，L3 逐字节比对 36.7 秒。

### 阶段 5.5 通过：行为 cfg 的 sensorStart 问题结案

**这是 6 月遗留的悬案。** 根因是**雷达片上 L3 RAM 超限**，
不是采集卡缓存，也不是双 CBUFF 会话冲突。

- L3 容量 768 KB (0xC0000) 查自 `xwr68xx_mmw_demo_dss.map:19`
  （TI 官方文档 refer/ 里查不到，在本机 SDK 的 map 文件里）
- 128 loops 需 819,200 B（radarCube 786,432 + detMatrix 32,768），**超 32,768 B**
- **120 loops 是唯一解**：`numDopplerChirps` 必须是 4 的倍数
  （`dopplerprocdsp.c:703`），122 够但不满足 4 的倍数，124 仍超
- **实测**：128 → 前 30 条 Done，`sensorStart` 挂死；120 → 31 条全部 Done
- 失败时是**死锁**而非返回错误码：MSS 卡在
  `Semaphore_pend(BIOS_WAIT_FOREVER)`（`mss_main.c:1735`），
  DPC 报错不走 `DPM_Report_IOCTL` 分支、信号量永远没人 post（`:2631`）
- **排除了双会话假设** —— 我们的 cfg 是 `lvdsStreamCfg -1 0 1 0` 单会话，照样失败

**cfg 改动**：`cow_behavior.cfg` 的 `frameCfg` 第 3 字段 128 → **120**。
代价（推算）：多普勒积累 −6.25%，速度分辨率 0.042→0.045 m/s，SNR −0.28 dB；
距离分辨率、带宽、最大距离、最大速度、TDM 顺序全不变。

### 判据体系：补三个缺口，从"4 个计数器"扩到 7 项

审计发现原实现**不是"任意判据出错都判 BAD"**：

| 判据 | 原状 | 现在 |
|---|---|---|
| tcpdump `dropped by kernel` > 0 | 只打印字符串 | **判 BAD** |
| L3 逐字节 | **根本没跑**（注释说委托给 `pcap_to_bin.py`，但从不调用） | **真的跑** |
| bin 总量不符 | 只打印"← 截断或多余" | **判 BAD** |

缺口 1 的实害：08-02 09:48 那次丢 22,510 包，靠 L2 缺口连带才判 BAD ——
**若只有 tcpdump 丢而 L2 恰好对上，旧代码会误判 GOOD**。

缺口 2 的实害：此前记录的"L3 精确"其实只比了**文件大小**，
而 CLI_Record 会零填充补齐，实测丢 4863 包后大小仍精确等于期望值。
**大小相等对丢包几乎免疫。**

还修了一句撒谎的输出：全过时原本打印 `[GOOD] csv 零丢包`，
而 csv 一直是 0 字节什么都没验。现在逐项列出真正通过了哪些。

### 修复 L1/L2 的致命 bug：累计值不随 start_record 归零

**DCA1000 的累计字节数和包序号是 FPGA 上电后持续累加的。**
上次结束在序号 1519121 / 累计 2,211,840,000，下次从 1519122 / 2,211,840,000 接着数。

旧代码把末包累计值当本次总量，同一 bug 表现成三条：
L2 报"主机侧丢 2.2 GB"、L1 报"卡侧丢包"、首包累计非 0 报"开头漏了包"。
**一次成功的采集被误判为 BAD。**

改为取增量 `(末包累计 + 末包载荷) − 首包累计`，用那份被误判的数据复验：
L1、L2 都精确为 0。同时删掉"首包累计非 0 就报警"这个错判据。

前几次没暴露，是因为都在 Pi 重启/FPGA 重配后跑，计数器恰好从 0 开始。

### 新增 RX 通道死活检测（`check_quality.py`）

四层判据唯一测不出的静默故障：某路 RX 恒零时**四层全过**，
但 12 元虚拟阵列的角度估计会全错。耗时 **0.85 秒**，故永远开启。

通道布局先验证再写代码（布局搞错会静默地测错东西）：
`adcbufCfg` 第 4 字段 `ChanInterleave=1` = 非交织
（`mmwave_sdk_user_guide.txt:993-998`）；实测按"每 RX 连续块"解读四通道
mean|x| = 214/227/251/326 区分度明显，按"每复数轮询交织"则是
256/258/252/252（把四路混匀的假象）。

判据用**相对比值**（低于通道中位数 15%）而非绝对阈值。
做了**反向测试**：人为把 RX2 置零，正确报出异常并返回退出码 1。

原计划的零填充块检测/差分分位/饱和计数**已砍掉** ——
传输完整性由其余判据覆盖；"是不是真实回波"由人和场景保证，软件判不了。

### 新增 L3 逐字节比对（`verify_pcap_bin.py`）并接入流程

流式、跨分片、纯 Python。行为 2.2 GB 在 SSD 上 36.7 秒（60.3 MB/s）、
microSD 79.9 秒；生命体征 750 MB 约 24 秒。

**不需要 numpy**：`array.array` 扩展切片赋值实测 8M 个 int16 重排 0.133 秒。
（此前记录"numpy 有导入问题（cwd 同名文件遮蔽）"的诊断不对，
真实情况是三份 numpy 冲突：`/usr/local` 那份损坏、`.local` 的循环导入失败、
apt 注册的文件已从磁盘消失。）

流式成立的依据：**1456 = 8 × 182**，包边界与四元组边界严格对齐。
（此前记录"1456 不是 8 的倍数、包边界会落在四元组中间"是错的。）
已在真实数据上验证：29 MB 用逐元素参照实现与切片实现按五种块大小
（含 1456 和 8）各算一遍，全部一致。

开关三种途径，优先级 `--verify` > `--no-verify` > `CAPTURE_L3` 环境变量 > 默认开。

### 新增采集前自检（`preflight()`），取代人工跑 prepare.sh

**动机**：`sysctl -w` 不持久，重启即失。08-02 重启后
`rmem_max` 掉回 212992（208 KB，差 1260 倍）、`netdev_max_backlog` 掉回 1000，
那次采集**丢了 22,510 个包且没有任何提示** —— 静默降级。
**忘记跑 prepare.sh 的代价远大于参数不够优的代价。**

自检项：内核缓冲（检出即自动修正）、落盘目录存在/可写/余量、eth0 有 IP。
修不好就中止采集。同时持久化到 `/etc/sysctl.d/99-dca1000-capture.conf`（双保险）。

### tcpdump 的 -B 改成按码率算

`-B` 的意义是**能吸收多少秒调度抖动**，必须随码率缩放。实测：

| 配置 | 码率 | -B | 余量 | dropped |
|---|---|---|---|---|
| 生命体征 | 2.61 MB/s | 8 MB | 3.2 秒 | **0** |
| 行为 | 7.23 MB/s | 8 MB | 1.1 秒 | **22,510**（1.5%） |
| 行为 | 7.37 MB/s | **56 MB** | 8.0 秒 | **0** |

按 8 秒余量自动算。这更正了"pcap 并行开销可承受"的适用范围 ——
**那只在生命体征码率下成立**。

### 修复 sensorStart 只等 0.5 秒的缺陷

原实现写完命令后 `time.sleep(0.5)` 只查一次 `in_waiting`。
而 `sensorStart` 成功时要**好几秒**才回 `Done`（它要真的分配 L3、
配 CBUFF、启动流水线）。**即使成功也会被误判为失败并中止采集**，
把一次好数据扔掉。3000 帧配置下随时会咬人。

改成等到 `Done`/`Error`/超时（12 秒），并区分三种失败：
前面有回话 + 超时 → 雷达挂死（提示需断电）；前面全无回显 → 串口不通。

### exFAT 上 tcpdump 静默写空文件（加 -Z）

tcpdump 降权到 `tcpdump` 用户，而 exFAT 无 Unix 所有权、
整个挂载按 `uid=1000` 固定，该用户**无写权限**。
症状：只报 `Couldn't change ownership of savefile`、**退出码 0**、
pcap 停在 **24 字节**。

实测（各发 500 包）：microSD 不加 `-Z` → 342 包；
exFAT 不加 → **0 包**；exFAT `-Z root` → **0 包**（反直觉）；
exFAT `-Z pi` → **500 包**。已加 `-Z PCAP_USER`。

**在实测前发现的** —— 否则会采完 5 分钟拿到空 pcap。

### ModemManager 抢串口（udev 规则）

它把每个新串口当调制解调器探测，独占约 10 秒并往雷达 CLI 灌 AT/QCDM 字节
（`[ttyACM0/probe] failed to parse QCDM version info command result: -5`）。
USB 一重新枚举就抢，**牛场设备上电自启必踩**。

修法：`/etc/udev/rules.d/99-ti-radar.rules` 设 `ID_MM_DEVICE_IGNORE=1`。
注意用 `ATTRS{}`（向上遍历父设备）不是 `ATTR{}` —— idVendor 在祖父节点。
验证不能只看 `udevadm test`，要真的 USB 重新枚举后轮询端口占用
（修复前 1 秒内被抢，修复后轮询 14 秒全程 FREE）。

### 波形切换改成命令行参数 + chCfg 断电检查

**起因是一次事故**：我 scp 覆盖了用户在 Pi 上改的
`JSON_CFG`/`PROFILE_CFG` 两个常量，脚本去读了生命体征 cfg，
对着记住行为 `chCfg` 的雷达下发，撞上 `mmw_cli.c:288` 的 `debugAssert`。

两处改进：
1. 波形选择改成 `--mode {behavior,vitalsigns}` + `CAPTURE_CFG_MODE` 环境变量，
   **不再依赖会被覆盖的代码常量**
2. 新增 `check_chcfg()`：记录上次的 `channelCfg`，切换时**在启动任何进程之前**
   中止并提示需断电。拦得早很重要 —— 撞上那次 tcpdump 和 CLI_Record
   都已启动，还得走一遍 `-4068` 收尾

### csv 修复尝试与最终放弃

查明根因：汇总由 **CLI_Record 自己的进程**写
（`rf_api.cpp:1882/1895` → fprintf + fclose `:2396,2398`），
而我们在 `-4068` 后立刻 SIGKILL 掉它。Windows 上有 csv，
正是因为那边 `stop_record` 能正常完成。

加了 `wait_for_csv()` 等它写完再杀。**但 `captureStopMode: "infinite"` 下
那条路径根本不触发** —— 卡不判定"完成"、不发 `STS_REC_COMPLETED`。
唯一替代是等接收线程 90 秒超时，代价太大而收益已被 pcap 覆盖。
**故放弃**，`check.csv` 恒为 `UNKNOWN`，不影响判定。

顺带修了一个潜伏的解析 bug：原正则会把
`Out of sequence seen from X to Y` 的**偏移量**当丢包数累加、
把 `zero filled bytes`（字节数）加进包数 ——
真值 oos=12 / zf=4863 被算成 **4108 / 7085391**。
现已锚定 TI 确切字段名（`rf_api.cpp:2358-2380`）。
**这个 bug 一直没暴露正是因为 csv 从来是空的**，
若哪天换 `frames` 模式它会立刻往"误判 BAD"方向咬人。

### meta 格式重做：人机两便

总判定放最前（`grep "^verdict="`），逐项 `check.<slug>=PASS|FAIL|UNKNOWN`
加 `.detail`，另补采集参数（`channel_cfg`、`data_rate_MBps`、
`samples`/`tx_count`/`rx_count`、`cfg_file` 等）。

注意键名含数字（`check.l0l1l2_pcap`、`check.l3_bytewise`），写正则时别漏。

### 落盘改到 USB SSD

`fileBasePath` → `/mnt/pssd/Ti_radar_data`。

实测 SSD 247 MB/s vs microSD 21 MB/s，需求仅 15.1 MB/s ——
**microSD 本来也够**（余量 40%，无周期性掉速），
两次采集失败都与介质无关。这更正了此前"必须用 NVMe、
microSD 测出的结论无参考价值"的判断。

SSD 的真实价值是**容量**（932 GB ≈ 200 段 vs microSD 剩 18 GB ≈ 3 段），
附带让 L3 比对快一倍。

**exFAT 风险（未验证，仅提示）**：无日志，异常掉电可能损坏文件系统。

---

## 2026-08-01

### 阶段 5 通过：生命体征满测 750 MB

两次（`--no-pcap` 与带 pcap）均零丢包、内核 drops 0、tcpdump dropped 0。
pcap 体积开销实测 **4.7%**（不是此前记的 2.7% —— 那个数只算了 1466/1456，
没计入 42 字节以太网头和 16 字节 pcap 包头）。

### 建立三层判据体系与四个独立计数器

关键认识：**"bin 与 pcap 一致"不足以证明没丢数据** ——
两者共享同一上游（内核网络栈），包在到达内核前就丢了的话两边都没有。

L2 的关键是每包偏移 4 起的 6 字节"累计已发送字节数"，
**该值来自卡侧**，据此可**纯软件区分卡侧与主机侧丢包** ——
这更正了此前"只能靠面板 LED 区分"的说法，封盒后 LED 看不见，
这是唯一替代手段。

**内核 drops 必须采集期间轮询**：socket 只在 CLI_Record 运行时存在，
采集后 `/proc/net/udp` 那一行消失，事后读永远是 None
（原实现就是这个 bug，两次实测均无效）。改为 `DropMonitor` 每 0.5 秒轮询。

### 查明 stop_record 必然超时的根因

```
CLI_CMD_TIMEOUT_DURATION           = 7000 ms  (Common/globals.h:181)
SOCKET_THREAD_TIMEOUT_DURATION_SEC = 90 s     (Common/rf_api_internal.h:135)
```

**7 秒 < 90 秒 ⇒ 数据流正常结束时必然触发**，是 TI 的设计缺陷。
数据此时已落盘，直接终止进程安全。加了 `kill_record_proc()`（四处调用）
与 `cleanup_shm()`。

排查中排除的两个可疑点（记下免得重查）：
`gethostbyname("localhost")` 返回正确、地址匹配；
TI 满篇 `memset(..., '0', ...)` 是把 `sin_zero` 填成 `0x30` 的笔误，
但在这条路径上无害。

---

## 2026-07-31

### 阶段 4 通过：首次成功采集

200 帧，bin 26,214,400 B 精确，pcap 18,005 包序号 1→18005，
载荷合计与 bin 完全相等，丢失/重复/乱序 0/0/0。

**csv 是 0 字节，pcap 救了这次采集** —— 而 csv 原本是唯一可信的丢包判据。
结论：pcap 不再只是交叉验证手段，它比 csv 更可靠。

### 坐实 lane 重排规则

```
网线上（pcap 载荷）  (a, b, c, d)
CLI_Record 写的 bin  (a, c, b, d)
```

13,107,200 个 int16 逐个比对**零差异**，参照物是 TI CLI_Record 自己的输出
（独立实现，不是自我验证）。这曾是最高优先级未验证项 ——
**错了不报错、只出错结果**。

pcap 剥离偏移是 **52** 不是 42（再跳 10 字节 DCA 包头）。

### captureStopMode 必须用 infinite

`"frames"` 报 `-4064` "valid only in raw mode"，
但代码 `cli_control_main.cpp:805` 实际判的是 `!= MULTI_MODE` ——
**TI 的提示文字与实现相反**，frames 只能配 multi。
而 multi 会产出 5 个 `_hdr_0ADC_n.bin` 使既有结论失效，故选 infinite。

### TI CLI 的 ARM64 移植修复（3 处，见 patches/）

TI 只在 Ubuntu 16.04 **x86_64** 上测过，这两个问题在 ARM64 上必然触发。
**编译通过 ≠ 能运行。**

1. **start_record 缺 `-q` 就静默失败**：`CLI_Control` 用
   `gnome-terminal -x` 起子进程（`cli_control_main.cpp:1660`），
   Pi OS 没有它 → `system()` 失败但**退出码仍是 0**。
   必须加 `-q` 走后台分支（`:1657`），并用 `pgrep -f` 确认
   （进程名超 15 字符，`-x` 匹配不到）
2. **pragma pack 污染导致 SIGBUS**：`#pragma pack(1)` 开了不关
   （`globals.h:63`、`rf_api_internal.h:95`），污染到 `cUdpDataReceiver`，
   实测 `alignof == 1`；类内嵌 `pthread_cond_t`/`pthread_mutex_t` 被压到
   未对齐地址，aarch64 glibc 直接 SIGBUS。三处加 `pack(push,8)`/`pop`
   + `aligned(8)`。**协议结构体一律不动** —— 走网线的结构必须逐字节紧凑，
   加对齐会使 FPGA 解析错乱且不报错
3. 排查弯路（教训）：先只给 typedef 加 `aligned(8)` 无效 ——
   `pack(1)` 仍压成员偏移，崩溃偏移三次不变。
   后来写探针实测 `alignof` 才定位到是**外层类**的问题。
   **该早点测量而不是连续猜。**

**阶段 1 判据的漏洞**：原判据是"产出三个文件"，冒烟测试只跑了
`CLI_Control` 的 usage，`CLI_Record` 从未被执行 —— 而缺陷全在它身上。
**跨架构移植时，判据必须是"每个二进制都实际运行过"。**

### 阶段 1–3 通过：编译、链路、串口

- 必须在 SourceCode 根目录跑 make（RF_API 规则靠源文件里的相对路径）
- makefile 是 CRLF，先 `dos2unix`
- `export LD_LIBRARY_PATH=$PWD:...`（TI 用户指南写的 `$pwd` 是笔误）
- 网卡名先查 `ip link`（Pi 5 用 macb 驱动，可能叫 `end0`）
- **ping DCA1000 永远不通，卡不响应 ICMP**，真判据是
  `DCA1000EVM_CLI_Control fpga` 返回成功
- JSON `fileBasePath` 目录须预先存在，**CLI 不自动创建**

---

## 2026-07-29 建立 wireless/

把第一阶段定稿的 Lua 波形参数转录成 .cfg + .json。
校验方法（每次改参数都该做）：
`samples × RX × 2 × 2 × loops × TX × frames`，
两个结果都与第一阶段实测字节数精确吻合，证明映射正确。

相比 6 月旧 cfg 的三处关键改动：
- `lvdsStreamCfg -1 0 1 0`（原 `-1 1 1 1`）：关 HSI header，
  产出纯 ADC 的 `_Raw_n.bin`。原设置会产出 `_hdr_0ADC_n.bin`
  使"每包 1456 字节无 padding"等既有结论失效
- JSON `dataLoggingMode: "raw"`（原 `"multi"`）：必须与上一条配对
  （`mmwave_sdk_user_guide.txt:655-658`）
- JSON `captureStopMode`：见 07-31 那条

相比 Windows 原版 `auto_capture.py` 的改动：
- 文件前缀从 JSON 的 `filePrefix` 读（原版硬编码 `cow_capture`，
  与 json 不一致会导致采完文件搬不走）
- 每帧字节数从 cfg 自动算，不硬编码 131072
- 帧数一致性检查（json `framesToCapture` vs cfg `frameCfg[4]`）
- 串口开口前清缓冲；报错只在 `sensorStart` 也失败时才中止采集
- meta 用 UTF-8（第一阶段用 ASCII 是为避开 Studio 的 GBK，Linux 无此约束）

---

## 待办

- 功耗与温度实测（计量插座测 Wh、5V 侧峰值电流、`vcgencmd measure_temp`、
  纸箱预演箱内温度）
- GPIO 状态灯（环境已就绪：`lgpio` 可用、`pinctrl` 在、5 个 gpiochip 都在）。
  方案：黄=配置中、绿闪=采集中、绿常亮=全部通过、红常亮=判废、红快闪=出错。
  封盒后这是唯一能看见状态的途径（DCA1000 面板灯也被盒子挡住）
- 封盒与防水
- 考虑把 SSD 格成 ext4（有日志，掉电更安全，且不再需要 `-Z`）
- **SSD 首次挂载掉线**的处置（供电排查 / 开机即挂载 / 去掉 automount 的 idle-timeout），方案待定
- **冷启动后雷达串口零回显**的根因（上电时序）
- 心率带：等首个 PMD 最长实测 8.6 s，离 10 s 上限只差 1.4 s，牛场若常超时需再议
- 心率带：未标定 BLE 批量延迟与 pcap 首包相对真实 chirp 的延迟（需敲击对时）
- 心率带："WiFi 开着但没连上任何网络"工况下的 BLE 未测
