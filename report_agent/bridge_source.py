# -*- coding: utf-8 -*-
"""真实监测数据适配器：直接读取“桥数据预处理”产出的统计值 JSON 与图库图片。

桥数据预处理项目（D:/Code/桥数据预处理/）会产出：
  统计值/<传感器编号>.json        每个传感器的分特征统计（中文键）+ 每日统计
  统计值/传感器编号名称.json       编号 -> 中文监测部位对照
  统计值/总览.json                全部传感器-特征总览
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

log = logging.getLogger("report-agent.bridge")


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

# 轴/方向分量 -> 同一特征组（与 build_chart_library.feature_group 保持一致）
_AXIS_INNER = {"Δx", "Δy", "Δz", "x", "y", "z"}

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
    """把模板统计量统一成英文规范键。"""
    return CN_STAT_MAP.get(stat, stat)


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
            "rotation": "倾角", "crack": "裂缝",
        }

        # 运行时状态
        self.overview: Optional[List[Dict]] = None       # 总览列表
        self.sensor_map: Dict[str, Dict] = {}            # 编号 -> 名称/部位
        self.name_dict: Dict[str, List[Dict]] = {}       # 名称 -> [{编号, 特征}]（人工对照表）
        self.point_map: Dict = {}                        # 测点映射：表类 -> [{断面位置, 测点}]
        self.table_map: Dict = {}                        # 表格映射：表类 -> 墩/位置 -> {编号, 特征}
        self._category_sensors: Dict[str, List[str]] = {}  # 类别 -> 编号列表（从名称对照表）
        self._stats_cache: Dict[str, Dict] = {}          # 编号 -> 统计值 JSON
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

            # 2b. 人工维护的“传感器名称 -> 编号/特征”对照表
            self._load_name_dict()

            self.loaded = True
        except Exception as exc:  # noqa: BLE001
            self.load_error = str(exc)
            log.exception("桥数据加载失败: %s", exc)
        return self.status()

    def _load_name_dict(self) -> None:
        """加载 统计值/传感器名称对照/<桥名>.json（名称 -> 编号/特征）。"""
        path = self.name_dict_path
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
                os.path.join(self.stats_dir, "传感器名称对照") if self.stats_dir else ""),
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
        sids = []
        if feat:
            for sid, feats in self._sensor_features.items():
                if self._is_excluded(sid):
                    continue
                if feat in feats:
                    sids.append(sid)
        if not sids:
            # 特征未知时：优先用监测类别限定（振动/应变/索力…），避免跨类别污染
            cat = self.metric_category.get(metric, "")
            if cat and cat in self._category_sensors:
                sids = [sid for sid in self._category_sensors[cat] if not self._is_excluded(sid)]
        if not sids:
            # 最后才退化为全部传感器
            sids = [sid for sid in self.sensor_map
                    if not self._is_excluded(sid)
                    and (not self.bridge_name or self.sensor_map[sid].get("桥名", "") == self.bridge_name)]
        return sorted(sids, key=lambda x: int(x) if x.isdigit() else x)

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

        # 2. 人工名称对照表（统计值/传感器名称对照/<桥名>.json）——精确命中率最高
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
            if self.bridge_name and info.get("桥名") and info.get("桥名") != self.bridge_name:
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
        path = os.path.join(self.stats_dir, f"{sensor_id}.json")
        if not os.path.isfile(path):
            self._stats_cache[sensor_id] = None
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._stats_cache[sensor_id] = data
            return data
        except Exception as exc:  # noqa: BLE001
            log.warning("读取统计值 %s 失败: %s", path, exc)
            self._stats_cache[sensor_id] = None
            return None

    def _feature_stats(self, sensor_id: str, metric: str, feature: str = "") -> Optional[Dict]:
        data = self._load_sensor_stats(sensor_id)
        if not data:
            return None
        feat = feature or self.metrics.get(metric, {}).get("feature", "")
        stats = data.get("特征统计", {}) or {}
        if feat and feat in stats:
            return stats[feat]
        # 特征没对上时，取唯一特征；多特征则取第一个（保证有值可读）
        if len(stats) == 1:
            return next(iter(stats.values()))
        if stats and not feat:
            return next(iter(stats.values()))
        return None

    def _sensor_stat(self, sensor_id: str, metric: str, stat: str, period: Dict,
                     feature: str = "") -> Optional[float]:
        """读取单个传感器（可指定特征）在报告期内的统计值。"""
        fstats = self._feature_stats(sensor_id, metric, feature=feature)
        if not fstats:
            return None
        daily = self._period_daily(fstats, period)
        if daily:
            return self._aggregate_daily(daily, stat, fstats=fstats)
        return self._full_period_stats(fstats, stat)

    @staticmethod
    def _extract_dunhao(title: str) -> str:
        """从表格标题提取墩号，如 '4#墩墩顶主梁梁端支座位移监测统计' -> '4'。"""
        m = re.search(r"(\d+)\s*#\s*墩", title)
        return m.group(1) if m else ""

    @staticmethod
    def _match_position(query: str, positions: List[str], threshold: float = 0.6) -> Optional[str]:
        """在候选断面位置里找与标题/列名最像的一个。"""
        best, best_score = None, 0.0
        for pos in positions:
            score = difflib.SequenceMatcher(None, _norm(query), _norm(pos)).ratio()
            if score > best_score:
                best, best_score = pos, score
        return best if best is not None and best_score >= threshold else None

    def _resolve_cell_by_table(self, metric: str, column: str, stat: str,
                               period: Dict, title: str,
                               row_index: int = 0) -> Optional[float]:
        """按表格标题上下文解析单元格（测点映射 / 表格映射）。"""
        if not title:
            return None
        t = title.replace("{{", "").replace("}}", "")

        # 1) 结构应变 / 结构振动表：断面位置 -> 测点N -> 编号
        for kw, mkey in (("应变", "结构应变监测表"), ("振动", "结构振动监测表")):
            if kw in t and mkey in self.point_map:
                plans = self.point_map[mkey]
                pos = self._match_position(t, [p.get("断面位置", "") for p in plans])
                if pos:
                    plan = next((p for p in plans if p.get("断面位置") == pos), None)
                    sid = (plan or {}).get("测点", {}).get(column)
                    if sid:
                        return self._sensor_stat(str(sid), metric, stat, period)

        # 2) 梁端支座位移表：墩号 + 左/右
        if "位移" in t and "支座" in t and "梁端支座位移表" in self.table_map:
            dun = self._extract_dunhao(t)
            side = "左" if "左" in column else "右"
            entry = self.table_map["梁端支座位移表"]
            row = (entry.get(dun + "#") or entry.get(dun) or {})
            entry = row.get(side)
            if entry:
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
                return self._sensor_stat(str(entry.get("编号", "")), metric, stat, period,
                                         feature=str(entry.get("特征", "")))

        # 4) 裂缝监测表：列名 -> 断面位置 -> 该位置全部裂缝传感器聚合
        if "裂缝" in t and "裂缝监测表" in self.table_map:
            pos = self._match_position(column, list(self.table_map["裂缝监测表"].keys()))
            if pos:
                ids = [str(x) for x in self.table_map["裂缝监测表"][pos]]
                vals = [self._sensor_stat(s, metric, stat, period) for s in ids]
                vals = [v for v in vals if v is not None]
                if vals:
                    if stat == "max":
                        return max(vals)
                    if stat == "min":
                        return min(vals)
                    if stat == "range":
                        return max(vals) - min(vals)
                    return sum(vals) / len(vals)

        # 5) 温湿度表 / 结构温度表：位置 -> 编号列表
        for kw, mkey in (("结构温度", "结构温度表"), ("温湿度", "温湿度表")):
            if kw in t and mkey in self.table_map:
                pos = self._match_position(column, list(self.table_map[mkey].keys()))
                if pos:
                    ids = [str(x) for x in self.table_map[mkey][pos]]
                    feat = "WD(temp)" if mkey == "结构温度表" else ""
                    if not ids:
                        return None
                    # 一个监测部位有多个传感器时，按表格行号取对应传感器
                    # （第1行 -> 第1个传感器，第2行 -> 第2个传感器…）
                    sid = ids[row_index % len(ids)]
                    return self._sensor_stat(sid, metric, stat, period, feature=feat)
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
                         fstats: Optional[Dict] = None) -> Optional[float]:
        """把每日统计聚合成报告期统计量。"""
        stat = _canon_stat(stat)
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
        """报告期不可用时，用 JSON 内的整体统计值。"""
        key = STAT_KEY_MAP.get(stat)
        if key and fstats.get(key) is not None:
            return float(fstats[key])
        return None

    def resolve_cell(self, metric: str, column: str, stat: str, period: Dict,
                     table_title: str = "", row_index: int = 0) -> Optional[float]:
        """解析 {{cell.<metric>.<column>.<stat>}}，可带表格标题上下文。"""
        actual = _canon_stat(STAT_KEY_MAP.get(stat, stat))
        # 0) 表格上下文映射（测点N / 左侧右侧 / 左X右X 等）
        if table_title:
            val = self._resolve_cell_by_table(metric, column, actual, period, table_title,
                                              row_index=row_index)
            if val is not None:
                self._match_stats["table_map"] = self._match_stats.get("table_map", 0) + 1
                return val
        sensor_id = self.find_sensor(metric, column)
        if sensor_id:
            fstats = self._feature_stats(sensor_id, metric)
            if fstats:
                daily = self._period_daily(fstats, period)
                val = self._aggregate_daily(daily, actual, fstats=fstats) if daily else None
                if val is not None:
                    return val
                # 报告期内无数据，但整体统计存在：仍返回整体值并告警？
                # 返回 None 由上层决定（填入“—”并计入待补）
        # 回退：该指标全部传感器聚合
        self._match_stats["metric_fallback"] += 1
        return self.resolve_metric_stat(metric, stat, period)

    def resolve_metric_stat(self, metric: str, stat: str, period: Dict) -> Optional[float]:
        """解析 {{stats.<metric>.<stat>}}：对该指标全部传感器聚合。"""
        actual = _canon_stat(STAT_KEY_MAP.get(stat, stat))
        vals = []
        for sid in self.sensors_for_metric(metric):
            fstats = self._feature_stats(sid, metric)
            if not fstats:
                continue
            daily = self._period_daily(fstats, period)
            if daily:
                v = self._aggregate_daily(daily, actual, fstats=fstats)
            else:
                v = self._full_period_stats(fstats, actual)
            if v is not None:
                vals.append(v)
        if not vals:
            return None
        if actual == "max":
            return max(vals)
        if actual == "min":
            return min(vals)
        if actual == "abs_max":
            return max(vals, key=abs)
        if actual == "range":
            return max(vals) - min(vals)
        if actual == "sum":
            return sum(vals)
        if actual in ("count", "days"):
            return sum(vals)
        return sum(vals) / len(vals)

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
        for sid, info in self.sensor_map.items():
            if self._is_excluded(sid):
                continue
            if feat:
                feats = self._sensor_features.get(sid, [])
                if feat not in feats:
                    continue
            names = [info.get("名称", ""), info.get("监测部位", "")]
            if any(n and len(n) >= 2 and (
                (_norm(n) in loc_n) or (len(loc_n) >= 2 and loc_n in _norm(n))
            ) for n in names):
                out.append(sid)
        return sorted(set(out), key=lambda x: int(x) if x.isdigit() else x)

    def _chart_sensor_id(self, chart_id: str, caption: str = "",
                         context=None) -> Optional[str]:
        """按占位符 ID / 图注推断传感器编号。"""
        if chart_id in self.chart_map:
            return str(self.chart_map[chart_id])

        ctx_texts = context if isinstance(context, (list, tuple)) else ([context] if context else [])
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
                # 按 (指标, 位置, 图型) 分别计数：
                # 时程图 1/2 -> 传感器 1/2，直方图 3/4 -> 传感器 1/2
                kind = parsed[1] if parsed else "trend"
                key = (found_metric or "?", _norm(location), kind)
                idx = self._chart_seq.get(key, 0)
                self._chart_seq[key] = idx + 1
                return loc_sids[idx % len(loc_sids)]

        # 2) 指标序号顺序分配（如 temperature_trend_2 -> 温度传感器第2个）
        if parsed and metric_from_id in self.metrics:
            sids = self.sensors_for_metric(metric_from_id)
            if sids and 1 <= parsed[2] <= len(sids):
                return sids[parsed[2] - 1]

        # 3) 泛型序号 + 指标回退（如 chart_trend_35 + 倾角 -> rotation 第35个，越界则失败）
        if parsed and parsed[0] is None and found_metric:
            sids = self.sensors_for_metric(found_metric)
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

    def resolve_chart_info(self, chart_id: str, caption: str = "", context=None,
                           metric_hint: str = "", sensor_hint: str = "",
                           feature_hint: str = "") -> Optional[Dict]:
        """解析图表，返回 {path, sensor_id, kind, display}；找不到返回 None。"""
        if not self.charts_dir:
            return None
        sensor_id = sensor_hint or self._chart_sensor_id(chart_id, caption, context)
        if not sensor_id:
            return None

        # 确定特征与图片文件名
        parsed = self._parse_chart_id(chart_id)
        kind = parsed[1] if parsed else "trend"
        if kind not in CHART_KIND_FILE:
            kind = "trend"

        # 优先：合并图库（图库/<监测部位>/<特征组>/<图型>.png）
        metric_feature = ""
        if parsed and parsed[0] in self.metrics:
            metric_feature = self.metrics[parsed[0]].get("feature", "")
        elif feature_hint:
            metric_feature = feature_hint
        merged = self._merged_chart_path(sensor_id, kind, metric_feature)
        if merged:
            display_metric = metric_hint or self._metric_alias_hit(
                f"{caption} {' '.join(context) if isinstance(context, (list, tuple)) else context or ''}"
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
        path = os.path.join(self.charts_dir, sensor_id, feat_dir, fname)
        if not os.path.isfile(path):
            return None
        display_metric = metric_hint or self._metric_alias_hit(
            f"{caption} {' '.join(context) if isinstance(context, (list, tuple)) else context or ''}"
        ) or None
        return {
            "path": path,
            "sensor_id": sensor_id,
            "kind": kind,
            "display": self.display_name_for(sensor_id, chart_id, kind,
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

    def _merged_chart_path(self, sensor_id: str, kind: str,
                           metric_feature: str = "") -> Optional[str]:
        """合并图库路径：图库/<监测部位>/<特征组>/<图型>.png。找不到返回 None。"""
        if not self.charts_dir:
            return None
        info = self.sensor_map.get(str(sensor_id), {})
        pos = info.get("名称") or info.get("监测部位")
        if not pos:
            return None
        g = feature_group(metric_feature or "")
        fname = CHART_KIND_FILE.get(kind, "时间序列图.png")
        p = os.path.join(self.charts_dir, _safe_dir(pos), _safe_dir(g), fname)
        return p if os.path.isfile(p) else None

    # ------------------------------------------------------------------
    # 待补图表占位图
    # ------------------------------------------------------------------

    def make_placeholder_chart(self, chart_id: str, reason: str, out_dir: str) -> str:
        """为解析不到的图表生成一张明显的占位图，避免生成流程中断。"""
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"pending_{chart_id}.png")
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
