#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步校验：对比目标目录里的关键文件与开发机基线（sync_manifest.json）。

用法：
  python check_sync.py --write          # 在开发机生成/更新基线清单
  python check_sync.py --root <项目根>  # 在服务器上校验同步是否完整

对每个文件输出 OK / OUTDATED / MISSING，并对 OUTDATED 给出 MD5 差异，
避免“只同步了部分文件”再次出现（如 conclusions / format_report_number）。
"""

import argparse
import glob
import hashlib
import json
import os
import sys

MANIFEST = "sync_manifest.json"

# 需要同步的核心文件（相对项目根）
PATTERNS = [
    "run_agent.py",
    "analyze_report.py",
    "check_sync.py",
    "requirements.txt",
    "report_agent/*.py",
    "preprocess/scripts/*.py",
    "tests/*.py",
]
# 明确要忽略的文件
IGNORE = {"sync_manifest.json"}


def _files(root: str):
    out = []
    for pat in PATTERNS:
        for p in glob.glob(os.path.join(root, pat)):
            rel = os.path.relpath(p, root).replace("\\", "/")
            if os.path.basename(rel) in IGNORE:
                continue
            out.append(rel)
    return sorted(set(out))


def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def main() -> int:
    ap = argparse.ArgumentParser(description="同步完整性校验")
    ap.add_argument("--root", default=".", help="项目根目录（服务器上为实际运行目录）")
    ap.add_argument("--write", action="store_true",
                    help="在开发机生成基线清单 sync_manifest.json")
    args = ap.parse_args()
    root = os.path.abspath(args.root)

    if args.write:
        manifest = {rel: _md5(os.path.join(root, rel))
                    for rel in _files(root)}
        out = os.path.join(root, MANIFEST)
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"说明": "关键文件 MD5 基线；服务器上运行 check_sync.py 校验",
                       "文件": manifest}, f, ensure_ascii=False, indent=2)
        print(f"已写出 {out}: {len(manifest)} 个文件")
        return 0

    mpath = os.path.join(root, MANIFEST)
    if not os.path.isfile(mpath):
        print(f"[错误] 未找到基线清单 {mpath}（应先同步 sync_manifest.json，"
              f"或在开发机用 --write 生成）", file=sys.stderr)
        return 2
    with open(mpath, encoding="utf-8") as f:
        manifest = json.load(f).get("文件") or {}

    bad = 0
    for rel, expect in sorted(manifest.items()):
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            print(f"MISSING   {rel}")
            bad += 1
            continue
        actual = _md5(p)
        if actual != expect:
            print(f"OUTDATED  {rel}  (server {actual} != dev {expect})")
            bad += 1
        else:
            print(f"OK        {rel}")
    extra = [rel for rel in _files(root) if rel not in manifest]
    for rel in extra:
        print(f"EXTRA     {rel}（开发机清单里没有，可能多出的旧文件）")
    print(f"\n结果: {len(manifest) - bad}/{len(manifest)} 一致"
          + (f"，{bad} 个需要重新同步" if bad else "，同步完整"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
