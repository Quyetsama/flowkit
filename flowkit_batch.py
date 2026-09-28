#!/usr/bin/env python3
"""FlowKit Batch CLI Runner — Command Line Batch Generator.

Usage:
    python flowkit_batch.py --prompts prompts.txt --mode video --duration 8 --delogo --output output/my_run
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

# Add project root to sys.path
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agent.studio.batch_engine import batch_engine
from agent.studio.models import BatchJobConfig
from agent.studio.profile_manager import profile_manager


def parse_args():
    parser = argparse.ArgumentParser(description="FlowKit Batch CLI Automation Runner")
    parser.add_argument(
        "--prompts", "-p",
        required=True,
        help="Đường dẫn file .txt / .csv chứa danh sách prompt (mỗi dòng 1 prompt)",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["video", "image"],
        default="video",
        help="Chế độ tạo (video hoặc image, mặc định video)",
    )
    parser.add_argument(
        "--duration", "-d",
        type=int,
        choices=[4, 6, 8, 10],
        default=8,
        help="Thời lượng video (giây, mặc định 8s)",
    )
    parser.add_argument(
        "--resolution", "-r",
        choices=["720p", "360p"],
        default="720p",
        help="Độ phân giải video (mặc định 720p)",
    )
    parser.add_argument(
        "--aspect", "-a",
        choices=["landscape", "portrait"],
        default="landscape",
        help="Tỷ lệ khung hình (landscape 16:9 hoặc portrait 9:16)",
    )
    parser.add_argument(
        "--output", "-o",
        default="output/batch_cli",
        help="Thư mục lưu file kết quả (mặc định output/batch_cli)",
    )
    parser.add_argument(
        "--delogo",
        action="store_true",
        default=True,
        help="Tự động xóa watermark Google AI góc dưới phải (mặc định bật)",
    )
    parser.add_argument(
        "--no-delogo",
        dest="delogo",
        action="store_false",
        help="Tắt tự động xóa watermark",
    )
    parser.add_argument(
        "--profiles",
        default="all",
        help="Danh sách ID profile sử dụng (phân cách bằng dấu phẩy, hoặc 'all')",
    )
    return parser.parse_args()


async def run_batch(args):
    prompt_file = Path(args.prompts)
    if not prompt_file.exists():
        print(f"❌ Lỗi: Không tìm thấy file prompt tại '{args.prompts}'")
        sys.exit(1)

    with open(prompt_file, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]

    if not lines:
        print(f"❌ Lỗi: File '{args.prompts}' không có prompt nào!")
        sys.exit(1)

    print(f"📋 Đã nạp {len(lines)} prompt từ: {prompt_file}")

    # Check profiles
    await profile_manager.check_all_profiles()
    all_profiles = profile_manager.list_profiles()
    connected = [p for p in all_profiles if p.is_connected]

    print(f"🔍 Kiểm tra profiles: {len(connected)}/{len(all_profiles)} profiles đang kết nối.")
    for p in all_profiles:
        status_str = "✅ Sẵn sàng" if p.is_connected else "❌ Chưa mở Chrome"
        print(f"   - [{p.id}] {p.name} (Port: {p.cdp_port}) &rarr; {status_str}")

    if not connected:
        print("\n⚠️ Không có profile nào đang kết nối Google Flow!")
        print("💡 Hãy mở FlowKit Studio bằng lệnh: python flowkit_studio.py")
        print("   hoặc mở Chrome thủ công với cdp port tương ứng.")
        sys.exit(1)

    selected_profiles = []
    if args.profiles != "all":
        selected_profiles = [p.strip() for p in args.profiles.split(",") if p.strip()]

    aspect_ratio = (
        "VIDEO_ASPECT_RATIO_LANDSCAPE"
        if args.aspect == "landscape"
        else "VIDEO_ASPECT_RATIO_PORTRAIT"
    )

    config = BatchJobConfig(
        prompts=lines,
        task_type=args.mode,
        duration_s=args.duration,
        resolution=args.resolution,
        aspect_ratio=aspect_ratio,
        auto_delogo=args.delogo,
        output_dir=args.output,
        selected_profiles=selected_profiles,
    )

    print(f"\n🚀 Đang khởi chạy Batch ({args.mode.upper()} {args.duration}s, {args.resolution})...")
    res = await batch_engine.start_batch(config)
    if not res.get("success"):
        print(f"❌ Không thể bắt đầu: {res.get('error')}")
        sys.exit(1)

    print(f"⚡ Batch đã kích hoạt! Phân bổ {len(lines)} nhiệm vụ lên {res.get('workers')} workers.\n")

    # Polling progress
    last_completed = 0
    while batch_engine.is_running:
        status = batch_engine.get_status()
        completed = status["completed"]
        failed = status["failed"]
        percent = status["progress_percent"]

        # Simple terminal progress line
        sys.stdout.write(
            f"\r⏳ Tiến độ: [{percent:3d}%] | Hoàn thành: {completed}/{status['total']} | Lỗi: {failed}  "
        )
        sys.stdout.flush()
        await asyncio.sleep(2.5)

    print("\n\n" + "=" * 60)
    final_status = batch_engine.get_status()
    print("🎉 BATCH HOÀN TẤT!")
    print(f"📁 Thư mục lưu: {final_status['output_dir']}")
    print(f"📊 Thành công: {final_status['completed']}/{final_status['total']}")
    if final_status["failed"] > 0:
        print(f"⚠️ Thất bại: {final_status['failed']}")
    print("=" * 60)


def main():
    args = parse_args()
    asyncio.run(run_batch(args))


if __name__ == "__main__":
    main()
