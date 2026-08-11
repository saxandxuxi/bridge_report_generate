# -*- coding: utf-8 -*-
import json, io
p = r"D:\Code\data_analysis\outputs\analysis\analysis_矮寨大桥.json"
with io.open(p, encoding="utf-8") as f:
    d = json.load(f)
texts = d.get("texts", [])
for i, t in enumerate(texts):
    if "主梁支座倾角监测" in str(t) and "时程曲线" in str(t):
        start = i
        break
print("== 倾角节 ==")
for i in range(start-2, min(start + 16, len(texts))):
    print(i, repr(str(texts[i])[:80]))
print("\n== 螺栓 ==")
for i, t in enumerate(texts):
    if "螺栓" in str(t) and "扭力" in str(t):
        print(i, repr(str(t)[:300]))
        break
