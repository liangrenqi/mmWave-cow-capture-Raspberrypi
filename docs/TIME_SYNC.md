# NTP 校时与双雷达对时

牛场两套同时采：A 套 IWR6843（带心率带 H10），B 套 IWR1843（做数据冗余）。
两套必须对上时间，且**用同一台 NTP 服务器：ntp.aliyun.com**。

本文写四件事：为什么开机必须先校时、闸门怎么工作、B 套怎么装怎么验、两套之间误差有多大。

---

## 一、当前配置（2026-10-20 定）

| 项 | 值 |
|---|---|
| 校时服务 | systemd-timesyncd（systemd 252）。chrony / ntp 都没装 |
| 服务器 | **ntp.aliyun.com**（实测解析到 203.107.6.88），只此一台，不加备份 |
| A 套 | 主文件 `/etc/systemd/timesyncd.conf` 里写的 `NTP=ntp.aliyun.com`（2026-09-01），没有 drop-in |
| B 套 | 装 [config/99-radar-timesync.conf](../config/99-radar-timesync.conf) 到 `/etc/systemd/timesyncd.conf.d/`，生效后与 A 套相同 |

**只用一台的理由**：两套用不同服务器，会多出一项服务器之间的偏差。

**代价**：阿里云不通时不会换别的服务器，开机闸门一直等，不能采集。
依据是 timesyncd.conf(5)：FallbackNTP "only relevant if no other NTP server information is known"，
配了 `NTP=` 就用不到。这一条是按 man 页说的，**没做过断网实测**。

不换 chrony：闸门等的是 timesyncd 写的标志文件（见第三节），换了就永远等不到。

---

## 二、为什么开机必须先校时

Pi 没有 RTC。开机时钟是上次存下的旧值，联网后 timesyncd 首次校时会把它往前拨：

- 10-19 实测跳了 +29 s 到 +8705 s
- 10-20 这次开机约 +91 s。这是按 journal 推算的：开机 5.0 s 时恢复到 15:09:53，35.2 s 时校到 15:11:54.06

**跳变超过 80 s 时，跨过这一刻的采集段 bin 会被截断，pcap 是完整的。**
10-19 三段 BAD 就是这样，截断点与校时时刻吻合（实测）。

原因在 TI CLI_Record 的捕获超时（读源码）：

- 超时是 `CAPTURE_TIMEOUT_DURATION_SEC 80`（`rf_api_internal.h:132`），用在 `rf_api.cpp:1789`
- 它算的是 `time()+80` 这个绝对时刻（`osal_linux.cpp:553-580`）
- 等的条件变量用默认属性初始化（`:592`），所以按 CLOCK_REALTIME 计时
- 时钟一往前跳，超时就立刻到期

本仓库脚本的 sensorStart 超时等也用 `time.time()`，同样会被跳变带偏。

---

## 三、闸门怎么工作

```
上电
 └─ timesyncd 恢复上次的旧时钟
 └─ radar-remote.service 拉起 remote_daemon.py
      ├─ 把供电状况写进日志（只记不播）
      ├─ 独占 POWER 键（等待期间按键不能漏给 logind）
      ├─ 没有 /run/systemd/timesync/synchronized
      │     → 播"正在等待联网校时"，每秒查一次，不设超时
      │     → 这期间按 POWER × 2：不响应，重播"正在等待联网校时"
      ├─ 标志出现 → 播"时间校正完毕"
      └─ 播"守护进程已启动"，POWER 开始响应

capture_linux.py 每段开采前再查一次标志
      没有 → 拒采；遥控模式下播"出错"
```

- timesyncd 每次校时成功都会 touch 标志文件（systemd-timesyncd.service(8)，Pi 上 systemd 252 已核实）
- 标志文件在 `/run`（tmpfs）里，每次开机清空，不会误用上次开机的状态
- **没联网就一直等，不放行**（用户 10-19 定）：中途联网照样会跳变
- capture_linux 的这道检查拦的是不经守护进程、直接跑脚本的情形

离线调试时可以关掉，两者都会打警告：

```bash
python3 scripts/remote_daemon.py --no-wait-time    # 不等校时，也给子进程传 --no-time-check
python3 scripts/capture_linux.py --no-time-check
```

10-19 用户现场实测，行为符合预期。10-20 开机的 journal 里，守护进程等了 27 秒才放行（实测）。

---

## 四、B 套（1843）部署

前提：B 套的 Pi 已按 [SETUP.md](SETUP.md) 装好本仓库和 radar-remote 服务。

**1. 代码更新到带校时闸门的版本**

```bash
cd ~/mmwave-cow-capture && git pull
grep -c TIME_SYNC_FLAG scripts/capture_linux.py scripts/remote_daemon.py   # 两个都应 ≥ 1
ls sounds/time_waiting.wav sounds/time_synced.wav                           # 两个都在
```

**2. 确认用的是 timesyncd**

```bash
systemctl is-active systemd-timesyncd     # active
dpkg -l chrony ntp 2>/dev/null | grep ^ii # 应无输出
```

**3. 装服务器配置**

```bash
sudo mkdir -p /etc/systemd/timesyncd.conf.d
sudo cp config/99-radar-timesync.conf /etc/systemd/timesyncd.conf.d/
sudo systemctl restart systemd-timesyncd
```

配置文件第一行 `NTP=` 置空，用来清掉主文件里可能写过的其它服务器。

**4. 判据（联网后查）**

```bash
timedatectl show-timesync -p SystemNTPServers   # SystemNTPServers=ntp.aliyun.com，只有这一个
timedatectl timesync-status | head -1           # Server: 203.107.6.88 (ntp.aliyun.com)
ls /run/systemd/timesync/synchronized           # 文件存在
python3 scripts/remote_daemon.py --check        # [5] ✓ 本次开机已校时
```

SystemNTPServers 里多出别的服务器时，说明还有别的配置在加：

- 用 `systemd-analyze cat-config systemd/timesyncd.conf` 看全部生效内容
- drop-in 按文件名排序合并，排在 `99-radar-timesync.conf` 后面的文件还能再加服务器

**5. 重启服务听一遍**

```bash
sudo systemctl restart radar-remote
```

应依次听到：
- "正在等待联网校时"（已校过时不播这句）
- "时间校正完毕"
- "守护进程已启动"

**B 套另外两件事，与校时无关，都还没定：**

- **心率带**：
  - 默认 `CAPTURE_H10=1`，B 套会去连 A 套那条 H10（同一个 MAC）。
  - B 套要关掉，在 unit 的 `[Service]` 里加 `Environment=CAPTURE_H10=0`（待确认）。
  - 守护进程拉子进程时不改环境，unit 里的变量会传下去。
- **波形**：
  - 仓库 `config/` 里的 cfg 都是 6843 的（60 GHz）。
  - 1843 用的 cfg 不在 main。08-03 在 A 套的 Pi 上试过一版（77 GHz、loops 128），那段判 BAD：L0/L1/L2、L3、RX 三项都 FAIL。
  - 这一版存档在分支 `archive/iwr1843-trial-20260803`，**未验证，不能直接用**。

---

## 五、两套之间误差有多大

**单台对服务器：**

- `timedatectl timesync-status` 显示的 Root distance = RootDelay/2 + RootDispersion。
  10-20 实测 15.899/2 + 10.040 = 17.989 ms，与显示值吻合。
- 它**不含 Pi 到服务器这一段链路**。
- 单台最坏情况约等于 Root distance + Delay/2，10-20 读数约 18 + 39 ≈ 57 ms。
- 实测 offset 只在 ±10 ms 量级。

**两台之间：**

- 用同一台服务器，只消掉了服务器之间的差异。
- 两台各自走蜂窝链路，链路不对称造成的误差抵消不了。

**漂移，比服务器影响大（推断）：**

- timesyncd 不保存频率：`/var/lib/systemd/timesync/` 下只有一个空的 `clock` 文件，每次开机都从头估。
- 10-20 开机 10 分钟时，频率修正还在从 −22 跳到 −12 ppm（实测）。
- 热点撤掉后若残差 10 ppm，每小时漂约 36 ms，两台各漂各的（推算）。

**所以：**

- NTP 只管粗对齐。
- 亚帧级对齐（妊娠一帧 50 ms）靠敲击：每天首段、末段两台都在采时，敲共同支架三下。从两份 pcap 找事件时刻求差，中间线性插值。
- 这一步**未验证**，出发前先在实验室并排测一次。
- `capture_meta.txt` 目前不记校时状态。

---

## 六、选服务器时的探测（2026-10-20，iPhone 热点，SNTP）

| 服务器 | 解析地址 | 时延 | 说明 |
|---|---|---|---|
| ntp.aliyun.com | 203.107.6.88 | 58 ms | 最小时延那次 offset +0.4 ms，**选用** |
| ntp1.aliyun.com | 223.4.249.80 | 64 ms | |
| ntp.tencent.com | 106.55.184.199 | 87 ms | offset 范围最窄（−7.9 ~ +4.4 ms） |
| ntp.ntsc.ac.cn | — | 180 ms 以上 | 只回了 1/4，不用 |
| cn.pool.ntp.org | 境外节点 | 239 ms | offset −35 ms，不用 |

---

## 七、排查

**开机一直播"正在等待联网校时"、POWER 没反应**

这是设计如此，校时前不放行。先查：

```bash
timedatectl timesync-status                    # 联系上服务器没有
journalctl -u systemd-timesyncd -b --no-pager  # 有没有 "Contacted time server"
journalctl -u radar-remote -b | grep 校时       # 守护进程每 60 秒打一行"仍在等待校时"
```

- 常见原因：手机热点没开、热点没流量、阿里云不通。
- timesyncd 没在跑时，守护进程启动就会报"⚠ systemd-timesyncd 没在运行"。

**bin 被截断、pcap 完整**

先看这一段是不是跨过了开机校时：
- 这一段是不是用了 `--no-time-check` / `--no-wait-time`
- 段起止时间里有没有 journal 的 "Initial clock synchronization"

pcap 完整的话，可以用 `pcap_to_bin.py` 重建 bin。
