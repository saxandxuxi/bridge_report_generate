# -*- coding: utf-8 -*-
import re
from report_agent.recognizer import CHART_MULTI_RE, parse_docx, _chart_block_locations

for s in ("位移*906*907*908*时程曲线_x1", "位移*906_907_908*频率分布_3x1"):
    m = CHART_MULTI_RE.match(s)
    print(repr(s), "->", m.groups() if m else None)

parsed = parse_docx(r"D:\Code\data_analysis\inputs\矮寨大桥.docx")
texts = parsed["texts"]
# 找滑坡体位移节
for i, t in enumerate(texts):
    if "滑坡体位移空间变位" in str(t) and "监测曲线图" in str(t):
        s = i
        print("节:", s, repr(str(t)[:60]))
        for j in range(s, s + 10):
            print(" ", j, repr(str(texts[j])[:70]))
        break
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
print("块位置:", _chart_block_locations(s + 1, cell_ref_paras, headings, texts=texts))
