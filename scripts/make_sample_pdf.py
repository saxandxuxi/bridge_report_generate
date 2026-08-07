#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成“某跨河桥梁月度监测报告”样例（PDF），用于验证识别器的 PDF 解析。
内容与 make_sample_bridge_report.py 的 DOCX 样例保持一致。
"""

import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_DIR = os.path.join(BASE_DIR, "outputs", "sample")
OUT_PDF = os.path.join(SAMPLE_DIR, "桥梁监测报告样例.pdf")


def build() -> None:
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

    title_style = ParagraphStyle(
        "title", fontName="STSong-Light", fontSize=18, leading=24,
        alignment=1, spaceAfter=4,
    )
    meta_style = ParagraphStyle(
        "meta", fontName="STSong-Light", fontSize=10, leading=14,
        alignment=1, spaceAfter=10,
    )
    heading_style = ParagraphStyle(
        "heading", fontName="STSong-Light", fontSize=14, leading=20,
        spaceBefore=10, spaceAfter=6, textColor=colors.HexColor("#2E74B5"),
    )
    body_style = ParagraphStyle(
        "body", fontName="STSong-Light", fontSize=11, leading=18, spaceAfter=6,
    )
    caption_style = ParagraphStyle(
        "caption", fontName="STSong-Light", fontSize=9, leading=13,
        spaceAfter=8, textColor=colors.HexColor("#595959"),
    )

    doc = SimpleDocTemplate(
        OUT_PDF, pagesize=A4,
        leftMargin=inch, rightMargin=inch, topMargin=inch, bottomMargin=inch,
    )
    story = []
    story.append(Paragraph("某跨河桥梁月度监测报告", title_style))
    story.append(Paragraph("报告日期：2026-08-03　合同编号：HT-2026-003", meta_style))

    story.append(Paragraph("一、工程概况", heading_style))
    story.append(Paragraph(
        "本桥为预应力混凝土连续梁桥，桥长123米，桥宽24.5米，"
        "跨径组合（30+60+30）米，设计荷载公路-I级，设计时速80km/h，"
        "抗震设防烈度7度，桥梁桩号K12+345，主梁采用C50混凝土。",
        body_style,
    ))

    story.append(Paragraph("二、监测指标统计", heading_style))
    story.append(Paragraph(
        "本周期共31天监测数据：最高温度26.0℃，最低温度22.5℃，"
        "平均温度24.3℃，温度标准差1.18℃；桥梁挠度最大2.3mm，平均1.1mm；"
        "温度超过30℃的天数为0天。",
        body_style,
    ))

    story.append(Paragraph("表1 关键监测指标", caption_style))
    data = [
        ["指标", "数值"],
        ["最高温度", "26.0℃"],
        ["最低温度", "22.5℃"],
        ["平均温度", "24.3℃"],
        ["最大挠度", "2.3mm"],
        ["平均挠度", "1.1mm"],
        ["数据天数", "31天"],
    ]
    tbl = Table(data, colWidths=[2.4 * inch, 2.4 * inch])
    tbl.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F4F7")),
            ]
        )
    )
    story.append(tbl)
    story.append(Spacer(1, 12))

    story.append(Paragraph("三、图表分析", heading_style))
    story.append(Image(os.path.join(SAMPLE_DIR, "chart_trend.png"),
                       width=5.6 * inch, height=2.94 * inch))
    story.append(Paragraph("图1 温度变化趋势图", caption_style))
    story.append(Image(os.path.join(SAMPLE_DIR, "chart_histogram.png"),
                       width=5.6 * inch, height=2.94 * inch))
    story.append(Paragraph("图2 温度分布直方图", caption_style))
    story.append(Image(os.path.join(SAMPLE_DIR, "schematic.png"),
                       width=5.6 * inch, height=2.61 * inch))
    story.append(Paragraph("图3 桥梁CAD示意图", caption_style))

    story.append(Paragraph("四、结论", heading_style))
    story.append(Paragraph(
        "本周期平均温度24.3℃，最高温度26.0℃，最低温度22.5℃，"
        "桥梁结构整体处于安全状态，最大挠度2.3mm未超限值。"
        "下阶段持续关注温度变化趋势与挠度发展。",
        body_style,
    ))

    doc.build(story)
    print(f"样例 PDF 已生成: {OUT_PDF}")


if __name__ == "__main__":
    build()
