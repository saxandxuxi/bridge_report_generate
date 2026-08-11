# -*- coding: utf-8 -*-
import json, io, re
from docx import Document
# 1) 模板螺栓内容
doc = Document(r"D:\Code\data_analysis\templates\矮寨大桥_template_v2.docx")
for k, para in enumerate(doc.paragraphs):
    if "螺栓进行检测" in para.text or "100个螺栓" in para.text:
        print("TPL 螺栓:", k, repr(para.text[:180]))
        break

# 2) 3.2.2.1 折叠复现
from report_agent.recognizer import _chart_block_locations, parse_docx
parsed = parse_docx(r"D:\Code\data_analysis\inputs\矮寨大桥.docx")
texts = parsed["texts"]
cell_ref_paras = {}
for ct in parsed["chart_texts"]:
    if ct.get("source") == "cell_ref":
        pp = ct.get("paragraph")
        rl = str(ct.get("row_label") or "").strip()
        if isinstance(pp, int) and rl and not rl.startswith("测点"):
            cell_ref_paras[pp] = (str(ct.get("table_title") or ""), rl)
headings = [i for i, t in enumerate(texts)
            if re.match(r"^\d+(?:\.\d+){0,3}\.?(?=[\u4e00-\u9fa5\s])", str(t).strip())
            and len(str(t).strip()) <= 60]
s = next(i for i, t in enumerate(texts) if "3.2.2.1" in str(t))
print("3.2.2.1 位置:", _chart_block_locations(s + 2, cell_ref_paras, headings, texts=texts))
s8 = next(i for i, t in enumerate(texts) if "3.3.1.8" in str(t))
print("3.3.1.8 位置:", _chart_block_locations(s8 + 2, cell_ref_paras, headings, texts=texts))
