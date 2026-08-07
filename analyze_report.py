#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""分析一份成品报告（DOCX / PDF），识别可替换的图片与数字。

用法：
  python analyze_report.py --input 报告.docx
  python analyze_report.py --input 报告.pdf --out outputs/analysis.json
  python analyze_report.py --input 报告.docx --annotate outputs/模板草稿.docx
  python analyze_report.py --input 报告.docx --config config.json

--annotate 会把识别为"动态"的数字改成 {{stats.*}} / {{data.N}} 占位符，
把动态图表段落改成 {{chart.<ID>}}，生成一份可直接复核的模板草稿；
固定项（如 CAD 图、"桥长123米"）保持不变。

--config 指定配置文件，若其中 llm.enabled=true 则启用 LLM 辅助识别。
识别流程：关键词初筛 → LLM 对模糊项二次判断（不可用时自动降级）。
"""

import argparse
import os

from report_agent.config import load_config
from report_agent.recognizer import annotate_docx, print_summary, recognize, save_analysis


def main() -> int:
    parser = argparse.ArgumentParser(description="成品报告解析识别")
    parser.add_argument("--input", required=True, help="输入报告 .docx / .pdf")
    parser.add_argument("--out", default=None, help="识别结果 JSON 输出路径")
    parser.add_argument("--annotate", default=None,
                        help="(DOCX) 生成标注草稿 .docx 的路径")
    parser.add_argument("--config", default=None,
                        help="配置文件路径（若 llm.enabled=true 则启用 LLM 辅助识别）")
    parser.add_argument("--llm", action="store_true",
                        help="强制启用 LLM 辅助识别（忽略 config 中的 llm.enabled 设置）")
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f"[错误] 文件不存在: {args.input}")
        return 1

    # 加载 LLM 配置
    llm_cfg = None
    if args.config or args.llm:
        try:
            cfg = load_config(args.config)
            llm_cfg = cfg.get("llm", {})
            if args.llm:
                llm_cfg["enabled"] = True
        except FileNotFoundError:
            if args.llm:
                print("[警告] 配置文件未找到，LLM 将使用默认配置")
                llm_cfg = {"enabled": True}

    analysis = recognize(args.input, llm_cfg=llm_cfg)
    out = args.out or os.path.join(
        "outputs",
        "analysis_" + os.path.splitext(os.path.basename(args.input))[0] + ".json",
    )

    if args.annotate:
        if not args.input.lower().endswith(".docx"):
            print("[提示] --annotate 仅支持 DOCX 输入，跳过标注。")
        else:
            # 传入已计算的 analysis，避免重复调用 LLM
            # annotate_docx 会将 data_values 注入 analysis
            result = annotate_docx(args.input, args.annotate, llm_cfg=llm_cfg,
                                   analysis=analysis)
            print(f"标注草稿已生成: {result['output']}")
            print(f"  替换数字 {result['replaced_numbers']} 个"
                  f"（跨格式跳过 {result['skipped_numbers_split_runs']} 个），"
                  f"替换图片 {result['replaced_images']} 张，"
                  f"图表文本 {result.get('replaced_chart_texts', 0)} 处，"
                  f"文本 {result.get('replaced_texts', 0)} 处，"
                  f"data 占位符 {len(analysis.get('data_values', {}))} 个")

    # 在 annotate 之后保存，确保 data_values 被写入 JSON
    save_analysis(analysis, out)
    print_summary(analysis)
    print(f"识别结果已保存: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
