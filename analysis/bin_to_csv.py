import struct
import pandas as pd
import sys
# Define the struct format: 
# Total = 4 + 8 + 8 + 8 = 28 bytes
struct_fmt = '=IQQq' 
struct_len = struct.calcsize(struct_fmt)
path = sys.argv[1]
data = []
with open(path, 'rb') as f:
    while chunk := f.read(struct_len):
        data.append(struct.unpack(struct_fmt, chunk))

df = pd.DataFrame(data, columns=['seq', 'tx', 'rx', 'latency'])
df.to_csv('data/data.csv', index=False)   
print(f"Converted {len(df)} records.")
