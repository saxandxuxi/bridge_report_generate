# -*- coding: utf-8 -*-
from docx import Document
doc = Document(r"D:\Code\data_analysis\templates\矮寨大桥_template_v8.docx")
for tbl in doc.tables:
    rows = [[c.text.strip() for c in r.cells] for r in tbl.rows]
    if any("车道1" in str(c) for r in rows for c in r):
        for r in rows:
            print(r)
        break
