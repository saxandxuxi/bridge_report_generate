# -*- coding: utf-8 -*-
import re
from report_agent.recognizer import STATIC_NUMBER_RE, CHART_TEXT_RE, parse_docx, _protect_static_numbers, _chart_block_locations

# 1) 静态保护
tests = ["抽取了61个螺栓进行检测", "除2774主桁架右幅上弦杆顶面大桩号侧节点板",
         "扭力值均在62～63N·m", "靠茶洞侧梁段30m处截面挠度监测"]
for t in tests:
    ms = [m.group(0) for m in STATIC_NUMBER_RE.finditer(t) if any(c.isdigit() for c in m.group(0))]
    print("保护:", repr(t[:30]), "->", ms)

# 2) 直方图_1 行识别
for s in ("结构温度1_直方图_1", "结构温度1_时程曲线图", "2#_2#墩墩顶位移时程曲线图"):
    m = CHART_TEXT_RE.search(s)
    print("CHART:", repr(s), "->", m.group(1) if m else None)

# 3) X/Y 折叠
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
start = next(i for i, t in enumerate(texts) if "3.3.5.1" in str(t))
print("倾角块位置:", _chart_block_locations(start + 2, cell_ref_paras, headings, texts=texts))
