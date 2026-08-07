# -*- coding: utf-8 -*-
"""配置加载：config.json，支持相对路径解析与命令行覆盖。"""

import json
import os


DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")


def _resolve_path(base_dir: str, path: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(base_dir, path))


def load_config(config_path: str = None) -> dict:
    """加载配置文件，并把所有相对路径转换为相对于配置文件目录的绝对路径。"""
    path = config_path or os.environ.get("REPORT_AGENT_CONFIG") or DEFAULT_CONFIG
    if not os.path.isfile(path):
        raise FileNotFoundError(f"配置文件不存在: {path}")

    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    base = os.path.dirname(os.path.abspath(path))
    cfg["_config_path"] = path
    cfg["template"] = _resolve_path(base, cfg.get("template", ""))
    cfg["output_dir"] = _resolve_path(base, cfg.get("output_dir", "outputs"))

    data = cfg.setdefault("data", {})
    if data.get("file"):
        data["file"] = _resolve_path(base, data["file"])

    charts = cfg.setdefault("charts", {})
    if charts.get("output_dir"):
        charts["output_dir"] = _resolve_path(base, charts["output_dir"])
    matlab = charts.setdefault("matlab", {})
    if matlab.get("script"):
        matlab["script"] = _resolve_path(base, matlab["script"])

    # 成品报告路径（用于 recognizer 和输出文件名推导）
    if cfg.get("source_report"):
        cfg["source_report"] = _resolve_path(base, cfg["source_report"])

    # 加载 chart_texts：从 analysis_file 或自动推导的分析 JSON 中读取
    # chart_texts 是模板识别阶段提取的图表占位信息，运行时需要用它生成图表定义
    analysis_file = cfg.get("analysis_file")
    if analysis_file:
        analysis_file = _resolve_path(base, analysis_file)
    elif cfg.get("source_report"):
        # 自动推导：outputs/analysis_<basename>.json
        basename = os.path.splitext(os.path.basename(cfg["source_report"]))[0]
        auto_path = os.path.join(cfg["output_dir"], f"analysis_{basename}.json")
        if os.path.isfile(auto_path):
            analysis_file = auto_path

    if analysis_file and os.path.isfile(analysis_file):
        try:
            with open(analysis_file, "r", encoding="utf-8") as f:
                analysis = json.load(f)
            chart_texts = analysis.get("chart_texts", [])
            if chart_texts:
                cfg["_chart_texts"] = chart_texts
            # 加载 data_values：annotate_docx 阶段保存的 {{data.N}} -> 原始值映射
            data_values = analysis.get("data_values", {})
            if data_values:
                cfg["_data_values"] = data_values
            # 全文档文本流（按段落索引），用于图表位置上下文继承
            texts = analysis.get("texts", [])
            if texts:
                cfg["_texts"] = texts
        except (json.JSONDecodeError, KeyError) as exc:
            import logging
            logging.getLogger("report-agent.config").warning(
                "加载 chart_texts / data_values 失败: %s", exc
            )

    return cfg
