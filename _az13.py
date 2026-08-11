# -*- coding: utf-8 -*-
import json, io
p = r"D:\Code\data_analysis\outputs\analysis\analysis_矮寨大桥_v4.json"
with io.open(p, encoding="utf-8") as f:
    d = json.load(f)
print("data.2637 =", d.get("data_values", {}).get("data.2637"))
print("data.2638 =", d.get("data_values", {}).get("data.2638"))
for n in d.get("numbers", []):
    if n.get("paragraph") == 140 and n.get("value") not in ("100","648","825"):
        if n.get("verdict") == "replace" or "RE24" in str(n.get("value")):
            print(json.dumps({k: n.get(k) for k in ("value","verdict","placeholder","position","confidence","reasons")}, ensure_ascii=False)[:400])
