# -*- coding: utf-8 -*-
import json, io
p = r"D:\Code\data_analysis\outputs\analysis\analysis_矮寨大桥_v3.json"
with io.open(p, encoding="utf-8") as f:
    d = json.load(f)
texts = d.get("texts", [])
for i, t in enumerate(texts):
    if "螺栓进行检测" in str(t):
        para = i
        print(i, repr(str(t)[:100]))
        break
for n in d.get("numbers", []):
    if n.get("paragraph") == para and n.get("value") in ("24", "25", "100", "648", "825"):
        print(json.dumps({k: n.get(k) for k in ("value","verdict","placeholder","position","confidence","reasons")}, ensure_ascii=False))
