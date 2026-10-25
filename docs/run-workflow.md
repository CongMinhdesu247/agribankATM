# Quản lý đợt đối soát

Quy trình hiện tại cho phép một đợt đang hoạt động. Các đợt trước vẫn có trong lịch sử.

| Tài khoản | Quyền |
| --- | --- |
| Admin | Xem trước/upload cặp file, xử lý, duyệt đề xuất của người khác, đóng đợt |
| Operator | Xem dữ liệu, gửi yêu cầu xử lý |
| Approver | Xem dữ liệu, phê duyệt hoặc từ chối yêu cầu của người khác |

## Thao tác hằng ngày

1. Admin tải cặp file đầu tiên. Mọi người thấy mã đợt và số giao dịch chưa xử lý/chờ duyệt.
2. Operator gửi đề xuất xử lý. Giao dịch chuyển sang **Chờ duyệt** trong nhật ký.
3. Approver duyệt: giao dịch hoàn tất. Nếu từ chối, giao dịch trở lại danh sách cần xử lý.
4. Khi hai bộ đếm đều bằng 0, admin bấm **Đóng đợt đã hoàn tất** và xác nhận.
5. Nút tải file được mở cho admin để bắt đầu đợt tiếp theo. Đợt cũ vẫn xem được trong **Lịch sử đối soát**, gồm trạng thái đã duyệt.

Không thể upload thay dữ liệu khi đợt hiện tại chưa đóng, kể cả mọi giao dịch đã khớp. Admin không được duyệt yêu cầu do chính mình gửi.

## File trùng và màn hình cũ

- Cặp file trùng nội dung nghiệp vụ sẽ được nhận diện kể cả khi đổi tên, đổi thứ tự dòng hoặc cách viết số tương đương. Web mở đợt đã có trong lịch sử và giữ nguyên kết quả.
- Nếu giá trị nghiệp vụ thay đổi, đó là cặp file mới. Phê duyệt của dữ liệu cũ không tự áp dụng cho nội dung mới.
- Khi có người đóng/mở đợt, màn hình khác kiểm tra thay đổi mỗi 15 giây khi đang hiển thị và cảnh báo tải lại. Mã đợt luôn được kiểm tra ở API; màn hình cũ không thể gửi xử lý vào đợt mới dù mã giao dịch giống nhau.
- Chưa có cơ chế tạm hoãn hoặc chuyển tồn đọng sang đợt sau. Đợt hiện tại phải hoàn tất trước khi mở đợt mới.

## Cập nhật phiên bản

Ứng dụng tự nâng cấp schema cho SQLite và PostgreSQL khi khởi động. Việc nâng cấp giữ dữ liệu hiện tại và bổ sung mã đợt cho dữ liệu cũ chưa có mã. Các đợt cũ không còn hoạt động được hiển thị **Lưu trữ trước nâng cấp**, không đồng nghĩa đã được phê duyệt đầy đủ.

API mới:

- `GET /api/workflow`: mã đợt, trạng thái đóng, người/thời gian đóng, số việc còn lại và điều kiện mở/đóng đợt.
- `POST /api/close-run`: chỉ admin, JSON `{"run_id":"..."}`. Trả 409 nếu còn việc chưa xong hoặc màn hình đã cũ.
- `POST /api/resolve`: bổ sung trường `run_id` bắt buộc.
- Hai API upload chỉ cho admin. Upload trùng trả 409 với `detail.code="duplicate_files"` và mã đợt đã có; preview cung cấp `duplicate_run_id`.
- API khôi phục dữ liệu gốc cũ trả 410; nút khôi phục đã được gỡ để bảo vệ quy trình.

Các kiểm tra dùng DB riêng, không thay đổi DB Aiven đang triển khai. Ngoài bộ kiểm thử Python, luồng khởi tạo/nâng cấp, upload, phê duyệt, đóng đợt, chống trùng và truy vấn lịch sử đã được chạy với PostgreSQL trong bộ nhớ bằng PGlite. Đây chưa phải kiểm thử trực tiếp trên hạ tầng Aiven/Render của bạn.
