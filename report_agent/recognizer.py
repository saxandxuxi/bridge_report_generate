# -*- coding: utf-8 -*-
"""成品报告解析识别：分析一份已完成的 DOCX / PDF 报告，判断

1. 哪些图片是动态数据图（建议替换为新图表），哪些是固定图（如 CAD 图、
   示意图、logo，保留）；
2. 哪些数字是动态统计值（建议替换为 {{stats.*}} 占位符），哪些是固定参数
   （如"桥长123米"这类设计常量，保留）。

识别流程（两轮筛选）：
  第一轮 — 关键词打分：基于上下文关键词、图题、文本长度进行启发式打分，
           给出 replace / keep / review 初步结论。
  第二轮 — LLM 完整性校验（llm_classifier.verify_and_complete，默认接入
           Qwen/DashScope）：
           1. 检查第一轮提取结果是否漏掉动态值（missed）
           2. 检查是否有静态值被误判为动态（wrong）
           3. 对待确认项（review）给出最终 replace / keep 判定
           4. 识别季度/时间表述并建议动态化（text_replacements）
           若全部正确，LLM 只回复"是"。
           LLM 不可用时自动降级为文本长度启发式。

输出统一的识别 JSON；对 DOCX 还可以生成一份"标注草稿"，把动态项直接改写为
统一标准的占位符，供人工确认后作为模板交给 run_agent.py 使用。

占位符统一标准：{{stats.<指标英文名>.<统计类型>}}
  指标英文名：temperature/humidity/deflection/displacement/rotation/strain/
             stress/cable_force/cable_clamp/vehicle_load/wind_load/wind_speed/
             earthquake_load/structural_temp/vehicle_count/settlement ...
  统计类型：max/min/avg/median/std/range

注意：这是基于关键词/上下文/图题/LLM 的启发式识别，结果需要人工复核；
不确定的项会标记为 review。
"""

import datetime as dt
import json
import logging
import os
import re
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("report-agent.recognizer")

# 词库文件路径（项目根目录）
_GLOSSARY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "keyword_glossary.json",
)

# 内置 fallback 信号（即使词库文件不存在也能运行）
_FALLBACK_DYNAMIC_TITLES = [
    "监测统计", "监测结果", "测值统计", "测值表", "动态数据",
    "变化监测", "时程监测", "频率分布", "分布统计", "变化统计",
]
_FALLBACK_STATIC_TITLES = [
    "对照表", "阈值表", "监测阈值", "作用阈值", "报警阈值", "控制值",
    "测点表", "测点布置", "测点布设", "测点一览", "监测内容表",
    "监测要素表", "监测项目表", "装备清单", "设备清单", "装备设置",
    "装备表", "设备表", "传感器类型", "分级对照", "等级对照",
    "风力分级", "风级对照", "风力对照", "规范表", "标准表",
    "技术参数", "车道分布", "车速统计", "车型统计", "交通量统计",
]

# 标题关键词 → metric 名
_TITLE_TO_METRIC = [
    # (匹配词, metric, 中文标签)
    ("温度", "temperature", "温度"),
    ("湿度", "humidity", "湿度"),
    ("应变", "strain", "应变"),
    ("挠度", "deflection", "挠度"),
    ("转角", "rotation", "转角"),
    ("倾角", "rotation", "倾角"),
    ("索夹", "cable_clamp", "索夹"),
    ("索力", "cable_force", "索力"),
    ("风速", "wind_speed", "风速"),
    ("风向", "wind_dir", "风向"),
    ("位移", "displacement", "位移"),
    ("变位", "displacement", "变位"),
    ("偏位", "displacement", "偏位"),
    ("振动", "vibration", "振动"),
    ("应力", "stress", "应力"),
    ("沉降", "settlement", "沉降"),
    ("车辆", "vehicle_count", "车辆"),
    ("交通", "vehicle_count", "交通"),
    ("荷载", "vehicle_count", "荷载"),
    ("车流", "vehicle_count", "车流"),
    ("桥面", "displacement", "桥面位移"),
    ("支座", "bearing_displacement", "支座位移"),
]

# 列头关键词 → stat 名（中文列头可能含"平均"，"最大"，"温差"等）
_HEADER_TO_STAT = [
    # (匹配词列表, stat, 优先级)
    (["平均温度", "平均值", "均值", "日均", "平均"], "avg", 0),
    (["最大温差", "最高温差"], "range", 0),
    (["最大", "最高"], "max", 0),
    (["最小", "最低"], "min", 0),
    (["绝对最大"], "abs_max", 0),
    (["均方根"], "rms", 0),
    (["温差", "差值", "极差"], "range", 0),
    (["标准差", "std"], "std", 0),
    (["中位数", "median"], "median", 0),
    (["合计", "累计", "总和"], "sum", 0),
]


@lru_cache(maxsize=1)
def load_glossary() -> Dict:
    """加载术语库（含动态/静态表格信号）。"""
    g: Dict = {
        "dynamic_signals": list(_FALLBACK_DYNAMIC_TITLES),
        "static_signals": list(_FALLBACK_STATIC_TITLES),
        "components": [],
        "bridge_names": [],
        "metric_keywords": [],
    }
    try:
        if os.path.isfile(_GLOSSARY_PATH):
            with open(_GLOSSARY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k in g:
                if k in data:
                    g[k] = data[k]
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("加载术语库失败，使用 fallback: %s", exc)
    return g


def slugify_label(label: str) -> str:
    """把行标签/列头清洗为可读占位符片段。

    例："岳阳索塔上游侧塔冠" → "岳阳索塔上游侧塔冠"（保留中文）
        "平均温度/℃" → "平均温度"（去除单位符号）
    """
    if not label:
        return ""
    out = label.strip()
    # 去除单位括号：平均温度/℃ → 平均温度
    out = re.sub(r"/[\w℃%°/]+$", "", out).strip()
    # 去除纯符号
    out = re.sub(r"[\s/\\()\[\]{}]+", "_", out)
    out = re.sub(r"_+", "_", out).strip("_")
    return out or label.strip()


def metric_from_title(title: str) -> str:
    """从表格标题推断 metric 名。"""
    if not title:
        return "generic"
    # 长度优先匹配（"塔冠环境温度监测统计" 中 "塔冠" 在前，但 "温度" 更具体）
    # 先按特定关键词，再按通用关键词
    for kw, metric, _ in _TITLE_TO_METRIC:
        if kw in title:
            return metric
    return "generic"


def stat_from_col_header(header: str) -> str:
    """从列头推断统计类型。"""
    if not header:
        return "value"
    # 用最长匹配（"绝对最大值" 优先于 "最大"）
    candidates = []
    for keys, stat, _ in _HEADER_TO_STAT:
        for k in keys:
            if k in header:
                candidates.append((len(k), stat))
                break
    if candidates:
        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1]
    # 启发式：纯数字列头可能是"测值"
    return "value"


def is_dynamic_table_title(title: str) -> bool:
    """判断表格标题是否属于动态监测表（值需替换）。"""
    g = load_glossary()
    t = title or ""
    # 静态信号胜出（更具体）
    for sig in g["static_signals"]:
        if sig in t:
            return False
    # 动态信号匹配
    for sig in g["dynamic_signals"]:
        if sig in t:
            return True
    return False




# 旧的 signal-driven 标题识别（保留供数字识别等使用）
TABLE_TITLE_SIGNALS_LEGACY = [
    "监测内容表", "监测项目表", "测点布设表", "测点布置表",
    "阈值表", "监测阈值", "作用监测阈值", "结构响应监测阈值",
    "报警阈值", "控制值表",
    "对照表", "分级表", "分级对照", "等级对照",
    "风力分级", "风级对照",
    "监测部位表", "设备清单", "传感器类型表",
    "规范表", "标准表",
]


from .llm_classifier import (
    LLMClassifier,
    SHORT_CONTEXT_THRESHOLD,
    LONG_CONTEXT_THRESHOLD,
    _text_length_heuristic,
)

log = logging.getLogger("report-agent.recognizer")

NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
MARKER_RE = re.compile(r"\{\{[^}]+\}\}")
SEQ_RE = re.compile(r"[图表第]\s*\d+")
DATE_RE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}")
KILO_POST_RE = re.compile(r"[Kk]\s*\d+\s*\+?\s*\d*")
CAPTION_RE = re.compile(r"^\s*(图|表|Figure|Fig\.?)\s*\d*")
# 图表文本占位：报告里 "xxx_time_series" / "xxx_histogram" 等行表示该处应插入图表
CHART_TEXT_RE = re.compile(r"_(time_series|histogram|curve|frequency|scatter|boxplot)$")

# 纯图题识别：图题末尾含"图"且包含动态图表关键词
# 例：交通荷载监测_车辆累计通过数量统计图、梁端转角测点变化时程曲线图
BARE_CHART_CAPTION_RE = re.compile(
    r"^(?P<prefix>.+?)(?P<kind>(曲线|时程|直方|柱状|分布|趋势|占比|变化|统计|频谱|频次|相关|车流|载荷|荷载)图?)$"
)
# 表格 Excel 单元格引用占位符：H(行,列) / M(行,列) / J(行,列)
# 字母前缀表示表格（指标），行号从 2 开始（标题行算 1），列号从 2 开始（标签列算 1）
# 例：H(10,2) = 索夹表 87-L 索夹 1 的平均值列
CELL_REF_RE = re.compile(r"([A-Z])\((\d+)\s*,\s*(\d+)\)")

# 动态统计关键词（出现则倾向于替换）
DYNAMIC_WORDS = [
    "最高", "最低", "最大", "最小", "平均", "均值", "中位数", "标准差", "极差", "方差",
    "占比", "比例", "增长率", "同比", "环比", "上涨", "下降", "超标", "超过", "低于", "高于",
    "累计", "合计", "总计", "共", "趋势", "温度", "湿度", "流量", "水位", "浓度", "沉降",
    "位移", "应变", "应力", "裂缝", "挠度", "温差", "雨量", "风速", "合格率", "通过率",
    "偏差", "波动", "监测", "统计", "指标", "产值", "营收", "销售额", "产量", "利润",
    "天数", "小时", "频次", "次数", "超标日",
    # 桥梁健康监测专用
    "索力", "索夹", "转角", "变位", "振动", "车辆", "地震", "加速度",
    "差值", "最大值", "最小值", "平均风速", "最大差值", "最小差值", "通过数量",
]

# 固定参数关键词（出现则倾向于保留）
# 注意：只放"真正的静态参数指标"，位置词（塔冠/上游侧/测点等）不放——
# 它们在动态上下文里也大量出现，会错误地压低动态值的分数。
STATIC_WORDS = [
    "桥长", "桥宽", "跨径", "孔径", "桩长", "桩径", "桩号", "设计", "标准", "规范",
    "编号", "图号", "图纸", "坐标", "高程", "比例尺", "型号", "规格", "地址", "电话",
    "传真", "合同", "标段", "里程", "日期", "年份", "版本", "负责人", "单位", "项目名称",
    "车道", "限速", "荷载等级", "强度等级", "材料", "材质",
    "面积", "容积", "功率", "电压", "频率", "重量", "单价", "金额", "造价", "预算", "投资",
    "预应力", "钢束", "锚具", "斜拉索",
    # 签字表 / 机构信息
    "检测有限公司", "资质", "证书", "职称", "签字", "姓名", "岗位", "职业资格",
    # 规范引用（后面跟编号）
    "交办公路", "指南", "规程", "规范编号",
]

# 图片图题关键词
IMAGE_REPLACE_WORDS = [
    "趋势", "曲线", "直方", "柱状", "折线", "饼图", "分布", "对比", "统计", "变化",
    "走势", "箱线", "散点", "相关性", "时序", "频率", "占比", "监测曲线",
]
IMAGE_KEEP_WORDS = [
    "示意", "图纸", "CAD", "设计图", "平面图", "立面图", "剖面图", "效果图", "照片",
    "现场", "logo", "标志", "流程图", "架构", "拓扑", "布置图", "总图", "地形图",
    "地质图", "断面图", "构造图", "配筋图",
]

METRIC_WORDS = {
    "温度": "temperature", "湿度": "humidity", "流量": "flow", "水位": "water_level",
    "浓度": "concentration", "沉降": "settlement", "位移": "displacement",
    "应变": "strain", "应力": "stress", "裂缝": "crack", "挠度": "deflection",
    "雨量": "rainfall", "风速": "wind_speed",
    # 桥梁健康监测专用（与 llm_classifier.METRIC_STANDARD 保持一致）
    "转角": "rotation", "索力": "cable_force", "索夹": "cable_clamp",
    "变位": "displacement", "振动": "vibration", "车辆": "vehicle_count",
    "荷载": "load", "结构温度": "structural_temp",
}
STAT_WORDS = {
    "最高": "max", "最大": "max", "最低": "min", "最小": "min",
    "平均": "avg", "均值": "avg", "中位数": "median", "标准差": "std", "极差": "range",
}


def _window(text: str, start: int, end: int, width: int = 20) -> str:
    return text[max(0, start - width):end + width]


def suggest_placeholder(before: str, after: str):
    """根据数字前后文本中距离最近的关键词建议占位符。

    优先取数字“前面”的关键词（指标词通常写在数字前，如“最高温度26.0℃”）；
    仅当前面不足时再参考后面。stat 词距离阈值 6 字、metric 词 10 字。
    """
    combined = {**METRIC_WORDS, **STAT_WORDS}
    best_metric = best_stat = None
    best_md = best_sd = None

    def consider(text, offset_penalty, from_end=False):
        nonlocal best_metric, best_stat, best_md, best_sd
        for kw, v in combined.items():
            idx = text.rfind(kw) if from_end else text.find(kw)
            if idx == -1:
                continue
            d = (len(text) - idx if from_end else idx) + offset_penalty
            if v in METRIC_WORDS.values() and d <= 10 and (best_md is None or d < best_md):
                best_metric, best_md = v, d
            elif v in STAT_WORDS.values() and d <= 6 and (best_sd is None or d < best_sd):
                best_stat, best_sd = v, d

    consider(before, 0, from_end=True)
    if best_metric is None or best_stat is None:
        consider(after, 30)  # 数字之后的词仅作补充，且带较大距离惩罚
    if best_metric and best_stat:
        return f"stats.{best_metric}.{best_stat}"
    return None


def classify_number(value: str, context: str, before: str = "", after: str = "") -> dict:
    """判断一个数字是否应替换。

    返回 {"verdict": replace|keep|review, "confidence": 0-1,
          "reasons": [...], "placeholder": 建议占位符}
    """
    score = 0.5
    reasons = []

    hits_dyn = [w for w in DYNAMIC_WORDS if w in context]
    hits_static = [w for w in STATIC_WORDS if w in context]
    if hits_dyn:
        score += 0.28
        reasons.append("上下文含动态关键词：" + "、".join(hits_dyn[:3]))
    if hits_static:
        score -= 0.32
        reasons.append("上下文含固定参数关键词：" + "、".join(hits_static[:3]))

    # 计算用于序号检测的干净上下文
    ctx_stripped = context.strip()

    # 数字后面紧跟单位
    if context and context[-1] in "℃%":
        score += 0.10
        reasons.append("带测量单位")

    if SEQ_RE.search(context):
        score -= 0.40
        reasons.append("图/表/第 序号")
    if DATE_RE.search(context):
        score -= 0.35
        reasons.append("日期格式")
    if re.fullmatch(r"\d{4}", value) and after.startswith("年"):
        score -= 0.55
        reasons.append("年份（后跟年字）")
    elif re.fullmatch(r"\d{4}", value) and 1950 <= int(value) <= 2100:
        score -= 0.30
        reasons.append("疑似年份")
    # 月份：年份后的 "01月-03月" 中的 01/03
    if re.fullmatch(r"\d{1,2}", value) and after.startswith("月") and re.search(r"年\d*$", before):
        score -= 0.55
        reasons.append("月份（年份后）")
    if KILO_POST_RE.search(context):
        score -= 0.50
        reasons.append("桩号/里程")

    # 测点布设数量（静态设计信息）："共布置11个测点" / "布设1个测点"
    if "共布置" in before or "共布设" in before or before.endswith(("布置", "布设")):
        score -= 0.50
        reasons.append("测点布设数量")

    # 测点编号：数字前后紧跟 "测点" / "测点N" / "N号测点"
    if re.search(r"测\s*点\s*$", before) or re.search(r"测\s*点\s*$", ctx_stripped):
        score -= 0.55
        reasons.append("测点编号（前）")
    elif re.search(r"^\s*测\s*点", after):
        score -= 0.55
        reasons.append("测点编号（后）")
    if re.search(r"^\s*号\s*测\s*点", after) or re.search(r"号\s*测\s*点$", before):
        score -= 0.55
        reasons.append("测点编号")

    # 索夹/吊索编号："87-L" / "87-R" / "88-L" / "88-R" 等字母结尾
    if re.search(r"[A-Za-z]$", after) and re.fullmatch(r"\d+", value):
        score -= 0.60
        reasons.append("索夹/吊索编号")
    if re.search(r"^\s*-\s*[A-Za-z]$", after) and re.fullmatch(r"\d+", value):
        score -= 0.60
        reasons.append("索夹/吊索编号")

    # 表格标题信号：context 含 "监测内容表" / "阈值表" / "对照表" / "分级表" 等
    table_title_signals = [
        "监测内容表", "监测内容", "监测项目表", "测点布设",
        "阈值表", "监测阈值", "作用监测阈值", "结构响应监测阈值", "报警阈值",
        "对照表", "分级表", "分级对照",
        "监测部位", "监测要素", "传感器类型", "设备清单",
    ]
    if any(sig in context for sig in table_title_signals):
        score -= 0.45
        reasons.append("表格标题属于规范/配置/阈值表")

    # 章节/列表序号（只作用于"数字本身"是序号的情况，不影响同段落其他数字）
    if not before.strip() and re.match(r"^\d+[\.、．]", ctx_stripped):
        score -= 0.60
        reasons.append("章节号（段落开头）")
    elif before.endswith(("（", "(", "第")) and after.startswith(("）", ")", "项")):
        score -= 0.60
        reasons.append("列表序号")
    elif re.search(r"\d\.\d*\.\s*$", before) or re.search(r"\.\d+\s*$", before):
        score -= 0.60
        reasons.append("多级章节号")

    # 文本长度影响：极短上下文倾向 keep（标签/序号），长描述倾向 replace（数据叙述）
    ctx_len = len(ctx_stripped)
    if ctx_len < SHORT_CONTEXT_THRESHOLD:
        score -= 0.08
        reasons.append(f"极短上下文（{ctx_len}字），疑似标签/序号")
    elif ctx_len > LONG_CONTEXT_THRESHOLD:
        score += 0.05
        reasons.append(f"长描述性上下文（{ctx_len}字），疑似数据叙述")

    score = max(0.05, min(0.95, score))
    if score >= 0.68:
        verdict = "replace"
    elif score <= 0.38:
        verdict = "keep"
    else:
        verdict = "review"

    placeholder = None
    if verdict == "replace":
        placeholder = suggest_placeholder(before, after)

    return {
        "verdict": verdict,
        "confidence": round(score, 2),
        "reasons": reasons,
        "placeholder": placeholder,
    }


def classify_image(caption: str, w_in: float = 0, h_in: float = 0,
                   in_header_footer: bool = False) -> dict:
    """判断一张图片是否应替换。"""
    if in_header_footer:
        return {"verdict": "keep", "confidence": 0.92,
                "reasons": ["位于页眉页脚，通常是 logo/装饰"], "placeholder": None}

    caption = (caption or "").strip()
    if not caption:
        if w_in and h_in and max(w_in, h_in) < 1.5:
            return {"verdict": "keep", "confidence": 0.75,
                    "reasons": ["无图题且尺寸小，疑似 logo/图标"], "placeholder": None}
        return {"verdict": "review", "confidence": 0.45,
                "reasons": ["无图题，需人工确认"], "placeholder": None}

    score = 0.5
    reasons = []
    if CAPTION_RE.match(caption):
        reasons.append("带图题")
        hits_r = [w for w in IMAGE_REPLACE_WORDS if w in caption]
        hits_k = [w for w in IMAGE_KEEP_WORDS if w in caption]
        if hits_r and not hits_k:
            score += 0.35
            reasons.append("图题含动态图表关键词：" + "、".join(hits_r[:3]))
        elif hits_k:
            score -= 0.42
            reasons.append("图题含固定图关键词：" + "、".join(hits_k[:3]))
        else:
            score -= 0.10
            reasons.append("图题无明确关键词")
    else:
        score -= 0.20
        reasons.append("无规范图题（图1/Figure 1 格式）")

    # 文本长度影响：极短图题可能是编号，长图题更可能含描述性关键词
    cap_len = len(caption.strip())
    if cap_len < SHORT_CONTEXT_THRESHOLD and score > 0.40:
        score -= 0.05
        reasons.append(f"极短图题（{cap_len}字）")
    elif cap_len > LONG_CONTEXT_THRESHOLD:
        score += 0.04
        reasons.append(f"长描述性图题（{cap_len}字）")

    score = max(0.05, min(0.95, score))
    if score >= 0.68:
        verdict = "replace"
    elif score <= 0.40:
        verdict = "keep"
    else:
        verdict = "review"
    return {"verdict": verdict, "confidence": round(score, 2),
            "reasons": reasons, "placeholder": None}


def _extract_numbers(text: str, base: dict, header_context: str = "") -> list:
    """从一段文本里提取所有数字并分类。"""
    out = []
    markers = list(MARKER_RE.finditer(text))
    for m in NUMBER_RE.finditer(text):
        if any(mo.start() <= m.start() and mo.end() >= m.end() for mo in markers):
            continue  # 跳过占位符内部
        value = m.group(0)
        ctx = _window(text, m.start(), m.end())
        before = text[max(0, m.start() - 12):m.start()]
        after = text[m.end():m.end() + 4]
        if header_context:
            ctx = header_context + " | " + ctx
            if not any(k in before for k in {**METRIC_WORDS, **STAT_WORDS}):
                before = header_context + " " + before
        info = classify_number(value, ctx, before, after)
        info.update(
            {
                "value": value,
                "context": ctx,          # 数字附近的真实上下文（含表格标题）
                "snippet": text.strip()[:80],
                "position": m.start(),
                **base,
            }
        )
        out.append(info)
    return out


# ---------------------------------------------------------------------------
# DOCX 解析
# ---------------------------------------------------------------------------

def _images_in_paragraph(p_elm) -> list:
    from docx.oxml.ns import qn

    imgs = []
    nodes = p_elm.findall(".//" + qn("wp:inline")) + p_elm.findall(".//" + qn("wp:anchor"))
    for node in nodes:
        ext = node.find(qn("wp:extent"))
        cx = cy = 0
        if ext is not None:
            try:
                cx, cy = int(ext.get("cx", 0)), int(ext.get("cy", 0))
            except (TypeError, ValueError):
                pass
        blip = node.find(".//" + qn("a:blip"))
        rid = blip.get(qn("r:embed")) if blip is not None else None
        imgs.append({"rid": rid, "w_emu": cx, "h_emu": cy})
    return imgs


def _is_table_title(text: str) -> bool:
    """判断段落是否为表格标题。"""
    t = text.strip()
    if not t:
        return False
    # 形如 "表1 xx表" / "表2.3 监测阈值表" / "Xxx对照表"
    if re.match(r"^表\s*\d+", t):
        return True
    title_keywords = [
        "监测内容表", "监测项目表", "测点布设表", "测点布置表",
        "阈值表", "监测阈值", "作用监测阈值", "结构响应监测阈值",
        "报警阈值", "控制值表",
        "对照表", "分级表", "分级对照", "等级对照",
        "风力分级", "风级对照",
        "监测部位表", "设备清单", "传感器类型表",
        "规范表", "标准表",
    ]
    return any(kw in t for kw in title_keywords)


def parse_docx(path: str) -> dict:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    from .report_builder import iter_block_items

    doc = Document(path)
    paragraphs = []  # 所有段落文本（含表格内、页眉页脚）
    numbers = []
    images = []
    # 表格布局：每个 cell_ref 段落 → (title, row_idx, col_idx, row_label, col_header)
    # 供后续 cell_ref 处理使用，避免只用 letter+position
    table_layout_for_para: Dict[int, Dict] = {}
    table_layout_for_para_pos: Dict[Tuple[int, int], Dict] = {}

    def handle_paragraph(p, header_footer=False, header_context="", table_title=""):
        idx = len(paragraphs)
        text = p.text
        paragraphs.append(text)
        imgs = _images_in_paragraph(p._p)
        if text.strip():
            ctx_parts = [x for x in (table_title, header_context) if x]
            full_ctx = " | ".join(ctx_parts) if ctx_parts else ""
            numbers.extend(
                _extract_numbers(
                    text, {"page": 1, "paragraph": idx, "table": None,
                           "table_title": table_title},
                    header_context=full_ctx,
                )
            )
        for img in imgs:
            images.append(
                {
                    "index": len(images),
                    "paragraph": idx,
                    "page": 1,
                    "w_in": img["w_emu"] / 914400.0,
                    "h_in": img["h_emu"] / 914400.0,
                    "rid": img["rid"],
                    "in_header_footer": header_footer,
                    "caption": "",
                }
            )

    last_table_title = ""  # 跟踪最近的表标题
    for item in iter_block_items(doc):
        if isinstance(item, Paragraph):
            text = item.text.strip()
            if _is_table_title(text) or is_dynamic_table_title(text):
                # 既要识别传统静态表标题，也要识别动态监测表标题
                last_table_title = text or last_table_title
            handle_paragraph(item, table_title=last_table_title)
        elif isinstance(item, Table):
            header_cells = [c.text.strip() for c in item.rows[0].cells] if item.rows else []
            # 收集每一列的"列头"——最后一行非空子串的列头信息
            for r_i, row in enumerate(item.rows):
                row_label = row.cells[0].text.strip() if row.cells else ""
                for c_i, cell in enumerate(row.cells):
                    col_header = header_cells[c_i] if c_i < len(header_cells) else ""
                    cell_info = {
                        "title": last_table_title or "",
                        "row_idx": r_i + 1,       # 1-based, 含表头
                        "col_idx": c_i + 1,       # 1-based, 含左标签列
                        "row_label": row_label,
                        "col_header": col_header,
                    }
                    for p in cell.paragraphs:
                        combined_title = last_table_title or ""
                        if c_i > 0 and row_label:
                            combined_title = f"{combined_title} {row_label}".strip()
                        elif header_cells and c_i < len(header_cells):
                            combined_title = f"{combined_title} {header_cells[c_i]}".strip()
                        idx = len(paragraphs)
                        handle_paragraph(p, table_title=combined_title)
                        # 记录段落 → 表格单元格布局
                        if c_i > 0:  # 标签列不需要（cell_ref 只在数据区）
                            table_layout_for_para[idx] = cell_info
            last_table_title = ""  # 重置

    for section in doc.sections:
        for p in section.header.paragraphs:
            handle_paragraph(p, header_footer=True)
        for p in section.footer.paragraphs:
            handle_paragraph(p, header_footer=True)

    # 图题关联：图片所在段之后第一个非空段落
    for img in images:
        nxt = img["paragraph"] + 1
        while nxt < len(paragraphs) and not paragraphs[nxt].strip():
            nxt += 1
        if nxt < len(paragraphs):
            img["caption"] = paragraphs[nxt].strip()

    # 预计算：每段对应的"最近且相邻"的表标题（用于 cell_ref 的上下文）
    # 只在距离 8 段以内的标题才算"本表"，避免远处表格标题污染
    last_table_title_for_para = {}
    current_title = ""
    gap = 0
    MAX_GAP = 8  # 距离阈值
    for idx_p, t in enumerate(paragraphs):
        t_strip = t.strip()
        if _is_table_title(t_strip):
            current_title = t_strip
            gap = 0
        else:
            gap += 1
        if current_title and gap <= MAX_GAP:
            last_table_title_for_para[idx_p] = current_title

    # 图表文本占位检测：报告中的 "xxx_time_series" / "xxx_histogram" 行
    # 以及"纯图题"（末尾含"曲线图/直方图/分布图/统计图"等关键词的段落）
    # 以及"表格 Excel 单元格引用"（H(行,列) / M(行,列) / J(行,列)）
    chart_texts = []
    seen_para = set()  # 避免同一段被多次加入
    for i, t in enumerate(paragraphs):
        t = t.strip()
        if not t or i in seen_para:
            continue
        m = CHART_TEXT_RE.search(t)
        if m:
            kind = m.group(1)
            # 从文本前缀提取指标关键字，生成更有意义的图表 ID
            prefix = t[:m.start()].strip()
            metric_kw = next(
                (kw for kw in ("挠度", "应变", "转角", "索夹", "位移", "风速", "车辆",
                               "温度", "湿度", "振动", "应力", "沉降", "索力")
                 if kw in prefix),
                "chart",
            )
            metric_en = {
                "挠度": "deflection", "应变": "strain", "转角": "rotation",
                "索夹": "cable_clamp", "位移": "displacement", "风速": "wind_speed",
                "车辆": "vehicle_count", "温度": "temperature", "湿度": "humidity",
                "振动": "vibration", "应力": "stress", "沉降": "settlement",
                "索力": "cable_force",
            }.get(metric_kw, "chart")
            chart_id = "trend" if kind in ("time_series", "curve") else \
                       "histogram" if kind == "histogram" else kind
            chart_texts.append({
                "paragraph": i,
                "kind": kind,
                "chart_id": chart_id,
                "metric": metric_en,
                "text": t,
                "source": "explicit_suffix",
            })
            seen_para.add(i)
            continue
        # === 纯图题识别：段落文本本身是图题且包含"图"和动态指标词 ===
        # 例："交通荷载监测_车辆累计通过数量统计图"、"梁端转角测点变化时程曲线图"
        if t.endswith("图") and any(
            kw in t for kw in ("曲线", "时程", "直方", "柱状", "分布", "趋势",
                               "占比", "变化", "统计", "频谱", "频次", "相关", "车流")
        ) and any(
            kw in t for kw in ("温度", "湿度", "风速", "风向", "转角", "索夹", "位移",
                               "车辆", "交通", "荷载", "应变", "应力", "挠度", "索力",
                               "沉降", "振动", "结构", "桥面", "主梁", "塔", "钢桁")
        ):
            # 从图题中推断指标
            metric_kw = next(
                (kw for kw in ("挠度", "应变", "转角", "索夹", "位移", "风速", "风向",
                               "车辆", "温度", "湿度", "振动", "应力", "沉降", "索力",
                               "交通", "荷载", "车流")
                 if kw in t),
                None,
            )
            metric_en = {
                "挠度": "deflection", "应变": "strain", "转角": "rotation",
                "索夹": "cable_clamp", "位移": "displacement", "风速": "wind_speed",
                "风向": "wind_dir", "车辆": "vehicle_count", "温度": "temperature",
                "湿度": "humidity", "振动": "vibration", "应力": "stress",
                "沉降": "settlement", "索力": "cable_force",
                "交通": "vehicle_count", "荷载": "load", "车流": "vehicle_count",
            }.get(metric_kw, "chart")
            # 推断图表类型
            if "直方" in t or "分布" in t or "频次" in t or "频谱" in t:
                kind = "histogram"
                chart_id = "histogram"
            elif "柱状" in t or "占比" in t or "车流" in t or "通过数量" in t or "累计" in t:
                kind = "bar"
                chart_id = "bar"
            elif "散点" in t or "相关" in t:
                kind = "scatter"
                chart_id = "scatter"
            else:
                kind = "trend"
                chart_id = "trend"
            chart_texts.append({
                "paragraph": i,
                "kind": kind,
                "chart_id": chart_id,
                "metric": metric_en,
                "text": t,
                "source": "bare_caption",
            })
            seen_para.add(i)
            continue
        # === 表格 Excel 单元格引用占位符识别 ===
        # 例："H(10,2)" "M(2,2)" "J(2,2)" 等
        # 表格字母 → 指标（与模板约定）+ 是否动态
        letter_to_metric = {
            # 动态表（监测统计结果）
            "H": ("cable_clamp", True),    # 索夹滑动 Hangers
            "M": ("rotation", True),       # 转角 Measurement
            "J": ("wind_speed", True),     # 风速
            # 静态表（规范/阈值/设备）
            "A": ("static_a", False), "B": ("static_b", False),
            "C": ("static_c", False), "D": ("static_d", False),
            "E": ("static_e", False), "F": ("static_f", False),
            "G": ("static_g", False),
        }
        # 表格标题信号（属于静态表的特征）
        static_table_signals = [
            "监测内容表", "监测内容", "监测项目表", "测点布设表", "测点布置表",
            "测点一览表", "传感器类型表", "设备清单", "装备设置", "装备清单",
            "阈值表", "监测阈值", "作用监测阈值", "结构响应监测阈值",
            "报警阈值", "控制值表", "作用阈值",
            "对照表", "分级表", "分级对照", "等级对照",
            "风力分级", "风级对照", "风力对照",
            "监测部位表", "监测要素表",
            "规范表", "标准表",
            "交通量统计表", "车道分布",
            "车速统计", "车型统计",
        ]
        if CELL_REF_RE.search(t):
            cell_refs = list(CELL_REF_RE.finditer(t))
            for cm in cell_refs:
                table_letter = cm.group(1)
                row_idx = int(cm.group(2))   # letter 模板给定的"行"
                col_idx = int(cm.group(3))   # letter 模板给定的"列"
                # 上下文：先看 table_layout（更准），再看 last_table_title_for_para
                layout = table_layout_for_para.get(i, {})
                ctx_title = layout.get("title") or last_table_title_for_para.get(i, "")
                row_label = layout.get("row_label", "")
                col_header = layout.get("col_header", "")
                # 推断 metric：用 table title 优先，再用 letter 兜底
                if ctx_title and is_dynamic_table_title(ctx_title):
                    metric_en = metric_from_title(ctx_title)
                    is_static_table = False
                else:
                    # 字母兜底（保留旧约定）
                    letter_info = {
                        "H": ("cable_clamp", True),
                        "M": ("rotation", True),
                        "J": ("wind_speed", True),
                    }
                    metric_en, is_dynamic_by_letter = letter_info.get(
                        table_letter, (f"table_{table_letter.lower()}", True)
                    )
                    if not is_dynamic_by_letter:
                        is_static_table = True
                    elif ctx_title and not is_dynamic_table_title(ctx_title):
                        is_static_table = True
                    else:
                        is_static_table = False
                        # 静态表但有合法 cell_ref，兜底用 letter title
                        if metric_en.startswith("table_"):
                            metric_en = ctx_title or metric_en
                # 推断 stat：用列头
                stat_from_header = stat_from_col_header(col_header)
                # 推断 row 标识：清洗 row_label
                row_slug = slugify_label(row_label) or f"r{row_idx}"
                col_slug = slugify_label(col_header) or f"c{col_idx}"
                verdict = "keep" if is_static_table else "replace"
                chart_texts.append({
                    "paragraph": i,
                    "kind": "cell_ref",
                    "chart_id": f"cell_{table_letter.lower()}_{row_idx}_{col_idx}",
                    "metric": metric_en,
                    "text": cm.group(0),
                    "table_letter": table_letter,
                    "row": row_idx,
                    "col": col_idx,
                    "row_label": row_label,
                    "col_header": col_header,
                    "table_title": ctx_title,
                    "position": cm.start(),
                    "verdict": verdict,
                    "source": "cell_ref",
                })

    return {
        "images": images,
        "numbers": numbers,
        "_table_layout": table_layout_for_para,
        "_chart_texts_raw": chart_texts,

        "numbers": numbers,
        "texts": paragraphs,
        "chart_texts": chart_texts,
    }


# ---------------------------------------------------------------------------
# PDF 解析
# ---------------------------------------------------------------------------

def _pdf_lines(page):
    """把一页文字按行归组，返回 [(top, text), ...]。"""
    words = page.extract_words(x_tolerance=2, y_tolerance=3) or []
    lines = {}
    for w in words:
        key = round(w["top"] / 4.0) * 4
        lines.setdefault(key, []).append(w)
    result = []
    for top, ws in sorted(lines.items()):
        ws.sort(key=lambda x: x["x0"])
        result.append((top, " ".join(w["text"] for w in ws)))
    # 合并被换行截断的数字：前一行以 "." 结尾且下一行以数字开头（如 "1." + "18℃"）
    merged = []
    for top, txt in result:
        if merged and merged[-1][1].endswith(".") and re.match(r"\d", txt):
            merged[-1] = (merged[-1][0], merged[-1][1] + txt)
        else:
            merged.append((top, txt))
    return merged


def parse_pdf(path: str) -> dict:
    import pdfplumber

    numbers = []
    images = []
    all_texts = []
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            lines = _pdf_lines(page)
            for top, txt in lines:
                numbers.extend(
                    _extract_numbers(txt, {"page": pno, "paragraph": int(top), "table": None})
                )
                all_texts.append(txt)

            for im in page.images:
                x0, x1 = im.get("x0", 0), im.get("x1", 0)
                y0, y1 = im.get("top", 0), im.get("bottom", 0)
                caption = None
                for top, txt in sorted(lines):
                    if top >= y1 - 2 and top <= y1 + 48 and CAPTION_RE.match(txt):
                        caption = txt
                        break
                images.append(
                    {
                        "index": len(images),
                        "page": pno,
                        "w_in": (x1 - x0) / 72.0,
                        "h_in": (y1 - y0) / 72.0,
                        "bbox": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)],
                        "in_header_footer": False,
                        "caption": caption or "",
                    }
                )
    return {"images": images, "numbers": numbers, "texts": all_texts}


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def _build_doc_text(parsed: dict, max_chars: int = 22000) -> str:
    """把解析出的段落文本整理为 LLM 校验用的报告文本摘录。

    只保留含数字的段落（漏掉的动态值必然是数字），并按行截断控制提示词规模。
    """
    texts = parsed.get("texts") or []
    lines = []
    total = 0
    for i, t in enumerate(texts):
        t = t.strip()
        if not t or not re.search(r"\d", t):
            continue
        # 行内截断，保留数字附近的上下文
        if len(t) > 150:
            t = t[:150] + "…"
        lines.append(f"段落{i}: {t}")
        total += len(t) + 8
        if total > max_chars:
            lines.append(f"……（文本过长已截断，共 {len(texts)} 段）")
            break
    return "\n".join(lines)


def _locate_value(value: str, snippet: str, texts: list) -> Optional[tuple]:
    """在段落文本中定位某个数字，返回 (paragraph_index, position) 或 None。

    优先用 snippet 匹配，其次用 value 匹配。
    """
    if not texts:
        return None
    # 尝试按 snippet 片段匹配
    if snippet:
        target = snippet.strip()
        # snippet 可能被截断，用前 30 字匹配
        probe = target[:30]
        for i, t in enumerate(texts):
            if probe and probe in t:
                pos = t.find(value)
                if pos != -1:
                    return i, pos
    # 退而求其次：按 value 匹配
    for i, t in enumerate(texts):
        pos = t.find(value)
        if pos != -1:
            return i, pos
    return None


def recognize(path: str, llm_cfg: Optional[dict] = None) -> dict:
    """解析报告并给图片/数字打上 replace / keep / review 结论。

    两轮筛选流程：
      第一轮 — 关键词打分：classify_number / classify_image 基于关键词和文本
              长度给出初步 verdict（replace / keep / review）。
      第二轮 — LLM 完整性校验（llm_classifier.verify_and_complete）：
              1. 检查第一轮提取结果是否漏掉动态值（missed）
              2. 检查是否有静态值被误判为动态（wrong）
              3. 对待确认项（review）给出最终判定
              4. 识别季度/时间表述并建议动态化（text_replacements）
              若全部正确，LLM 只回复"是"。
      第二轮不可用（无 API Key / 调用失败）时，自动降级为文本长度启发式。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        parsed = parse_docx(path)
    elif ext == ".pdf":
        parsed = parse_pdf(path)
    else:
        raise ValueError(f"仅支持 .docx / .pdf 报告，收到: {path}")

    # 第一轮：关键词打分
    for img in parsed["images"]:
        info = classify_image(
            img["caption"], img.get("w_in", 0), img.get("h_in", 0),
            img.get("in_header_footer", False),
        )
        img.update(info)
        img["_image_index"] = img.get("index", 0)

    for n in parsed["numbers"]:
        # 占位符规范化：统一为 {{stats.<metric>.<stat>}} 标准
        ph = n.get("placeholder")
        if ph and not re.match(r"^stats\.[a-zA-Z_]+\.(max|min|avg|median|std|range)$", ph):
            n["placeholder"] = None

    # 第二轮：LLM 完整性校验
    classifier = LLMClassifier(llm_cfg)

    review_numbers = [n for n in parsed["numbers"] if n["verdict"] == "review"]
    review_images = [img for img in parsed["images"] if img["verdict"] == "review"]

    # review 项过多时只把最模糊的（置信度接近 0.5）发给 LLM，其余本地启发式兜底
    MAX_LLM_REVIEW = 120
    review_numbers.sort(key=lambda n: abs(n.get("confidence", 0.5) - 0.5))
    llm_batch_reviews = review_numbers[:MAX_LLM_REVIEW]
    heuristic_rest = review_numbers[MAX_LLM_REVIEW:]
    for i, n in enumerate(llm_batch_reviews):
        n["_review_index"] = i
    for i, img in enumerate(review_images):
        img["_review_index"] = i

    extracted = [
        {
            "value": n.get("value", ""),
            "snippet": n.get("snippet", ""),
            "placeholder": n.get("placeholder") or "",
        }
        for n in parsed["numbers"]
        if n["verdict"] == "replace"
    ]

    doc_text = _build_doc_text(parsed)
    llm_result = classifier.verify_and_complete(
        doc_text=doc_text,
        extracted=extracted,
        review_items=llm_batch_reviews,
        images=parsed["images"],
        review_images=review_images,
    )

    # 2a-1. 应用 LLM 对 review 数字的判定
    for n in llm_batch_reviews:
        idx = n.pop("_review_index")
        dec = llm_result["review_decisions"].get(idx)
        if dec and dec["verdict"] != "review":
            n["verdict"] = dec["verdict"]
            n["confidence"] = dec.get("confidence", 0.85)
            n["reasons"].append(dec.get("reason", "LLM 判定"))
            if dec["verdict"] == "replace":
                n["placeholder"] = dec.get("placeholder") or suggest_placeholder(
                    (n.get("snippet") or "")[:12], (n.get("snippet") or "")[12:16]
                )

    # 2a-2. 未发送给 LLM 的 review 项：文本长度启发式兜底
    for n in heuristic_rest:
        r = _text_length_heuristic(n)
        if r["verdict"] != "review":
            n["verdict"] = r["verdict"]
            n["confidence"] = r["confidence"]
            n["reasons"].append(r["reason"])
            if n["verdict"] == "replace" and not n.get("placeholder"):
                n["placeholder"] = suggest_placeholder(
                    (n.get("snippet") or "")[:12], (n.get("snippet") or "")[12:16]
                )

    # 2b. 补充漏掉的动态值（LLM 找到的 missed）
    texts = parsed.get("texts") or []
    data_fallback_counter = [sum(1 for n in parsed["numbers"] if n["verdict"] == "replace")]
    for m in llm_result["missed"]:
        loc = _locate_value(m.get("value", ""), m.get("snippet", ""), texts)
        if loc is None:
            continue
        p_idx, pos = loc
        # 去重：同一位置已存在 replace 项则跳过
        if any(
            n.get("paragraph") == p_idx and n.get("position") == pos
            for n in parsed["numbers"]
        ):
            continue
        data_fallback_counter[0] += 1
        marker = m.get("placeholder") or f"{{{{data.{data_fallback_counter[0]}}}}}"
        parsed["numbers"].append({
            "verdict": "replace",
            "confidence": 0.90,
            "reasons": [f"LLM 补充识别：{m.get('reason', '')}"],
            "placeholder": marker,
            "value": m.get("value", ""),
            "snippet": m.get("snippet", ""),
            "position": pos,
            "page": 1,
            "paragraph": p_idx,
            "table": None,
        })

    # 2c. 纠正误判（LLM 认为应为 keep 的）
    wrong_values = {(w.get("value", ""), w.get("snippet", "")[:30]) for w in llm_result["wrong"]}
    for n in parsed["numbers"]:
        key = (n.get("value", ""), (n.get("snippet") or "")[:30])
        if n["verdict"] == "replace" and key in wrong_values:
            n["verdict"] = "keep"
            n["confidence"] = 0.85
            n["reasons"].append("LLM 判定为静态值，改回保留")
            n["placeholder"] = None

    # 2d. 应用图片 review 判定与误判纠正
    for img in review_images:
        idx = img.pop("_review_index")
        v = llm_result["images_review"].get(idx)
        if v in ("replace", "keep"):
            img["verdict"] = v
            img["confidence"] = 0.85
            img["reasons"].append("LLM 判定")
    for img in parsed["images"]:
        if img["verdict"] == "replace" and img["_image_index"] in llm_result["images_wrong"]:
            img["verdict"] = "keep"
            img["reasons"].append("LLM 判定为固定图，改回保留")
    for img in parsed["images"]:
        img.pop("_image_index", None)

    summary = {
        "images": {
            "replace": sum(1 for i in parsed["images"] if i["verdict"] == "replace"),
            "keep": sum(1 for i in parsed["images"] if i["verdict"] == "keep"),
            "review": sum(1 for i in parsed["images"] if i["verdict"] == "review"),
        },
        "numbers": {
            "replace": sum(1 for n in parsed["numbers"] if n["verdict"] == "replace"),
            "keep": sum(1 for n in parsed["numbers"] if n["verdict"] == "keep"),
            "review": sum(1 for n in parsed["numbers"] if n["verdict"] == "review"),
        },
        "chart_texts": len(parsed.get("chart_texts", [])),
        "llm": {
            "enabled": classifier.available(),
            "complete": llm_result.get("complete", False),
            "missed": len(llm_result.get("missed", [])),
            "wrong": len(llm_result.get("wrong", [])),
            "text_replacements": len(llm_result.get("text_replacements", [])),
        },
    }
    return {
        "source": os.path.abspath(path),
        "type": ext.lstrip("."),
        "parsed_at": dt.datetime.now().isoformat(timespec="seconds"),
        "summary": summary,
        "images": parsed["images"],
        "numbers": parsed["numbers"],
        "chart_texts": parsed.get("chart_texts", []),
        "text_replacements": llm_result.get("text_replacements", []),
        "texts": parsed.get("texts", []),
    }


def save_analysis(analysis: dict, out_path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2)
    return out_path


def print_summary(analysis: dict) -> None:
    print("=" * 64)
    print("报告解析识别结果")
    print("=" * 64)
    print(f"来源: {analysis['source']}（{analysis['type']}）")
    s = analysis["summary"]
    llm_info = s.get("llm", {})
    if llm_info:
        status = "已启用" if llm_info.get("enabled") else "不可用（降级）"
        complete = "全部正确（回复'是'）" if llm_info.get("complete") else "有补充修改"
        print(
            f"LLM 二次筛选: {status} | {complete} | "
            f"补漏 {llm_info.get('missed', 0)} | 纠错 {llm_info.get('wrong', 0)} | "
            f"文本替换 {llm_info.get('text_replacements', 0)}"
        )
    print(f"\n图片: 建议替换 {s['images']['replace']} / 保留 {s['images']['keep']} / 待确认 {s['images']['review']}")
    if s.get("chart_texts"):
        print(f"图表文本占位: {s['chart_texts']} 处（_time_series/_histogram 行，将替换为 {{chart.*}}）")
    for i in analysis["images"]:
        cap = i.get("caption") or "（无图题）"
        mark = {"replace": "[替换]", "keep": "[保留]", "review": "[待确认]"}[i["verdict"]]
        print(f"  {mark} 图#{i['index']} 页{i['page']} {i.get('w_in', 0):.1f}x{i.get('h_in', 0):.1f}in  {cap}")

    print(f"\n数字: 建议替换 {s['numbers']['replace']} / 保留 {s['numbers']['keep']} / 待确认 {s['numbers']['review']}")
    shown = 0
    for n in analysis["numbers"]:
        if n["verdict"] != "replace":
            continue
        ph = f"  ->  {n['placeholder']}" if n.get("placeholder") else ""
        print(f"  [替换] {n['value']:>8}  置信 {n['confidence']:.2f}{ph}")
        print(f"          {n['snippet']}")
        shown += 1
        if shown >= 15:
            print("  ... 其余替换项见 JSON")
            break
    kept = [n for n in analysis["numbers"] if n["verdict"] == "keep"]
    if kept:
        print(f"\n保留的固定数值示例（前 8 条）:")
        for n in kept[:8]:
            print(f"  [保留] {n['value']:>8}  {n['snippet']}")
    print(f"\n完整结果: 请查看输出的 JSON 文件")


# ---------------------------------------------------------------------------
# DOCX 标注草稿：动态项 -> 占位符
# ---------------------------------------------------------------------------

def _suggest_chart_id(caption: str, fallback: str) -> str:
    caption = caption or ""
    for kw, cid in [
        ("直方", "histogram"),
        ("柱状", "daily_bars"),
        ("箱线", "boxplot"),
        ("折线", "trend"),
        ("曲线", "trend"),
        ("趋势", "trend"),
        ("分布", "histogram"),
        ("对比", "daily_bars"),
    ]:
        if kw in caption:
            return cid
    return fallback


def annotate_docx(src: str, dst: str, llm_cfg: Optional[dict] = None,
                  analysis: Optional[dict] = None) -> dict:
    """把识别出的动态项改写为占位符，生成标注草稿（仅 DOCX）。

    - 动态数字 -> {{stats.<指标>.<统计>}}（无法映射时 {{data.N}}）
    - 动态图片 -> 该段替换为 {{chart.<图表ID>}}（按图题关键词推测，可手动改名）
    - 图表文本占位（xxx_time_series / xxx_histogram 行）-> {{chart.<ID>}}
    - 季度/时间表述 -> {{date.period_label}} / {{date.period_label_cn}}
    - 固定数字 / 固定图片（CAD 图等）原样保留

    llm_cfg: LLM 配置字典，传入则启用 LLM 二次筛选
    analysis: 可传入已计算好的识别结果，避免重复调用 LLM（内部会重新 recognize）
    """
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    from .report_builder import iter_block_items

    if analysis is None:
        analysis = recognize(src, llm_cfg=llm_cfg)
    doc = Document(src)

    num_targets = {}   # paragraph index -> {pos: marker}
    data_counter = [0]
    data_values = {}   # data.N -> original value (for fallback resolution)
    for n in analysis["numbers"]:
        if n["verdict"] != "replace":
            continue
        marker = n.get("placeholder")
        if marker is None:
            data_counter[0] += 1
            data_key = f"data.{data_counter[0]}"
            marker = f"{{{{data.{data_counter[0]}}}}}"
            # 保存原始值，供 report_builder 回填
            data_values[data_key] = n.get("value", "")
        elif not marker.startswith("{{"):
            # 统一补花括号（suggest_placeholder 返回无花括号的 key）
            marker = f"{{{{{marker}}}}}"
        num_targets.setdefault(n["paragraph"], {})[n["position"]] = marker

    # 将 data_values 存入 analysis，供后续 report_builder 使用
    analysis["data_values"] = data_values

    img_targets = {}   # paragraph index -> [(image_index, marker)]
    for im in analysis["images"]:
        if im["verdict"] != "replace":
            continue
        fallback = f"chart_{im['index']}"
        marker = f"{{{{chart.{_suggest_chart_id(im.get('caption'), fallback)}}}}}"
        img_targets.setdefault(im["paragraph"], []).append((im["index"], marker))

    # 图表文本占位：xxx_time_series / xxx_histogram 行 / 纯图题 -> {{chart.<id>}}
    # 表格单元格引用：H(行,列) / M(行,列) / J(行,列) -> {{cell.<metric>.<column>.<stat>}}
    chart_text_targets = {}   # paragraph index -> marker
    chart_text_counter = {}
    cell_ref_targets = {}     # paragraph index -> {pos: marker}
    # bare_caption 图题文本段（仅做识别提示，不替换为占位符；由 build_report 阶段补上编号图注）
    bare_caption_paras: set = set()

    # “编号(特征)_图型”行（如 184(xJsd)_时程曲线）：
    # 直接给出 传感器编号 + 特征 + 图型，识别为精确图表占位符
    CHART_LINE_RE = re.compile(
        r"^\s*(\d{1,5})\s*\(([^()]+)\)\s*_(时程曲线|频率分布直方图|时间序列图|时间序列|直方图|时程|曲线)\s*$"
    )
    chart_line_entries = []    # 额外写入 analysis.chart_texts 的条目
    cell_ref_columns_map = {
        # 表格列 → 统计类型
        # H 表 (索夹)：列2=avg, 列3=max, 列4=min, 列5=abs_max, 列6=rms, 列7=range
        "H": {2: "avg", 3: "max", 4: "min", 5: "abs_max", 6: "rms", 7: "range"},
        # M 表 (转角)：列2=avg, 列3=max, 列4=min, 列5=abs_max, 列6=rms, 列7=range
        "M": {2: "avg", 3: "max", 4: "min", 5: "abs_max", 6: "rms", 7: "range"},
        # J 表 (风速)：列2=avg, 列3=max, 列4=min, 列5=abs_max, 列6=rms, 列7=range
        # 注意：实际文档中 J 表无列5，但保持一致以便扩展
        "J": {2: "avg", 3: "max", 4: "min", 5: "abs_max", 6: "rms", 7: "range"},
    }
    # 行 → 测点列名（根据用户实际表格：第 2 行起为数据行）
    cable_clamp_row_to_col = {
        2: "h_87L_1", 3: "h_87R_1", 4: "h_88L_1", 5: "h_88R_1",
    }
    # 索夹第 2 组（如有）
    cable_clamp_row2_to_col = {
        6: "h_87L_2", 7: "h_87R_2", 8: "h_88L_2", 9: "h_88R_2",
    }
    rotation_row_to_col = {
        2: "m_junshan_x", 3: "m_junshan_y", 4: "m_yueyang_x", 5: "m_yueyang_y",
    }
    # J 表（风速）按列号取：col 2=avg, 3=max, 4=min, 6=rms, 7=range
    # 行号 2-5 对应：君山塔顶、岳阳塔顶、1/2 上游、1/2 下游
    wind_row_to_col = {
        2: "j_junshan_top", 3: "j_yueyang_top",
        4: "j_half_upstream", 5: "j_half_downstream",
    }

    # 通用行映射：表格第 N 行（从2开始） → 第 N-1 个测点列
    metric_point_map = {
        "cable_clamp": {**cable_clamp_row_to_col, **cable_clamp_row2_to_col},
        "rotation": rotation_row_to_col,
        "wind_speed": wind_row_to_col,
    }

    for ct in analysis.get("chart_texts", []):
        if ct.get("source") == "cell_ref":
            # 静态表的 cell_ref 不替换（保持原值）
            if ct.get("verdict") == "keep":
                continue
            # 表格单元格引用占位符
            metric = ct.get("metric", "")
            row = ct.get("row", 0)
            col = ct.get("col", 0)
            table_letter = ct.get("table_letter", "")
            # 优先用识别阶段记录的 row_label / col_header（更稳定）
            row_label = ct.get("row_label", "")
            col_header = ct.get("col_header", "")
            row_slug = slugify_label(row_label) or f"r{row}"
            col_slug = slugify_label(col_header) or f"c{col}"
            # 列 → stat：用列头优先，再用 letter 映射兜底
            stat = stat_from_col_header(col_header)
            if stat == "value" and col_header == "":
                stat_map = cell_ref_columns_map.get(table_letter, {})
                stat = stat_map.get(col, f"col{col}")
            # 行 → 测点列名：先用识别阶段的 row_slug，再用 letter 模板映射
            row_map = metric_point_map.get(metric, {})
            column_from_map = row_map.get(row)
            if column_from_map and not row_label:
                column = column_from_map
            elif column_from_map and row_slug in column_from_map:
                column = column_from_map
            else:
                column = row_slug or column_from_map or f"unknown_{metric}_r{row}"
            marker = f"{{{{cell.{metric}.{column}.{stat}}}}}"
            cell_ref_targets.setdefault(ct["paragraph"], {})[ct.get("position", 0)] = marker
        else:
            # 图表文本占位符
            # bare_caption 是图题文本（如“跨中断面环境温度时程曲线图”），源头 DOCX
            # 在 _time_series / _histogram 占位符之后紧接着就是图题段。annotate_docx
            # 不应再为图题段生成 {{chart.*}} 占位符——否则 build_report 会重复插入
            # 同一张图，导致图片覆盖文字。
            if ct.get("source") == "bare_caption":
                bare_caption_paras.add(ct["paragraph"])
                continue
            metric = ct.get("metric", "chart")
            cid = ct.get("chart_id", "trend")
            chart_text_counter[metric] = chart_text_counter.get(metric, 0) + 1
            unique_cid = f"{metric}_{cid}_{chart_text_counter[metric]}"
            chart_text_targets[ct["paragraph"]] = f"{{{{chart.{unique_cid}}}}}"
            ct["_unique_chart_id"] = unique_cid  # 供运行时 agent 引用

    # 扫描“编号(特征)_图型”行（在 texts 里查找，替换为精确图表占位符）
    for pidx, t in enumerate(analysis.get("texts", []) or []):
        m = CHART_LINE_RE.match(str(t))
        if not m:
            continue
        sensor_id, feature_raw, chart_word = m.group(1), m.group(2), m.group(3)
        kind = "histogram" if "直方图" in chart_word or "直方" in chart_word else "trend"
        cid = f"chart_sensor_{sensor_id}_{kind}"
        chart_text_targets[pidx] = f"{{{{chart.{cid}}}}}"
        analysis.setdefault("chart_texts", []).append({
            "paragraph": pidx,
            "kind": "histogram" if kind == "histogram" else "time_series",
            "chart_id": "trend" if kind == "trend" else "histogram",
            "text": str(t),
            "source": "sensor_line",
            "_unique_chart_id": cid,
            "sensor_id": sensor_id,
            "feature": feature_raw,
        })
        chart_line_entries.append(cid)
    if chart_line_entries:
        log.info("识别 %d 行“编号(特征)_图型”精确图表占位符", len(chart_line_entries))

    # 文本级替换（季度/时间表述动态化）
    text_targets = {}   # paragraph index -> [(original, replacement)]
    # 默认替换规则（不依赖 LLM，始终应用）
    default_text_replacements = [
        ("XXXX年第X季度（xx月-xx月）", "{{date.period_label_cn}}（{{date.period_label}}）"),
        ("XXXX年第X季度", "{{date.period_label_cn}}"),
        # 落款日期（封面/签字页）
        ("xxxx年xx月xx日", "{{date.signature}}"),
        ("XXXX年XX月XX日", "{{date.signature}}"),
        ("xxxx年XX月XX日", "{{date.signature}}"),
        ("XXXX年xx月xx日", "{{date.signature}}"),
        # 结论段报告期范围
        ("xx月-xx月", "{{date.period_start_month}}-{{date.period_end_month}}"),
    ]
    all_text_replacements = default_text_replacements + list(
        analysis.get("text_replacements", [])
    )
    for tr in all_text_replacements:
        if isinstance(tr, dict):
            original = tr.get("original", "")
            replacement = tr.get("replacement", "")
        else:
            original, replacement = tr
        if not original or not replacement:
            continue
        for i, t in enumerate(analysis.get("texts", []) or []):
            if original in t:
                text_targets.setdefault(i, []).append((original, replacement))

    replaced_numbers = 0
    skipped_numbers = 0
    replaced_images = 0
    replaced_texts = 0
    replaced_chart_texts = 0
    replaced_cell_refs = 0

    def process_paragraph(p, idx):
        nonlocal replaced_numbers, skipped_numbers, replaced_images, replaced_texts, replaced_chart_texts, replaced_cell_refs
        # bare_caption 图题段：原 caption 文本会被 build_report 的 auto-caption 取代，
        # 清空段落文本，避免重复 caption
        if idx in bare_caption_paras:
            _clear_paragraph_text(p)
            return
        # 图表文本占位优先（整段替换为 {{chart.<id>}}）
        if idx in chart_text_targets:
            _replace_whole_paragraph(p, chart_text_targets[idx])
            replaced_chart_texts += 1
            return
        # 表格单元格引用占位符（H(行,列) 等）→ {{cell.xxx}}，按位置替换
        if idx in cell_ref_targets:
            n_ok, n_skip = _replace_cell_refs_in_paragraph(p, cell_ref_targets[idx])
            replaced_cell_refs += n_ok
            skipped_numbers += n_skip
        targets = num_targets.get(idx, {})
        if targets:
            full = "".join(r.text for r in p.runs)
            # 位置名里的数字（第7跨L3/4断面、第五跨L/4处主梁等）不替换
            targets = _filter_location_numbers(full, targets)
            n_ok, n_skip = _replace_numbers_in_paragraph(p, targets)
            replaced_numbers += n_ok
            skipped_numbers += n_skip
        if img_targets.get(idx):
            _replace_image_paragraph(p, img_targets[idx][0][1])
            replaced_images += 1
        if text_targets.get(idx):
            for original, replacement in text_targets[idx]:
                if _replace_text_in_paragraph(p, original, replacement):
                    replaced_texts += 1

    idx = 0
    for item in iter_block_items(doc):
        if isinstance(item, Paragraph):
            process_paragraph(item, idx)
            idx += 1
        elif isinstance(item, Table):
            for row in item.rows:
                for cell in row.cells:
                    for p in cell.paragraphs:
                        process_paragraph(p, idx)
                        idx += 1
    for section in doc.sections:
        for p in section.header.paragraphs:
            process_paragraph(p, idx)
            idx += 1
        for p in section.footer.paragraphs:
            process_paragraph(p, idx)
            idx += 1

    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
    doc.save(dst)
    return {
        "output": os.path.abspath(dst),
        "replaced_numbers": replaced_numbers,
        "skipped_numbers_split_runs": skipped_numbers,
        "replaced_images": replaced_images,
        "replaced_texts": replaced_texts,
        "replaced_chart_texts": replaced_chart_texts,
        "replaced_cell_refs": replaced_cell_refs,
        "llm_summary": analysis["summary"].get("llm", {}),
    }


def _replace_whole_paragraph(p, marker: str) -> None:
    """把段落整体替换为占位符文本。"""
    runs = p.runs
    if runs:
        runs[0].text = marker
        for r in runs[1:]:
            r.text = ""
    else:
        p.add_run(marker)


def _clear_paragraph_text(p) -> None:
    """清空段落所有 run 的文本，保留段落本身用于间距。"""
    for r in p.runs:
        r.text = ""


def _replace_text_in_paragraph(p, original: str, replacement: str) -> bool:
    """在段落中按文本片段替换（合并 run，保留首个 run 格式）。

    返回是否替换成功。
    """
    runs = p.runs
    if not runs:
        return False
    full = "".join(r.text for r in runs)
    if original not in full:
        return False
    new_text = full.replace(original, replacement)
    runs[0].text = new_text
    for r in runs[1:]:
        r.text = ""
    return True


def _replace_numbers_in_paragraph(p, targets: dict):
    """在段落里按全局位置替换数字。

    当数字跨多个 run 时（如 '3' + '9.93' 分属不同 run），
    先合并所有 run 再替换，避免跨 run 匹配被跳过。
    """
    runs = p.runs
    if not runs:
        return 0, 0
    full = "".join(r.text for r in runs)
    offsets = []
    cur = 0
    for r in runs:
        offsets.append(cur)
        cur += len(r.text)

    hits = []
    for m in NUMBER_RE.finditer(full):
        if m.start() in targets:
            hits.append((m.start(), m.end(), targets[m.start()]))
    if not hits:
        return 0, 0

    # 检查是否有匹配跨越多个 run
    needs_merge = False
    for start, end, marker in hits:
        ri = max(i for i, off in enumerate(offsets) if off <= start)
        run_end = offsets[ri] + len(runs[ri].text)
        if end > run_end:
            needs_merge = True
            break

    if needs_merge:
        # 合并所有 run 到第一个 run，保留首个 run 的格式
        runs[0].text = full
        for r in runs[1:]:
            r.text = ""
        offsets = [0]
        runs = [runs[0]]

    ok = 0
    skip = 0
    for start, end, marker in sorted(hits, key=lambda x: -x[0]):
        ri = max(i for i, off in enumerate(offsets) if off <= start)
        run_end = offsets[ri] + len(runs[ri].text)
        if end > run_end:
            skip += 1
            continue
        rel = start - offsets[ri]
        runs[ri].text = runs[ri].text[:rel] + marker + runs[ri].text[rel + (end - start):]
        ok += 1
    return ok, skip


# 位置名中的数字（第7跨L3/4断面、第五跨L/4处主梁、4#墩 等）应视为静态
LOCATION_SPAN_RE = re.compile(
    r"第\s*\d+\s*跨|L\s*\d+\s*/\s*\d+\s*(?:断面|处)|L\s*/\s*\d+\s*(?:断面|处)|"
    r"\d+\s*L\s*/\s*\d+\s*(?:断面|处)|"
    r"第\s*\d+\s*跨\s*跨中|\d+\s*#\s*(?:塔|墩)|\d+\s*号\s*塔|第五跨L/\d+处"
)


def _filter_location_numbers(full_text: str, targets: dict) -> dict:
    """把位置名里的数字从替换目标中剔除（不替换为 data.N 占位符）。"""
    if not targets:
        return targets
    protected = [(m.start(), m.end()) for m in LOCATION_SPAN_RE.finditer(full_text)]
    if not protected:
        return targets
    return {pos: marker for pos, marker in targets.items()
            if not any(s <= pos < e for s, e in protected)}


def _replace_cell_refs_in_paragraph(p, targets: dict):
    """在段落里按全局位置替换 H(行,列) / M(行,列) / J(行,列) 单元格引用占位符。

    targets: {start_pos: marker} 字典

    当 cell_ref 文本跨多个 run 时（如 'J' + '(2,2)' 分属不同 run），
    先合并所有 run 再替换，避免跨 run 匹配被跳过。
    """
    runs = p.runs
    if not runs:
        return 0, 0
    full = "".join(r.text for r in runs)

    # 在全文里找所有 cell_ref 引用，按位置匹配 targets
    matches = []
    for m in CELL_REF_RE.finditer(full):
        if m.start() in targets:
            matches.append((m.start(), m.end(), targets[m.start()]))
    if not matches:
        return 0, 0

    # 计算 run 偏移表
    offsets = []
    cur = 0
    for r in runs:
        offsets.append(cur)
        cur += len(r.text)

    # 检查是否有匹配跨越多个 run
    needs_merge = False
    for start, end, marker in matches:
        ri = max(i for i, off in enumerate(offsets) if off <= start)
        run_end = offsets[ri] + len(runs[ri].text)
        if end > run_end:
            needs_merge = True
            break

    if needs_merge:
        # 合并所有 run 到第一个 run，保留首个 run 的格式
        runs[0].text = full
        for r in runs[1:]:
            r.text = ""
        # 重置为单 run 模式
        offsets = [0]
        runs = [runs[0]]

    # 从右向左替换（保持左侧位置不变）
    ok = 0
    skip = 0
    for start, end, marker in sorted(matches, key=lambda x: -x[0]):
        ri = max(i for i, off in enumerate(offsets) if off <= start)
        run_end = offsets[ri] + len(runs[ri].text)
        if end > run_end:
            skip += 1
            continue
        rel = start - offsets[ri]
        runs[ri].text = runs[ri].text[:rel] + marker + runs[ri].text[rel + (end - start):]
        ok += 1
    return ok, skip


def _replace_image_paragraph(p, marker: str) -> None:
    """把段落里的图片元素移除，并将段落文本替换为图表占位符。"""
    from docx.oxml.ns import qn

    for tag in ("w:drawing", "w:pict", "w:object"):
        for node in p._p.findall(".//" + qn(tag)):
            node.getparent().remove(node)
    runs = p.runs
    for r in runs[1:]:
        r.text = ""
    if runs:
        runs[0].text = marker
    else:
        p.add_run(marker)
