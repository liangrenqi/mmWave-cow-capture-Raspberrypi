#!/bin/bash
# 采集启动器 —— 处理好环境变量再调 capture_linux.py
#
# 直接跑 python3 scripts/capture_linux.py 也可以，但需要自己设
# LD_LIBRARY_PATH 和 DCA1000_CLI_DIR。这个脚本替你做了。
#
# 用法：
#   ./run.sh                      行为波形（默认）
#   ./run.sh --mode vitalsigns    生命体征
#   ./run.sh --no-verify          跳过 L3 逐字节比对（现场省时间）
#   ./run.sh --loop 5             连续采 5 段
#   ./run.sh --help               全部参数
#
# 首次使用请先跑 ./check.sh 确认环境就绪。

set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 仓库自带已编译的 ARM64 二进制（bin/）。若你自己编译了别处的版本，
# 用 DCA1000_CLI_DIR 覆盖即可。
export DCA1000_CLI_DIR="${DCA1000_CLI_DIR:-$HERE/bin}"
export LD_LIBRARY_PATH="$DCA1000_CLI_DIR:$LD_LIBRARY_PATH"

# 先补执行权限再检查是否存在 —— 顺序反了会把"没有 +x"误报成"找不到文件"
chmod +x "$DCA1000_CLI_DIR"/DCA1000EVM_CLI_* 2>/dev/null || true

if [ ! -f "$DCA1000_CLI_DIR/DCA1000EVM_CLI_Control" ]; then
    echo "[ERROR] 找不到 $DCA1000_CLI_DIR/DCA1000EVM_CLI_Control"
    echo "        仓库应自带 bin/，或用 DCA1000_CLI_DIR 指定你编译的位置"
    exit 1
fi
if [ ! -x "$DCA1000_CLI_DIR/DCA1000EVM_CLI_Control" ]; then
    echo "[ERROR] $DCA1000_CLI_DIR/DCA1000EVM_CLI_Control 没有执行权限，且补不上"
    echo "        手动执行： chmod +x $DCA1000_CLI_DIR/DCA1000EVM_CLI_*"
    exit 1
fi

exec python3 "$HERE/scripts/capture_linux.py" "$@"
