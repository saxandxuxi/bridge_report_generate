# -*- coding: utf-8 -*-
import json, io
p = r"D:\Code\data_analysis\outputs\analysis\analysis_矮寨大桥_v4.json"
with io.open(p, encoding="utf-8") as f:
    d = json.load(f)
for n in d.get("numbers", []):
    v = str(n.get("value", ""))
    if "RE" in v or "24-25" in v:
        print(json.dumps({k: n.get(k) for k in ("value","verdict","placeholder","position","paragraph","reasons")}, ensure_ascii=False)[:300])
        break
# 检查 missed 是否在 summary
print("summary.missed:", d.get("summary", {}).get("llm", {}).get("missed"))
