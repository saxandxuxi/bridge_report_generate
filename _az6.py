# -*- coding: utf-8 -*-
import json, io
p = r"D:\Code\data_analysis\outputs\analysis\analysis_矮寨大桥_v2.json"
with io.open(p, encoding="utf-8") as f:
    d = json.load(f)
texts = d.get("texts", [])
print("== 螺栓内容 ==")
for i, t in enumerate(texts):
    if "螺栓进行检测" in str(t) or "扭力值" in str(t):
        print(i, repr(str(t)[:260]))
print("\n== 3.2.2.1 块 chart_texts ==")
for i, t in enumerate(texts):
    if "3.2.2.1" in str(t):
        s = i
        break
for ct in d.get("chart_texts", []):
    pp = ct.get("paragraph")
    if isinstance(pp, int) and s <= pp < s + 12:
        print(pp, "|", ct.get("source"), "|", ct.get("_unique_chart_id") or ct.get("chart_id"), "|", ct.get("text","")[:40], "| loc:", ct.get("location",""))
print("\n== 3.3.1.8 块 ==")
for i, t in enumerate(texts):
    if "3.3.1.8" in str(t):
        s8 = i
        break
for ct in d.get("chart_texts", []):
    pp = ct.get("paragraph")
    if isinstance(pp, int) and s8 <= pp < s8 + 8:
        print(pp, "|", ct.get("source"), "|", ct.get("_unique_chart_id") or ct.get("chart_id"), "|", ct.get("text","")[:40], "| loc:", ct.get("location",""))
