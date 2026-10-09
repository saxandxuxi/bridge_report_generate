# -*- coding: utf-8 -*-
"""真实监测数据适配器：直接读取“桥数据预处理”产出的统计值 JSON 与图库图片。

桥数据预处理项目（D:/Code/桥数据预处理/）会产出：
  统计值/<传感器编号>.json        每个传感器的分特征统计（中文键）
  统计值/总览.json                全部传感器-特征总览
  统计值_<期>/<桥名>/位置统计/<位置>.json
                              位置统计库：{位置: {测点X: {特征: {统计,
                              传感器编号}}}}（与图库位置目录一致）
  统计值_<期>/<桥名>/季度总结/     季度/年度聚合统计（季度统计.json、年度统计.json）
  传感器对照/传感器编号名称.json    编号 -> 中文监测部位对照（固定产物）
  传感器对照/传感器名称对照/<桥名>.json  中文名称 -> 编号/特征（固定产物）
  图库/<传感器编号>/<特征>/时间序列图.png / 频率分布图.png / 相关性_*.png

本模块把这些产物变成报告生成引擎的“数据源”：
  - resolve_cell()        解析 {{cell.<指标>.<测点>.<统计量>}} 占位符
  - resolve_metric_stat() 解析 {{stats.<指标>.<统计量>}} 占位符
  - resolve_chart()       把 {{chart.<ID>}} 映射到图库中的真实图片
  - coverage()            给 Web 管理台提供数据覆盖度 / 待补清单

测点 -> 传感器编号的匹配顺序：
  1. 配置中的 sensor_aliases 精确映射
  2. 编号（纯数字）直接命中
  3. 与“名称 / 监测部位”全等匹配
  4. 模糊包含匹配（长度加权相似度 >= fuzzy_threshold）
  5. 匹配不到 -> 回退到该指标全传感器聚合值
"""

import datetime as dt
import json
import logging
import math
import os
import re
import statistics
import difflib
from typing import Dict, List, Optional

from report_agent.config import resolve_bridge_subdir

# 方位词（长的在前，避免“上游侧”被“上游”先吃掉；含裸“左/右”前缀，
# 统一规范成 L/R/U/D，使“左跨中1/2截面”≈“跨中左幅1/2截面”）
_SIDE_RE = re.compile(r"(上游侧|下游侧|左幅|右幅|左侧|右侧|上游|下游|左|右)")

# 桥端/地点侧词（岳阳/君山/湘潭/随州/炎陵/汝城/矮寨/吉首/茶洞/赤石 等）：
# 在位置名中可能前后调换（“岳阳侧伸缩缝处”==“伸缩缝处岳阳侧”），
# 匹配时从主体剥离、单独比较地点集合。
_SITE_WORDS = (
    "岳阳侧", "君山侧", "湘潭侧", "随州侧", "炎陵侧", "汝城侧",
    "矮寨侧", "吉首侧", "茶洞侧", "赤石侧",
    "岳阳", "君山", "湘潭", "随州", "炎陵", "汝城",
    "矮寨", "吉首", "茶洞", "赤石",
)
_SITE_CODE = {
    "岳阳": "YY", "君山": "JS", "湘潭": "XT", "随州": "SZ",
    "炎陵": "YL", "汝城": "RC", "矮寨": "AZ", "吉首": "JSH",
    "茶洞": "CD", "赤石": "CS",
}
_SITE_RE = re.compile("|".join(
    re.escape(w) for w in sorted(_SITE_WORDS, key=len, reverse=True)))


def _site_set(s: str) -> set:
    """返回位置名里的桥端地点码集合（岳阳=YY、君山=JS…）。"""
    codes = set()
    for w in _SITE_RE.findall(str(s or "")):
        for k, code in _SITE_CODE.items():
            if k in w:
                codes.add(code)
                break
    return codes


def _strip_sites(s: str) -> str:
    """剥掉桥端地点词，仅保留主体（“伸缩缝处岳阳侧”->“伸缩缝处”）。"""
    return _SITE_RE.sub("", str(s or ""))


def _side_set(s: str) -> set:
    """返回位置名里的方位码集合：左类=L、右类=R、上游=U、下游=D。"""
    codes = set()
    for tok in _SIDE_RE.findall(str(s or "")):
        if tok in ("左", "左幅", "左侧"):
            codes.add("L")
        elif tok in ("右", "右幅", "右侧"):
            codes.add("R")
        elif tok in ("上游", "上游侧"):
            codes.add("U")
        elif tok in ("下游", "下游侧"):
            codes.add("D")
    return codes


def _fmt_range_readable(s: str) -> str:
    """把紧凑缺失时间段(4.1 0~4.8 14)转成可读形式(4.1日0点至4.8日14点)，
    用于总结段落。已是可读形式/无法解析时原样返回。
    兼容同月缩写与跨年带年份(2026.1.18 3~2027.1.19 5)两种存储格式。
    """
    t = str(s or "").strip()
    if not t or ("日" in t and "点" in t):
        return t
    m = re.match(
        r"^(?P<y1>\d{4})\.(?P<m1>\d{1,2})\.(?P<d1>\d{1,2}) "
        r"(?P<h1>\d{1,2})[~～\-—–]"
        r"(?P<y2>\d{4})\.(?P<m2>\d{1,2})\.(?P<d2>\d{1,2}) "
        r"(?P<h2>\d{1,2})$", t)
    if m:
        return (f"{m.group('y1')}.{int(m.group('m1'))}.{int(m.group('d1'))}"
                f"日{int(m.group('h1'))}点至"
                f"{m.group('y2')}.{int(m.group('m2'))}.{int(m.group('d2'))}"
                f"日{int(m.group('h2'))}点")
    m = re.match(
        r"^(?P<m1>\d{1,2})\.(?P<d1>\d{1,2}) (?P<h1>\d{1,2})"
        r"[~～\-—–]"
        r"(?P<m2>\d{1,2})\.(?P<d2>\d{1,2}) (?P<h2>\d{1,2})$", t)
    if m:
        return (f"{int(m.group('m1'))}.{int(m.group('d1'))}"
                f"日{int(m.group('h1'))}点至"
                f"{int(m.group('m2'))}.{int(m.group('d2'))}"
                f"日{int(m.group('h2'))}点")
    return t


log = logging.getLogger("report-agent.bridge")


def _bridge_name_match(a, b):
    """桥名兼容匹配：洣水河特大桥 <-> 洣水河、矮寨大桥 <-> 矮寨 都算同一桥。
    传感器对照表的桥名来自原始文档（可能不带“大桥/特大桥”），
    配置 bridge_name 用全称，二者必须兼容匹配，否则传感器会被全部过滤。"""
    if not a or not b:
        return False

    def _strip(x):
        x = str(x)
        for s in ("特大桥", "大桥"):
            if x.endswith(s):
                return x[: -len(s)]
        return x

    x, y = _strip(a), _strip(b)
    return x in y or y in x


# ---------------------------------------------------------------------------
# 常量与工具
# ---------------------------------------------------------------------------

# 模板统计量 -> 预处理统计值 JSON 的中文键
STAT_KEY_MAP = {
    "max": "最大值",
    "min": "最小值",
    "avg": "平均值",
    "mean": "平均值",
    "median": "中位数",
    "std": "标准差",
    "range": "差值",
    "abs_max": "绝对最大值",
    "rms": "均方根值",
    "value": "平均值",
    "temp_rm_max": "剔除温度最大值",
    "temp_rm_min": "剔除温度最小值",
    "temp_rm_range": "剔除温度差值",
    "corr": "相关性系数",
    "剔除温度最大值": "剔除温度最大值",
    "剔除温度最小值": "剔除温度最小值",
    "相关性系数": "相关性系数",
    "count": "覆盖天数",
    "days": "覆盖天数",
    "最大值": "最大值",
    "最小值": "最小值",
    "平均值": "平均值",
    "中位数": "中位数",
    "标准差": "标准差",
    "差值": "差值",
    "绝对最大值": "绝对最大值",
    "均方根值": "均方根值",
}

# 中文统计键 -> 英文规范键（用于 _aggregate_daily 的分支判断）
CN_STAT_MAP = {
    "最大值": "max",
    "最小值": "min",
    "平均值": "avg",
    "中位数": "median",
    "标准差": "std",
    "差值": "range",
    "绝对最大值": "abs_max",
    "均方根值": "rms",
    "覆盖天数": "days",
}

# 图表类型 -> 图库文件名（kind 归一化）
CHART_KIND_FILE = {
    "trend": "时间序列图.png",
    "timeseries": "时间序列图.png",
    "time_series": "时间序列图.png",
    "histogram": "频率分布图.png",
    "hist": "频率分布图.png",
    "bar": "时间序列图.png",
    "box": "频率分布图.png",
}


def _pick_chart_file(dirpath: str, base_name: str) -> Optional[str]:
    """取图表文件：优先 base_name；振动按天出图时退化为
    base_YYYY-MM-DD.png 中日期最新的一个(如 时间序列图_2026-03-31.png)。"""
    p = os.path.join(dirpath, base_name)
    if os.path.isfile(p):
        return p
    if os.path.isdir(dirpath):
        prefix = base_name.rsplit(".", 1)[0] + "_"
        cand = sorted(fn for fn in os.listdir(dirpath)
                      if fn.startswith(prefix) and fn.endswith(".png"))
        if cand:
            return os.path.join(dirpath, cand[-1])
    return None


# 轴/方向分量 -> 同一特征组（与 build_chart_library.feature_group 保持一致）
_AXIS_INNER = {"Δx", "Δy", "Δz", "x", "y", "z", "ax", "ay", "az"}


def _axis_inner(feature: str) -> str:
    """提取特征括号内的轴编码，如 GNSS(Δx) -> Δx；非轴特征返回空串。"""
    m = re.match(r"^[A-Za-z0-9]+\(([^)]+)\)$", str(feature or ""))
    if not m:
        return ""
    inner = m.group(1)
    if inner in _AXIS_INNER or inner.lower() in _AXIS_INNER:
        return inner
    if inner.lower().endswith(("jd", "jsd")):
        return inner
    if len(inner) >= 2 and inner[-1].lower() in ("s", "x"):
        return inner
    return ""


def _feature_code(feature: str) -> str:
    """提取特征括号内编码（小写），如 WSD(temp)/WD(temp) -> temp、
    GNSS(Δx)/WY(Δx) -> Δx；无括号取整体。用于同族回退比较。"""
    m = re.search(r"\(([^)]+)\)$", str(feature or ""))
    return (m.group(1) if m else str(feature or "")).strip().lower()


# 默认指标 -> 特征名（可在 config.bridge_data.metrics 中覆盖）
DEFAULT_METRIC_FEATURES = {
    "temperature": "WSD(temp)",
    "humidity": "WSD(rh)",
    "wind_speed": "WSD(ws)",
}


def _norm(text: str) -> str:
    """归一化名称：全角转半角、去空格、统一小写。"""
    if not text:
        return ""
    out = []
    for ch in str(text):
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out).strip().lower().replace(" ", "").replace("（", "(").replace("）", ")")


def _safe_dir(path_seg: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", str(path_seg)).strip()


def _dun_no(text: str) -> Optional[str]:
    """提取位置名里的墩号（如 “3#墩墩顶” -> "3"）；没有返回 None。"""
    # 兼容 “3#墩墩顶” 与 “3#柱墩墩底” 两种写法（柱墩之间无空格）。
    m = re.search(r"(\d+)\s*#\s*(?:柱)?墩", str(text or ""))
    return m.group(1) if m else None


def _position_similarity(a: str, b: str) -> float:
    """位置名相似度(0~1)：归一化后按公共子序列/字符重合度评估。

    用于模板占位符位置与名称对照表/图库目录名的模糊匹配，
    容忍“内/侧/梁”等修饰字差异和词序不同(如
    “上游随州侧边跨跨中箱梁顶板” vs “随州侧边跨跨中箱梁内顶板上游”)。
    硬约束：两边都含墩号且墩号不同（3#墩 vs 2#墩）直接判 0，
    避免图/表/统计在相邻墩之间张冠李戴。
    """
    da, db = _dun_no(a), _dun_no(b)
    if da is not None and db is not None and da != db:
        return 0.0
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    try:
        from difflib import SequenceMatcher
    except Exception:  # noqa: BLE001
        return 1.0 if (na in nb or nb in na) else 0.0
    sm = SequenceMatcher(None, na, nb)
    ratio = sm.ratio()
    # 公共子序列覆盖度：保证长位置的主要词序一致
    lcs = sum(blk.size for blk in sm.get_matching_blocks())
    # 覆盖度 = 公共子序列长度 / 较长串长度（0~1）。不能用 2*，否则近似
    # 匹配（如 “59#墩墩顶截面上游”）会超过精确匹配的 1.0，把精确位置挤掉。
    cover = float(lcs) / max(len(na), len(nb))
    score = max(ratio, cover)
    # 端侧词词序不同（塔梁交接处岳阳侧 vs 岳阳侧塔梁交接处支座）时，
    # SequenceMatcher 把端侧词当主体的一部分打低分；先剥掉端侧/方位词
    # 比较主体并给同端加分（与 _match_position 的语义一致）。
    if _site_set(na) or _site_set(nb):
        _ba = _strip_sites(_SIDE_RE.sub("", na))
        _bb = _strip_sites(_SIDE_RE.sub("", nb))
        _sa, _sb = _site_set(na), _site_set(nb)
        if _ba and _bb and _sa == _sb and _sa:
            _body_r = SequenceMatcher(None, _ba, _bb).ratio()
            score = max(score, min(1.0, _body_r + 0.12))
    return score


_LOC_METRIC_SUFFIXES = (
    "空间变位", "位移", "变位", "挠度", "应变", "倾角", "振动", "地震",
    "结构温度", "环境温度", "环境湿度", "温度", "湿度", "风速", "风向",
    "索力", "裂缝", "监测", "统计", "时程曲线图", "频率分布直方图",
    "曲线图", "直方图",
)


def _strip_loc_metric_suffix(loc: str) -> str:
    """去掉位置关键词末尾的指标/图型词（如 “3#墩承台空间变位” -> “3#墩承台”）。

    表格行标签常把指标名拼进监测部位（“3#墩承台空间变位”），而名称对照/
    图库目录名是纯位置（“3#墩承台”），匹配前先剥掉后缀。
    """
    t = str(loc or "")
    changed = True
    while changed:
        changed = False
        for w in _LOC_METRIC_SUFFIXES:
            if len(t) > len(w) and t.endswith(w):
                t = t[:-len(w)].rstrip(" 、，,和及")
                changed = True
                break
    return t


def _position_side_words(text: str) -> set:
    """提取位置里的方向/部位关键方位词(上游/下游/左/右/顶/底等)。"""
    t = _norm(text)
    out = set()
    for w in ("上游", "下游", "左幅", "右幅", "左侧", "右侧", "左", "右",
              "顶板", "底板", "顶", "底"):
        if w in t:
            out.add(w)
    # “上游侧/下游侧”归一为“上游/下游”，保证与图库/对照表命名一致
    for w in ("上游侧", "下游侧"):
        if w in t:
            out.add(w.replace("侧", ""))
    return out


# 字母复合单位：数字与单位之间必须恰好一个空格（6.9m/s² -> 6.9 m/s²）。
# 长的在前，避免 mm/s² 被 m/s 先吃掉；后缀排除字母/数字，防止命中 kNm 等。
_UNIT_SPACE_RE = re.compile(
    r"(?P<num>-?\d+(?:\.\d+)?)"
    r"(?P<unit>mm/s²|mm/s2|m/s²|m/s2|mm/s|m/s|km/h|kN|MPa)(?![A-Za-z0-9])")
_UNIT_TRAIL_RE = re.compile(
    r"(mm/s²|mm/s2|m/s²|m/s2|mm/s|m/s|km/h|kN|MPa) +(?=[。，；、！？!?]|$)")


def normalize_unit_spacing(text: str) -> str:
    """规范化数值与单位之间的空格：字母复合单位前补一个空格、
    单位后多余空格去掉、连续空格压成 1 个。℃/% 等符号单位保持中文习惯不拆。"""
    if not text:
        return text
    t = re.sub(r" {2,}", " ", str(text))
    t = re.sub(r"。{2,}", "。", t)   # “。。/。。。” -> “。”
    t = _UNIT_SPACE_RE.sub(
        lambda m: f"{m.group('num')} {m.group('unit')}", t)
    t = _UNIT_TRAIL_RE.sub(r"\1", t)
    return t.strip()


def format_report_number(value) -> str:
    """报告数值统一格式：
      - 0 以上：保留两位小数（如 21.57、26.08）
      - 0 以下：保留三位有效数字（如 -3.37、-15.1）
      - 量级特别小（|x| < 0.01 且非 0）：科学计数法，保留三位有效数字
        （如 2.81e-04、-1.90e-04）
      - 整数（如 0、90）原样输出
    总结段落、表格单元格统一走这里，保证口径一致。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(v):
        # NaN/Inf：数据无效（如统计值里混入的 NaN），填“—”而不是
        # int(NaN) 崩溃，保证报告流程不中断
        return "—"
    if v == 0.0:
        return "0"
    if v == int(v) and abs(v) < 1e9:
        return str(int(v))
    if abs(v) < 1e-5:
        return f"{v:.3e}"
    if abs(v) < 0.01:
        # 工程报告里 1.59e-04 可读性差：改用普通小数并保留有效数字
        return f"{v:.10f}".rstrip("0").rstrip(".")
    if v > 0:
        return f"{v:.2f}"
    return f"{v:.3g}"


def feature_group(feature: str) -> str:
    """特征编码归组：GNSS(Δx/y/z)、EZJD(xJd/yJd) 等轴分量同组；WSD(rh)/WSD(temp) 各自成组。"""
    m = re.match(r"^([A-Za-z0-9]+)\(([^)]+)\)$", feature)
    if not m:
        return feature
    prefix, inner = m.group(1), m.group(2)
    if inner in _AXIS_INNER or inner.lower() in _AXIS_INNER:
        return prefix
    if inner.lower().endswith(("jd", "jsd")):
        return prefix
    if len(inner) >= 2 and inner[-1].lower() in ("s", "x"):
        return f"{prefix}({inner[-1].lower()})"
    return feature


_AXIS_TOKEN_MAP = {
    "X方向": "x", "Y方向": "y", "Z方向": "z",
    "纵桥向": "x", "横桥向": "y", "竖向": "z",
    "纵向": "x", "横向": "y", "垂直向": "z",
    "Δx": "x", "Δy": "y", "Δz": "z",
}


def _hint_axes(unit_hint: str) -> set:
    """从表格列头提示里提取轴向集合（如 纵桥向(X方向)/横桥向(Y方向) -> {x,y}）。"""
    t = str(unit_hint or "")
    return {ax for tok, ax in _AXIS_TOKEN_MAP.items() if tok in t}


def _hint_unit(unit_hint: str) -> str:
    """从表格列头提示里提取单位（m/s²、mm、℃ 等），用于与特征组单位族对账。"""
    t = str(unit_hint or "")
    for u in ("m/s²", "m/s2", "mm", "με", "kN", "℃", "%", "°", "辆"):
        if u in t:
            return "m/s²" if u == "m/s2" else u
    return ""


def _feature_axis_set(feature: str) -> set:
    """特征串括号内的轴集合：SZJSD(xJsd) -> {'x'}、GNSS(Δx) -> {'x'}。"""
    inner = _axis_inner(feature)
    if not inner:
        return set()
    i = re.sub(r"j[ds]$", "", inner.lower())
    return {ax for ax in ("x", "y", "z") if ax in i}


def _group_unit_family(group: str) -> str:
    """特征组前缀 → 单位族（用于与表格列头单位对账，如 SZJSD/JSD -> m/s²）。"""
    g = str(group or "").upper()
    if any(w in g for w in ("JSD", "JXD", "JYD", "JZD")):
        return "m/s²"
    if g.startswith(("GNSS", "WY", "ND", "LF")):
        return "mm"
    if g == "YB":
        return "με"
    if g == "SL":
        return "kN"
    if g.startswith("EZJD"):
        return "°"
    if g.startswith("WD"):
        return "℃"
    if g.startswith("WSD"):
        return "℃"
    return ""


def _group_axis_guess(group: str) -> set:
    """目录只有特征组名（如 SZJSD）没有精确特征时，按前缀猜测轴覆盖。"""
    g = str(group or "").upper()
    if g.startswith(("SZ", "GNSS")):
        return {"x", "y", "z"}
    if g.startswith("EZ"):
        return {"x", "y"}
    if g.startswith("DZ"):
        return {"x"}
    return set()


def _similarity(a: str, b: str) -> float:
    """包含关系加权相似度，用于模糊匹配测点名称。"""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b:
        return 0.5 + 0.5 * len(a) / len(b)
    if b in a:
        return 0.5 + 0.5 * len(b) / len(a)
    return 0.0


def _canon_stat(stat: str) -> str:
    """把模板统计量统一成英文规范键。

    除精确表以外加一层“表头式”兜底：模板里常写“平均温度/最高温度/
    最低温度/最大温差”这类带物理量的列名，精确匹配不到就会一路返回 None
    被填成“—”（历史上“平均值写—、最高/最低正常”就是这么来的）。
    """
    s = str(stat or "")
    if s in CN_STAT_MAP:
        return CN_STAT_MAP[s]
    if "最大温差" in s or "最大差" in s or "差值" in s or "极差" in s:
        return "range"
    if "绝对" in s and "最大" in s:
        return "abs_max"
    if "均方根" in s:
        return "rms"
    if "中位" in s:
        return "median"
    if "标准" in s:
        return "std"
    if "覆盖" in s or "有效天数" in s:
        return "days"
    if "平均" in s or "均值" in s:
        return "avg"
    if "最高" in s or "最大" in s:
        return "max"
    if "最低" in s or "最小" in s:
        return "min"
    return s


def _fuzzy_find(query: str, candidates: List[str], threshold: float) -> Optional[str]:
    best, best_score = None, 0.0
    for cand in candidates:
        score = _similarity(query, cand)
        if score > best_score:
            best, best_score = cand, score
    if best is not None and best_score >= threshold:
        return best
    return None


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class BridgeData:
    """真实监测数据适配器。"""

    def __init__(self, cfg: Optional[Dict] = None, base_dir: str = ""):
        cfg = cfg or {}
        self.cfg = cfg
        self.base_dir = base_dir
        self.bridge_name = cfg.get("bridge_name", "")
        self.fuzzy_threshold = float(cfg.get("fuzzy_threshold", 0.7))
        self.period_aggregate = bool(cfg.get("period_aggregate", True))

        self.stats_dir = self._resolve(cfg.get("stats_dir", ""))
        self.charts_dir = self._resolve(cfg.get("charts_dir", ""))
        # 目录名可能用桥名全称/简称（洣水河特大桥 <-> 洣水河），
        # 这里再做一次模糊下钻，避免配置里的桥名写法与实际目录不一致导致加载失败
        if self.bridge_name:
            self.stats_dir = resolve_bridge_subdir(
                self.stats_dir, self.bridge_name)
            self.charts_dir = resolve_bridge_subdir(
                self.charts_dir, self.bridge_name)
        self.sensor_map_path = self._resolve(cfg.get("sensor_map", ""))
        self.overview_path = self._resolve(cfg.get("overview", ""))
        self.name_dict_path = self._resolve(cfg.get("name_dict", ""))

        # 指标 -> 特征 映射（用户可配置）
        self.metrics: Dict[str, Dict] = {}
        for name, mcfg in (cfg.get("metrics", {}) or {}).items():
            self.metrics[name] = dict(mcfg)
            self.metrics[name].setdefault("feature", DEFAULT_METRIC_FEATURES.get(name, ""))
        # 把默认指标补进去，未配置的指标也保留（feature 可能为空）
        for name, feat in DEFAULT_METRIC_FEATURES.items():
            self.metrics.setdefault(name, {"feature": feat})

        # 传感器别名：测点描述 -> 传感器编号
        self.sensor_aliases: Dict[str, str] = cfg.get("sensor_aliases", {}) or {}
        # 图表占位符 -> 传感器编号（“20% 待完善”的人工映射表）
        self.chart_map: Dict[str, str] = cfg.get("chart_map", {}) or {}
        # 排除的传感器：编号 或 名称子串（用于绕过明显异常的数据）
        self.sensor_exclude: List[str] = [str(x) for x in (cfg.get("sensor_exclude", []) or [])]
        # 指标 -> 监测类别（回退聚合时只在该类别内取传感器，避免跨类别污染）
        self.metric_category = {
            "temperature": "温湿度", "humidity": "温湿度", "structure_temperature": "结构温度",
            "wind_speed": "风荷载", "cable_force": "索力", "displacement": "空间变位",
            "deflection": "挠度", "strain": "应变", "vibration": "振动",
            "earthquake_load": "地震",
            "rotation": "倾角", "crack": "裂缝",
        }
        # 总结段落“XXX监测数据正常稳定”的指标标签
        self._status_labels = {
            "structure_temperature": "结构温度监测数据",
            "strain": "结构应变监测数据",
            "vibration": "振动监测数据",
            "displacement": "空间变位监测数据",
            "deflection": "挠度监测数据",
            "temperature": "环境温度监测数据",
            "humidity": "环境湿度监测数据",
            "earthquake_load": "地震监测数据",
            "wind_speed": "风速监测数据",
            "cable_force": "索力监测数据",
            "rotation": "倾角监测数据",
            "crack": "裂缝监测数据",
        }

        # 运行时状态
        self.overview: Optional[List[Dict]] = None       # 总览列表
        self.sensor_map: Dict[str, Dict] = {}            # 编号 -> 名称/部位
        self.name_dict: Dict[str, List[Dict]] = {}       # 名称 -> [{编号, 特征}]（人工对照表）
        self.point_map: Dict = {}                        # 测点映射：表类 -> [{断面位置, 测点}]
        self.table_map: Dict = {}                        # 表格映射：表类 -> 墩/位置 -> {编号, 特征}
        self._category_sensors: Dict[str, List[str]] = {}  # 类别 -> 编号列表（从名称对照表）
        self._stats_cache: Dict[str, Dict] = {}          # 编号 -> 统计值 JSON
        self._agg_cache: Dict[str, Dict] = {}             # 期型 -> 季度/年度统计.json
        self._summary_cache: Dict[str, str] = {}          # (指标|报告期) -> 总结句缓存
        self._conclusions_cache: Dict[str, str] = {}      # 报告期 -> 4.1结论缓存
        self._source_text: str = ""                       # 成品报告原文（供总结润色对照）
        self._sensor_features: Dict[str, List[str]] = {} # 编号 -> 特征列表
        self._match_stats = {"name_dict": 0, "alias": 0, "sensor_map": 0, "fuzzy": 0, "metric_fallback": 0}
        self._chart_seq: Dict = {}                       # (metric,位置) -> 已分配序号
        self.loaded = False
        self.load_error: Optional[str] = None

    # ------------------------------------------------------------------
    # 初始化 / 加载
    # ------------------------------------------------------------------

    def _resolve(self, path: str) -> str:
        if not path:
            return ""
        if os.path.isabs(path):
            return path
        if self.base_dir:
            return os.path.join(self.base_dir, path)
        return path

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False))

    def load(self, force: bool = False) -> Dict:
        """加载传感器总览与对照表；返回加载状态摘要。"""
        if self.loaded and not force:
            return self.status()
        self._stats_cache.clear()
        self._pos_stats = {}     # 传感器编号 -> {位置, 测点, 特征统计}（来自位置统计库）
        self.overview = None
        self.sensor_map = {}
        self._sensor_features = {}
        self.name_dict = {}
        self.point_map = {}
        self.table_map = {}
        self._category_sensors = {}
        self._match_stats = {"name_dict": 0, "alias": 0, "sensor_map": 0, "fuzzy": 0, "metric_fallback": 0}
        self._chart_seq = {}

        try:
            # 1. 总览（传感器 -> 特征）
            if self.overview_path and os.path.isfile(self.overview_path):
                with open(self.overview_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for item in data.get("传感器", []) or []:
                    sid = str(item.get("编号", ""))
                    feats = list(item.get("特征", []) or [])
                    self._sensor_features[sid] = feats
                    self.sensor_map[sid] = {
                        "名称": item.get("名称", ""),
                        "桥名": item.get("桥名", ""),
                        "监测部位": item.get("名称", ""),
                    }

            # 2. 编号 -> 名称对照表（更完整时以它为准）
            if self.sensor_map_path and os.path.isfile(self.sensor_map_path):
                with open(self.sensor_map_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for sid, info in (data.get("传感器", {}) or {}).items():
                    name = info.get("名称", "") or info.get("监测部位", "")
                    self.sensor_map[str(sid)] = {
                        "名称": name,
                        "桥名": info.get("桥名", ""),
                        "监测部位": info.get("监测部位", "") or name,
                        "类别": info.get("类别", ""),
                    }
                    # 特征列表（传感器编号名称.json 用“特征编码”字段；
                    # 兼容旧格式“特征”）——缺了它 sensors_for_metric 会退化为全量传感器
                    feats = list(info.get("特征编码", []) or info.get("特征", []) or [])
                    if feats:
                        self._sensor_features[str(sid)] = feats

            # 2b. 人工维护的“传感器名称 -> 编号/特征”对照表
            self._load_name_dict()

            # 2c. 位置统计库：统计值_<期>/<桥名>/位置统计/<位置>.json
            # （以“位置→测点→特征→统计”为准，传感器编号 JSON 已弃用）
            self._load_position_stats()
            # 2d. 按实际数据（位置统计库/图库目录）校正传感器特征与名称对照，
            # 避免对照表硬编码 DZJSD(xJsd) 而 daily/图库实际是 SZJSD(xJsd)
            # 之类“单位不一致”导致图表/统计索引错位（只更新内存，不改文件）。
            self._sync_actual_features()
            if self.charts_dir and not os.path.isdir(self.charts_dir):
                log.warning("图库目录不存在: %s", self.charts_dir)

            self.loaded = True
        except Exception as exc:  # noqa: BLE001
            self.load_error = str(exc)
            log.exception("桥数据加载失败: %s", exc)
        return self.status()

    def _sync_actual_features(self) -> None:
        """按实际数据源（位置统计库精确特征、图库位置/<特征组>/ 目录）更新
        传感器的真实特征编码，并同步写回内存中的名称对照表条目。

        不同批次/桥的 daily 数据单位可能不同（如振动加速度既有 DZJSD 也有
        SZJSD），名称对照表是固定产物无法预知，运行时以实际数据为准，
        这样 cell/chart 索引都能按真实单位命中。
        """
        updated = 0
        covered = set()
        # 1) 位置统计库：文件里是精确特征（如 SZJSD(xJsd)），最可信，
        #    直接覆盖内存特征（含同一传感器多特征，如 WSD(temp)+WSD(rh)）
        for sid, rec in self._pos_stats.items():
            feats = [str(k) for k in (rec.get("特征统计") or {}).keys() if k]
            if not feats:
                continue
            covered.add(str(sid))
            old = self._sensor_features.get(str(sid)) or []
            if sorted(set(feats)) != sorted(set(old)):
                self._sensor_features[str(sid)] = feats
                updated += 1
        # 2) 图库目录：图库_<期>/<桥名>/<位置>/<特征组>/（如 …/SZJSD/），
        #    只在传感器没有任何特征信息时补特征组名（不污染已有精确特征，
        #    避免温度/应变等测点因同位置目录混入 GNSS/DZJSD 组被误取）
        if self.charts_dir and os.path.isdir(self.charts_dir):
            for pos_name, entries in self.name_dict.items():
                if not entries:
                    continue
                base = self._fuzzy_position_dir(pos_name)
                if not os.path.isdir(base):
                    continue
                try:
                    groups = [d for d in os.listdir(base)
                              if os.path.isdir(os.path.join(base, d))]
                except OSError:
                    continue
                groups = [g for g in groups if not g.startswith("相关性")]
                if not groups:
                    continue
                for e in entries or []:
                    sid = str(e.get("编号", ""))
                    if not sid or sid in covered:
                        continue
                    if self._sensor_features.get(sid):
                        continue
                    if len(groups) == 1:
                        self._sensor_features[sid] = [groups[0]]
                        updated += 1
        # 3) 名称对照表条目（内存）同步为实际特征，供“按行取传感器”等
        #    逻辑在特征过滤时命中（特征编码含 DZJSD(xJsd) 与 SZJSD(xJsd)
        #    同族时，按实际特征优先）。
        for entries in self.name_dict.values():
            for e in entries or []:
                sid = str(e.get("编号", ""))
                if not sid:
                    continue
                actual = self._sensor_features.get(sid) or []
                if not actual:
                    continue
                cur = [str(x) for x in (e.get("特征编码") or [])]
                if sorted(set(actual)) != sorted(set(cur)):
                    e["特征编码"] = list(actual)
                    updated += 1
        if updated:
            log.info("按实际数据校正传感器特征/名称对照: %d 处", updated)

    def _load_name_dict(self) -> None:
        """加载 传感器对照/传感器名称对照/<桥名>.json（名称 -> 编号/特征）。
        对照表是固定产物，统一放 preprocess/传感器对照/；旧布局
        (统计值_<期>/<桥名>/传感器名称对照/)仍兼容回退。"""
        path = self.name_dict_path
        if not path:
            base = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "preprocess", "传感器对照", "传感器名称对照")
            for cand in (f"{self.bridge_name}大桥.json",
                         f"{self.bridge_name}.json"):
                p = os.path.join(base, cand)
                if os.path.isfile(p):
                    path = p
                    break
        if not path and self.stats_dir:
            base = os.path.join(self.stats_dir, "传感器名称对照")
            for cand in (f"{self.bridge_name}大桥.json", f"{self.bridge_name}.json"):
                p = os.path.join(base, cand)
                if os.path.isfile(p):
                    path = p
                    break
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw = data.get("传感器名称", {}) or {}
            for name, entries in raw.items():
                key = _norm(name)
                if not key:
                    continue
                self.name_dict[key] = list(entries or [])
            # 测点映射（应变/振动表：断面位置 -> 测点N -> 编号）与表格映射（位移/倾角/裂缝等）
            self.point_map = data.get("测点映射", {}) or {}
            self.table_map = data.get("表格映射", {}) or {}
            # 类别 -> 编号索引（用于指标回退时限定同类别传感器）
            cat_index = {}
            for name, entries in raw.items():
                for e in entries or []:
                    cat = e.get("特征", "")
                    sid = str(e.get("编号", ""))
                    if cat and sid:
                        cat_index.setdefault(cat, set()).add(sid)
            self._category_sensors = {k: sorted(v, key=lambda x: int(x) if x.isdigit() else x)
                                      for k, v in cat_index.items()}
            log.info("加载传感器名称对照表: %s（%d 个名称）", path, len(self.name_dict))
        except Exception as exc:  # noqa: BLE001
            log.warning("加载传感器名称对照表失败 %s: %s", path, exc)
            self.name_dict = {}

    def _pick_from_name_dict(self, key: str, metric: str) -> Optional[str]:
        """名称对照表命中后，优先选与指标特征匹配的编号。"""
        entries = self.name_dict.get(key) or []
        if not entries:
            return None
        feat = self.metrics.get(metric, {}).get("feature", "")
        for e in entries:
            sid = str(e.get("编号", ""))
            if not sid or self._is_excluded(sid):
                continue
            if feat and feat in self._sensor_features.get(sid, []):
                return sid
        for e in entries:
            sid = str(e.get("编号", ""))
            if sid and not self._is_excluded(sid):
                return sid
        return None

    def _sensors_at_position(self, pos: str, metric: str,
                             _visiting: Optional[set] = None) -> List[str]:
        """返回某监测部位中支持该指标的传感器编号（按名称对照顺序）。

        指标有特征时按特征编码过滤；无特征时按“类别”过滤（如 挠度/应变/风荷载），
        避免同一位置混装多种传感器时取错（如 5#塔梁交接处主梁 同时有结构温度/应变/挠度）。
        """
        key = _norm(pos)
        if _visiting is None:
            _visiting = set()
        if key in _visiting:
            return []
        _visiting.add(key)
        key_sides = _side_set(key)
        # 精确键优先：先只按精确键过滤，命中该指标的传感器就直接返回，
        # 避免“跨中1/2截面”把“跨中1/2截面上游/下游”等带方位变体并进来
        # （图表占位符跨中1/2截面 -> 应取无方位的 368，而不是上游 398）。
        # 精确键下没有该指标传感器时，再做模糊/方位顺序无关的候选键合并
        # （如“跨中1/2截面左幅”能补进温度传感器“跨中左幅1/2截面”；
        #  “2#墩墩顶”只有左/右幅候选时也能取到）。
        entries = list(self.name_dict.get(key) or [])
        seen_ids = {str(e.get("编号", "")) for e in entries if e.get("编号")}

        def _filter_entries(entries):
            feat = self.metrics.get(metric, {}).get("feature", "")
            cat = self.metric_category.get(metric, "")
            sids, cat_sids = [], []
            for e in entries:
                sid = str(e.get("编号", ""))
                if not sid or self._is_excluded(sid):
                    continue
                feats = [str(x) for x in (e.get("特征编码") or [])]
                entry_cat = str(e.get("特征") or "")
                if cat and entry_cat == cat:
                    sids.append(sid)
                    if not feat or feat in feats or any(
                            feature_group(f) == feature_group(feat)
                            for f in feats if f):
                        cat_sids.append(sid)
                elif not cat:
                    if not feat or feat in feats:
                        sids.append(sid)
            if cat_sids:
                sids = cat_sids
            if not sids and feat:
                # 同位置特征族回退：temperature(WSD(temp)) 表所在位置若只有
                # structure_temperature(WD(temp)) 传感器（如 索塔塔底），
                # 按括号内编码 temp 命中，避免“跳到别的位置的 WSD 传感器”
                # 导致塔底行填成塔冠值。
                _inner = _feature_code(feat)
                if _inner:
                    for e in entries:
                        sid = str(e.get("编号", ""))
                        if not sid or self._is_excluded(sid) \
                                or sid in sids:
                            continue
                        feats = [str(x) for x in
                                 (e.get("特征编码") or [])]
                        if any(_feature_code(f) == _inner for f in feats):
                            sids.append(sid)
            return sids

        sids = _filter_entries(entries)
        if not sids:
            # 精确键未命中该指标传感器：模糊/方位顺序无关合并
            key_sites = _site_set(key)
            for k, v in self.name_dict.items():
                kn = _norm(k)
                ok = (kn == key
                      or (len(key) >= 2 and key in kn)
                      or (len(kn) >= 2 and kn in key))
                if not ok:
                    # 方位词顺序不同（如 跨中1/2截面左幅 vs 跨中左幅1/2截面）：
                    # 去掉方位词后相同、且双方方位一致才匹配
                    skey = _SIDE_RE.sub("", key)
                    skn = _SIDE_RE.sub("", kn)
                    if (skey and skn and skey == skn
                            and _side_set(kn) == key_sides):
                        ok = True
                if not ok:
                    # 桥端地点词顺序不同（岳阳侧伸缩缝处 vs 伸缩缝处岳阳侧）：
                    # 主体（再去掉方位词）相同、地点/方位集合一致才匹配
                    tkey = _strip_sites(_SIDE_RE.sub("", key))
                    tkn = _strip_sites(_SIDE_RE.sub("", kn))
                    if (tkey and tkn and tkey == tkn
                            and _site_set(kn) == key_sites
                            and _side_set(kn) == key_sides):
                        ok = True
                if ok and key_sides:
                    # 位置带方位时，候选键必须带相同方位
                    if _side_set(kn) != key_sides:
                        ok = False
                if ok and key_sites:
                    # 位置带桥端地点时，候选键必须带相同地点（岳阳≠君山）
                    if _site_set(kn) != key_sites:
                        ok = False
                if not ok:
                    continue
                for e in v or []:
                    sid = str(e.get("编号", ""))
                    if sid and sid not in seen_ids:
                        seen_ids.add(sid)
                        entries.append(e)
            sids = _filter_entries(entries)
        feat = self.metrics.get(metric, {}).get("feature", "")
        cat = self.metric_category.get(metric, "")
        if not sids and not feat and cat:
            # 类别严格匹配不到时（如 vibration 表取到 特征=“地震”的传感器，
            # 洣水河 3#/4#柱墩墩底按“地震荷载”登记），在地震/振动同族内兜底，
            # 避免模板占位符 vibration_3#柱墩墩底左幅_trend_1 解析失败。
            family = ("地震", "振动") if cat in ("地震", "振动") else (cat,)
            for e in entries:
                sid = str(e.get("编号", ""))
                if sid and not self._is_excluded(sid) \
                        and str(e.get("特征", "")) in family:
                    sids.append(sid)
        if not sids:
            # 墩顶支座倾角表：位置如 “4#墩墩顶主梁支座左侧Y” -> 墩号+左/右+X/Y
            if metric == "rotation" and "墩顶支座倾角表" in (self.table_map or {}):
                m = re.search(r"(\d+)#[^左右]*?(左|右)[^xyXY]*?([xyXY])", key)
                if m:
                    entry = (self.table_map["墩顶支座倾角表"].get(m.group(1) + "#")
                             or self.table_map["墩顶支座倾角表"].get(m.group(1)) or {})
                    want = _norm(m.group(2) + m.group(3))
                    e = None
                    for ek, ev in entry.items():
                        if _norm(str(ek)) == want:
                            e = ev
                            break
                    if e and e.get("编号"):
                        return [str(e["编号"])]
        if not sids:
            # 梁端支座位移表：位置如 “4#墩墩顶主梁梁端” -> 墩号 -> 左/右 传感器
            if "梁端" in key and "梁端支座位移表" in (self.table_map or {}):
                m = re.search(r"(\d+)#", key)
                if m:
                    row = (self.table_map["梁端支座位移表"].get(m.group(1) + "#")
                           or self.table_map["梁端支座位移表"].get(m.group(1)) or {})
                    out = []
                    # 位置已带方向（如 “4#墩墩顶主梁梁端左侧”）时只取对应侧，
                    # 避免左侧/右侧都合并成同一组传感器导致图注错位
                    if "左侧" in key or ("左" in key and "右" not in key):
                        sides = ("左",)
                    elif "右侧" in key or ("右" in key and "左" not in key):
                        sides = ("右",)
                    else:
                        sides = ("左", "右")
                    for side in sides:
                        e = row.get(side)
                        if e and e.get("编号"):
                            out.append(str(e["编号"]))
                    if out:
                        return out
        if not sids:
            # 表格映射兜底（结构温度表/温湿度表/裂缝监测表等的位置 -> 传感器列表）
            mkey = {
                "structure_temperature": "结构温度表",
                "temperature": "温湿度表",
                "humidity": "温湿度表",
                "crack": "裂缝监测表",
            }.get(metric, "")
            if mkey and mkey in (self.table_map or {}):
                for k, v in (self.table_map[mkey] or {}).items():
                    if _norm(k) == key:
                        return [str(x) for x in v]
        if not sids:
            # 传感器对照表兜底：位置名精确匹配（如 7LX（S）-22 索力位置）
            feat = self.metrics.get(metric, {}).get("feature", "")
            cat = self.metric_category.get(metric, "")
            for sid, info in self.sensor_map.items():
                nm = _norm(info.get("名称") or "") or _norm(info.get("监测部位") or "")
                if nm != key or self._is_excluded(sid):
                    continue
                feats = self._sensor_features.get(sid, [])
                if feat and feat in feats:
                    sids.append(str(sid))
                elif not feat and cat and info.get("类别") == cat:
                    sids.append(str(sid))
                elif not feat and not cat:
                    sids.append(str(sid))
            if not sids and not feat and cat:
                family = ("地震", "振动") if cat in ("地震", "振动") else (cat,)
                for sid, info in self.sensor_map.items():
                    nm = _norm(info.get("名称") or "") or _norm(info.get("监测部位") or "")
                    if nm != key or self._is_excluded(sid):
                        continue
                    if str(info.get("类别", "")) in family:
                        sids.append(str(sid))
        if not sids:
            # 位置名不一致（如 “58#墩顶部截面” vs “58#墩墩顶截面”）时，
            # 按相似度在全部位置里找有该指标传感器的相近位置，避免振动/
            # 温度等指标因位置名差异取不到数据。
            feat = self.metrics.get(metric, {}).get("feature", "")
            key_sites = _site_set(key)
            key_body = _strip_sites(_SIDE_RE.sub("", key))
            best_pos, best_score = None, 0.0
            for cand, entries2 in self.name_dict.items():
                cand_n = _norm(cand)
                # 查询带明确上游/下游/左/右时，不允许跨侧替身
                # （如 上游无风速传感器时填成 下游 1403 造成两行同值）
                if key_sides and _side_set(cand_n) != key_sides:
                    continue
                cand_body = _strip_sites(_SIDE_RE.sub("", cand_n))
                sc = difflib.SequenceMatcher(None, key_body, cand_body).ratio()
                cand_sites = _site_set(cand)
                if cand_sites and cand_sites == key_sites:
                    sc += 0.12
                elif key_sites:
                    sc -= 0.2
                if sc <= best_score:
                    continue
                has_feat = False
                for e in entries2 or []:
                    feats2 = [str(x) for x in (e.get("特征编码") or [])]
                    if feat and feat in feats2:
                        has_feat = True
                        break
                    # 同位置特征族回退（内码相同即可，忽略 WSD/WD、GNSS/WY
                    # 等模块前缀）：如 支座位移表写 displacement，实际该位置
                    # 只有 WY(Δx) 传感器；温度表写 temperature、位置只有 WD(temp)
                    if feat and any(
                            f and _feature_code(f) == _feature_code(feat)
                            for f in feats2):
                        has_feat = True
                        break
                    if (not feat and cat and e.get("特征") == cat):
                        has_feat = True
                        break
                    if not feat and not cat:
                        has_feat = True
                        break
                if has_feat:
                    best_pos, best_score = cand, sc
            if best_pos is not None and best_score >= 0.6:
                return self._sensors_at_position(best_pos, metric,
                                                 _visiting=_visiting)
        if not sids:
            # 名称对照特征编码与实际统计库不一致（如对照表 DZJSD、
            # 实际 SZJSD）时，按实际统计库特征收集该位置的传感器。
            feat = self.metrics.get(metric, {}).get("feature", "")
            feat_inner = _axis_inner(feat)
            for sid, rec in self._pos_stats.items():
                if _norm(rec.get("位置", "")) != key:
                    continue
                if self._is_excluded(sid):
                    continue
                fstats = rec.get("特征统计") or {}
                hit = False
                if feat and feat in fstats:
                    hit = True
                elif feat_inner:
                    hit = any(_axis_inner(f) == feat_inner
                              for f in fstats)
                elif fstats:
                    hit = True
                if hit and str(sid) not in sids:
                    sids.append(str(sid))
        if not sids:
            # 位移类指标同族回退：同一位置可能混装 GNSS/WY 位移计，
            # 模板把指标写错时（如 伸缩缝支座位移 的图表占位符写成
            # displacement，实际数据是 bearing_displacement/WY(Δx)）
            # 仍能取到传感器，避免“有图有数据却匹配不上”。
            alts = {"displacement": ("bearing_displacement",),
                    "bearing_displacement": ("displacement",),
                    # 环境温度表常把 结构温度(WD(temp)) 的测点行当成
                    # temperature(WSD(temp))：位置上有 WD 而无 WSD 时，
                    # 先按同位置结构温度特征取数，而不是跳到别的位置的
                    # 环境温度传感器（如 索塔塔底 错取 索塔塔冠 1231）。
                    "temperature": ("structure_temperature",),
                    }.get(metric, ())
            if alts:
                for alt in alts:
                    if alt == metric or alt not in self.metrics:
                        continue
                    am = self.metrics.get(alt) or {}
                    af = str(am.get("feature", "") or "")
                    ag = feature_group(af) if af else ""
                    if not af and not ag:
                        continue
                    for e in entries:
                        sid = str(e.get("编号", ""))
                        if not sid or self._is_excluded(sid):
                            continue
                        feats2 = [str(x) for x in (e.get("特征编码") or [])]
                        if not feats2:
                            continue
                        if (af and af in feats2) or (ag and any(
                                feature_group(f) == ag for f in feats2)):
                            if sid not in sids:
                                sids.append(sid)
        return sids

    def _axis_features_at_position(self, pos: str, metric: str) -> List[str]:
        """返回某位置中该指标的轴分量特征（按 X/Y/Z 顺序）。

        如 displacement -> GNSS(Δx/Δy/Δz)、earthquake_load/vibration ->
        SZJSD(xJsd/yJsd/zJsd) 或 DZJSD(xJsd)。按指标特征的前缀组收集，
        只返回括号内编码属于轴集合的特征；无轴分量返回空列表。
        """
        key = _norm(pos)
        key_sites = _site_set(key)
        key_sides = _side_set(key)
        entries = self.name_dict.get(key) or []
        if not entries:
            for k, v in self.name_dict.items():
                kn = _norm(k)
                ok = (kn == key or (len(key) >= 2 and key in kn)
                      or (len(kn) >= 2 and kn in key))
                if not ok:
                    # 桥端地点词/方位词顺序不同时按主体比较
                    tk = _strip_sites(_SIDE_RE.sub("", key))
                    tn = _strip_sites(_SIDE_RE.sub("", kn))
                    ok = (tk and tn and tk == tn
                          and _site_set(kn) == key_sites
                          and _side_set(kn) == key_sides)
                if ok:
                    entries = list(v)
                    break
        feat = self.metrics.get(metric, {}).get("feature", "")
        # 指标特征前缀组：如 GNSS(Δx) -> GNSS、SZJSD(xJsd) -> SZJSD
        fm = re.match(r"^([A-Za-z0-9]+)\(", str(feat or ""))
        prefix = fm.group(1) if fm else ""
        feat_inner = _axis_inner(feat)

        def _candidates():
            """返回 (特征, 是否前缀匹配) 序列：先统计库后名称对照。"""
            for sid, rec in self._pos_stats.items():
                if _norm(rec.get("位置", "")) != key:
                    continue
                for f in (rec.get("特征统计") or {}):
                    yield f, (prefix and f.startswith(prefix + "("))
            for e in entries or []:
                for f in (e.get("特征编码") or []):
                    yield str(f), (prefix and str(f).startswith(prefix + "("))

        # 第一轮：前缀匹配优先（GNSS(Δx/Δy/Δz)）
        out = []
        seen = set()
        for f, is_prefix in _candidates():
            if not is_prefix or not _axis_inner(f) or f in seen:
                continue
            seen.add(f)
            out.append(f)
        if len(out) < 2:
            # 第二轮：括号内编码同族回退（如对照表 DZJSD 但数据 SZJSD）。
            # 按特征前缀分组（DZJSD/SZJSD/GNSS），取包含请求轴编码、且轴
            # 集合最完整的组——DZJSD 只有 xJsd，而 SZJSD 有 xJsd/yJsd/zJsd，
            # 地震表按方向取三行必须用 SZJSD 组。
            groups: Dict[str, List[str]] = {}
            for f, _is_prefix in _candidates():
                inner = _axis_inner(f)
                if not inner:
                    continue
                pre = re.match(r"^([A-Za-z0-9]+)\(", str(f))
                g = pre.group(1) if pre else str(f)
                groups.setdefault(g, []).append(str(f))
            best_group = []
            for gfs in groups.values():
                uniq = []
                for x in gfs:
                    if x not in uniq:
                        uniq.append(x)
                axis = [x for x in uniq if _axis_inner(x)]
                if not feat_inner:
                    continue
                if not any(_axis_inner(x) and _axis_inner(x).lower()
                           == feat_inner.lower() for x in axis):
                    continue
                if len(axis) > len(best_group):
                    best_group = axis
            if len(best_group) > len(out):
                out = best_group
        if not out and feat and feat_inner:
            out = [feat]
        # X/Y/Z 顺序稳定排序
        def _order(f):
            i = _axis_inner(f) or ""
            for pos_i, token in enumerate(("Δx", "Δy", "Δz", "x", "y", "z",
                                           "xJsd", "yJsd", "zJsd",
                                           "xJd", "yJd")):
                if token.lower() == i.lower():
                    return pos_i
            return 99
        return sorted(out, key=_order)

    def status(self) -> Dict:
        """返回加载状态摘要（供 Web 端展示）。"""
        stats_ok = bool(self.stats_dir) and os.path.isdir(self.stats_dir)
        charts_ok = bool(self.charts_dir) and os.path.isdir(self.charts_dir)
        return {
            "enabled": self.enabled,
            "loaded": self.loaded,
            "bridge_name": self.bridge_name,
            "stats_dir": self.stats_dir,
            "charts_dir": self.charts_dir,
            "stats_dir_ok": stats_ok,
            "charts_dir_ok": charts_ok,
            "sensor_count": len(self.sensor_map),
            "sensor_map_path": self.sensor_map_path,
            "name_dict_path": self.name_dict_path or (
                os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "preprocess", "传感器对照", "传感器名称对照")
                if os.path.isdir(os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "preprocess", "传感器对照", "传感器名称对照")) else ""),
            "name_dict_count": len(self.name_dict),
            "match_stats": dict(self._match_stats),
            "error": self.load_error,
        }

    # ------------------------------------------------------------------
    # 传感器定位
    # ------------------------------------------------------------------

    def sensors_for_metric(self, metric: str) -> List[str]:
        """返回支持某指标特征的传感器编号（按编号排序）。"""
        feat = self.metrics.get(metric, {}).get("feature", "")
        cat = self.metric_category.get(metric, "")
        sids = []
        # 1) 监测类别优先（名称对照表 特征 列）：地震/振动/应变/温湿度…
        #    避免 config 特征写错（如 洣水河 earthquake_load 配 DZJSD，
        #    实际地震传感器是 SZJSD）导致按特征过滤取到另一类传感器。
        cat_cands = [sid for sid in (self._category_sensors.get(cat) or [])
                     if not self._is_excluded(sid)]
        if cat_cands:
            if feat:
                g0 = feature_group(feat)
                matched = [
                    sid for sid in cat_cands
                    if any(feature_group(f) == g0
                           for f in self._sensor_features.get(sid, []))]
                if matched:
                    sids = matched
                else:
                    # 类别内特征匹配不到（如 config 写 DZJSD、实际 SZJSD）：
                    # 直接按类别返回，保证地震/振动各自取对传感器
                    sids = cat_cands
            else:
                sids = cat_cands
        # 2) 特征过滤兜底（类别缺失 / 类别内无传感器时）
        if not sids and feat:
            for sid, feats in self._sensor_features.items():
                if self._is_excluded(sid):
                    continue
                if feat in feats:
                    # 只取当前桥的传感器（对照表包含多座桥）
                    if self.bridge_name:
                        bname = self.sensor_map.get(sid, {}).get("桥名", "")
                        if bname and not _bridge_name_match(
                                bname, self.bridge_name):
                            continue
                    sids.append(sid)
        if not sids:
            # 特征未知时：优先用监测类别限定（振动/应变/索力…），避免跨类别污染
            if cat and cat in self._category_sensors:
                sids = [sid for sid in self._category_sensors[cat] if not self._is_excluded(sid)]
        if not sids:
            # 最后才退化为全部传感器
            sids = [sid for sid in self.sensor_map
                    if not self._is_excluded(sid)
                    and (not self.bridge_name
                         or _bridge_name_match(
                             self.sensor_map[sid].get("桥名", ""),
                             self.bridge_name))]
        # 指标级排除（如 displacement 排除“边坡”测点——其 GNSS 统计是
        # 大地坐标绝对值而非桥体位移，会把极值污染成几米/几百米）
        _m = re.match(r"^(.*)_([xyz])$", metric)
        mkey = _m.group(1) if _m else metric
        excl_words = [w for w in (self.metrics.get(mkey, {}) or {}).get(
            "exclude_position_words") or [] if w]
        if excl_words:
            sids = [sid for sid in sids
                    if not any(w in self._position_for_sensor(sid)
                               for w in excl_words)]
        return sorted(sids, key=lambda x: int(x) if x.isdigit() else x)

    def _sensors_for_metric_family(self, metric: str) -> List[str]:
        """返回该指标特征族（含 X/Y/Z 轴分量）的全部传感器。

        用于方向化聚合（displacement_z 等）：sensors_for_metric 只按
        metric.feature（如 GNSS(Δx)）过滤，会把 GNSS(Δy/Δz) 传感器漏掉，
        导致 Y/Z 方向取不到正确测点。这里按 feature_group 归组补全。
        """
        feat = self.metrics.get(metric, {}).get("feature", "")
        group = feature_group(feat) if feat else ""
        sids = []
        for sid, feats in self._sensor_features.items():
            if self._is_excluded(sid):
                continue
            if self.bridge_name:
                bname = self.sensor_map.get(sid, {}).get("桥名", "")
                if bname and not _bridge_name_match(bname, self.bridge_name):
                    continue
            if group and any(feature_group(f) == group for f in feats):
                sids.append(sid)
            elif feat and feat in feats:
                sids.append(sid)
        if not sids:
            sids = self.sensors_for_metric(metric)
        # 指标级排除位置词与 sensors_for_metric 保持一致
        excl_words = [w for w in (self.metrics.get(metric, {}) or {}).get(
            "exclude_position_words") or [] if w]
        if excl_words:
            sids = [sid for sid in sids
                    if not any(w in self._position_for_sensor(sid)
                               for w in excl_words)]
        return sorted(set(sids), key=lambda x: int(x) if x.isdigit() else x)

    def _is_excluded(self, sensor_id: str) -> bool:
        if sensor_id in self.sensor_exclude:
            return True
        info = self.sensor_map.get(sensor_id, {})
        name = f"{info.get('名称', '')} {info.get('监测部位', '')}"
        return any(x and x in name for x in self.sensor_exclude if not x.isdigit())

    def find_sensor(self, metric: str, column: str) -> Optional[str]:
        """把 (指标, 测点描述) 解析成传感器编号。找不到返回 None。"""
        if not column:
            return None
        col = str(column).strip()

        if col.isdigit() and self._is_excluded(col):
            return None

        # 1. 配置别名
        alias = self.sensor_aliases.get(col) or self.sensor_aliases.get(_norm(col))
        if alias:
            self._match_stats["alias"] += 1
            return str(alias)

        # 2. 人工名称对照表（传感器对照/传感器名称对照/<桥名>.json）——精确命中率最高
        key = _norm(col)
        sid = self._pick_from_name_dict(key, metric) if key else None
        if sid:
            self._match_stats["name_dict"] += 1
            return sid

        # 3. 纯编号
        if col.isdigit():
            if col in self.sensor_map and not self._is_excluded(col):
                self._match_stats["sensor_map"] += 1
                return col
            return None

        # 候选：本桥传感器，优先看名称/监测部位
        candidates = []
        for sid, info in self.sensor_map.items():
            if self._is_excluded(sid):
                continue
            if self.bridge_name and info.get("桥名") and not _bridge_name_match(
                    info.get("桥名"), self.bridge_name):
                continue
            names = [info.get("名称", ""), info.get("监测部位", "")]
            if any(_norm(col) == _norm(n) for n in names if n):
                self._match_stats["sensor_map"] += 1
                return sid
            candidates.append((sid, names))

        # 4. 模糊包含匹配（候选含名称对照表的键，提高简称/变体命中率）
        flat = [(sid, n) for sid, names in candidates for n in names if n]
        for nk in self.name_dict:
            nsid = self._pick_from_name_dict(nk, metric)
            if nsid:
                flat.append((nsid, nk))
        best_sid, best_score = None, 0.0
        for sid, n in flat:
            if not sid:
                continue
            score = _similarity(col, n)
            if score > best_score:
                best_sid, best_score = sid, score
        if best_sid is not None and best_score >= self.fuzzy_threshold:
            self._match_stats["fuzzy"] += 1
            return best_sid

        # 5. 特征限定：在支持该指标特征的传感器里再找一次（名称可能更规范）
        metric_sids = set(self.sensors_for_metric(metric))
        if metric_sids:
            best_sid, best_score = None, 0.0
            for sid in metric_sids:
                info = self.sensor_map.get(sid, {})
                for n in (info.get("名称", ""), info.get("监测部位", "")):
                    if not n:
                        continue
                    score = _similarity(col, n)
                    if score > best_score:
                        best_sid, best_score = sid, score
            if best_sid is not None and best_score >= self.fuzzy_threshold:
                self._match_stats["fuzzy"] += 1
                return best_sid
        return None

    # ------------------------------------------------------------------
    # 统计值解析
    # ------------------------------------------------------------------

    def _load_sensor_stats(self, sensor_id: str) -> Optional[Dict]:
        if sensor_id in self._stats_cache:
            return self._stats_cache[sensor_id]
        # 位置统计库优先（位置 -> 测点N -> 特征 -> 统计）
        rec = self._pos_stats.get(str(sensor_id))
        if rec:
            data = {
                "编号": str(sensor_id),
                "名称": rec.get("位置", ""),
                "桥名": self.bridge_name,
                "特征统计": rec.get("特征统计", {}),
            }
            self._stats_cache[str(sensor_id)] = data
            return data
        # 只使用合并的位置统计库（merged），不再读取逐传感器 <编号>.json
        self._stats_cache[str(sensor_id)] = None
        return None

    def _load_position_stats(self) -> None:
        """加载 位置统计库（与图库目录结构对齐）。

        新结构:
          统计值_<期>/<桥名>/位置统计/<位置>/<特征>.json
          内容: {位置: {测点N: {"统计": {...}, "传感器编号": sid}}}
          相关性: 位置统计/<位置>/相关性_<特征A>-<特征B>.json
        兼容旧结构: 位置统计/<位置>.json
          内容: {位置: {测点N: {特征: {"统计": {...}, "传感器编号": sid}}}}
        建 传感器编号 -> (位置, 测点, 特征统计) 索引供运行时取值。
        """
        pos_dir = os.path.join(self.stats_dir, "位置统计")
        if not os.path.isdir(pos_dir):
            log.warning(
                "位置统计目录不存在: %s（请检查 config bridge_data.stats_dir "
                "指向的目录名/结构；上一期成功时该目录应存在）", pos_dir)
            return
        def _index_pos(pos, points):
            """索引测点结构。

            新结构(单特征 JSON):  {位置: {测点X: {"统计": {...}, "传感器编号": sid}}}
            旧结构(多特征 JSON):  {位置: {测点X: {特征: {"统计": {...}, "传感器编号": sid}}}}
            """
            for pt, feats in (points or {}).items():
                if not isinstance(feats, dict):
                    continue
                if "统计" in feats and isinstance(feats["统计"], dict):
                    # 新结构: 单个特征，统计直接在测点下
                    sid = str(feats.get("传感器编号") or "")
                    if not sid:
                        continue
                    feat_name = str(feats.get("特征") or "")
                    self._pos_stats.setdefault(sid, {
                        "位置": str(pos), "测点": str(pt),
                        "特征统计": {},
                    })["特征统计"][feat_name] = dict(feats["统计"])
                    continue
                # 旧结构: 多个特征
                feat_stats = {}
                sid = ""
                for feat, v in feats.items():
                    if isinstance(v, dict) and "统计" in v:
                        feat_stats[str(feat)] = dict(v["统计"])
                        if not sid and v.get("传感器编号"):
                            sid = str(v["传感器编号"])
                if not sid:
                    continue
                self._pos_stats[sid] = {
                    "位置": str(pos),
                    "测点": str(pt),
                    "特征统计": feat_stats,
                }

        # 新结构: 位置统计/<位置>/<特征>.json
        try:
            pos_items = [d for d in os.listdir(pos_dir)
                         if os.path.isdir(os.path.join(pos_dir, d))]
        except OSError:
            pos_items = []
        new_loaded = 0
        for pname in pos_items:
            pdir = os.path.join(pos_dir, pname)
            try:
                for fn in os.listdir(pdir):
                    if not fn.endswith(".json"):
                        continue
                    with open(os.path.join(pdir, fn), "r",
                              encoding="utf-8") as f:
                        data = json.load(f)
                    for pos, points in (data or {}).items():
                        _index_pos(pos, points)
                        new_loaded += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("读取位置统计目录 %s 失败: %s", pdir, exc)
        # 兼容旧结构: 位置统计/<位置>.json
        if not new_loaded:
            try:
                files = [f for f in os.listdir(pos_dir)
                         if f.endswith(".json")]
            except OSError:
                files = []
            for fn in files:
                try:
                    with open(os.path.join(pos_dir, fn), "r",
                              encoding="utf-8") as f:
                        data = json.load(f)
                except Exception as exc:  # noqa: BLE001
                    log.warning("读取位置统计 %s 失败: %s", fn, exc)
                    continue
                for pos, points in (data or {}).items():
                    _index_pos(pos, points)
        if self._pos_stats:
            log.info("位置统计库: %s（%d 个传感器）", pos_dir, len(self._pos_stats))

    def _load_aggregate_stats(self, period: Optional[Dict] = None) -> Dict:
        """加载 季度统计.json / 年度统计.json(按监测部位聚合)，用于血缘回退。

        新布局: 统计值_<期>/<桥名>/季度总结/季度统计.json 或 年度统计.json；
        同时兼容旧布局 统计值_<期>/<桥名>/<文件名>。
        年度报告优先 年度统计.json、季度报告优先 季度统计.json，
        避免同目录两文件并存时年度报告取到季度数据。
        """
        yearly = bool(period and str(period.get("label") or "").endswith("年"))
        ck = "yearly" if yearly else "quarterly"
        if ck in self._agg_cache:
            return self._agg_cache[ck]
        order = (("年度统计.json", "季度统计.json") if yearly
                 else ("季度统计.json", "年度统计.json"))
        agg = {}
        for fn in order:
            for base in (os.path.join(self.stats_dir, "季度总结"),
                         self.stats_dir):
                p = os.path.join(base, fn)
                if fn in agg or not os.path.isfile(p):
                    continue
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        agg[fn] = json.load(f)
                except Exception as exc:  # noqa: BLE001
                    log.warning("读取聚合统计 %s 失败: %s", p, exc)
        self._agg_cache[ck] = agg
        return agg

    def _aggregate_sensor_stat(self, sensor_id: str, metric: str, stat: str,
                               feature: str = "") -> Optional[float]:
        """通过 季度/年度统计文件(按桥+监测部位+特征)查找统计值。"""
        info = self.sensor_map.get(str(sensor_id), {})
        bridge = info.get("桥名", "") or ""
        loc = info.get("监测部位") or info.get("名称") or ""
        feat = feature or self.metrics.get(metric, {}).get("feature", "")
        if not bridge or not loc or not feat:
            return None
        agg = self._load_aggregate_stats()
        for data in agg.values():
            bridges = data.get("桥", {}) or {}
            b = next((bk for bk in bridges if bridge in bk or bk in bridge), None)
            if not b:
                continue
            # 新格式: 特征为最高键 -> {全桥统计, 位置:{位置:{测点X:{统计}}}}
            fe = bridges[b].get(feat)
            v = None
            if isinstance(fe, dict):
                # 该位置指定测点：无数据传感器（该测点不在位置统计/聚合里）
                # 不回退到“全桥统计”——否则整季无数据的测点行会填成全桥
                # 聚合值（多行同值/假数据），应整行“—”。
                pts = ((fe.get("位置") or {}).get(loc) or {})
                for _pt, rec in pts.items():
                    if str(rec.get("传感器编号", "")) == str(sensor_id):
                        st = (rec.get("统计") or {})
                        # 聚合库里有什么就填什么（不再按“疑似恒值”二次判断）
                        v = self._full_period_stats(st, stat)
                        if v is not None:
                            break
            if v is None:
                # 旧格式: 位置为最高键 -> {位置:{特征:{统计}}}
                pos = bridges[b].get(loc)
                if isinstance(pos, dict):
                    fe2 = pos.get(feat)
                    if isinstance(fe2, dict):
                        st = (fe2.get("统计") or {})
                        v = self._full_period_stats(st, stat)
            if v is not None:
                return v
        return None

    def _agg_feature_location(self, feature: str, stat: str,
                              period: Optional[Dict] = None) -> str:
        """从季度/年度统计 全桥统计 里取极值对应监测部位（如 最大值位置）。

        build_quarterly_stats 已把 最大值/最小值/绝对最大值/差值/
        剔除温度差值 等极值的位置写入 JSON，总结段落
        “对应测点为…”直接引用，避免运行时逐传感器重算。
        """
        if not feature:
            return ""
        key = STAT_KEY_MAP.get(stat, stat)
        pos_key = f"{key}位置"
        agg = self._load_aggregate_stats(period)
        for data in agg.values():
            bridges = data.get("桥", {}) or {}
            for b in bridges.values():
                if not isinstance(b, dict):
                    continue
                fe = b.get(feature)
                if isinstance(fe, dict):
                    v = (fe.get("全桥统计") or {}).get(pos_key)
                    if v:
                        return str(v)
        return ""

    def abnormal_positions(self, metric: str, period: Dict) -> List[str]:
        """返回该指标下存在缺失数据的监测部位。

        判定：位置统计里任一测点缺失 ≥7 天（或达到 summary_miss_hours
        阈值），以及名称对照里属于该特征、但统计库完全没有记录的监测
        部位（完全无数据）。
        """
        feat = self.metrics.get(metric, {}).get("feature", "")
        if not feat:
            return []
        out = []
        agg = self._load_aggregate_stats(period)
        for data in agg.values():
            bridges = data.get("桥", {}) or {}
            for b in bridges.values():
                if not isinstance(b, dict):
                    continue
                fe = b.get(feat)
                if not isinstance(fe, dict):
                    continue
                pos_entries = fe.get("位置") or {}
                if not isinstance(pos_entries, dict):
                    continue
                pos_entries, _ = self._filter_pos_entries(
                    metric, pos_entries)
                for pos, points in pos_entries.items():
                    if not isinstance(points, dict):
                        continue
                    for _pt, rec in points.items():
                        st = (rec.get("统计") or {}) if isinstance(rec, dict) else {}
                        try:
                            miss_days = float(st.get("缺失天数") or 0)
                            miss_hours = float(st.get("缺失小时数") or 0)
                        except (TypeError, ValueError):
                            miss_days = miss_hours = 0
                        if (miss_days >= 7
                                or miss_hours
                                >= self._summary_miss_threshold()):
                            out.append(str(pos))
                            break
        # 名称对照里属于该特征、但统计库完全没有记录的监测部位（完全缺失）
        with_data = {str(rec.get("位置"))
                     for rec in self._pos_stats.values()
                     if feat in (rec.get("特征统计") or {})}
        for key, entries in (self.name_dict or {}).items():
            for e in entries or []:
                feats = [str(x) for x in (e.get("特征编码") or [])]
                if feat not in feats:
                    continue
                sid = str(e.get("编号", ""))
                info = self.sensor_map.get(sid, {}) or {}
                if self.bridge_name and info.get("桥名") \
                        and not _bridge_name_match(
                            info.get("桥名"), self.bridge_name):
                    continue
                if key not in with_data and key not in out:
                    out.append(str(key))
                break
        return out

    def resolve_data_status(self, metric: str, period: Dict) -> str:
        """总结段落状态句：无缺失 -> “XXX监测数据正常稳定”；
        有缺失 -> “位置A、位置B位置数据异常，其余XXX监测数据正常稳定”。"""
        label = (self._status_labels.get(metric)
                 or f"{self.metrics.get(metric, {}).get('label', '')}监测数据")
        abnormal = self.abnormal_positions(metric, period)
        if not abnormal:
            return f"{label}正常稳定"
        return "、".join(abnormal) + "位置数据异常，其余" + label + "正常稳定"

    def resolve_abnormal_clause(self, metric: str, period: Dict) -> str:
        """总结段落异常句首：有缺失 -> “本季度内，位置A、位置B数据出现异常，
        由设备设置错误引起。其余”；无缺失 -> 空串。"""
        abnormal = self.abnormal_positions(metric, period)
        if not abnormal:
            return ""
        return ("本季度内，" + "、".join(abnormal)
                + "数据出现异常，由设备设置错误引起。其余")

    def build_feature_summary(self, metric: str, period: Dict,
                              llm_cfg: Optional[Dict] = None) -> str:
        """基于季度/年度统计生成某指标的结论性总结（≤100 字）。

        数据源：季度总结/季度统计.json（或年度统计.json）里该特征键的
        全桥统计（极值 + 对应位置）+ 各位置缺失/持续为 0 情况。
        LLM 可用时由 LLM 生成（重点突出缺失与极值特殊位置），
        否则用规则化兜底文本。

        同一（指标, 报告期）只生成一次并缓存——模板里 3.3.5 小结和
        4.1 结论等多次出现的 {{summary.<metric>}} 拿到的是同一句话，
        避免同一指标前后数值不一致。
        """
        mcfg = self.metrics.get(metric) or {}
        feat = self._metric_summary_feature(metric, period)
        label = mcfg.get("label", metric)
        unit = mcfg.get("unit", "")
        if not feat:
            return ""
        cache_key = f"{metric}|{period.get('start')}|{period.get('end')}"
        if cache_key in self._summary_cache:
            return self._summary_cache[cache_key]
        digest = self._feature_summary_digest(feat, label, unit, period, metric)
        if not digest:
            return ""
        from .llm_classifier import LLMClassifier
        text = ""
        classifier = LLMClassifier(llm_cfg or {})
        if classifier.available():
            src = self._source_excerpt_for(label)
            prompt = digest["prompt"]
            if src:
                prompt = (
                    prompt
                    + "。成品报告原文对应该指标的结论（仅作对照，若与真实数据矛盾"
                    "必须以真实数据为准）：" + src
                )
            # 方向化指标（空间变位/地震/振动 X/Y/Z）极值+位置较多，
            # 220 字不够会被截成半句（如“Z方向…、最小值”），放大上限
            text = classifier.summarize_feature(
                prompt, max_chars=600)
            if text and not self._summary_text_valid(text, digest):
                log.warning("总结数值与统计摘要不一致，降级为规则化兜底: %s",
                            text)
                text = ""
        if not text:
            text = digest["fallback"]
        text = normalize_unit_spacing(text)
        # 分轴总结（空间变位 GNSS X/Y/Z 等）LLM 可能漏掉主语
        # （如直接以“X方向最大值…”开头）：补上指标名，保持与其它
        # 分项（挠度监测/应变/振动指标）一致的表达。
        if text and re.match(r"^[XYZ]方向", text) and label \
                and not text.startswith(label):
            text = f"{label}：" + text
        self._summary_cache[cache_key] = text
        return text

    def _metric_summary_feature(self, metric: str,
                                period: Dict) -> str:
        """返回该指标用于总结的实际特征键。

        config 特征可能过时（如 地震 配 DZJSD(xJsd)、实际数据是
        SZJSD(xJsd/yJsd/zJsd)）：当 config 特征组不在该类别的实际特征组里
        时，改用 _feature_for_metric（类别传感器实际特征族优先）。这样
        地震/振动的总结与表格（分方向）一致，而不是读到同族其它指标的数据。
        """
        mcfg = self.metrics.get(metric) or {}
        cfg_feat = str(mcfg.get("feature", "") or "")
        cat = self.metric_category.get(metric, "")
        actual_groups = set()
        if cat:
            for sid in (self._category_sensors.get(cat) or []):
                for f in (self._sensor_features.get(str(sid)) or []):
                    if f:
                        actual_groups.add(feature_group(f))
        if cfg_feat:
            if actual_groups and feature_group(cfg_feat) not in actual_groups:
                return self._feature_for_metric(metric, period) or cfg_feat
            return cfg_feat
        return self._feature_for_metric(metric, period)

    def build_conclusions(self, period: Dict,
                          llm_cfg: Optional[Dict] = None) -> str:
        """生成 4.1 监测结论：以各监测分项的小结为上下文，LLM 综合整理。

        覆盖所有有季度统计的分项（温度/湿度/风速/应变/挠度/索力/倾角/
        支座位移/裂缝/空间变位/振动/交通等），每条含关键数值、对应位置、
        异常/故障/缺失情况。无 LLM 或失败时用规则化条目兜底。
        """
        ck = f"conclusions|{period.get('start')}|{period.get('end')}"
        if ck in self._conclusions_cache:
            return self._conclusions_cache[ck]
        items = []
        for metric, mcfg in (self.metrics or {}).items():
            if not isinstance(mcfg, dict):
                continue
            try:
                feat = mcfg.get("feature", "") \
                    or self._feature_for_metric(metric, period)
            except Exception:  # noqa: BLE001
                feat = ""
            if not feat:
                continue
            txt = self.build_feature_summary(metric, period, llm_cfg=llm_cfg)
            if not txt:
                continue
            label = mcfg.get("label", metric)
            items.append((label, txt))
        # 交通荷载（车道1~4 数值/比例）：vehicle_count 无 feature，单独补
        tr = self.build_traffic_summary(period)
        if tr:
            items.append(("交通荷载", tr))
        if not items:
            return ""
        context = "\n".join(f"{i + 1}. {lb}：{tx}"
                            for i, (lb, tx) in enumerate(items))
        from .llm_classifier import LLMClassifier
        classifier = LLMClassifier(llm_cfg or {})
        if classifier.available():
            text = classifier.summarize_conclusions(context)
            if text:
                self._conclusions_cache[ck] = text
                return text
        bn = self.bridge_name or "该桥"
        # 各分项总结已自带“指标：”主语（结构温度：最高…），避免再重复
        text = "\n".join(
            f"（{i + 1}）{bn}{tx}" if tx.startswith(f"{lb}：")
            else f"（{i + 1}）{bn}{lb}：{tx}"
            for i, (lb, tx) in enumerate(items))
        self._conclusions_cache[ck] = text
        return text

    def build_traffic_summary(self, period: Dict) -> str:
        """交通荷载小结：按实际车道数（车道1~N）输出 数值(辆)/比例(%)。"""
        parts = []
        for ln in self._traffic_lane_names():
            st = self._traffic_lane_stat(ln)
            if not st:
                continue
            num = st.get("数值")
            if num is None:
                continue
            txt = f"{ln}方向车辆总数为{float(num):.0f}辆"
            ratio = st.get("比例")
            if ratio is not None:
                txt += f"（占比{float(ratio):.1f}%）"
            parts.append(txt)
        return "、".join(parts) + "。" if parts else ""

    def _traffic_lane_names(self) -> List[str]:
        """位置统计/交通荷载/交通荷载.json 里实际存在的 车道N 键（升序）。

        不同桥车道数不同（矮寨 4、洞庭湖 6…），不写死 车道1~4。
        """
        out = []
        if not self.stats_dir:
            return out
        p = os.path.join(self.stats_dir, "位置统计",
                         "交通荷载", "交通荷载.json")
        if not os.path.isfile(p):
            return out
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:  # noqa: BLE001
            return out
        nums = set()
        for _pos, points in (data or {}).items():
            if not isinstance(points, dict):
                continue
            for k in points:
                m = re.search(r"车道\s*(\d+)", str(k))
                if m:
                    nums.add(int(m.group(1)))
        return [f"车道{i}" for i in sorted(nums)]

    def _feature_for_metric(self, metric: str, period: Dict) -> str:
        """返回该指标在季度/年度统计里的实际特征键。

        判定优先级（保证与“该节真实测点”一致，接近人工语义识别）：
          1) 该指标类别传感器（名称对照表 特征 列）的实际特征族 -> 聚合键。
             例如 洣水河 地震传感器实际是 SZJSD(xJsd)，即使 config 把
             earthquake_load 写成 DZJSD(xJsd)，也以实际 SZJSD 为准；
          2) config 配置的特征（特征族匹配）；
          3) 类别族探测（振动/地震 -> DZJSD/SZJSD/EZJD）。
        """
        cat = self.metric_category.get(metric, "")
        mcfg = self.metrics.get(metric) or {}
        cfg_feat = mcfg.get("feature", "")
        agg = self._load_aggregate_stats(period)
        agg_keys = []
        for data in agg.values():
            for b in (data.get("桥") or {}).values():
                if isinstance(b, dict):
                    agg_keys.extend(k for k in b.keys()
                                    if isinstance(k, str) and k)
        fam = ("DZJSD", "SZJSD", "EZJD") if cat in ("地震", "振动") else ()
        # 1) 类别传感器实际特征族 -> 聚合键
        if cat:
            actual_groups = set()
            for sid in (self._category_sensors.get(cat) or []):
                for f in (self._sensor_features.get(str(sid)) or []):
                    actual_groups.add(feature_group(f))
            for k in agg_keys:
                if feature_group(k) in actual_groups:
                    return k
        # 2) config 特征（族匹配）
        if cfg_feat:
            g0 = feature_group(cfg_feat)
            for k in agg_keys:
                if k == cfg_feat or feature_group(k) == g0:
                    return k
        # 3) 类别族探测
        for k in agg_keys:
            if fam and re.match(r"^(?:%s)\(" % "|".join(fam), k):
                return k
            if cat and k.startswith(cat):
                return k
        return ""

    def _source_excerpt_for(self, label: str) -> str:
        """从成品报告原文里取该指标相关的一段结论（≤160 字），供总结润色对照。"""
        src = (self._source_text or "").strip()
        if not src or not label:
            return ""
        # 按句切分，找含指标标签的句子，向前后各延 1 句
        sents = re.split(r"(?<=[。；;])", src)
        hits = [i for i, s in enumerate(sents) if label in s]
        if not hits:
            # 标签没命中时，退回包含“监测/正常/异常/故障/缺失”的结论句
            hits = [i for i, s in enumerate(sents)
                    if any(w in s for w in ("监测", "正常", "异常", "故障", "缺失"))]
        if not hits:
            return ""
        i = hits[0]
        lo = max(0, i - 1)
        hi = min(len(sents), i + 2)
        excerpt = "".join(sents[lo:hi]).strip()
        return excerpt[:160]

    @staticmethod
    def _summary_text_valid(text: str, digest: Dict) -> bool:
        """校验 LLM 总结里的数值都来自摘要给定值集合（防止 LLM 自行编造
        差值/极值，如把 581.8 写成 232.2）。位置/测点里的数字（58#墩、测点2）
        不算数值声明，跳过。"""
        if not text or not digest:
            return True
        refs = []
        for v in digest.get("values") or []:
            try:
                refs.append(float(v))
            except (TypeError, ValueError):
                continue
        if not refs:
            return True
        # 位置/编号里的数字不算数值声明：1/4截面、1/2截面、58#墩、测点2、
        # 第6跨等。先把这些从待校验文本里剔除，只校验真正的数值声明，
        # 避免“中跨1/4截面顶板上游”这种位置描述把 LLM 总结误判为不一致。
        t = re.sub(r"\d+\s*/\s*\d+", "", str(text))
        t = re.sub(r"\d+\s*[#号]", "", t)
        t = re.sub(r"测点\s*\d+", "测点", t)
        t = re.sub(r"第\s*\d+", "第", t)
        t = re.sub(r"(\d+)\s*跨", "", t)
        t = re.sub(r"恒\s*0", "", t)
        for m in re.finditer(r"-?\d+(?:\.\d+)?", t):
            after = t[m.end():m.end() + 1]
            if after and after[0] in "#号点跨墩":
                continue
            try:
                num = float(m.group(0))
            except ValueError:
                continue
            if any(abs(num - r) <= max(abs(r) * 0.005, 0.05) for r in refs):
                continue
            return False
        return True

    def _feature_summary_digest(self, feature: str, label: str, unit: str,
                                period: Dict, metric: str = "") -> Optional[Dict]:
        """组装特征统计摘要（给 LLM）与规则化兜底句。"""
        agg = self._load_aggregate_stats(period)
        fe = None
        bridge_feats = None
        for data in agg.values():
            for b in (data.get("桥") or {}).values():
                if isinstance(b, dict) and feature in b:
                    fe = b.get(feature)
                    bridge_feats = b
                    break
            if fe is not None:
                break
        if not isinstance(fe, dict):
            return None
        gs = fe.get("全桥统计") or {}
        pos_entries = fe.get("位置") or {}
        if not isinstance(pos_entries, dict):
            pos_entries = {}

        zero_pos, seg_pos, abnormal = self._fault_positions(
            gs, pos_entries, feature, metric, period)
        # 方向化指标（GNSS(Δx/Δy/Δz)、SZJSD(xJsd/yJsd/zJsd) 等）：
        # 按方向分别给极值，避免总结把 Y 方向位置串给 Z 方向。
        axes = self._summary_axes(bridge_feats, feature)
        if len(axes) >= 2:
            return self._direction_summary_digest(
                metric, label, unit, period, axes, bridge_feats,
                pos_entries, zero_pos, seg_pos, abnormal)

        def _f(key):
            try:
                v = float(gs.get(key))
                return v if v == v else None
            except (TypeError, ValueError):
                return None

        avg = _f("平均值")
        miss_h = _f("缺失小时数")
        days = gs.get("覆盖天数") or ""
        # 极值/位置与 {{stats.<metric>.max|min[.loc]}} 同一口径（逐传感器聚合 +
        # 季度统计位置键），保证总结句与正文统计占位符一致；
        # 解析不到时回退到 全桥统计 的极值与位置键
        max_v, max_loc = self._metric_extreme(metric, "max", "最大值",
                                              "最大值位置", period, gs,
                                              pos_entries)
        min_v, min_loc = self._metric_extreme(metric, "min", "最小值",
                                              "最小值位置", period, gs,
                                              pos_entries)
        range_v, range_loc = self._metric_extreme(metric, "range", "差值",
                                                  "差值位置", period, gs,
                                                  pos_entries)
        abs_v, abs_loc = self._metric_extreme(metric, "abs_max", "绝对最大值",
                                              "绝对最大值位置", period, gs,
                                              pos_entries)
        trm_v, trm_loc = self._metric_extreme(
            metric, "temp_rm_range", "剔除温度差值",
            "剔除温度差值位置", period, gs, pos_entries)
        # 严格类别隔离后，本类别极值全不可用（超物理范围/无有效测点）时，
        # 必须提示重跑统计库，不能写成“整体正常”。
        _no_extreme = bool(
            metric and self._category_sensor_ids(metric) is not None
            and max_v is None and min_v is None and range_v is None)

        # 最小值==0 时，把逐传感器明细里值为 0 的测点补进 疑似故障位置，
        # 避免位置统计扫描漏掉（如季度统计里该位置不是 max==min==0 记录）
        if min_v is not None and min_v == 0.0 and metric:
            try:
                _v0, _d0 = self.resolve_metric_stat_detail(
                    metric, "min", period)
                for _s in (_d0 or {}).get("逐传感器") or []:
                    if not isinstance(_s, dict):
                        continue
                    try:
                        _sv = float(_s.get("值"))
                    except (TypeError, ValueError):
                        continue
                    if abs(_sv) <= 1e-9:
                        _pos0 = str(_s.get("监测部位") or "").strip()
                        if _pos0 and _pos0 not in zero_pos:
                            zero_pos.append(_pos0)
            except Exception:  # noqa: BLE001
                pass

        # 最小值==0 且存在恒0故障位置：0 极可能来自故障测点（如结构温度 0℃），
        # 用位置统计清洗值重算真实最小值（跳过恒值/恒0测点），
        # 避免把故障 0 当真实极值、位置还取错。
        if min_v is not None and min_v == 0.0 and (zero_pos or seg_pos):
            cv, cloc = self._clean_extreme_from_positions(
                pos_entries, feature, "最小值", exclude_zero=True)
            if cv is not None and abs(cv) > 1e-9:
                min_v, min_loc = cv, cloc
        # 重算后仍为 0 且存在恒0位置：位置强制指向故障位置
        if min_v is not None and min_v == 0.0 \
                and (zero_pos or seg_pos) and not min_loc:
            min_loc = (zero_pos or seg_pos)[0]

        prompts = [f"指标：{label}；报告期：{period.get('start')} ~ {period.get('end')}"]
        if _no_extreme:
            prompts.append(
                "本指标类别的测点极值均异常（超出物理范围或无可信数据），"
                "需重跑统计库后复核；不得写成“整体正常”。")
        values = []
        if days:
            try:
                days_txt = str(int(float(days)))
            except (TypeError, ValueError):
                days_txt = str(days)
            prompts.append(f"覆盖{days_txt}天")
            try:
                values.append(float(days))
            except (TypeError, ValueError):
                pass
        if avg is not None:
            prompts.append(f"平均值{format_report_number(avg)}{unit}")
            values.append(avg)
        if max_v is not None:
            prompts.append(f"最大值{format_report_number(max_v)}{unit}"
                           + (f"（位置：{max_loc}）" if max_loc else ""))
            values.append(max_v)
        if min_v is not None:
            prompts.append(f"最小值{format_report_number(min_v)}{unit}"
                           + (f"（位置：{min_loc}）" if min_loc else ""))
            values.append(min_v)
        for v, loc, name in ((range_v, range_loc, "最大差值"),
                             (abs_v, abs_loc, "绝对最大值"),
                             (trm_v, trm_loc, "剔除温度效应后最大差值")):
            if v is not None:
                prompts.append(f"{name}{format_report_number(v)}{unit}"
                               + (f"（位置：{loc}）" if loc else ""))
                values.append(v)
        if miss_h and miss_h >= self._summary_miss_threshold():
            prompts.append(f"全桥缺失小时数合计{format_report_number(miss_h)}")
        _cap = self._cap_positions
        _yearly = str(period.get("label") or "").endswith("年")
        month_miss = []
        if _yearly:
            avg_cov = gs.get("平均覆盖天数")
            if avg_cov is not None:
                try:
                    prompts.append(
                        f"全年平均覆盖天数{format_report_number(avg_cov)}天")
                except (TypeError, ValueError):
                    pass
            month_miss = self._month_missing_positions(pos_entries, 30)
        # 年度报告只报“缺失一个月以上”，季度/月度报 summary_miss_hours
        # （默认 168h=7 天）阈值；避免年度同时出现两套缺失口径。
        missing_pos = month_miss if _yearly else abnormal
        missing_label = self._miss_label(_yearly)
        if missing_pos:
            prompts.append(missing_label + _cap(missing_pos))
        # 多数传感器公共缺失时间段（build_chart_library 生成的小时级区间）
        cm_periods = gs.get("多数传感器缺失时间段") or []
        if cm_periods:
            prompts.append("多数传感器公共缺失时间段："
                           + "、".join(_fmt_range_readable(x)
                                       for x in cm_periods))
        if zero_pos:
            prompts.append("恒0/恒值疑似故障位置：" + _cap(zero_pos))
        if seg_pos:
            prompts.append("疑似故障位置（含故障段）：" + _cap(seg_pos))
        if zero_pos or seg_pos or missing_pos:
            prompts.append(
                "总结必须把上面列出的 恒0/恒值故障、含故障段、数据缺失 "
                "位置写进去（即使原文没提也要补上）；位置过多时列前 5 个"
                "并加“等”，不要全部罗列；含故障段位置不得写成“恒0”，"
                "数据缺失不得写成“故障”。")
        if min_v is not None and min_v == 0.0 and zero_pos:
            prompts.append(
                "注意：最低值0来自持续为0疑似故障位置，属传感器故障，"
                "总结时必须写明“传感器故障”，位置使用故障位置"
                f"{min_loc or zero_pos[0]}，不得把0当作真实极值。")
        prompts.append(
            "只能引用上面给出的数值，禁止自行计算或编造新的数值；"
            "摘要中没有出现的数值（尤其差值/极值）一律不要写。")
        digest_text = "；".join(prompts).rstrip("。；") + "。"

        # 规则化兜底句（恒0/恒值、含故障段、缺失分开表述，避免把“0℃故障”
        # 当真实极值；故障/缺失位置完整列出，不截断）
        parts = []
        if max_v is not None:
            parts.append(f"最高{format_report_number(max_v)}{unit}"
                         + (f"（{max_loc}）" if max_loc else ""))
        if min_v is not None:
            parts.append(f"最低{format_report_number(min_v)}{unit}"
                         + (f"（{min_loc}）" if min_loc else ""))
        if not parts:
            parts.append("整体正常")
        if _no_extreme:
            parts = [x for x in parts if x != "整体正常"]
            parts.append("极值数据异常，需重跑统计库后复核")
        head = "、".join(parts)
        special = []
        if _no_extreme:
            special.append("本指标类别测点极值数据异常，需重跑统计库后复核")
        if zero_pos:
            special.append("恒0/恒值疑似故障位置：" + _cap(zero_pos))
        if seg_pos:
            special.append("疑似故障位置（含故障段）：" + _cap(seg_pos))
        if missing_pos:
            special.append(missing_label + _cap(missing_pos))
        if cm_periods:
            special.append("多数传感器在" + "、".join(
                _fmt_range_readable(x) for x in cm_periods)
                           + "时间段内数据缺失")
        if not special:
            fallback = f"{label}监测数据整体正常，{head}。"
        else:
            # 有故障/缺失时补上指标主语（如 结构温度：最高…、最低…），
            # 避免小结段落里只有数值没有监测对象
            fallback = f"{label}：{head}；{'；'.join(special)}，" \
                       f"其余测点正常，需关注。"
        return {"prompt": digest_text, "fallback": fallback, "values": values}

    def _summary_miss_threshold(self) -> float:
        try:
            # 结论段只说明缺失超过 7 天（168h）的时段；短时掉线不进结论
            return float(self.cfg.get("summary_miss_hours", 168) or 168)
        except (TypeError, ValueError):
            return 168.0

    def _miss_label(self, yearly: bool = False) -> str:
        """缺失位置提示词：年度按“一个月以上”，季度/月度按配置阈值
        （summary_miss_hours，默认 168h=7 天），与 abnormal_positions /
        _fault_positions 的判定口径保持一致——避免文案写 72h、判定却用
        7 天这种“说的和做的不一样”。"""
        if yearly:
            return "数据缺失一个月以上位置（缺失天数>30天）："
        thr = self._summary_miss_threshold()
        return (f"数据缺失超过{format_report_number(thr / 24.0)}天位置"
                f"（缺失合计≥{format_report_number(thr)}h）：")

    @staticmethod
    def _cap_positions(items, cap: int = 5) -> str:
        """位置列表过多时截断：列出前 cap 个并加“等”，避免总结过长影响阅读。"""
        items = [str(x) for x in (items or []) if str(x).strip()]
        if len(items) <= cap:
            return "、".join(items)
        return "、".join(items[:cap]) + "等"

    def _month_missing_positions(self, pos_entries: Dict,
                                 min_days: int = 30) -> List[str]:
        """返回缺失天数超过 min_days 的监测部位（年度报告“数据缺失一个月
        以上”用）。"""
        out = []
        for pos, points in (pos_entries or {}).items():
            if not isinstance(points, dict):
                continue
            worst = 0.0
            for _pt, rec in points.items():
                st = (rec.get("统计") or {}) if isinstance(rec, dict) else {}
                try:
                    worst = max(worst, float(st.get("缺失天数") or 0))
                except (TypeError, ValueError):
                    continue
            if worst > min_days:
                out.append(str(pos))
        return out

    def _fault_positions(self, gs: Dict, pos_entries: Dict, feature: str,
                         metric: str, period: Dict):
        """返回 (恒0/恒值疑似故障位置, 含故障段疑似故障位置, 数据缺失位置)。

        恒0/恒值 = 整季恒值（含持续为0）；含故障段 = 存在连续恒0等疑似
        故障时间段、但仍有正常数据的测点（如 635 号传感器），总结里不得
        把这类写成“恒0”。
        """
        # 类别隔离：振动/地震等同特征码的指标只用本类别的测点位置，
        # 避免故障/缺失清单互相串（仅在无类别信息时保留旧行为）。
        pos_entries, _strict = self._filter_pos_entries(metric, pos_entries)
        # 1) 含故障段位置：季度统计的“疑似故障时间段”键（位置（测点）），
        #    以及位置统计里带“疑似故障时间段”字段的测点
        seg_keys = set()
        for k in ("疑似故障时间段", "疑似故障时间段（位置）"):
            v = gs.get(k)
            if isinstance(v, dict):
                seg_keys.update(str(x) for x in v.keys())
            elif isinstance(v, list):
                seg_keys.update(str(x) for x in v)
        zero_pos, seg_pos = [], []
        # 2) 位置统计逐测点分类：整季恒值（含恒0）-> 恒0/恒值（优先判定，
        #    整季为0即使被记为故障段也属恒0）；有故障段但仍有正常数据 ->
        #    含故障段
        for pos, points in pos_entries.items():
            if not isinstance(points, dict):
                continue
            for _pt, rec in points.items():
                st = (rec.get("统计") or {}) if isinstance(rec, dict) else {}
                try:
                    mx = float(st.get("最大值"))
                    mn = float(st.get("最小值"))
                except (TypeError, ValueError):
                    continue
                label = f"{pos}（{_pt}）" if len(points) > 1 else str(pos)
                if abs(mx - mn) <= 1e-9 \
                        and self._constant_faulty(st, feature):
                    if label not in zero_pos:
                        zero_pos.append(label)
                elif st.get("疑似故障时间段") and label not in seg_pos:
                    seg_pos.append(label)

        # 3) 季度统计权威清单补充：位置统计已分类的位置（含具体测点）不再
        #    重复加位置级条目，避免同一位置既出现在“恒0/恒值”又出现在
        #    “含故障段”（如 汝城侧边跨跨中截面顶板下游 的 测点1/2 整季恒0，
        #    位置级条目不得再进 含故障段）。完全没覆盖到的位置再按
        #    “是否在 疑似故障时间段 键里”拆成 含故障段 / 恒0·恒值。
        covered_exact = set(zero_pos) | set(seg_pos)
        covered_bases = {
            re.sub(r"（[^）]*）$", "", str(x)) for x in covered_exact}
        for k in (() if _strict else ("疑似故障传感器位置", "持续为0位置")):
            for p in (gs.get(k) or []):
                ps = str(p)
                if not ps or ps in covered_exact:
                    continue
                if re.sub(r"（[^）]*）$", "", ps) in covered_bases:
                    continue
                if any(ps == s or (s and s.startswith(ps + "（"))
                       for s in seg_keys):
                    seg_pos.append(ps)
                else:
                    zero_pos.append(ps)
                covered_exact.add(ps)

        # 去重并保持“位置（测点）”更具体的形式优先
        def _dedup(items):
            out, seen = [], set()
            for it in items:
                it = str(it).strip()
                if not it or it in seen:
                    continue
                # 已有点位级条目时，位置级重复条目忽略
                if any(o != it and (it in o) for o in seen):
                    continue
                seen.add(it)
                out.append(it)
            return out

        zero_pos = _dedup(zero_pos)
        seg_pos = _dedup(seg_pos)

        # 4) 数据缺失位置：**照抄季度/年度总结的清单**
        #    （build_quarterly_stats 的 “数据缺失严重的传感器位置”，口径应为
        #    缺失合计 ≥7 天/168h）。旧版本季度总结是按 72h 出的，直接抄会把
        #    短时掉线又写回结论，所以用位置统计里的缺失小时数按 7 天口径复核
        #    一遍；位置统计里查不到的位置（如完全无数据）按总结原样保留。
        miss_hours_thr = self._summary_miss_threshold()
        miss_pos = []
        for p in [str(x) for x in
                  (gs.get("数据缺失严重的传感器位置") or []) if x]:
            _sev = self._position_miss_severe(p, pos_entries, miss_hours_thr)
            if _sev is None or _sev:
                miss_pos.append(p)
        # 旧库没有该键 → 按位置统计重算（同样 7 天口径）
        if not (gs.get("数据缺失严重的传感器位置") or []):
            for pos, points in pos_entries.items():
                if not isinstance(points, dict):
                    continue
                for _pt, rec in points.items():
                    st = (rec.get("统计") or {}) if isinstance(rec, dict) else {}
                    try:
                        mh = float(st.get("缺失小时数") or 0)
                        md = float(st.get("缺失天数") or 0)
                    except (TypeError, ValueError):
                        continue
                    if mh >= miss_hours_thr or md >= 7:
                        miss_pos.append(str(pos))
                        break
        # 补充完全无数据的监测部位（名称对照里属于该特征但统计库无记录）
        for p in (self.abnormal_positions(metric, period) if metric else []):
            if p not in miss_pos:
                miss_pos.append(p)
        miss_pos = _dedup(miss_pos)
        return zero_pos, seg_pos, miss_pos

    @staticmethod
    def _position_miss_severe(pos: str, pos_entries: Dict,
                              thr: float) -> Optional[bool]:
        """某个位置是否达到“缺失严重”阈值（供复核季度总结清单用）。

        统计库里查不到该位置时返回 None（无从复核，按总结原样保留）。
        """
        base = re.sub(r"（[^）]*）$", "", str(pos))
        found, hit = False, False
        for p, points in (pos_entries or {}).items():
            if str(p) not in (base, str(pos)):
                continue
            if not isinstance(points, dict):
                continue
            found = True
            for _pt, rec in points.items():
                st = (rec.get("统计") or {}) if isinstance(rec, dict) else {}
                try:
                    mh = float(st.get("缺失小时数") or 0)
                    md = float(st.get("缺失天数") or 0)
                except (TypeError, ValueError):
                    continue
                if mh >= thr or md >= 7:
                    hit = True
        return hit if found else None

    def _metric_extreme(self, metric: str, stat: str, gs_key: str,
                        gs_loc_key: str, period: Dict, gs: Dict,
                        pos_entries: Optional[Dict] = None):
        """按“该指标类别的逐传感器聚合”取极值+位置。

        禁止回退到共享特征码的全桥统计（如 振动/地震同用 DZJSD(xJsd)），
        避免两个指标总结到同一批数据；仅当该指标没有类别信息（旧配置）
        时才允许使用 全桥统计 兜底。
        """
        v, loc = None, ""
        strict = self._category_sensor_ids(metric) if metric else None
        if metric:
            val, detail = self.resolve_metric_stat_detail(metric, stat, period)
            if val is not None:
                v = float(val)
                loc = str((detail or {}).get("位置") or "")
            if v is None and strict is not None:
                filt, _ = self._filter_pos_entries(metric, pos_entries or {})
                if filt:
                    _feat = self.metrics.get(metric, {}).get("feature", "")
                    v, loc = self._clean_extreme_from_positions(
                        filt, _feat, gs_key,
                        exclude_zero=(stat == "min"))
                return v, loc
        if v is None:
            try:
                v = float(gs.get(gs_key))
            except (TypeError, ValueError):
                v = None
            loc = str(gs.get(gs_loc_key) or "")
        return v, loc

    def _category_sensor_ids(self, metric: str):
        """该指标类别的传感器编号集合；无类别信息返回 None。"""
        if not metric:
            return None
        cat = self.metric_category.get(metric, "")
        if not cat:
            return None
        sids = [str(s) for s in (self._category_sensors.get(cat) or [])
                if not self._is_excluded(s)]
        return set(sids)

    def _filter_pos_entries(self, metric: str, pos_entries: Dict):
        """按指标类别过滤 位置统计（位置->测点->{统计,传感器编号}）。

        返回 (过滤后, 是否启用类别隔离)。类别信息缺失时原样返回，
        允许旧配置走共享特征码回退。
        """
        sids = self._category_sensor_ids(metric)
        if sids is None:
            return (pos_entries or {}), False
        out = {}
        for pos, points in (pos_entries or {}).items():
            if not isinstance(points, dict):
                continue
            for pt, rec in points.items():
                sid = str((rec or {}).get("传感器编号") or "")
                if sid in sids:
                    out.setdefault(pos, {})[pt] = rec
        return out, True

    def _clean_extreme_from_positions(self, pos_entries: Dict, feature: str,
                                      stat_key: str,
                                      exclude_zero: bool = False):
        """从位置统计逐测点扫描极值，跳过恒值/恒0故障测点。返回 (值, 位置)。"""
        best_v, best_loc = None, ""
        for pos, points in pos_entries.items():
            if not isinstance(points, dict):
                continue
            for _pt, rec in points.items():
                st = (rec.get("统计") or {}) if isinstance(rec, dict) else {}
                try:
                    v = float(st.get(stat_key))
                except (TypeError, ValueError):
                    continue
                if self._constant_faulty(st, feature):
                    continue
                _stat_name = {"最大值": "max", "最小值": "min",
                              "差值": "range",
                              "绝对最大值": "abs_max"}.get(stat_key, stat_key)
                if self._gross_stat_fault(st, feature, _stat_name):
                    continue
                if exclude_zero and abs(v) <= 1e-9:
                    continue
                if best_v is None:
                    best_v, best_loc = v, str(pos)
                elif stat_key == "最小值" and v < best_v:
                    best_v, best_loc = v, str(pos)
                elif stat_key != "最小值" and v > best_v:
                    best_v, best_loc = v, str(pos)
        return best_v, best_loc

    def _summary_axes(self, bridge_feats: Optional[Dict],
                      feature: str) -> Dict[str, str]:
        """检测特征是否有 X/Y/Z 方向分量，返回 {轴: 特征键}（如
        {"X": "GNSS(Δx)", "Y": "GNSS(Δy)", "Z": "GNSS(Δz)"}）。"""
        if not isinstance(bridge_feats, dict):
            return {}
        m = re.match(r"^([A-Za-z0-9]+)\(", feature or "")
        base = m.group(1) if m else ""
        if not base:
            return {}
        keys = [str(k) for k in bridge_feats.keys()]
        axis_pat = {
            "X": (rf"^{base}\(Δx\)$", rf"^{base}\([xX]Jsd?\)$",
                  rf"^{base}\([xX]Jd\)$"),
            "Y": (rf"^{base}\(Δy\)$", rf"^{base}\([yY]Jsd?\)$",
                  rf"^{base}\([yY]Jd\)$"),
            "Z": (rf"^{base}\(Δz\)$", rf"^{base}\([zZ]Jsd?\)$"),
        }
        out = {}
        for ax, pats in axis_pat.items():
            hit = next((k for k in keys
                        if any(re.search(p, k) for p in pats)), "")
            if hit:
                out[ax] = hit
        return out

    def _direction_summary_digest(self, metric: str, label: str, unit: str,
                                  period: Dict, axes: Dict[str, str],
                                  bridge_feats: Dict, pos_entries: Dict,
                                  zero_pos: List[str],
                                  seg_pos: List[str],
                                  abnormal: List[str]) -> Optional[Dict]:
        """方向化指标（GNSS X/Y/Z 等）摘要：每个方向单独给极值与位置，
        避免总结把 Y 方向位置串给 Z 方向。"""
        axis_labels = {"X": "X方向", "Y": "Y方向", "Z": "Z方向"}
        prompts = [
            f"指标：{label}（分方向统计）；总结开头必须带指标名"
            f"（如“{label}：X方向最大值…”）；"
            f"报告期：{period.get('start')} ~ {period.get('end')}"
        ]
        fallback_parts = []
        values = []
        for ax in axes:
            feat_key = axes[ax]
            fe_ax = bridge_feats.get(feat_key) or {}
            gs_ax = fe_ax.get("全桥统计") or {}
            am = f"{metric}_{ax.lower()}" if metric else ""
            lines = []
            for stat, gs_key, gs_loc_key, cn in (
                    ("max", "最大值", "最大值位置", "最大"),
                    ("min", "最小值", "最小值位置", "最小"),
                    ("range", "差值", "差值位置", "差值")):
                stat_cn = cn if stat == "range" else f"{cn}值"
                v, loc = None, ""
                if am:
                    val, detail = self.resolve_metric_stat_detail(
                        am, stat, period)
                    if val is not None:
                        v = float(val)
                        loc = str((detail or {}).get("位置") or "")
                if v is None:
                    # 类别隔离：同指标类别内取极值；仅在无类别信息时回退全桥统计
                    v, loc = self._metric_extreme(
                        am, stat, gs_key, gs_loc_key, period, gs_ax,
                        pos_entries)
                if v is None:
                    continue
                values.append(v)
                lines.append(f"{stat_cn}{format_report_number(v)}{unit}"
                             + (f"（位置：{loc}）" if loc else ""))
                fallback_parts.append(
                    f"{axis_labels[ax]}{stat_cn}{format_report_number(v)}{unit}"
                    + (f"（{loc}）" if loc else ""))
            if lines:
                prompts.append(f"{axis_labels[ax]}：" + "、".join(lines))
        _cap = self._cap_positions
        _yearly = str(period.get("label") or "").endswith("年")
        month_miss = []
        if _yearly:
            _fpos, _ = self._filter_pos_entries(metric, pos_entries)
            month_miss = self._month_missing_positions(_fpos, 30)
        # 年度报告只报“缺失一个月以上”，季度/月度报 summary_miss_hours 阈值
        missing_pos = month_miss if _yearly else abnormal
        missing_label = self._miss_label(_yearly)
        if missing_pos:
            prompts.append(missing_label + _cap(missing_pos))
        cm_periods = gs_ax.get("多数传感器缺失时间段") or []
        if cm_periods:
            prompts.append("多数传感器公共缺失时间段："
                           + "、".join(_fmt_range_readable(x)
                                       for x in cm_periods))
        if zero_pos:
            prompts.append("恒0/恒值疑似故障位置：" + _cap(zero_pos))
        if seg_pos:
            prompts.append("疑似故障位置（含故障段）：" + _cap(seg_pos))
        if zero_pos or seg_pos or missing_pos:
            prompts.append(
                "总结必须把上面列出的 恒0/恒值故障、含故障段、数据缺失 "
                "位置写进去（即使原文没提也要补上）；位置过多时列前 5 个"
                "并加“等”，不要全部罗列；含故障段位置不得写成“恒0”，"
                "数据缺失不得写成“故障”。")
        prompts.append(
            "X/Y/Z 各方向的数值与对应测点位置必须按上面逐一对应，"
            "不得把某一方向的位置串用到其他方向；"
            "只能引用上面给出的数值，禁止编造新的数值。")
        digest_text = "；".join(prompts).rstrip("。；") + "。"
        special = []
        if zero_pos:
            special.append("恒0/恒值疑似故障位置：" + _cap(zero_pos))
        if seg_pos:
            special.append("疑似故障位置（含故障段）：" + _cap(seg_pos))
        if missing_pos:
            special.append(missing_label + _cap(missing_pos))
        if cm_periods:
            special.append("多数传感器在" + "、".join(
                _fmt_range_readable(x) for x in cm_periods)
                           + "时间段内数据缺失")
        head = "、".join(fallback_parts) if fallback_parts else "整体正常"
        if not special:
            fallback = f"{label}监测数据整体正常，{head}。"
        else:
            fallback = f"{label}：{head}；{'；'.join(special)}，" \
                       f"其余测点正常，需关注。"
        return {"prompt": digest_text, "fallback": fallback, "values": values}

    @staticmethod
    def _constant_faulty(fstats: Dict, feature: str) -> bool:
        """恒值传感器判定：整季最大值==最小值（恒0/恒非0）视为故障/无效，
        表格行填“—”，不参与聚合。
        例外：裂缝(LF)/挠度(ND)/风速(spfs,szfs) 等“0为正常值”的特征，恒为 0
        属正常状态，不算故障。
        注：故障时间段已在统计生成时剔除（build_chart_library 对温度/湿度类
        剔除连续恒0段与零星0点），因此这里不再因 min==0 就判整行无效——
        有正常数据段的传感器照常展示清洗后的统计值。"""
        if not isinstance(fstats, dict):
            return False
        try:
            mx = float(fstats.get("最大值"))
            mn = float(fstats.get("最小值"))
        except (TypeError, ValueError):
            return False
        m = re.search(r"\(([^)]+)\)$", str(feature or ""))
        code = (m.group(1) if m else "").lower()
        # 风速符号随服务器不同（FSFX2(spfs)/FSFX2(szfs)/FSFX2(s)/裸码）：
        # 轴码以 fs 结尾、或风模块(FSFX*)下轴码 s 都按“0为正常值”处理
        module = str(feature or "").split("(", 1)[0].strip()
        zero_ok = (code in ("nd", "spfs", "szfs")
                   or str(feature or "").upper().startswith("LF")
                   or code.endswith("fs")
                   or (code == "s" and module.upper().startswith("FSFX")))
        if zero_ok:
            return False
        span = abs(mx - mn)
        if span <= 1e-9:
            return True
        # “近恒值”：整季只有微幅噪声（如温度卡在 15.000±0.06、应变卡在
        # 1.000±0.0005），本质也是传感器故障/未接入。按特征给不同阈值，
        # 避免把健康的小波动测点（索力/位移/倾角等）误判；正常温度/湿度/
        # 应变测点一个季度内的总变化都远大于这些阈值。
        near_const_thr = {
            "temp": 0.5,   # ℃：正常测点季度温差 >0.5℃
            "rh": 0.5,     # %：正常湿度测点季度变化 >0.5%
            "rsg": 0.5,    # με：正常应变测点季度变化 >0.5με
        }.get(code, 0.0)
        if near_const_thr > 0.0 and span <= near_const_thr:
            return True
        return False

    @staticmethod
    def _gross_faulty(fstats: Dict, feature: str) -> bool:
        """统计库为旧版本、未清洗时整季极值严重超出物理范围（如湿度 5.2e8、
        温度 -82.7℃）的故障测点：读取阶段直接视为无效（整行“—”），
        避免把不可能值填进报告/结论。正常统计库（已清洗）不会触发。"""
        if not isinstance(fstats, dict):
            return False
        try:
            mx = float(fstats.get("最大值"))
            mn = float(fstats.get("最小值"))
        except (TypeError, ValueError):
            return False
        code = _feature_code(feature)
        if code == "rh":
            return mx > 100.0 or mn < -10.0
        if code == "temp":
            # 7~9 月出现的 -30.7℃、温差 60.95℃ 属明显故障；冬季正常
            # 结构温度可到 -27℃ 左右，因此下限取 -30℃、温差上限取 50℃。
            try:
                rng = float(fstats.get("差值"))
            except (TypeError, ValueError):
                rng = abs(mx - mn)
            return mx > 80.0 or mn < -30.0 or rng > 50.0
        if code == "spfs":
            return mx > 100.0 or mn < 0.0
        if code == "szfs":
            return mx > 60.0 or mn < -60.0
        # 平均值不在 [最小值, 最大值] 内（如 平均121 > 最大100）：
        # 统计口径自相矛盾，视为故障测点，整行“—”
        try:
            av = float(fstats.get("平均值"))
            tol = 1e-6 * max(abs(mx), abs(mn), 1.0)
            if av < mn - tol or av > mx + tol:
                return True
        except (TypeError, ValueError):
            pass
        return False

    @staticmethod
    def _robust_stat_faulty(fstats: Dict, feature: str, can: str) -> bool:
        """稳健统计量（平均/中位/均方根/标准差）**自身**是否已离谱。

        用于“极值/差值异常、但均值这类量仍然可信”的场景：例如温度测点
        最小值 -21.9℃ 属故障（差值随之 73℃ 超限），但平均值 12.5℃ 正常，
        这时平均温度必须照常显示，不能整行填“—”。
        """
        key = {"avg": "平均值", "mean": "平均值", "value": "平均值",
               "median": "中位数", "rms": "均方根值",
               "std": "标准差"}.get(can)
        if not key:
            return False
        try:
            v = float(fstats.get(key))
        except (TypeError, ValueError):
            return False
        code = _feature_code(feature)
        if code == "rh":
            if v > 100.0 or v < -10.0:
                return True
        elif code == "temp":
            if v > 80.0 or v < -30.0:
                return True
        elif code == "spfs":
            if v > 100.0 or v < 0.0:
                return True
        elif code == "szfs":
            if v > 60.0 or v < -60.0:
                return True
        # 均值落在 [最小值, 最大值] 之外 → 统计口径自相矛盾，视为无效
        try:
            mx, mn = float(fstats.get("最大值")), float(fstats.get("最小值"))
            tol = 1e-6 * max(abs(mx), abs(mn), 1.0)
            if v < mn - tol or v > mx + tol:
                return True
        except (TypeError, ValueError):
            pass
        return False

    @staticmethod
    def _zero_polluted_extreme(fstats: Dict) -> bool:
        """极值被“恒0故障段”污染：JSON 已列出疑似故障时间段，且最小值恰为 0、
        平均值明显不为 0（0 是掉零/缺数，不是真实读数）。

        这类统计库（预处理漏剔恒0段的旧库）最小值会被打成 0、差值随之等于
        最大值，报告里必须先走“每日重算 → 同族测点中位数”再用，不能照抄 0。
        """
        if not isinstance(fstats, dict):
            return False
        if not (fstats.get("疑似故障时间段") or fstats.get("持续为0位置")):
            return False
        try:
            mn = float(fstats.get("最小值"))
            av = float(fstats.get("平均值"))
        except (TypeError, ValueError):
            return False
        return abs(mn) <= 1e-9 and abs(av) > 1e-6

    @staticmethod
    def _gross_stat_fault(fstats: Dict, feature: str, stat: str) -> bool:
        """按“具体统计量”判断是否明显失真：
        - 温湿度/风速等整条序列异常的，整行无效；
        - 加速度/应变/位移等：只把超物理范围的极值/差值判失效，
          平均值/最小值等正常统计仍可用（如振动最大值 5.6e6 m/s² 时
          最大值与差值填“—”，平均值/最小值照常显示）。
        """
        if not isinstance(fstats, dict):
            return False
        code = _feature_code(feature)
        if code in ("rh", "temp", "spfs", "szfs"):
            # 平均/中位/均方根这类稳健量只按“自身是否离谱”判：
            # 极值故障（如最低温 -21.9℃、差值 73℃）不应把均值也打成“—”
            can = _canon_stat(STAT_KEY_MAP.get(stat, stat))
            if can in ("avg", "mean", "value", "median", "rms", "std"):
                return BridgeData._robust_stat_faulty(fstats, feature, can)
            # 恒0故障段把最小值打成 0（差值跟着等于最大值）→ 极值不可信，
            # 交给“每日重算 / 同族中位数”取值，不要照抄 0
            if can in ("min", "range", "diff", "abs_max", "absmax"):
                if BridgeData._zero_polluted_extreme(fstats):
                    return True
            return BridgeData._gross_faulty(fstats, feature)
        limit = None
        if code.endswith("jsd") or code in ("xjsd", "yjsd", "zjsd"):
            limit = 1000.0
        elif code == "rsg":
            limit = 50000.0
        elif code in ("nd", "δx", "δy", "δz", "ax", "ay", "az"):
            limit = 100000.0
        if not limit:
            return False

        def _f(key):
            try:
                return float(fstats.get(key))
            except (TypeError, ValueError):
                return None

        mx, mn, av = _f("最大值"), _f("最小值"), _f("平均值")
        can = _canon_stat(STAT_KEY_MAP.get(stat, stat))
        if can == "max":
            return mx is not None and abs(mx) > limit
        if can in ("abs_max", "absmax"):
            vals = [abs(v) for v in (mx, mn) if v is not None]
            return bool(vals) and max(vals) > limit
        if can == "min":
            return mn is not None and abs(mn) > limit
        if can in ("range", "diff"):
            d = _f("差值")
            if d is None and mx is not None and mn is not None:
                d = mx - mn
            return d is not None and abs(d) > 2 * limit
        if av is not None and abs(av) > limit:
            return True
        if mx is not None and mn is not None and av is not None:
            tol = 1e-6 * max(abs(mx), abs(mn), 1.0)
            if av < mn - tol or av > mx + tol:
                return True
        return False



    def _feature_stats(self, sensor_id: str, metric: str, feature: str = "") -> Optional[Dict]:
        data = self._load_sensor_stats(sensor_id)
        feat = feature or self.metrics.get(metric, {}).get("feature", "")
        if data:
            stats = data.get("特征统计", {}) or {}
            if feat and feat in stats:
                return stats[feat]
            # 特征编码同族回退：对照表写 DZJSD(xJsd) 但实际统计库是
            # SZJSD(xJsd)（括号内编码一致）时，按括号内编码匹配取该传感器
            # 的真实统计，避免掉到“季度聚合回退”拿到全桥同一值。
            if feat:
                want_inner = _axis_inner(feat)
                if want_inner:
                    for _f, _st in stats.items():
                        _in = _axis_inner(_f)
                        if _in and _in.lower() == want_inner.lower():
                            return _st
            # 特征没对上时，取唯一特征；多特征则取第一个（保证有值可读）
            if len(stats) == 1:
                return next(iter(stats.values()))
            if stats and not feat:
                return next(iter(stats.values()))
        # 旧版 <编号>.json 缺失时，从位置统计库读取
        # (统计值_<期>/<桥名>/位置统计/<位置>.json -> 测点X -> 特征)
        info = self.sensor_map.get(str(sensor_id), {})
        loc = info.get("监测部位") or info.get("名称") or ""
        if loc:
            rec = self._position_stat_feature(loc, str(sensor_id), feat)
            if rec:
                fstats = dict(rec.get("统计") or {})
                dl = rec.get("每日统计")
                if isinstance(dl, list):
                    fstats["每日统计"] = dl
                return fstats
        return None

    def _position_stat_feature(self, pos: str, sensor_id: str,
                               feature: str) -> Optional[Dict]:
        """从位置统计库读取 位置/测点X/特征 的记录。"""
        if not self.stats_dir:
            return None
        import re as _re
        safe = _re.sub(r'[\\/:*?"<>|]', "_", str(pos)).strip()
        p = os.path.join(self.stats_dir, "位置统计", f"{safe}.json")
        if not os.path.isfile(p):
            # 位置名匹配图库目录时带“内/侧”差异，做模糊匹配
            pdir = os.path.join(self.stats_dir, "位置统计")
            if os.path.isdir(pdir):
                best, best_score = None, 0.0
                for fn in os.listdir(pdir):
                    if not fn.endswith(".json"):
                        continue
                    cand = fn[:-5]
                    sc = _similarity(pos, cand)
                    if sc > best_score:
                        best, best_score = fn, sc
                if best and best_score >= 0.72:
                    p = os.path.join(pdir, best)
        if not os.path.isfile(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        for _pos, points in (data or {}).items():
            if not isinstance(points, dict):
                continue
            for _pt, feats in points.items():
                if not isinstance(feats, dict):
                    continue
                for feat, rec in feats.items():
                    if not isinstance(rec, dict):
                        continue
                    if str(rec.get("传感器编号", "")) == str(sensor_id) \
                            and (not feature or feature == feat):
                        return rec
        return None

    def _sensor_stat(self, sensor_id: str, metric: str, stat: str, period: Dict,
                     feature: str = "") -> Optional[float]:
        """读取单个传感器（可指定特征）在报告期内的统计值。

        原则：**统计库里有什么就填什么，没有就返回 None（填“—”）**。
        报告只做只读回填，不再做“疑似故障/恒值/极值超限 → 改写或借同族
        测点”的二次判断——清洗责任在预处理（build_chart_library）。
        """
        fstats = self._feature_stats(sensor_id, metric, feature=feature)
        if not fstats:
            # 位置统计里没有该测点：退到季度/年度聚合统计（也是“库里有的”）
            return self._aggregate_sensor_stat(sensor_id, metric, stat,
                                               feature=feature)
        v = self._full_period_stats(fstats, stat)
        if v is not None:
            return v
        # 该统计量整体统计里没有（如未生成“剔除温度差值”）：
        # 用库内每日明细按报告期平算，同样不做额外清洗
        daily = self._period_daily(fstats, period)
        if daily:
            return self._aggregate_daily(daily, stat, fstats=fstats,
                                         clean=False)
        return None


    def _stat_detail(self, sensor_id: str, metric: str, stat: str, period: Dict,
                     feature: str = "") -> Optional[Dict]:
        """单个传感器统计 + 数据来源明细；读不到返回 None（填“—”）。

        与 _sensor_stat 同一原则：统计库有什么就回填什么，不做二次判断。
        """
        fstats = self._feature_stats(sensor_id, metric, feature=feature)
        _feat = feature or self.metrics.get(metric, {}).get("feature", "")
        if not fstats:
            v = self._aggregate_sensor_stat(sensor_id, metric, stat,
                                            feature=feature)
            if v is None:
                return None
            info = self.sensor_map.get(str(sensor_id), {})
            return {
                "传感器编号": str(sensor_id),
                "监测部位": info.get("名称") or info.get("监测部位") or "",
                "特征": feature or self.metrics.get(metric, {}).get("feature", ""),
                "统计文件": os.path.join(self.stats_dir, "季度总结",
                                       "季度统计.json"),
                "数据来源": "季度/年度聚合统计",
                "天数": 0,
                "值": v,
            }
        info = self.sensor_map.get(str(sensor_id), {})
        feat_resolved = feature or self.metrics.get(metric, {}).get("feature", "")
        src_file = self._actual_stats_path(sensor_id, feature=feat_resolved)
        # 1) 统计库里有的字段直接回填（预处理已清洗，报告不再二次判断）
        v = self._full_period_stats(fstats, stat)
        if v is None and _canon_stat(stat) in ("temp_rm_range", "剔除温度差值"):
            mx = fstats.get("剔除温度最大值")
            mn = fstats.get("剔除温度最小值")
            if mx is not None and mn is not None:
                v = float(mx) - float(mn)
        if v is not None:
            return {
                "传感器编号": str(sensor_id),
                "监测部位": info.get("名称") or info.get("监测部位") or "",
                "特征": feature or self.metrics.get(metric, {}).get("feature", ""),
                "统计文件": src_file,
                "数据来源": "统计值JSON直读（预处理清洗后口径）",
                "天数": int(fstats.get("覆盖天数") or 0),
                "值": v,
            }
        # 2) 该统计量整体统计里没算：用库内每日明细按报告期平算（不加额外清洗）
        daily = self._period_daily(fstats, period)
        if not daily:
            return None
        v = self._aggregate_daily(daily, stat, fstats=fstats, clean=False)
        if v is None:
            return None
        return {
            "传感器编号": str(sensor_id),
            "监测部位": info.get("名称") or info.get("监测部位") or "",
            "特征": feature or self.metrics.get(metric, {}).get("feature", ""),
            "统计文件": src_file,
            "数据来源": "库内每日明细按报告期平算",
            "天数": len(daily),
            "值": v,
        }

    def _actual_stats_path(self, sensor_id: str, feature: str = "") -> str:
        """返回该传感器统计实际来源文件。

        现在统计库已改为“位置统计/<位置>/<特征>.json”结构，逐传感器的
        <编号>.json 已不再生成；血缘日志的“统计文件”应指向真实读取的文件，
        避免显示成不存在的 <编号>.json。
        """
        rec = self._pos_stats.get(str(sensor_id))
        if rec:
            loc = str(rec.get("位置") or "")
            feats = rec.get("特征统计") or {}
            if not loc:
                return os.path.join(self.stats_dir, "位置统计")
            safe = re.sub(r'[\\/:*?"<>|]', "_", loc)
            # 精确特征 -> 同族特征（DZJSD(xJsd) 实际 SZJSD(xJsd)）-> 唯一特征
            feat = ""
            if feature:
                for f in feats:
                    if f == feature:
                        feat = f
                        break
                if not feat:
                    want = _axis_inner(feature)
                    for f in feats:
                        if want and _axis_inner(f) \
                                and _axis_inner(f).lower() == want.lower():
                            feat = f
                            break
            if not feat and len(feats) == 1:
                feat = next(iter(feats))
            if feat:
                p = os.path.join(self.stats_dir, "位置统计", safe, f"{feat}.json")
                if os.path.isfile(p):
                    return p
            return os.path.join(self.stats_dir, "位置统计", safe)
        return os.path.join(self.stats_dir, "位置统计")

    def _traffic_lane_stat(self, lane: str) -> Optional[Dict]:
        """从 位置统计/交通荷载/交通荷载.json 取 车道X 的整体统计。

        结构: {交通荷载: {车道1: {"统计": {...}, "传感器编号": "车道1", ...}}}。
        """
        if not self.stats_dir:
            return None
        p = os.path.join(self.stats_dir, "位置统计",
                         "交通荷载", "交通荷载.json")
        if not os.path.isfile(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        for _pos, points in (data or {}).items():
            if not isinstance(points, dict):
                continue
            # 列名可能是 车道1 / 交通分析车道1 / 车道1(交通分析) 等，
            # 统一取 “车道N” 与 位置统计/交通荷载/交通荷载.json 的键对齐
            m = re.search(r"车道\s*(\d+)", str(lane or ""))
            lane_key = f"车道{m.group(1)}" if m else str(lane)
            rec = points.get(lane_key)
            if isinstance(rec, dict):
                st = rec.get("统计") or {}
                return st if isinstance(st, dict) else None
        return None

    @staticmethod
    def _extract_dunhao(title: str) -> str:
        """从表格标题提取墩号，如 '4#墩墩顶主梁梁端支座位移监测统计' -> '4'。"""
        m = re.search(r"(\d+)\s*#\s*墩", title)
        return m.group(1) if m else ""

    @staticmethod
    def _match_position(query: str, positions: List[str], threshold: float = 0.6) -> Optional[str]:
        """在候选断面位置里找与标题/列名最像的一个。

        位置词（上游/下游/左/右）常被挪到句首（“上游58#墩墩顶截面”），
        而名称对照里在句尾（“58#墩墩顶截面上游”）。SequenceMatcher 把
        主体当最长公共块后，方向词分居两侧找不到匹配，导致上游/下游
        打成平手、先遍历到的候选获胜。因此先剥离方向词匹配主体，
        再按方向词是否一致加减分。
        """
        dir_words = ("上游", "下游", "左侧", "右侧", "左", "右")

        def _direction(text: str) -> str:
            for w in dir_words:
                if w in text:
                    return w
            return ""

        qn = _norm(query)
        qd = _direction(qn)
        q_sites = _site_set(qn)
        q_body = _strip_sites(_SIDE_RE.sub("", qn))
        # 精确键优先：查询与候选完全一致时直接返回，避免无方位查询
        # （如“跨中1/2截面”）被“跨中1/2截面上游/下游”等带方位变体
        # 以相同相似度先遍历到而抢走（与 _sensors_at_position 的
        # 精确键优先策略一致）。
        for pos in positions:
            if _norm(pos) == qn:
                return pos
        best, best_score = None, -1.0
        for pos in positions:
            pn = _norm(pos)
            pd = _direction(pn)
            p_sites = _site_set(pn)
            p_body = _strip_sites(_SIDE_RE.sub("", pn))
            score = difflib.SequenceMatcher(None, q_body, p_body).ratio()
            if q_sites or p_sites:
                if q_sites and q_sites == p_sites:
                    score += 0.18
                elif q_sites and p_sites:
                    score -= 0.30
                elif q_sites and not p_sites:
                    score -= 0.15
            if qd and pd:
                score += 0.15 if qd == pd else -0.25
            elif qd and not pd:
                score -= 0.10
            if score > best_score:
                best, best_score = pos, score
        return best if best is not None and best_score >= threshold else None

    def _point_plan_for_row(self, plans: List[Dict], title: str, column: str,
                            row_index: int = 0):
        """在测点映射里找与“表格标题 + 行标签”匹配的断面位置。

        行标签可能是：
          - 顶板测点1 / 底板测点3 / 腹板测点2（部位词 + 测点号）
          - 上游 / 下游（纯方位行）
          - 测点2（纯测点号）
        返回 (断面位置, 传感器编号) 或 None。
        """
        if not plans:
            return None
        t = _norm(title)
        direction = ""
        # 标题里的方位词：左幅/右幅/左侧/右侧 与 上游/下游 都要认，
        # 否则“右幅汝城侧边跨跨中截面…统计”会因方向为空串到左幅测点
        for w in ("上游", "下游", "左幅", "右幅", "左侧", "右侧"):
            if w in t:
                direction = w
                break
        col = _norm(column)
        part = ""
        point_no = ""
        # column 可能是完整位置（如“上游随州侧边跨跨中箱梁顶板测点1”），
        # 也可能是简单形式（如“顶板测点1”）：用搜索而非整串匹配
        m = re.search(r"(顶板|底板|腹板|翼板)测点\s*(\d+)$", col)
        if m:
            part, point_no = m.group(1), f"测点{m.group(2)}"
        else:
            m2 = re.match(r"^(顶板|底板|腹板|翼板)(测点\s*\d+)$", col)
            if m2:
                part, point_no = m2.group(1), m2.group(2)
        # 完整位置里也可能带方位（上游/下游），从 column 提取方向补充
        if not direction:
            for w in ("上游", "下游", "左幅", "右幅", "左侧", "右侧"):
                if w in col:
                    direction = w
                    break
        elif re.match(r"^(上游|下游|左|右)$", col):
            direction = col
        elif re.match(r"^测点\s*\d+$", col):
            point_no = col
        # 列名是“完整位置+测点N”且没有 顶板/底板 前缀时（如“吉首侧主塔
        # 中部截面测点1”），上面都提取不到，从列名尾部补提取；否则按行号
        if not point_no:
            _m3 = re.search(r"测点\s*(\d+)$", col)
            point_no = (f"测点{_m3.group(1)}"
                        if _m3 else f"测点{row_index + 1}")
        # 标题基座：去掉 方向 / 应变监测统计 等，再取核心段。
        # 先去掉带“侧”的完整方位词（上游侧/下游侧），再处理裸方位，
        # 避免“上游侧”只去掉“上游”留下孤立“侧”污染核心段。
        base = re.sub(r"(上游侧|下游侧|左幅|右幅|左侧|右侧)", "", title)
        base = re.sub(r"(上游|下游|左幅|右幅|左|右)", "", base)
        base = re.sub(r"(结构)?(应变|振动).*$", "", base)
        base = base.replace("监测", "").replace("统计", "").strip()
        core = ""
        cm = re.search(r"(.+?)(?:截面|箱梁|断面|梁段)", base)
        if cm:
            core = cm.group(1)
        elif len(base) >= 2:
            core = base

        # 方向兼容：传感器登记用 左幅/右幅、报告表格用 上游/下游 的桥
        # （如洣水河应变表“上游…应变监测统计”对应“…顶板左幅”测点）。
        # 只作为别名候选：先按原方向精确匹配，无候选时才把
        # 上游≈左幅、下游≈右幅 视为同侧，避免既有精确匹配被改写。
        def _dir_ok(pn, alias):
            if not direction:
                return True
            if direction in pn:
                return True
            if alias:
                sides = _side_set(pn)
                if (direction == "上游" and "L" in sides) \
                        or (direction == "下游" and "R" in sides) \
                        or (direction == "左幅" and "U" in sides) \
                        or (direction == "右幅" and "D" in sides):
                    return True
            return False

        def _search(alias, fuzzy):
            for plan in plans:
                pos = str(plan.get("断面位置") or "")
                pn = _norm(pos)
                if not _dir_ok(pn, alias):
                    continue
                if part and part not in pn:
                    continue
                if core and core not in pn:
                    if not (fuzzy and difflib.SequenceMatcher(
                            None, core, pn).ratio() >= 0.5):
                        continue
                pts = plan.get("测点") or {}
                sid = pts.get(point_no) if point_no else None
                if sid:
                    return pos, str(sid)
            return None

        # 0) 无方位表两侧拼接：标题/列名没有方位、核心位置只有 左幅/右幅
        # (或上游/下游) 变体、没有无方位断面时，整表按 左/上游 → 右/下游
        # 拼接（测点编号偏移），避免“吉首侧主塔中部截面测点1..8”整表串到
        # 同一侧或后半段重复上一行。
        if not direction:
            _merged = self._merged_point_plan(plans, core, part, point_no)
            if _merged:
                return _merged
        # 1) 严格匹配：基座核心 + 部位词 + 方向 都命中
        found = _search(alias=False, fuzzy=False)
        if found:
            return found
        # 2) 模糊匹配：difflib 相似度（如 顶部 vs 墩顶）
        if core:
            found = _search(alias=False, fuzzy=True)
            if found:
                return found
        # 1b) 方向别名匹配（上游≈左幅、下游≈右幅）：
        # 只有精确方向(含模糊核心)无候选时才启用，避免覆盖既有精确匹配
        found = _search(alias=True, fuzzy=False)
        if found:
            return found
        if core:
            found = _search(alias=True, fuzzy=True)
            if found:
                return found
        return None

    def _merged_point_plan(self, plans: List[Dict], core: str, part: str,
                           point_no: str):
        """无方位表两侧拼接（测点映射用）：核心位置只有 左幅/右幅
        (或上游/下游) 变体、没有无方位断面时，把两侧测点合并成一张表——
        左/上游 测点1..N 原样保留，右/下游 测点1..N 偏移到 N+1.. 之后，
        返回 point_no 对应的 (断面位置组合标签, 传感器编号)；否则 None。
        """
        if not core or not point_no:
            return None
        left_pts, right_pts = {}, {}
        side_names = []
        for plan in plans or []:
            p = str(plan.get("断面位置") or "")
            if part and part not in p:
                continue
            if core not in p:
                continue
            sides = _side_set(p)
            pts = plan.get("测点") or {}
            if not sides:
                return None   # 存在无方位断面，不拼接
            if sides & {"L", "U"}:
                left_pts.update(pts)
                side_names.append(p)
            elif sides & {"R", "D"}:
                right_pts.update(pts)
                side_names.append(p)
        if not left_pts or not right_pts:
            return None
        merged = dict(left_pts)
        offset = 0
        for k in left_pts:
            m = re.search(r"(\d+)", str(k))
            if m:
                offset = max(offset, int(m.group(1)))
        for k, v in right_pts.items():
            m = re.search(r"(\d+)", str(k))
            if m:
                merged[f"测点{offset + int(m.group(1))}"] = v
        if point_no in merged:
            return "、".join(sorted(side_names)), str(merged[point_no])
        return None

    def _merged_side_ids(self, mkey: str, pos: str):
        """表格无方位时，把 位置 的方位变体(左幅/右幅、上游/下游、左侧/右侧)
        的测点按 左/上游 → 右/下游 拼接（如 吉首侧索塔中截面测点1..8 =
        左幅4 + 右幅4）。
        仅当该核心没有“无方位键”、且确实存在两侧键时返回拼接列表；
        否则返回 None（保持原按行取号逻辑）。
        """
        keys = list((self.table_map.get(mkey) or {}).keys())
        if pos not in keys or not _position_side_words(pos):
            return None

        def _core(k):
            c = re.sub(
                r"[（(]?(?:左幅|右幅|上游|下游|左侧|右侧|左|右)[）)]?",
                "", str(k))
            return c.replace("（）", "").replace("()", "").strip()

        core = _core(pos)
        if not core:
            return None
        variants = [k for k in keys if _core(k) == core]
        if core in variants:
            return None  # 存在无方位键，应精确命中，不拼接
        left, right = [], []
        for k in variants:
            ids = [str(x) for x in self.table_map[mkey][k]]
            if any(w in k for w in ("左幅", "左侧", "上游")):
                left.extend(ids)
            elif any(w in k for w in ("右幅", "右侧", "下游")):
                right.extend(ids)
        if not left or not right:
            return None
        return left + right

    def _resolve_cell_by_table(self, metric: str, column: str, stat: str,
                               period: Dict, title: str,
                               row_index: int = 0,
                               trace: Optional[Dict] = None) -> Optional[float]:
        """按表格标题上下文解析单元格（测点映射 / 表格映射）。
        trace: 传入字典时记录命中的分支与传感器编号（供血缘日志）。"""
        if not title:
            return None
        t = title.replace("{{", "").replace("}}", "")

        # 1) 结构应变 / 结构振动表：断面位置 -> 测点N -> 编号
        for kw, mkey in (("应变", "结构应变监测表"), ("振动", "结构振动监测表")):
            if kw in t and mkey in self.point_map:
                plans = self.point_map[mkey]
                found = self._point_plan_for_row(plans, t, column, row_index)
                if found:
                    pos, sid = found
                    if trace is not None:
                        trace.update({"branch": "测点映射表", "position": pos,
                                      "sensor_id": sid, "column": column})
                    v = self._sensor_stat(str(sid), metric, stat, period)
                    if v is None and trace is not None:
                        trace["_map_blocked"] = ("测点映射表", pos, str(sid))
                    return v

        # 1b) 结构温度/温度监测表（如“君山侧塔梁交接处钢桁梁温度监测统计”）：
        #     与应变/振动一样按官方“测点映射”取传感器，避免依赖名称对照表里
        #     传感器排列顺序（不同版本名称对照顺序不一致会导致行↔传感器错位）。
        if (("结构温度" in t
             or ("温度" in t and "环境" not in t and "湿度" not in t))
                and "结构温度监测表" in self.point_map):
            plans = self.point_map["结构温度监测表"]
            # 标题里的“温度/结构温度监测统计”是表类型词，去掉后才是断面
            # 位置（如“君山侧塔梁交接处钢桁梁温度监测统计”）。
            _t_core = re.sub(r"(结构温度|温湿度|温度).*$", "", t)
            found = self._point_plan_for_row(plans, _t_core, column, row_index)
            if found:
                pos, sid = found
                if trace is not None:
                    trace.update({"branch": "结构温度测点映射表",
                                  "position": pos, "sensor_id": sid,
                                  "column": column})
                v = self._sensor_stat(str(sid), metric, stat, period,
                                      feature="WD(temp)")
                if v is None and trace is not None:
                    trace["_map_blocked"] = ("结构温度测点映射表", pos, str(sid))
                return v

        # 2) 梁端支座位移表：墩号 + 左/右
        if "位移" in t and "支座" in t and "梁端支座位移表" in self.table_map:
            dun = self._extract_dunhao(t)
            side = "左" if "左" in column else "右"
            entry = self.table_map["梁端支座位移表"]
            row = (entry.get(dun + "#") or entry.get(dun) or {})
            entry = row.get(side)
            if entry:
                if trace is not None:
                    trace.update({"branch": "梁端支座位移表", "position": f"{dun}#墩{side}",
                                  "sensor_id": str(entry.get("编号", "")), "column": column})
                return self._sensor_stat(str(entry.get("编号", "")), metric, stat, period,
                                         feature=str(entry.get("特征", "")))

        # 3) 墩顶支座倾角表：墩号 + 左/右 + X/Y
        if "倾角" in t and "墩顶支座倾角表" in self.table_map:
            dun = self._extract_dunhao(t)
            side = "左" if "左" in column else "右"
            axis = "Y" if "Y" in column else "X"
            entry = self.table_map["墩顶支座倾角表"]
            row = (entry.get(dun + "#") or entry.get(dun) or {})
            entry = row.get(side + axis)
            if entry:
                if trace is not None:
                    trace.update({"branch": "墩顶支座倾角表", "position": f"{dun}#墩{side}{axis}",
                                  "sensor_id": str(entry.get("编号", "")), "column": column})
                return self._sensor_stat(str(entry.get("编号", "")), metric, stat, period,
                                         feature=str(entry.get("特征", "")))

        # 4) 裂缝监测表：列名 -> 断面位置 -> 按表格行号取对应传感器
        if "裂缝" in t and "裂缝监测表" in self.table_map:
            pos = self._match_position(column, list(self.table_map["裂缝监测表"].keys()))
            if pos:
                ids = [str(x) for x in self.table_map["裂缝监测表"][pos]]
                if not ids:
                    return None
                # 一个监测部位有多个传感器时，按表格行号取对应传感器
                # （第1行 -> 第1个传感器，第2行 -> 第2个传感器…）
                sid = ids[row_index % len(ids)]
                if trace is not None:
                    trace.update({"branch": "裂缝监测表", "position": pos,
                                  "sensor_id": sid, "column": column})
                return self._sensor_stat(sid, metric, stat, period)

        # 5) 温湿度表 / 结构温度表：位置 -> 编号列表
        for kw, mkey in (("结构温度", "结构温度表"), ("温湿度", "温湿度表")):
            if kw in t and mkey in self.table_map:
                pos = self._match_position(column, list(self.table_map[mkey].keys()))
                if pos is None:
                    # column 带“上游…箱梁顶板测点1/底板测点3”等组合时，
                    # 去掉“测点N”后缀，再在表格映射里模糊匹配位置
                    col_no_pt = re.sub(r"(顶板|底板|腹板|翼板)?测点\s*\d+$", "",
                                       column).strip(" 、，,和及")
                    if col_no_pt and len(col_no_pt) >= 2:
                        pos = self._match_position(
                            col_no_pt, list(self.table_map[mkey].keys()))
                if pos:
                    ids = [str(x) for x in self.table_map[mkey][pos]]
                    feat = "WD(temp)" if mkey == "结构温度表" else ""
                    # 表格无方位、但匹配到的位置键带方位（左幅/右幅等），且
                    # 没有无方位键时：把两侧测点拼接后按行取（如 吉首侧索塔
                    # 中截面测点1..8 = 左幅4 + 右幅4），避免整表填成同一侧
                    if (not _position_side_words(column)
                            and _position_side_words(pos)):
                        merged = self._merged_side_ids(mkey, pos)
                        if merged:
                            ids = merged
                    if not ids:
                        return None
                    # 一个监测部位有多个传感器时，按表格行号取对应传感器
                    # （第1行 -> 第1个传感器，第2行 -> 第2个传感器…）
                    sid = ids[row_index % len(ids)]
                    if trace is not None:
                        trace.update({"branch": f"{mkey}", "position": pos,
                                      "sensor_id": sid, "column": column})
                    return self._sensor_stat(sid, metric, stat, period, feature=feat)

        # 6) 地震监测表：位置 -> 编号（方向列按轴特征取对应特征）
        if "地震" in t and "地震监测表" in self.table_map:
            pos = self._match_position(column,
                                       list(self.table_map["地震监测表"].keys()))
            if pos:
                ids = [str(x) for x in self.table_map["地震监测表"][pos]]
                if not ids:
                    return None
                sid = ids[row_index % len(ids)]
                feat = ""
                axis = self._axis_features_at_position(pos, metric)
                if axis:
                    feat = axis[row_index % len(axis)]
                if trace is not None:
                    trace.update({"branch": "地震监测表", "position": pos,
                                  "sensor_id": sid, "column": column})
                return self._sensor_stat(sid, metric, stat, period,
                                         feature=feat or None)
        return None

    def _period_daily(self, fstats: Dict, period: Dict) -> List[Dict]:
        """取报告期内的每日统计列表。"""
        start = period.get("start")
        end = period.get("end")
        daily = fstats.get("每日统计", []) or []
        if not self.period_aggregate or not start or not end:
            return daily
        out = []
        for d in daily:
            try:
                day = dt.date.fromisoformat(str(d.get("日期", "")))
            except (ValueError, TypeError):
                continue
            if start <= day <= end:
                out.append(d)
        return out

    def _clean_daily(self, daily: List[Dict], fstats: Optional[Dict], stat: str) -> List[Dict]:
        """剔除缺失/异常日：全零日、0 污染日、以及数量级异常的尖峰日。"""
        if not (self.cfg.get("zero_cleanup", True) or self.cfg.get("spike_cleanup", True)):
            return daily
        overall_avg = None
        if fstats:
            try:
                overall_avg = float(fstats.get("平均值"))
            except (TypeError, ValueError):
                overall_avg = None
        # 尖峰阈值：同时用 绝对倍数 与 稳健 MAD 两种规则
        spike_thr = None
        if self.cfg.get("spike_cleanup", True):
            abs_maxs = []
            for d in daily:
                for k in ("最大值", "最小值"):
                    try:
                        v = abs(float(d.get(k)))
                    except (TypeError, ValueError):
                        continue
                    if v > 0:
                        abs_maxs.append(v)
            if abs_maxs:
                sorted_v = sorted(abs_maxs)
                med = sorted_v[len(sorted_v) // 2]
                p95 = sorted_v[min(len(sorted_v) - 1, int(len(sorted_v) * 0.95))]
                mad = statistics.median([abs(v - med) for v in abs_maxs])
                thr_abs = max(med * 1000.0, p95 * 10.0)
                thr_mad = med + max(30.0 * mad, p95 * 3.0)
                # 取更严格（较小）的阈值：任一规则命中即视为尖峰
                spike_thr = max(min(thr_abs, thr_mad), 1e-9)
        out = []
        for d in daily:
            try:
                mx, mn, av = float(d.get("最大值")), float(d.get("最小值")), float(d.get("平均值"))
            except (TypeError, ValueError):
                continue
            if self.cfg.get("zero_cleanup", True) and mx == 0 and mn == 0 and av == 0:
                continue  # 全天无数据（缺失记 0）
            if self.cfg.get("zero_cleanup", True) and stat == "max" and overall_avg is not None and overall_avg < 0 and mx == 0:
                continue  # 负值传感器：每日最大值 0 来自缺失小时
            if self.cfg.get("zero_cleanup", True) and stat == "min" and overall_avg is not None and overall_avg > 0 and mn == 0:
                continue  # 正值传感器：每日最小值 0 来自缺失小时
            if spike_thr is not None and (abs(mx) > spike_thr or abs(mn) > spike_thr):
                continue  # 数量级异常的尖峰日（传感器数据毛刺）
            out.append(d)
        return out

    def _aggregate_daily(self, daily: List[Dict], stat: str,
                         fstats: Optional[Dict] = None,
                         clean: bool = True) -> Optional[float]:
        """把每日统计聚合成报告期统计量。

        clean=False 时不做额外的“零值/尖峰日”剔除（供报告只读回填使用：
        库里有的数原样聚合，清洗在预处理已做）。
        """
        stat = _canon_stat(stat)
        if clean:
            daily = self._clean_daily(daily, fstats, stat)
        if not daily:
            return None
        means = [float(d["平均值"]) for d in daily if d.get("平均值") is not None]
        maxs = [float(d["最大值"]) for d in daily if d.get("最大值") is not None]
        mins = [float(d["最小值"]) for d in daily if d.get("最小值") is not None]
        if stat in ("avg", "mean", "value"):
            vals = means or maxs or mins
            return sum(vals) / len(vals) if vals else None
        if stat == "max":
            return max(maxs) if maxs else (max(means) if means else None)
        if stat == "min":
            return min(mins) if mins else (min(means) if means else None)
        if stat == "abs_max":
            vals = [(m, "max") for m in maxs] + [(n, "min") for n in mins]
            if not vals:
                return None
            return max(vals, key=lambda p: abs(p[0]))[0]
        if stat == "range":
            if maxs and mins:
                return max(maxs) - min(mins)
            return None
        if stat == "rms":
            vals = means or []
            return math.sqrt(sum(v * v for v in vals) / len(vals)) if vals else None
        if stat == "median":
            vals = means or []
            return statistics.median(vals) if vals else None
        if stat == "std":
            vals = means or []
            return statistics.pstdev(vals) if len(vals) > 1 else 0.0
        if stat in ("count", "days"):
            return float(len(daily))
        return None

    def _full_period_stats(self, fstats: Dict, stat: str) -> Optional[float]:
        """读取 JSON 内的整体统计值（预处理已清洗，报告直接回填）。

        统计量名兼容三种写法：规范键(avg/max/…)、中文标准名(平均值/最大值)、
        表头式名称(平均温度/最高温度/最大温差…)。
        """
        key = STAT_KEY_MAP.get(stat)
        if key is None:
            can = _canon_stat(stat)
            key = STAT_KEY_MAP.get(can, can)
        if key and fstats.get(key) is not None:
            return float(fstats[key])
        return None

    def resolve_cell(self, metric: str, column: str, stat: str, period: Dict,
                     table_title: str = "", row_index: int = 0) -> Optional[float]:
        """解析 {{cell.<metric>.<column>.<stat>}}，可带表格标题上下文。"""
        value, _detail = self.resolve_cell_detail(metric, column, stat, period,
                                                  table_title=table_title,
                                                  row_index=row_index)
        return value

    def resolve_cell_detail(self, metric: str, column: str, stat: str, period: Dict,
                            table_title: str = "", row_index: int = 0):
        """解析 {{cell.<metric>.<column>.<stat>}}，返回 (值, 数据链路明细)。"""
        actual = _canon_stat(STAT_KEY_MAP.get(stat, stat))
        trace: Dict = {}
        val = None
        # 0) 交通荷载(车辆计数)：cell.vehicle_count.车道X.count / .ratio
        #    数据源 统计值_<期>/<桥名>/位置统计/交通荷载/交通荷载.json（车道X 为键）
        if metric == "vehicle_count":
            st = self._traffic_lane_stat(column)
            if st:
                if stat in ("count", "数值"):
                    val = st.get("数值")
                elif stat in ("ratio", "比例"):
                    val = st.get("比例")
            if val is not None:
                detail = {
                    "占位符": f"cell.{metric}.{column}.{stat}",
                    "指标": metric, "统计量": stat,
                    "报告期": f"{period.get('start')} ~ {period.get('end')}",
                    "分支": "交通荷载位置统计库(车道X)",
                    "监测部位": "交通荷载", "表格标题": table_title,
                    "表格行号": row_index + 1,
                    "传感器": {"传感器编号": column, "监测部位": "交通荷载",
                              "特征": "交通荷载",
                              "统计文件": os.path.join(
                                  self.stats_dir, "位置统计",
                                  "交通荷载", "交通荷载.json"),
                              "数据来源": "位置统计库(交通荷载)",
                              "天数": 0, "值": val},
                    "最终值": val,
                }
                return val, detail
            return None, {
                "占位符": f"cell.{metric}.{column}.{stat}",
                "结果": "未找到",
                "原因": f"交通荷载 {column} 无 数值/比例 统计"
                        f"（位置统计/交通荷载/交通荷载.json）",
                "分支": "交通荷载位置统计库", "监测部位": column,
                "表格标题": table_title, "表格行号": row_index + 1,
            }
        # 0) 表格上下文映射（测点N / 左侧右侧 / 左X右X 等）
        if table_title:
            val = self._resolve_cell_by_table(metric, column, actual, period, table_title,
                                              row_index=row_index, trace=trace)
            if val is not None:
                self._match_stats["table_map"] = self._match_stats.get("table_map", 0) + 1
            elif trace.get("_map_blocked"):
                # 官方测点映射已命中该传感器，但该传感器恒值/无统计：
                # 整行填“—”，不回退到同位置其它传感器（否则坏点会串成
                # 同位置其它测点的值，如 温度测点12 恒值 3296 被 3286 顶替）。
                _blk = trace.pop("_map_blocked")
                _br, _pos, _sid = _blk[0], _blk[1], _blk[2]
                return None, {
                    "占位符": f"cell.{metric}.{column}.{stat}",
                    "结果": "未找到",
                    "原因": f"测点 {column} 对应传感器({_sid})恒值或"
                            f"无统计数据（官方测点映射命中），整行填“—”",
                    "分支": _br,
                    "监测部位": _pos,
                    "表格标题": table_title,
                    "表格行号": row_index + 1,
                    "传感器": {
                        "传感器编号": _sid,
                        "监测部位": _pos,
                    },
                }
        # 1) 通用位置多传感器：column 为监测部位名时按表格行号取该位置第 N 个传感器
        if val is None:
            pos = self._match_position(column, list(self.name_dict.keys()))
            if pos:
                # 方向行（X/Y/Z 向）优先按“轴特征”选择：
                # 如 displacement 的 GNSS(Δx/Δy/Δz)、earthquake_load 的
                # SZJSD(xJsd/yJsd/zJsd) —— 同一传感器的多个轴分量特征，
                # 表格第 N 行对应第 N 个轴特征，避免三行都取第一个特征。
                axis_feats = self._axis_features_at_position(pos, metric)
                if axis_feats and len(axis_feats) >= 2:
                    idx = row_index % len(axis_feats)
                    feat = axis_feats[idx]
                    sids = self._sensors_at_position(pos, metric)
                    if sids:
                        sid = str(sids[row_index % len(sids)])
                        trace.update({"branch": "位置-轴特征按方向取",
                                      "position": pos, "sensor_id": sid,
                                      "feature": feat, "column": column})
                        val = self._sensor_stat(sid, metric, actual, period,
                                                feature=feat)
                        if val is not None:
                            self._match_stats["name_dict"] = \
                                self._match_stats.get("name_dict", 0) + 1
                # 列名带明确轴向(X/Y向)时：该位置实际没有对应轴向数据
                # （如转角只存 EZJD(xJd)），Y 行不许拿 X 值重复填充 → 整行“—”
                if (val is None and axis_feats
                        and re.search(r"[XY](?:向)?$", str(column))):
                    _want = "y" if re.search(r"Y(?:向)?$", str(column)) \
                        else "x"
                    if not any(
                            (_axis_inner(f) or "").lower().startswith(_want)
                            for f in axis_feats):
                        return None, {
                            "占位符": f"cell.{metric}.{column}.{stat}",
                            "结果": "未找到",
                            "原因": f"测点 {column} 该位置无 {_want} 向轴数据"
                                    f"，整行填“—”",
                            "分支": "位置-轴特征按方向取",
                            "监测部位": pos,
                            "表格标题": table_title,
                            "表格行号": row_index + 1,
                        }
                if val is None:
                    sids = self._sensors_at_position(pos, metric)
                    if sids:
                        # 同一位置多个传感器时优先按行号取，若该传感器无对应
                        # 特征统计值（如对照表写 DZJSD 但实际数据是 SZJSD），
                        # 顺延到该位置下一个有值的传感器；第一轮只接受真实
                        # 逐传感器统计（跳过“季度/年度聚合”回退值，回退值会
                        # 把全桥聚合复制到每个缺失传感器上，导致多行同值）。
                        order = sids[row_index % len(sids):] + sids[:row_index % len(sids)]
                        # 恒值传感器（整季恒0/恒值）：该测点整行填“—”，
                        # 不“顺延”到同位置其他传感器，避免坏测点串成好测点值
                        if order:
                            _primary = str(order[0])
                            _feat = self.metrics.get(metric, {}).get("feature", "")
                            _pf = self._feature_stats(_primary, metric,
                                                      feature=_feat)
                            if _pf and self._constant_faulty(_pf, _feat):
                                return None, {
                                    "占位符": f"cell.{metric}.{column}.{stat}",
                                    "结果": "未找到",
                                    "原因": f"测点 {column} 对应传感器恒值"
                                            f"(疑似故障)，整行填“—”",
                                    "分支": "名称对照位置-按行取传感器",
                                    "监测部位": pos,
                                    "表格标题": table_title,
                                    "表格行号": row_index + 1,
                                    "传感器": {
                                        "传感器编号": _primary,
                                        "监测部位": self._position_for_sensor(
                                            _primary),
                                        "特征": _feat,
                                    },
                                }
                            # 行号取测点(column 含“测点N”)且主传感器既无逐
                            # 传感器统计也无聚合统计时，整行填“—”，不再顺延
                            # 到同位置其他传感器——否则缺数/坏测点会串成
                            # 上一行同值（如 测点2 重复 测点1 的值）。
                            if (self._pos_stats
                                    and str(_primary) not in self._pos_stats
                                    and re.search(
                                        r"测点\s*\d+", str(column))):
                                # 传感器不在位置统计库中（整季无数据，或逐
                                # 传感器 JSON 残留旧版假值）：整行“—”，
                                # 不回退到逐传感器 JSON / 聚合统计
                                return None, {
                                    "占位符": f"cell.{metric}.{column}.{stat}",
                                    "结果": "未找到",
                                    "原因": f"测点 {column} 对应传感器"
                                            f"({_primary})不在位置统计库中"
                                            f"(整季无数据)，整行填“—”",
                                    "分支": "名称对照位置-按行取传感器"
                                            "(位置统计库无此传感器)",
                                    "监测部位": pos,
                                    "表格标题": table_title,
                                    "表格行号": row_index + 1,
                                    "传感器": {
                                        "传感器编号": _primary,
                                        "监测部位": self._position_for_sensor(
                                            _primary),
                                        "特征": _feat,
                                    },
                                }
                            if _pf is None and re.search(
                                    r"测点\s*\d+", str(column)) and \
                                    self._aggregate_sensor_stat(
                                        _primary, metric, actual,
                                        feature=_feat) is None:
                                return None, {
                                    "占位符": f"cell.{metric}.{column}.{stat}",
                                    "结果": "未找到",
                                    "原因": f"测点 {column} 对应传感器"
                                            f"({_primary})无统计数据，"
                                            f"整行填“—”",
                                    "分支": "名称对照位置-按行取传感器(缺数不串行)",
                                    "监测部位": pos,
                                    "表格标题": table_title,
                                    "表格行号": row_index + 1,
                                    "传感器": {
                                        "传感器编号": _primary,
                                        "监测部位": self._position_for_sensor(
                                            _primary),
                                        "特征": _feat,
                                    },
                                }
                        if val is None:
                            for sid in order:
                                d_try = self._stat_detail(sid, metric, actual, period)
                                if d_try is not None and \
                                        d_try.get("数据来源") != "季度/年度聚合统计":
                                    sid = str(sid)
                                    trace.update({"branch": "名称对照位置-按行取传感器",
                                                  "position": pos, "sensor_id": sid,
                                                  "column": column})
                                    val = d_try["值"]
                                    break
                        if val is None:
                            for sid in order:
                                v_try = self._sensor_stat(sid, metric, actual, period)
                                if v_try is not None:
                                    sid = str(sid)
                                    trace.update({"branch": "名称对照位置-按行取传感器(回退聚合)",
                                                  "position": pos, "sensor_id": sid,
                                                  "column": column})
                                    val = v_try
                                    break
                        if val is not None:
                            self._match_stats["name_dict"] = self._match_stats.get("name_dict", 0) + 1
        # 2) find_sensor 直接命中
        if val is None:
            sensor_id = self.find_sensor(metric, column)
            if sensor_id:
                trace.update({"branch": "find_sensor", "sensor_id": str(sensor_id),
                              "column": column})
                val = self._sensor_stat(sensor_id, metric, actual, period)
                if val is not None:
                    trace.update({"position": self._position_for_sensor(sensor_id)})
        # 3) 找不到传感器：不聚合全指标（否则每行都填同一个值），
        #    返回 None 由上层填“—”并在血缘日志写明缺失原因
        if val is None:
            detail = {
                "占位符": f"cell.{metric}.{column}.{stat}",
                "结果": "未找到",
                "原因": (f"表格[{table_title}] 行[{row_index + 1}] 找不到 "
                         f"{metric}/{column} 对应的传感器或统计值（已关闭全指标回退）"),
                "分支": trace.get("branch", "未命中任何映射"),
                "表格标题": table_title,
                "表格行号": row_index + 1,
            }
            return None, detail
        # 组装常规明细
        sensor_id = trace.get("sensor_id")
        d = None
        if sensor_id:
            d = self._stat_detail(sensor_id, metric, actual, period)
        detail = {
            "占位符": f"cell.{metric}.{column}.{stat}",
            "指标": metric,
            "统计量": stat,
            "报告期": f"{period.get('start')} ~ {period.get('end')}",
            "分支": trace.get("branch", ""),
            "监测部位": trace.get("position") or column,
            "表格标题": table_title,
            "表格行号": row_index + 1,
            "传感器": d,
            "最终值": val,
        }
        return val, detail

    def resolve_metric_stat(self, metric: str, stat: str, period: Dict) -> Optional[float]:
        """解析 {{stats.<metric>.<stat>}}：对该指标全部传感器聚合。"""
        value, _detail = self.resolve_metric_stat_detail(metric, stat, period)
        return value

    def resolve_metric_stat_detail(self, metric: str, stat: str, period: Dict):
        """解析 {{stats.<metric>.<stat>}}，返回 (值, 数据链路明细)。

        明细包含：报告期、聚合规则、每个传感器的统计文件/天数/数值，
        供 data_lineage 日志使用。
        """
        actual = _canon_stat(STAT_KEY_MAP.get(stat, stat))
        per_sensor = []
        # 方向化指标（如 displacement_x/y/z、vibration_x 等）：
        # 只统计对应方向特征（GNSS(Δx/Δy/Δz)、SZJSD/DZJSD(x/y/z)）
        metric_dir = ""
        mdir = re.match(r"^(.*)_([xyz])$", metric)
        if mdir:
            metric_dir = mdir.group(2).upper()
            metric = mdir.group(1)
        dir_feature = ""
        if metric_dir:
            dir_feature = {
                "X": ("GNSS(Δx)", "SZJSD(xJsd)", "DZJSD(xJsd)", "EZJD(xJd)"),
                "Y": ("GNSS(Δy)", "SZJSD(yJsd)", "DZJSD(yJsd)", "EZJD(yJd)"),
                "Z": ("GNSS(Δz)", "SZJSD(zJsd)", "DZJSD(zJsd)"),
            }.get(metric_dir, ())
        family_sids = self._sensors_for_metric_family(metric) if metric_dir \
            else self.sensors_for_metric(metric)
        for sid in family_sids:
            if metric_dir:
                feats = self._sensor_features.get(str(sid), []) or []
                # 方向 x/y/z 匹配括号内编码：Δx/x/xJsd/yJsd/zJsd 等
                # X->Δx/x/xJsd；Y->Δy/y/yJsd；Z->Δz/z/zJsd
                # 比较时统一小写（Δ 大写保持，避免 δx 误判）
                axis_want = {"X": {"Δx", "x", "xjsd", "xjd"},
                             "Y": {"Δy", "y", "yjsd", "yjd"},
                             "Z": {"Δz", "z", "zjsd"}}[metric_dir]
                if not any(
                        _axis_inner(f) and
                        _axis_inner(f).lower().replace("δ", "Δ") in axis_want
                        for f in feats):
                    continue
                # 找到该传感器对应方向的实际特征（GNSS(Δx)/SZJSD(xJsd)…）
                dir_feat = next((f for f in feats
                                 if _axis_inner(f) and
                                 _axis_inner(f).lower().replace("δ", "Δ") in axis_want), "")
            else:
                dir_feat = ""
            d = self._stat_detail(sid, metric, actual, period,
                                  feature=dir_feat)
            if d:
                per_sensor.append(d)
        if not per_sensor:
            return None, {
                "占位符": f"stats.{metric}.{stat}",
                "结果": "未找到",
                "原因": f"指标 {metric} 无可用传感器统计值",
            }
        # 极值/差值聚合前剔除“0污染”测点（与季度统计口径一致）：
        # 非“0为正常值”的特征，某测点 最小值==0 且 最大值>0，说明 0 来自
        # 故障段（如 635 结构温度存在恒0时间段），会把 min=0 / range 拉偏，
        # 报告里出现“温度最低为0℃”。整季恒值故障已在 _stat_detail 排除。
        if actual in ("min", "range"):
            _feat = dir_feat or self.metrics.get(metric, {}).get("feature", "")
            _m = re.search(r"\(([^)]+)\)$", str(_feat or ""))
            _code = (_m.group(1) if _m else "").lower()
            _zero_ok = _code in ("nd", "spfs", "szfs") \
                or str(_feat or "").upper().startswith("LF")
            if not _zero_ok:
                _clean = []
                for _d in per_sensor:
                    _sid = str(_d.get("传感器编号") or "")
                    _fs = (self._feature_stats(_sid, metric, feature=_feat)
                           if _sid else None)
                    _polluted = False
                    if _fs:
                        try:
                            _mx = float(_fs.get("最大值"))
                            _mn = float(_fs.get("最小值"))
                        except (TypeError, ValueError):
                            _mx = _mn = None
                        _polluted = (_mx is not None and _mn is not None
                                     and _mn == 0.0 and _mx > 0.0)
                    if not _polluted:
                        _clean.append(_d)
                if _clean:
                    per_sensor = _clean
        vals = [d["值"] for d in per_sensor]
        if actual in ("temp_rm_max", "剔除温度最大值"):
            value, rule = max(vals), "跨传感器取剔除温度残差最大"
        elif actual in ("temp_rm_min", "剔除温度最小值"):
            value, rule = min(vals), "跨传感器取剔除温度残差最小"
        elif actual == "max":
            value, rule = max(vals), "跨传感器取最大"
        elif actual == "min":
            value, rule = min(vals), "跨传感器取最小"
        elif actual == "abs_max":
            value, rule = max(vals, key=abs), "跨传感器取绝对值最大"
        elif actual == "range":
            value, rule = max(vals), "跨传感器取最大差值（各传感器报告期内最大-最小）"
        elif actual in ("temp_rm_range", "剔除温度差值"):
            value, rule = max(vals), "跨传感器取剔除温度残差最大差值"
        elif actual == "sum":
            value, rule = sum(vals), "跨传感器求和"
        elif actual in ("count", "days"):
            value, rule = sum(vals), "跨传感器求和"
        else:
            value, rule = sum(vals) / len(vals), "跨传感器取平均"
        detail = {
            "占位符": f"stats.{metric}.{stat}",
            "指标": metric,
            "统计量": stat,
            "报告期": f"{period.get('start')} ~ {period.get('end')}",
            "聚合规则": rule,
            "传感器数": len(per_sensor),
            "逐传感器": per_sensor,
            "最终值": value,
        }
        # 最值/差值对应的监测部位（供 {{stats.<metric>.<stat>.loc}} 使用）
        # 优先按“值 == 最终极值”的逐传感器定位（数值与位置必然对应），
        # 且跳过“季度/年度聚合统计”回退项——回退项会把聚合值复制到每个缺失
        # 传感器的头上，导致多个传感器同值、最值位置取到第一个而失真；
        # 逐传感器匹配不到时（如聚合来自季度统计回退）再回退 全桥统计 位置键，
        # 避免出现“值来自某传感器、位置却取到另一测点”的错位。
        feat_for_loc = (dir_feat if metric_dir
                        else self.metrics.get(metric, {}).get("feature", ""))
        loc = ""
        real = [d for d in per_sensor
                if d.get("数据来源") != "季度/年度聚合统计"]
        pool = real or per_sensor
        for d in pool:
            if abs(d["值"] - value) < 1e-9:
                loc = d.get("监测部位") or ""
                break
        if not loc:
            loc = self._agg_feature_location(feat_for_loc, actual, period)
        if loc:
            detail["位置"] = loc
        return value, detail

    def estimate_days(self, period: Dict) -> int:
        """估算报告期内的数据覆盖天数（用于 {{stats.days}} 等占位符）。"""
        best = 0
        for metric, mcfg in self.metrics.items():
            if not mcfg.get("feature"):
                continue
            for sid in self.sensors_for_metric(metric)[:3]:
                fstats = self._feature_stats(sid, metric)
                if not fstats:
                    continue
                daily = self._period_daily(fstats, period)
                if daily:
                    best = max(best, len(daily))
        return best

    # ------------------------------------------------------------------
    # 图表解析
    # ------------------------------------------------------------------

    def _parse_chart_id(self, chart_id: str):
        """解析图表占位符：<metric>_<kind>_<n>。指标名可能含下划线，按已知指标优先匹配。"""
        for metric in sorted(self.metrics, key=len, reverse=True):
            prefix = metric + "_"
            if chart_id.startswith(prefix):
                rest = chart_id[len(prefix):]
                m = re.match(r"^(?P<kind>[a-z_]+)_(?P<n>\d+)$", rest)
                if m:
                    return metric, m.group("kind"), int(m.group("n"))
        m = re.match(r"^(?P<kind>[a-z_]+)_(?P<n>\d+)$", chart_id)
        if m:
            kind = m.group("kind")
            if kind.startswith("chart_"):
                kind = kind[len("chart_"):]
            return None, kind, int(m.group("n"))
        return None

    def display_name_for(self, sensor_id: str, chart_id: str, kind: str = "",
                         metric_for_label: str = "") -> str:
        """生成图名：监测部位 + 指标名 + 图型（如“第6跨跨中断面主梁箱内环境温度时程曲线图”）。"""
        info = self.sensor_map.get(str(sensor_id), {})
        loc = info.get("名称") or info.get("监测部位") or str(sensor_id)
        parsed = self._parse_chart_id(chart_id)
        metric = metric_for_label or (parsed[0] if parsed else None)
        label = self.metrics.get(metric, {}).get("label", "") if metric else ""
        if not label:
            label = info.get("类别", "")
        if not kind and parsed:
            kind = parsed[1]
        type_name = {
            "trend": "时程曲线图",
            "timeseries": "时程曲线图",
            "time_series": "时程曲线图",
            "histogram": "频率分布直方图",
            "hist": "频率分布直方图",
            "scatter": "散点图",
            "bar": "柱状图",
            "box": "箱线图",
        }.get(kind, "时程曲线图")
        return f"{loc}{label}{type_name}"

    def _metric_alias_hit(self, caption: str) -> Optional[str]:
        """在图注/上下文中找指标名（含配置的 label / feature / aliases）。"""
        for name, mcfg in self.metrics.items():
            aliases = [name, mcfg.get("label", ""), mcfg.get("feature", "")]
            aliases += list(mcfg.get("aliases", []) or [])
            if any(a and len(a) >= 2 and _norm(a) in _norm(caption) for a in aliases):
                return name
        return None

    @staticmethod
    def _has_spans(location: str) -> bool:
        """位置词是否为多跨组合（如 “第6、7跨跨中断面”）。"""
        m = re.search(r"第([\d、，,和及]+)跨", location)
        return bool(m and len(re.findall(r"\d+", m.group(1))) >= 2)

    def _location_sensors(self, metric: str, location: str) -> List[str]:
        """返回某位置相关的传感器编号（优先表格映射，其次名称匹配）。"""
        loc_n = _norm(location)
        out = []
        # 0) 多跨组合位置（“第6、7跨跨中断面”）-> 逐跨展开（第6跨->102，第7跨->105）
        m = re.search(r"第([\d、，,和及]+)跨(.{0,14})", location)
        if m:
            spans = re.findall(r"\d+", m.group(1))
            suffix = m.group(2)
            if len(spans) >= 2:
                for s in spans:
                    for sid, info in self.sensor_map.items():
                        if self._is_excluded(sid):
                            continue
                        names = [info.get("名称", ""), info.get("监测部位", "")]
                        if any(n and f"第{s}跨" in _norm(n) and _norm(suffix) in _norm(n)
                               for n in names):
                            out.append(sid)
                            break
                if out:
                    return out
        # 0b) 多墩组合位置（“3、4#墩承台”）-> 逐墩展开（3#墩承台 / 4#墩承台）
        mm = re.match(r"^([\d、，,和及]+)#(?:柱)?墩(.*)$", loc_n)
        if mm and len(re.findall(r"\d+", mm.group(1))) >= 2:
            suffix = _norm(mm.group(2))
            for d in re.findall(r"\d+", mm.group(1)):
                for sid, info in self.sensor_map.items():
                    if self._is_excluded(sid):
                        continue
                    names = [info.get("名称", ""), info.get("监测部位", "")]
                    if any(n and f"{d}#墩" in _norm(n)
                           and (not suffix or suffix in _norm(n))
                           for n in names):
                        out.append(sid)
                        break
            if out:
                return out
        # 墩顶支座倾角表：位置含 '#墩' 时按墩号取 左X/右X
        if "墩" in loc_n:
            m = re.search(r"(\d+)#", location)
            if m and "墩顶支座倾角表" in self.table_map:
                dun = m.group(1)
                for k in ("左X", "右X"):
                    entry = self.table_map["墩顶支座倾角表"]
                    row = (entry.get(dun + "#") or entry.get(dun) or {})
                    e = row.get(k)
                    if e:
                        out.append(str(e.get("编号", "")))
        if out:
            return out
        # 裂缝监测表：位置 -> 该位置全部裂缝传感器（图按顺序分配）
        if metric == "crack" and "裂缝监测表" in self.table_map:
            pos = self._match_position(location, list(self.table_map["裂缝监测表"].keys()))
            if pos:
                return [str(x) for x in self.table_map["裂缝监测表"][pos]]
        # 包含匹配：收集该位置的全部传感器（按指标特征过滤），
        # 如“跨中断面” -> 第6跨主梁箱内、第7跨主梁箱内、第7跨桥面右侧
        feat = self.metrics.get(metric, {}).get("feature", "")
        cat = self.metric_category.get(metric, "")
        g0 = feature_group(feat) if feat else ""
        pos_all = []
        pos_cat = []
        pos_feat = []
        for sid, info in self.sensor_map.items():
            if self._is_excluded(sid):
                continue
            names = [info.get("名称", ""), info.get("监测部位", "")]
            if any(n and len(n) >= 2 and (
                (_norm(n) in loc_n) or (len(loc_n) >= 2 and loc_n in _norm(n))
            ) for n in names):
                pos_all.append(sid)
                if cat and (str(info.get("类别") or "") == cat
                            or sid in (self._category_sensors.get(cat) or [])):
                    pos_cat.append(sid)
                if feat:
                    feats = self._sensor_features.get(sid, [])
                    if feat in feats or (g0 and any(
                            feature_group(f) == g0 for f in feats)):
                        pos_feat.append(sid)
        # 类别优先：config 特征可能写错/过时（如地震配 DZJSD(xJsd)、实际
        # SZJSD(xJsd/yJsd/zJsd)），同位置还常混有振动/空间变位传感器，
        # 先限定监测类别，再在类别内做特征同族匹配；类别内无同族时整类返回
        # （保证地震节取地震传感器、振动节取振动传感器）。
        if cat and pos_cat:
            if g0:
                matched = [sid for sid in pos_cat
                           if any(feature_group(f) == g0
                                  for f in self._sensor_features.get(sid, []))]
                out = matched or pos_cat
            else:
                out = pos_cat
        elif pos_feat:
            out = pos_feat
        elif pos_all:
            out = pos_all
        else:
            out = []
        return sorted(set(out), key=lambda x: int(x) if x.isdigit() else x)

    def _chart_sensor_id(self, chart_id: str, caption: str = "",
                         context=None) -> Optional[str]:
        """按占位符 ID / 图注推断传感器编号。"""
        if chart_id in self.chart_map:
            return str(self.chart_map[chart_id])

        ctx_texts = context if isinstance(context, (list, tuple)) else (
            [context] if context else [])
        side = self._context_side(ctx_texts, caption)

        # 0) 精确传感器占位符：chart_sensor_<编号>_<图型>
        #    （如 chart_sensor_304_trend / chart_sensor_184_histogram，
        #      由“304(xJsd)_时程曲线”等行识别生成）
        m0 = re.match(r"^chart_sensor_(\d+)_([a-z_]+)$", chart_id)
        if m0:
            sid = str(m0.group(1))
            if sid in self.sensor_map or sid in self._sensor_features:
                return sid

        # 0) 位置化占位符：<metric>_<监测部位>_<kind>_<n>（如 strain_4#墩底部_trend_1）
        pp = self._parse_position_chart_id(chart_id)
        if pp:
            metric, pos, _kind, n = pp
            # 模板图表 ID 的指标可能与节上下文冲突：识别器把地震节的
            # “304(xJsd)_时程曲线”生成成 vibration_…（xJsd 特征码被当成
            # 振动）。地震/振动同族且上下文给出相反指标时，以节上下文为准，
            # 否则 58# 地震会取到同位置的振动传感器（DZJSD）。
            _ctx_text = " ".join(
                [str(caption or "")] +
                [str(x) for x in (ctx_texts or [])]).strip()
            _ctx_metric = self._metric_alias_hit(_ctx_text)
            if (_ctx_metric in ("earthquake_load", "vibration")
                    and metric in ("earthquake_load", "vibration")
                    and _ctx_metric != metric):
                metric = _ctx_metric
            sids = self._sensors_at_position(pos, metric)
            if sids:
                if side:
                    # 同一位置分左右幅/上下游时，按节上下文定向，
                    # 避免“右幅”节取到“左幅”传感器
                    sided = [s for s in sids
                             if side in (self._position_for_sensor(s) or "")]
                    if sided:
                        sids = sided
                return sids[(n - 1) % len(sids)]

        text = " ".join([caption] + [str(x) for x in ctx_texts if x]).strip()
        parsed = self._parse_chart_id(chart_id)
        metric_from_id = parsed[0] if parsed else None
        # 图注/上下文里识别指标（如“倾角” -> rotation）
        found_metric = self._metric_alias_hit(text) or metric_from_id

        # 1) 从图注/上下文提取位置（如 “4#墩墩顶主梁支座” / “第6、7跨跨中断面”）。
        #    多跨组合优先取上下文（“第6、7跨…如下图所示”这句），其次图注本身，
        #    最后上下文里的描述性图注。
        location = ""
        for ctx in ctx_texts:
            loc = self._extract_location(str(ctx))
            if loc and self._has_spans(loc):
                location = loc
                break
        if not location:
            location = self._extract_location(caption)
        if not location:
            for ctx in ctx_texts:
                location = self._extract_location(str(ctx))
                if location:
                    break
        if location:
            loc_sids = self._location_sensors(found_metric or "temperature", location)
            if loc_sids:
                if side:
                    sided = [s for s in loc_sids
                             if side in (self._position_for_sensor(s) or "")]
                    if sided:
                        loc_sids = sided
                # 按 (指标, 位置, 图型) 分别计数：
                # 时程图 1/2 -> 传感器 1/2，直方图 3/4 -> 传感器 1/2
                kind = parsed[1] if parsed else "trend"
                key = (found_metric or "?", _norm(location), kind)
                idx = self._chart_seq.get(key, 0)
                self._chart_seq[key] = idx + 1
                # 同一位置多个传感器时按监测部位分组再取序号，
                # 避免 5#/6#/7#/8#塔梁交接处主梁 等场景前两个序号都落在 5#
                by_pos = {}
                for sid in loc_sids:
                    p = self._position_for_sensor(sid)
                    by_pos.setdefault(p, []).append(sid)
                ordered = [v[0] for _, v in sorted(by_pos.items())]
                pool = ordered if ordered else loc_sids
                return pool[idx % len(pool)]

        # 2) 指标序号顺序分配（如 temperature_trend_2 -> 温度传感器第2个）
        if parsed and metric_from_id in self.metrics:
            sids = self.sensors_for_metric(metric_from_id)
            if sids and 1 <= parsed[2] <= len(sids):
                return sids[parsed[2] - 1]

        # 3) 泛型序号 + 指标回退（如 chart_trend_35 + 倾角 -> rotation 第35个，越界则失败）

    @staticmethod
    def _context_side(context, caption=""):
        """从最近的上下文/图注里找明确方位词（右幅/左幅/下游/上游/右侧/左侧）。

        同一句同时出现左右（如“左幅、右幅”）视为无明确方向，继续往前找。
        用于“炎陵侧边跨跨中截面”这类同时存在左/右幅同名位置时按节定向。
        """
        texts = [str(x) for x in (context or []) if x]
        if caption:
            texts.append(str(caption))
        for t in reversed(texts):
            if "左幅" in t and "右幅" in t:
                continue
            if "左幅" in t:
                return "左幅"
            if "右幅" in t:
                return "右幅"
            if "上游" in t and "下游" in t:
                continue
            if "上游" in t:
                return "上游"
            if "下游" in t:
                return "下游"
            if "左侧" in t and "右侧" in t:
                continue
            if "左侧" in t:
                return "左侧"
            if "右侧" in t:
                return "右侧"
        return ""
        if parsed and parsed[0] is None and found_metric:
            sids = self.sensors_for_metric(found_metric)
            # 按监测部位分组，先位置后传感器（避免同位置多传感器重复占用前几个序号）
            by_pos = {}
            for sid in sids:
                p = self._position_for_sensor(sid)
                by_pos.setdefault(p, []).append(sid)
            ordered = [v[0] for _, v in sorted(by_pos.items())]
            if ordered and 1 <= parsed[2] <= len(ordered):
                return ordered[parsed[2] - 1]
            if sids and 1 <= parsed[2] <= len(sids):
                return sids[parsed[2] - 1]

        # 兜底：图注包含传感器名称/部位
        if caption:
            for sid, info in self.sensor_map.items():
                if self._is_excluded(sid):
                    continue
                names = [info.get("名称", ""), info.get("监测部位", "")]
                if any(n and len(n) >= 4 and _norm(n) in _norm(caption) for n in names):
                    return sid
        return None

    def _parse_position_chart_id(self, chart_id: str):
        """解析 位置化图表ID：<metric>_<监测部位>_<kind>_<n>。

        如 strain_4#墩底部_trend_1 -> ("strain", "4#墩底部", "trend", 1)。
        找不到返回 None。
        """
        if not chart_id or not self.name_dict:
            return None
        cands = self._position_candidates()
        # 泛型 chart_<位置>_<kind>_<n>：识别器没识别出指标时用 “chart” 占位。
        # 位置文本里带指标词（如 “炎陵侧边跨跨中截面振动” -> vibration）时
        # 直接推断并转成标准 metric 前缀再解析；位置里没有指标词的
        # （如 “chart_3#墩根部截面_trend_5”）返回 None，由图注/节上下文兜底。
        if chart_id.startswith("chart_"):
            _rest = chart_id[len("chart_"):]
            _mp = re.match(r"^(?P<loc>.+?)_(?P<kind>[a-z_]+)_(?P<n>\d+)$",
                           _rest)
            if _mp:
                _loc = _mp.group("loc")
                _kind = _mp.group("kind")
                _n = _mp.group("n")
                _m = self._metric_alias_hit(_loc)
                if not _m:
                    _clean = _strip_loc_metric_suffix(_loc)
                    _m = self._metric_alias_hit(_clean)
                if _m:
                    return self._parse_position_chart_id(
                        f"{_m}_{_loc}_{_kind}_{_n}")
            return None
        for metric in sorted(self.metrics, key=len, reverse=True):
            prefix = metric + "_"
            if not chart_id.startswith(prefix):
                continue
            rest = chart_id[len(prefix):]
            rest_norm = _norm(rest)
            for pos in cands:
                pn = _norm(pos)
                if not rest_norm.startswith(pn + "_"):
                    continue
                after = rest[len(pos) + 1:]
                m = re.match(r"^(?P<kind>[a-z_]+)_(?P<n>\d+)$", after)
                if m:
                    return metric, pos, m.group("kind"), int(m.group("n"))
            # 精确前缀匹配失败：模糊匹配候选位置。
            # 模板位置与名称对照可能存在“内/侧”等修饰字差异或词序不同，
            # 但关键方位词(上游/下游/左/右/顶/底)必须一致，避免顶/底或上下游串位。
            pos_part = re.match(r"^(?P<loc>.+?)_(?P<kind>[a-z_]+)_(?P<n>\d+)$",
                                rest)
            if pos_part:
                loc_raw = pos_part.group("loc")
                kind = pos_part.group("kind")
                n = int(pos_part.group("n"))
                # 章节号与墩号粘连（如 3.4.2.759#墩… 实为 3.4.2.7 + 59#墩…，
                # 来自 Word 自动编号与标题文字无空格拼接）：逐级剥离章节号
                # 后仍取剩余形如 N#墩 的变体参与匹配。
                loc_variants = [loc_raw]
                # 指标词后缀（如 “3#墩承台空间变位” -> “3#墩承台”，表格行
                # 标签常把指标名拼进监测部位）
                _clean = _strip_loc_metric_suffix(loc_raw)
                if _clean and _clean != loc_raw:
                    loc_variants.append(_clean)
                for _mm in re.finditer(r"(\d{1,2})#(?:柱)?墩", loc_raw):
                    _pre = loc_raw[:_mm.start(1)]
                    if re.match(r"^\d+(?:\.\d+){1,3}$", _pre):
                        _cand = loc_raw[_mm.start(1):]
                        if _cand not in loc_variants:
                            loc_variants.append(_cand)
                best = None
                best_score = 0.0
                for lc in loc_variants:
                    loc_words = _position_side_words(lc)
                    for pos in cands:
                        cand_words = _position_side_words(pos)
                        # 关键方位词必须一致：模板有“上游”候选必须有“上游”，
                        # 模板没有的方位词候选也不得有(顶/底板除外，见下)
                        if loc_words and cand_words:
                            if not loc_words.issubset(cand_words):
                                continue
                        elif loc_words and not cand_words:
                            continue
                        elif not loc_words and cand_words:
                            # 模板没提方位但候选带方位时仍可接受(去方位词派生)
                            pass
                        # 顶板/底板、左幅/右幅等部位词必须一致
                        for kw in ("顶板", "底板", "左幅", "右幅"):
                            if kw in lc and kw not in pos:
                                continue
                        score = _position_similarity(lc, pos)
                        if score > best_score:
                            best_score = score
                            best = pos
                if best and best_score >= 0.70:
                    # 同主体多候选时优先带实体词的位置（支座/伸缩缝/索夹/
                    # 锚碇…）：如 “塔梁交接处君山侧” 应命中
                    # “君山侧塔梁交接处支座(WY)”，而不是同主体的 GNSS
                    # 空间变位 “君山侧塔梁交接处下游侧”。
                    _preferred = ("支座", "伸缩缝", "索夹", "锚碇", "散索鞍")
                    if not any(w in best for w in _preferred):
                        for _p2 in cands:
                            if any(w in _p2 for w in _preferred) \
                                    and _site_set(_p2) == _site_set(best) \
                                    and _position_similarity(lc, _p2) >= 0.70:
                                best = _p2
                                break
                    return metric, best, kind, n
        return None

    def _position_candidates(self) -> List[str]:
        """位置化图表 ID 可匹配的监测位置候选集（按长度降序）。

        覆盖：传感器名称对照表 + 表格映射位置（结构温度表/裂缝监测表等）
        + 测点映射断面 + 传感器对照表的 名称/监测部位（如 7LX（S）-22）。

        另外从带方位词的名称派生“去方位词”的通用位置（如
        “随州侧边跨跨中截面上游” -> “随州侧边跨跨中截面”），供模板中
        不带方位词的图表占位符（如 temperature_随州侧边跨跨中截面_trend_1）
        匹配；传感器查找时 _sensors_at_position 会自动合并上游/下游。
        """
        cands = set(self.name_dict.keys())
        for tname, m in (self.table_map or {}).items():
            if isinstance(m, dict):
                cands.update(str(k) for k in m.keys())
                # 梁端支座位移表：补 “4#墩墩顶主梁梁端” 组合位置
                if "梁端支座位移表" in str(tname):
                    for dun in m:
                        cands.add(f"{dun}墩墩顶主梁梁端")
        # 从名称对照已有的“左侧x/右侧x”派生“左侧Y/右侧Y”位置（字符保持一致）
        extra = set()
        for c in list(cands):
            if re.search(r"(左|右)侧x$", _norm(c)):
                extra.add(c[:-1] + "Y")
        cands.update(extra)
        for m in (self.point_map or {}).values():
            if isinstance(m, list):
                for pl in m:
                    p = (pl or {}).get("断面位置")
                    if p:
                        cands.add(str(p))
        for info in self.sensor_map.values():
            for f in ("名称", "监测部位"):
                v = info.get(f)
                if v:
                    cands.add(str(v))
        # 派生去方位词的通用位置（上游/下游/左/右/左幅/右幅/左侧/右侧）
        _SIDE_WORDS = ("上游", "下游", "左幅", "右幅", "左侧", "右侧",
                       "左", "右")
        derived = set()
        for c in cands:
            cn = _norm(c)
            for w in _SIDE_WORDS:
                if cn.endswith(w) and len(cn) > len(w):
                    derived.add(c[: len(c) - len(w)])
                    break
        cands.update(derived)
        return sorted(cands, key=len, reverse=True)

    def _extract_location(self, text: str) -> str:
        """从图注/上下文提取监测位置关键词。"""
        if not text:
            return ""
        # 0) 多跨组合位置：保留跨号并截断到指标词（如 “第6、7跨跨中断面”），供逐跨展开
        m = re.search(
            r"第\s*([\d、，,和及]+)\s*跨(?P<loc>[^，。：\s]{0,10}?)"
            r"(?=(?:环境温度|环境湿度|结构温度|温度|湿度|风速|风向|倾角|裂缝|应变|振动|"
            r"位移|挠度|索力|监测|时程|频率|分布|变化|统计|如下图所示|$))",
            text,
        )
        if m and len(re.findall(r"\d+", m.group(1))) >= 2:
            return f"第{m.group(1).replace(' ', '')}跨{m.group('loc')}"
        # 0b) 多墩组合位置（“3、4#墩承台空间变位…”）-> “3、4#墩承台”，
        #     供 _location_sensors 逐墩展开（3#墩承台 / 4#墩承台）
        m = re.search(
            r"([\d、，,和及]+\s*#\s*(?:柱)?墩[^，。：\s]{0,8}?)"
            r"(?=(?:空间变位|位移|挠度|应变|倾角|振动|地震|监测|"
            r"时程|频率|分布|变化|统计|如下图所示|$))",
            text,
        )
        if m and len(re.findall(r"\d+", m.group(1))) >= 2:
            return m.group(1).replace(" ", "")
        # 1) 墩号定位（倾角/位移表）：如 “4#墩墩顶主梁支座倾角变化时程曲线图” -> “4#墩”
        m = re.search(r"(\d+)#\s*墩", text)
        if m:
            return m.group(0).replace(" ", "")
        # 2) 传感器名称完整出现在文本中（最长优先）
        best = ""
        for info in self.sensor_map.values():
            for n in (info.get("名称", ""), info.get("监测部位", "")):
                if n and len(n) > len(best) and _norm(n) in _norm(text):
                    best = n
        if best:
            return best
        # 3) 去掉指标/图型词后的残余位置词
        t = text
        for w in ("时程曲线图", "频率分布直方图", "时间序列图", "时间序列", "直方图", "曲线图",
                  "如下图所示", "如下", "变化趋势", "变化", "监测统计", "监测数据", "统计",
                  "监测", "数据", "测点布置图", "布置图", "示意图", "平面图",
                  "倾角", "结构温度", "温度", "湿度", "风速", "风向", "位移", "应变", "振动",
                  "挠度", "索力", "裂缝", "环境", "结构", "截面", "分布"):
            t = t.replace(w, "")
        t = re.sub(r"[a-z_]+", " ", t)
        t = re.sub(r"[、，。：；！？\s]+", " ", t).strip()
        t = " ".join(t.split())
        # 保留位置里的数字（如“第五跨L/4处主梁”），纯数字会被下面的中文检查剔除
        if len(t) < 2 or not re.search(r"[\u4e00-\u9fa5]", t):
            return ""
        return t

    def resolve_chart(self, chart_id: str, caption: str = "", context: str = "") -> Optional[str]:
        """把图表占位符解析为图库图片路径；找不到返回 None。"""
        info = self.resolve_chart_info(chart_id, caption, context)
        return info["path"] if info else None

    def find_sensor_by_hint(self, metric: str, hint: str,
                            side: str = "") -> Optional[str]:
        """按“位置/方向”提示定位传感器（供自动修复重索引用）。

        先用 _extract_location 抽位置，再用 _sensors_at_position / 位置展开找
        候选；有明确左右幅/上下游时做方向过滤。找不到返回 None（不猜）。
        """
        if not hint:
            return None
        loc = self._extract_location(str(hint)) or str(hint)
        sids = self._sensors_at_position(loc, metric) if metric else []
        if not sids:
            sids = self._location_sensors(metric, loc)
        if not sids:
            return None
        if side:
            sided = [s for s in sids
                     if side in (self._position_for_sensor(s) or "")]
            if sided:
                sids = sided
        return sids[0] if sids else None

    def resolve_chart_with_hint(self, chart_id: str, hint: str,
                                kind: str = "", metric: str = "") -> Optional[Dict]:
        """用纠正后的位置/方向提示重新解析一张图（供自动修复）。

        返回 {path, sensor_id, kind, display}；解析不到或候选不唯一时返回 None。
        """
        if not self.charts_dir or not hint:
            return None
        parsed = self._parse_chart_id(chart_id)
        metric = metric or (parsed[0] if parsed else "")
        kind = kind or (parsed[1] if parsed else "trend")
        if kind not in CHART_KIND_FILE and kind not in ("scatter", "correlation"):
            kind = "trend"
        side = self._context_side([hint])
        sid = self.find_sensor_by_hint(metric, hint, side=side)
        if not sid:
            return None
        png = self.chart_png_for(sid, kind, metric)
        if not png:
            return None
        return {
            "path": png,
            "sensor_id": sid,
            "kind": kind,
            "display": self.display_name_for(
                sid, chart_id, kind, metric_for_label=metric),
        }

    def resolve_chart_info(self, chart_id: str, caption: str = "", context=None,
                           metric_hint: str = "", sensor_hint: str = "",
                           feature_hint: str = "",
                           unit_hint: str = "") -> Optional[Dict]:
        """解析图表，返回 {path, sensor_id, kind, display}；找不到返回 None。"""
        if not self.charts_dir:
            return None
        tp = self._traffic_chart_path(chart_id)
        if tp:
            return {
                "path": tp,
                "sensor_id": "交通荷载",
                "kind": "trend",
                "display": ("交通荷载各车道车辆累计通过数量图"
                            if "cumulative" in chart_id
                            else "交通荷载各车道通过数量比例图"),
            }
        sensor_id = sensor_hint or self._chart_sensor_id(chart_id, caption, context)
        if not sensor_id:
            return None

        # 确定特征与图片文件名
        parsed = self._parse_chart_id(chart_id)
        pp = self._parse_position_chart_id(chart_id)
        ms = re.match(r"^chart_sensor_\d+_([a-z_]+)$", chart_id)
        if pp:
            metric_from_id, _pos, kind = pp[0], pp[1], pp[2]
        elif ms:
            metric_from_id = None
            kind = ms.group(1)
        else:
            metric_from_id = parsed[0] if parsed else None
            kind = parsed[1] if parsed else "trend"
            # 位置化占位符的位置模糊匹配失败时（如 洣水河
            # strain_左幅中跨1/2顶板_scatter_69），仍从原始 ID 提取图型，
            # 避免 scatter/histogram 被当成 trend 去匹配时程图。
            if kind == "trend":
                _mk = re.search(
                    r"_(?P<k>scatter|correlation|histogram|hist|"
                    r"timeseries|time_series|trend)_\d+$",
                    str(chart_id))
                if _mk:
                    kind = _mk.group("k")
        if kind not in CHART_KIND_FILE:
            if kind in ("scatter", "correlation"):
                # 相关性散点图：图库/<监测部位>/相关性_<特征A>-<特征B>.png
                return self._resolve_scatter_chart(
                    sensor_id, metric_from_id, metric_hint,
                    feature_hint, chart_id, caption)
            kind = "trend"

        # 优先：合并图库（图库/<监测部位>/<特征组>/<图型>.png）
        # 特征选择：节上下文推断的指标(metric_hint) > chart_id 前缀指标 > 特征提示
        metric_feature = ""
        if metric_hint and metric_hint in self.metrics:
            metric_feature = self.metrics[metric_hint].get("feature", "")
        if not metric_feature and metric_from_id and metric_from_id in self.metrics:
            metric_feature = self.metrics[metric_from_id].get("feature", "")
        if not metric_feature and feature_hint:
            metric_feature = feature_hint
        # 显式特征提示(如 xJsd)时按提示为准，不再用表格提示覆盖
        merged = self._merged_chart_path(
            sensor_id, kind, metric_feature,
            unit_hint=unit_hint if not feature_hint else "")
        if merged:
            display_metric = metric_hint or self._metric_alias_hit(
                caption
            ) or None
            return {
                "path": merged,
                "sensor_id": sensor_id,
                "kind": kind,
                "display": self.display_name_for(sensor_id, chart_id, kind,
                                                  metric_for_label=display_metric),
            }

        feat_dir = self._feature_dir_for_sensor(sensor_id, chart_id, kind,
                                                feature_hint=feature_hint)
        if not feat_dir:
            return None
        fname = CHART_KIND_FILE.get(kind, "时间序列图.png")
        if kind in ("scatter", "correlation"):
            # 相关性图在特征目录下: 相关性_<特征A>_<特征B>.png（此处不强行匹配）
            fname = "相关性_" + feat_dir + ".png"
        path = _pick_chart_file(
            os.path.join(self.charts_dir, sensor_id, feat_dir), fname)
        if not path:
            return None
        display_metric = metric_hint or self._metric_alias_hit(caption) or None
        return {
            "path": path,
            "sensor_id": sensor_id,
            "kind": kind,
            "display": self.display_name_for(sensor_id, chart_id, kind,
                                              metric_for_label=display_metric),
        }

    def _resolve_scatter_chart(self, sensor_id: str, metric_from_id: str,
                               metric_hint: str, feature_hint: str,
                               chart_id: str, caption: str) -> Optional[Dict]:
        """解析相关性散点图：图库/<监测部位>/相关性_<特征A>-<特征B>.png。

        优先选包含该传感器特征（如 YB(rsg)）的相关图，避免多个相关性图
        时取错；找不到对应图时返回 None（上层生成占位图）。
        """
        pos = self._position_for_sensor(sensor_id)
        if not pos:
            return None
        base_dir = self._fuzzy_position_dir(pos)
        cands = []
        if os.path.isdir(base_dir):
            cands = sorted(f for f in os.listdir(base_dir)
                           if f.startswith("相关性_") and f.endswith(".png"))
        pick_dir = base_dir
        if not cands:
            # 兜底：名称对照与图库目录命名顺序/修饰词不同时的散点图搜索
            # （如 模板/对照“左幅炎陵侧边跨跨中顶板” vs 图库目录
            #  “炎陵侧边跨跨中截面顶板左幅”）
            best_dir, best_score, best_cands = "", 0.0, []
            try:
                for d in os.listdir(self.charts_dir):
                    dp = os.path.join(self.charts_dir, d)
                    if not os.path.isdir(dp):
                        continue
                    cs = sorted(f for f in os.listdir(dp)
                                if f.startswith("相关性_")
                                and f.endswith(".png"))
                    if not cs:
                        continue
                    sc = _position_similarity(pos, d)
                    if sc > best_score:
                        best_score, best_dir, best_cands = sc, dp, cs
            except OSError:
                pass
            if best_dir and best_score >= 0.6:
                pick_dir, cands = best_dir, best_cands
            else:
                return None
        feat = feature_hint or ""
        if not feat:
            for m in (metric_hint, metric_from_id):
                if m and m in self.metrics:
                    feat = self.metrics[m].get("feature", "") or ""
                    if feat:
                        break
        feats = self._sensor_features.get(str(sensor_id), []) or []
        pick = None
        for c in cands:
            if feat and feat in c:
                pick = c
                break
        if pick is None:
            for c in cands:
                if any(f and f in c for f in feats):
                    pick = c
                    break
        if pick is None:
            pick = cands[0]
        path = os.path.join(pick_dir, pick)
        if not os.path.isfile(path):
            return None
        display_metric = (metric_hint
                          or self._metric_alias_hit(caption)
                          or metric_from_id)
        return {
            "path": path,
            "sensor_id": sensor_id,
            "kind": "scatter",
            "display": self.display_name_for(sensor_id, chart_id, "scatter",
                                              metric_for_label=display_metric),
        }

    def chart_png_for(self, sensor_id: str, kind: str, metric: str = "") -> Optional[str]:
        """按传感器编号 + 图型直接取图库图片（用于缺图自动补齐，不消耗顺序计数）。"""
        if not self.charts_dir:
            return None
        kind = kind if kind in CHART_KIND_FILE else "trend"
        metric_feature = self.metrics.get(metric, {}).get("feature", "") if metric else ""
        merged = self._merged_chart_path(sensor_id, kind, metric_feature)
        if merged:
            return merged
        feat_dir = ""
        feat = self.metrics.get(metric, {}).get("feature", "") if metric else ""
        if feat and os.path.isdir(os.path.join(self.charts_dir, str(sensor_id), feat)):
            feat_dir = feat
        else:
            feat_dir = self._feature_dir_for_sensor(
                str(sensor_id), f"{metric}_{kind}_1" if metric else f"{kind}_1", kind) or ""
        if not feat_dir:
            return None
        path = os.path.join(self.charts_dir, str(sensor_id), feat_dir, CHART_KIND_FILE.get(kind, "时间序列图.png"))
        return path if os.path.isfile(path) else None

    def _traffic_chart_path(self, chart_id: str) -> Optional[str]:
        """交通荷载跨车道图：{{chart.traffic_cumulative_trend_1}} ->
        图库/<期>/<桥>/交通荷载/各车道车辆累计通过数量图.png。"""
        if not self.charts_dir or not chart_id:
            return None
        base = os.path.join(self.charts_dir, "交通荷载")
        mapping = (
            ("traffic_cumulative", "各车道车辆累计通过数量图.png"),
            ("traffic_ratio", "各车道通过数量比例图.png"),
            ("traffic_freq", "各车道频率分布图.png"),
        )
        for prefix, fn in mapping:
            if str(chart_id).startswith(prefix):
                p = os.path.join(base, fn)
                return p if os.path.isfile(p) else None
        return None

    def _feature_dir_for_sensor(self, sensor_id: str, chart_id: str, kind: str,
                                feature_hint: str = "") -> Optional[str]:
        """确定传感器目录下用哪个特征子目录。"""
        # 0) 精确特征提示（如 “xJsd” -> DZJSD(xJsd)）：按包含关系匹配
        if feature_hint:
            base = os.path.join(self.charts_dir, str(sensor_id))
            hint_n = _norm(feature_hint)
            if os.path.isdir(base):
                for d in os.listdir(base):
                    if hint_n and hint_n in _norm(d):
                        return d
        # 优先：图表占位符中的指标对应的特征
        parsed = self._parse_chart_id(chart_id)
        if parsed and parsed[0] in self.metrics:
            feat = self.metrics[parsed[0]].get("feature", "")
            if feat and os.path.isdir(os.path.join(self.charts_dir, sensor_id, feat)):
                return feat
        # 其次：传感器已有特征里选一个（优先该指标特征，否则第一个）
        feats = self._sensor_features.get(sensor_id, [])
        if feats:
            return feats[0]
        base = os.path.join(self.charts_dir, sensor_id)
        if os.path.isdir(base):
            subs = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
            return subs[0] if subs else None
        return None

    def chart_siblings(self, path: str) -> List[str]:
        """同一图表拆分出的多张图(时间序列图_2.png / _3.png ...)，
        按序号返回；找不到返回空列表。"""
        if not path:
            return []
        base, ext = os.path.splitext(path)
        out = []
        k = 2
        while True:
            p = f"{base}_{k}{ext}"
            if os.path.isfile(p):
                out.append(p)
                k += 1
            else:
                break
        return out

    def _position_for_sensor(self, sensor_id: str) -> str:
        """从名称对照表反查传感器所在位置名（与合并图库目录同名）。
        找不到时回退到传感器对照表的 名称/监测部位。"""
        sid = str(sensor_id)
        for pos, entries in self.name_dict.items():
            for e in entries or []:
                if str(e.get("编号", "")) == sid:
                    return pos
        info = self.sensor_map.get(sid, {})
        return info.get("名称") or info.get("监测部位") or ""

    def _group_has_sensor(self, dirpath: str, sensor_id: str) -> bool:
        """合并图目录下的 预处理记录.json 是否包含该传感器。

        用于“同一位置多个特征组”时按图库实际归属选图：如 58#墩墩顶截面上游
        的 DZJSD 组只有 377(振动)、SZJSD 组只有 304(地震)，即使统计库/
        名称对照把 304 的特征标成 DZJSD，也以图库自身记录为准取 SZJSD。
        记录文件缺失时返回 False（由其它候选逻辑兜底）。
        """
        rec = os.path.join(dirpath, "预处理记录.json")
        if not os.path.isfile(rec):
            return False
        try:
            with open(rec, "r", encoding="utf-8") as f:
                data = json.load(f)
            return any(str(x.get("传感器")) == str(sensor_id)
                       for x in (data.get("记录") or []))
        except Exception:  # noqa: BLE001
            return False

    def _merged_chart_path(self, sensor_id: str, kind: str,
                           metric_feature: str = "",
                           unit_hint: str = "") -> Optional[str]:
        """合并图库路径，按优先级查找：
          1) 图库/<监测部位>/<特征组>/<图型>.png（多传感器子图）
          2) 图库/<监测部位>/<特征组>/<特征>/<图型>.png（单传感器多特征复制布局）
         没有指标特征时(如 chart_sensor_304_trend 直接按编号取图)，
         自动在位置目录下按传感器特征选特征组子目录。
         精确目录不存在时，在图库位置目录中做模糊匹配（关键方位词一致、
         容忍“内/侧”等修饰字差异），避免“名称对照多一个字”导致找不到图。
         unit_hint 为邻近表格的列头/单位提示（如“纵桥向(X方向)横桥向(Y方向)
         竖向(Z方向)m/s²”）：同一位置存在多个候选特征组（地震 DZJSD/SZJSD、
         振动 DZJSD 等）时按“轴向覆盖 + 单位族”打分选图，避免地震节插错成
         振动图；单个候选组或没有提示时不影响原有逻辑。
        找不到返回 None。"""
        if not self.charts_dir:
            return None
        pos = self._position_for_sensor(sensor_id)
        if not pos:
            return None
        fname = CHART_KIND_FILE.get(kind, "时间序列图.png")
        base_dir = self._fuzzy_position_dir(pos)
        if metric_feature:
            g = feature_group(metric_feature)
            feats = self._sensor_features.get(str(sensor_id), []) or []
            g_actual = sorted(
                {feature_group(f) for f in feats if feature_group(f)})
            # 候选组：config 组 + 传感器实际特征组 + 位置目录组（去重）
            groups = []
            for gg in [g] + g_actual:
                if gg and gg not in groups:
                    groups.append(gg)
            if os.path.isdir(base_dir):
                for sub in sorted(os.listdir(base_dir)):
                    if (os.path.isdir(os.path.join(base_dir, sub))
                            and not sub.startswith("相关性")
                            and sub not in groups):
                        groups.append(sub)
            # 同位置多特征组（DZJSD/SZJSD/GNSS…）：优先选“预处理记录含目标
            # 传感器”的组——即使统计库把 304 标成 DZJSD，也按图库实际归属
            # 取 SZJSD，避免地震节插进同位置的振动图。
            owned = [gg for gg in groups
                     if self._group_has_sensor(
                         os.path.join(base_dir, _safe_dir(gg)),
                         sensor_id)]
            if owned:
                groups = owned
            p = None
            # 表格单位/轴向校验：有提示且候选组不唯一时，按提示对候选组打分，
            # 分数最高且目录真实存在者优先（如 3 轴地震表 -> SZJSD 而非 DZJSD）。
            if unit_hint and len(groups) > 1:
                hint_axes = _hint_axes(unit_hint)
                hint_unit = _hint_unit(unit_hint)
                scored = []
                for gg in groups:
                    score = 0.0
                    axes = set()
                    feats_g = [f for f in feats if feature_group(f) == gg]
                    for f in feats_g:
                        axes |= _feature_axis_set(f)
                    if not axes:
                        axes = _group_axis_guess(gg)
                    if hint_axes:
                        covered = len(hint_axes & axes)
                        score += 3.0 if covered == len(hint_axes) \
                            else float(covered)
                    if hint_unit:
                        if feats_g:
                            unit_ok = any(
                                _group_unit_family(feature_group(f))
                                == hint_unit for f in feats_g)
                        else:
                            unit_ok = _group_unit_family(gg) == hint_unit
                        if unit_ok:
                            score += 2.0
                    if gg == g:
                        score += 0.5
                    scored.append((score, gg))
                scored.sort(key=lambda x: -x[0])
                for _score, gg in scored:
                    p = _pick_chart_file(
                        os.path.join(base_dir, _safe_dir(gg)), fname)
                    if p:
                        break
            if not p:
                # 候选组（已按传感器归属收敛）逐个尝试；config 组优先，
                # 但传感器实际特征组/图库归属组优先于过时的 config 特征。
                ordered = []
                for gg in groups:
                    if gg not in ordered:
                        ordered.append(gg)
                for gg in ordered:
                    p = _pick_chart_file(
                        os.path.join(base_dir, _safe_dir(gg)), fname)
                    if p:
                        break
            if not p and os.path.isdir(base_dir):
                # 名称对照表/总览的特征编码与图库实际目录不一致时
                # （如对照表写 DZJSD(xJsd) 但图库目录为 SZJSD），最后在
                # 位置目录下按“同族特征 + 文件名”扫描兜底；同族都不满足时
                # 只接受目录里唯一含目标图名的子目录，避免取到异类特征图。
                family = ("JSD",) if any(
                    w in str(metric_feature).upper()
                    for w in ("JSD", "JXD", "JYD", "JZD")) else ("",)
                cands = []
                for sub in sorted(os.listdir(base_dir)):
                    subp = os.path.join(base_dir, sub)
                    if not os.path.isdir(subp):
                        continue
                    if family and not any(w in sub.upper() for w in family):
                        continue
                    if _pick_chart_file(subp, fname):
                        cands.append(subp)
                if len(cands) == 1:
                    p = cands[0]
        else:
            # 无指标特征：按“图库预处理记录归属”优先，再按传感器特征组
            # 逐个尝试，找不到再扫描位置目录。同样要避免同位置多特征组
            # （DZJSD/SZJSD…）时按陈旧统计取到其它传感器的图。
            p = None
            feats = self._sensor_features.get(str(sensor_id), []) or []
            groups = [feature_group(f) for f in feats if feature_group(f)]
            if os.path.isdir(base_dir):
                for sub in sorted(os.listdir(base_dir)):
                    if (os.path.isdir(os.path.join(base_dir, sub))
                            and not sub.startswith("相关性")
                            and sub not in groups):
                        groups.append(sub)
            owned = [gg for gg in groups
                     if self._group_has_sensor(
                         os.path.join(base_dir, _safe_dir(gg)),
                         sensor_id)]
            if owned:
                groups = owned
            for gg in groups:
                p = _pick_chart_file(
                    os.path.join(base_dir, _safe_dir(gg)), fname)
                if p:
                    break
            if not p and os.path.isdir(base_dir):
                for sub in sorted(os.listdir(base_dir)):
                    subp = os.path.join(base_dir, sub)
                    if os.path.isdir(subp):
                        p = _pick_chart_file(subp, fname)
                        if p:
                            break
        if p:
            return p
        if metric_feature:
            for gg in groups:
                p2 = _pick_chart_file(
                    os.path.join(base_dir, _safe_dir(gg),
                                 _safe_dir(metric_feature)), fname)
                if p2:
                    return p2
        return None

    def _fuzzy_position_dir(self, pos: str) -> str:
        """返回图库中与 pos 最匹配的位置目录；精确目录优先，找不到做模糊匹配。"""
        exact = os.path.join(self.charts_dir, _safe_dir(pos))
        if os.path.isdir(exact):
            return exact
        if not os.path.isdir(self.charts_dir):
            return exact
        loc_words = _position_side_words(pos)
        best = None
        best_score = 0.0
        try:
            names = os.listdir(self.charts_dir)
        except OSError:
            return exact
        for name in names:
            if not os.path.isdir(os.path.join(self.charts_dir, name)):
                continue
            cand_words = _position_side_words(name)
            if loc_words and cand_words and not loc_words.issubset(cand_words):
                continue
            for kw in ("顶板", "底板", "左幅", "右幅"):
                if kw in pos and kw not in name:
                    continue
            score = _position_similarity(pos, name)
            if score > best_score:
                best_score = score
                best = name
        if best and best_score >= 0.72:
            return os.path.join(self.charts_dir, best)
        return exact

    # ------------------------------------------------------------------
    # 待补图表占位图
    # ------------------------------------------------------------------

    def make_placeholder_chart(self, chart_id: str, reason: str, out_dir: str) -> str:
        """为解析不到的图表生成一张明显的占位图，避免生成流程中断。"""
        os.makedirs(out_dir, exist_ok=True)
        safe = re.sub(r'[\\/:*?"<>|]', "_", str(chart_id))
        path = os.path.join(out_dir, f"pending_{safe}.png")
        try:
            from PIL import Image, ImageDraw, ImageFont
            w, h = 1200, 700
            img = Image.new("RGB", (w, h), "white")
            draw = ImageDraw.Draw(img)
            draw.rectangle([8, 8, w - 8, h - 8], outline="#b0b0b0", width=4)
            font = self._pick_font(size=40)
            small = self._pick_font(size=28)
            draw.text((w / 2, h / 2 - 60), "图表待补充", font=font, fill="#c0392b", anchor="mm")
            draw.text((w / 2, h / 2 + 30), f"占位符: {chart_id}", font=small, fill="#555555", anchor="mm")
            draw.text((w / 2, h / 2 + 90), reason or "未匹配到图库图片", font=small, fill="#888888", anchor="mm")
            img.save(path)
            return path
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _pick_font(size: int):
        for fp in (
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ):
            if os.path.isfile(fp):
                try:
                    from PIL import ImageFont
                    return ImageFont.truetype(fp, size)
                except Exception:  # noqa: BLE001
                    continue
        from PIL import ImageFont
        return ImageFont.load_default()

    # ------------------------------------------------------------------
    # 覆盖度 / 待补清单
    # ------------------------------------------------------------------

    def coverage(self) -> Dict:
        """生成数据覆盖度报告（供 Web 端展示）。"""
        if not self.loaded:
            self.load()
        metrics_out = []
        scan_cap = int(self.cfg.get("coverage_scan_cap", 100))
        for metric, mcfg in self.metrics.items():
            feat = mcfg.get("feature", "")
            sids = self.sensors_for_metric(metric)
            sampled = False
            scan_sids = sids
            if not feat and len(sids) > scan_cap:
                scan_sids = sids[:scan_cap]
                sampled = True
            entry = {
                "metric": metric,
                "label": mcfg.get("label", metric),
                "feature": feat,
                "unit": mcfg.get("unit", ""),
                "sensor_count": len(sids),
                "sampled": sampled,
                "scanned": len(scan_sids),
                "with_data": 0,
                "first_day": None,
                "last_day": None,
            }
            for sid in scan_sids:
                fstats = self._feature_stats(sid, metric)
                if not fstats:
                    continue
                entry["with_data"] += 1
                fd = fstats.get("起始日期") or fstats.get("覆盖天数")
                ld = fstats.get("结束日期")
                if fd and (entry["first_day"] is None or str(fd) < str(entry["first_day"])):
                    entry["first_day"] = fd
                if ld and (entry["last_day"] is None or str(ld) > str(entry["last_day"])):
                    entry["last_day"] = ld
            metrics_out.append(entry)
        return {
            "bridge_name": self.bridge_name,
            "status": self.status(),
            "metrics": metrics_out,
            "sensor_count": len(self.sensor_map),
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
