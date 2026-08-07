# -*- coding: utf-8 -*-
"""项目依赖引导：优先使用项目内 vendor/ 目录（内嵌依赖，免配环境）。

所有入口脚本（run_agent.py / serve_scheduler.py / web/app.py / analyze_report.py /
preprocess/pipeline.py）都会先 import 本模块，把 vendor/ 与 .deps/ 加入 sys.path。
服务器上如已安装同版本依赖，vendor 不存在时自动回退到系统环境。
"""

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
for _sub in ("vendor", ".deps"):
    _p = os.path.join(_ROOT, _sub)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
