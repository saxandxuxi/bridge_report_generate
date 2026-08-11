# -*- coding: utf-8 -*-
import re
from report_agent.recognizer import (
    CHART_TEXT_RE, _position_from_title, _chart_block_locations,
    classify_number, parse_docx, _protect_static_numbers,
)
# 1) 英文图型捕获
for s in ("new_sensor_group_8_time_series", "结构温度1_直方图_1", "结构温度1_时程曲线图"):
    m = CHART_TEXT_RE.search(s)
    print("CHART:", repr(s), "->", next((g for g in m.groups() if g), None) if m else None)
# 2) 表标题去表
print("title:", _position_from_title("吉首侧索塔中截面结构温度监测统计表"))
# 3) 字母前缀编号
r = classify_number("24", "RE24-RE25主桁架节点板", "除RE", "25主桁架")
print("RE24 classify:", r["verdict"], r["reasons"][-1])
# 4) 3.2.2.1 折叠
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
