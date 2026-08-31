#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把桥服务器上的日级预处理数据（daily）同步到接收方服务器（SFTP）。

设计：
  - 脚本跑在桥服务器（Windows）上；
  - 源目录默认取 preprocess/config.json 的 daily_dir（如 E:/preprocess_sensor_data）；
  - 整库同步：不带期号参数时，同步 daily_dir 下全部内容；
  - 按期同步：--start/--end 或 --year/--quarter 指定期，只同步 daily_<期> 目录；
  - 目标目录里没有日级数据时，先调用 preprocess/scripts/preprocess_sensor_data.py
    生成，再传输；
  - 接收方已有且大小一致的文件自动跳过（断点续传，重跑不重复传）。

配置：sync_daily_receiver.json（可用 --config 指定），示例见
sync_daily_receiver.example.json。密码可放配置里，或用环境变量覆盖。

用法（在项目根目录或 sync_daily/ 目录下均可）：
  python sync_daily/sync_daily_to_receiver.py                              # 整库同步
  python sync_daily/sync_daily_to_receiver.py --year 2026 --quarter 1      # 只同步 2026Q1
  python sync_daily/sync_daily_to_receiver.py --start 2026-01-01 --end 2026-03-31
  python sync_daily/sync_daily_to_receiver.py --dry-run                    # 只统计不传输
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CONFIG_FILE = SCRIPT_DIR / "sync_daily_receiver.json"
EXAMPLE_FILE = SCRIPT_DIR / "sync_daily_receiver.example.json"
PREPROCESS_CONFIG = PROJECT_ROOT / "preprocess" / "config.json"
PREPROCESS_SCRIPT = PROJECT_ROOT / "preprocess" / "scripts" / "preprocess_sensor_data.py"
ENV_PASSWORD = "SYNC_SFTP_PASSWORD"

log = logging.getLogger("sync-daily")


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(SCRIPT_DIR / "sync_daily.log", encoding="utf-8"),
        ],
    )


def load_json(path):
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_config(args):
    cfg = load_json(args.config)
    # 桥名：命令行 > 配置
    bridge = args.bridge or cfg.get("bridge", "")
    # 源日级目录：命令行 > 配置 > preprocess/config.json 的 daily_dir
    source = args.source_dir or cfg.get("source_daily_dir", "")
    if not source:
        pc = load_json(PREPROCESS_CONFIG)
        source = pc.get("daily_dir", "")
    if not source:
        raise SystemExit("未找到源日级目录：请用 --source-dir 或配置 source_daily_dir")
    if not bridge:
        raise SystemExit("未指定桥名：请用 --bridge 或配置 bridge")
    receiver = dict(cfg.get("receiver", {}) or {})
    receiver["host"] = args.host or receiver.get("host", "")
    receiver["port"] = args.port or receiver.get("port", 22)
    receiver["username"] = args.user or receiver.get("username", "")
    receiver["target_dir"] = args.target_dir or receiver.get("target_dir", "D:/")
    password = args.password or receiver.get("password", "")
    if not password:
        password = os.environ.get(receiver.get("password_env") or ENV_PASSWORD, "")
    receiver["password"] = password
    if not receiver["host"] or not receiver["username"] or not receiver["password"]:
        raise SystemExit("接收方 host/username/password 未配置完整（配置或环境变量）")
    return {
        "bridge": bridge,
        "source": os.path.normpath(os.path.expandvars(source)),
        "receiver": receiver,
        "raw_data_dir": args.raw_data_dir or cfg.get("raw_data_dir", ""),
    }


def period_tag(start="", end=""):
    """起止日期 -> 期号标签，与 preprocess 一致（2026.1~3 / 2026.07 / 2026）。"""
    def _parse(s):
        try:
            return date.fromisoformat(str(s).strip())
        except (ValueError, AttributeError):
            return None
    d0, d1 = _parse(start), _parse(end)
    if not d0 or not d1:
        return ""
    if d0.year == d1.year:
        if d0.month == d1.month:
            return f"{d0.year}.{d0.month:02d}"
        return f"{d0.year}.{d0.month}~{d1.month}"
    return f"{d0.year}.{d0.month}~{d1.year}.{d1.month}"


def quarter_range(year, quarter):
    start = date(year, (quarter - 1) * 3 + 1, 1)
    if quarter == 4:
        end = date(year, 12, 31)
    else:
        end = date(year, quarter * 3 + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def resolve_period(args):
    """返回 (start, end, tag)；None 表示整库同步。"""
    start, end = args.start, args.end
    if args.quarter:
        y = args.year or datetime.now().year
        start, end = quarter_range(y, args.quarter)
    elif args.year and not args.start:
        start, end = f"{args.year}-01-01", f"{args.year}-12-31"
    if start or end:
        return start or "", end or "", period_tag(start, end)
    return None


def find_period_dirs(root, tag):
    """在源目录下找 daily_<tag> 目录（如 daily_2026.1~3）。"""
    wanted = f"daily_{tag}" if tag else "daily"
    found = []
    for dirpath, dirnames, _ in os.walk(root):
        if os.path.basename(dirpath) == wanted:
            found.append(dirpath)
            dirnames[:] = []
    return found


def collect_files(source, period_dirs):
    """返回 [(本地绝对路径, 相对 source 的路径), ...]"""
    files = []
    if period_dirs:
        roots = period_dirs
    else:
        roots = [source]
    for root in roots:
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, source)
                files.append((full, rel))
    return files


def run_preprocess(cfg, start="", end=""):
    """缺失日级数据时调用预处理脚本生成。"""
    script = PREPROCESS_SCRIPT
    if not script.is_file():
        raise SystemExit(f"找不到预处理脚本: {script}")
    raw = cfg["raw_data_dir"]
    if not raw:
        pc = load_json(PREPROCESS_CONFIG)
        raw = pc.get("raw_data_dir", "")
    if not raw:
        raise SystemExit("未配置 raw_data_dir，无法自动生成日级数据（可用 --raw-data-dir）")
    output_root = os.path.join(cfg["source"], cfg["bridge"])
    cmd = [sys.executable, str(script), "--mode", "all",
           "--data-root", os.path.expandvars(raw),
           "--output-root", output_root,
           "--bridge", cfg["bridge"]]
    if start:
        cmd += ["--start", start]
    if end:
        cmd += ["--end", end]
    log.info("日级数据缺失，开始预处理: %s", " ".join(cmd))
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise SystemExit(f"预处理失败，退出码 {proc.returncode}")


def ensure_remote_dir(sftp, path):
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    cur = ""
    for p in parts:
        cur = cur + "/" + p
        try:
            sftp.stat(cur)
        except FileNotFoundError:
            try:
                sftp.mkdir(cur)
            except OSError:
                pass


def sync_files(sftp, files, target_dir, dry_run):
    sent = skipped = 0
    failed = []
    target = target_dir.replace("\\", "/").rstrip("/")
    for i, (local, rel) in enumerate(files, 1):
        remote = target + "/" + rel.replace("\\", "/")
        size = os.path.getsize(local)
        if dry_run:
            continue
        try:
            try:
                st = sftp.stat(remote)
                if st.st_size == size:
                    skipped += 1
                    if i % 200 == 0:
                        log.info("进度 %d/%d（跳过 %d）", i, len(files), skipped)
                    continue
            except FileNotFoundError:
                pass
            ensure_remote_dir(sftp, os.path.dirname(remote))
            sftp.put(local, remote)
            sent += 1
            if i % 100 == 0:
                log.info("进度 %d/%d（已传 %d）", i, len(files), sent)
        except Exception as e:  # noqa: BLE001
            failed.append((rel, str(e)))
            log.error("传输失败 %s: %s", rel, e)
    return sent, skipped, failed


def main():
    ap = argparse.ArgumentParser(description="同步日级预处理数据到接收方服务器(SFTP)")
    ap.add_argument("--config", default=CONFIG_FILE, help="配置文件（默认 sync_daily_receiver.json）")
    ap.add_argument("--bridge", default="", help="桥名（覆盖配置）")
    ap.add_argument("--source-dir", default="", help="源 daily 目录（覆盖配置）")
    ap.add_argument("--raw-data-dir", default="", help="原始数据目录（缺失时生成用）")
    ap.add_argument("--start", default="", help="开始日期 YYYY-MM-DD")
    ap.add_argument("--end", default="", help="结束日期 YYYY-MM-DD")
    ap.add_argument("--year", type=int, default=0, help="年度（配合 --quarter 或单独年度）")
    ap.add_argument("--quarter", type=int, default=0, help="季度 1-4")
    ap.add_argument("--host", default="", help="接收方 IP/主机（覆盖配置）")
    ap.add_argument("--port", type=int, default=0, help="接收方 SFTP 端口（默认配置或 22）")
    ap.add_argument("--user", default="", help="接收方用户名（覆盖配置）")
    ap.add_argument("--password", default="", help="接收方密码（优先环境变量，不推荐命令行）")
    ap.add_argument("--target-dir", default="", help="接收方目标目录（默认 D:/）")
    ap.add_argument("--skip-preprocess", action="store_true", help="缺失时不自动生成，直接报错")
    ap.add_argument("--dry-run", action="store_true", help="只统计要传输的文件，不连接服务器")
    args = ap.parse_args()

    setup_logging()
    cfg = load_config(args)
    period = resolve_period(args)
    log.info("桥: %s | 源目录: %s | 接收方: %s:%s -> %s",
             cfg["bridge"], cfg["source"], cfg["receiver"]["host"],
             cfg["receiver"]["port"], cfg["receiver"]["target_dir"])

    # 1. 找出要同步的目录；缺失则先生成
    if period:
        start, end, tag = period
        log.info("同步期号: %s (%s ~ %s)", tag or "全部", start or "-", end or "-")
        dirs = find_period_dirs(cfg["source"], tag)
        if not dirs and not args.skip_preprocess:
            run_preprocess(cfg, start, end)
            dirs = find_period_dirs(cfg["source"], tag)
        if not dirs:
            raise SystemExit(f"源目录下没有 {tag or 'daily'} 日级数据，请检查或先跑预处理")
        log.info("匹配到日级目录: %s", dirs)
        files = collect_files(cfg["source"], dirs)
    else:
        if not os.path.isdir(cfg["source"]) or not any(os.scandir(cfg["source"])):
            if args.skip_preprocess:
                raise SystemExit(f"源目录为空或不存在: {cfg['source']}")
            run_preprocess(cfg)
        files = collect_files(cfg["source"], None)

    if not files:
        raise SystemExit("没有需要同步的文件")
    total_size = sum(os.path.getsize(p) for p, _ in files) / 1024 / 1024
    log.info("待同步文件 %d 个，共 %.1f MB", len(files), total_size)
    if args.dry_run:
        log.info("--dry-run 结束，未连接服务器")
        return

    # 2. SFTP 传输
    try:
        import paramiko
    except ImportError:
        raise SystemExit("缺少 paramiko，请先安装: pip install paramiko")
    recv = cfg["receiver"]
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(hostname=recv["host"], port=int(recv["port"]),
                    username=recv["username"], password=recv["password"],
                    timeout=30)
        sftp = ssh.open_sftp()
        try:
            ensure_remote_dir(sftp, recv["target_dir"])
            sent, skipped, failed = sync_files(sftp, files, recv["target_dir"], False)
        finally:
            sftp.close()
    finally:
        ssh.close()

    log.info("传输完成: 新传 %d，跳过 %d，失败 %d", sent, skipped, len(failed))
    if failed:
        log.error("失败文件示例: %s", failed[:10])
        sys.exit(1)


if __name__ == "__main__":
    main()
