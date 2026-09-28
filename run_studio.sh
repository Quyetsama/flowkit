#!/bin/bash
# ==============================================================================
# FlowKit Studio Launcher (macOS / Linux)
# ==============================================================================

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "=========================================================="
echo "   ✨ FLOWKIT STUDIO — 1-CLICK LAUNCHER (macOS/Linux)     "
echo "=========================================================="

# Check Python3
if ! command -v python3 &>/dev/null; then
    echo "❌ Không tìm thấy Python 3! Vui lòng cài đặt Python 3.10+."
    exit 1
fi

# Activate or create virtual environment
if [ -d "venv" ]; then
    source venv/bin/activate
else
    echo "📦 Đang tạo virtual environment (venv)..."
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip
    pip install -r requirements.txt
    pip install playwright python-multipart pytest-mock
fi

# Run FlowKit Studio
python3 flowkit_studio.py
