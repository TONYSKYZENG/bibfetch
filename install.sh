#!/usr/bin/env bash
# 自动安装 bibfetch 及所有运行依赖的脚本
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================="
echo " 正在检查并自动安装 bibfetch 依赖..."
echo "=========================================="

PYTHON_BIN="$(which python3 || which python)"
if [ -z "$PYTHON_BIN" ]; then
    echo "错误：未找到 Python 解释器，请先安装 Python 3.10+。" >&2
    exit 1
fi

echo "使用 Python: $PYTHON_BIN"

# 安装/更新核心依赖
"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install -e .

echo ""
echo "=========================================="
echo " 依赖与工程安装成功！"
echo "=========================================="
echo "你可以通过以下任意命令启动："
echo "  1. 图形界面：python3 -m gui"
echo "  2. 桌面命令：bibfetch-gui"
echo "  3. 命令行检索：bibfetch --help"
echo "=========================================="
