# v7.6.3 — Visible Manual Login Real Bet

## Cược thật TOP7 theo mức 2.000 → 6.000 → 8.000 → 10.000đ/số

Mỗi lệnh chỉ chọn 7 số. Mức mặc định là 2.000đ mỗi số, tổng 14.000đ.
Nếu thua, lệnh tiếp theo là 6.000đ mỗi số, tổng 42.000đ. Nếu thua tiếp,
lệnh tiếp theo là 8.000đ mỗi số, tổng 56.000đ; sau lần thua thứ ba là
10.000đ mỗi số, tổng 70.000đ. Nếu tiếp tục thua, giữ mức 10.000đ mỗi số.
Thắng ở bất cứ bước nào cũng trở lại 2.000đ mỗi số. Kỳ bị bỏ qua do TOP7 Gate
không đạt không thay đổi bước. Bot dừng khi số dư không đủ cho cả 7 số ở mức
hiện tại và hiển thị số dư, tổng tiền cần và số tiền thiếu trên dashboard.
Không còn tự dừng theo số lần thua, mục tiêu lợi nhuận, tỷ lệ giảm số dư hoặc thời gian.
Lỗi vận hành và lệnh chưa xác nhận vẫn dừng để tránh đặt trùng cược.
Chỉ mức cược thật đổi; các chiến thuật thử nghiệm trên giấy giữ nguyên.

## Xác nhận cược và lệnh UNKNOWN

WIN/LOSS của lệnh đã được website xác nhận dựa trực tiếp vào số mở thưởng:
số ra nằm trong 7 số đã chọn là WIN, còn lại là LOSS. Việc chốt kết quả
không chờ số dư tăng do trả thưởng và không tự cộng tiền thưởng dự kiến vào
số dư hiển thị. Số dư chỉ được dùng ở bước riêng để xác nhận website nhận
lệnh cược và kiểm tra đủ tiền trước khi cược.

Sau khi bấm đặt cược, writer đọc số dư trực tiếp trên trang hiện tại tối đa 12 giây.
Nếu số dư chưa cập nhật sau 5 giây, writer tải lại trang đúng một lần, không gửi thêm lệnh.
Chỉ khi số dư giảm đúng tổng tiền TOP7, lệnh mới được xác nhận `OPEN`.
Nếu vẫn không xác nhận được thì lệnh là `UNKNOWN` và writer dừng để tránh cược trùng.
Dãy số của lệnh UNKNOWN vẫn được đối chiếu với số mở thưởng và hiển thị
Trúng/Trượt; đó chưa phải tiền thắng/thua thực nếu 88i chưa xác nhận đơn cược.
Danh sách “Đặt cược thành công” trên website có thể gồm lệnh cũ nên không được dùng
một mình để tự động gỡ `UNKNOWN` cho một kỳ cược cụ thể.

Với lệnh `UNKNOWN` đã tồn tại: dừng writer, kiểm tra đúng **kỳ, 7 số và tổng tiền**
trong lịch sử cược 88i. Xem dữ liệu DB trước (không sửa):

```powershell
python .\reconcile_real_bet.py --issue 202609252574
```

Chỉ khi website xác nhận đúng kỳ `202609252574`, bảy số `4,9,8,7,6,1,3`
và tổng cược cũ `21000`, đối soát (script tự tạo backup DB và chốt kết quả nếu đã có):

```powershell
python .\reconcile_real_bet.py --issue 202609252574 --mark-placed --expected-numbers 4,9,8,7,6,1,3 --expected-total 21000
```

Sau đó chạy lại writer, đăng nhập và bấm START TOOL. Không xóa DB để gỡ UNKNOWN.

Với kỳ `202609252643` trong ảnh mới, dừng writer rồi xem DB:

```powershell
python .\reconcile_real_bet.py --issue 202609252643
```

Nếu lịch sử 88i xác nhận đã nhận đúng kỳ này, 7 số `0,4,3,6,5,9,1`
và tổng `14000`, dùng:

```powershell
python .\reconcile_real_bet.py --issue 202609252643 --mark-placed --expected-numbers 0,4,3,6,5,9,1 --expected-total 14000
```

Nếu lịch sử 88i xác nhận không nhận lệnh đó, dùng lệnh dưới đây thay thế:

```powershell
python .\reconcile_real_bet.py --issue 202609252643 --mark-not-placed --expected-numbers 0,4,3,6,5,9,1 --expected-total 14000
```

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

## Điều kiện STOP
Bot tự dừng khi không đủ tiền cho 7 số của bước hiện tại. Các lỗi login,
đọc số dư, thao tác giỏ cược và xác nhận lệnh vẫn dừng tool; END và EMERGENCY STOP
vẫn dùng được. Mặc định submit tại countdown second 10.

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

Mở http://localhost:8501, điều chỉnh giây cược nếu muốn, tick xác nhận và bấm START TOOL.

## Session hết hạn giữa lúc chạy

Nếu không đọc được balance, tool fail-closed và STOP. Không cố đặt cược.
Hãy restart writer để mở Chromium sạch, đăng nhập lại rồi START phiên mới.

## Lưu ý

`dry_run=false` trong package này vì đây là bản visible real-bet theo yêu cầu. START TOOL vẫn là công tắc bắt buộc.

Kiểm thử offline, không chạm website và không gửi lệnh tiền thật: `python -m unittest -v test_realbet`. Các kiểm thử xác nhận gate, giỏ 7 số, trạng thái END, kỳ cược đổi, giỏ cũ, dry run, cược trùng và xác nhận lệnh. Việc trang 88i thực tế có còn đúng selector và hiển thị tổng tiền như cấu hình chỉ xác nhận được khi chạy dry run trên tài khoản đã đăng nhập.
