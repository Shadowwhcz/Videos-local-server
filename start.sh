#!/bin/bash
# 局域网视频服务器启动脚本

cd "$(dirname "$0")"

# 检查 Python 环境
if ! command -v python3 &> /dev/null; then
    echo "❌ 错误: 未找到 Python3"
    echo "请先安装 Python 3.8 或更高版本"
    exit 1
fi

# 检查虚拟环境
if [ ! -d "venv" ]; then
    echo "📦 创建虚拟环境..."
    python3 -m venv venv
fi

# 激活虚拟环境
source venv/bin/activate

# 安装依赖
echo "📥 检查依赖..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

# 启动服务（多worker模式，提升并发处理能力）
echo "🚀 启动视频服务器 (4 workers)..."
uvicorn app:app --host 0.0.0.0 --port 8000 --workers 4