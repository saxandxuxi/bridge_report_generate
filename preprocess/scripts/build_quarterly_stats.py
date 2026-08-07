#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
季度统计值生成(小时级，按监测部位合并多传感器)
================================================

1) 读取 daily/<传感器>/<特征>/<日期>.csv 的小时级数据；
2) 每个传感器序列先做 物理范围过滤 + 尖峰清洗(与图库同一套逻辑)；
3) 同一监测部位、同一特征有多个传感器时，按小时取各传感器的
   均值(最大值取各传感器最大、最小值取各传感器最小)；
4) 按小时级数据计算季度统计值。

输出: 统计值/季度统计.json
结构: {桥名: {监测部位: {特征: {统计: {...}, 传感器: [...], 剔除异常值: n}}}}

用法:
    python build_quarterly_stats.py [--daily-root ...] [--lib-root ...]
                                    [--start ...] [--end ...]
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from collections import defaultdict

import numpy as np

import build_chart_library as bcl

DEFAULT_DAILY_ROOT = r"D:\preprocess_sensor_data\daily"
DEFAULT_LIB_ROOT = r"D:\Code\桥数据预处理"


def load_sensor_map(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("传感器", {})


def discover_pairs(daily_root):
    """扫描 daily/<传感器>/<特征>/，返回 [(传感器, 特征)]。"""
    pairs = []
    if not os.path.isdir(daily_root):
        return pairs
    for sensor in sorted(os.listdir(daily_root)):
        sroot = os.path.join(daily_root, sensor)
        if not os.path.isdir(sroot):
            continue
        for feat in sorted(os.listdir(sroot)):
            fdir = os.path.join(sroot, feat)
            if os.path.isdir(fdir) and any(
                    f.lower().endswith(".csv") for f in os.listdir(fdir)):
                pairs.append((sensor, feat))
    return pairs


def clean_sensor_series(hours, means, maxs, mins, feature):
    """单传感器清洗：物理范围 + 尖峰(与图库一致)。返回 (hours, m,x,n, 剔除数)。"""
    vrange = bcl.feature_range(feature)
    spike_k = (0 if bcl.feature_code(feature) in bcl.DIRECTION_CODES else 5.0)
    means, r1, _, _ = bcl.clean_series_value(
        hours, means, "小时均值", spike_k, hour_level=True, vrange=vrange)
    maxs, r2, _, _ = bcl.clean_series_value(
        hours, maxs, "小时最大值", spike_k, hour_level=True, vrange=vrange)
    mins, r3, _, _ = bcl.clean_series_value(
        hours, mins, "小时最小值", spike_k, hour_level=True, vrange=vrange)
    removed = len(r1) + len(r2) + len(r3)
    return hours, means, maxs, mins, removed


def compute_quarter_stats(hours, means, maxs, mins):
    arr = np.array(means, dtype=float)
    if arr.size == 0:
        return None
    return {
        "起始时间": hours[0].strftime("%Y-%m-%d %H:%M"),
        "结束时间": hours[-1].strftime("%Y-%m-%d %H:%M"),
        "有效小时数": int(arr.size),
        "覆盖天数": len({h.date() for h in hours}),
        "平均值": round(float(arr.mean()), 6),
        "中位数": round(float(np.median(arr)), 6),
        "标准差": round(float(arr.std()), 6),
        "最大值": round(float(np.max(arr)), 6),
        "最小值": round(float(np.min(arr)), 6),
        "差值": round(float(np.max(arr) - np.min(arr)), 6),
        "最大值_实测": round(float(np.max(maxs)), 6),
        "最小值_实测": round(float(np.min(mins)), 6),
        "绝对最大值": round(
            max(abs(np.max(maxs)), abs(np.min(mins))), 6),
        "均方根值": round(float(np.sqrt(np.mean(np.square(arr)))), 6),
    }


def main():
    ap = argparse.ArgumentParser(description="季度统计值(小时级, 按监测部位合并)")
    ap.add_argument("--daily-root", default=DEFAULT_DAILY_ROOT)
    ap.add_argument("--lib-root", default=DEFAULT_LIB_ROOT)
    ap.add_argument("--sensor-map", default="",
                    help="传感器编号名称.json 路径(默认 统计值/传感器编号名称.json)")
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--limit-sensors", type=int, default=0)
    args = ap.parse_args()

    stats_dir = os.path.join(args.lib_root, "统计值")
    os.makedirs(stats_dir, exist_ok=True)
    map_path = args.sensor_map or os.path.join(stats_dir, "传感器编号名称.json")
    sensor_map = load_sensor_map(map_path)
    print(f"传感器名称对照: {'已加载(' + str(len(sensor_map)) + '个)' if sensor_map else '未找到'}")

    pairs = discover_pairs(args.daily_root)
    if not pairs:
        print(f"[错误] daily 目录为空: {args.daily_root}")
        sys.exit(1)
    if args.limit_sensors:
        keep = set(sorted({p[0] for p in pairs})[:args.limit_sensors])
        pairs = [p for p in pairs if p[0] in keep]

    # 按 (桥, 监测部位, 特征) 分组
    groups = defaultdict(lambda: {"sensors": [], "feature": None})
    for sensor, feat in pairs:
        info = sensor_map.get(sensor, {})
        bridge = info.get("桥名", "")
        loc = info.get("监测部位", "") or sensor
        key = (bridge, loc, feat)
        groups[key]["sensors"].append(sensor)
        groups[key]["feature"] = feat

    t0 = time.time()
    result = {"说明": "季度统计值(小时级, 同监测部位多传感器取均值)，"
                      "统计前已做物理范围过滤与尖峰清洗",
              "生成时间": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "桥": {}}
    done = 0
    for (bridge, loc, feat), g in groups.items():
        hour_map = defaultdict(lambda: {"m": [], "x": [], "n": []})
        removed_total = 0
        for sensor in g["sensors"]:
            fdir = os.path.join(args.daily_root, sensor, feat)
            (hours, means, maxs, mins, _, _, _, _, _, _) = \
                bcl.read_hourly_series(fdir)
            if not hours:
                continue
            keep = [i for i, h in enumerate(hours)
                    if (not args.start or h.date().isoformat() >= args.start)
                    and (not args.end or h.date().isoformat() <= args.end)]
            hours = [hours[i] for i in keep]
            means = [means[i] for i in keep]
            maxs = [maxs[i] for i in keep]
            mins = [mins[i] for i in keep]
            hours, means, maxs, mins, removed = clean_sensor_series(
                hours, means, maxs, mins, feat)
            removed_total += removed
            for h, m, x, n in zip(hours, means, maxs, mins):
                hour_map[h]["m"].append(m)
                hour_map[h]["x"].append(x)
                hour_map[h]["n"].append(n)
        if not hour_map:
            continue
        hours = sorted(hour_map)
        means = [float(np.mean(hour_map[h]["m"])) for h in hours]
        maxs = [float(np.max(hour_map[h]["x"])) for h in hours]
        mins = [float(np.min(hour_map[h]["n"])) for h in hours]
        stats = compute_quarter_stats(hours, means, maxs, mins)
        if stats is None:
            continue
        entry = {
            "统计": stats,
            "传感器": g["sensors"],
            "剔除异常值数": removed_total,
        }
        result["桥"].setdefault(bridge or "未归类", {}).setdefault(
            loc, {})[feat] = entry
        done += 1
        if done % 50 == 0:
            print(f"  进度 {done} 组, 用时 {time.time()-t0:.0f}s", flush=True)

    out = os.path.join(stats_dir, "季度统计.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[完成] 共 {done} 个(部位,特征)组合 -> {out}")


if __name__ == "__main__":
    main()
