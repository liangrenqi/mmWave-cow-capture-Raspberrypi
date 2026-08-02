#!/bin/bash
# 环境自检 —— clone 之后先跑这个，逐项告诉你还缺什么
#
# 只做检查，不改系统（除了提示你要跑哪条命令）。
# 采集脚本自己也会自检内核参数并自动修正，但这里能提前发现
# 依赖缺失、权限不足、硬件没接之类无法自动修的问题。

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OK=0; FAIL=0
pass() { echo "  [OK]   $1"; OK=$((OK+1)); }
fail() { echo "  [缺]   $1"; [ -n "$2" ] && echo "         → $2"; FAIL=$((FAIL+1)); }

echo "=== 1. 依赖 ==="
python3 -c "import serial" 2>/dev/null \
    && pass "pyserial" || fail "pyserial" "sudo apt install python3-serial"
command -v tcpdump >/dev/null \
    && pass "tcpdump $(tcpdump --version 2>&1 | head -1 | awk '{print $3}')" \
    || fail "tcpdump" "sudo apt install tcpdump"
python3 -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" \
    && pass "python3 $(python3 -V 2>&1 | awk '{print $2}')" \
    || fail "python3 版本过低（需 3.8+）"

echo
echo "=== 2. 权限 ==="
id -nG | tr ' ' '\n' | grep -qx dialout \
    && pass "当前用户在 dialout 组" \
    || fail "不在 dialout 组（串口打不开）" "sudo usermod -aG dialout $USER 然后重新登录"
sudo -n true 2>/dev/null \
    && pass "sudo 免密（tcpdump 与 sysctl 需要）" \
    || echo "  [注意] sudo 需要密码，采集时会提示输入"

echo
echo "=== 3. TI CLI 二进制 ==="
BIN="${DCA1000_CLI_DIR:-$HERE/bin}"
# 先补执行权限：文件存在但没 +x 时，症状是"找不到 CLI"，很误导。
# git 本身保留执行位，但打包/解压/拷贝的路径上可能丢。
for _f in "$BIN"/DCA1000EVM_CLI_Control "$BIN"/DCA1000EVM_CLI_Record; do
    [ -f "$_f" ] && [ ! -x "$_f" ] && chmod +x "$_f" 2>/dev/null \
        && echo "  [修]   已补上 $(basename "$_f") 的执行权限"
done
if [ -x "$BIN/DCA1000EVM_CLI_Control" ] && [ -x "$BIN/DCA1000EVM_CLI_Record" ]; then
    pass "在 $BIN"
    # file 会把 aarch64 打印两次（ELF 头 + 详细描述），用 head -1 取一次
    arch=$(file -b "$BIN/DCA1000EVM_CLI_Control" 2>/dev/null \
           | grep -o 'aarch64\|x86-64' | head -1)
    host_arch=$(uname -m)
    if [ "$arch" = "aarch64" ] && [ "$host_arch" = "aarch64" ]; then
        pass "架构 aarch64，与本机一致"
    elif [ "$arch" = "aarch64" ]; then
        fail "二进制是 aarch64，本机是 $host_arch" "见 docs/SETUP.md 自行编译"
    else
        fail "二进制是 $arch，本机是 $host_arch" "见 docs/SETUP.md 自行编译"
    fi
    # 判据用 ldd 而非 --help：CLI_Record 没有 --help 参数，
    # 且它对任何参数都返回退出码 0（TI 的实现如此），
    # 所以退出码不能当判据。ldd 能确认动态库全部可解析 ——
    # 这才是"能不能跑起来"的真判据（SIGBUS 是运行时问题，静态查不出，
    # 那个已由 bin/ 里预编译的补丁版本保证）。
    if [ "$(LD_LIBRARY_PATH="$BIN" ldd "$BIN/DCA1000EVM_CLI_Record" 2>&1 \
            | grep -c 'not found')" -eq 0 ]; then
        pass "CLI_Record 动态库齐全（含 libRF_API.so）"
    else
        fail "CLI_Record 缺动态库" "确认 bin/libRF_API.so 存在且 LD_LIBRARY_PATH 已设"
    fi
else
    fail "找不到 CLI 二进制" "仓库应自带 bin/；或 export DCA1000_CLI_DIR=..."
fi

echo
echo "=== 4. 内核参数（采集脚本会自动修正，这里只是提示）==="
r=$(cat /proc/sys/net/core/rmem_max 2>/dev/null || echo 0)
[ "$r" -ge 268435456 ] && pass "rmem_max = $r" \
    || fail "rmem_max = $r 偏小" "sudo cp config/99-dca1000-capture.conf /etc/sysctl.d/ && sudo sysctl -p /etc/sysctl.d/99-dca1000-capture.conf"
b=$(cat /proc/sys/net/core/netdev_max_backlog 2>/dev/null || echo 0)
[ "$b" -ge 5000 ] && pass "netdev_max_backlog = $b" || fail "netdev_max_backlog = $b 偏小" "同上"

echo
echo "=== 5. udev 规则（防 ModemManager 抢串口）==="
[ -f /etc/udev/rules.d/99-ti-radar.rules ] && pass "已安装" \
    || fail "未安装" "sudo cp config/99-ti-radar.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=tty"

echo
echo "=== 6. 硬件 ==="
lsusb -d 0451:bef3 >/dev/null 2>&1 && pass "雷达 USB 已枚举（XDS110）" \
    || fail "没找到雷达 USB" "检查 USB 线"
[ -e /dev/ttyACM0 ] && pass "/dev/ttyACM0 存在" || fail "/dev/ttyACM0 不存在"
if h=$(sudo -n fuser /dev/ttyACM0 2>/dev/null); then
    fail "串口被进程 $h 占用" "等 10 秒（ModemManager 探测），或装 udev 规则"
else
    pass "串口空闲"
fi
ip -4 addr show eth0 2>/dev/null | grep -q "inet " && pass "eth0 有 IPv4" \
    || fail "eth0 没有 IPv4" "见 docs/SETUP.md 第 2 步"

echo
echo "=== 7. 落盘目录 ==="
base=$(python3 -c "
import json,sys
try:
    print(json.load(open('$HERE/config/dca1000_behavior.json'))['DCA1000Config']['captureConfig']['fileBasePath'])
except Exception as e: sys.exit(1)
" 2>/dev/null)
if [ -z "$base" ]; then
    fail "读不出 fileBasePath"
elif [ -d "$base" ] && [ -w "$base" ]; then
    free=$(df -BG --output=avail "$base" 2>/dev/null | tail -1 | tr -dc '0-9')
    pass "$base 可写，剩余 ${free} GB"
    [ "${free:-0}" -lt 5 ] && echo "  [注意] 一段行为采集约需 4.4 GB（bin+pcap）"
else
    fail "$base 不存在或不可写" "sudo mkdir -p '$base' && sudo chown $USER '$base'；CLI 不会自动创建"
fi

echo
echo "=== 8. 雷达是否在应答 ==="
if [ -e /dev/ttyACM0 ]; then
    if timeout 20 python3 "$HERE/scripts/probe_radar.py" 2>/dev/null | grep -q "雷达在应答"; then
        pass "雷达回应 version"
    else
        fail "雷达无应答" "查 SOP 跳线是否 001（mode 4）、5V 是否接好；见 docs/TROUBLESHOOTING.md"
    fi
else
    echo "  [跳过] 串口不存在"
fi

echo
echo "============================================"
echo "  通过 $OK 项，待解决 $FAIL 项"
if [ "$FAIL" -eq 0 ]; then
    echo "  环境就绪，可以 ./run.sh 采集"
else
    echo "  按上面的 → 提示逐项处理"
fi
echo "============================================"
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
