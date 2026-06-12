# FINDINGS 

## 1. Lựa chọn Similarity Function 

E đã sử dụng một hàm similarity hybrid (lai) thay vì chỉ dùng một điểm số dựa trên raw-text (văn bản thô) hoặc metric (chỉ số) duy nhất. Điểm số cuối cùng là sự pha trộn có trọng số: log 0.38, trace 0.34, affected service (dịch vụ bị ảnh hưởng) 0.18, và metric 0.10. Các log được chuẩn hóa thành template và token; các trace được so sánh cả dưới dạng token của cạnh có hướng (directed edge token) và chữ ký bất thường của cạnh (edge anomaly signature); các service sử dụng độ chồng chéo Jaccard; các metric sử dụng cosine trên các tên metric bị thay đổi.

E đã xem xét việc dùng cosine trên toàn bộ token đơn giản hơn, nhưng nó khiến các trường hợp có bằng chứng xung đột trở nên quá giòn (dễ gãy/sai lệch). E06 là ví dụ dẫn đến sự lựa chọn này: các log hàng đầu của nó là các thông báo về payment pool, nhưng cạnh trace mạnh nhất là `cart-svc->cart-redis` với error rate (tỷ lệ lỗi) 0.3459 và p99 deviation (độ lệch p99) 3.2412. Dạng biểu diễn hybrid giúp duy trì sự hiện diện của cả hai tín hiệu. Quyết định cuối cùng là leo thang (escalate) với `page_oncall` thay vì tự động rollback (auto-rolling back) `payment-svc`; cả hai hành động thanh toán đều bị chặn do độ tin cậy được hiệu chuẩn (calibrated confidence) của chúng giảm xuống dưới ngưỡng vùng ảnh hưởng (blast-radius threshold).

## 2. Hiệu ứng của Outcome-Weighted Voting (Bỏ phiếu theo trọng số kết quả)

Trọng số kết quả (outcome weighting) đã thay đổi xếp hạng rõ ràng nhất trên E05. Các lân cận hàng đầu (top neighbors) là `INC-2025-09-05` ở mức similarity 0.7541 với kết quả (outcome) `success`, `INC-2026-05-10` ở mức 0.7541 với kết quả `partial`, và `INC-2025-07-04` ở mức 0.7331 với kết quả `success`.

Nếu không có outcome weighting, incident (sự cố) về pool thành công gần nhất sẽ khiến `rollback_service` và `increase_pool_size` trông gần như hòa nhau vì cả hai đều xuất hiện trong `INC-2025-09-05`. Với outcome weighting, `partial` chỉ đóng góp 0.45x, vì vậy `INC-2026-05-10` cộng thêm 0.3393 cho `rollback_service` thay vì trọn vẹn 0.7541. Sức mạnh bỏ phiếu (vote strength) cuối cùng là: `rollback_service` 1.4825, `increase_pool_size` 1.1432, và `restart_pod` 0.7331. Việc được hỗ trợ thêm tiền lệ có trọng số đó đã giúp `rollback_service:payment-svc` chiến thắng ở E05.

## 3. Ví dụ về tính toán EV (Giá trị kỳ vọng)

Đối với E05, tập hợp các ứng viên là:

- `rollback_service:payment-svc`: confidence 0.9287, vote strength 1.4825, cost 10 min, downtime 2 min, blast radius 1, utility 1.4559.
- `increase_pool_size:payment-svc`: confidence 0.8799, vote strength 1.1432, cost 1 min, downtime 0 min, blast radius 1, utility 1.4356.
- `restart_pod:payments-db`: confidence 0.8263, vote strength 0.7331, cost 2 min, downtime 1 min, blast radius 1, utility 1.1043.
- `page_oncall`: confidence 0.7902, vote strength 0.2169, cost 0, blast radius 0, utility 0.4639.

Công thức tiện ích (utility formula) thưởng cho độ tin cậy được hiệu chuẩn và sức mạnh bỏ phiếu, sau đó trừ đi cost (chi phí), downtime (thời gian chết), rủi ro vùng ảnh hưởng (blast-radius risk), và hình phạt cho khoảng thời gian rollback (rollback-window penalty). Đối với các auto-action, hành động cũng phải vượt qua ngưỡng rủi ro dựa trên blast radius và downtime. Trên E05, `rollback_service` đã vượt qua ngưỡng 0.62 của nó với confidence là 0.9287 và đánh bại `increase_pool_size` với mức chênh lệch utility là 0.0203, do đó nó đã được chọn.

## 4. Hành vi Escalation (Leo thang)

Engine đã chọn `page_oncall` trên E02, E04, E06, E07 và E08. Cả năm trường hợp đều được chấp nhận bởi ground truth (dữ liệu chuẩn). E07 là trường hợp OOD (Out-of-Distribution) rõ ràng nhất: độ similarity tốt nhất chỉ là 0.2504, dưới ngưỡng OOD 0.28, vì vậy `no_precedent` (không có tiền lệ) là true và engine đã leo thang với độ tin cậy 0.7496.

E04 cũng leo thang vì độ similarity của tiền lệ tốt nhất là 0.1795. E08 leo thang vì độ similarity tốt nhất là 0.2223, mặc dù bằng chứng trace của nó đã xác định `esb->t24-service`; không có sự trùng khớp lịch sử gần gũi nào cho chuỗi service đó. E06 không phải là OOD, với độ similarity tốt nhất là 0.4877, nhưng các auto-action trên `payment-svc` đã bị chặn vì các log và trace không thống nhất. Độ tin cậy cho rollback thanh toán là 0.5314, dưới ngưỡng rủi ro 0.62 của nó, vì vậy `page_oncall` là hành động được phép.

## 5. Chế độ lỗi có khả năng xảy ra cao nhất (Most Likely Failure Mode)

Chế độ lỗi có khả năng xảy ra cao nhất là một chuỗi service mới chia sẻ ngôn ngữ log chung chung với một incident đã biết. E08 cho thấy điều này: các log chứa văn bản về cạn kiệt pool và cạnh trace trỏ đến `esb->t24-service`, nhưng các kết quả khớp lịch sử tốt nhất là các incident cũ về payment pool chỉ ở mức độ similarity 0.2223. Việc leo thang đã được chấp nhận, nhưng engine không tự động đề xuất `rollback_service:t24-service`, vốn cũng được chấp nhận.

Một cải tiến cụ thể sẽ là nhắm mục tiêu lại gốc theo nhận thức topology (topology-aware root retargeting) cho các luồng cascade: khi một cạnh trace có error rate cao và service luồng xuôi (downstream) là lá/gốc của luồng cascade, hãy nhắm mục tiêu lại các hành động dành riêng cho service vào service downstream đó nếu log template ngược lại là một khớp lịch sử mạnh mẽ. E đã không triển khai nó vì nó nằm giữa Layer 2 và Layer 3 và sẽ cần các cơ chế bảo vệ cẩn thận để tránh việc biến các kết quả khớp văn bản chung chung thành các auto-action không an toàn.

## Tóm tắt phiên chạy (Run Summary)

Tệp `audit.jsonl` được tạo ra chứa chính xác 8 mục nhập: từ E01 đến E08. Các hành động được chọn là: `increase_pool_size` một lần, `rollback_service` hai lần và `page_oncall` năm lần. Chạy lệnh `python grade.py --audit audit.jsonl --expected eval/expected.json` tạo ra kết quả:

```text
Correct: 8/8
Forbidden (chose must_not_action): 0/8
Missing from audit: 0/8
Auto-rubric estimate (excluding FINDINGS + features review): 85/85
```

