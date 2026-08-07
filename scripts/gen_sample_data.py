#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Generate sample data files for cable_clamp, rotation, wind_speed, vehicle_count."""

import csv
import datetime as dt
import random
import os

random.seed(42)  # Reproducible

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# Date range: match temperature data (2026-06-01 to 2026-08-03)
start = dt.date(2026, 6, 1)
end = dt.date(2026, 8, 3)
dates = []
d = start
while d <= end:
    dates.append(d)
    d += dt.timedelta(days=1)

print(f"Generating data for {len(dates)} days ({start} to {end})")


def write_csv(filename, columns, gen_func):
    path = os.path.join(DATA_DIR, filename)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date"] + columns)
        for d in dates:
            row = [d.isoformat()]
            for col in columns:
                row.append(f"{gen_func(col, d):.2f}")
            writer.writerow(row)
    print(f"  Written: {path} ({len(dates)} rows, {len(columns)} columns)")


# 1. Cable clamp displacement (mm) - small values, slow drift
cable_clamp_cols = ["h_87L_1", "h_87R_1", "h_88L_1", "h_88R_1",
                    "h_87L_2", "h_87R_2", "h_88L_2", "h_88R_2"]
base_clamp = {col: random.uniform(0.5, 2.0) for col in cable_clamp_cols}

def gen_cable_clamp(col, date):
    day_num = (date - start).days
    # Slow drift + daily noise
    drift = 0.01 * day_num
    noise = random.gauss(0, 0.15)
    return max(0, base_clamp[col] + drift + noise)

write_csv("cable_clamp.csv", cable_clamp_cols, gen_cable_clamp)


# 2. Rotation (degrees) - small angles, oscillating
rotation_cols = ["m_junshan_x", "m_junshan_y", "m_yueyang_x", "m_yueyang_y"]
base_rot = {col: random.uniform(-0.1, 0.1) for col in rotation_cols}

def gen_rotation(col, date):
    day_num = (date - start).days
    # Sinusoidal + noise
    period = random.uniform(20, 40)
    amplitude = random.uniform(0.05, 0.15)
    sinusoid = amplitude * (day_num / period * 2 * 3.14159)
    noise = random.gauss(0, 0.02)
    return base_rot[col] + sinusoid + noise

write_csv("rotation.csv", rotation_cols, gen_rotation)


# 3. Wind speed (m/s) - varying, some gusts
wind_cols = ["j_junshan_top", "j_yueyang_top",
             "j_half_upstream", "j_half_downstream", "j_10min_avg"]

def gen_wind(col, date):
    day_num = (date - start).days
    # Base wind varies seasonally (higher in summer storms)
    seasonal = 5 + 3 * (day_num / 60)  # increases over summer
    # Daily variation
    daily = 2 * (day_num % 7 - 3)  # weekly cycle
    # Location factor
    loc_factor = {"j_junshan_top": 1.3, "j_yueyang_top": 1.25,
                  "j_half_upstream": 0.9, "j_half_downstream": 0.85,
                  "j_10min_avg": 0.8}.get(col, 1.0)
    base = max(0.5, (seasonal + daily) * loc_factor)
    noise = random.gauss(0, 1.5)
    return max(0, base + noise)

write_csv("wind_speed.csv", wind_cols, gen_wind)


# 4. Vehicle count (daily count per lane) - weekday/weekend pattern
vehicle_cols = ["lane_1", "lane_2", "lane_3", "lane_4", "lane_5", "lane_6"]
lane_base = {col: random.randint(8000, 15000) for col in vehicle_cols}

def gen_vehicle(col, date):
    day_num = (date - start).days
    weekday = date.weekday()
    # Weekend reduction (20% less)
    weekend_factor = 0.8 if weekday >= 5 else 1.0
    # Slow growth trend
    growth = 1.0 + 0.002 * day_num
    # Daily noise
    noise = random.gauss(1.0, 0.1)
    return int(lane_base[col] * weekend_factor * growth * noise)

# Vehicle count needs integer values
path = os.path.join(DATA_DIR, "vehicle_count.csv")
with open(path, "w", encoding="utf-8-sig", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["date"] + vehicle_cols)
    for d in dates:
        row = [d.isoformat()]
        for col in vehicle_cols:
            row.append(gen_vehicle(col, d))
        writer.writerow(row)
print(f"  Written: {path} ({len(dates)} rows, {len(vehicle_cols)} columns)")

print("\nAll data files generated successfully!")
