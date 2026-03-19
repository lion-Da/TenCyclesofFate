#!/bin/bash
# ══════════════════════════════════════════════
#   浮生十梦 Linux/macOS 启动脚本
# ══════════════════════════════════════════════

set -e

# 切换到脚本所在目录
cd "$(dirname "$0")"

echo
echo " [浮生十梦] 正在初始化..."
echo

# ─── 1. 检测 Python ───
if ! command -v python3 &>/dev/null; then
    echo "[错误] 未找到 python3，请安装 Python 3.10+"
    exit 1
fi

PY_OK=$(python3 -c "import sys; print('ok' if sys.version_info >= (3,10) else 'no')" 2>/dev/null || echo "no")
if [ "$PY_OK" != "ok" ]; then
    echo "[警告] Python 版本可能不兼容，建议 3.10+"
fi
echo "[*] 检测到 $(python3 --version 2>&1)"

# ─── 2. 虚拟环境 ───
if [ ! -f ".venv/bin/activate" ]; then
    echo "[*] 创建虚拟环境..."
    python3 -m venv .venv
    echo "[ok] 虚拟环境已创建"
else
    echo "[ok] 虚拟环境已存在"
fi

source .venv/bin/activate

# ─── 3. 安装依赖 ───
if [ -f "backend/requirements.txt" ]; then
    echo "[*] 检查并安装依赖..."
    pip install -r backend/requirements.txt -q
    echo "[ok] 依赖就绪"
fi

# ─── 4. .env 配置 ───
if [ ! -f "backend/.env" ]; then
    if [ -f "backend/.env.example" ]; then
        echo "[*] 首次运行：正在从 .env.example 创建 .env ..."
        cp backend/.env.example backend/.env
        echo
        echo "════════════════════════════════════════════════════"
        echo " 请编辑 backend/.env 填入你的 API Key 等配置后重新运行"
        echo " 例如: nano backend/.env"
        echo "════════════════════════════════════════════════════"
        echo
        exit 0
    else
        echo "[错误] 未找到 backend/.env 和 backend/.env.example"
        exit 1
    fi
fi

# ─── 5. 从 .env 安全读取 HOST 和 PORT ───
# 先修正 Windows 换行符
sed -i 's/\r$//' backend/.env 2>/dev/null || true

HOST=$(python3 -c "
from dotenv import dotenv_values
v = dotenv_values('backend/.env')
print(v.get('HOST', '0.0.0.0'))
" 2>/dev/null || echo "0.0.0.0")

PORT=$(python3 -c "
from dotenv import dotenv_values
v = dotenv_values('backend/.env')
print(v.get('PORT', '8000'))
" 2>/dev/null || echo "8000")

# ─── 6. 启动 ───
echo
echo "╔══════════════════════════════════════════════════════════╗"
echo "║           浮 生 十 梦  /  Ten Cycles of Fate            ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  地址:  http://${HOST}:${PORT}                          ║"
echo "║  停止:  Ctrl + C                                        ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo

python -m uvicorn backend.app.main:app --host "${HOST}" --port "${PORT}" --reload

echo
echo "[!] 服务器已停止"