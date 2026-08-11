# -*- coding: utf-8 -*-
import report_agent.recognizer as R
import re
print(repr(R.CHART_TEXT_RE.pattern))
for s in ("结构温度1_直方图_1", "结构温度1_时程曲线图"):
    m = R.CHART_TEXT_RE.search(s)
    print(repr(s), "->", m.groups() if m else None)
