# HƯỚNG DẪN SỬ DỤNG & TÀI LIỆU KỸ THUẬT FLOWKIT STUDIO

**FlowKit Studio** là công cụ tự động hóa toàn diện cho Google Flow, hỗ trợ chạy đa luồng song song, quản lý nhiều tài khoản (Google Profiles), tạo hàng loạt Video và Ảnh, tự động tải về và xóa watermark 1-click trên cả macOS và Windows.

---

## 1. Tổng Quan & Mục Đích

Trước đây, quy trình tạo video bằng Google Flow đòi hỏi nhiều thao tác dòng lệnh thủ công phức tạp:
1. Mở Chrome với remote debugging port đặc biệt qua terminal.
2. Gửi lệnh cURL tạo video/ảnh kèm payload mã hóa.
3. Gửi lệnh cURL polling kiểm tra trạng thái render.
4. Lấy link tải và gõ lệnh cURL download.
5. Dùng lệnh ffmpeg để xóa watermark Google AI.

**FlowKit Studio** đóng gói toàn bộ quy trình này thành một ứng dụng Web UI đồ họa trực quan chạy cục bộ (`http://127.0.0.1:8100/studio`), giúp người dùng tạo hàng chục đến hàng trăm video/ảnh hoàn toàn tự động chỉ với 1 nút bấm.

---

## 2. Kiến Trúc Hệ Thống (Architecture)

FlowKit Studio gồm 3 tầng chính:

```
┌────────────────────────────────────────────────────────┐
│             Web UI (HTML5 / Tailwind CSS)              │
│       Tab Batch Studio       │    Tab Profiles Manager │
└───────────────────────────▲────────────────────────────┘
                            │ SSE / REST API
┌───────────────────────────▼────────────────────────────┐
│                  FastAPI Backend Server                │
│    agent/studio/routes.py    │ agent/studio/models.py  │
│    agent/studio/batch_engine.py                        │
└─────────────┬───────────────────────────┬──────────────┘
              │                           │
              ▼                           ▼
┌──────────────────────────┐  ┌──────────────────────────┐
│ Profile 1 Worker         │  │ Profile 2 Worker         │
│ CDP Port: 9224           │  │ CDP Port: 9225           │
│ UserDir: FlowkitChrome   │  │ UserDir: profile_2       │
│ Chrome Tab (Account A)   │  │ Chrome Tab (Account B)   │
└──────────────────────────┘  └──────────────────────────┘
```

1. **Profile Manager (`agent/studio/profile_manager.py`)**:
   - Quản lý cấu hình nhiều tài khoản trong file `profiles.json`.
   - Cấp phát các cổng Remote Debugging CDP độc lập (9224, 9225, 9226...).
   - Tự động dò tìm đường dẫn nhị phân Google Chrome trên mọi hệ điều hành (macOS, Windows, Linux).
   - Kiểm tra kết nối trực tiếp đến Chrome qua endpoint loopback `/json` để kiểm tra tài khoản nào đang mở Flow.

2. **Batch Engine (`agent/studio/batch_engine.py`)**:
   - Sử dụng hàng đợi `asyncio.Queue` và phân bổ tác vụ cho các worker theo từng profile độc lập.
   - Tương thích 2 chế độ tạo:
     - **Video**: Gọi `generate_omni_flash_text_video` (Omni 1.1 Flash 4s/6s/8s/10s, 720p/360p, 16:9/9:16).
     - **Ảnh**: Gọi `generate_images` (Nano Banana 2 / Pro, 1:1, 16:9, 9:16, 4:3, 3:4).
   - Tự động polling kiểm tra tiến độ đến khi có đường dẫn tải file.
   - Tự động download file vào thư mục output đã chọn.
   - Tự động xóa watermark bằng thuật toán delogo của FFmpeg nếu người dùng bật tùy chọn.
   - Phát sự kiện trạng thái thời gian thực qua Server-Sent Events (SSE) `/api/studio/events`.

3. **Playwright Bridge (`agent/services/flow_playwright_generation.py`)**:
   - Kết nối vào Chrome qua CDP port của profile được chỉ định.
   - Thao tác composer trên giao diện web của Google Flow (nhập prompt, chọn model, aspect ratio, nhấn Generate) để sinh captcha hợp lệ từ Google.

---

## 3. Hướng Dẫn Sử Dụng Nhanh (Quick Start)

### Cách khởi động 1-Click
- **Windows**: Nhấp đúp chuột vào file `run_studio.bat`.
- **macOS**: Nhấp đúp chuột vào file `run_studio.command` hoặc chạy lệnh:
  ```bash
  python flowkit_studio.py
  # hoặc: ./run_studio.sh
  ```
Trình duyệt sẽ tự động mở giao diện tại: **`http://127.0.0.1:8100/studio`**

---

### Quy trình tạo hàng loạt (Step-by-Step)

#### Bước 1: Chuẩn bị Profile & Đăng nhập Google Flow
1. Chuyển sang tab **"Quản lý Profiles"** trên giao diện Studio.
2. Với mỗi profile muốn sử dụng, bấm nút **"Mở Chrome"**.
3. Cửa sổ Chrome mở ra, hãy đăng nhập tài khoản Google và truy cập vào `https://flow.google.com/`.
4. Mở sẵn một dự án (hoặc bấm tạo một Dự án mới) trên cửa sổ Chrome đó.
5. Quay lại trang Studio, bấm nút **"Làm mới"** — trạng thái profile sẽ chuyển sang xanh: **"Đang chạy & Đã kết nối"** kèm mã Dự án (Active Project).

#### Bước 2: Cấu hình Batch
1. Chuyển sang tab **"Tạo hàng loạt"**.
2. **Nhập danh sách Prompt**:
   - Dán trực tiếp danh sách câu lệnh prompt (mỗi dòng một prompt), hoặc
   - Bấm **"Chọn file .txt"** để tải file danh sách prompt lên.
3. **Cấu hình thông số**:
   - Chọn loại tác vụ: **Video** hoặc **Hình ảnh**.
   - Video: Chọn thời lượng (4s, 6s, 8s, 10s), độ phân giải (720p hoặc 360p), tỷ lệ khung hình (16:9 ngang hoặc 9:16 dọc).
   - Hình ảnh: Chọn model ảnh (Nano Banana 2 / Pro), tỷ lệ ảnh (1:1, 16:9, 9:16, 4:3, 3:4).
4. **Tùy chọn nâng cao**:
   - Tích chọn **"Tự động xóa Watermark Google AI"** nếu muốn video/ảnh tải về không có logo.
   - Chọn thư mục lưu file (mặc định là `output/batch_YYYYMMDD_HHMMSS`).
   - Chọn các tài khoản (Profiles) tham gia xử lý song song.

#### Bước 3: Chạy & Giám sát
1. Bấm nút **"BẮT ĐẦU TẠO HÀNG LOẠT"**.
2. Bảng tiến trình hiển thị trạng thái từng dòng:
   - `queued`: Đang chờ lượt trong hàng đợi.
   - `submitting`: Đang gửi lệnh tạo vào Google Flow.
   - `generating`: Đang chờ Google render xong.
   - `downloading`: Đang tải file về máy.
   - `delogoing`: Đang tự động xóa watermark.
   - `completed`: Hoàn tất, có nút Xem trước và Tải về.
3. Khi hoàn tất, bấm nút **"Mở thư mục"** để xem các file video/ảnh đã tạo trên ổ đĩa.

---

## 4. Ghi Chú Kỹ Thuật Về Lỗi & Bug Còn Tồn Đọng (Known Issues)

### 🐛 Bug: Xung đột Đa Profile & Chuyển hướng 404 (`404?reason=project`)

#### 1. Hiện tượng:
Khi cấu hình từ 2 Profile trở lên và chạy batch đồng thời:
- Profile 1 (hoặc một trong các profile) bị chuyển hướng trên Chrome sang trang lỗi:
  `https://flow.google.com/404?reason=project` ("Không tìm thấy trang").
- Một profile khác đứng yên tại màn hình dự án nhưng không thực thi.
- Studio báo lỗi: `FlowBatchError: ogiZ0b: UI_GENERATION_FAILED`.

#### 2. Nguyên nhân cốt lõi (Root Cause):
- **Cơ chế phân quyền riêng tư của Google Flow**: Dự án Google Flow được gắn chặt với Google Account tạo ra nó. Tài khoản A không thể mở hoặc thao tác trên URL project của tài khoản B. Nếu Chrome của tài khoản A cố truy cập vào `https://flow.google.com/project/<project_id_cua_B>`, Google Flow sẽ redirect ngay lập tức về `https://flow.google.com/404?reason=project`.
- **Dùng chung cổng CDP & Singleton Project ID**:
  - Trong kiến trúc ban đầu của backend FlowKit, các hàm gọi RPC `batch_rpc`, `generate_omni_flash_text_video` và `generate_images` sử dụng cổng CDP mặc định `9224` (`_CDP_ENDPOINT`) và biến singleton `client._batch_active_project` dùng chung cho toàn bộ tiến trình Python.
  - Khi chạy đồng thời 2 worker, luồng của Profile 2 lại kết nối sang port 9224 (Chrome của Profile 1) và ép Profile 1 phải điều hướng sang `project_id` của Profile 2.
  - Hậu quả: Chrome của Profile 1 bị 404. Tại trang 404, Playwright không tìm thấy nút tạo ảnh/video `button.settings-trigger-button` và sau 20s sẽ timeout báo lỗi `UI_GENERATION_FAILED`. Trong khi đó, Chrome của Profile 2 trên port 9225 không nhận được lệnh điều khiển.

#### 3. Các bản vá đã triển khai (Implemented Fixes):
1. **Phân tách luồng CDP Endpoint**:
   - `batch_engine.py` hiện truyền rõ ràng `cdp_endpoint=http://127.0.0.1:<port>` và `project_id=profile.active_project_id` xuống mọi tầng (`generate_omni_flash_text_video`, `generate_images`, `get_media`, `_batch_media_urls`, `batch_rpc`).
2. **Cơ chế tự phục hồi chống 404 trong `flow_playwright_generation.py`**:
   - Hàm `_flow_page` kiểm tra URL hiện tại của tab. Nếu thấy chứa `404`, tự động điều hướng trở về `https://flow.google.com/` và mở thẻ dự án sẵn có của tài khoản đó.
   - Không ép chuyển URL sang project ID khác nếu tab Chrome hiện tại đã mở sẵn một dự án hợp lệ.
3. **Cập nhật nhận diện Profile trong `profile_manager.py`**:
   - Lọc đúng các tab kiểu `page`, trích xuất `active_project_id` từ URL tab thực tế và lưu vào `profiles.json`.

#### 4. Vấn đề còn tồn đọng & Điểm cần lưu ý tiếp theo:
1. **Yêu cầu mở sẵn Project trên từng Chrome Profile trước khi chạy**:
   - Do tính năng tự động tạo Project mới qua API Google Flow (`o0cbec`) đôi khi yêu cầu tương tác giao diện hoặc bị rate-limit, khuyến nghị hiện tại là: **Mỗi profile Chrome cần được người dùng mở sẵn một dự án Google Flow trước khi bắt đầu bấm chạy batch**.
2. **Tranh chấp Focus cửa sổ trình duyệt (Window Focus & Input)**:
   - Playwright chạy tương tác click chuột và bàn phím (`page.locator().click()`, `page.keyboard.press()`) trên cùng một màn hình máy tính. Nếu nhiều cửa sổ Chrome của các profile cùng chạy song song, việc chiếm focus có thể làm một trong các thao tác click bị lệch hoặc popup menu bị ẩn ngoài ý muốn.
   - *Hướng giải quyết tiếp theo*: Nghiên cứu chuyển hoàn toàn sang gửi RPC ngầm (`batchexecute` fetch qua JS injection trong trang) thay vì dùng Playwright click chuột mô phỏng người dùng, giúp các profile chạy nền song song mà không cần can thiệp chuột/bàn phím.
3. **Độ trễ và Cooldown giữa các lượt submit**:
   - Google Flow áp dụng giới hạn bảo vệ hoạt động bất thường (`PUBLIC_ERROR_UNUSUAL_ACTIVITY`). Khi gửi nhiều prompt dồn dập, worker cần tuân thủ thời gian giãn cách tối thiểu (khoảng 10-15 giây giữa các lượt submit trong cùng 1 profile) để tránh bị khóa tạm thời.

---

## 5. Danh Mục Mã Nguồn Liên Quan

| Tệp tin | Chức năng chính |
| :--- | :--- |
| `flowkit_studio.py` | Điểm khởi chạy chính ứng dụng web Studio (port 8100). |
| `agent/studio/routes.py` | API endpoints cho giao diện Studio (`/api/studio/*`). |
| `agent/studio/models.py` | Pydantic models: `ProfileConfig`, `BatchJobConfig`, `BatchTask`. |
| `agent/studio/profile_manager.py` | Quản lý Profile Chrome, CDP port, tự động mở trình duyệt. |
| `agent/studio/batch_engine.py` | Engine điều phối hàng đợi tác vụ, worker song song, tải file, delogo. |
| `agent/studio/templates/studio.html` | Toàn bộ giao diện người dùng (UI SPA kèm CSS & JS). |
| `agent/services/flow_playwright_generation.py` | Kết nối Playwright tới Chrome qua CDP, tự phục hồi 404, điều khiển composer. |
| `agent/services/flow_client.py` | Client giao tiếp với Flow batch RPC, nhận `cdp_endpoint` theo profile. |
| `agent/services/omni_flash.py` | Các hàm tạo video Omni Flash 1.1 (text, frame, first-last, ref-video). |
| `tests/unit/test_studio.py` | Bộ unit test kiểm tra profile lifecycle và cách ly đa profile. |

---

## 6. Hướng Dẫn Kiểm Thử (Unit Tests)

Để chạy kiểm thử toàn bộ hệ thống FlowKit Studio và backend:
```bash
./venv/bin/pytest tests/unit/test_studio.py -v
./venv/bin/pytest tests/unit/
```
Toàn bộ 411 unit tests hiện tại đều đạt chuẩn PASS 100%.
