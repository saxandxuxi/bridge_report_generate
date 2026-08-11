# -*- coding: utf-8 -*-
import json, io
p = r"D:\Code\data_analysis\outputs\analysis\analysis_矮寨大桥_v3.json"
with io.open(p, encoding="utf-8") as f:
    d = json.load(f)
# 找所有 value 24/25 或 position 35/40/62/67 的条目
for n in d.get("numbers", []):
    if n.get("paragraph") == 140:
        v = n.get("value"); pos = n.get("position")
        if v in ("24","25") or pos in (35,40,62,67):
            print(json.dumps({k: n.get(k) for k in ("value","verdict","placeholder","position","confidence","reasons")}, ensure_ascii=False))
print("--- data.2763/2764 出现在哪 ---")
for k, v in d.get("data_values", {}).items():
    if k in ("data.2763", "data.2764"):
        print(k, "=", v)
