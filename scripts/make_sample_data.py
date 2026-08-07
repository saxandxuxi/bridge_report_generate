#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成示例数据：data/temperature_daily.csv（日期 + 温度 + 湿度）。"""

import csv
import datetime as dt
import math
import os
import random


def main() -> None:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(base, "data", "temperature_daily.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)

    random.seed(42)
    start = dt.date(2026, 6, 1)
    end = dt.date(2026, 8, 3)

    rows = []
    d = start
    while d <= end:
        elapsed = (d - start).days
        # 季节趋势 + 昼夜波动 + 噪声
        season = 25.0 + 4.5 * math.sin(elapsed / 64.0 * math.pi)
        cycle = 3.0 * math.sin(d.toordinal() % 14 / 14.0 * 2 * math.pi)
        temp = season + cycle + random.uniform(-2.2, 2.2)
        humidity = 62 + 14 * math.sin(d.toordinal() % 9 / 9.0 * 2 * math.pi) + random.uniform(-7, 7)
        rows.append([d.isoformat(), round(temp, 1), round(humidity, 1)])
        d += dt.timedelta(days=1)

    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "temperature", "humidity"])
        writer.writerows(rows)

    print(f"已生成示例数据: {out}（{len(rows)} 行，{start} ~ {end}）")


if __name__ == "__main__":
    main()
