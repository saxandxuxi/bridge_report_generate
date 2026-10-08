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
    build_value_resolver,
    format_report_number,
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
from report_agent.bridge_source import BridgeData
from report_agent.reviewer import self_check_report


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


class ConclusionsFallbackTest(unittest.TestCase):
    """{{conclusions}} 不得因桥数据缺失/异常而中断报告。"""

    def _period(self):
        return {"start": dt.date(2026, 1, 1),
                "end": dt.date(2026, 3, 31),
                "label": "2026.1~3",
                "label_cn": "2026年第1季度"}

    def test_no_bridge_returns_marker(self):
        sink = []
        r = build_value_resolver({}, self._period(), bridge=None,
                                 missing_sink=sink)
        self.assertEqual(r("conclusions"), "—")
        self.assertIn("conclusions", sink)

    def test_bridge_exception_returns_marker(self):
        class BadBridge:
            def build_conclusions(self, period, llm_cfg=None):
                raise RuntimeError("boom")

        sink = []
        r = build_value_resolver({}, self._period(), bridge=BadBridge(),
                                 missing_sink=sink)
        self.assertEqual(r("conclusions"), "—")

    def test_bridge_text_returned(self):
        class OkBridge:
            def build_conclusions(self, period, llm_cfg=None):
                return "（1）桥梁结构处于良好状态。"

        r = build_value_resolver({}, self._period(), bridge=OkBridge())
        self.assertIn("良好状态", r("conclusions"))


class ReportNumberFormatTest(unittest.TestCase):
    def test_number_rules(self):
        self.assertEqual(format_report_number(21.5665), "21.57")
        self.assertEqual(format_report_number(-3.371), "-3.37")
        self.assertEqual(format_report_number(-15.123), "-15.1")
        self.assertEqual(format_report_number(0.001234), "0.001234")
        self.assertEqual(format_report_number(0.000159), "0.000159")
        self.assertEqual(format_report_number(1.234e-7), "1.234e-07")
        self.assertEqual(format_report_number(0), "0")
        self.assertEqual(format_report_number(90), "90")
        self.assertEqual(format_report_number(float("nan")), "—")
        self.assertEqual(format_report_number(float("inf")), "—")


class GrossStatFaultTest(unittest.TestCase):
    def test_vibration_extreme_only_invalidates_extremes(self):
        st = {"最大值": 5602250.0, "最小值": -0.0975,
              "平均值": 5.0e-4, "差值": 5602250.1}
        f = "DZJSD(yJsd)"
        self.assertTrue(BridgeData._gross_stat_fault(st, f, "max"))
        self.assertTrue(BridgeData._gross_stat_fault(st, f, "range"))
        self.assertTrue(BridgeData._gross_stat_fault(st, f, "abs_max"))
        self.assertFalse(BridgeData._gross_stat_fault(st, f, "min"))
        self.assertFalse(BridgeData._gross_stat_fault(st, f, "avg"))

    def test_strain_extreme(self):
        st = {"最大值": 1.2e6, "最小值": -88.0, "平均值": 112.0,
              "差值": 1.2e6}
        self.assertTrue(BridgeData._gross_stat_fault(st, "YB(rsg)", "max"))
        self.assertFalse(BridgeData._gross_stat_fault(st, "YB(rsg)", "min"))

    def test_temp_summer_anomaly(self):
        st = {"最大值": 30.25, "最小值": -30.7, "平均值": 0.0,
              "差值": 60.95}
        self.assertTrue(BridgeData._gross_stat_fault(
            st, "WD(temp)", "min"))
        self.assertTrue(BridgeData._gross_stat_fault(
            st, "WD(temp)", "range"))

    def test_daily_recompute_drops_spike(self):
        b = BridgeData({})
        st = {"每日统计": [
            {"最大值": 5602250.0, "最小值": -0.1, "平均值": 0.0},
            {"最大值": 4.75, "最小值": -1.64, "平均值": 0.001},
            {"最大值": 6.38, "最小值": -2.0, "平均值": 0.002},
        ]}
        v, note = b._daily_clean_extreme(st, "DZJSD(yJsd)", "max")
        self.assertAlmostEqual(v, 6.38, places=6)
        self.assertIn("清洗后重算", note)
        v2, _ = b._daily_clean_extreme(st, "DZJSD(yJsd)", "range")
        self.assertAlmostEqual(v2, 8.38, places=6)

    def test_sibling_median_fallback(self):
        b = BridgeData({})
        b.sensors_for_metric = lambda m: ["a", "b", "c"]
        data = {
            "a": {"最大值": 2.0, "最小值": -1.0, "差值": 3.0},
            "b": {"最大值": 4.0, "最小值": -2.0, "差值": 6.0},
            "c": {"最大值": 5602250.0, "最小值": -0.1,
                  "差值": 5602250.1},
        }
        b._feature_stats = lambda sid, metric, feature="": data[sid]
        v, note = b._sibling_clean_extreme("vibration", "DZJSD(yJsd)",
                                           "max")
        self.assertAlmostEqual(v, 3.0, places=6)
        self.assertIn("中位数", note)


class SelfCheckPhysicsTest(unittest.TestCase):
    def test_physical_and_placeholder_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "r.docx")
            doc = Document()
            t = doc.add_table(rows=2, cols=1)
            t.rows[0].cells[0].text = "最大值(m/s²)"
            t.rows[1].cells[0].text = "5602250"
            doc.add_paragraph("strain_左幅3#墩根部顶板_scatter_59")
            doc.save(p)
            issues = self_check_report(p)
            types = {i["type"] for i in issues}
            self.assertIn("physical_range", types)
            self.assertIn("chart_placeholder", types)


class CategoryIsolationTest(unittest.TestCase):
    """总结极值只允许在该指标类别的传感器内聚合，不共享特征码回退。"""

    def _bridge(self):
        b = BridgeData({})
        b.metric_category = {"vibration": "振动",
                             "earthquake_load": "地震"}
        b._category_sensors = {"振动": ["101"], "地震": ["201"]}
        b.metrics = {"vibration": {"feature": "DZJSD(xJsd)"},
                     "earthquake_load": {"feature": "DZJSD(xJsd)"}}
        b.resolve_metric_stat_detail = lambda m, stat, period: (None, {})
        b._is_excluded = lambda sid: False
        return b

    def test_extreme_category_isolation(self):
        b = self._bridge()
        period = self._period()
        pos_entries = {
            "振动位置": {"测点1": {"统计": {
                "最大值": 1.68, "最小值": -9.57, "差值": 11.25},
                "传感器编号": "101"}},
            "地震位置": {"测点1": {"统计": {
                "最大值": 5602250.0, "最小值": -0.1,
                "差值": 5602250.1}, "传感器编号": "201"}},
        }
        gs = {"最大值": 5602250.0, "最小值": -0.1,
              "差值": 5602250.1}
        v, loc = b._metric_extreme("vibration", "max", "最大值",
                                   "最大值位置", period, gs, pos_entries)
        self.assertAlmostEqual(v, 1.68, places=6)
        self.assertEqual(loc, "振动位置")
        # 地震类别唯一的传感器极值异常且无同族有效值：
        # 严格类别模式下不得回退到共享特征码的 5602250 全桥统计
        v2, _ = b._metric_extreme("earthquake_load", "max", "最大值",
                                  "最大值位置", period, gs, pos_entries)
        self.assertIsNone(v2)

    def _period(self):
        return {"start": dt.date(2026, 7, 1),
                "end": dt.date(2026, 9, 30),
                "label": "2026.7~9"}


class ChartCleaningGranularityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "bcl_clean",
            os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))),
                "preprocess", "scripts", "build_chart_library.py"))
        cls.bcl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.bcl)

    def test_granularity_mapping(self):
        g = self.bcl.feature_granularity
        self.assertEqual(g("DZJSD(yJsd)"), "second")
        self.assertEqual(g("SZJSD(xJsd)"), "second")
        self.assertEqual(g("FSFX2(spfs)"), "10min")
        self.assertEqual(g("WD(temp)"), "hour")

    def test_budget_scales_with_granularity(self):
        spk, dist, rm = self.bcl.granularity_cleaning_budget(
            "DZJSD(yJsd)", 5, 5, 5)
        self.assertGreaterEqual(spk, 200)
        spk2, _, _ = self.bcl.granularity_cleaning_budget(
            "FSFX2(spfs)", 5, 5, 5)
        self.assertGreaterEqual(spk2, 20)

    def test_gross_spike_removed_even_when_ratio_low(self):
        # 90% 数据在量程外但未到 10 倍量程（如量程漂移），10% 是
        # 1e7 超量级毛刺：命中率门槛会失效，但毛刺必须无条件剔除
        values = [50000.0] * 90 + [1e7] * 10
        times = [dt.datetime(2026, 7, 1) + dt.timedelta(hours=i)
                 for i in range(len(values))]
        out, recs, ix, rx = self.bcl.clean_series_value(
            times, values, "t", spike_k=5.0, hour_level=True,
            vrange=(-10000.0, 10000.0), max_spikes=200,
            max_dist_outliers=100, max_total_removals=100)
        self.assertLess(max(out), 1e6)


class ScatterFallbackTest(unittest.TestCase):
    def test_folder_name_order_tolerance(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        name_dict = os.path.join(root, "preprocess", "传感器对照",
                                 "传感器名称对照", "洣水河特大桥.json")
        if not os.path.isfile(name_dict):
            self.skipTest("缺少洣水河名称对照表")
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "左幅炎陵侧边跨跨中顶板")
            os.makedirs(d)
            png = os.path.join(d, "相关性_WD(temp)-YB(rsg).png")
            with open(png, "wb") as f:
                f.write(b"x")
            import json as _json
            cfgp = os.path.join(root, "config", "config_mishuihe.json")
            if not os.path.isfile(cfgp):
                self.skipTest("缺少洣水河配置")
            cfg = _json.load(open(cfgp, encoding="utf-8"))["bridge_data"]
            cfg = dict(cfg)
            cfg["sensor_map"] = os.path.join(
                root, "preprocess", "传感器对照",
                "传感器编号名称.json")
            cfg["name_dict"] = name_dict
            cfg["stats_dir"] = ""
            cfg["charts_dir"] = tmp
            b = BridgeData(cfg)
            b.load()
            info = b.resolve_chart_info(
                "strain_左幅炎陵侧边跨跨中顶板_scatter_57",
                "strain_左幅炎陵侧边跨跨中顶板_scatter_57")
            self.assertIsNotNone(info)
            self.assertTrue(info["path"].endswith(".png"))

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
