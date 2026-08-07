# -*- coding: utf-8 -*-
"""报告生成：把统计值、日期、图表填充进 Word 模板。

流程：
  1. 展开表格中的可重复行（{{rows.<数据集>}} + {{col.<字段>}}）；
  2. 处理条件渲染块（{{?condition}}...{{?}}）；
  3. 替换所有正文/表格/页眉页脚里的 {{stats.*}}、{{date.*}} 占位符；
  4. 处理带默认值的占位符（{{key|default:值}}）和表达式（{{expr:...}}）；
  5. 把 {{chart.<ID>}} 独占段落替换为图表图片；
  6. 最后检查是否还有未替换的占位符。

支持的占位符语法：
  {{stats.<指标>.<统计>}}              基本统计值
  {{stats.<指标>.<统计>:0.1f}}         带格式说明符
  {{date.<字段>}}                       报告期日期
  {{chart.<ID>}}                         图表位置
  {{rows.<数据集>}} + {{col.<字段>}}    表格可重复行
  {{key|default:默认值}}                带默认值的占位符（找不到时用默认值）
  {{expr:a - b}}                         简单表达式计算（+ - * /）
  {{?condition}}...{{?}}                 条件渲染块
"""

import copy
import datetime as dt
import hashlib
import io
import logging
import os
import re
import shutil
import tempfile
import zipfile
from typing import Callable, Dict, List, Optional

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt
from docx.table import Table
from docx.text.paragraph import Paragraph

log = logging.getLogger("report-agent.report_builder")

# 占位符正则：key + 可选格式说明符 + 可选默认值
# key 用 [^:|}]+ 匹配除分隔符外的任意字符（含中文、#、（）等），避免特殊字符占位符"隐形"
MARKER_RE = re.compile(r"\{\{([^:|}]+)(?::([^}|]+))?(?:\|default:([^}]*))?\}\}")
# 条件块正则：{{?condition}}content{{?}}
COND_RE = re.compile(r"\{\{\?(.+?)\}\}(.*?)\{\{\?\}\}", re.DOTALL)
# 表达式前缀
EXPR_PREFIX = "expr:"


def _eval_simple_expr(expr: str, stats: Dict) -> Optional[float]:
    """安全地计算简单算术表达式（仅支持 + - * / 和数字、stats 值引用）。

    如 "stats.temperature.max - stats.temperature.min" → 9.5
    如果无法计算返回 None。
    """
    # 提取所有标识符引用
    refs = re.findall(r"stats\.[a-zA-Z0-9_.]+", expr)
    replacements = {}
    for ref in refs:
        parts = ref.split(".")
        node = stats
        try:
            for p in parts[1:]:
                node = node[p]
            if isinstance(node, (int, float)):
                replacements[ref] = str(node)
            else:
                return None
        except (KeyError, TypeError):
            return None
    # 替换引用为数值
    for ref, val in replacements.items():
        expr = expr.replace(ref, val)
    # 验证：只允许数字、运算符和空格
    if not re.fullmatch(r"[\d\s+\-*/.()]+", expr):
        return None
    try:
        result = eval(expr, {"__builtins__": {}}, {})
        return float(result)
    except Exception:  # noqa: BLE001
        return None


def build_value_resolver(stats: Dict, period: Dict,
                           data_registry=None, data_values: Dict = None,
                           bridge=None, missing_sink: Optional[list] = None) -> Callable[[str], str]:
    """根据统计结果和报告期构建占位符解析函数。

    支持以下占位符类型：
      stats.<指标>.<统计>     — 统计值
      date.<字段>             — 报告期日期
      expr:<表达式>           — 简单算术表达式
      cell.<metric>.<column>.<stat>  — 表格单元格（多数据源 + 测点级查询）
      data.N                 — 通用数据占位符（回填原始值）

    bridge: 可选的真实监测数据适配器（report_agent.bridge_source.BridgeData），
            优先于 data_registry / stats 解析 cell 与 stats 占位符。
    missing_sink: 可选的列表，解析不到的 cell/stats 键会追加进去（供 Web 待补清单）。
    """

    def resolve(key: str, table_title: str = "", row_index: int = 0) -> str:
        # 表达式计算
        if key.startswith(EXPR_PREFIX):
            expr = key[len(EXPR_PREFIX):]
            result = _eval_simple_expr(expr, stats)
            if result is not None:
                return f"{result:.1f}"
            raise KeyError(f"表达式无法计算: {key}")
        # 表格单元格：cell.metric.column.stat
        if key.startswith("cell."):
            from .data_loader import resolve_cell
            parts = key.split(".")
            if len(parts) != 4:
                raise KeyError(f"cell 占位符格式错误（期望 cell.metric.column.stat）: {key}")
            _, metric, column, stat = parts
            value = None
            if bridge is not None:
                try:
                    value = bridge.resolve_cell(metric, column, stat, period,
                                                table_title=table_title or "",
                                                row_index=row_index)
                except Exception as exc:  # noqa: BLE001
                    log.warning("桥数据解析 cell 失败 %s: %s", key, exc)
            if value is None and bridge is None and data_registry is not None:
                value = resolve_cell(data_registry, metric, column, stat, period)
            if value is None:
                if missing_sink is not None:
                    missing_sink.append(key)
                # 桥模式下解析不到 -> 填占位符（真实数据源已尝试过）
                if bridge is not None:
                    return "—"
                # 指标无数据源（如 strain/displacement）——返回占位符而非崩溃
                if data_registry is not None and data_registry.get(metric) is None:
                    import logging
                    logging.getLogger("report-agent.builder").warning(
                        "指标 %s 无数据源，cell.%s 填入占位符", metric, key
                    )
                    return "—"
                # 未知列（如表格行超出已映射测点范围）——返回占位符而非崩溃
                if column.startswith("unknown_"):
                    import logging
                    logging.getLogger("report-agent.builder").warning(
                        "未知测点列，cell.%s 填入占位符", key
                    )
                    return "—"
                raise KeyError(f"无法计算单元格: {key}（数据源 {metric} 或列 {column} 不存在）")
            return _format_cell_value(stat, value)
        if key.startswith("stats."):
            parts = key.split(".")
            if len(parts) == 3 and bridge is not None:
                metric, _, stat = parts
                value = None
                try:
                    value = bridge.resolve_metric_stat(metric, stat, period)
                except Exception as exc:  # noqa: BLE001
                    log.warning("桥数据解析 stats 失败 %s: %s", key, exc)
                if value is not None:
                    return _format_stat_value(key, value)
                if missing_sink is not None:
                    missing_sink.append(key)
            node = stats
            found = True
            for p in parts[1:]:
                if not isinstance(node, dict) or p not in node:
                    found = False
                    break
                node = node[p]
            if found:
                return _format_stat_value(key, node)
            # 兼容多数据源：尝试从 data_registry 加载
            if data_registry is not None and len(parts) == 3:
                from .data_loader import resolve_metric_stat
                metric, col, stat = parts[1], None, parts[2]
                value = resolve_metric_stat(data_registry, metric, stat, period)
                if value is not None:
                    return _format_stat_value(key, value)
            # 指标无数据源——返回占位符而非崩溃
            _metric = parts[1] if len(parts) > 1 else "?"
            if data_registry is not None and _metric not in data_registry.all_metrics():
                import logging
                logging.getLogger("report-agent.builder").warning(
                    "指标 %s 无数据源，填入占位符: %s", _metric, key
                )
                return "—"
            raise KeyError(f"统计量不存在: {key}")
        if key.startswith("date."):
            field = key.split(".", 1)[1]
            # 落款日期：2026年8月6日
            if field == "signature":
                sig = _signature_date(period)
                return f"{sig.year}年{sig.month}月{sig.day}日"
            # 报告期起止月份（结论段“xx月-xx月”用）
            if field == "period_start_month":
                start = period.get("start")
                return f"{start.month}月" if isinstance(start, dt.date) else ""
            if field == "period_end_month":
                end = period.get("end")
                return f"{end.month}月" if isinstance(end, dt.date) else ""
            # 周期标签：period_label（2026.1~3）/ period_label_cn（2026年第一季度）
            if field in ("period_label", "label"):
                value = period.get("label") or period.get("label_cn")
                return str(value) if value else ""
            if field in ("period_label_cn", "label_cn"):
                value = period.get("label_cn") or period.get("label")
                return str(value) if value else ""
            alias = {"period_start": "start", "period_end": "end"}.get(field, field)
            value = period.get(alias)
            if value is None:
                raise KeyError(f"日期字段不存在: {key}")
            if isinstance(value, dt.date):
                return value.strftime("%Y-%m-%d")
            if isinstance(value, dt.datetime):
                return value.strftime("%Y-%m-%d %H:%M")
            return str(value)
        # 通用数据占位符：回填 annotate_docx 阶段保存的原始值
        if key.startswith("data."):
            if data_values and key in data_values:
                return str(data_values[key])
            raise KeyError(f"无原始值映射的 data 占位符: {key}")
        raise KeyError(f"不支持的占位符: {key}")

    return resolve


def _format_cell_value(stat: str, value: float) -> str:
    """格式化表格单元格值。"""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # 整数化的值（如 0.0）显示为整数
        if value == int(value) and abs(value) < 1e9:
            return str(int(value))
        # 标准差用 2 位小数
        if stat in ("std", "均方根", "rms"):
            return f"{value:.2f}"
        return f"{value:.2f}"
    return str(value)


def _format_stat_value(key: str, value) -> str:
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if key.endswith(".std"):
            return f"{value:.2f}"
        return f"{value:.1f}"
    return str(value)


def _format_col_value(field: str, value) -> str:
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float):
        if field == "deviation":
            return f"{value:+.1f}"
        return f"{value:.1f}"
    if isinstance(value, int):
        return str(value)
    return str(value)


def _fill_paragraph(paragraph: Paragraph, resolver: Callable[[str], str],
                    table_title: str = "", row_index: int = 0) -> int:
    """替换段落内所有占位符，返回替换数量。合并多个 run，保留首个 run 的格式。

    支持语法：
      {{key}}               基本替换
      {{key:0.1f}}          带格式说明符
      {{key|default:值}}    带默认值（key 不存在时用默认值）
      {{key:0.1f|default:0}} 带格式和默认值
    """
    runs = paragraph.runs
    if not runs:
        return 0
    full = "".join(r.text for r in runs)
    matches = list(MARKER_RE.finditer(full))
    if not matches:
        return 0

    parts = []
    last = 0
    replaced = 0
    for m in matches:
        key, spec, default = m.group(1), m.group(2), m.group(3)
        parts.append(full[last:m.start()])
        try:
            value = resolver(key, table_title=table_title, row_index=row_index)
            if spec:
                try:
                    if isinstance(value, (int, float)):
                        value = format(value, spec.strip())
                    elif isinstance(value, str) and value.replace(".", "").replace("-", "").isdigit():
                        value = format(float(value), spec.strip())
                except (ValueError, TypeError):
                    pass  # 格式化失败则保持原值
            parts.append(str(value))
            replaced += 1
        except KeyError as exc:
            if default is not None:
                # 有默认值，使用默认值替代
                parts.append(default)
                replaced += 1
            else:
                raise KeyError(f"{exc}（位置：\"{full.strip()[:40]}\"）") from exc
        last = m.end()
    parts.append(full[last:])

    runs[0].text = "".join(parts)
    for r in runs[1:]:
        r.text = ""
    return replaced


def _paragraph_text(paragraph: Paragraph) -> str:
    return "".join(r.text for r in paragraph.runs)


def _ensure_rgb(path: str) -> str:
    """把 RGBA/调色板 PNG 转成 RGB（WPS/Word 对 RGBA 渲染兼容性差，可能显示空白）。"""
    try:
        from PIL import Image
        img = Image.open(path)
        if img.mode not in ("RGBA", "LA", "P", "PA"):
            return path
        cache = os.path.join(tempfile.gettempdir(), "report_rgb")
        os.makedirs(cache, exist_ok=True)
        key = hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:16]
        out = os.path.join(cache, key + ".png")
        if os.path.isfile(out):
            return out
        bg = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode in ("RGBA", "LA", "PA"):
            rgba = img.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
        else:
            bg.paste(img.convert("RGB"))
        bg.save(out, "PNG")
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("PNG 转 RGB 失败 %s: %s", path, exc)
        return path


def _insert_chart(paragraph: Paragraph, png_path: str, width_inches: float) -> None:
    """把段落清空并插入居中图片。"""
    paragraph.clear()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run()
    run.add_picture(_ensure_rgb(png_path), width=Inches(width_inches))


def iter_block_items(parent):
    if hasattr(parent, "element") and hasattr(parent.element, "body"):
        body = parent.element.body
    else:
        body = parent._tc
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, parent)
        elif child.tag == qn("w:tbl"):
            yield Table(child, parent)


def _expand_row_tables(
    doc: Document,
    row_datasets: Dict[str, List[Dict]],
    base_resolver: Callable[[str], str],
) -> int:
    """展开 {{rows.<数据集>}} 模板行，返回展开的行数。"""
    expanded = 0
    for table in doc.tables:
        for row in list(table.rows):
            dataset = None
            for cell in row.cells:
                m = re.search(r"\{\{rows\.([a-zA-Z0-9_.]+)\}\}", cell.text)
                if m:
                    dataset = m.group(1)
                    break
            if not dataset:
                continue

            records = row_datasets.get(dataset)
            if not records:
                raise ValueError(f"行数据集 '{dataset}' 没有可用数据")

            tr = row._tr
            anchor = tr
            for rec in records:
                new_tr = copy.deepcopy(tr)
                for tc in new_tr.findall(qn("w:tc")):
                    for p_elm in tc.findall(qn("w:p")):
                        para = Paragraph(p_elm, None)

                        def col_resolver(key: str, _rec=rec) -> str:
                            if key.startswith("rows."):
                                return ""  # 行模板指令，填充时移除
                            if key.startswith("col."):
                                field = key.split(".", 1)[1]
                                if field not in _rec:
                                    raise KeyError(f"记录缺少字段: {field}")
                                return _format_col_value(field, _rec[field])
                            return base_resolver(key)

                        _fill_paragraph(para, col_resolver)
                anchor.addnext(new_tr)
                anchor = new_tr
                expanded += 1
            tr.getparent().remove(tr)
    return expanded


def _walk_paragraphs(doc: Document):
    for item in iter_block_items(doc):
        if isinstance(item, Paragraph):
            yield item
        elif isinstance(item, Table):
            for row in item.rows:
                for cell in row.cells:
                    for p in cell.paragraphs:
                        yield p
    for section in doc.sections:
        for p in section.header.paragraphs:
            yield p
        for p in section.footer.paragraphs:
            yield p


def _eval_condition(condition: str, resolver: Callable[[str], str]) -> bool:
    """评估条件表达式，返回 True/False。

    支持的格式：
      stats.temperature.days_above_30>0     — 比较
      stats.temperature.max>=35            — 大于等于
      stats.temperature.min<10             — 小于
      stats.humidity.avg                   — 仅检查存在性和真值
      not stats.temperature.days_above_30   — 取反
    """
    condition = condition.strip()

    # 取反
    negate = False
    if condition.startswith("not "):
        negate = True
        condition = condition[4:].strip()

    # 尝试匹配比较运算符
    for op in (">=", "<=", "!=", "==", ">", "<"):
        if op in condition:
            left, right = condition.split(op, 1)
            left = left.strip()
            right = right.strip()
            try:
                left_val = float(resolver(left)) if left.startswith(("stats.", "date.", "expr:")) else float(left)
                right_val = float(right) if not right.startswith(("stats.", "date.")) else float(resolver(right))
            except (KeyError, ValueError):
                return negate  # 无法解析的值默认 False（取反后 True）

            if op == ">":
                result = left_val > right_val
            elif op == "<":
                result = left_val < right_val
            elif op == ">=":
                result = left_val >= right_val
            elif op == "<=":
                result = left_val <= right_val
            elif op == "!=":
                result = left_val != right_val
            elif op == "==":
                result = left_val == right_val
            return (not result) if negate else result

    # 无运算符：检查存在性和真值
    try:
        val = resolver(condition)
        result = bool(val) and val != "0" and val != "0.0"
        return (not result) if negate else result
    except KeyError:
        return negate  # 不存在 → False（取反后 True）


def _process_conditional_blocks(paragraph: Paragraph, resolver: Callable[[str], str]) -> None:
    """处理段落中的条件渲染块 {{?condition}}content{{?}}。

    条件为真时保留 content（移除条件标记），条件为假时移除整个块。
    """
    runs = paragraph.runs
    if not runs:
        return
    full = "".join(r.text for r in runs)
    if "{{?" not in full:
        return

    def replace_block(m: re.Match) -> str:
        condition = m.group(1).strip()
        content = m.group(2)
        if _eval_condition(condition, resolver):
            return content
        return ""

    new_text = COND_RE.sub(replace_block, full)
    if new_text != full:
        runs[0].text = new_text
        for r in runs[1:]:
            r.text = ""


# ---------------------------------------------------------------------------
# 报告期文字修正：页眉年份/季度、落款日期、目录页码刷新
# ---------------------------------------------------------------------------

def _signature_date(period: Dict) -> dt.datetime:
    """落款日期：优先用报告期生成时间，其次报告期结束日。"""
    val = period.get("generated_at")
    if isinstance(val, dt.datetime):
        return val
    if isinstance(val, dt.date):
        return dt.datetime(val.year, val.month, val.day)
    end = period.get("end")
    if isinstance(end, dt.date):
        return dt.datetime(end.year, end.month, end.day)
    return dt.datetime.now()


def _quarter_no(period: Dict) -> int:
    end = period.get("end") or period.get("generated_at")
    if isinstance(end, dt.datetime):
        end = end.date()
    if isinstance(end, dt.date):
        return (end.month - 1) // 3 + 1
    return 0


def _period_text_replacements(period: Dict):
    """返回 (正则, 替换串) 列表，按顺序应用。

    覆盖模板中残留的字面量日期/季度写法：
      xxxx年xx月xx日 / XXXX年XX月XX日 / ××××年××月××日
      xxxx年 / XXXX年
      第X季度 / 第x季度
      xx月xx日
    """
    sig = _signature_date(period)
    y, m, d = sig.year, sig.month, sig.day
    q = _quarter_no(period)
    x4 = r"[xX×]{4}"
    x2 = r"[xX×]{2}"
    start = period.get("start")
    end = period.get("end")
    sm = getattr(start, "month", m)
    em = getattr(end, "month", m)
    return [
        (re.compile(rf"{x4}年{x2}月{x2}日"), f"{y}年{m}月{d}日"),
        # 报告期范围：xx月-xx月（如结论段“2026年xx月-xx月”）
        (re.compile(rf"{x2}月[-—–~～]{x2}月"), f"{sm}月-{em}月"),
        (re.compile(rf"{x4}年"), f"{y}年"),
        (re.compile(rf"第[xX×]季度"), f"第{q}季度" if q else "第X季度"),
        (re.compile(rf"{x2}月{x2}日"), f"{m}月{d}日"),
        # 签字表等处的裸月/日占位（顺序在范围之后，避免误伤）
        (re.compile(rf"{x2}月"), f"{m}月"),
        (re.compile(rf"{x2}日"), f"{d}日"),
    ]


def _apply_period_text_fixes(doc: Document, period: Dict) -> int:
    """把页眉/页脚/正文里的字面量日期与季度替换为实际报告期。"""
    replacements = _period_text_replacements(period)
    changed = 0
    for paragraph in _walk_paragraphs(doc):
        text = _paragraph_text(paragraph)
        new_text = text
        for pat, rep in replacements:
            new_text = pat.sub(rep, new_text)
        if new_text != text:
            runs = paragraph.runs
            if not runs:
                continue
            runs[0].text = new_text
            for r in runs[1:]:
                r.text = ""
            changed += 1
    return changed


def _enable_update_fields(doc: Document) -> None:
    """在 settings.xml 中写入 <w:updateFields/>，让 Word 打开时自动刷新目录页码。"""
    settings = doc.settings.element
    if settings.find(qn("w:updateFields")) is not None:
        return
    el = OxmlElement("w:updateFields")
    el.set(qn("w:val"), "true")
    # 按 CT_Settings 顺序插入：characterSpacingControl 之后、hdrShapeDefaults 之前
    anchor = settings.find(qn("w:hdrShapeDefaults"))
    if anchor is None:
        anchor = settings.find(qn("w:footnotePr"))
    if anchor is None:
        anchor = settings.find(qn("w:compat"))
    if anchor is not None:
        anchor.addprevious(el)
    else:
        settings.append(el)


def _flatten_rgba_in_docx(path: str) -> int:
    """把 docx 包内所有 RGBA/调色板 PNG 原位转成 RGB（WPS/Word 兼容性兜底）。

    返回转换的图片数量。转换失败不影响文档本身。
    """
    tmp = path + ".rgb.tmp"
    converted = 0
    try:
        with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename.startswith("word/media/") and data.startswith(b"\x89PNG"):
                    try:
                        from PIL import Image
                        img = Image.open(io.BytesIO(data))
                        if img.mode in ("RGBA", "LA", "PA", "P"):
                            bg = Image.new("RGB", img.size, (255, 255, 255))
                            rgba = img.convert("RGBA")
                            bg.paste(rgba, mask=rgba.split()[-1])
                            buf = io.BytesIO()
                            bg.save(buf, "PNG")
                            data = buf.getvalue()
                            converted += 1
                    except Exception:  # noqa: BLE001
                        pass
                zout.writestr(item, data)
        shutil.move(tmp, path)
    except Exception as exc:  # noqa: BLE001
        log.warning("输出图 RGB 平铺失败: %s", exc)
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return converted


def _copy_ppr(target_el, source_el) -> None:
    """把源段落的段落属性（pPr，含样式/间距/行高）复制到目标段落。"""
    try:
        src = source_el.find(qn("w:pPr"))
        if src is None:
            return
        import copy as _copy
        tgt = target_el.find(qn("w:pPr"))
        if tgt is not None:
            target_el.remove(tgt)
        target_el.insert(0, _copy.deepcopy(src))
    except Exception:  # noqa: BLE001
        pass


def _add_caption_after(paragraph: Paragraph, text: str) -> Paragraph:
    """在图表段落之后插入一行居中图注（图X.X.X 图名）。"""
    new_p = OxmlElement("w:p")
    paragraph._p.addnext(new_p)
    _copy_ppr(new_p, paragraph._p)  # 继承图表段落的样式/间距，避免行高塌陷
    p = Paragraph(new_p, paragraph._parent)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(text)
    run.font.size = Pt(10.5)
    return p


def _new_paragraph_after(anchor_el, parent, source_el=None) -> Paragraph:
    """在指定元素后新建一个空段落，返回 Paragraph 包装。"""
    new_p = OxmlElement("w:p")
    anchor_el.addnext(new_p)
    if source_el is not None:
        _copy_ppr(new_p, source_el)
    return Paragraph(new_p, parent)


def build_report(
    template_path: str,
    output_path: str,
    resolver: Callable[[str], str],
    chart_images: Dict[str, str],
    row_datasets: Optional[Dict[str, List[Dict]]] = None,
    chart_width_inches: float = 5.8,
    strict: bool = True,
    period: Optional[Dict] = None,
    chart_captions: Optional[Dict[str, str]] = None,
    extra_charts: Optional[Dict[str, List[Dict]]] = None,
) -> List[str]:
    """基于模板生成报告，返回未替换的占位符列表（strict=True 时抛出异常）。"""
    doc = Document(template_path)
    row_datasets = row_datasets or {}
    chart_captions = chart_captions or {}
    extra_charts = extra_charts or {}

    if row_datasets:
        _expand_row_tables(doc, row_datasets, resolver)

    # 正文：按块顺序处理；记录最近的短文本作为表格标题上下文，
    # 供 cell 占位符按“表格标题 -> 断面位置 -> 测点N”解析。
    current_title = ""
    current_prefix = ""          # 章节号（如 3.1.1）
    fig_counters: Dict[str, int] = {}
    global_fig_no = 0
    for item in iter_block_items(doc):
        if isinstance(item, Paragraph):
            _process_conditional_blocks(item, resolver)
            text = _paragraph_text(item).strip()
            # 检测章节标题（如 “3.1.1.1  第6、7跨跨中断面环境温度”），用于图号前缀
            hm = re.match(r"^(\d+(?:\.\d+){1,3})(?![\d.])", text)
            if hm and len(text) <= 60:
                parts = hm.group(1).split(".")
                current_prefix = ".".join(parts[:3])
            chart_match = MARKER_RE.fullmatch(text)
            if chart_match and chart_match.group(1).startswith("chart."):
                chart_id = chart_match.group(1).split(".", 1)[1]
                if chart_id not in chart_images:
                    raise KeyError(f"缺少图表 {chart_id} 的图片文件")
                _insert_chart(item, chart_images[chart_id], chart_width_inches)
                # 图号 + 图名（图3.1.1-1 第6跨跨中断面主梁箱内环境温度时程曲线图）
                caption = chart_captions.get(chart_id, "")
                if caption:
                    if current_prefix:
                        fig_counters[current_prefix] = fig_counters.get(current_prefix, 0) + 1
                        fig_no = f"图{current_prefix}-{fig_counters[current_prefix]}"
                    else:
                        global_fig_no += 1
                        fig_no = f"图{global_fig_no}"
                    anchor_cap_p = _add_caption_after(item, f"{fig_no} {caption}")
                # 自动补齐的缺图（同一节监测部位多、模板占位符少）：
                # 必须插在锚点图注之后，避免图注顺序错乱
                anchor_el = anchor_cap_p._p if caption else item._p
                for ex in extra_charts.get(chart_id, []):
                    ex_p = _new_paragraph_after(anchor_el, item._parent, source_el=item._p)
                    ex_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    ex_p.add_run().add_picture(_ensure_rgb(ex["path"]), width=Inches(chart_width_inches))
                    ex_anchor = ex_p._p
                    ex_caption = ex.get("caption", "")
                    if ex_caption:
                        if current_prefix:
                            fig_counters[current_prefix] = fig_counters.get(current_prefix, 0) + 1
                            ex_fig = f"图{current_prefix}-{fig_counters[current_prefix]}"
                        else:
                            global_fig_no += 1
                            ex_fig = f"图{global_fig_no}"
                        ex_cap_p = _add_caption_after(ex_p, f"{ex_fig} {ex_caption}")
                        ex_anchor = ex_cap_p._p
                    anchor_el = ex_anchor
            else:
                _fill_paragraph(item, resolver)
            new_text = _paragraph_text(item).strip()
            if new_text and len(new_text) <= 80:
                current_title = new_text
        elif isinstance(item, Table):
            for ri, row in enumerate(item.rows):
                for cell in row.cells:
                    for p in cell.paragraphs:
                        _process_conditional_blocks(p, resolver)
                        _fill_paragraph(p, resolver, table_title=current_title, row_index=ri)

    # 页眉页脚
    for section in doc.sections:
        for p in section.header.paragraphs:
            _process_conditional_blocks(p, resolver)
            _fill_paragraph(p, resolver)
        for p in section.footer.paragraphs:
            _process_conditional_blocks(p, resolver)
            _fill_paragraph(p, resolver)

    # 页眉/落款等字面量日期季度修正 + 目录页码刷新标记
    fixed = 0
    if period:
        fixed = _apply_period_text_fixes(doc, period)
    _enable_update_fields(doc)
    if fixed:
        log.info("报告期文字修正 %d 处（页眉/落款日期/季度）", fixed)

    unfilled = _remaining_markers(doc)
    if unfilled and strict:
        raise RuntimeError(
            "模板中存在未替换的占位符:\n" + "\n".join(f"  {u}" for u in unfilled)
        )

    import os

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    doc.save(output_path)
    rgb_n = _flatten_rgba_in_docx(output_path)
    if rgb_n:
        log.info("输出文档 RGBA 图片转 RGB：%d 张", rgb_n)
    return unfilled


def _remaining_markers(doc: Document) -> List[str]:
    leftovers = []
    for paragraph in _walk_paragraphs(doc):
        text = _paragraph_text(paragraph)
        for m in MARKER_RE.finditer(text):
            leftovers.append(f"{m.group(0)}  <- \"{text.strip()[:50]}\"")
        # 检测未处理的条件块
        if "{{?" in text:
            leftovers.append(f"未处理的条件块  <- \"{text.strip()[:50]}\"")
    return leftovers
