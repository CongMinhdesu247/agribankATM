# Rà soát bảo mật ngày 25/10/2026

Phạm vi: toàn bộ mã Python, giao diện HTML/JavaScript, script quản trị, cấu hình Docker/Render, danh sách file Git và kiểm thử trong repository. Các kiểm thử dùng cơ sở dữ liệu SQLite tạm riêng, không sửa dữ liệu giao dịch hay thông tin đăng nhập hiện có.

## Vấn đề đã sửa

| Vấn đề | Khả năng bị lợi dụng | Thay đổi |
| --- | --- | --- |
| Đăng nhập thành công xóa bộ đếm theo `IP OR username` | Người có tài khoản hợp lệ có thể xóa lịch sử thử sai của tài khoản khác cùng IP, làm yếu giới hạn dò mật khẩu | Tính mọi lần đăng nhập, giữ hạn mức độc lập theo IP và tài khoản; ghi nhận trước khi băm mật khẩu để chặn yêu cầu đồng thời vượt hạn mức |
| Băm mật khẩu trong khóa ghi dùng chung | Yêu cầu đăng nhập gây chậm các thao tác ghi khác, kể cả đối soát/phê duyệt | Chạy scrypt sau khi nhả khóa; kiểm tra lại tài khoản trước khi cấp phiên |
| Đăng xuất chỉ xóa cookie; mở khóa tài khoản làm token cũ có hiệu lực lại | Bản sao token bị lấy trước đó vẫn truy cập được đến khi hết hạn, hoặc sau khi tài khoản được mở lại | Thêm bảng `app_sessions` cho SQLite và PostgreSQL; chỉ lưu hash token, thu hồi phiên khi đăng xuất và toàn bộ phiên khi khóa tài khoản |
| Chỉ giới hạn body của hai API upload | Người chưa đăng nhập có thể gửi JSON rất lớn vào API đăng nhập, gây tiêu tốn bộ nhớ trước xác thực | Giới hạn body thông thường 64 KiB, giữ hạn mức upload riêng; kiểm tra cả Content-Length và số byte nhận thực tế; trả 413 cả khi parser bắt ngoại lệ |
| CSV suy luận kiểu trước khi chuẩn hóa tên cột, chấp nhận cấu trúc không rõ ràng | Mã có số 0 đầu có thể bị thay đổi; dòng thừa/thiếu trường có thể bị diễn giải khác ý định; nhiều cột làm tăng bộ nhớ | Đọc CSV nghiêm ngặt theo dòng, giữ mã dạng chuỗi, yêu cầu số trường khớp header, giới hạn 100 cột/250.000 dòng, chỉ dựng DataFrame cho ba cột cần thiết; từ chối NUL, header trùng và trường nghiệp vụ quá dài |
| Chưa chặn riêng yêu cầu đăng nhập từ website khác | Có thể bị ép đăng nhập vào tài khoản do bên tấn công kiểm soát nếu vượt qua các giới hạn trình duyệt | Kiểm tra Origin và Fetch Metadata tại API đăng nhập |
| Docker context còn có thể chứa `outputs/`, SQLite dạng khác và file khóa | `COPY . .` có thể đưa báo cáo cục bộ, bản sao DB hoặc khóa riêng vào image triển khai | Bổ sung quy tắc loại trừ Docker và Git cho dữ liệu tạm, SQLite journal/WAL, thư mục thông tin xác thực và khóa riêng |
| Ngày kết thúc `9999-12-31` gây tràn khi cộng một ngày | Yêu cầu hợp lệ về cú pháp gây lỗi 500 | Trả lỗi kiểm tra đầu vào 422 |

## Gia cố bổ sung

- Tách JavaScript khỏi HTML và chuyển handler sang callback rõ ràng, thêm Content Security Policy chặn script nội tuyến, object, iframe và kết nối API ngoài nguồn.
- Thêm SHA-384 SRI và `crossorigin="anonymous"` cho bốn tài nguyên CDN. Hash được tính từ đúng URL phiên bản đang dùng.
- Thêm HSTS khi HTTPS hoặc `COOKIE_SECURE=true`.
- Mỗi lần đăng nhập sinh token khác nhau; giao diện chỉ hoàn tất đăng xuất sau khi máy chủ xác nhận.
- Xóa dependency xác thực cũ không dùng để tránh đường xác thực chỉ kiểm tra chữ ký mà bỏ qua trạng thái phiên.

Việc thu hồi phiên phía máy chủ phù hợp với [OWASP Session Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html). Giới hạn toàn bộ body bổ sung cho giới hạn từng phần multipart; xem [tài liệu Starlette](https://starlette.dev/requests/).

## Kết quả xác minh

- `python -m unittest discover -s tests -v`: **39 bài kiểm thử qua**. Có kiểm thử đầy đủ upload → preview → đối soát → đề xuất → người khác phê duyệt → xuất CSV; kiểm tra CSRF, phân quyền, SQL injection, token giả/hết hạn/thu hồi, đăng nhập đồng thời, giới hạn body và CSV sai cấu trúc.
- `node --check agribank_recon/static/app.js`: qua.
- `pip-audit -r requirements.txt`: không phát hiện lỗ hổng đã biết trong 28 gói được giải tại thời điểm kiểm tra. Báo cáo quét riêng môi trường `.venv` liệt kê 25 gói và cũng không phát hiện lỗ hổng đã biết. Không cần đổi phiên bản thư viện để xử lý kết quả lần quét này.
- Bandit quét `agribank_recon/` và `scripts/`: 0 cảnh báo mức cao, 9 mức trung bình và 4 mức thấp. Đã kiểm tra thủ công: SQL động ghép từ tên bảng/cột cố định hoặc đã kiểm tra; giá trị bên ngoài dùng tham số. Các cảnh báo còn lại là chuỗi `token_type="cookie"` và bộ sinh ngẫu nhiên cho dữ liệu demo. Không che các cảnh báo bằng cấu hình bỏ qua.
- Kiểm tra Chrome trên bản chạy riêng dùng dữ liệu thử: dashboard, biểu đồ, tab tài khoản, nhật ký và lịch sử tải được; không ghi nhận lỗi JavaScript/CSP trong console. Bản xem thử dùng dependency override trong `outputs/`, không thuộc mã triển khai.
- Kiểm tra danh sách file Git và lịch sử tên file nhạy cảm: `.env`, `.env.railway`, DB, CSV nghiệp vụ và backups không được theo dõi; `.env.example` chứa placeholder được giữ lại. Đây không phải chứng nhận rằng mọi bí mật trong mọi commit đã được phát hiện.

Kết quả JSON của hai lần quét thư viện và Bandit được lưu trong `outputs/`, thư mục bị loại khỏi Git và Docker.

## Khi triển khai bản cập nhật

- Sao lưu DB trước khi triển khai. Ứng dụng tự tạo bảng/index phiên khi khởi động; không xóa dữ liệu nghiệp vụ. Người dùng cần đăng nhập lại do token cũ chưa có phiên phía máy chủ.
- API client dùng Bearer phải dùng token đã được đăng ký qua luồng đăng nhập. API login vẫn dùng cookie như trước; client có thể giữ cookie qua cookie jar.
- Dùng HTTPS, đặt `COOKIE_SECURE=true`, cấu hình `APP_ALLOWED_HOSTS` đúng hostname. Giữ hạn mức request/rate/timeout tại reverse proxy; giới hạn ứng dụng không thay thế chống DDoS ở hạ tầng.
- Hạn mức đăng nhập hiện tính cả lần thành công: 20/IP/5 phút và 10/tài khoản/15 phút. Các nhân viên dùng chung IP có thể cùng chạm hạn mức.
- Công cụ di chuyển SQLite sang PostgreSQL không chuyển phiên đăng nhập; người dùng đăng nhập lại sau di chuyển.

Chưa kiểm thử tích hợp trên PostgreSQL thật hoặc hạ tầng Render/Railway, chưa thực hiện pentest dịch vụ công khai. Kết quả rà soát mã nguồn và các công cụ tự động không bảo đảm loại bỏ mọi lỗ hổng.
