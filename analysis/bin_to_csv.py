import struct
import pandas as pd
import argparse

parser = argparse.ArgumentParser()

parser.add_argument("bin_file")
parser.add_argument("csv_file")
args = parser.parse_args()
# Define the struct format: 
# Total = 4 + 8 + 8 + 8 = 28 bytes
struct_fmt = '=IQQq' 
struct_len = struct.calcsize(struct_fmt)
data = []
with open(args.bin_file, 'rb') as f:
    while chunk := f.read(struct_len):
        data.append(struct.unpack(struct_fmt, chunk))

df = pd.DataFrame(data, columns=['seq', 'tx', 'rx', 'latency'])
df.to_csv(args.csv_file, index=False)   
print(f"Converted {len(df)} records.")
