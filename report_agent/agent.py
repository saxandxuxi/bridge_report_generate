# -*- coding: utf-8 -*-
"""数据分析报告智能体：数据 -> 统计 -> 出图 -> 填充模板 -> 输出 Word 报告。"""

import calendar
import datetime as dt
import json
import logging
import os
import re
from typing import Dict, List, Optional

from . import chart_generator, data_loader, report_builder, stats
from .config import load_config
from .bridge_source import _norm

log = logging.getLogger("report-agent.agent")

from .period_utils import last_completed_quarter, quarter_range  # noqa: E402


_UNIT_HINT_HEADING_RE = re.compile(r"^\d+(?:\.\d+){1,3}\s*\S.*$")


def _chart_unit_hint(chart_texts: List[Dict], para, texts=None) -> str:
    """取图表段邻近表格的列头（单位/轴向）作为图片插入校验提示。

    规则：向下找最近的表格（同表标题连续、中间不能跨节标题）；向下
    没有时向上找。返回列头文本拼接，如
    “纵桥向(X方向) 横桥向(Y方向) 竖向(Z方向) 平均/（m/s²）”。
    """
    if not isinstance(para, int) or not chart_texts:
        return ""
    refs = [
        r for r in chart_texts
        if str(r.get("kind", "")).startswith("cell")
        and isinstance(r.get("paragraph"), int)
        and str(r.get("col_header", "")).strip()
    ]
    if not refs:
        return ""
    below = sorted((r for r in refs if r["paragraph"] >= para),
                   key=lambda r: r["paragraph"])
    above = sorted((r for r in refs if r["paragraph"] < para),
                   key=lambda r: -r["paragraph"])
    candidates = below or above
    if not candidates:
        return ""
    p_first = candidates[0]["paragraph"]
    # 图表与表格之间不能跨节标题（如“3.2.3作用监测小结”），否则表格属于下一节
    lo, hi = (para + 1, p_first) if below else (p_first + 1, para)
    if texts:
        for pno in range(lo, hi):
            t = str(texts[pno]).strip() if 0 <= pno < len(texts) else ""
            if (t and len(t) <= 40 and _UNIT_HINT_HEADING_RE.match(t)
                    and re.search(r"监测|分析|小结", t)):
                return ""
    title = candidates[0].get("table_title", "")
    headers = []
    for r in candidates:
        if r.get("table_title") != title:
            break
        h = str(r.get("col_header", "")).strip()
        if h and h not in headers:
            headers.append(h)
    return " ".join(headers)


def resolve_period(
    mode: str,
    report_date: Optional[dt.date] = None,
    period_cfg: Optional[Dict] = None,
) -> Dict:
    """根据模式确定报告数据区间。

    weekly    : 最近 7 天（含报告日）
    monthly   : 最近 30 天（含报告日）
    quarterly : 报告日所在自然季度（如 2026-01-01 ~ 2026-03-31）
    yearly    : 报告日所在自然年（如 2026-01-01 ~ 2026-12-31）
    manual    : 最近 7 天，或由 --date 指定结束日

    返回的 period 包含 label / label_cn 两个展示字段：
      label    — 用于文件名/标题，如 "2026.1~3"、"2026.03"、"2026.08.05"
      label_cn — 用于正文表述，如 "2026年第一季度"、"2026年3月"、"2026年8月"
    """
    period_cfg = period_cfg or {}
    end = report_date or dt.date.today()
    mode = mode or "weekly"

    if mode == "yearly":
        start = dt.date(end.year, 1, 1)
        end = dt.date(end.year, 12, 31)
        label = f"{end.year}年"
        label_cn = f"{end.year}年度"
    elif mode == "quarterly":
        # 自然季度：1-3 / 4-6 / 7-9 / 10-12 月
        q = (end.month - 1) // 3 + 1
        q_start_month = (q - 1) * 3 + 1
        q_end_month = q * 3
        start = dt.date(end.year, q_start_month, 1)
        end = dt.date(end.year, q_end_month, calendar.monthrange(end.year, q_end_month)[1])
        label = f"{end.year}.{q_start_month}~{q_end_month}"
        label_cn = f"{end.year}年第{q}季度"
    elif mode == "monthly":
        days = int(period_cfg.get("monthly_days", 30))
        start = end - dt.timedelta(days=days - 1)
        label = f"{end.year}.{end.month:02d}"
        label_cn = f"{end.year}年{end.month}月"
    else:
        days = int(period_cfg.get("weekly_days", 7))
        start = end - dt.timedelta(days=days - 1)
        label = f"{end.year}.{end.month:02d}.{end.day:02d}"
        week_of_month = (end.day - 1) // 7 + 1
        label_cn = f"{end.year}年{end.month}月第{week_of_month}周"

    return {
        "mode": mode,
        "start": start,
        "end": end,
        "generated_at": dt.datetime.now(),
        "label": label,
        "label_cn": label_cn,
    }


def build_daily_records(records: List[Dict], value_column: str) -> List[Dict]:
    """构造逐日明细行：date / 数值列 / 较均值偏差。"""
    values = [float(r[value_column]) for r in records if r.get(value_column) is not None]
    avg = sum(values) / len(values) if values else 0.0
    rows = []
    for r in records:
        if r.get(value_column) is None:
            continue
        rows.append(
            {
                "date": r["date"],
                value_column: r[value_column],
                "deviation": round(float(r[value_column]) - avg, 1),
            }
        )
    return rows


def _bridge_key(s) -> str:
    """桥名归一化：去空白、去“大桥/特大桥”后缀、转小写（与 bridges 一致）。"""
    s = str(s or "").strip().lower().replace(" ", "")
    for suffix in ("特大桥", "大桥"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s


def _is_other_registered_bridge_name(name: str, current: str) -> bool:
    """name_prefix 是否属于“其他已注册桥”的桥名（如配置残留 赤石大桥，
    但当前桥是 洣水河特大桥）。是则报告名必须跟随当前桥。"""
    if not name:
        return False
    try:
        from .bridges import list_bridges
        bridges = list_bridges()
    except Exception:  # noqa: BLE001
        return False
    nk = _bridge_key(name)
    ck = _bridge_key(current)
    if not nk or nk == ck:
        return False
    return any(_bridge_key(b.get("name")) == nk for b in bridges)


def _fix_period_dirs(cfg: dict, bridge_cfg: dict, period: Dict) -> None:
    """把 bridge_data 的 charts_dir/stats_dir 修正到当前报告期目录。

    季度报告期 -> 图库_<label>/统计值_<label>（如 图库_2026.4~6）；
    年度报告期 -> 图库_<年>.1~12/统计值_<年>.1~12（如 图库_2025.1~12）。
    仅在配置目录是标准“图库_/统计值_”布局时生效；自定义目录不覆盖。
    """
    cd = str(bridge_cfg.get("charts_dir") or "")
    sd = str(bridge_cfg.get("stats_dir") or "")
    if "图库" not in cd and "统计值" not in sd:
        return
    bridge = str(bridge_cfg.get("bridge_name") or "")
    base = ""
    for marker in ("图库_", "图库"):
        idx = cd.find(marker)
        if idx >= 0:
            base = cd[:idx].rstrip("/")
            break
    if not base:
        return
    label = str(period.get("label") or "")
    dir_label = re.sub(r"^(\d{4})年$", r"\1.1~12", label) or label
    if not dir_label:
        return
    from report_agent.config import resolve_bridge_subdir

    def _leaf(p):
        return os.path.join(p, bridge) if bridge else p

    charts = resolve_bridge_subdir(
        _leaf(os.path.join(base, f"图库_{dir_label}")), bridge)
    stats = resolve_bridge_subdir(
        _leaf(os.path.join(base, f"统计值_{dir_label}")), bridge)
    if "图库" in cd and os.path.normpath(cd) != os.path.normpath(charts):
        bridge_cfg["charts_dir"] = charts
        log.info("修正图库目录 -> %s（报告期 %s）", charts, label)
    if "统计值" in sd and os.path.normpath(sd) != os.path.normpath(stats):
        bridge_cfg["stats_dir"] = stats
        log.info("修正统计值目录 -> %s（报告期 %s）", stats, label)


def _quarterly_digest(agg: Dict) -> Dict:
    """把季度/年度统计压缩成“特征 -> 全桥统计”摘要（去掉逐位置明细）。

    供 LLM 审查总结/结论段落核对数值与位置（全桥统计含 疑似故障传感器位置、
    数据缺失严重的传感器位置、疑似故障时间段 等权威清单）。
    """
    digest = {}
    for data in agg.values():
        for bname, b in (data.get("桥") or {}).items():
            digest[str(bname)] = {
                feat: {"全桥统计": (fe.get("全桥统计") or {})}
                for feat, fe in (b or {}).items()
                if isinstance(fe, dict) and fe.get("全桥统计")
            }
    return digest


class ReportAgent:
    def __init__(self, config: Dict):
        self.cfg = config

    def run(
        self,
        mode: Optional[str] = None,
        report_date: Optional[dt.date] = None,
        engine: Optional[str] = None,
        inspect_only: bool = False,
    ) -> Dict:
        """执行一次完整报告生成，返回结果摘要。"""
        mode = mode or self.cfg.get("schedule", {}).get("mode", "weekly")
        if mode not in ("weekly", "monthly", "quarterly", "yearly", "manual"):
            raise ValueError(f"未知模式: {mode}（支持 weekly / monthly / quarterly / yearly / manual）")

        period = resolve_period(mode, report_date, self.cfg.get("period"))
        data_cfg = self.cfg.get("data", {})

        # 0. 真实监测数据适配器（桥数据预处理产物）
        bridge = None
        bridge_status = {}
        pending_charts: List[Dict] = []
        missing_sinks: List[str] = []
        bridge_cfg = self.cfg.get("bridge_data", {}) or {}
        if bridge_cfg.get("enabled", False):
            # 修正 图库/统计值 目录指向当前报告期（季度/年度）目录：
            # 配置可能残留上一期（如 2026.1~3），直接命令行跑年度/季度时会
            # 用到旧期图库（如年度报告插进第一季度图片）
            if mode in ("quarterly", "yearly"):
                _fix_period_dirs(self.cfg, bridge_cfg, period)
            from .bridge_source import BridgeData
            base_dir = os.path.dirname(os.path.abspath(self.cfg.get("_config_path", "config.json")))
            bridge = BridgeData(bridge_cfg, base_dir=base_dir)
            bridge_status = bridge.load()
            if not bridge_status.get("loaded"):
                log.error("桥数据加载失败: %s", bridge_status.get("error"))

        # 1. 读取并过滤数据（兼容单数据源；桥模式下 CSV 可缺失）
        records: List[Dict] = []
        load_stats: Dict = {"total_rows": 0, "loaded_rows": 0, "skipped_rows": 0, "none_value_counts": {}}
        csv_available = False
        if data_cfg.get("file"):
            try:
                data_strict = data_cfg.get("strict_mode", False)
                all_records, load_stats = data_loader.load_csv(
                    data_cfg.get("file", ""),
                    date_column=data_cfg.get("date_column", "date"),
                    value_columns=data_cfg.get("value_columns"),
                    strict=data_strict,
                    return_stats=True,
                )
                records = data_loader.filter_period(all_records, period["start"], period["end"])
                csv_available = len(records) > 0
            except Exception as exc:  # noqa: BLE001
                if bridge is None:
                    raise
                log.warning("CSV 数据不可用，桥模式下继续（%s）: %s",
                            data_cfg.get("file"), exc)
        if not records and bridge is None:
            raise ValueError(
                f"数据区间 {period['start']} 至 {period['end']} 内没有数据，"
                f"请检查数据文件 {data_cfg.get('file')}"
            )

        # 1b. 加载多数据源注册表（按指标路由）
        base_dir = os.path.dirname(os.path.abspath(self.cfg.get("_config_path", "config.json")))
        data_registry = data_loader.DataSourceRegistry(
            self.cfg.get("data_sources", {}),
            base_dir=base_dir,
        )
        available = data_registry.available_metrics()
        if available:
            log.info("已加载多数据源: %s", ", ".join(available))
        else:
            log.info("未配置 data_sources，使用单数据源模式（data.file）")

        # 2. 统计
        if records:
            computed = stats.compute_stats(
                records,
                value_columns=data_cfg.get("value_columns"),
                thresholds=data_cfg.get("thresholds", []),
            )
        else:
            computed = {"days": bridge.estimate_days(period) if bridge is not None else 0}
        log.info(
            "数据区间: %s ~ %s  (%s 天)",
            period["start"], period["end"], computed.get("days", 0),
        )
        for col in data_cfg.get("value_columns", []):
            if col in computed:
                s = computed[col]
                log.info(
                    f"  {col}: 最大 {s['max']:.1f}（{s.get('max_date')}）  "
                    f"最小 {s['min']:.1f}（{s.get('min_date')}）  "
                    f"平均 {s['avg']:.1f}  标准差 {s['std']:.2f}"
                )

        # 3. 图表（MATLAB 优先，Python 兜底）
        # 3a. 用户在 config.charts.definitions 里写死的图表
        # 3b. 从 chart_texts 自动生成的图表（如有）
        charts_cfg = self.cfg.get("charts", {})
        user_chart_defs = list(charts_cfg.get("definitions", []) or [])

        chart_texts_for_runtime = self.cfg.get("_chart_texts", [])
        chart_images: Dict[str, str] = {}
        chart_captions: Dict[str, str] = {}
        chart_sensors: Dict[str, str] = {}
        chart_kinds: Dict[str, str] = {}
        chart_para: Dict[str, int] = {}
        extra_charts: Dict[str, List[Dict]] = {}
        chart_gaps: List[Dict] = []
        if bridge is not None:
            # 桥模式：图表优先从图库解析；解析不到生成占位图并记入待补清单
            out_dir = charts_cfg.get("output_dir", "outputs/charts")
            os.makedirs(out_dir, exist_ok=True)
            resolved_bridge = 0
            # 只保留真正的图表（跳过表格单元格引用 + bare_caption 图题段）
            chart_items = [
                ct for ct in chart_texts_for_runtime
                if ct.get("source") != "bare_caption"
                and not (str(ct.get("_unique_chart_id") or ct.get("chart_id") or "").startswith("cell_")
                        or str(ct.get("kind", "")).startswith("cell"))
            ]
            for i, ct in enumerate(chart_items):
                cid = ct.get("_unique_chart_id") or ct.get("chart_id")
                if not cid:
                    continue
                # 上下文 = 前 3 个正文段落（如“第6、7跨…如下图所示：”这句）
                #          + 按距离排序的邻近图注（用于“倾角1_time_series”裸图注继承位置）
                n = len(chart_items)
                texts = self.cfg.get("_texts", []) or []
                ctx_texts = []
                para = ct.get("paragraph")
                if isinstance(para, int):
                    # 前 4 段正文，从最近往前（“…如下图所示：”这句优先）
                    for pno in range(para - 1, max(para - 5, -1), -1):
                        if 0 <= pno < len(texts) and texts[pno]:
                            t = str(texts[pno]).strip()
                            if t and t not in ctx_texts:
                                ctx_texts.append(t)
                order = [0, 1, -1, 2, -2]
                for off in order:
                    j = i + off
                    if 0 <= j < n and j != i:
                        t = chart_items[j].get("text", "")
                        if t and t not in ctx_texts:
                            ctx_texts.append(t)
                # 指标提示：只用图注 + 节前正文判断（避免相邻节图注串扰）
                metric_hint = ""
                if isinstance(para, int):
                    body_texts = []
                    for pno in range(para - 1, max(para - 5, -1), -1):
                        if 0 <= pno < len(texts) and texts[pno]:
                            body_texts.append(str(texts[pno]))
                    # 指标判断：节上下文(正文)优先于图注原文（图注原文可能有笔误，如结构温度节写成“环境温度”）
                metric_hint = (
                    bridge._metric_alias_hit(" ".join(body_texts))
                    or bridge._metric_alias_hit(ct.get("text", ""))
                    or ""
                )
                unit_hint = _chart_unit_hint(
                    chart_texts_for_runtime, ct.get("paragraph"),
                    self.cfg.get("_texts", []) or [],
                )
                info = bridge.resolve_chart_info(cid, ct.get("text", ""), context=ctx_texts,
                                                 metric_hint=metric_hint,
                                                 sensor_hint=str(ct.get("sensor_id") or ""),
                                                 feature_hint=str(ct.get("feature") or ""),
                                                 unit_hint=unit_hint)
                if info:
                    chart_images[cid] = info["path"]
                    chart_captions[cid] = info["display"]
                    chart_sensors[cid] = info["sensor_id"]
                    chart_kinds[cid] = info["kind"]
                    chart_para[cid] = ct.get("paragraph") or 0
                    resolved_bridge += 1
                else:
                    reason = f"未匹配到图库图片（图注: {ct.get('text', '') or '无'}）"
                    placeholder = bridge.make_placeholder_chart(cid, reason, out_dir)
                    if placeholder:
                        chart_images[cid] = placeholder
                    chart_captions[cid] = ct.get("text", "") or cid
                    pending_charts.append({"chart_id": cid, "caption": ct.get("text", ""), "reason": reason})
            for d in user_chart_defs:
                cid = d.get("id")
                if cid and cid not in chart_images:
                    info = bridge.resolve_chart_info(cid, d.get("title", ""))
                    if info:
                        chart_images[cid] = info["path"]
                        chart_captions[cid] = info["display"]
                        resolved_bridge += 1
            log.info("桥模式图表解析完成：命中图库 %d 张，待补 %d 张",
                     resolved_bridge, len(pending_charts))

            # ---- 缺图推断：按本节约应有的监测部位数 vs 实际图表数 ----
            # 防御：服务器 agent.py 版本不一致时（缺少该方法）不崩溃，
            # 只跳过缺图补齐并告警（完整同步后自动恢复）
            if hasattr(self, "_detect_chart_gaps"):
                chart_gaps = self._detect_chart_gaps(
                    bridge, chart_items, chart_sensors, chart_kinds, chart_para
                )
            else:
                log.warning("当前 agent.py 缺少 _detect_chart_gaps，"
                            "跳过缺图补齐（请用最新版本完整覆盖 report_agent/）")
                chart_gaps = []
            extra_charts: Dict[str, List[Dict]] = {}
            if chart_gaps and (bridge_cfg.get("auto_fill_missing_charts", True)):
                for gap in chart_gaps:
                    anchor = gap.get("anchor_cid")
                    if not anchor:
                        continue
                    for item in gap.get("missing", []):
                        sid = item["sensor_id"]
                        kind = item["kind"]
                        png = bridge.chart_png_for(sid, kind, metric=item.get("metric", ""))
                        if not png:
                            continue
                        metric = item.get("metric", "")
                        caption = bridge.display_name_for(
                            sid, f"{metric}_{kind}_1" if metric else f"{kind}_1",
                            kind, metric_for_label=metric or "")
                        extra_charts.setdefault(anchor, []).append({
                            "path": png,
                            "caption": caption,
                            "sensor_id": sid,
                            "kind": kind,
                        })
                        # 补齐图本身也可能是多面板拆分图（_2/_3.png），一并插入
                        for _p in bridge.chart_siblings(png):
                            extra_charts.setdefault(anchor, []).append({
                                "path": _p,
                                "caption": caption,
                                "sensor_id": sid,
                                "kind": kind,
                            })
            log.info("缺图推断：%d 节存在缺图（自动补齐 %d 张）",
                     len(chart_gaps), sum(len(v) for v in extra_charts.values()))
            # 模板中额外的位置化图表占位符（如特殊应变 4#/5#墩底部），
            # 未出现在 analysis chart_texts 时按位置直接解析，避免“有占位无图”
            try:
                from docx import Document as _Doc
                from .report_builder import _paragraph_text, _walk_paragraphs
                tpl_doc = _Doc(self.cfg.get("template", ""))
                extra_ids = []
                recent = []  # 图表占位符前面的最近正文（用于左右幅等方位定向）
                for para in _walk_paragraphs(tpl_doc):
                    t = _paragraph_text(para).strip()
                    m = re.fullmatch(r"\{\{chart\.([^}]+)\}\}", t)
                    if not m:
                        if t:
                            recent.append(t)
                            if len(recent) > 8:
                                recent = recent[-8:]
                        continue
                    cid = m.group(1)
                    if cid not in chart_images:
                        extra_ids.append((cid, list(recent)))
                for cid, ctx in extra_ids:
                    # 传最近正文作上下文，让“右幅”节能定向到右幅传感器
                    info = bridge.resolve_chart_info(cid, cid, context=ctx)
                    if not info:
                        # 匹配不到也生成占位图并记入待补清单，避免整个报告
                        # 因个别图表占位符匹配失败而中断（报告仍可正常生成）
                        reason = f"未匹配到图库图片（模板占位符: {cid}）"
                        placeholder = bridge.make_placeholder_chart(
                            cid, reason, out_dir)
                        if placeholder:
                            chart_images[cid] = placeholder
                        chart_captions[cid] = cid
                        pending_charts.append({
                            "chart_id": cid, "caption": cid, "reason": reason,
                        })
                        continue
                    chart_images[cid] = info["path"]
                    chart_captions[cid] = info["display"]
                    chart_sensors[cid] = info["sensor_id"]
                    chart_kinds[cid] = info["kind"]
                    log.info("模板位置化图表 %s -> %s", cid, info["path"])
            except Exception as exc:  # noqa: BLE001
                log.warning("扫描模板额外图表占位符失败: %s", exc)
            # CSV 可用时，剩余的 user_chart_defs 仍可走 matplotlib 兜底
            leftover_defs = [d for d in user_chart_defs if d.get("id") not in chart_images]
            if records and leftover_defs:
                chart_images.update(chart_generator.generate_charts(
                    leftover_defs, records,
                    charts_cfg.get("output_dir", "outputs/charts"),
                    engine=engine or charts_cfg.get("engine", "auto"),
                    matlab_cfg=charts_cfg.get("matlab", {}),
                    data_registry=data_registry,
                    period=period,
                ))
        else:
            auto_defs = []
            if chart_texts_for_runtime and data_registry is not None:
                from .chart_generator import auto_chart_defs_from_texts
                auto_defs = auto_chart_defs_from_texts(chart_texts_for_runtime)
                log.info("从 %d 个 chart_text 自动生成 %d 个图表定义",
                         len(chart_texts_for_runtime), len(auto_defs))
            all_chart_defs = user_chart_defs + auto_defs
            chart_images = chart_generator.generate_charts(
                all_chart_defs,
                records,
                charts_cfg.get("output_dir", "outputs/charts"),
                engine=engine or charts_cfg.get("engine", "auto"),
                matlab_cfg=charts_cfg.get("matlab", {}),
                data_registry=data_registry,
                period=period,
            )
        # 拆分的多面板合并图/相关性图（时间序列图_2.png、频率分布图_2.png、
        # 相关性_..._2.png …）：等“模板额外占位符”也解析完后统一补，
        # 图库里有几张就插几张；caption 与首图一致，保证每张都有独立图号。
        if bridge is not None:
            for cid, png in list(chart_images.items()):
                if not png:
                    continue
                base_caption = chart_captions.get(cid, "")
                exist = {ex.get("path")
                         for ex in extra_charts.get(cid, [])}
                for _p in bridge.chart_siblings(png):
                    if _p in exist:
                        continue
                    exist.add(_p)
                    extra_charts.setdefault(cid, []).append({
                        "path": _p,
                        "caption": base_caption,
                        "sensor_id": chart_sensors.get(cid, ""),
                        "kind": chart_kinds.get(cid, ""),
                    })
        for cid, png in chart_images.items():
            log.info("  图表 %s: %s", cid, png)

        # 4. 逐日明细
        row_datasets: Dict[str, List[Dict]] = {}
        if records:
            value_col = (data_cfg.get("value_columns") or ["temperature"])[0]
            daily_rows = build_daily_records(records, value_col)
            row_datasets = {"daily_records": daily_rows}

        # 5. 填充模板
        lineage: List[Dict] = []
        resolver = report_builder.build_value_resolver(
            computed, period,
            data_registry=data_registry,
            data_values=self.cfg.get("_data_values", {}),
            bridge=bridge,
            missing_sink=missing_sinks,
            lineage=lineage,
            data_meta=self.cfg.get("_data_number_meta", {}),
            llm_cfg=self.cfg.get("llm"),
        )

        # 输出文件名：优先使用 config.report_name_prefix；
        # 若未配置则尝试从 source_report（成品报告）文件名推导；
        # 最终兜底用模板文件名。始终附加日期+时间后缀。
        name_cfg = self.cfg.get("report", {})
        name_prefix = name_cfg.get("name_prefix", "")
        # 桥模式下报告名必须跟随当前桥：若配置里的 name_prefix 是其他已注册
        # 桥的桥名（历史模板/源报告上传残留，如 赤石大桥），自动纠正为当前桥，
        # 避免“生成洣水河却输出 赤石大桥.docx”。
        if bridge is not None and bridge.bridge_name:
            bn = bridge.bridge_name
            if (not name_prefix
                    or _is_other_registered_bridge_name(name_prefix, bn)):
                name_prefix = bn
        if not name_prefix:
            source_report = self.cfg.get("source_report", "")
            if source_report:
                name_prefix = os.path.splitext(os.path.basename(source_report))[0]
            else:
                name_prefix = os.path.splitext(os.path.basename(self.cfg.get("template", "")))[0]
            # 去掉 _template / _模板 等后缀
            name_prefix = re.sub(r"[_-]template$|[_-]模板$", "", name_prefix, flags=re.IGNORECASE)

        # 报告名带上模板版本（如 _template_v17），便于区分不同模板生成的报告
        if "_template_v" not in name_prefix:
            m = re.search(r"_v(\d+)(?:\.docx)?$",
                          os.path.basename(self.cfg.get("template", "")))
            if m:
                name_prefix = f"{name_prefix}_template_v{m.group(1)}"

        with_ts = name_cfg.get("with_timestamp", True)
        # 模板版本前缀（如 _template_v1）与报告期之间补下划线，
        # 避免拼成 “洣水河特大桥_template_v12026.4~6.docx”。
        _period_sep = "_" if "_template_v" in name_prefix else ""
        if mode == "quarterly":
            # 季度命名：洞庭湖大桥2026.1~3.docx（用户指定格式）
            out_name = f"{name_prefix}{_period_sep}{period['label']}.docx"
        elif mode == "yearly":
            out_name = f"{name_prefix}{_period_sep}{period['label']}.docx"
        elif with_ts:
            out_name = (
                f"{name_prefix}_{period['start'].strftime('%Y%m%d')}_"
                f"{period['end'].strftime('%Y%m%d')}_"
                f"{dt.datetime.now().strftime('%H%M%S')}.docx"
            )
        else:
            out_name = (
                f"{name_prefix}_{period['start'].strftime('%Y%m%d')}_"
                f"{period['end'].strftime('%Y%m%d')}.docx"
            )
        out_path = os.path.join(self.cfg.get("output_dir", "outputs"), out_name)
        # 同周期重复生成不覆盖旧文件：已存在时追加毫秒级时间戳
        if os.path.exists(out_path):
            _base, _ext = os.path.splitext(out_path)
            _stamp = dt.datetime.now().strftime("%H%M%S%f")[:10]
            out_path = f"{_base}_{_stamp}{_ext}"
        repair_stats = {}
        from .reviewer import ReportReviewer, _read_docx_text, self_check_report
        from .repairer import ReportRepairer

        # 成品报告原文：既给审查做对照，也给总结润色做“原文 vs 真实数据”对照
        source_text = ""
        try:
            source_text = _read_docx_text(self.cfg.get("source_report", ""))
        except Exception:  # noqa: BLE001
            source_text = ""
        if bridge is not None:
            bridge._source_text = source_text

        # 图表索引表：chart_id -> 图注 -> 位置，供 LLM 命名 chart/caption 修复目标
        chart_index_str = ""
        if bridge is not None:
            lines = []
            for cid in chart_images:
                pos = (bridge._position_for_sensor(
                    chart_sensors.get(cid, "")) if hasattr(
                        bridge, "_position_for_sensor") else "") or ""
                lines.append(f"{cid} | {chart_captions.get(cid, '')} | {pos}")
            chart_index_str = "\n".join(lines)

        max_rounds = int((self.cfg.get("review", {}) or {}).get("max_rounds", 3) or 3)
        caption_removals: List[str] = []
        caption_replacements: Dict[str, str] = {}
        report_review = None
        self_check: List[Dict] = []
        rounds_log: List[Dict] = []
        repair_log: List[Dict] = []
        prior_issues_json = ""
        reviewer = ReportReviewer(self.cfg.get("llm"))
        out_dir_abs = os.path.abspath(self.cfg.get("output_dir", "outputs"))
        logs_dir = os.path.join(os.path.dirname(out_dir_abs), "logs")
        os.makedirs(logs_dir, exist_ok=True)

        # 模板占位符级补全（只做一次，所有审查轮次共用同一份临时模板）：
        # 小结段落按“小节标题 + chart/cell 占位符前缀 + 季度统计实际特征”
        # 联合识别该段应覆盖的指标，缺哪个 {{summary.<metric>}} 就补哪个，
        # 保证总结段落囊括全部指标的季度总结内容（极值 + 故障/缺失位置）。
        tpl_path = self.cfg.get("template", "")
        _orig_tpl = tpl_path
        if bridge is not None:
            # 清理上次运行残留的临时模板（tpl_summary_*.docx）：
            # 报告中途异常退出时循环后的清理不会执行，这里先清掉避免累积
            try:
                _tpl_dir = os.path.dirname(os.path.abspath(tpl_path))
                if os.path.isdir(_tpl_dir):
                    for _fn in os.listdir(_tpl_dir):
                        if _fn.startswith("tpl_summary_") \
                                and _fn.endswith(".docx"):
                            _stale = os.path.join(_tpl_dir, _fn)
                            try:
                                os.remove(_stale)
                                log.info("清理上次残留临时模板: %s", _stale)
                            except OSError:
                                pass
            except OSError:
                pass
            enriched = self._enrich_template_summaries(
                tpl_path, bridge, period)
            if enriched and enriched != tpl_path:
                tpl_path = enriched
                log.info("模板小结段补全完成，使用临时模板: %s", tpl_path)

        for rnd in range(1, max_rounds + 1):
            lineage.clear()
            unfilled = report_builder.build_report(
                template_path=tpl_path,
                output_path=out_path,
                resolver=resolver,
                chart_images=chart_images,
                row_datasets=row_datasets,
                chart_width_inches=float(charts_cfg.get("width_inches", 5.8)),
                strict=True,
                period=period,
                chart_captions=chart_captions,
                extra_charts=extra_charts,
                text_replace=bridge_cfg.get("text_replace") or None,
                caption_removals=caption_removals,
                caption_replacements=caption_replacements,
                repair_stats=repair_stats,
            )

            # 数据链路 + 填表校验（供审查对照）
            verify_warns = []
            lineage_digest = ""
            table_warnings = ""
            if lineage:
                report_builder._write_data_lineage(lineage, logs_dir, period)
                try:
                    verify_warns = report_builder.verify_table_columns(
                        out_path, lineage=lineage, logs_dir=logs_dir,
                        label=period.get("label") or "report")
                except Exception as exc:  # noqa: BLE001
                    log.warning("填表校验失败: %s", exc)
                missed = [e for e in lineage
                          if e.get("结果") in ("未找到", "回退")]
                lineage_digest = json.dumps(
                    missed[:200], ensure_ascii=False, default=str)
                table_warnings = json.dumps(
                    verify_warns[:100], ensure_ascii=False, default=str)

            # LLM 审查
            review = None
            if reviewer.available():
                name_dict_json = ""
                quarterly_stats_json = ""
                if bridge is not None:
                    try:
                        _nd = {}
                        if bridge.name_dict_path and os.path.isfile(
                                bridge.name_dict_path):
                            with open(bridge.name_dict_path, "r",
                                      encoding="utf-8") as _f:
                                _nd = json.load(_f)
                        name_dict_json = json.dumps(
                            _nd, ensure_ascii=False, default=str)
                    except Exception:  # noqa: BLE001
                        name_dict_json = json.dumps(
                            bridge.name_dict, ensure_ascii=False, default=str)
                    quarterly_stats_json = json.dumps(
                        _quarterly_digest(bridge._load_aggregate_stats()),
                        ensure_ascii=False, default=str)
                review = reviewer.review_report(
                    source_text, _read_docx_text(out_path),
                    lineage_digest=lineage_digest,
                    table_warnings=table_warnings,
                    chart_index=chart_index_str,
                    prior_issues=prior_issues_json,
                    name_dict_json=name_dict_json,
                    quarterly_stats_json=quarterly_stats_json,
                )
                n_issues = len(review.get("issues", []))
                if n_issues:
                    log.warning("第 %d 轮审查发现 %d 处问题", rnd, n_issues)
                    for iss in review.get("issues", []):
                        log.warning("  - [%s] %s", iss.get("type", "other"),
                                    iss.get("detail", ""))
                else:
                    log.info("第 %d 轮审查完成：未发现问题", rnd)

            # 确定性体检
            sc = []
            try:
                sc = self_check_report(out_path)
                if sc:
                    log.warning("第 %d 轮确定性体检发现 %d 处问题",
                                rnd, len(sc))
            except Exception as exc:  # noqa: BLE001
                log.warning("确定性体检失败: %s", exc)

            rounds_log.append({"round": rnd, "review": review,
                               "self_check": sc})
            report_review = review
            self_check = sc

            # 应用结构化修复（验证后落地），有实际落地才继续下一轮
            repairs = (review or {}).get("repairs", []) if review else []
            if not repairs or bridge is None:
                break
            rp = ReportRepairer(
                bridge, chart_images, chart_captions,
                chart_sensors, chart_kinds, period).apply(repairs)
            repair_log.append(rp)
            caption_removals.extend(rp.get("caption_removals", []))
            caption_replacements.update(rp.get("caption_replacements", {}))
            caption_replacements.update(rp.get("text_replacements", {}))
            prior_issues_json = json.dumps(
                (review or {}).get("issues", []), ensure_ascii=False,
                default=str)
            if rp.get("applied"):
                log.info("第 %d 轮自动修复 %d 处，进入下一轮复审",
                         rnd, len(rp.get("applied", [])))
                continue
            break

        # 临时模板（templates/tpl_summary_*.docx）用完即删，
        # 避免每次生成报告都残留垃圾文件
        if tpl_path and tpl_path != _orig_tpl \
                and os.path.isfile(tpl_path):
            try:
                os.remove(tpl_path)
                log.info("临时模板已清理: %s", tpl_path)
            except OSError as exc:
                log.warning("临时模板清理失败: %s", exc)

        # 审查/修复完整记录落盘，web 面板可看每轮与最终待人工项
        review_path = os.path.join(
            logs_dir, f"review_report_{period.get('label') or 'report'}.json")
        try:
            with open(review_path, "w", encoding="utf-8") as f:
                json.dump({
                    "period": {k: (str(v) if hasattr(v, "isoformat")
                                   else v) for k, v in period.items()},
                    "rounds": rounds_log,
                    "repairs": repair_log,
                    "max_rounds": max_rounds,
                    "final": report_review,
                    "self_check": self_check,
                }, f, ensure_ascii=False, indent=2, default=str)
            log.info("报告审查/修复记录已保存: %s", review_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("报告审查记录保存失败: %s", exc)

        # 审查修复过的报告另存一份“修正标红审查版”到 outputs/marked_report/，
        # reports/ 目录保留纯净版
        marked_path = ""
        if caption_replacements or caption_removals:
            try:
                import shutil as _shutil
                marked_dir = os.path.join(
                    os.path.dirname(os.path.dirname(out_path)), "marked_report")
                os.makedirs(marked_dir, exist_ok=True)
                marked_name = (
                    os.path.splitext(out_name)[0] + "_marked.docx")
                marked_path = os.path.join(marked_dir, marked_name)
                _shutil.copyfile(out_path, marked_path)
                from docx import Document as _MarkDoc
                md = _MarkDoc(marked_path)
                report_builder._apply_text_replacements(
                    md, caption_replacements, mark=True)
                report_builder._remove_paragraphs_by_fragment(
                    md, caption_removals, mark=True)
                md.save(marked_path)
                log.info("审查版报告（修正标红）已写出: %s", marked_path)
            except Exception as exc:  # noqa: BLE001
                log.warning("生成审查版报告失败: %s", exc)

        final_issues = (report_review or {}).get("issues", []) if report_review else []
        # 只有 LLM 明确标注 needs_human 的问题才进“需人工处理”；
        # 可自动修复的（caption 删除/数据替换/总结润色）不占人工名额
        needs_human_issues = [
            iss for iss in final_issues
            if iss.get("needs_human") in (True, "true", "True")
        ]
        repairer_human = sum(len(r.get("needs_human", [])) for r in repair_log)
        repair = {
            "auto_fixed": repair_stats.get("spaces_collapsed", 0),
            "repairs_applied": sum(len(r.get("applied", []))
                                   for r in repair_log),
            "manual_needed": len(needs_human_issues) + len(self_check)
                             + repairer_human,
            "final_issue_count": len(final_issues),
            "llm_called": report_review is not None,
            "llm_ok": bool(report_review and report_review.get("raw")),
            "rounds": len(rounds_log),
            "needs_human": needs_human_issues,
        }

        summary = {
            "output": out_path,
            "marked_output": marked_path,
            "period": {
                "mode": mode,
                "start": period["start"].isoformat(),
                "end": period["end"].isoformat(),
                "label": period.get("label", ""),
                "label_cn": period.get("label_cn", ""),
            },
            "days": computed.get("days", 0),
            "charts": {cid: png for cid, png in chart_images.items()},
            "unfilled": unfilled,
            "data_load_stats": load_stats,
            "csv_available": csv_available,
            "bridge": bridge_status,
            "pending_charts": pending_charts,
            "missing_cells": missing_sinks[:500],
            "chart_gaps": chart_gaps if bridge is not None else [],
            "review": report_review,
            "self_check": self_check,
            "repair": repair,
        }
        # 生成结束后刷新桥数据状态，让 match_stats 反映本次运行的实际命中情况
        if bridge is not None:
            summary["bridge"] = bridge.status()
        if inspect_only:
            summary["stats"] = {k: v for k, v in computed.items() if k != "days"}
        log.info("报告已生成: %s", out_path)
        return summary

    # ------------------------------------------------------------------
    # 模板小结段补全：按“小节标题 + 占位符前缀 + 季度统计特征”识别指标
    # ------------------------------------------------------------------

    def _enrich_template_summaries(self, template_path: str, bridge,
                                   period: Dict) -> str:
        """扫描模板，为“小结/总结”段落补全缺失的 {{summary.<metric>}}。

        识别逻辑（三条件联合，接近人工语义）：
          1) 小节标题：如 “3.3.1应变监测数据分析” -> strain；
          2) 该节内 chart/cell 占位符的指标前缀（如
             {{chart.vibration_3#柱墩墩底左幅_trend_1}} -> vibration），
             仅在标题识别不出指标时补充；
          3) 季度统计实际特征键（bridge._feature_for_metric 非空才补）。
        返回临时模板路径（无改动时返回原路径），不改动原始模板文件。
        """
        if not template_path or not os.path.isfile(template_path):
            return template_path
        try:
            from docx import Document as _TplDoc
            from .recognizer import _metrics_in_text, SUMMARY_CLAIM_RE
            doc = _TplDoc(template_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("模板小结段补全读取失败: %s", exc)
            return template_path
        repaired_cols = self._repair_pseudo_cell_columns(doc, bridge)
        if repaired_cols:
            log.info("模板伪位置表格列修正 %d 处（新增测点N -> 真实监测部位）",
                     repaired_cols)

        summary_metrics = {}   # 小结标题段索引 -> [metric]
        sections = []          # [(节标题指标, 节内占位符指标)]
        cur_head, cur_chart = set(), set()
        for idx, p in enumerate(doc.paragraphs):
            t = p.text.strip()
            if not t:
                continue
            hm = re.match(r"^(\d+(?:\.\d+){1,3})\s*(\S.*)$", t)
            if hm and len(t) <= 40 and re.search(r"监测|分析|小结", t):
                title = hm.group(2)
                if "小结" in title:
                    merged = set()
                    for h, c in sections:
                        merged |= (h or c)
                    merged |= (cur_head or cur_chart)
                    if merged:
                        summary_metrics[idx] = sorted(merged)
                    sections = []
                    cur_head, cur_chart = set(), set()
                else:
                    if cur_head or cur_chart:
                        sections.append((cur_head, cur_chart))
                    cur_head = set(_metrics_in_text(title))
                    cur_chart = set()
                continue
            pref = self._template_para_metric_prefixes([t], bridge, period)
            if pref:
                cur_chart |= pref

        # 找每个小结标题后的“总结正文段”（含 监测数据结果表明/稳定/正常 等
        # 特征句，跳过“本季度，我们对…进行了持续监测”这类引子段），把缺的
        # {{summary.<metric>}} 补到段首
        if not summary_metrics:
            return template_path
        added = 0
        for hidx, metrics in summary_metrics.items():
            target = None
            for j in range(hidx + 1, min(hidx + 5, len(doc.paragraphs))):
                pt = doc.paragraphs[j].text.strip()
                if pt and (SUMMARY_CLAIM_RE.search(pt)
                           or "{{summary." in pt):
                    target = doc.paragraphs[j]
                    break
            if target is None:
                continue
            existing = {m for m in re.findall(
                r"\{\{summary\.([a-zA-Z_]+)\}\}", target.text)}
            missing = [m for m in metrics if m not in existing
                       and self._metric_summary_ok(bridge, m, period)]
            if not missing:
                continue
            # 多特征小结之间用换行分隔，避免 地震+结构温度 等挤成一大段；
            # 若后面紧跟已有 {{summary.*}}，也补一个换行分隔
            ph = "\n".join(f"{{{{summary.{m}}}}}" for m in missing)
            after = target.text[target.text.find("。") + 1:] \
                if "。" in target.text else ""
            if after.lstrip().startswith("{{summary."):
                ph += "\n"
            # 插到段落第一句（如“依据《…》…未出现异常。”）之后，阅读更顺
            inserted = False
            for r in target.runs:
                if r.text and "。" in r.text:
                    i = r.text.find("。")
                    r.text = r.text[:i + 1] + ph + r.text[i + 1:]
                    inserted = True
                    break
            if not inserted:
                if target.runs:
                    target.runs[0].text = ph + (target.runs[0].text or "")
                else:
                    target.add_run(ph)
            added += len(missing)
            log.info("模板小结段补全 %s -> %s", target.text[:40], missing)
        # 小结段里已被 {{summary.*}} 覆盖的“最高/最低/绝对最大/差值 + 对应测点
        # 位置”固定句会与总结重复（如 3.2.3 的地震、结构温度），删除这些含
        # {{stats.<指标>.<统计>.loc}} 的分句，避免同一数据出现两遍。
        _strip_redundant_loc_clauses(doc)
        if not added and not repaired_cols:
            return template_path
        import tempfile as _tf
        fd, tmp_path = _tf.mkstemp(suffix=".docx",
                                   prefix="tpl_summary_", dir=os.path.dirname(
                                       os.path.abspath(template_path)))
        os.close(fd)
        try:
            doc.save(tmp_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("模板小结段补全保存失败: %s", exc)
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return template_path
        return tmp_path

    def _repair_pseudo_cell_columns(self, doc, bridge) -> int:
        """把表格里“新增测点N/新增位置N”这类伪占位列修正为真实监测部位。

        伪列不像正经位置名词（如 “新增结构温度监测统计” 表的
        cell.structure_temperature.新增测点1.avg），resolver 无法匹配统计库；
        而该表所属小节上方通常有 {{chart.<metric>_<真实位置>_...}} 占位符，
        用最近的同指标图表位置回填列名：
          cell.structure_temperature.新增测点1.avg  ->
          cell.structure_temperature.1/2主跨钢桁架桥面板.avg
        找不到/有歧义时保留原占位符并告警，供 LLM/人工检查修复。
        返回修正的占位符数量；不改动原始模板文件。
        """
        if doc is None or bridge is None:
            return 0
        try:
            from .report_builder import iter_block_items
        except Exception:  # noqa: BLE001
            return 0
        name_keys = list((bridge.name_dict or {}).keys())
        blocks = list(iter_block_items(doc))
        chart_re = re.compile(
            r"\{\{chart\.([A-Za-z_]+)_(.*?)_(?:trend|histogram|scatter)"
            r"(?:_\d+)?\}\}")
        pseudo_re = re.compile(
            r"\{\{cell\.([A-Za-z_]+)\.(新增|原有|原)"
            r"(?:测点|位置|传感器)\s*(\d+)\.([a-zA-Z_]+)(?:#(\d+))?\}\}")
        repaired = 0
        unresolved = []
        for bi, blk in enumerate(blocks):
            if not hasattr(blk, "rows"):
                continue
            # 最近图表/上下文（同一节通常图在表前 10 段内）
            ctx = []
            for j in range(bi - 1, max(bi - 25, -1), -1):
                bj = blocks[j]
                if hasattr(bj, "rows"):
                    continue
                t = bj.text.strip() if hasattr(bj, "text") else ""
                if t:
                    ctx.append(t)
                if len(ctx) >= 12:
                    break
            # 逐 cell 找伪列并回填
            for row in blk.rows:
                for cell in row.cells:
                    for para in cell.paragraphs:
                        full = "".join(r.text for r in para.runs)
                        if not full or ("新增" not in full
                                        and "原有" not in full):
                            continue
                        fixed = list(full)
                        changed = False
                        for m in reversed(list(pseudo_re.finditer(full))):
                            metric = m.group(1)
                            pseudo_col = m.group(2) + "测点" + m.group(3)
                            # 从后往前找最近的同指标 chart 位置
                            pos = ""
                            for t in ctx:
                                if "chart" not in t:
                                    continue
                                for cm in chart_re.finditer(t):
                                    if cm.group(1) != metric:
                                        continue
                                    cand = cm.group(2)
                                    if not pos and cand:
                                        pos = cand
                            if not pos and name_keys:
                                # 退一步：上下文里含真实位置词的表题句
                                for t in ctx:
                                    if "监测统计" not in t \
                                            and "统计结果如下表" not in t:
                                        continue
                                    for k in sorted(
                                            name_keys, key=len, reverse=True):
                                        if k in t:
                                            pos = k
                                            break
                                    if pos:
                                        break
                            if not pos:
                                unresolved.append(
                                    f"{pseudo_col}({metric})")
                                continue
                            new_tok = (f"{{{{cell.{metric}.{pos}."
                                       f"{m.group(4)}"
                                       + (f"#{m.group(5)}"
                                          if m.group(5) else "")
                                       + "}}")
                            s, e = m.span()
                            fixed[s:e] = list(new_tok)
                            changed = True
                            repaired += 1
                            log.warning(
                                "模板伪位置列 %s(%s) -> %s（依据同节 "
                                "{{chart.%s_%s_...}} 回填）",
                                pseudo_col, metric, pos, metric, pos)
                        if changed:
                            para.runs[0].text = "".join(fixed)
                            for r in para.runs[1:]:
                                r.text = ""
        if unresolved:
            log.warning(
                "模板存在无法自动回填的伪位置列 %s，请人工/LLM 检查该节"
                "表格位置名词（可能需在源文档中补位置列）",
                "、".join(dict.fromkeys(unresolved)))
        return repaired

    @staticmethod
    def _template_para_metric_prefixes(texts, bridge, period) -> set:
        """从段落文本里提取 chart/cell 占位符的指标前缀。"""
        out = set()
        for t in texts or []:
            for mm in re.finditer(r"\{\{(?:chart|cell)\.([A-Za-z_]+)", str(t)):
                prefix = mm.group(1)
                metric = next(
                    (mk for mk in sorted(bridge.metrics, key=len,
                                         reverse=True)
                     if prefix == mk or prefix.startswith(mk + "_")),
                    None)
                if metric:
                    out.add(metric)
        return out

    def _metric_summary_ok(self, bridge, metric: str, period: Dict) -> bool:
        """该指标的小结能否生成：季度统计里有实际特征键。"""
        try:
            return bool(bridge._feature_for_metric(metric, period))
        except Exception:  # noqa: BLE001
            return False


    @staticmethod
    def _metric_for_chart(cid: str, bridge) -> str:
        """从图表 ID 推导指标名（temperature_trend_1 -> temperature）。"""
        parsed = bridge._parse_chart_id(cid)
        return parsed[0] if parsed and parsed[0] else ""

    def _section_plan(self, bridge, cluster, texts) -> Optional[Dict]:
        """推断一个图表集群（节）应出的（指标, 监测部位列表, 图型集合）。

        返回 {"metric", "positions": [(位置, [传感器编号])], "kinds": [...]} 或 None。
        位置来源：
          - 温湿度/结构温度/应变/振动等有映射表的，用表格映射/测点映射；
          - 其余（风荷载/挠度/位移/倾角/索力/裂缝等）用该节表格 cell_ref 的行标签。
        """
        paras = [c.get("paragraph") for c in cluster if isinstance(c.get("paragraph"), int)]
        if not paras:
            return None
        p0 = min(paras)
        section_title = ""
        heading_pno = None
        for pno in range(p0 - 1, max(p0 - 120, -1), -1):
            if 0 <= pno < len(texts):
                t = str(texts[pno]).strip()
                if not t:
                    continue
                if re.match(r"^\d+(\.\d+){1,3}(?=[\u4e00-\u9fa5\s])", t) and len(t) <= 60:
                    section_title = t
                    heading_pno = pno
                    break
        if not section_title or heading_pno is None:
            return None
        metric, mkey = None, ""
        if "结构温度" in section_title:
            metric, mkey = "structure_temperature", "结构温度表"
        elif "环境温度" in section_title:
            metric, mkey = "temperature", "温湿度表"
        elif "环境湿度" in section_title:
            metric, mkey = "humidity", "温湿度表"
        elif "风速" in section_title or "风向" in section_title or "风荷载" in section_title:
            metric = "wind_speed"
        elif "挠度" in section_title:
            metric = "deflection"
        elif "应变" in section_title:
            metric, mkey = "strain", "结构应变监测表"
        elif "位移" in section_title:
            metric = "displacement"
        elif "倾角" in section_title or "转角" in section_title:
            metric = "rotation"
        elif "索力" in section_title:
            metric = "cable_force"
        elif "裂缝" in section_title:
            metric, mkey = "crack", "裂缝监测表"
        elif "振动" in section_title:
            metric, mkey = "vibration", "结构振动监测表"
        if not metric:
            return None
        # 图型集合：节标题 + 该节说明句（“……时程曲线图、频率分布直方图如下图所示”）
        kinds = set()
        _win = [section_title]
        for _pno in range(p0 - 1, max(p0 - 6, -1), -1):
            if 0 <= _pno < len(texts) and str(texts[_pno]).strip():
                _win.append(str(texts[_pno]))
        _joined = "".join(_win)
        if "直方图" in _joined or "频率分布" in _joined:
            kinds.add("histogram")
        if "时程" in _joined or "时间序列" in _joined:
            kinds.add("trend")
        if not kinds:
            kinds = {"trend"}
        # 位置集合
        positions = []
        if mkey and (bridge.table_map or {}).get(mkey):
            for pos, sids in (bridge.table_map[mkey] or {}).items():
                positions.append((pos, [str(x) for x in sids]))
        elif mkey and mkey in (bridge.point_map or {}):
            for pl in bridge.point_map[mkey]:
                sids = [str(x) for x in ((pl.get("测点") or {}).values())]
                positions.append((pl.get("断面位置", ""), sids))
        else:
            positions = self._cell_ref_positions(bridge, heading_pno)
        if not positions:
            return None
        positions = self._filter_positions(positions, section_title)
        if not positions:
            return None
        return {"metric": metric, "positions": positions, "kinds": sorted(kinds)}

    @staticmethod
    def _filter_positions(positions, section_title) -> List[tuple]:
        """按节标题里的位置词过滤监测部位，避免补图时把同指标其它节的位置也补进来。"""
        body = re.sub(r"^\d+(\.\d+){1,3}\s*", "", section_title)
        for w in ("环境温度", "环境湿度", "结构温度", "风速", "风向", "风荷载", "挠度", "应变",
                  "位移", "空间变位", "变位", "倾角", "转角", "索力", "裂缝", "振动",
                  "监测", "统计",
                  "数据分析", "结构", "主梁"):
            body = body.replace(w, "")
        body = body.strip()
        if not body:
            return positions  # 节标题没有位置词（如“风荷载监测数据分析”）-> 全量
        norm_body = _norm(body)
        # 方位感知：节标题带方位（上游/下游/左幅/右幅…）时只匹配同方位位置；
        # 节标题不带方位时，不把带方位的位置拉进来（如标题“跨中1/2截面环境
        # 温度”只应有 跨中1/2截面 的图，不能补出 跨中1/2截面上游/下游）。
        _SIDES = ("上游侧", "下游侧", "左侧", "右侧", "上游", "下游",
                  "左幅", "右幅")
        body_sides = [s for s in _SIDES if s in norm_body]
        positions_norm = {_norm(p): p for p, _ in positions}
        def _base(s):
            return re.sub("|".join(_SIDES), "", s)

        out = []
        # 第一轮：去方位基座匹配。标题带方位时只收同方位位置；标题不带
        # 方位时只收无方位位置（避免“跨中1/2截面环境温度”补出上游/下游图）。
        for np, pos in positions_norm.items():
            if body_sides:
                if not any(s in np for s in body_sides):
                    continue
            elif any(s in np for s in _SIDES):
                continue
            if (_base(np) == norm_body
                    or (len(norm_body) >= 2 and norm_body in _base(np))
                    or (len(_base(np)) >= 2 and _base(np) in norm_body)):
                out.append(pos)
        if not out and not body_sides:
            # 标题无方位且没有无方位位置（如“2#墩墩顶空间变位”表里只有
            # 左幅/右幅）：回退到含方位位置的基座匹配
            for np, pos in positions_norm.items():
                if (_base(np) == norm_body
                        or (len(norm_body) >= 2 and norm_body in _base(np))
                        or (len(_base(np)) >= 2 and _base(np) in norm_body)):
                    out.append(pos)
        if not out:
            # 原逻辑兜底：位置词完整片段匹配（前后是顿号/逗号/开头/结尾）
            for np, pos in positions_norm.items():
                if norm_body == np or norm_body in np or re.search(
                        rf"(^|[、，,和及]){re.escape(np)}([、，,和及]|$)",
                        norm_body):
                    out.append(pos)
        # 列表式墩号位置（如 “4#、5#墩底部”）-> 展开为 4#墩底部、5#墩底部
        m_list = re.match(r"^(\d+#(?:[、，,和及]\d+#)+)(.+)$", norm_body)
        if m_list:
            nums = re.findall(r"(\d+)#", m_list.group(1))
            suffix = m_list.group(2)
            for n in nums:
                cand = f"{n}#{suffix}"
                for np, pos in positions_norm.items():
                    if cand == np or cand in np or np in cand:
                        out.append(pos)
        # 跨号展开（如 “第6、7跨跨中断面”），要求含“跨中”时位置也含“跨中”
        spans = re.findall(r"\d+", "".join(re.findall(r"第([\d、，,和及]+)跨", body)))
        if not out and spans:
            need_mid = "跨中" in body
            for pos, _ in positions:
                np = _norm(pos)
                ok = any(f"第{s}跨" in np for s in spans)
                if ok and need_mid and "跨中" not in np:
                    ok = False
                if ok:
                    out.append(pos)
        seen = set()
        dedup = []
        for p in out:
            if p not in seen:
                seen.add(p)
                dedup.append(p)
        return [(p, sids) for p, sids in positions if p in seen]

    def _cell_ref_positions(self, bridge, heading_pno: int) -> List[tuple]:
        """从 analysis cell_ref 收集该节表格的监测部位（位置 -> 该位置传感器）。

        范围取“小节标题之后、下一个小节标题之前”，避免表格行距图表占位符较远时漏采。
        """
        texts = self.cfg.get("_texts", []) or []
        if not (0 <= heading_pno < len(texts)):
            return []
        lo = heading_pno
        hi = min(heading_pno + 250, len(texts))
        for pno in range(heading_pno + 1, hi):
            t = str(texts[pno]).strip()
            if t and re.match(r"^\d+(\.\d+){1,3}(?=[\u4e00-\u9fa5\s])", t) and len(t) <= 60:
                hi = pno
                break
        out = {}
        for ct in (self.cfg.get("_chart_texts", []) or []):
            if ct.get("source") != "cell_ref":
                continue
            p = ct.get("paragraph")
            if not isinstance(p, int) or not (lo < p < hi):
                continue
            row = str(ct.get("row_label") or "").strip()
            if not row or row.startswith("测点"):
                continue
            metric = str(ct.get("metric") or "")
            sids = bridge._sensors_at_position(row, metric) if metric else []
            if sids:
                out.setdefault(row, sids)
        return [(pos, sids) for pos, sids in out.items()]

    def _metric_for_cluster(self, cl, texts) -> str:
        """按最近节标题推断指标。"""
        paras = [c.get("paragraph") for c in cl if isinstance(c.get("paragraph"), int)]
        if not paras:
            return ""
        p0 = min(paras)
        for pno in range(p0 - 1, max(p0 - 120, -1), -1):
            if 0 <= pno < len(texts):
                t = str(texts[pno]).strip()
                if re.match(r"^\d+(\.\d+){1,3}(?=[\u4e00-\u9fa5\s])", t) and len(t) <= 60:
                    for kw, m in (("结构温度", "structure_temperature"),
                                  ("环境温度", "temperature"),
                                  ("环境湿度", "humidity"),
                                  ("风速", "wind_speed"), ("风向", "wind_speed"),
                                  ("挠度", "deflection"), ("应变", "strain"),
                                  ("位移", "displacement"), ("倾角", "rotation"),
                                  ("转角", "rotation"), ("索力", "cable_force"),
                                  ("裂缝", "crack"), ("振动", "vibration")):
                        if kw in t:
                            return m
                    break
        return ""

    def _detect_chart_gaps(self, bridge, chart_items, chart_sensors,
                           chart_kinds, chart_para) -> List[Dict]:
        """按“该节表格的监测部位 × 图型”检测缺图，并生成补齐清单。"""
        texts = self.cfg.get("_texts", []) or []
        # 聚类：按节标题切分（每出现一个数字编号标题就开新簇），
        # 避免相邻节图表离得近时被并到同一簇、锚点选到下一节
        heading_idx = []
        for pno, t in enumerate(texts):
            ts = str(t).strip()
            if ts and len(ts) <= 60 and re.match(r"^\d+(\.\d+){1,3}(?=[\u4e00-\u9fa5\s])", ts):
                heading_idx.append(pno)

        def _section_of(p: int):
            h = None
            for hh in heading_idx:
                if hh < p:
                    h = hh
                else:
                    break
            return h

        clusters: Dict[int, List[Dict]] = {}
        for ct in chart_items:
            p = ct.get("paragraph") or 0
            clusters.setdefault(_section_of(p), []).append(ct)
        clusters = [v for k, v in sorted(clusters.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))]

        gaps = []
        for cl in clusters:
            cids = [str(ct.get("_unique_chart_id") or ct.get("chart_id"))
                    for ct in cl if ct.get("_unique_chart_id") or ct.get("chart_id")]
            if not cids:
                continue
            used = sorted({s for c in cids if (s := chart_sensors.get(c))})
            if not used:
                continue
            plan = self._section_plan(bridge, cl, texts)
            if not plan:
                continue
            metric = plan["metric"]
            target_kinds = plan["kinds"]
            # 已插图组合 (监测部位, 图型)
            charted = set()
            for c in cids:
                s = chart_sensors.get(c)
                k = chart_kinds.get(c)
                if not s or not k:
                    continue
                p = bridge._position_for_sensor(s)
                if p:
                    charted.add((_norm(p), k))
            missing_items = []
            for pos, sids in plan["positions"]:
                for k in target_kinds:
                    if (_norm(pos), k) in charted:
                        continue
                    sid = str(sids[0]) if sids else ""
                    if not sid:
                        sids2 = bridge._sensors_at_position(pos, metric)
                        sid = str(sids2[0]) if sids2 else ""
                    if not sid:
                        continue
                    missing_items.append({
                        "sensor_id": sid, "position": pos,
                        "kind": k, "metric": metric,
                    })
            if not missing_items:
                continue
            section = ""
            if cl and isinstance(cl[0].get("paragraph"), int):
                p0 = cl[0]["paragraph"]
                section = str(texts[p0 - 2])[:40] if 0 <= p0 - 2 < len(texts) else ""
            gaps.append({
                "section": section,
                "metric": metric,
                "positions": [p for p, _ in plan["positions"]],
                "kinds": target_kinds,
                "charted_sensors": used,
                "anchor_cid": cids[-1],
                "missing": missing_items,
            })
        return gaps


def _strip_redundant_loc_clauses(doc) -> int:
    """删除小结段里含 {{stats.<指标>.<统计>.loc}} 的重复分句。

    仅作用于含 {{summary.<指标>}} 的段落；按 ；。 切分，去掉那些夹着
    “…最高/最低/绝对最大…为{{stats.X.Y}}…对应测点位置为{{stats.X.Y.loc}}”
    的整句（总结里已给出极值与位置）。
    """
    _STAT_WORDS = re.compile(r"最大|最小|绝对|差值|变化|平均|均值|最高|最低")
    _STALE_CLAIM_RE = re.compile(
        # 前缀排除 “{ }”，避免匹配串穿进 {{summary.*}} 占位符把占位符吞掉
        r"(?:监测数据结果表明，)?[^。；\n{}]{0,30}"
        r"(?:监测数据|监测结果|测点状态)"
        r"(?:连续稳定|良好稳定|正常稳定|状态正常|表现良好)")
    _METRIC_LABELS = {
        "strain": ("应变",), "displacement": ("位移", "变位", "GNSS"),
        "vibration": ("振动",), "deflection": ("挠度",),
        "structure_temperature": ("结构温度",), "temperature": ("温度",),
        "humidity": ("湿度",), "cable_force": ("索力",), "crack": ("裂缝",),
        "rotation": ("倾角", "转角"), "bearing_displacement": ("支座位移",),
        "wind_speed": ("风速",),
    }
    removed = 0
    for para in doc.paragraphs:
        t = para.text
        if "{{summary." not in t:
            continue
        summary_metrics = set(re.findall(
            r"\{\{summary\.([a-zA-Z_]+)\}\}", t))
        new_parts = []
        for clause in re.split(r"(?<=[。；\n])", t):
            # 1) 只删除“XX监测数据/监测结果…连续稳定/良好稳定”旧话术子串，
            #    保留前面的 {{summary.*}} 占位符（否则整句删除会连占位符一起删掉）
            c2 = _STALE_CLAIM_RE.sub("", clause)
            if c2 != clause:
                removed += 1
            clause = c2
            # 含 summary 占位符的残留句不再整句删除
            if "{{summary." in clause:
                new_parts.append(clause)
                continue
            locs = re.findall(r"\{\{stats\.([a-zA-Z_]+)\.\w+\.loc\}\}",
                              clause)
            if locs and any(m in summary_metrics for m in locs):
                removed += 1
                continue
            # 非 .loc 的重复数值句（如 3.3.5 “最大应变差值为{{stats.strain.max}}”、
            # “GNSS…X方向最大位移为{{stats.displacement_x.max}}mm”）：分句含
            # 指标词+统计词，且该指标已有总结占位符，删除避免同一数据出现两遍
            plain = re.findall(r"\{\{stats\.([a-zA-Z_]+)\.\w+\}\}", clause)
            if plain and _STAT_WORDS.search(clause):
                hit = False
                for m in plain:
                    base = re.sub(r"_(x|y|z)$", "", m)
                    if base in summary_metrics and any(
                            lb in clause for lb in _METRIC_LABELS.get(base, ())):
                        hit = True
                        break
                if hit:
                    removed += 1
                    continue
            new_parts.append(clause)
        new_t = "".join(new_parts)
        # 清理占位符后残留的多余句号（{{summary.X}}。。 -> {{summary.X}}）
        new_t = re.sub(r"(\}\})\。+", r"\1", new_t)
        if new_t != t and para.runs:
            para.runs[0].text = new_t
            for r in para.runs[1:]:
                r.text = ""
    return removed


def run_once(
    config_path: str = None,
    mode: str = None,
    report_date: str = None,
    engine: str = None,
    inspect_only: bool = False,
    template_override: str = None,
) -> Dict:
    cfg = load_config(config_path)
    if template_override:
        tpl = template_override
        if not os.path.isabs(tpl):
            # 相对路径统一相对项目根目录解析（config 文件已移到 config/ 下）
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            tpl = os.path.normpath(os.path.join(base, tpl))
        if os.path.isfile(tpl):
            cfg["template"] = tpl
        else:
            import logging as _lg
            _lg.getLogger("report-agent.agent").warning(
                "指定的模板不存在，继续使用配置模板: %s", tpl)

    # 统一日志：与 scheduler 一致的 handler 风格
    output_dir = cfg.get("output_dir", "outputs")
    os.makedirs(output_dir, exist_ok=True)
    logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "outputs", "logs")
    os.makedirs(logs_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                os.path.join(logs_dir, "agent.log"),
                encoding="utf-8",
            ),
        ],
        force=True,  # 覆盖已有配置（scheduler 调用时也统一格式）
    )

    date = None
    if report_date:
        date = dt.date.fromisoformat(report_date)
    return ReportAgent(cfg).run(
        mode=mode,
        report_date=date,
        engine=engine,
        inspect_only=inspect_only,
    )


def save_summary(summary: Dict, output_dir: str) -> str:
    """把本次生成摘要保存为 JSON，便于后续归档/通知。"""
    path = os.path.join(output_dir, "last_run.json")
    os.makedirs(output_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    return path
