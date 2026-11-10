#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模板质量审计：把模板里的 chart/cell 占位符逐条拿去真实图库/统计库试解析，
输出匹配率与未命中清单，用于判断“模板生成的占位符是否大都正确”。

用法：
  python audit_template_quality.py --template templates/洞庭湖大桥_template_v3.docx \
      --config config/config_dongtinghu.json \
      --out outputs/analysis/template_quality_v3.json
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from report_agent.bridge_source import BridgeData
from report_agent.config import load_config
from report_agent.report_builder import iter_block_items


def main() -> int:
    ap = argparse.ArgumentParser(description="模板占位符质量审计")
    ap.add_argument("--template", required=True)
    ap.add_argument("--config", default="config/config.json")
    ap.add_argument("--out", default="outputs/analysis/template_quality.json")
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-03-31")
    ap.add_argument("--semantic", action="store_true",
                    help="语义模式：只按名称对照/测点映射判断位置是否可解析，"
                         "不要求存在图库/统计值文件")
    args = ap.parse_args()

    cfg = load_config(args.config)
    bd = dict(cfg.get("bridge_data") or {})
    for k in ("stats_dir", "charts_dir", "sensor_map", "name_dict",
              "overview"):
        v = bd.get(k) or ""
        if v and not os.path.isabs(v):
            bd[k] = os.path.join(os.getcwd(), v)
    bridge = BridgeData(bd)
    status = bridge.load()

    period = {
        "start": dt.date.fromisoformat(args.start),
        "end": dt.date.fromisoformat(args.end),
    }

    doc = Document(args.template)
    chart_total = chart_ok = 0
    missing_charts = []
    cell_total = cell_ok = 0
    missing_cells = []
    reason_counter = Counter()
    recent = []
    cur_title = ""

    def _handle_cell(metric, column, stat, row_index, title):
        nonlocal cell_total, cell_ok
        cell_total += 1
        try:
            val, detail = bridge.resolve_cell_detail(
                metric, column, stat, period,
                table_title=title or "", row_index=row_index)
        except Exception as exc:  # noqa: BLE001
            val, detail = None, {"原因": f"异常: {exc}"}
        if val is not None:
            cell_ok += 1
            return
        if args.semantic:
            # 语义可解析：column 能匹配名称对照位置，或按测点映射找到行
            pos = bridge._match_position(column, list(bridge.name_dict))
            if pos:
                cell_ok += 1
                return
            for kw, mkey in (("应变", "结构应变监测表"),
                             ("振动", "结构振动监测表"),
                             ("温度", "结构温度监测表")):
                plans = (bridge.point_map or {}).get(mkey)
                if plans and kw in str(title or ""):
                    found = bridge._point_plan_for_row(
                        plans, str(title or ""), column, row_index)
                    if found:
                        cell_ok += 1
                        return
        reason = str((detail or {}).get("原因") or "未找到")[:120]
        reason_counter[reason] += 1
        missing_cells.append({
            "表格标题": title, "行号": row_index + 1,
            "占位符": f"cell.{metric}.{column}.{stat}",
            "原因": reason,
        })

    for blk in iter_block_items(doc):
        if isinstance(blk, Paragraph):
            t = blk.text.strip()
            if not t:
                continue
            recent.append(t)
            if len(recent) > 8:
                recent = recent[-8:]
            m = re.fullmatch(r"\{\{chart\.([^}]+)\}\}", t)
            if m:
                chart_total += 1
                cid = m.group(1)
                info = bridge.resolve_chart_info(
                    cid, cid, context=list(recent[:-1]))
                ok = bool(info and info.get("path"))
                if not ok and args.semantic:
                    ok = bool(bridge._parse_position_chart_id(cid)) or \
                        bool(bridge._chart_sensor_id(cid, cid,
                                                    list(recent[:-1])))
                if ok:
                    chart_ok += 1
                else:
                    missing_charts.append({
                        "chart_id": cid,
                        "上下文": " | ".join(recent[-3:])[:160],
                    })
            elif len(t) <= 60 and re.search(r"监测统计|统计表|小结", t):
                cur_title = t
        elif isinstance(blk, Table):
            seen_tc = set()
            for ri, row in enumerate(blk.rows):
                for cell in row.cells:
                    k = id(getattr(cell, "_tc", None))
                    if k in seen_tc:
                        continue
                    seen_tc.add(k)
                    for p in cell.paragraphs:
                        txt = p.text
                        for mk in re.finditer(
                                r"\{\{cell\.([A-Za-z_]+)\.([^.#|}]+)"
                                r"\.([A-Za-z_]+)(?:#(\d+))?\}\}", txt):
                            row_idx = (int(mk.group(4)) - 1
                                       if mk.group(4) else ri)
                            _handle_cell(mk.group(1), mk.group(2),
                                         mk.group(3), row_idx, cur_title)

    def _rate(ok, total):
        return round(ok / total * 100, 1) if total else None

    out = {
        "生成时间": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "模板": os.path.abspath(args.template),
        "桥数据加载": bool(status.get("loaded")),
        "图表": {"总数": chart_total, "命中": chart_ok,
                 "命中率%": _rate(chart_ok, chart_total)},
        "单元格": {"总数": cell_total, "命中": cell_ok,
                   "命中率%": _rate(cell_ok, cell_total)},
        "未命中原因TOP": reason_counter.most_common(10),
        "未命中图表": missing_charts,
        "未命中单元格": missing_cells,
    }
    # 模板中遗留的 data.N 占位符数量（递归统计，含表格）
    try:
        _txt = "\n".join(p.text for p in doc.paragraphs)
        _txt += "\n".join(c.text for t in doc.tables
                          for r in t.rows for c in r.cells)
        out["data占位符数"] = _txt.count("{{data.")
    except Exception:  # noqa: BLE001
        out["data占位符数"] = None
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"图表 {chart_ok}/{chart_total}（{out['图表']['命中率%']}%） | "
          f"单元格 {cell_ok}/{cell_total}（{out['单元格']['命中率%']}%）")
    print("未命中原因TOP:", reason_counter.most_common(5))
    print("已写出:", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
