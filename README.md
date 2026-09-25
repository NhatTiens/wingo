# v7.6.3 — Visible Manual Login Real Bet

Sửa UX của v7.6.2:
- Sau khi login thành công, xóa `last_error` cũ.
- Ghi ngay `current_balance` lên dashboard.
- Trạng thái chuyển thành `STOPPED_READY / AWAITING_MANUAL_START`.
- Không tự ARM và không tự đặt cược; vẫn bắt buộc người dùng tick xác nhận + START TOOL.
- Nếu Chromium bị đóng, writer fail-closed và phải chạy lại/login lại.

# Wingo Adaptive v7.6.2 — Visible Chromium + Manual Login Each Run

Bản này được cấu hình theo yêu cầu:
- Chromium luôn HIỂN THỊ (`headless=false`).
- Mỗi lần chạy writer mở một browser context sạch.
- Người dùng tự đăng nhập tài khoản trực tiếp trên website 88i.
- Tool không nhận, không ghi file và không lưu username/password.
- Sau login, Chromium giữ mở để bạn nhìn thấy chọn TOP7, nhập tiền và click Đặt Cược.
- `dry_run=false`: đây là chế độ cược tiền thật.
- Luồng cược thật chỉ chọn 7 số TOP7 và đặt cùng mức tiền cho mỗi số. Mặc định paper bet Lớn/Nhỏ cũng tắt; các xác suất Lớn/Nhỏ vẫn được in để quan sát.
- Writer restart luôn STOPPED/DISARMED; login thành công không tự đặt cược. Bạn vẫn phải tick xác nhận và bấm START TOOL trên dashboard.

## Điều kiện STOP mặc định
Nếu dashboard để trống:
- 5 LOSS liên tiếp
- lợi nhuận +100% so với số dư lúc START
- số dư giảm còn 50% số dư lúc START
- 90 phút
- đặt cược countdown second 10

Không có giới hạn 100 lệnh.

## Chạy

PowerShell 1:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
python -u .\wingo_id77_adaptive_v7_6.py
```

Chromium sẽ mở. Đăng nhập trực tiếp trên website và chờ terminal báo:

```text
[88I LOGIN] LOGIN OK
```

PowerShell 2:

```powershell
.\venv\Scripts\Activate.ps1
python -m streamlit run .\wingo_dashboard_v7_6.py
```

Mở http://localhost:8501, nhập điều kiện STOP nếu muốn thay mặc định, tick xác nhận và bấm START TOOL.

## Session hết hạn giữa lúc chạy

Nếu không đọc được balance, tool fail-closed và STOP. Không cố đặt cược.
Hãy restart writer để mở Chromium sạch, đăng nhập lại rồi START phiên mới.

## Lưu ý

`dry_run=false` trong package này vì đây là bản visible real-bet theo yêu cầu. START TOOL vẫn là công tắc bắt buộc.

Kiểm thử offline, không chạm website và không gửi lệnh tiền thật: `python -m unittest -v test_realbet`. Các kiểm thử xác nhận gate, giỏ 7 số, trạng thái END, kỳ cược đổi, giỏ cũ, dry run, cược trùng và xác nhận lệnh. Việc trang 88i thực tế có còn đúng selector và hiển thị tổng tiền như cấu hình chỉ xác nhận được khi chạy dry run trên tài khoản đã đăng nhập.
