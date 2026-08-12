# -*- coding: utf-8 -*-
import re
from docx import Document
doc = Document(r"D:\Code\data_analysis\templates\矮寨大桥_template_v9.docx")
paras = [p.text for p in doc.paragraphs]
print("== 滑坡体位移节 ==")
for i, t in enumerate(paras):
    if "滑坡体位移空间变位的变化情况" in t:
        for j in range(i, min(i + 8, len(paras))):
            print(j, repr(paras[j]))
        break
print("\n== 全局 ==")
print("滑坡体位移占位符:", [t for t in paras if "滑坡体位移" in t and "chart." in t])
print("位移_ 残留:", len([t for t in paras if re.match(r"^位移[_＊*]?\d", t.strip())]))
