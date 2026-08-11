# -*- coding: utf-8 -*-
import re
pat = r"[\d.+\-–—~～eE%％,，\s]+"
for v in ("RE24-RE25", "24-25", "100", "648～825"):
    print(repr(v), "->", bool(re.fullmatch(pat, v.strip())))
