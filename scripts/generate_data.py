import csv
import random
from datetime import datetime, timedelta

NUM_RECORDS = 10000  # Bắt đầu thử nghiệm với 10.000 dòng

def generate_mock_transactions():
    core_records = []
    partner_records = []
    
    channels = ['MOBILE', 'ATM', 'POS']
    start_time = datetime.now() - timedelta(days=1)

    for i in range(1, NUM_RECORDS + 1):
        trans_id = f"TX{202610050000 + i}"
        amount = random.choice([100000, 200000, 500000, 1000000, 2000000])
        trans_time = (start_time + timedelta(seconds=i*5)).strftime("%Y-%m-%d %H:%M:%S")
        channel = random.choice(channels)
        
        # Mặc định 98% giao dịch thành công và khớp nhau
        rand_val = random.random()
        
        if rand_val < 0.98:  # 98% MATCHED
            core_records.append([trans_id, amount, 'SUCCESS', trans_time, channel])
            partner_records.append([trans_id, amount, 'SUCCESS', trans_time, channel])
            
        elif rand_val < 0.985:  # Lệch tiền (AMOUNT_MISMATCH)
            core_records.append([trans_id, amount, 'SUCCESS', trans_time, channel])
            partner_records.append([trans_id, amount - 10000, 'SUCCESS', trans_time, channel])
            
        elif rand_val < 0.99:  # Lệch trạng thái (STATUS_MISMATCH / PENDING)
            core_records.append([trans_id, amount, 'SUCCESS', trans_time, channel])
            partner_records.append([trans_id, amount, 'PENDING', trans_time, channel])
            
        elif rand_val < 0.995:  # Mất vết ở Partner (MISSING_IN_PARTNER)
            core_records.append([trans_id, amount, 'SUCCESS', trans_time, channel])
            
        else:  # Mất vết ở Core (MISSING_IN_CORE)
            partner_records.append([trans_id, amount, 'SUCCESS', trans_time, channel])

    # Ghi ra file CSV
    with open('core_log.csv', 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['trans_id', 'amount', 'status', 'trans_time', 'channel'])
        writer.writerows(core_records)

    with open('partner_log.csv', 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['trans_id', 'amount', 'status', 'trans_time', 'channel'])
        writer.writerows(partner_records)

    print("Generated core_log.csv and partner_log.csv successfully.")

if __name__ == '__main__':
    generate_mock_transactions()
