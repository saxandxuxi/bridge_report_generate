# -*- coding: utf-8 -*-
"""冒烟测试：覆盖近期修复的高风险路径。

运行：python -m unittest tests.test_smoke
（已同步到 requirements.txt 的 pytest 也可直接 pytest tests/）
"""
import datetime as dt
import importlib.util
import os
import tempfile
import unittest

from docx import Document

from report_agent.report_builder import (
    _apply_period_text_fixes,
    _expand_row_tables,
    _unique_cells,
    verify_table_columns,
)
from report_agent.template_analyzer import analyze_template
from run_placeholder_audit import (
    _is_obviously_static,
    _key_valid,
    _options_for,
)
from report_agent.placeholder_arbiter import (
    arbitrate_numbers,
    apply_verdict,
    build_options,
    is_obviously_static,
)


class RowsExpansionTest(unittest.TestCase):
    def test_col_resolver_accepts_table_kwargs(self):
        """{{rows.*}} 展开时 _fill_paragraph 传入的
        table_title/row_index 关键字不得导致 TypeError。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "t.docx")
            doc = Document()
            t = doc.add_table(rows=1, cols=2)
            t.rows[0].cells[0].text = "{{rows.daily_records}}"
            t.rows[0].cells[1].text = "{{col.date}}/{{col.value}}"
            doc.save(p)

            doc = Document(p)
            datasets = {
                "daily_records": [
                    {"date": "2026-01-01", "value": "3.5"},
                    {"date": "2026-01-02", "value": "4.2"},
                ]
            }
            n = _expand_row_tables(
                doc, datasets, lambda key, **kw: "")
            self.assertEqual(n, 2)
            texts = [c.text for r in doc.tables[0].rows
                     for c in r.cells]
            joined = "".join(texts)
            self.assertIn("2026-01-01/3.5", joined)
            self.assertIn("2026-01-02/4.2", joined)


class PeriodFixTest(unittest.TestCase):
    def test_stale_year_month_range(self):
        doc = Document()
        doc.add_paragraph("综上，大桥在2025年01月-03月之间正常。")
        n = _apply_period_text_fixes(doc, {
            "start": dt.date(2026, 1, 1),
            "end": dt.date(2026, 3, 31),
            "mode": "quarterly",
        })
        self.assertEqual(n, 1)
        self.assertIn("2026年1月至3月之间", doc.paragraphs[0].text)


class MergedCellTest(unittest.TestCase):
    def test_unique_cells_dedupe(self):
        doc = Document()
        t = doc.add_table(rows=2, cols=2)
        t.cell(0, 0).merge(t.cell(0, 1))
        raw = len(t.rows[0].cells)
        unique = len(_unique_cells(t.rows[0]))
        self.assertLess(unique, raw)


class NumberScanTest(unittest.TestCase):
    def test_mixed_paragraph_numbers_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "t.docx")
            doc = Document()
            doc.add_paragraph("year {{date.period_label_cn}} total 3.5 m")
            doc.save(p)
            result = analyze_template(p)
            nums = [x["number"] for x in result["candidate_numbers"]]
            self.assertIn("3.5", nums)
            self.assertNotIn("{{date.period_label_cn}}", nums)


class VerifyTableTest(unittest.TestCase):
    def test_same_value_column_reports_sensor_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "out.docx")
            doc = Document()
            t = doc.add_table(rows=3, cols=2)
            t.rows[0].cells[0].text = "测点"
            t.rows[0].cells[1].text = "值"
            t.rows[1].cells[0].text = "A"
            t.rows[1].cells[1].text = "12.34"
            t.rows[2].cells[0].text = "B"
            t.rows[2].cells[1].text = "12.34"
            doc.save(p)
            lineage = [
                {"占位符": "cell.x.A.avg", "输出": "12.34",
                 "传感器": {"传感器编号": "111"}},
                {"占位符": "cell.x.B.avg", "输出": "12.34",
                 "传感器": {"传感器编号": "222"}},
            ]
            warns = verify_table_columns(p, lineage, label="t")
            same = [w for w in warns if w.get("问题") == "整列同值"]
            self.assertTrue(same)
            self.assertEqual(same[0]["血缘命中传感器数"], 2)


class AuditHelpersTest(unittest.TestCase):
    def test_key_valid_schema(self):
        mkeys = {"temperature", "wind_speed", "structure_temperature",
                 "displacement"}
        self.assertTrue(_key_valid("stats.temperature.max", mkeys))
        self.assertTrue(_key_valid("stats.wind_speed.avg", mkeys))
        # 关键词表里没有的统计量 / 非法结构一律拒绝
        self.assertFalse(_key_valid("stats.wind.10min_max", mkeys))
        self.assertFalse(_key_valid(
            "cell.temperature.time_series.section_1_2", mkeys))
        self.assertFalse(_key_valid("stats.bridge.width.avg", mkeys))

    def test_options_only_from_vocab(self):
        opts = _options_for({
            "text": "最大10min平均风速为11.93 m/s。",
            "rule_placeholder": None,
        })
        self.assertIn("stats.wind_speed.max", opts)
        self.assertTrue(all(o.startswith("stats.") for o in opts))

    def test_chart_id_and_citation_are_static(self):
        self.assertTrue(_is_obviously_static(
            {"text": "1_2主跨钢桁梁桥面板_应变_histogram"}))
        self.assertTrue(_is_obviously_static(
            {"text": "《公路桥梁…》JTG/T D65-05-2015"}))
        self.assertFalse(_is_obviously_static(
            {"text": "结构温度最大值为40.59℃"}))
        # 同段有规范引用，但真实数据离引用较远 -> 不能判静态
        mixed = "依据《公路桥梁结构监测技术规范》(JT/T 1037-2022)，最高湿度为100.55％。"
        self.assertFalse(is_obviously_static(
            mixed, mixed.index("100.55")))


class ArbiterTest(unittest.TestCase):
    def test_build_options_and_static(self):
        opts = build_options("最大10min平均风速为11.93 m/s。")
        self.assertIn("stats.wind_speed.max", opts)
        self.assertTrue(is_obviously_static("…_应变_histogram"))
        self.assertTrue(is_obviously_static("JTG/T D65-05-2015"))

    def test_apply_verdict_flip_and_key(self):
        n = {"verdict": "replace", "placeholder": None, "reasons": []}
        action = apply_verdict(n, "keep", None, ["stats.temperature.max"],
                               confidence=0.9)
        self.assertEqual(action, "flip_keep")
        self.assertEqual(n["verdict"], "keep")
        # 低置信不翻案
        n2 = {"verdict": "replace", "placeholder": None, "reasons": []}
        self.assertEqual(
            apply_verdict(n2, "keep", None, [], confidence=0.5),
            "keep_rule")
        self.assertEqual(n2["verdict"], "replace")
        # key 必须来自 options
        n3 = {"verdict": "review", "placeholder": None, "reasons": []}
        self.assertEqual(
            apply_verdict(n3, "replace", "stats.temperature.max",
                          ["stats.wind_speed.max"], 0.9),
            "replace_no_key")
        self.assertIsNone(n3["placeholder"])
        n4 = {"verdict": "review", "placeholder": None, "reasons": []}
        self.assertEqual(
            apply_verdict(n4, "replace", "stats.wind_speed.max",
                          ["stats.wind_speed.max"], 0.9),
            "set_key")
        self.assertEqual(n4["placeholder"], "stats.wind_speed.max")


class ChartLayoutTest(unittest.TestCase):
    """总标题与子图标题、图例与坐标轴不得重叠。"""

    def test_suptitle_does_not_overlap_axes_titles(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        spec = importlib.util.spec_from_file_location(
            "bcl_layout",
            os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))),
                "preprocess", "scripts", "build_chart_library.py"))
        bcl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bcl)
        fig, axes = plt.subplots(2, 2, figsize=(10, 8))
        for i, ax in enumerate(axes.reshape(-1)):
            ax.plot([0, 1], [0, 1])
            ax.set_title(f"子图{i}", fontsize=12)
        fig.suptitle("总标题", fontsize=16)
        bcl._finalize_layout(fig, rows=2)
        fig.canvas.draw()
        r = fig.canvas.get_renderer()
        su = fig._suptitle.get_window_extent(r)

        def _ov(a, b):
            return not (a.x1 <= b.x0 or b.x1 <= a.x0
                        or a.y1 <= b.y0 or b.y1 <= a.y0)
        for ax in axes.reshape(-1):
            self.assertFalse(_ov(su, ax.title.get_window_extent(r)))
        plt.close(fig)

    def test_static_keep_works_without_llm(self):
        analysis = {
            "texts": ["A(2,2)", "最大10min平均风速为11.93 m/s。"],
            "numbers": [
                {"value": "2", "paragraph": 0, "verdict": "replace",
                 "placeholder": None, "reasons": []},
                {"value": "11.93", "paragraph": 1, "verdict": "replace",
                 "placeholder": None, "reasons": []},
            ],
        }
        stats = arbitrate_numbers(analysis, {"enabled": False})
        self.assertFalse(stats["enabled"])
        self.assertGreaterEqual(stats["static_keep"], 1)
        self.assertEqual(analysis["numbers"][0]["verdict"], "keep")
        self.assertEqual(analysis["numbers"][1]["verdict"], "replace")


if __name__ == "__main__":
    unittest.main()
