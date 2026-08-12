# -*- coding: utf-8 -*-
import re
from report_agent.recognizer import CHART_MULTI_RE
for s in ("位移_906_907_908_时程曲线_3x1", "位移_906_907_908_频率分布_3x1", "位移*906*907*908*时程曲线_x1"):
    m = CHART_MULTI_RE.match(s)
    print(repr(s), "->", m.groups() if m else None)
