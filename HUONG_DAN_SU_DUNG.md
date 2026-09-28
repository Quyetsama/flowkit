# Hướng Dẫn Sử Dụng FlowKit — Tự Động Hóa Google Flow

Tài liệu hướng dẫn toàn diện từ cài đặt, xử lý lỗi thực tế, tạo video qua API cURL, kiểm tra trạng thái, tải video và xử lý hậu kỳ (xóa watermark).

---

## 1. Tổng Quan Kiến Trúc

FlowKit hoạt động dựa trên mô hình kết hợp:
* **FastAPI Backend (`http://127.0.0.1:8100`)**: Cung cấp REST API cho các tác vụ tạo ảnh, video, quản lý dự án, polling trạng thái.
* **Chrome CDP (Remote Debugging Port `9224`)**: Chạy một profile Chrome riêng (`FlowkitChrome`) để điều khiển tự động qua Playwright.
* **FlowKit Chrome Extension**: Cầu nối WebSocket (`ws://127.0.0.1:9222`) bắt token phiên đăng nhập từ `flow.google.com`.

---

## 2. Chuẩn Bị & Cài Đặt Môi Trường

### Yêu cầu hệ thống
* **Hệ điều hành**: macOS / Linux
* **Công cụ**: Python 3.10+, Google Chrome, `ffmpeg`, `jq`

### Cài đặt môi trường Python
```bash
cd /Users/quyetnguyen/Documents/Me/Flowkit

# Tạo và kích hoạt virtual environment
python3 -m venv venv
source venv/bin/activate

# Cài đặt dependencies
pip install -r requirements.txt
pip install playwright python-multipart pytest-mock

# Cài đặt trình duyệt cho Playwright (nếu chưa có)
playwright install chromium
```

---

## 3. Khởi Chạy Hệ Thống

Để tạo video thành công, bạn cần chạy **2 tiến trình song song**:

### Bước 1: Khởi động Chrome với cổng Remote Debugging (CDP)
Chạy lệnh sau trên một Terminal riêng để mở Chrome với profile chuyên dụng:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9224 \
  --user-data-dir="$HOME/Library/Application Support/FlowkitChrome" &
```

> **Lưu ý quan trọng:**
> 1. Trên cửa sổ Chrome vừa mở, truy cập `https://flow.google.com/` và đăng nhập tài khoản Google của bạn.
> 2. Vào `chrome://extensions`, bật **Developer mode** (Chế độ cho nhà phát triển).
> 3. Bấm **Load unpacked** (Tải tiện ích đã giải nén) và chọn thư mục `extension` trong repo Flowkit.
> 4. Ghim (Pin) extension Flowkit lên thanh công cụ.

### Bước 2: Khởi động FlowKit API Server
Mở một Terminal khác và chạy:

```bash
cd /Users/quyetnguyen/Documents/Me/Flowkit
source venv/bin/activate

# Cách 1 (Chuẩn của FlowKit):
python -m agent.main

# Cách 2 (Dùng file main.py ở root):
python main.py

# Cách 3 (Chạy trực tiếp qua uvicorn):
uvicorn agent.main:app --host 127.0.0.1 --port 8100 --reload
```

### Bước 3: Kiểm tra kết nối hệ thống
Kiểm tra xem backend và extension đã sẵn sàng chưa:

```bash
# Kiểm tra extension đã kết nối chưa
curl -s http://127.0.0.1:8100/health
# Kết quả mong muốn: {"extension_connected": true}

# Kiểm tra trạng thái Google Flow và Project ID đang hoạt động
curl -s http://127.0.0.1:8100/api/flow/status | jq .
```

---

## 4. Các Lỗi Thường Gặp & Cách Khắc Phục (Crucial Fixes)

Trong quá trình sử dụng thực tế, bạn có thể gặp các lỗi sau:

### Lỗi 1: Extension hiện "no token"
* **Nguyên nhân**: Bạn chưa mở tab `https://flow.google.com/` hoặc phiên làm việc bị hết hạn cookie.
* **Cách khắc phục**:
  1. Mở tab `https://flow.google.com/` trên trình duyệt Chrome đang bật remote debugging.
  2. Bấm F5 tải lại trang Google Flow.
  3. Bấm vào icon Flowkit Extension trên thanh tiện ích, đảm bảo hiển thị trạng thái `Token Active`.

### Lỗi 2: `UI_GENERATION_FAILED: Locator.wait_for: Timeout 20000ms exceeded ... settings-trigger-button`
* **Nguyên nhân**: Google Flow cập nhật giao diện, bật chế độ "Tác nhân" (`agent-mode-chip`) hoặc mở ngăn kéo (side drawer) che khuất nút bánh răng cài đặt.
* **Khắc phục**: Bản cập nhật Flowkit trong file `agent/services/flow_playwright_generation.py` đã tự động tìm và click tắt chip tác nhân cũng như đóng drawer trước khi click nút settings. Nếu gặp lại, hãy đảm bảo trên giao diện web Google Flow không có popup/modal nào đang chắn màn hình.

### Lỗi 3: Lấy sai hoặc thiếu Project ID
* Bạn có thể xem Project ID đang kết nối bằng lệnh:
  ```bash
  curl -s http://127.0.0.1:8100/api/flow/status | jq -r .flow_project_id
  ```
* Export biến môi trường trong Terminal để tái sử dụng:
  ```bash
  export FLOW_PROJECT_ID=$(curl -s http://127.0.0.1:8100/api/flow/status | jq -r .flow_project_id)
  echo "Project ID: $FLOW_PROJECT_ID"
  ```

---

## 5. Quy Trình Tạo Video Qua API cURL

### 5.1 Gửi yêu cầu tạo Video (Omni Flash / Veo Text-to-Video)

Gửi request POST tới endpoint `/api/flow/generate-video-omni-text`:

```bash
curl -X POST http://127.0.0.1:8100/api/flow/generate-video-omni-text \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "0-3s: Wide shot of a mysterious stickman warrior with a straw hat and katana standing in an ancient dojo, sunbeams piercing through wooden roof boards, floating dust particles. 3-6s: The camera slowly dollies in as the stickman reaches for the katana hilt. 6-8s: Dramatic side lighting, cinematic atmosphere, 8k render, photorealistic shadows. Negative: subtitles, captions, watermark, text on screen, logo, blurry faces.",
    "duration_s": 8,
    "resolution": "720p",
    "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE"
  }'
```

**Các tham số chính:**
* `duration_s`: Thời lượng video (`4`, `6`, `8`, hoặc `10` giây).
* `resolution`: Độ phân giải (`720p` hoặc `360p`).
* `aspect_ratio`: `VIDEO_ASPECT_RATIO_LANDSCAPE` (ngang 16:9) hoặc `VIDEO_ASPECT_RATIO_PORTRAIT` (dọc 9:16).

---

### 5.2 Nhận diện ID từ kết quả trả về

Kết quả trả về sẽ có định dạng JSON tương tự:

```json
{
  "media": [
    { "name": "852bbb90-77dd-4538-a430-5edbb17586d8" }
  ],
  "workflows": [
    {
      "name": "0cbc972a-1253-4e9e-8070-b5267001d803",
      "primary_media_id": "852bbb90-77dd-4538-a430-5edbb17586d8",
      "project_id": "5ee5271c-c32e-464d-8c5f-df2a2cff715e"
    }
  ]
}
```

* **`primary_media_id`**: `852bbb90-77dd-4538-a430-5edbb17586d8` (UUID của video).
* **`workflow_name`**: `0cbc972a-1253-4e9e-8070-b5267001d803`.
* **`project_id`**: `5ee5271c-c32e-464d-8c5f-df2a2cff715e`.

---

### 5.3 Kiểm tra trạng thái và Lấy link tải video

Video Google Flow thường mất khoảng **1 – 3 phút** để render xong.

#### Cách 1: Kiểm tra trực tiếp qua Media ID (Đơn giản nhất)
```bash
# Kiểm tra xem video đã có URL tải về chưa (khi chưa xong sẽ trả về null)
curl -s http://127.0.0.1:8100/api/flow/media/852bbb90-77dd-4538-a430-5edbb17586d8 | jq -r .url
```

#### Cách 2: Kiểm tra qua API `check-status`
```bash
curl -s -X POST http://127.0.0.1:8100/api/flow/check-status \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "5ee5271c-c32e-464d-8c5f-df2a2cff715e",
    "workflows": [
      {
        "name": "0cbc972a-1253-4e9e-8070-b5267001d803",
        "primary_media_id": "852bbb90-77dd-4538-a430-5edbb17586d8"
      }
    ]
  }' | jq .
```

---

### 5.4 Tải Video về máy tính

Khi lệnh kiểm tra trả về một URL (bắt đầu bằng `https://...`), chạy lệnh sau để tự động lấy URL và tải video:

```bash
MEDIA_ID="852bbb90-77dd-4538-a430-5edbb17586d8"
URL=$(curl -s "http://127.0.0.1:8100/api/flow/media/${MEDIA_ID}" | jq -r .url)

# Tải về file video
curl -L -o "my_video.mp4" "$URL"
```

---

## 6. Hậu Kỳ & Xóa Watermark Google AI

Video xuất từ Google Flow tự động bị đóng dấu watermark hình ngôi sao 4 cánh (Google AI Sparkle) ở góc dưới bên phải. Không thể tắt watermark này trên giao diện Google Flow, nhưng bạn có thể xử lý dễ dàng bằng `ffmpeg`.

### Cách 1: Dùng FFmpeg `delogo` (Khuyên dùng — Xóa sạch không méo hình)

Filter `delogo` sẽ tự động lấy các điểm ảnh xung quanh nội suy để làm biến mất hoàn toàn watermark:

* **Cho video 720p (1280x720):**
```bash
ffmpeg -y -i my_video.mp4 -vf "delogo=x=1130:y=575:w=65:h=65" -c:a copy my_video_clean.mp4
```

* **Cho video 1080p (1920x1080):**
```bash
ffmpeg -y -i my_video.mp4 -vf "delogo=x=1700:y=865:w=95:h=95" -c:a copy my_video_clean.mp4
```

### Cách 2: Đè Logo thương hiệu cá nhân lên watermark
Nếu bạn làm video cho kênh YouTube/TikTok, cách tốt nhất là đè logo của bạn lên góc dưới bên phải để che watermark của Google:

```bash
ffmpeg -y -i my_video.mp4 -i "channel_logo.png" \
  -filter_complex "[1:v]scale=90:90[icon];[0:v][icon]overlay=W-w-16:H-h-16" \
  -c:v libx264 -preset fast -crf 18 -c:a copy my_video_branded.mp4
```

---

## 7. Tiêu Chuẩn Viết Prompt Video (Cinematic Standard)

Để video đạt chất lượng hình ảnh và chuyển động cao nhất:
1. **Phân đoạn thời gian (Sub-clip timing)**: Chia rõ các khoảng thời gian (ví dụ: `0-3s`, `3-6s`, `6-8s`).
2. **Mô tả chuyển động máy quay**: Sử dụng các thuật ngữ như `Slow cinematic camera push in`, `tracking shot`, `dolly in`, `dramatic overhead shot`.
3. **Mô tả ánh sáng**: `Volumetric sunbeams`, `golden hour lighting`, `dramatic rim light`, `deep shadows`.
4. **Phần loại trừ (Negative Prompt)**: Luôn kết thúc prompt bằng đoạn sau để ngăn AI tự vẽ thêm watermark giả hoặc chữ lỗi:
   ```text
   Negative: subtitles, captions, watermark, text on screen, logo, blurry faces, distorted hands.
   ```
