#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 1 旁路：占位符识别“规则召回 + LLM 裁决”对照审计。

不修改 annotate_docx/recognize 主链路；只把规则候选导出，让 LLM 对每个
候选给出 keep/replace + 建议 key，落盘 verdicts JSON 供人工抽查：
  1) 规则建议 replace、LLM 说 keep / 规则建议 keep、LLM 说 replace
  2) 规则给了 stats.* key、LLM 给了不同 key
  3) 规则 placeholder 为 None（原会落成 {{data.N}}）但 LLM 能给 key

用法：
  python run_placeholder_audit.py \
      --input inputs/洞庭湖大桥.docx \
      --out outputs/analysis/verdicts_audit.json \
      [--max-candidates 400] [--batch-chars 5000] [--seed 1]
"""

import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import time
from typing import Dict, List, Optional

from report_agent.config import load_config
from report_agent.llm_classifier import LLMClassifier
from report_agent.recognizer import (
    METRIC_WORDS, STAT_WORDS, recognize)
from report_agent.placeholder_arbiter import (
    build_options as _build_options,
    is_obviously_static as _arb_static,
)


def _is_obviously_static(c: Dict) -> bool:
    """规则候选里混入的明显静态项（坐标/规范文号）先剔除，
    让 LLM 审核聚焦在真正需要的“数据 vs 编号”歧义上。"""
    return _arb_static(str(c.get("text") or ""), c.get("position"))


def _key_valid(key: str, metric_keys: set) -> bool:
    """LLM 给出的 key 必须是已知 schema：stats.<metric>.<stat> 或
    cell.<metric>.*，metric/stat 至少能在词表/配置里对上。"""
    if not key:
        return False
    parts = str(key).split(".")
    if parts[0] == "stats" and len(parts) != 3:
        return False
    if parts[0] == "cell" and len(parts) < 4:
        return False
    if len(parts) < 3 or parts[0] not in ("stats", "cell"):
        return False
    metric = parts[1]
    if metric not in metric_keys and metric not in set(
            METRIC_WORDS.values()):
        return False
    _ok_stats = set(STAT_WORDS.values()) | {
        "avg", "max", "min", "range", "abs_max", "rms",
        "count", "ratio", "median", "std"}
    stat = parts[2] if parts[0] == "stats" else parts[-1]
    if stat not in _ok_stats:
        return False
    return True


def _candidates_from_analysis(analysis: dict, max_candidates: int,
                              seed: int, static_filter: bool = True) -> List[Dict]:
    texts = analysis.get("texts") or []
    nums = analysis.get("numbers") or []
    cands = []
    skipped_static = 0
    for n in nums:
        if n.get("verdict") not in ("replace", "review"):
            continue
        para = n.get("paragraph")
        text = ""
        if isinstance(para, int) and 0 <= para < len(texts):
            text = str(texts[para])
        cands.append({
            "id": f"c{len(cands) + 1}",
            "paragraph": para,
            "text": text,
            "value": n.get("value", ""),
            "position": n.get("position", 0),
            "rule_verdict": n.get("verdict"),
            "rule_confidence": n.get("confidence"),
            "rule_placeholder": n.get("placeholder"),
            "rule_reasons": n.get("reasons", []),
        })
    if static_filter:
        kept = []
        for c in cands:
            if _is_obviously_static(c):
                skipped_static += 1
                continue
            kept.append(c)
        cands = kept
    # 优先保留“规则没把握/给不出 key”的候选（最需要语义裁决）；
    # 超出上限再按 seed 抽样其余，保证可复现。
    prio = [c for c in cands
            if c["rule_verdict"] == "review"
            or not c["rule_placeholder"]]
    rest = [c for c in cands if c not in prio]
    random.Random(seed).shuffle(rest)
    out = prio + rest
    out = out[:max_candidates]
    if skipped_static:
        print(f"已过滤明显静态项（坐标/规范文号）{skipped_static} 个",
              flush=True)
    return out


def _batches(cands: List[Dict], batch_chars: int) -> List[List[Dict]]:
    batches, cur, size = [], [], 0
    for c in cands:
        add = len(c.get("text") or "") + 80
        if cur and size + add > batch_chars:
            batches.append(cur)
            cur, size = [], 0
        cur.append(c)
        size += add
    if cur:
        batches.append(cur)
    return batches


SYSTEM_PROMPT = (
    "你是桥梁监测报告数字识别审核员。我会给你一批数字候选，每个候选包含"
    "所在段落全文、数字值、数字前后文字、规则判定和规则建议。"
    "你的任务：判断该数字是否应被替换成动态统计占位符（replace），"
    "还是保持原文（keep）。替换仅当它是“本报告期实测数据/由数据源决定的值”，"
    "如温度、应变、位移、索力、风速、日期范围等；表号、图号、章节号、"
    "测点编号、桩号、规范固定阈值、设计参数、传感器编号、编号年份等保持原文。"
    "若 replace，只能在候选给出的 options 列表里选一个 key；options 为空或"
    "没有合适项时，metric/stat 必须为 null，绝对不要自己拼 key。只输出 JSON。"
)

# 占位符 key 允许的统计量（只能从中选，不能自造）
_STAT_KEYS = sorted(set(STAT_WORDS.values()) | {
    "avg", "max", "min", "range", "abs_max", "rms",
    "count", "ratio", "median", "std"})


def _options_for(c: Dict) -> List[str]:
    """按段落上下文生成该候选可选的 key 列表（LLM 只能从中选）。"""
    return _build_options(str(c.get("text") or ""),
                          str(c.get("rule_placeholder") or ""))


def _ask(clf: LLMClassifier, batch: List[Dict]) -> Optional[List[Dict]]:
    items = []
    for c in batch:
        items.append({
            "id": c["id"],
            "value": c["value"],
            "text": (c.get("text") or "")[:2000],
            "rule_verdict": c["rule_verdict"],
            "rule_placeholder": c["rule_placeholder"],
            "rule_confidence": c["rule_confidence"],
            "options": c.get("options") or [],
        })
    user = (
        "请对以下候选逐条裁决：\n"
        + json.dumps(items, ensure_ascii=False)
        + '\n返回 JSON 数组：[{"id":"c1","verdict":"keep|replace",'
          '"key":null 或 options 中的一个字符串,"confidence":0~1,'
          '"reason":"简短原因"}]'
)
    raw = clf._chat([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ])
    return clf._parse_json_loose(raw)


def main() -> int:
    ap = argparse.ArgumentParser(description="占位符识别 LLM 裁决旁路审计")
    ap.add_argument("--input", required=True, help="源报告 .docx")
    ap.add_argument("--config", default="config/config.json")
    ap.add_argument("--out", default="outputs/analysis/verdicts_audit.json")
    ap.add_argument("--max-candidates", type=int, default=400)
    ap.add_argument("--batch-chars", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-static-filter", action="store_true",
                    help="不过滤 A(5,3)/JTG 规范文号等明显静态候选")
    args = ap.parse_args()

    cfg = {}
    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        cfg = {"llm": {"enabled": True}}
    llm_cfg = cfg.get("llm") or {}
    metric_keys = set((cfg.get("bridge_data") or {}).get(
        "metrics", {}).keys())

    print(f"规则识别（无 LLM 干扰）: {args.input}", flush=True)
    t0 = time.time()
    analysis = recognize(args.input, llm_cfg=None)
    print(f"识别完成 {time.time() - t0:.0f}s，数字候选 "
          f"{sum(1 for n in analysis.get('numbers', [])
                 if n.get('verdict') in ('replace', 'review'))} 个",
          flush=True)

    cands = _candidates_from_analysis(analysis, args.max_candidates,
                                      args.seed,
                                      static_filter=not args.no_static_filter)
    for c in cands:
        c["options"] = _options_for(c)
    print(f"送入 LLM 候选 {len(cands)} 个", flush=True)
    if not cands:
        print("无候选，退出")
        return 0

    clf = LLMClassifier(llm_cfg)
    if not clf.available():
        print("[错误] LLM 不可用（api_key/api_base/enabled），"
              "旁路需要 LLM 才能出裁决", file=sys.stderr)
        return 2

    batches = _batches(cands, args.batch_chars)
    verdicts: List[Dict] = []
    t0 = time.time()
    for i, batch in enumerate(batches, 1):
        decided = {}
        try:
            resp = _ask(clf, batch)
            if isinstance(resp, list):
                for r in resp:
                    if isinstance(r, dict) and r.get("id"):
                        decided[str(r["id"])] = r
        except Exception as exc:  # noqa: BLE001
            print(f"批次 {i} 失败，回退规则: {exc}", flush=True)
        for c in batch:
            d = decided.get(c["id"])
            lv = d.get("verdict") if isinstance(d, dict) else None
            if lv not in ("keep", "replace"):
                lv = c["rule_verdict"]
            lkey = (d.get("key") if isinstance(d, dict)
                    else None)
            lkey_raw = lkey
            in_options = bool(lkey and lkey in (c.get("options") or []))
            if lv == "replace" and not in_options:
                lkey = None      # 越界自造 key 一律作废
            lconf = d.get("confidence") if isinstance(d, dict) else None
            lreason = (d.get("reason") if isinstance(d, dict) else "")
            agree_verdict = (lv == c["rule_verdict"])
            agree_key = (
                agree_verdict
                and (lv != "replace"
                     or (lkey or "") == (c["rule_placeholder"] or ""))
            )
            verdicts.append({
                **c,
                "llm_verdict": lv,
                "llm_key": lkey,
                "llm_key_raw": lkey_raw,
                "llm_key_in_options": in_options,
                "llm_key_valid": (in_options and _key_valid(lkey, metric_keys))
                if lv == "replace" and lkey else None,
                "llm_confidence": lconf,
                "llm_reason": lreason,
                "verdict_agree": agree_verdict,
                "key_agree": agree_key,
            })
        if i % 5 == 0 or i == len(batches):
            print(f"  批次 {i}/{len(batches)} "
                  f"({time.time() - t0:.0f}s)", flush=True)

    out = {
        "说明": "Phase1 占位符规则召回+LLM裁决对照审计（旁路，不影响主链路）",
        "生成时间": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "源报告": os.path.abspath(args.input),
        "参数": {"max_candidates": args.max_candidates,
                 "batch_chars": args.batch_chars, "seed": args.seed},
        "汇总": {
            "候选数": len(verdicts),
            "verdict一致": sum(1 for v in verdicts if v["verdict_agree"]),
            "key一致": sum(1 for v in verdicts if v["key_agree"]),
            "LLM说keep规则说replace": sum(
                1 for v in verdicts
                if v["llm_verdict"] == "keep"
                and v["rule_verdict"] == "replace"),
            "LLM说replace规则说review/keep": sum(
                1 for v in verdicts
                if v["llm_verdict"] == "replace"
                and v["rule_verdict"] != "replace"),
            "规则无key但LLM给了key": sum(
                1 for v in verdicts
                if not v["rule_placeholder"] and v.get("llm_key")),
            "LLM从选项中选择": sum(
                1 for v in verdicts if v.get("llm_key_in_options")),
            "LLM越界自造key作废": sum(
                1 for v in verdicts
                if v["llm_verdict"] == "replace"
                and v.get("llm_key_raw")
                and not v.get("llm_key_in_options")),
            "options为空": sum(
                1 for v in verdicts if not v.get("options")),
            "LLM给的key合法": sum(
                1 for v in verdicts if v.get("llm_key_valid") is True),
            "LLM给的key非法": sum(
                1 for v in verdicts if v.get("llm_key_valid") is False),
            "低置信(<0.6)": sum(
                1 for v in verdicts
                if isinstance(v.get("llm_confidence"), (int, float))
                and v["llm_confidence"] < 0.6),
        },
        "候选": verdicts,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n汇总: {json.dumps(out['汇总'], ensure_ascii=False)}")
    print(f"已写出: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
