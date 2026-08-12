# -*- coding: utf-8 -*-
import os, glob
from docx import Document
for tpl in glob.glob(r"D:\Code\data_analysis\templates\*大桥_template*.docx"):
    try:
        doc = Document(tpl)
    except Exception:
        continue
    for tbl in doc.tables:
        rows = [[c.text.strip() for c in r.cells] for r in tbl.rows]
        if any("车道1" in str(c) for r in rows for c in r):
            print("===", os.path.basename(tpl))
            for r in rows:
                print("  ", r)
            break
