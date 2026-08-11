# -*- coding: utf-8 -*-
import re
from docx import Document
doc = Document(r"D:\Code\data_analysis\templates\矮寨大桥_template_v2.docx")
paras = [p.text for p in doc.paragraphs]
print("== 螺栓段 ==")
for t in paras:
    if "螺栓" in t and "扭力" in t:
        print(repr(t[:260]))
        break
print("\n== 靠茶洞侧梁段 ==")
for t in paras:
    if "靠茶洞侧梁段" in t:
        print(repr(t[:80]))
        break
for i, t in enumerate(paras):
    if "靠茶洞侧梁段30m处截面挠度监测曲线图" in t or "靠茶洞侧梁段" in t and "挠度监测曲线图" in t:
        for j in range(i, i+4):
            print(" ", repr(paras[j]))
        break
print("\n== 应变 3.2.2.1 直方图_1 ==")
for i, t in enumerate(paras):
    if "3.2.2.1" in t:
        for j in range(i, min(i+10, len(paras))):
            print(j, repr(paras[j]))
        break
print("\n== 倾角 3.3.5.1 ==")
for i, t in enumerate(paras):
    if "3.3.5.1" in t:
        for j in range(i, min(i+8, len(paras))):
            print(j, repr(paras[j]))
        break
print("\n== 全局 ==")
print("残留 [A-:", len([t for t in paras if re.search(r"\[[A-Z]-[A-Z0-9-]+", t)]))
print("30m 被替换:", len([t for t in paras if re.search(r"侧梁段\{\{data\.\d+\}\}m", t)]))
