#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图库生成脚本（集成版）
======================

读取预处理结果，为每个传感器、每个特征：
  1) 计算统计值（中文键）      -> 统计值/<传感器编号>.json
  2) 生成统计图                -> 图库/<传感器编号>/<特征>/时间序列图.png
                                图库/<传感器编号>/<特征>/频率分布图.png
可选 --correlation：同一传感器不同特征间的相关性分析
                                -> 图库/<传感器编号>/相关性_<特征A>_<特征B>.png

数据源（自动选择）:
  A) daily 目录(小时级明细，优先): daily/<传感器>/<特征>/<日期>.csv，
     每天 24 行，按小时描点(一天 24 个点)，横轴仍标日期
  B) summary.csv(日级汇总):  sensor,feature,date,files,seconds,missing_seconds,
                             min,mean,max  —— 仅在没有 daily 明细时回退

进一步预处理(小时级):
  - 零散尖峰点(均值/最大/最小各自检测): 用去掉上下 5% 极端值后的
    稳健基线判定，连续 <=3 小时、且总占比 <=2% 才替代;
    同一小时在多天重复出现的"规律性高峰"(如早晚高峰)视为常态不替代;
    尖峰占比 >2% 时判定疑似传感器异常，不替代并在图上/JSON 里警告;
  - 长时间突变段: 当天"偏高/偏低小时"占多数(>=60%)且连续 >= 7 天，
    才判定为突变区间，图上着色标注 起始时间~结束时间(精确到小时);

统计值覆盖参考脚本的目标口径：
  平均值 / 最大值 / 最小值 / 差值 / 绝对最大值 / 均方根值 / 标准差 / 中位数
  有效小时数 / 缺失小时数 / 有效天数 / 缺失天数，以及每日统计明细。

用法:
    python build_chart_library.py [--daily-root ...] [--lib-root ...]
                                  [--correlation] [--limit-sensors N]
"""

import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import bootstrap  # noqa: E402

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "Arial Unicode MS"]
plt.rcParams["axes.unicode_minus"] = False

try:
    from scipy import stats as scipy_stats
    HAS_SCIPY = True
except Exception:
    HAS_SCIPY = False

# ---------------- 默认路径（可按需修改/用命令行参数覆盖） ----------------
DEFAULT_DAILY_ROOT = r"D:\preprocess_sensor_data\daily"   # 预处理后的 daily 目录
DEFAULT_LIB_ROOT = r"D:\Code\桥数据预处理"                # 图库/统计值 的上级目录
# ---------------------------------------------------------------------

# 特征英文代号 -> 中文名（便于中文查询，未列出的按原名显示）
FEATURE_CN = {
    "rh": "湿度", "temp": "温度",
    "nd": "挠度", "rsg": "应变", "sl": "索力", "lf": "裂缝",
    "xJsd": "X向加速度", "xJd": "X向倾角", "yJd": "Y向倾角",
    "spfs": "风速", "spfx": "风向", "szfs": "竖向风速", "szfx": "竖向风向",
    "Δx": "X方向", "Δy": "Y方向", "Δz": "Z方向",
    "Ax": "X方向", "Ay": "Y方向", "Az": "Z方向",
}

# 特征括号内代号 -> 物理合理范围(超出即视为错误值，统计/绘图前剔除)
FEATURE_RANGES = {
    "rh": (0.0, 100.0),            # 湿度 %
    "temp": (-80.0, 80.0),         # 温度 ℃
    "rsg": (-50000.0, 50000.0),    # 应变 με
    "xJsd": (-10000.0, 10000.0),   # 加速度 mg
    "spfs": (0.0, 100.0),          # 水平风速 m/s
    "szfs": (-100.0, 100.0),       # 竖向风速(带符号) m/s
    "spfx": (0.0, 360.0),          # 风向 °
    "szfx": (-360.0, 360.0),       # 竖向风向(数据为带符号小值)
    "xJd": (-90.0, 90.0),          # 倾角 °
    "yJd": (-90.0, 90.0),
    "nd": (-100000.0, 100000.0),   # 挠度 mm
    "sl": (0.0, 100000.0),         # 索力 KN
    "lf": (-10000.0, 10000.0),     # 裂缝 mm
    "Δx": (-100000.0, 100000.0),   # 位移/空间变位 mm
    "Δy": (-100000.0, 100000.0),
    "Δz": (-100000.0, 100000.0),
}

# 不做统计尖峰替代的特征(只做物理范围过滤)：
#  - spfx/szfx 方向是圆形量，线性"尖峰"没有意义;
#  - spfs/szfs 风速是长尾分布，大风/阵风是真实天气，不是尖峰。
DIRECTION_CODES = {"spfx", "szfx", "spfs", "szfs"}


def feature_range(feature):
    """按特征名(如 WSD(rh)/GNSS(Δx))取物理合理范围，取不到返回 None。"""
    m = re.search(r"\(([^)]+)\)$", feature)
    code = m.group(1) if m else feature
    return FEATURE_RANGES.get(code)


def feature_code(feature):
    m = re.search(r"\(([^)]+)\)$", feature)
    return m.group(1) if m else feature


def feature_display(feature):
    """把 WSD(rh) 之类的特征名补上中文说明。"""
    m = re.search(r"\(([^)]+)\)$", feature)
    if m and m.group(1) in FEATURE_CN:
        return f"{feature}（{FEATURE_CN[m.group(1)]}）"
    return feature


def _fmt_cn_date(d):
    """2026-02-05 -> 2月5日(图上标注用)。"""
    try:
        y, m, day = d.split("-")
        return f"{int(m)}月{int(day)}日"
    except ValueError:
        return d


def clean_series_value(times, values, label, spike_k=5.0, max_run=3,
                       hour_level=True, vrange=None, max_spikes=3):
    """
    尖峰替代 v2：
      0) 物理范围过滤(vrange=(min,max)): 超出合理范围的值(如错误码 435000、
         9e7 等)一律用稳健基线替代，不论连续多长；
      1) 稳健基线: 去掉上下 5% 极端值后的中位数(不被少数大值拉偏);
      2) 稳健尺度: 修剪后的 MAD(1.4826 倍);
      3) 候选异常: |x - 基线| > spike_k * 尺度(仅对范围内值);
      4) 孤立性: 连续 <= max_run 个点的异常段才算尖峰
         (长段是突变/常态区间，留给突变段检测);
      5) 规律性高峰: 小时级数据中，同一小时在多天(>=20%天数)重复出现
         的高点视为常态(如早晚高峰)，不替代只记录;
      6) 数量上限: 满足条件的尖峰若超过 max_spikes(默认3)个，只替代
         偏离最极端的 max_spikes 个，其余视为正常分布尾部，不替代。
    返回 (新序列, 记录, 尖峰替代下标, 范围外替代下标)。
    """
    n = len(values)
    if n < 10:
        return list(values), [], [], []
    arr = np.array(values, dtype=float)
    finite = np.isfinite(arr)
    in_range = finite.copy()
    if vrange:
        rlo, rhi = vrange
        in_range &= (arr >= rlo) & (arr <= rhi)

    # 1) 稳健基线(只用范围内、去 5%~95% 极值的数据)
    base_arr = arr[in_range]
    if base_arr.size < 5:
        return list(values), [], [], []
    lo, hi = np.percentile(base_arr, [5.0, 95.0])
    trim = base_arr[(base_arr >= lo) & (base_arr <= hi)]
    if trim.size < 5:
        return list(values), [], [], []
    base = float(np.median(trim))
    mad = float(np.median(np.abs(trim - base)))
    scale = (1.4826 * mad if mad > 0
             else (float(trim.std()) if trim.std() > 0 else 1.0))
    fixed = arr.copy()
    indices = []
    range_indices = []
    records = []

    # 0) 范围外/非有限值: 一律替代(不论连续长短)
    bad = ~in_range
    if np.any(bad):
        for t in np.flatnonzero(bad):
            fixed[t] = base
            range_indices.append(int(t))
            reason = ("超出合理范围" if finite[t] else "非有限值(inf/nan)")
            records.append({
                "时间": str(times[int(t)]),
                "系列": label,
                "原值": round(float(arr[t]), 6),
                "处理": f"{reason}，用稳健基线替代",
            })

    # 4) 连续段分类
    if spike_k > 0:
        cand = in_range & (np.abs(arr - base) / scale > spike_k)
    else:
        cand = np.zeros(n, dtype=bool)
    runs = []
    i = 0
    while i < n:
        if cand[i]:
            j = i
            while j < n and cand[j]:
                j += 1
            runs.append((i, j - 1))
            i = j
        else:
            i += 1
    spike_pos = set()
    for a, b in runs:
        if b - a + 1 <= max_run:
            spike_pos.update(range(a, b + 1))

    # 5) 规律性高峰(小时级)
    regular_hours = set()
    if hour_level and spike_pos:
        total_days = len({t.date() for t in times})
        by_hour = defaultdict(set)
        for t in spike_pos:
            by_hour[times[t].hour].add(times[t].date())
        for hour, days in by_hour.items():
            if len(days) >= max(3, int(0.2 * total_days)):
                regular_hours.add(hour)

    if hour_level:
        real = sorted(t for t in spike_pos
                      if times[t].hour not in regular_hours)
    else:
        real = sorted(spike_pos)

    if regular_hours:
        records.append({
            "说明": f"规律性高峰小时 {sorted(regular_hours)}，"
                    f"视为常态未替代",
        })
    if not real:
        return fixed.tolist(), records, indices, range_indices

    # 6) 数量上限: 只保留最极端的 max_spikes 个
    if len(real) > max_spikes:
        real = sorted(real, key=lambda t: abs(arr[t] - base),
                      reverse=True)[:max_spikes]
        records.append({
            "说明": f"尖峰候选超过上限，只替代最极端的 {max_spikes} 个，"
                    f"其余视为正常分布尾部未替代",
        })

    for t in real:
        fixed[t] = base
        indices.append(t)
        records.append({
            "时间": str(times[t]),
            "系列": label,
            "原值": round(float(arr[t]), 6),
            "处理": "零散尖峰，用稳健基线替代",
        })
    return fixed.tolist(), records, indices, range_indices


def fill_long_gaps(hours, means, maxs, mins, min_gap_hours=24):
    """
    检测连续缺失 >= min_gap_hours(默认 24 小时)的数据段：
      1) 在有效小时序列中，相邻两点间隔 >= 24h 即为缺段;
      2) 缺失小时用首尾有效值之间的线性插值填充(只用于绘图，不改统计);
      3) 返回 (填充后的 4 个序列, 缺段记录)。
    """
    n = len(hours)
    if n < 2:
        return (list(hours), list(means), list(maxs), list(mins)), []
    filled_hours = list(hours)
    filled_means = list(means)
    filled_maxs = list(maxs)
    filled_mins = list(mins)
    gaps = []
    for i in range(n - 1):
        t0, t1 = hours[i], hours[i + 1]
        gap_h = (t1 - t0).total_seconds() / 3600.0
        if gap_h < min_gap_hours:
            continue
        miss_count = int(round(gap_h)) - 1
        gaps.append({
            "起始时间": t0.strftime("%Y-%m-%d %H:%M"),
            "结束时间": t1.strftime("%Y-%m-%d %H:%M"),
            "缺失小时数": miss_count,
        })
        v0m, v1m = filled_means[i], filled_means[i + 1]
        v0x, v1x = filled_maxs[i], filled_maxs[i + 1]
        v0n, v1n = filled_mins[i], filled_mins[i + 1]
        for k in range(1, int(round(gap_h))):
            frac = k / gap_h
            filled_hours.append(t0 + dt.timedelta(hours=k))
            filled_means.append(v0m + (v1m - v0m) * frac)
            filled_maxs.append(v0x + (v1x - v0x) * frac)
            filled_mins.append(v0n + (v1n - v0n) * frac)
    # 按时间排序(填充点插在中间)
    order = sorted(range(len(filled_hours)), key=lambda k: filled_hours[k])
    filled_hours = [filled_hours[k] for k in order]
    filled_means = [filled_means[k] for k in order]
    filled_maxs = [filled_maxs[k] for k in order]
    filled_mins = [filled_mins[k] for k in order]
    return (filled_hours, filled_means, filled_maxs, filled_mins), gaps


def _find_runs(labels):
    """找出连续非零段 [(起点, 终点)]。"""
    runs = []
    i, n = 0, len(labels)
    while i < n:
        if labels[i] != 0:
            j = i
            while j < n and labels[j] == labels[i]:
                j += 1
            runs.append((i, j - 1))
            i = j
        else:
            i += 1
    return runs


def detect_level_shifts(hours, values, min_days=7, k=2.5, max_iter=4,
                        day_frac=0.6):
    """
    小时级突变段检测：
      1) 用基线(中位数)与稳健尺度给每个小时打 高/低/正常 标签；
      2) 按天统计"偏高/偏低小时占比"，占比 >= day_frac 的天才是突变天；
      3) 3 天多数票平滑，剔除最长段后重估基线(迭代)；
      4) 连续 >= min_days 的突变天合并为区间，起止精确到小时。
    返回 [{起始时间, 结束时间, 方向, 段内平均值, 段内最大/最小值,
           基线平均值}]。
    """
    arr = np.array(values, dtype=float)
    n = len(arr)
    if n < 24 * (min_days + 2) or k <= 0:
        return []
    # 按天分组
    day_index = {}
    for i, h in enumerate(hours):
        day_index.setdefault(h.date().isoformat(), []).append(i)
    day_dates = sorted(day_index)

    med = np.median(arr)
    mad = np.median(np.abs(arr - med))
    scale = 1.4826 * mad if mad > 0 else (arr.std() if arr.std() > 0 else 1.0)
    day_labels = np.zeros(len(day_dates), dtype=int)

    for _ in range(max_iter):
        dev = arr - med
        high = dev > k * scale
        low = dev < -k * scale
        lab = np.zeros(len(day_dates), dtype=int)
        for di, d in enumerate(day_dates):
            pos = day_index[d]
            nh = int(np.count_nonzero(high[pos]))
            nl = int(np.count_nonzero(low[pos]))
            if nh >= day_frac * len(pos) and nh > nl:
                lab[di] = 1
            elif nl >= day_frac * len(pos) and nl > nh:
                lab[di] = -1
        # 3 天多数票平滑，去掉零散标签
        sm = np.zeros(len(day_dates), dtype=int)
        for i in range(len(day_dates)):
            w = lab[max(0, i - 1):min(len(day_dates), i + 2)]
            if np.count_nonzero(w == 1) >= 2:
                sm[i] = 1
            elif np.count_nonzero(w == -1) >= 2:
                sm[i] = -1
        day_labels = sm
        # 剔除最长段后重估基线，避免长突变污染"正常"水平
        runs = _find_runs(day_labels)
        if not runs:
            break
        longest = max(runs, key=lambda r: r[1] - r[0] + 1)
        exclude_days = set(day_dates[longest[0]:longest[1] + 1])
        mask = np.array([h.date().isoformat() not in exclude_days
                         for h in hours])
        if mask.sum() < 24 * 5:
            break
        new_med = np.median(arr[mask])
        if abs(new_med - med) < 1e-12:
            break
        med = new_med
        mad = np.median(np.abs(arr[mask] - med))
        scale = (1.4826 * mad if mad > 0
                 else (arr[mask].std() if arr[mask].std() > 0 else 1.0))

    shifts = []
    for a, b in _find_runs(day_labels):
        if b - a + 1 < min_days:
            continue
        d0, d1 = day_dates[a], day_dates[b]
        start_dt = dt.datetime.fromisoformat(d0)
        end_dt = dt.datetime.fromisoformat(d1).replace(hour=23, minute=59)
        pos = [i for i in range(n)
               if day_dates[a] <= hours[i].date().isoformat() <= day_dates[b]]
        seg = arr[pos]
        shifts.append({
            "起始时间": start_dt.strftime("%Y-%m-%d %H:%M"),
            "结束时间": end_dt.strftime("%Y-%m-%d %H:%M"),
            "方向": "偏高" if day_labels[a] == 1 else "偏低",
            "段内平均值": round(float(seg.mean()), 6),
            "段内最大值": round(float(seg.max()), 6),
            "段内最小值": round(float(seg.min()), 6),
            "基线平均值": round(float(med), 6),
        })
    return shifts


def detect_level_shifts_daily(dates, values, min_days=7, k=2.5, max_iter=4):
    """日级突变段检测(仅在 summary.csv 回退模式下使用)。"""
    arr = np.array(values, dtype=float)
    n = len(arr)
    if n < min_days + 3 or k <= 0:
        return []
    med = np.median(arr)
    mad = np.median(np.abs(arr - med))
    scale = 1.4826 * mad if mad > 0 else (arr.std() if arr.std() > 0 else 1.0)
    labels = np.zeros(n, dtype=int)
    for _ in range(max_iter):
        dev = arr - med
        lab = np.zeros(n, dtype=int)
        lab[dev > k * scale] = 1
        lab[dev < -k * scale] = -1
        sm = np.zeros(n, dtype=int)
        for i in range(n):
            w = lab[max(0, i - 1):min(n, i + 2)]
            if np.count_nonzero(w == 1) >= 2:
                sm[i] = 1
            elif np.count_nonzero(w == -1) >= 2:
                sm[i] = -1
        labels = sm
        runs = _find_runs(labels)
        if not runs:
            break
        longest = max(runs, key=lambda r: r[1] - r[0] + 1)
        mask = np.ones(n, dtype=bool)
        mask[longest[0]:longest[1] + 1] = False
        if mask.sum() < 5:
            break
        new_med = np.median(arr[mask])
        if abs(new_med - med) < 1e-12:
            break
        med = new_med
        mad = np.median(np.abs(arr[mask] - med))
        scale = (1.4826 * mad if mad > 0
                 else (arr[mask].std() if arr[mask].std() > 0 else 1.0))
    shifts = []
    for a, b in _find_runs(labels):
        if b - a + 1 < min_days:
            continue
        seg = arr[a:b + 1]
        shifts.append({
            "起始时间": dates[a] + " 00:00",
            "结束时间": dates[b] + " 23:59",
            "方向": "偏高" if labels[a] == 1 else "偏低",
            "段内平均值": round(float(seg.mean()), 6),
            "段内最大值": round(float(seg.max()), 6),
            "段内最小值": round(float(seg.min()), 6),
            "基线平均值": round(float(med), 6),
        })
    return shifts


def load_sensor_map(path):
    """读取 传感器编号名称.json，返回 {编号: {信息}} 或 {}。"""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("传感器", {})
    except Exception as exc:
        print(f"[警告] 传感器编号名称.json 读取失败: {exc}")
        return {}


def discover_sensor_features(daily_root):
    """扫描 daily/<传感器>/<特征>/ 目录结构。"""
    result = {}
    if not os.path.isdir(daily_root):
        return result
    for sensor in sorted(os.listdir(daily_root)):
        sroot = os.path.join(daily_root, sensor)
        if not os.path.isdir(sroot):
            continue
        feats = []
        for feature in sorted(os.listdir(sroot)):
            froot = os.path.join(sroot, feature)
            if os.path.isdir(froot) and any(
                    fn.lower().endswith(".csv") for fn in os.listdir(froot)):
                feats.append(feature)
        if feats:
            result[sensor] = feats
    return result


def read_hourly_series(feature_dir):
    """
    读取某个特征目录下的全部 daily CSV(每天 24 行小时统计)。
    返回 (hours, hmeans, hmaxs, hmins, day_dates, day_means,
          day_maxs, day_mins, day_secs, day_miss)。
    hours 为每个非空小时的起点(datetime)，对应的小时均值/最大/最小
    分别放在 hmeans/hmaxs/hmins；同时给出清洗前的每日聚合。
    """
    hours, hmeans, hmaxs, hmins = [], [], [], []
    day_dates, day_means, day_maxs, day_mins = [], [], [], []
    day_secs, day_miss = [], []
    for fn in sorted(os.listdir(feature_dir)):
        if not fn.lower().endswith(".csv"):
            continue
        date = fn[:-4]
        path = os.path.join(feature_dir, fn)
        d_means, d_maxs, d_mins, d_secs = [], [], [], 0
        try:
            with open(path, "r", newline="", encoding="utf-8",
                      errors="replace") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                for row in reader:
                    if len(row) < 6:
                        continue
                    try:
                        count = int(row[1])
                        mean = float(row[2])
                        vmin = float(row[3])
                        vmax = float(row[4])
                    except ValueError:
                        continue
                    if (count <= 0 or not (math.isfinite(mean)
                                           and math.isfinite(vmin)
                                           and math.isfinite(vmax))):
                        continue
                    try:
                        t = dt.datetime.fromisoformat(row[0])
                    except ValueError:
                        continue
                    hours.append(t)
                    hmeans.append(mean)
                    hmaxs.append(vmax)
                    hmins.append(vmin)
                    d_means.append(mean)
                    d_maxs.append(vmax)
                    d_mins.append(vmin)
                    d_secs += count
        except OSError:
            continue
        if d_means:
            day_dates.append(date)
            day_means.append(float(np.mean(d_means)))
            day_maxs.append(float(np.max(d_maxs)))
            day_mins.append(float(np.min(d_mins)))
            day_secs.append(d_secs)
            day_miss.append(max(0, 86400 - d_secs))
    return (hours, hmeans, hmaxs, hmins,
            day_dates, day_means, day_maxs, day_mins, day_secs, day_miss)


def aggregate_daily_from_hours(hours, means, maxs, mins):
    """把清洗后的小时序列重新聚合成每日序列(统计口径用)。"""
    by_day = {}
    for h, m, x, n in zip(hours, means, maxs, mins):
        d = h.date().isoformat()
        by_day.setdefault(d, [[], [], []])
        by_day[d][0].append(m)
        by_day[d][1].append(x)
        by_day[d][2].append(n)
    day_dates, day_means, day_maxs, day_mins = [], [], [], []
    day_secs, day_miss = [], []
    for d in sorted(by_day):
        ms, xs, ns = by_day[d]
        day_dates.append(d)
        day_means.append(float(np.mean(ms)))
        day_maxs.append(float(np.max(xs)))
        day_mins.append(float(np.min(ns)))
        day_secs.append(len(ms) * 3600)
        day_miss.append(max(0, 86400 - len(ms) * 3600))
    return (day_dates, day_means, day_maxs, day_mins,
            day_secs, day_miss)


def read_summary(path):
    """
    读取 summary.csv(sensor,feature,date,files,seconds,missing_seconds,
    min,mean,max)，返回 {(sensor, feature): 系列数据}。
    """
    result = {}
    with open(path, "r", newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) < 9:
                continue
            sensor, feature, date = row[0], row[1], row[2]
            try:
                seconds = int(float(row[4]))
                missing = int(float(row[5]))
                vmin = float(row[6])
                vmean = float(row[7])
                vmax = float(row[8])
            except ValueError:
                continue
            # 个别传感器存在 inf/NaN 异常值，跳过该日
            if not (math.isfinite(vmin) and math.isfinite(vmean)
                    and math.isfinite(vmax)):
                continue
            key = (sensor, feature)
            item = result.setdefault(key, {
                "dates": [], "means": [], "maxs": [], "mins": [],
                "seconds": [], "missing": [],
            })
            item["dates"].append(date)
            item["means"].append(vmean)
            item["maxs"].append(vmax)
            item["mins"].append(vmin)
            item["seconds"].append(seconds)
            item["missing"].append(missing)
    # summary.csv 是流式写入的，日期无序；按日期排序
    for item in result.values():
        order = sorted(range(len(item["dates"])),
                       key=lambda i: item["dates"][i])
        for field in ("dates", "means", "maxs", "mins", "seconds", "missing"):
            item[field] = [item[field][i] for i in order]
    return result


def compute_feature_stats(dates, means, maxs, mins, seconds=None,
                          missing=None, start=None, end=None):
    """
    由日期/日均值/日最大/日最小系列计算整体统计值 + 每日统计。
    seconds/missing 为每日有效/缺失秒数(可选，来自 summary.csv 或 daily 明细)。
    返回 (stats_dict, dates, means, maxs, mins)。
    """
    if not dates:
        return None, [], [], [], []

    # 日期过滤
    kept = [(d, m, x, n, (s if seconds else 0),
             (k if missing else 0))
            for d, m, x, n, s, k in zip(
                dates, means, maxs, mins,
                seconds or [0] * len(dates), missing or [0] * len(dates))
            if (not start or d >= start) and (not end or d <= end)]
    if not kept:
        return None, [], [], [], []
    dates, means, maxs, mins, secs, mis = zip(*kept)
    dates = list(dates)
    means = list(means)
    maxs = list(maxs)
    mins = list(mins)
    secs = list(secs)
    mis = list(mis)

    arr = np.array(means, dtype=float)
    daily = []
    for d, m, x, n in zip(dates, means, maxs, mins):
        daily.append({
            "日期": d,
            "平均值": round(float(m), 6),
            "最大值": round(float(x), 6),
            "最小值": round(float(n), 6),
        })

    stats = {
        "起始日期": dates[0],
        "结束日期": dates[-1],
        "覆盖天数": len(dates),
        "有效小时数": 0,
        "缺失小时数": 0,
        "平均值": round(float(np.mean(arr)), 6),
        "中位数": round(float(np.median(arr)), 6),
        "标准差": round(float(np.std(arr)), 6),
        "最大值": round(float(np.max(arr)), 6),
        "最小值": round(float(np.min(arr)), 6),
        "差值": round(float(np.max(arr) - np.min(arr)), 6),
        "最大值_实测": round(float(np.max(maxs)), 6),
        "最小值_实测": round(float(np.min(mins)), 6),
        "绝对最大值": round(
            max(abs(np.max(maxs)), abs(np.min(mins))), 6),
        "均方根值": round(float(np.sqrt(np.mean(np.square(arr)))), 6),
        "每日统计": daily,
    }

    # 有效/缺失小时数：秒数->小时；无秒数时按天估算(每天 24 小时)
    if any(secs):
        total_sec = sum(secs)
        missing_sec = sum(mis)
        stats["有效小时数"] = round(total_sec / 3600, 1)
        stats["缺失小时数"] = round(missing_sec / 3600, 1)
        stats["有效天数"] = sum(1 for s in secs if s > 0)
        stats["缺失天数"] = sum(1 for s in secs if s <= 0)
    else:
        stats["有效小时数"] = len(dates) * 24
        stats["缺失小时数"] = 0
        stats["有效天数"] = len(dates)
        stats["缺失天数"] = 0
    return stats, dates, means, maxs, mins


def _fmt_cn_dt(s):
    """2026-03-26 14:00 -> 3月26日14时(图上标注用)。"""
    try:
        d, t = s.split(" ")
        y, m, day = d.split("-")
        hh = t.split(":")[0]
        return f"{int(m)}月{int(day)}日{int(hh)}时"
    except (ValueError, AttributeError):
        return s


def plot_time_series(sensor_id, sensor_name, feature, times, means, maxs, mins,
                     out_path, shifts=None, replaced_indices=None,
                     replaced_range_indices=None, hour_level=True, gaps=None):
    """
    时间序列图。
    hour_level=True 时按小时描点(一天 24 个点)，横轴仍标日期；
    突变区间着色标注，文字放在图内顶部(不与标题重叠)。
    """
    x = list(range(len(times)))
    fig, ax = plt.subplots(figsize=(15, 5.5))
    if len(times) == 1:
        ax.plot(x, means, "o", color="#1f77b4", markersize=8, label="均值")
        ax.plot(x, maxs, "s", color="#d62728", markersize=6, label="最大值")
        ax.plot(x, mins, "^", color="#2ca02c", markersize=6, label="最小值")
    elif hour_level:
        ax.plot(x, means, "-", color="#1f77b4", linewidth=0.9,
                label="小时均值")
        ax.plot(x, maxs, "-", color="#d62728", linewidth=0.5, alpha=0.55,
                label="小时最大值")
        ax.plot(x, mins, "-", color="#2ca02c", linewidth=0.5, alpha=0.55,
                label="小时最小值")
    else:
        ax.plot(x, means, "-", color="#1f77b4", linewidth=1.6, label="日均值")
        ax.plot(x, maxs, "--", color="#d62728", linewidth=1.0,
                label="日最大值")
        ax.plot(x, mins, "--", color="#2ca02c", linewidth=1.0,
                label="日最小值")

    # 尖峰替代位置打叉标记(黑=统计尖峰, 红=物理范围外)
    if replaced_indices:
        ax.plot([x[i] for i in replaced_indices],
                [means[i] for i in replaced_indices],
                "x", color="black", markersize=8, mew=2,
                label="已替换尖峰点(统计)")
    if replaced_range_indices:
        ax.plot([x[i] for i in replaced_range_indices],
                [means[i] for i in replaced_range_indices],
                "x", color="#d62728", markersize=9, mew=2,
                label="已剔除异常值(物理范围外)")

    # 横轴: 日期刻度(小时模式下按天定位，避免挤)
    if hour_level:
        day_start = {}
        for i, t in enumerate(times):
            day_start.setdefault(t.date().isoformat(), i)
        day_list = sorted(day_start)
        step = max(1, len(day_list) // 15)
        tick_days = day_list[::step]
        ax.set_xticks([day_start[d] for d in tick_days])
        ax.set_xticklabels([_fmt_cn_date(d) for d in tick_days],
                           rotation=30, fontsize=9)
    else:
        step = max(1, len(times) // 15)
        ax.set_xticks(x[::step])
        ax.set_xticklabels([_fmt_cn_date(t) for t in times[::step]],
                           rotation=30, fontsize=9)

    # 突变区间标注: 先给图内顶部留出文字空间，再逐条标注
    if shifts:
        y0, y1 = ax.get_ylim()
        span = (y1 - y0) or 1.0
        ax.set_ylim(y0, y1 + 0.16 * span + 0.04 * span * len(shifts))
        for si, s in enumerate(shifts):
            try:
                t0 = dt.datetime.strptime(s["起始时间"], "%Y-%m-%d %H:%M")
                t1 = dt.datetime.strptime(s["结束时间"], "%Y-%m-%d %H:%M")
                if hour_level:
                    a = min(range(len(times)),
                            key=lambda i: abs((times[i] - t0).total_seconds()))
                    b = min(range(len(times)),
                            key=lambda i: abs((times[i] - t1).total_seconds()))
                else:
                    a = times.index(s["起始时间"][:10])
                    b = times.index(s["结束时间"][:10])
            except (ValueError, KeyError):
                continue
            color = "#d62728" if s["方向"] == "偏高" else "#2ca02c"
            ax.axvspan(a - 0.5, b + 0.5, color=color, alpha=0.12)
            label = (f"{_fmt_cn_dt(s['起始时间'])}~"
                     f"{_fmt_cn_dt(s['结束时间'])} "
                     f"均值{s['段内平均值']:.4g} {s['方向']}")
            ax.text((a + b) / 2.0, y1 + 0.03 * span + si * 0.04 * span,
                    label, ha="center", va="bottom", fontsize=9,
                    color=color, bbox=dict(facecolor="white", alpha=0.75,
                                           pad=1))

    # 数据缺失(>=24h)标注: 着色 + 图内底部文字说明(已用插值填充)
    if gaps:
        y0, y1 = ax.get_ylim()
        span = (y1 - y0) or 1.0
        for gi, g in enumerate(gaps):
            try:
                t0 = dt.datetime.strptime(g["起始时间"], "%Y-%m-%d %H:%M")
                t1 = dt.datetime.strptime(g["结束时间"], "%Y-%m-%d %H:%M")
                a = min(range(len(times)),
                        key=lambda i: abs((times[i] - t0).total_seconds()))
                b = min(range(len(times)),
                        key=lambda i: abs((times[i] - t1).total_seconds()))
            except (ValueError, KeyError):
                continue
            ax.axvspan(a - 0.5, b + 0.5, color="#ff7f0e", alpha=0.18)
            label = (f"数据缺失 {g['缺失小时数']}h: "
                     f"{_fmt_cn_dt(g['起始时间'])}~"
                     f"{_fmt_cn_dt(g['结束时间'])} (已线性填充)")
            ax.text((a + b) / 2.0, y0 + 0.05 * span + gi * 0.05 * span,
                    label, ha="center", va="bottom", fontsize=9,
                    color="#d2691e",
                    bbox=dict(facecolor="white", alpha=0.85, pad=1))

    ax.set_xlabel("日期")
    ax.set_ylabel("数值")
    if sensor_name:
        title = (f"{sensor_name}（编号{sensor_id}）- "
                 f"{feature_display(feature)} 时间序列")
    else:
        title = f"传感器{sensor_id} - {feature_display(feature)} 时间序列"
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=10, title=f"编号 {sensor_id}")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_histogram(sensor_id, sensor_name, feature, means, out_path):
    """频率分布直方图：小时均值分布。"""
    if not means:
        return
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.hist(means, bins=min(40, max(10, len(means) // 5)),
            density=True, alpha=0.75, color="#4c72b0", edgecolor="black")
    ax.set_xlabel("数值")
    ax.set_ylabel("频率")
    if sensor_name:
        title = (f"{sensor_name}（编号{sensor_id}）- "
                 f"{feature_display(feature)} 频率分布")
    else:
        title = f"传感器{sensor_id} - {feature_display(feature)} 频率分布"
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def density2d(x, y):
    """2D 密度估计(参考原脚本的核密度做法)。"""
    if not HAS_SCIPY or len(x) < 5:
        return None
    xi = np.linspace(min(x), max(x), 100)
    yi = np.linspace(min(y), max(y), 100)
    xi, yi = np.meshgrid(xi, yi)
    kernel = scipy_stats.gaussian_kde(np.vstack([x, y]))
    zi = np.reshape(kernel(np.vstack([xi.ravel(), yi.ravel()])).T, xi.shape)
    try:
        from scipy.interpolate import griddata
        grid_z = griddata((xi.flatten(), yi.flatten()), zi.flatten(),
                          (np.column_stack((x, y))[:, 0],
                           np.column_stack((x, y))[:, 1]), method="linear")
        return grid_z
    except Exception:
        return None


def plot_correlation(feat_a, feat_b, x, y, sensor_name, sensor_id, out_path):
    """相关性分析图：密度散点 + 回归直线 + 相关系数。"""
    if len(set(x)) < 2 or len(set(y)) < 2:
        return None  # 常数列无法计算相关系数
    fig, ax = plt.subplots(figsize=(9, 6))
    density = density2d(x, y)
    if density is not None:
        ax.scatter(x, y, c=density, s=8, cmap="GnBu")
    else:
        ax.scatter(x, y, s=8, alpha=0.6, color="#4c72b0")
    slope, intercept, r = np.polyfit(x, y, 1)[0], 0.0, 0.0
    if len(x) >= 2:
        slope, intercept = np.polyfit(x, y, 1)
        r = np.corrcoef(x, y)[0, 1]
    xq = np.linspace(min(x), max(x), 100)
    ax.plot(xq, slope * xq + intercept, "k:", linewidth=1.5, label="回归直线")
    ax.set_title(f"{sensor_name}（编号{sensor_id}）相关性分析")
    ax.set_xlabel(feature_display(feat_a))
    ax.set_ylabel(feature_display(feat_b))
    ax.text(0.03, 0.90, f"y = {slope:.4f}x + {intercept:.4f}\nr = {r:.4f}",
            transform=ax.transAxes, fontsize=11,
            bbox=dict(facecolor="white", alpha=0.8))
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return {"相关系数": round(float(r), 6),
            "斜率": round(float(slope), 6),
            "截距": round(float(intercept), 6)}


# ==================== 合并绘图（按监测部位分组，一张图多测点/多特征） ====================

AXIS_INNER = {"Δx", "Δy", "Δz", "x", "y", "z"}


def feature_group(feature: str) -> str:
    """把特征编码归组：
      - GNSS(Δx/Δy/Δz)、EZJD(xJd/yJd)、DZJSD(xJsd) 等轴/方向分量 -> 同一组(prefix)
      - WSD(rh)/WSD(temp)、WD(temp)、YB(rsg) 等不同测量 -> 各自成组
      - FSFX2(spfs/szfs) 风速、FSFX2(spfx/szfx) 风向 -> 按类成组
    """
    m = re.search(r"^([A-Za-z0-9]+)\(([^)]+)\)$", feature)
    if not m:
        return feature
    prefix, inner = m.group(1), m.group(2)
    if inner in AXIS_INNER or inner.lower() in AXIS_INNER:
        return prefix
    if inner.lower().endswith(("jd", "jsd")):
        return prefix
    if len(inner) >= 2 and inner[-1].lower() in ("s", "x"):
        return f"{prefix}({inner[-1].lower()})"
    return feature


def load_position_map(name_dict_path: str = "", sensor_map_path: str = "") -> dict:
    """返回 {位置名: [(传感器编号, 特征编码), ...]}。
    优先用 传感器名称对照/<桥>.json 的 传感器名称；否则回退 传感器编号名称.json。"""
    pos_map = defaultdict(list)
    if name_dict_path and os.path.isfile(name_dict_path):
        try:
            with open(name_dict_path, encoding="utf-8") as f:
                data = json.load(f)
            for name, entries in (data.get("传感器名称") or {}).items():
                for e in entries or []:
                    feats = e.get("特征编码") or ([e["特征"]] if e.get("特征") else [])
                    for feat in feats:
                        pos_map[name].append((str(e["编号"]), feat))
        except Exception as exc:  # noqa: BLE001
            print(f"[警告] 读取名称对照失败: {exc}")
    if not pos_map and sensor_map_path and os.path.isfile(sensor_map_path):
        try:
            with open(sensor_map_path, encoding="utf-8") as f:
                data = json.load(f)
            for sid, info in (data.get("传感器") or {}).items():
                name = info.get("监测部位") or info.get("名称") or ""
                if name:
                    pos_map[name].append((str(sid), info.get("类别", "")))
        except Exception as exc:  # noqa: BLE001
            print(f"[警告] 读取传感器对照失败: {exc}")
    return {k: sorted(set(v)) for k, v in pos_map.items()}


def _safe_dirname(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", str(s)).strip()


def read_clean_hourly_means(daily_root, sensor, feature, start="", end="",
                            spike_k=5.0, max_spikes=3):
    """读取某 (传感器,特征) 的小时均值序列（已做日期过滤 + 尖峰清洗）。"""
    feat_dir = os.path.join(daily_root, sensor, feature)
    if not os.path.isdir(feat_dir):
        return [], []
    hours, means, _, _ = read_hourly_series(feat_dir)[:4]
    if not hours:
        return [], []
    keep = [i for i, h in enumerate(hours)
            if (not start or h.date().isoformat() >= start)
            and (not end or h.date().isoformat() <= end)]
    hours = [hours[i] for i in keep]
    means = [means[i] for i in keep]
    if spike_k > 0 and hours:
        vrange = feature_range(feature)
        fixed, _, _, _ = clean_series_value(
            hours, means, f"{sensor}-{feature}", spike_k,
            hour_level=True, vrange=vrange, max_spikes=max_spikes)
        means = fixed
    return hours, means


def plot_group_time_series(position, group, series, out_path, dpi=200):
    """同位置同特征组的时间序列图：每条线 = 一个 (传感器,特征) 的小时均值。"""
    n = len(series)
    if n == 0:
        return
    fig, ax = plt.subplots(figsize=(16, 6.5))
    colors = plt.cm.tab10.colors + plt.cm.Set2.colors
    for i, (label, hours, means) in enumerate(series):
        ax.plot(hours, means, "-", linewidth=1.4,
                color=colors[i % len(colors)], label=label)
    all_hours = sorted({h for _, hs, _ in series for h in hs})
    if all_hours:
        day_starts = {}
        for h in all_hours:
            day_starts.setdefault(h.date().isoformat(), h)
        days = sorted(day_starts)
        step = max(1, len(days) // 12)
        ticks = [day_starts[d] for d in days[::step]]
        ax.set_xticks(ticks)
        ax.set_xticklabels([_fmt_cn_date(d) for d in days[::step]],
                           rotation=30, fontsize=11)
    ax.set_xlabel("日期", fontsize=14)
    ax.set_ylabel("小时均值", fontsize=14)
    ax.tick_params(axis="y", labelsize=12)
    ax.set_title(f"{position}｜{group} 小时均值时间序列（{n} 个测点）",
                 fontsize=17)
    ax.grid(True, alpha=0.3)
    if n <= 10:
        ax.legend(loc="best", fontsize=12)
    else:
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.08),
                  ncol=min(5, n), fontsize=10)
    fig.tight_layout()
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def plot_group_histogram(position, group, series, out_path, dpi=200):
    """频率分布图：测点少时叠加一张，多时用子图网格。"""
    n = len(series)
    if n == 0:
        return
    cols = 2
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(14, 3.8 * rows))
    axes = np.array(axes).reshape(-1)
    colors = plt.cm.tab10.colors + plt.cm.Set2.colors
    if n <= 4:
        ax = axes[0]
        for i, (label, _, means) in enumerate(series):
            ax.hist(means, bins=min(40, max(10, len(means) // 5)),
                    density=True, alpha=0.45, color=colors[i % len(colors)],
                    edgecolor="black", linewidth=0.5, label=label)
        ax.set_xlabel("数值", fontsize=13)
        ax.set_ylabel("频率", fontsize=13)
        ax.legend(fontsize=11)
        for j in range(1, len(axes)):
            axes[j].axis("off")
    else:
        for i, (label, _, means) in enumerate(series):
            ax = axes[i]
            ax.hist(means, bins=min(40, max(10, len(means) // 5)),
                    density=True, alpha=0.7, color=colors[i % len(colors)],
                    edgecolor="black")
            ax.set_title(label, fontsize=12)
            ax.grid(True, alpha=0.3)
        for j in range(n, len(axes)):
            axes[j].axis("off")
    fig.suptitle(f"{position}｜{group} 频率分布直方图（{n} 个测点）", fontsize=17)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def plot_group_correlation(position, group, series, out_path, dpi=200):
    """组内特征两两相关：一张含多个子图的图片。"""
    feat_series = defaultdict(list)
    for label, hours, means in series:
        feat = label.split("-", 1)[-1]
        feat_series[feat].extend(zip(hours, means))
    feats = sorted(feat_series)
    pairs = [(a, b) for i, a in enumerate(feats) for b in feats[i + 1:]]
    if not pairs:
        return
    cols = 2
    rows = (len(pairs) + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(14, 5.6 * rows))
    axes = np.array(axes).reshape(-1)
    for k, (a, b) in enumerate(pairs):
        ax = axes[k]
        da = dict(feat_series[a])
        db = dict(feat_series[b])
        xs = [da[t] for t in da if t in db]
        ys = [db[t] for t in da if t in db]
        if len(xs) < 3 or len(set(xs)) < 2 or len(set(ys)) < 2:
            ax.axis("off")
            continue
        slope, intercept = np.polyfit(xs, ys, 1)
        r = np.corrcoef(xs, ys)[0, 1]
        ax.scatter(xs, ys, s=7, alpha=0.5, color="#4c72b0")
        xq = np.linspace(min(xs), max(xs), 100)
        ax.plot(xq, slope * xq + intercept, "k:", linewidth=1.3)
        ax.set_title(f"{a} vs {b}　r = {r:.4f}", fontsize=13)
        ax.grid(True, alpha=0.3)
    for j in range(len(pairs), len(axes)):
        axes[j].axis("off")
    fig.suptitle(f"{position}｜{group} 特征相关性分析", fontsize=17)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description="图库 + 统计值生成(集成版)")
    ap.add_argument("--mode", choices=["merged", "per_sensor"], default="merged",
                    help="merged=按监测部位合并出图(多传感器/多特征一张图，默认)；"
                         "per_sensor=旧的按传感器出图")
    ap.add_argument("--position-map", default="",
                    help="传感器名称对照 json 路径(默认扫描 统计值/传感器名称对照/*.json)")
    ap.add_argument("--dpi", type=int, default=200,
                    help="图片分辨率(默认 200，保证插入 Word 清晰)")
    ap.add_argument("--daily-root", default=DEFAULT_DAILY_ROOT,
                    help="预处理输出的 daily 目录")
    ap.add_argument("--lib-root", default=DEFAULT_LIB_ROOT,
                    help="图库/统计值 的上级目录")
    ap.add_argument("--charts-dir", default="",
                    help="图库输出目录(默认 <lib-root>/图库)")
    ap.add_argument("--stats-dir", default="",
                    help="统计值输出目录(默认 <lib-root>/统计值)")
    ap.add_argument("--sensor-map", default="",
                    help="传感器编号名称.json 路径(默认 统计值/传感器编号名称.json)")
    ap.add_argument("--summary", default="",
                    help="summary.csv 路径(默认 <lib-root>/summary.csv，"
                         "daily 目录不可用时自动使用)")
    ap.add_argument("--correlation", action="store_true",
                    help="同时生成同传感器不同特征间的相关性分析图")
    ap.add_argument("--limit-sensors", type=int, default=0,
                    help="只处理前 N 个传感器(试跑用)")
    ap.add_argument("--rebuild-summary", action="store_true",
                    help="先从 daily 目录重新生成 summary.csv，"
                         "再生成图库(不重跑预处理)")
    ap.add_argument("--spike-threshold", type=float, default=5.0,
                    help="尖峰检测阈值(偏离稳健基线多少个MAD，0=关闭尖峰清洗)")
    ap.add_argument("--max-spikes", type=int, default=3,
                    help="每个序列最多替代几个尖峰(只保留最极端的，默认3)")
    ap.add_argument("--gap-fill-hours", type=float, default=24,
                    help="连续缺失达到该小时数时，图上标注并用线性插值"
                         "填充绘图(默认24小时，0=关闭)")
    ap.add_argument("--shift-threshold", type=float, default=2.5,
                    help="突变段检测阈值(偏离基线多少个尺度，0=关闭突变标注)")
    ap.add_argument("--shift-min-days", type=int, default=7,
                    help="突变段最短持续天数")
    ap.add_argument("--start", default="", help="起始日期 YYYY-MM-DD(可选)")
    ap.add_argument("--end", default="", help="结束日期 YYYY-MM-DD(可选)")
    args = ap.parse_args()

    chart_dir = args.charts_dir or os.path.join(args.lib_root, "图库")
    stats_dir = args.stats_dir or os.path.join(args.lib_root, "统计值")
    os.makedirs(chart_dir, exist_ok=True)
    os.makedirs(stats_dir, exist_ok=True)

    map_path = args.sensor_map or os.path.join(stats_dir, "传感器编号名称.json")
    sensor_map = load_sensor_map(map_path)
    print(f"传感器名称对照: {'已加载(' + str(len(sensor_map)) + '个)' if sensor_map else '未找到'}")

    # 可选: 直接从 daily 重新汇总 summary.csv，不重跑预处理
    if args.rebuild_summary:
        sensor_feats0 = discover_sensor_features(args.daily_root)
        if not sensor_feats0:
            print(f"[错误] --rebuild-summary 需要 daily 目录有数据: {args.daily_root}")
            sys.exit(1)
        summary_path = args.summary or os.path.join(args.lib_root, "summary.csv")
        try:
            import build_summary_from_daily as bsd
        except ImportError:
            print("[错误] 找不到 build_summary_from_daily.py(应与本脚本同目录)")
            sys.exit(1)
        print(f"正在从 daily 重新汇总: {summary_path}")
        n = bsd.rebuild_summary(args.daily_root, summary_path)
        print(f"汇总完成: {n} 行")

    # ---------- 选择数据源 ----------
    source = None
    sensor_feats = discover_sensor_features(args.daily_root)
    if sensor_feats:
        source = "daily"
        print(f"数据源: daily 目录(小时级明细) {args.daily_root}")
    else:
        summary_path = args.summary or os.path.join(args.lib_root, "summary.csv")
        if os.path.exists(summary_path):
            source = "summary"
            print(f"数据源: summary.csv(日级汇总) {summary_path}")
        else:
            print(f"[错误] daily 目录为空且找不到 summary.csv，无法生成图库")
            sys.exit(1)

    # 收集 (传感器, 特征) 列表
    if source == "daily":
        pairs = [(s, f) for s, feats in sorted(sensor_feats.items())
                 for f in feats]
    else:
        summary_data = read_summary(summary_path)
        pairs = sorted(summary_data, key=lambda k: (int(k[0]) if k[0].isdigit()
                                                    else k[0], k[1]))
    sensors = sorted({p[0] for p in pairs})
    if args.limit_sensors:
        sensors = sensors[:args.limit_sensors]
        pairs = [p for p in pairs if p[0] in set(sensors)]
    print(f"共发现 {len(sensors)} 个传感器，开始生成图库...")

    t0 = time.time()
    issues = []   # 失败/数据不足记录
    overview = []
    for idx, sensor in enumerate(sensors, 1):
        info = sensor_map.get(sensor, {})
        sensor_name = info.get("名称", "") or sensor
        bridge = info.get("桥名", "")
        sensor_stats = {
            "编号": sensor,
            "名称": sensor_name,
            "桥名": bridge,
            "生成时间": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "特征统计": {},
        }
        corr_results = {}
        daily_by_feat = {}

        for feature in [f for (s, f) in pairs if s == sensor]:
            try:
                if source == "daily":
                    # ---------- 小时级数据(一天 24 个点) ----------
                    (hours, hmeans, hmaxs, hmins,
                     day_dates, day_means, day_maxs, day_mins,
                     day_secs, day_miss) = read_hourly_series(
                         os.path.join(args.daily_root, sensor, feature))
                    if not hours:
                        issues.append(f"无数据: {sensor}/{feature}")
                        continue
                    keep = [i for i, h in enumerate(hours)
                            if (not args.start
                                or h.date().isoformat() >= args.start)
                            and (not args.end
                                 or h.date().isoformat() <= args.end)]
                    if not keep:
                        issues.append(f"无数据: {sensor}/{feature}")
                        continue
                    hours = [hours[i] for i in keep]
                    hmeans = [hmeans[i] for i in keep]
                    hmaxs = [hmaxs[i] for i in keep]
                    hmins = [hmins[i] for i in keep]

                    # 尖峰清洗: 均值/最大/最小 三个序列各自检测替代
                    spike_rec = []
                    spike_idx = []
                    vrange = feature_range(feature)
                    spike_k = (0 if feature_code(feature) in DIRECTION_CODES
                               else args.spike_threshold)
                    hmeans, r1, ix1, rx1 = clean_series_value(
                        hours, hmeans, "小时均值", spike_k,
                        hour_level=True, vrange=vrange,
                        max_spikes=args.max_spikes)
                    hmaxs, r2, ix2, rx2 = clean_series_value(
                        hours, hmaxs, "小时最大值", spike_k,
                        hour_level=True, vrange=vrange,
                        max_spikes=args.max_spikes)
                    hmins, r3, ix3, rx3 = clean_series_value(
                        hours, hmins, "小时最小值", spike_k,
                        hour_level=True, vrange=vrange,
                        max_spikes=args.max_spikes)
                    spike_rec = r1 + r2 + r3
                    spike_idx = sorted(set(ix1) | set(ix2) | set(ix3))
                    range_idx = sorted(set(rx1) | set(rx2) | set(rx3))

                    # 突变段检测(小时级，按"当天多数小时偏离"判定)
                    shifts = []
                    if args.shift_threshold > 0:
                        shifts = detect_level_shifts(
                            hours, hmeans, args.shift_min_days,
                            args.shift_threshold)

                    # 数据缺失(>=24h)检测: 图上标注，并用线性插值填充绘图序列
                    gaps = []
                    plot_hours, plot_means = hours, hmeans
                    plot_maxs, plot_mins = hmaxs, hmins
                    if args.gap_fill_hours > 0:
                        (plot_hours, plot_means, plot_maxs, plot_mins), gaps = \
                            fill_long_gaps(hours, hmeans, hmaxs, hmins,
                                           args.gap_fill_hours)
                    # 原序列下标 -> 填充后下标(尖峰标记位置对齐)
                    filled_map = {}
                    fi = 0
                    for oi, h in enumerate(hours):
                        while plot_hours[fi] != h:
                            fi += 1
                        filled_map[oi] = fi
                        fi += 1
                    spike_idx_plot = [filled_map[i] for i in spike_idx]
                    range_idx_plot = [filled_map[i] for i in range_idx]

                    # 统计值用"真实数据(已去尖峰, 不含填充值)"
                    (day_dates, day_means, day_maxs, day_mins,
                     day_secs, day_miss) = aggregate_daily_from_hours(
                         hours, hmeans, hmaxs, hmins)
                    stats, day_dates, day_means, day_maxs, day_mins = \
                        compute_feature_stats(
                            day_dates, day_means, day_maxs, day_mins,
                            day_secs, day_miss, None, None)
                    if stats is None:
                        issues.append(f"无数据: {sensor}/{feature}")
                        continue
                    stats["特征"] = feature
                    stats["特征中文名"] = feature_display(feature)
                    stats["预处理"] = {
                        "尖峰替代": spike_rec,
                        "突变区间": shifts,
                        "数据缺失填充": gaps,
                    }
                    sensor_stats["特征统计"][feature] = stats
                    daily_by_feat[feature] = (day_dates, day_means)

                    if args.mode == "per_sensor":
                        fout = os.path.join(chart_dir, sensor, feature)
                        os.makedirs(fout, exist_ok=True)
                        plot_time_series(sensor, sensor_name, feature, plot_hours,
                                         plot_means, plot_maxs, plot_mins,
                                         os.path.join(fout, "时间序列图.png"),
                                         shifts=shifts,
                                         replaced_indices=spike_idx_plot,
                                         replaced_range_indices=range_idx_plot,
                                         gaps=gaps,
                                         hour_level=True)
                        plot_histogram(sensor, sensor_name, feature, hmeans,
                                       os.path.join(fout, "频率分布图.png"))
                else:
                    # ---------- summary 回退(日级) ----------
                    item = summary_data[(sensor, feature)]
                    dates, means = item["dates"], item["means"]
                    maxs, mins = item["maxs"], item["mins"]
                    secs, miss = item["seconds"], item["missing"]
                    kept = [(d, m, x, n, (s if secs else 0),
                             (k if miss else 0))
                            for d, m, x, n, s, k in zip(
                                dates, means, maxs, mins,
                                secs or [0] * len(dates),
                                miss or [0] * len(dates))
                            if (not args.start or d >= args.start)
                            and (not args.end or d <= args.end)]
                    if not kept:
                        issues.append(f"无数据: {sensor}/{feature}")
                        continue
                    dates = [k[0] for k in kept]
                    means = [k[1] for k in kept]
                    maxs = [k[2] for k in kept]
                    mins = [k[3] for k in kept]
                    secs = [k[4] for k in kept]
                    miss = [k[5] for k in kept]

                    spike_rec = []
                    spike_idx = []
                    vrange = feature_range(feature)
                    spike_k = (0 if feature_code(feature) in DIRECTION_CODES
                               else args.spike_threshold)
                    means, r1, ix1, rx1 = clean_series_value(
                        dates, means, "日均值", spike_k,
                        hour_level=False, vrange=vrange,
                        max_spikes=args.max_spikes)
                    maxs, r2, ix2, rx2 = clean_series_value(
                        dates, maxs, "日最大值", spike_k,
                        hour_level=False, vrange=vrange,
                        max_spikes=args.max_spikes)
                    mins, r3, ix3, rx3 = clean_series_value(
                        dates, mins, "日最小值", spike_k,
                        hour_level=False, vrange=vrange,
                        max_spikes=args.max_spikes)
                    spike_rec = r1 + r2 + r3
                    spike_idx = sorted(set(ix1) | set(ix2) | set(ix3))
                    range_idx = sorted(set(rx1) | set(rx2) | set(rx3))
                    shifts = []
                    if args.shift_threshold > 0:
                        shifts = detect_level_shifts_daily(
                            dates, means, args.shift_min_days,
                            args.shift_threshold)
                    stats, dates, means, maxs, mins = compute_feature_stats(
                        dates, means, maxs, mins,
                        secs if any(secs) else None,
                        miss if any(miss) else None, None, None)
                    if stats is None:
                        issues.append(f"无数据: {sensor}/{feature}")
                        continue
                    stats["特征"] = feature
                    stats["特征中文名"] = feature_display(feature)
                    stats["预处理"] = {
                        "尖峰替代": spike_rec,
                        "突变区间": shifts,
                    }
                    sensor_stats["特征统计"][feature] = stats
                    daily_by_feat[feature] = (dates, means)

                    if args.mode == "per_sensor":
                        fout = os.path.join(chart_dir, sensor, feature)
                        os.makedirs(fout, exist_ok=True)
                        plot_time_series(sensor, sensor_name, feature, dates, means,
                                         maxs, mins,
                                         os.path.join(fout, "时间序列图.png"),
                                         shifts=shifts,
                                         replaced_indices=spike_idx,
                                         replaced_range_indices=range_idx,
                                         hour_level=False)
                        plot_histogram(sensor, sensor_name, feature, means,
                                       os.path.join(fout, "频率分布图.png"))
                if stats and len(day_dates if source == "daily" else dates) < 2:
                    stats["提示"] = "数据不足，仅 1 天"
                    issues.append(f"数据不足: {sensor}/{feature} 仅 1 天")
            except Exception as exc:
                issues.append(f"错误: {sensor}/{feature}: {exc}")
                print(f"[警告] {sensor}/{feature} 处理失败: {exc}", flush=True)

        # 相关性分析：同一传感器内两两特征，按日期对齐（仅 per_sensor 模式）
        if args.mode == "per_sensor" and args.correlation and len(daily_by_feat) >= 2:
            feats_list = list(daily_by_feat)
            for i in range(len(feats_list)):
                for j in range(i + 1, len(feats_list)):
                    fa, fb = feats_list[i], feats_list[j]
                    da, ma = daily_by_feat[fa]
                    db, mb = daily_by_feat[fb]
                    common = sorted(set(da) & set(db))
                    if len(common) < 10:
                        continue
                    ia = {d: i for i, d in enumerate(da)}
                    ib = {d: i for i, d in enumerate(db)}
                    x = [ma[ia[d]] for d in common]
                    y = [mb[ib[d]] for d in common]
                    out = os.path.join(chart_dir, sensor,
                                       f"相关性_{fa}_{fb}.png")
                    r = plot_correlation(fa, fb, x, y, sensor_name, sensor,
                                         out)
                    if r is not None:
                        corr_results[f"{fa} ~ {fb}"] = r
        sensor_stats["相关性"] = corr_results

        with open(os.path.join(stats_dir, f"{sensor}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(sensor_stats, f, ensure_ascii=False, indent=2)

        overview.append({
            "编号": sensor, "名称": sensor_name, "桥名": bridge,
            "特征": list(sensor_stats["特征统计"]),
        })
        if idx % 20 == 0 or idx == len(sensors):
            el = time.time() - t0
            print(f"  进度 {idx}/{len(sensors)}  已用 {el:.0f}s", flush=True)

    with open(os.path.join(stats_dir, "总览.json"), "w",
              encoding="utf-8") as f:
        json.dump({
            "说明": "全部传感器-特征图库总览",
            "生成时间": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "传感器数量": len(overview),
            "传感器": overview,
        }, f, ensure_ascii=False, indent=2)

    # ---------- 合并图库（按监测部位分组，一张图多测点/多特征） ----------
    if args.mode == "merged":
        print("开始生成合并图库（按监测部位分组）...")
        pos_map = {}
        if args.position_map and os.path.isfile(args.position_map):
            pos_map = load_position_map(args.position_map)
        else:
            nd_dir = os.path.join(stats_dir, "传感器名称对照")
            if os.path.isdir(nd_dir):
                for f in sorted(os.listdir(nd_dir)):
                    if f.endswith(".json"):
                        pm = load_position_map(os.path.join(nd_dir, f))
                        for k, v in pm.items():
                            pos_map.setdefault(k, []).extend(v)
                pos_map = {k: sorted(set(v)) for k, v in pos_map.items()}
        if not pos_map:
            pos_map = load_position_map("", map_path)
        if not pos_map:
            print("[警告] 未找到位置-传感器映射，合并图库跳过")
        else:
            allowed = set(sensors) if args.limit_sensors else None
            merged_ok = 0
            merged_fail = 0
            for pos, pairs in sorted(pos_map.items()):
                if allowed is not None:
                    pairs = [(s, f) for s, f in pairs if s in allowed]
                if not pairs:
                    continue
                groups = defaultdict(list)
                for sensor, feat in pairs:
                    groups[feature_group(feat)].append((sensor, feat))
                for g, gf_pairs in sorted(groups.items()):
                    series = []
                    uniq_sensors = {s for s, _ in gf_pairs}
                    uniq_feats = {f for _, f in gf_pairs}
                    for sensor, feat in gf_pairs:
                        spike_k = (0 if feature_code(feat) in DIRECTION_CODES
                                   else args.spike_threshold)
                        hours, means = read_clean_hourly_means(
                            args.daily_root, sensor, feat,
                            args.start, args.end, spike_k, args.max_spikes)
                        if not hours:
                            continue
                        if len(uniq_sensors) == 1:
                            label = feat if len(uniq_feats) > 1 else sensor
                        elif len(uniq_feats) == 1:
                            label = sensor
                        else:
                            label = f"{sensor}-{feat}"
                        series.append((label, hours, means))
                    if not series:
                        merged_fail += 1
                        continue
                    out_dir = os.path.join(chart_dir, _safe_dirname(pos),
                                           _safe_dirname(g))
                    os.makedirs(out_dir, exist_ok=True)
                    try:
                        plot_group_time_series(pos, g, series,
                                               os.path.join(out_dir, "时间序列图.png"),
                                               dpi=args.dpi)
                        plot_group_histogram(pos, g, series,
                                             os.path.join(out_dir, "频率分布图.png"),
                                             dpi=args.dpi)
                        if len(uniq_feats) >= 2:
                            plot_group_correlation(pos, g, series,
                                                   os.path.join(out_dir, "相关性图.png"),
                                                   dpi=args.dpi)
                        merged_ok += 1
                    except Exception as exc:  # noqa: BLE001
                        merged_fail += 1
                        issues.append(f"合并图错误: {pos}/{g}: {exc}")
                        print(f"[警告] 合并图失败 {pos}/{g}: {exc}", flush=True)
            print(f"合并图库完成: 成功 {merged_ok} 组，失败 {merged_fail} 组")

    # 失败/数据不足记录
    issue_path = os.path.join(chart_dir, "生成失败记录.txt")
    with open(issue_path, "w", encoding="utf-8") as f:
        f.write(f"图库生成记录\n生成时间: "
                f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"问题总数: {len(issues)}\n")
        f.write("=" * 50 + "\n")
        f.write("\n".join(issues) if issues else "无\n")
    print(f"问题记录已写入: {issue_path} ({len(issues)} 条)")

    print(f"[完成] 共处理 {len(sensors)} 个传感器，"
          f"总耗时 {time.time()-t0:.0f}s")
    print(f"  图库目录: {chart_dir}")
    print(f"  统计值目录: {stats_dir}")


if __name__ == "__main__":
    main()
