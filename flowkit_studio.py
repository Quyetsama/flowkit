#!/usr/bin/env python3
"""FlowKit Studio — Cross-Platform Multi-Profile Batch Automation Tool.

Launches the local FlowKit Studio Web Application and automatically opens
the dashboard in your browser (Windows & macOS).

Usage:
    python flowkit_studio.py
    # or: ./run_studio.sh / run_studio.bat
"""

import os
import sys
import time
import webbrowser
from pathlib import Path

# Add project root to sys.path
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import uvicorn
from agent.config import API_HOST, API_PORT


def main():
    print("=" * 65)
    print("   ✨ FLOWKIT STUDIO — GOOGLE FLOW BATCH GENERATOR ✨")
    print("   Hệ Thống Tự Động Hóa Đa Luồng & Đa Profile (Windows / macOS)")
    print("=" * 65)

    studio_url = f"http://127.0.0.1:{API_PORT}/studio"
    print(f"\n🚀 Đang khởi chạy máy chủ tại: {studio_url}")
    print("🌐 Tự động mở trình duyệt sau 2 giây...")
    print("💡 Nhấn Ctrl + C để dừng máy chủ bất kỳ lúc nào.\n")

    # Auto-open browser after slight delay
    def open_browser():
        time.sleep(1.8)
        webbrowser.open(studio_url)

    import threading
    t = threading.Thread(target=open_browser, daemon=True)
    t.start()

    # Run Uvicorn
    uvicorn.run(
        "agent.main:app",
        host=API_HOST,
        port=API_PORT,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
