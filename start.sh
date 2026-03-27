#!/bin/bash
# 局域网视频服务器启动脚本

cd "$(dirname "$0")"

# 检查 Python 环境
if ! command -v python3 &> /dev/null; then
    echo "❌ 错误: 未找到 Python3"
    echo "请先安装 Python 3.8 或更高版本"
    exit 1
fi

# 统一使用 .venv，避免和历史 venv/ 混用
VENV_DIR=".venv"

# 检查虚拟环境
if [ ! -d "$VENV_DIR" ]; then
    echo "📦 创建虚拟环境到 $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
fi

if [ -d "venv" ] && [ "$VENV_DIR" = ".venv" ]; then
    echo "ℹ️ 检测到历史环境 venv/，当前将优先使用 .venv/"
fi

# 激活虚拟环境
source "$VENV_DIR/bin/activate"

# 安装依赖
echo "📥 检查依赖..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

# 启动服务（多worker模式，提升并发处理能力）
echo "🚀 启动视频服务器 (4 workers)..."
uvicorn app:app --host 0.0.0.0 --port 8000 --workers 4
