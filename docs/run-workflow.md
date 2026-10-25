# Quản lý đợt đối soát

Quy trình hiện tại cho phép một đợt đang hoạt động. Các đợt trước vẫn có trong lịch sử.

| Tài khoản | Quyền |
| --- | --- |
| Admin | Xem trước/upload cặp file, xử lý, duyệt đề xuất của người khác, đóng đợt, lưu CSV/tạm dừng và tiếp tục đợt cũ |
| Operator | Xem dữ liệu, gửi yêu cầu xử lý |
| Approver | Xem dữ liệu, phê duyệt hoặc từ chối yêu cầu của người khác |

## Thao tác hằng ngày

1. Admin tải cặp file đầu tiên. Mọi người thấy mã đợt và số giao dịch chưa xử lý/chờ duyệt.
2. Operator gửi đề xuất xử lý. Giao dịch chuyển sang **Chờ duyệt** trong nhật ký.
3. Approver duyệt: giao dịch hoàn tất. Nếu từ chối, giao dịch trở lại danh sách cần xử lý.
4. Khi hai bộ đếm đều bằng 0, admin bấm **Đóng đợt đã hoàn tất** và xác nhận.
5. Nút tải file được mở cho admin để bắt đầu đợt tiếp theo. Đợt cũ vẫn xem được trong **Lịch sử đối soát**, gồm trạng thái đã duyệt.

Trong chế độ thường, không thể upload thay dữ liệu khi đợt hiện tại chưa đóng, kể cả mọi giao dịch đã khớp. Ngoại lệ xử lý khẩn yêu cầu admin lưu CSV và tạm dừng theo quy trình dưới đây. Admin không được duyệt yêu cầu do chính mình gửi.

## Khi có cặp file cần xử lý gấp

1. Admin bấm **Lưu CSV và tạm dừng**, xác nhận, và giữ lại file `tam_dung_….csv` vừa tải xuống. CSV chứa toàn bộ giao dịch chưa xử lý và đang chờ duyệt, gồm mã yêu cầu, người đề xuất, hình thức xử lý, mã tham chiếu và ghi chú.
2. Đợt cũ được tạm dừng: operator và approver không thể sửa hoặc duyệt trong lúc đó. Hệ thống giữ thêm bản lưu trong DB. Chỉ sau khi trình duyệt nhận đủ CSV và xác nhận đúng mã kiểm tra, nút **Upload cặp file xử lý gấp** mới sáng.
3. Admin tải cặp file gấp, mọi người xử lý/phê duyệt bình thường rồi admin **Đóng đợt đã hoàn tất**.
4. Trong **Các đợt đang tạm dừng**, bấm **Nhập CSV để tiếp tục đợt cũ**, chọn nguyên file đã lưu và xác nhận.
5. Hệ thống mở lại đợt cũ. Giao dịch chưa xử lý trở lại danh sách; các yêu cầu chờ duyệt giữ nguyên mã, người đề xuất và ghi chú. Giao dịch đã được duyệt trước đó vẫn hoàn tất, không phải làm lại.

Các lưu ý:

- Nếu tải lỗi, đợt vẫn được giữ nhưng nút upload khẩn chưa mở. Bấm **Tải lại CSV công việc** hoặc **Tải lại CSV** trong danh sách để nhận lại đúng bản lưu.
- Có thể tải lại CSV của một đợt đang tạm dừng ngay cả khi đã mở đợt khác. Nếu không muốn mở đợt gấp nữa, nhập CSV để tiếp tục ngay đợt đang tạm dừng.
- Chỉ đóng hoặc lưu tạm đợt hiện tại trước khi chuyển sang đợt đã lưu khác. Đợt tạm dừng không bị coi là đã hoàn tất và yêu cầu chờ duyệt không bị hủy.
- Không chỉnh sửa hoặc lưu lại CSV bằng Excel. Hệ thống kiểm tra toàn bộ byte của file; bản bị sửa sẽ bị từ chối. Một bản lưu đã tiếp tục xử lý không được nhập lại lần nữa; nếu cần tạm dừng tiếp, xuất bản mới.
- CSV chứa dữ liệu giao dịch cần giữ kín và có giới hạn 64 MB. File chỉ khôi phục công việc khi DB gốc vẫn giữ bản lưu; đây không thay thế bản sao lưu toàn bộ DB.
- Việc xác nhận của trình duyệt chứng minh đã nhận đủ nội dung, không kiểm tra được quá trình ghi file của hệ điều hành. Nếu file không xuất hiện trong thư mục tải xuống, admin có thể tải lại từ danh sách.

## File trùng và màn hình cũ

- Cặp file trùng nội dung nghiệp vụ sẽ được nhận diện kể cả khi đổi tên, đổi thứ tự dòng hoặc cách viết số tương đương. Web mở đợt đã có trong lịch sử và giữ nguyên kết quả.
- Nếu giá trị nghiệp vụ thay đổi, đó là cặp file mới. Phê duyệt của dữ liệu cũ không tự áp dụng cho nội dung mới.
- Khi có người đóng/mở đợt, màn hình khác kiểm tra thay đổi mỗi 15 giây khi đang hiển thị và cảnh báo tải lại. Mã đợt luôn được kiểm tra ở API; màn hình cũ không thể gửi xử lý vào đợt mới dù mã giao dịch giống nhau.
- Khi tạm dừng/tiếp tục đợt, màn hình cũ cũng bị khóa thao tác và yêu cầu tải lại. Nhật ký ghi rõ yêu cầu nào thuộc đợt tạm dừng; việc phê duyệt chỉ mở cho đợt đang hoạt động.

## Cập nhật phiên bản

Ứng dụng tự nâng cấp schema cho SQLite và PostgreSQL khi khởi động. Việc nâng cấp giữ dữ liệu hiện tại và bổ sung mã đợt cho dữ liệu cũ chưa có mã. Các đợt cũ không còn hoạt động được hiển thị **Lưu trữ trước nâng cấp**, không đồng nghĩa đã được phê duyệt đầy đủ.

API mới:

- `GET /api/workflow`: mã đợt, trạng thái đóng, người/thời gian đóng, số việc còn lại và điều kiện mở/đóng đợt.
- `POST /api/close-run`: chỉ admin, JSON `{"run_id":"..."}`. Trả 409 nếu còn việc chưa xong hoặc màn hình đã cũ.
- `POST /api/resolve`: bổ sung trường `run_id` bắt buộc.
- Hai API upload chỉ cho admin. Upload trùng trả 409 với `detail.code="duplicate_files"` và mã đợt đã có; preview cung cấp `duplicate_run_id`.
- API khôi phục dữ liệu gốc cũ trả 410; nút khôi phục đã được gỡ để bảo vệ quy trình.
- `POST /api/suspensions/export`: admin gửi `run_id`, nhận CSV và các header `X-Suspension-ID`, `X-Reconciliation-Run-ID`. Việc gọi lại khi đang tạm dừng trả cùng bản CSV.
- `POST /api/suspensions/{id}/acknowledge`: gửi `run_id` và `csv_hash` SHA-256 của nội dung tải đầy đủ; sau đó mới được upload đợt khẩn.
- `GET /api/suspensions` và `GET /api/suspensions/{id}.csv`: danh sách và tải lại bản lưu.
- `POST /api/suspensions/resume`: multipart `saved_file` và `expected_run_id`. Trả 409 nếu file bị sửa, đã dùng, đợt thay đổi hoặc còn công việc chưa được đóng/lưu tạm.

Migration phiên bản 3 bổ sung trạng thái tạm dừng, bảng bản lưu và chi tiết công việc trên SQLite/PostgreSQL. Công cụ chuyển SQLite sang PostgreSQL cũng sao chép các bản lưu này.

Các kiểm tra dùng DB riêng, không thay đổi DB Aiven đang triển khai. Ngoài bộ kiểm thử Python, luồng khởi tạo/nâng cấp, upload, phê duyệt, đóng đợt, chống trùng và truy vấn lịch sử đã được chạy với PostgreSQL trong bộ nhớ bằng PGlite. Đây chưa phải kiểm thử trực tiếp trên hạ tầng Aiven/Render của bạn.
