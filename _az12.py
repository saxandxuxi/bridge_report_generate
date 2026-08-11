# -*- coding: utf-8 -*-
import re
from docx import Document
doc = Document(r"D:\Code\data_analysis\templates\矮寨大桥_template_v4.docx")
paras = [p.text for p in doc.paragraphs]
print("== 螺栓 ==")
for t in paras:
    if "螺栓进行检测" in t:
        print(repr(t[:240]))
        break
print("\n== 3.2.2.1 / 3.3.1.8 / 倾角 抽查 ==")
for i, t in enumerate(paras):
    if t.startswith("{{chart.structure_temperature_吉首侧索塔中截面") or t.startswith("{{chart.deflection_靠茶洞") or t.startswith("{{chart.rotation_吉首侧主梁支座"):
        print(repr(t))
print("\n== 全局 ==")
print("RE24 被替换:", len([t for t in paras if "RE24" in t and "{{data" in t]))
print("残留 [A-:", len([t for t in paras if re.search(r"\[[A-Z]-[A-Z0-9-]+", t)]))
