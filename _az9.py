# -*- coding: utf-8 -*-
import re
from docx import Document
doc = Document(r"D:\Code\data_analysis\templates\矮寨大桥_template_v3.docx")
paras = [p.text for p in doc.paragraphs]
print("== 螺栓 ==")
for t in paras:
    if "螺栓进行检测" in t:
        print(repr(t[:240]))
        break
print("\n== 3.2.2.1 温度 ==")
for i, t in enumerate(paras):
    if "3.2.2.1" in t:
        for j in range(i, i + 5):
            print(j, repr(paras[j]))
        break
print("\n== 3.3.1.8 挠度 ==")
for i, t in enumerate(paras):
    if "靠茶洞侧梁段30m处截面挠度监测曲线图" in t:
        for j in range(i, i + 4):
            print(j, repr(paras[j]))
        break
print("\n== 倾角 ==")
for i, t in enumerate(paras):
    if "3.3.5.1" in t:
        for j in range(i, i + 6):
            print(j, repr(paras[j]))
        break
print("\n== 全局 ==")
print("残留 [A-:", len([t for t in paras if re.search(r"\[[A-Z]-[A-Z0-9-]+", t)]))
print("双位置重复:", len([t for t in paras if re.search(r"chart\.[a-z_]+_[^}]*表[^}]*_[a-z_]+", t)]))
