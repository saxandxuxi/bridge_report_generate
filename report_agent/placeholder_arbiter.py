# -*- coding: utf-8 -*-
"""占位符识别：规则召回 + LLM 受限裁决（Phase 2 主链路）。

规则负责尽量召回（classify_number 的 replace/review），LLM 只在
给定 `options` 内裁决 keep/replace 并选择 key，不允许自造 key。
LLM 不可用、超时或解析失败时按批次回退规则，不影响生成流程。
"""

import json
import logging
import random
import re
import time
from typing import Dict, List, Optional

from .llm_classifier import LLMClassifier
from .recognizer import METRIC_WORDS, STAT_WORDS

log = logging.getLogger("report-agent.placeholder_arbiter")

_STAT_KEYS = sorted(set(STAT_WORDS.values()) | {
    "avg", "max", "min", "range", "abs_max", "rms",
    "count", "ratio", "median", "std"})

SYSTEM_PROMPT = (
    "你是桥梁监测报告数字识别审核员。判断给定数字是否应替换为动态统计"
    "占位符（replace）还是保持原文（keep）。替换仅当它是本报告期实测数据/"
    "由数据源决定的值（温度、应变、位移、索力、风速、日期范围等）；"
    "表号、图号、章节号、测点编号、桩号、规范固定阈值、设计参数、"
    "布设数量、证书编号等保持原文。"
    "若 replace，只能在候选给出的 options 列表里选一个 key；options 为空或"
    "没有合适项时 key 必须为 null，绝对不要自己拼 key。只输出 JSON。"
)

# 规则候选里混入的明显静态项（坐标/规范文号/图表ID片段），
# 送 LLM 前剔除，避免样本被无效候选淹没。
_STATIC_RES = [
    re.compile(r"[A-Z]\(\d+\s*,\s*\d+\)"),
    re.compile(r"(?:JTG|GB|JT/T|交办公路|公路[^\s]{0,6}号)"),
    re.compile(r"\[\d{4}\]\s*\d+\s*号"),
    re.compile(r"(?:time_series|histogram|trend|scatter)\b"),
]


def is_obviously_static(text: str, position: Optional[int] = None) -> bool:
    """判断数字是否为明显静态项。

    position 给定时只看数字附近的局部窗口，避免“同段有《规范》引用”
    就把段落里的真实数据（如最高湿度 100.55%）也误判为静态。
    """
    t = str(text or "")
    if position is not None and t:
        try:
            p = int(position)
        except (TypeError, ValueError):
            p = -1
        if 0 <= p <= len(t):
            t = t[max(0, p - 12):min(len(t), p + 12)]
    return any(rx.search(t) for rx in _STATIC_RES)


def build_options(text: str, rule_placeholder: str = "") -> List[str]:
    """按段落上下文生成候选 key 列表（LLM 只能从中选）。"""
    text = str(text or "")
    opts: List[str] = []
    if rule_placeholder:
        opts.append(rule_placeholder)
    if str(rule_placeholder).startswith("cell."):
        return opts
    metrics = []
    for kw, metric in METRIC_WORDS.items():
        if kw and kw in text and metric not in metrics:
            metrics.append(metric)
    stats = [s for kw, s in STAT_WORDS.items() if kw and kw in text]
    if not stats:
        stats = ["avg", "max", "min", "range", "abs_max", "rms"]
    for m in metrics:
        for s in stats:
            k = f"stats.{m}.{s}"
            if k not in opts:
                opts.append(k)
    return opts[:24]


def apply_verdict(number: Dict, llm_verdict: Optional[str],
                  llm_key: Optional[str], options: List[str],
                  confidence: Optional[float],
                  min_flip_conf: float = 0.75,
                  min_key_conf: float = 0.5) -> str:
    """把 LLM 裁决落到 number 上，返回动作名（供审计/测试）。

    稳定性约束：只有置信度达标的 keep 才推翻规则 replace；
    key 必须来自 options，否则只采纳 verdict、不采纳 key。
    """
    conf = confidence if isinstance(confidence, (int, float)) else 0.0
    rule_v = number.get("verdict")
    if llm_verdict == "keep" and rule_v in ("replace", "review") \
            and conf >= min_flip_conf:
        number["verdict"] = "keep"
        number.setdefault("reasons", []).append(
            f"LLM裁决keep(conf={conf:.2f})")
        return "flip_keep"
    if llm_verdict == "replace":
        if rule_v != "replace":
            number["verdict"] = "replace"
            number.setdefault("reasons", []).append(
                f"LLM裁决replace(conf={conf:.2f})")
        if llm_key and llm_key in options and conf >= min_key_conf:
            number["placeholder"] = llm_key
            number.setdefault("reasons", []).append(f"LLM选定key={llm_key}")
            return "set_key"
        return "replace_no_key"
    return "keep_rule"


def _batches(items: List[Dict], batch_chars: int) -> List[List[Dict]]:
    batches, cur, size = [], [], 0
    for c in items:
        add = len(c.get("text") or "") + 80
        if cur and size + add > batch_chars:
            batches.append(cur)
            cur, size = [], 0
        cur.append(c)
        size += add
    if cur:
        batches.append(cur)
    return batches


def _ask(clf: LLMClassifier, batch: List[Dict]) -> Optional[List[Dict]]:
    items = [{
        "id": c["id"],
        "value": c["value"],
        "text": (c.get("text") or "")[:2000],
        "rule_verdict": c["rule_verdict"],
        "rule_placeholder": c["rule_placeholder"],
        "rule_confidence": c["rule_confidence"],
        "options": c.get("options") or [],
    } for c in batch]
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


def arbitrate_numbers(analysis: Dict, llm_cfg: Optional[Dict],
                      max_candidates: int = 600,
                      batch_chars: int = 6000,
                      min_flip_conf: float = 0.75,
                      min_key_conf: float = 0.5,
                      seed: int = 1) -> Dict:
    """对 analysis['numbers'] 做 LLM 受限裁决；就地修改 verdict/placeholder。

    返回汇总（enabled/原因/各动作计数）。LLM 不可用或异常时回退规则。
    """
    stats = {"enabled": False, "reason": "", "候选数": 0,
             "flip_keep": 0, "set_key": 0, "replace_no_key": 0,
             "keep_rule": 0, "static_keep": 0, "batches": 0, "elapsed": 0.0}
    cfg = llm_cfg or {}
    if cfg.get("arbitrate", True) is False:
        stats["reason"] = "配置关闭 arbitrate"
        return stats

    # 纯规则增益：明显静态项直接判 keep（与 LLM 是否可用无关）
    texts = analysis.get("texts") or []
    static_audits = []
    for n in (analysis.get("numbers") or []):
        if n.get("verdict") not in ("replace", "review"):
            continue
        para = n.get("paragraph")
        text = ""
        if isinstance(para, int) and 0 <= para < len(texts):
            text = str(texts[para])
        if not text:
            text = str(n.get("context") or n.get("snippet") or "")
        if is_obviously_static(text, n.get("position")):
            n["verdict"] = "keep"
            n.setdefault("reasons", []).append("明显静态项，规则改为keep")
            stats["static_keep"] += 1
            static_audits.append({
                "id": f"s{len(static_audits) + 1}",
                "值": n.get("value", ""),
                "规则判定": "replace", "规则key": n.get("placeholder"),
                "LLM判定": None, "LLMkey": None, "LLM置信": None,
                "原因": "明显静态项（坐标/规范/图表ID）", "动作": "static_keep",
                "options": [],
            })

    clf = LLMClassifier(cfg)
    if not clf.available():
        stats["reason"] = "LLM 不可用（api_key/api_base/enabled），回退规则"
        log.warning("占位符 LLM 裁决不可用，回退规则: %s", stats["reason"])
        analysis["llm_verdicts"] = static_audits
        return stats

    nums = [n for n in (analysis.get("numbers") or [])
            if n.get("verdict") in ("replace", "review")]
    cands = []
    for n in nums:
        para = n.get("paragraph")
        text = ""
        if isinstance(para, int) and 0 <= para < len(texts):
            text = str(texts[para])
        if not text:
            text = str(n.get("context") or n.get("snippet") or "")
        opts = build_options(text, n.get("placeholder") or "")
        cands.append({
            "id": f"c{len(cands) + 1}",
            "number": n,
            "value": n.get("value", ""),
            "text": text,
            "rule_verdict": n.get("verdict"),
            "rule_placeholder": n.get("placeholder"),
            "rule_confidence": n.get("confidence"),
            "options": opts,
        })
    prio = [c for c in cands
            if c["rule_verdict"] == "review" or not c["rule_placeholder"]]
    rest = [c for c in cands if c not in prio]
    random.Random(seed).shuffle(rest)
    cands = (prio + rest)[:max_candidates]
    stats["候选数"] = len(cands)
    if not cands:
        stats["enabled"] = True
        return stats

    t0 = time.time()
    audits = list(static_audits)
    batches = _batches(cands, batch_chars)
    for bi, batch in enumerate(batches, 1):
        decided = {}
        try:
            resp = _ask(clf, batch)
            if isinstance(resp, list):
                for r in resp:
                    if isinstance(r, dict) and r.get("id"):
                        decided[str(r["id"])] = r
        except Exception as exc:  # noqa: BLE001
            log.warning("占位符 LLM 裁决批次 %d 失败，回退规则: %s",
                        bi, exc)
        for c in batch:
            d = decided.get(c["id"]) or {}
            action = apply_verdict(
                c["number"], d.get("verdict"), d.get("key"),
                c["options"], d.get("confidence"),
                min_flip_conf=min_flip_conf, min_key_conf=min_key_conf)
            stats[action] = stats.get(action, 0) + 1
            audits.append({
                "id": c["id"], "值": c["value"],
                "规则判定": c["rule_verdict"],
                "规则key": c["rule_placeholder"],
                "LLM判定": d.get("verdict"),
                "LLMkey": d.get("key"),
                "LLM置信": d.get("confidence"),
                "原因": d.get("reason", ""),
                "动作": action,
                "options": c["options"],
            })
    stats["batches"] = len(batches)
    stats["elapsed"] = round(time.time() - t0, 1)
    stats["enabled"] = True
    analysis["llm_verdicts"] = audits
    return stats
