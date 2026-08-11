# -*- coding: utf-8 -*-
import json, io
p = r"D:\Code\data_analysis\outputs\analysis\analysis_矮寨大桥.json"
with io.open(p, encoding="utf-8") as f:
    d = json.load(f)
texts = d.get("texts", [])
# 1) 靠茶洞侧梁段30m处
for i, t in enumerate(texts):
    if "靠茶洞侧梁段" in str(t) and "挠度" in str(t):
        start = i
        break
print("== 靠茶洞侧梁段 ==")
for i in range(start, min(start + 20, len(texts))):
    print(i, repr(str(texts[i])[:75]))
print("\n== 应变 直方图_1 ==")
for i, t in enumerate(texts):
    if "直方图_1" in str(t):
        for j in range(i-4, i+10):
            print(j, repr(str(texts[j])[:75]))
        break
