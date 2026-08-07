# -*- coding: utf-8 -*-
"""报告智能体 Web 管理台。

两种运行模式：
  bridge（默认）：本服务管理本机的一个/多个桥配置，提供运行、下载、覆盖度等接口。
  hub：部署在中心服务器（如 222.242.152.65），汇总各桥服务器的状态并提供跳转。

环境变量：
  REPORT_WEB_HOST        监听地址（默认 127.0.0.1，公网请用 nginx 反代 + HTTPS）
  REPORT_WEB_PORT        端口（默认 8080）
  REPORT_WEB_TOKEN       访问令牌；为空时不鉴权（仅建议本机调试）
  REPORT_WEB_MODE        bridge | hub（默认 bridge）
  REPORT_WEB_REGISTRY    桥梁注册表路径（默认 bridges/registry.json）
  REPORT_PROJECT_ROOT    项目根目录（默认自动推断）

启动：
  python web/app.py
  REPORT_WEB_TOKEN=xxx python web/app.py
"""

import datetime as dt
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Dict, Optional

from flask import Flask, Response, jsonify, request, send_file

ROOT = os.environ.get("REPORT_PROJECT_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bootstrap  # noqa: E402

from report_agent.bridges import get_bridge, list_bridges, resolve_bridge_config  # noqa: E402

app = Flask(__name__, static_folder="static", static_url_path="/static")

log = logging.getLogger("report-web")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

WEB_TOKEN = os.environ.get("REPORT_WEB_TOKEN", "")
WEB_MODE = os.environ.get("REPORT_WEB_MODE", "bridge")
REGISTRY = os.environ.get("REPORT_WEB_REGISTRY", os.path.join(ROOT, "bridges", "registry.json"))

PREPROCESS_DIR = os.path.join(ROOT, "preprocess")
PREPROCESS_CONFIG = os.path.join(PREPROCESS_DIR, "config.json")
PREPROCESS_STATUS = os.path.join(PREPROCESS_DIR, "status.json")
PREPROCESS_LOG = os.path.join(PREPROCESS_DIR, "pipeline.log")
_preprocess: Dict = {}

# 每个桥的“运行中”状态
_running: Dict[str, Dict] = {}
_run_lock = threading.Lock()
_schedulers: Dict[str, Dict] = {}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _config_for(bridge_id: str) -> Optional[Dict]:
    cfg_path = resolve_bridge_config(bridge_id, REGISTRY)
    if not cfg_path:
        return None
    try:
        from report_agent.config import load_config
        return load_config(cfg_path)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "_config_path": cfg_path}


def _config_path_for(bridge_id: str) -> Optional[str]:
    return resolve_bridge_config(bridge_id, REGISTRY)


def _save_config(cfg: Dict, cfg_path: str) -> None:
    """写回配置（先备份 .bak）。"""
    try:
        shutil.copyfile(cfg_path, cfg_path + ".bak")
    except OSError:
        pass
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _mask_secrets(cfg: Dict) -> Dict:
    out = dict(cfg)
    llm = out.get("llm")
    if isinstance(llm, dict) and llm.get("api_key"):
        llm = dict(llm)
        llm["api_key"] = "******" if llm["api_key"] else ""
        out["llm"] = llm
    return out


def _bridge_snapshot(bridge: Dict) -> Dict:
    bid = bridge.get("id", "")
    cfg = _config_for(bid)
    snap = {
        "id": bid,
        "name": bridge.get("name", bid),
        "host": bridge.get("host", ""),
        "port": bridge.get("port", 8080),
        "token_env": bridge.get("token_env", ""),
        "description": bridge.get("description", ""),
        "config": bridge.get("config", ""),
        "config_ok": isinstance(cfg, dict) and "error" not in cfg,
        "config_error": cfg.get("error") if isinstance(cfg, dict) else None,
    }
    if isinstance(cfg, dict) and "error" not in cfg:
        snap["template"] = cfg.get("template", "")
        snap["output_dir"] = cfg.get("output_dir", "")
        snap["bridge_data"] = bool((cfg.get("bridge_data") or {}).get("enabled", False))
        snap["schedule"] = cfg.get("schedule", {})
        out_dir = cfg.get("output_dir", "")
        if out_dir and os.path.isdir(out_dir):
            docs = [f for f in os.listdir(out_dir)
                    if f.lower().endswith(".docx") and not f.startswith("~$")]
            docs.sort(key=lambda f: os.path.getmtime(os.path.join(out_dir, f)), reverse=True)
            snap["report_count"] = len(docs)
            snap["latest_report"] = docs[0] if docs else None
            snap["latest_report_mtime"] = (
                dt.datetime.fromtimestamp(os.path.getmtime(os.path.join(out_dir, docs[0]))).isoformat(timespec="seconds")
                if docs else None
            )
        last_run = os.path.join(out_dir or "", "last_run.json")
        if os.path.isfile(last_run):
            try:
                with open(last_run, "r", encoding="utf-8") as f:
                    lr = json.load(f)
                snap["last_run"] = {
                    "output": lr.get("output"),
                    "period": lr.get("period"),
                    "days": lr.get("days"),
                    "pending_charts": len(lr.get("pending_charts", [])),
                    "missing_cells": len(lr.get("missing_cells", [])),
                }
            except Exception:  # noqa: BLE001
                pass
    snap["running"] = bool(_running.get(bid, {}).get("running"))
    return snap


def _require_token() -> Optional[Response]:
    if not WEB_TOKEN:
        return None
    token = request.headers.get("X-Auth-Token", "") or request.args.get("token", "")
    if token != WEB_TOKEN:
        return jsonify({"error": "未授权：token 无效"}), 401
    return None


def _subprocess_env() -> Dict:
    env = dict(os.environ)
    deps = os.path.join(ROOT, ".deps")
    paths = [ROOT]
    if os.path.isdir(deps):
        paths.insert(0, deps)
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/status")
def api_status():
    auth = _require_token()
    if auth:
        return auth
    return jsonify({
        "ok": True,
        "mode": WEB_MODE,
        "project_root": ROOT,
        "time": dt.datetime.now().isoformat(timespec="seconds"),
        "registry": REGISTRY,
        "auth_required": bool(WEB_TOKEN),
        "version": "1.0",
    })


@app.route("/api/bridges")
def api_bridges():
    auth = _require_token()
    if auth:
        return auth
    return jsonify({"bridges": [_bridge_snapshot(b) for b in list_bridges(REGISTRY)]})


@app.route("/api/bridges/<bridge_id>")
def api_bridge(bridge_id):
    auth = _require_token()
    if auth:
        return auth
    b = get_bridge(bridge_id, REGISTRY)
    if not b:
        return jsonify({"error": f"未找到桥梁 {bridge_id}"}), 404
    return jsonify(_bridge_snapshot(b))


@app.route("/api/bridges/<bridge_id>/config")
def api_bridge_config(bridge_id):
    auth = _require_token()
    if auth:
        return auth
    cfg = _config_for(bridge_id)
    if cfg is None:
        return jsonify({"error": f"未找到桥梁 {bridge_id} 的配置"}), 404
    return jsonify(_mask_secrets(cfg))


@app.route("/api/bridges/<bridge_id>/config", methods=["POST"])
def api_bridge_config_update(bridge_id):
    """更新桥配置（数据路径 / 模板 / 调度 / 报告命名等），先备份再写回。"""
    auth = _require_token()
    if auth:
        return auth
    cfg_path = _config_path_for(bridge_id)
    if not cfg_path:
        return jsonify({"error": f"未找到桥梁 {bridge_id} 的配置"}), 404
    try:
        from report_agent.config import load_config
        cfg = load_config(cfg_path)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"配置不可用: {exc}"}), 400

    data = request.get_json(silent=True) or {}
    bd = cfg.setdefault("bridge_data", {})
    if isinstance(data.get("paths"), dict):
        for k in ("stats_dir", "charts_dir", "sensor_map", "name_dict", "overview"):
            if k in data["paths"] and data["paths"][k] is not None:
                bd[k] = str(data["paths"][k]).strip()
    for k in ("sensor_exclude", "auto_fill_missing_charts", "fuzzy_threshold", "period_aggregate"):
        if k in data:
            bd[k] = data[k]
    if isinstance(data.get("metrics"), dict):
        bd["metrics"] = data["metrics"]
    if data.get("template"):
        cfg["template"] = str(data["template"]).strip()
    if data.get("output_dir"):
        cfg["output_dir"] = str(data["output_dir"]).strip()
    if data.get("analysis_file") is not None:
        cfg["analysis_file"] = str(data["analysis_file"]).strip() or None
    if isinstance(data.get("schedule"), dict):
        sch = data["schedule"]
        if sch.get("mode") in ("weekly", "monthly", "quarterly", "yearly"):
            cfg.setdefault("schedule", {})["mode"] = sch["mode"]
        for k in ("weekday", "day_of_month", "hour", "minute"):
            if k in sch and sch[k] is not None:
                try:
                    cfg.setdefault("schedule", {})[k] = int(sch[k])
                except (TypeError, ValueError):
                    pass
    if isinstance(data.get("report"), dict) and data["report"].get("name_prefix") is not None:
        cfg.setdefault("report", {})["name_prefix"] = str(data["report"]["name_prefix"])

    _save_config(cfg, cfg_path)
    return jsonify({"ok": True, "config_path": cfg_path})


@app.route("/api/bridges/<bridge_id>/template", methods=["POST"])
def api_bridge_template_upload(bridge_id):
    """上传报告模板 .docx，保存到 templates/ 并更新配置。"""
    auth = _require_token()
    if auth:
        return auth
    cfg_path = _config_path_for(bridge_id)
    if not cfg_path:
        return jsonify({"error": f"未找到桥梁 {bridge_id} 的配置"}), 404
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "未选择文件"}), 400
    name = os.path.basename(f.filename)
    if not name.lower().endswith(".docx"):
        return jsonify({"error": "仅支持 .docx 模板"}), 400
    templates_dir = os.path.join(ROOT, "templates")
    os.makedirs(templates_dir, exist_ok=True)
    dest = os.path.join(templates_dir, name)
    f.save(dest)
    try:
        from report_agent.config import load_config
        cfg = load_config(cfg_path)
        cfg["template"] = os.path.join("templates", name)
        _save_config(cfg, cfg_path)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"模板已上传但配置更新失败: {exc}"}), 500
    return jsonify({"ok": True, "template": os.path.join("templates", name), "size": os.path.getsize(dest)})


@app.route("/api/bridges/<bridge_id>/data")
def api_bridge_data_check(bridge_id):
    """校验数据存放路径是否可用，并返回统计值/图库概况。"""
    auth = _require_token()
    if auth:
        return auth
    cfg = _config_for(bridge_id)
    if not cfg or "error" in cfg:
        return jsonify({"error": "配置不可用"}), 404
    bd = cfg.get("bridge_data") or {}
    def _info(path):
        if not path or not os.path.isdir(path):
            return {"path": path, "ok": False}
        return {"path": path, "ok": True}
    stats = _info(bd.get("stats_dir", ""))
    charts = _info(bd.get("charts_dir", ""))
    if stats.get("ok"):
        stats["json_files"] = len([x for x in os.listdir(stats["path"]) if x.endswith(".json")])
    if charts.get("ok"):
        charts["sensor_dirs"] = len([x for x in os.listdir(charts["path"])
                                     if os.path.isdir(os.path.join(charts["path"], x))])
    return jsonify({
        "stats_dir": stats,
        "charts_dir": charts,
        "sensor_map": _info(bd.get("sensor_map", "")),
        "name_dict": _info(bd.get("name_dict", "")),
        "overview": _info(bd.get("overview", "")),
        "bridge_name": bd.get("bridge_name", ""),
    })


@app.route("/api/bridges/<bridge_id>/coverage")
def api_bridge_coverage(bridge_id):
    auth = _require_token()
    if auth:
        return auth
    cfg = _config_for(bridge_id)
    if not cfg or "error" in cfg:
        return jsonify({"error": "配置不可用"}), 404
    bcfg = cfg.get("bridge_data") or {}
    if not bcfg.get("enabled", False):
        return jsonify({"enabled": False, "message": "该桥未启用真实数据（bridge_data.enabled=false）"})
    from report_agent.bridge_source import BridgeData
    bridge = BridgeData(bcfg, base_dir=os.path.dirname(cfg.get("_config_path", ROOT)))
    bridge.load()
    return jsonify(bridge.coverage())


@app.route("/api/bridges/<bridge_id>/analysis")
def api_bridge_analysis(bridge_id):
    auth = _require_token()
    if auth:
        return auth
    cfg = _config_for(bridge_id)
    if not cfg or "error" in cfg:
        return jsonify({"error": "配置不可用"}), 404
    analysis = cfg.get("_chart_texts")
    out = {
        "chart_texts_count": len(analysis) if analysis else 0,
        "data_values_count": len(cfg.get("_data_values", {})),
    }
    # 模板占位符统计
    try:
        from report_agent.template_analyzer import analyze_template
        result = analyze_template(cfg.get("template", ""))
        placeholders = result.get("placeholders", [])
        from collections import Counter
        out["placeholder_total"] = len(placeholders)
        def _p_type(p: Dict) -> str:
            t = p.get("type", "unknown")
            if t != "unknown":
                return t
            key = str(p.get("key", ""))
            if key.startswith("cell."):
                return "cell"
            if key.startswith("data."):
                return "data"
            return t
        out["placeholder_by_type"] = dict(Counter(_p_type(p) for p in placeholders))
    except Exception as exc:  # noqa: BLE001
        out["template_error"] = str(exc)
    return jsonify(out)


@app.route("/api/bridges/<bridge_id>/reports")
def api_bridge_reports(bridge_id):
    auth = _require_token()
    if auth:
        return auth
    cfg = _config_for(bridge_id)
    if not cfg or "error" in cfg:
        return jsonify({"error": "配置不可用"}), 404
    out_dir = cfg.get("output_dir", "")
    if not out_dir or not os.path.isdir(out_dir):
        return jsonify({"reports": []})
    reports = []
    for f in os.listdir(out_dir):
        if f.lower().endswith(".docx") and not f.startswith("~$"):
            p = os.path.join(out_dir, f)
            reports.append({
                "name": f,
                "size": os.path.getsize(p),
                "mtime": dt.datetime.fromtimestamp(os.path.getmtime(p)).isoformat(timespec="seconds"),
            })
    reports.sort(key=lambda r: r["mtime"], reverse=True)
    return jsonify({"reports": reports, "output_dir": out_dir})


@app.route("/api/bridges/<bridge_id>/reports/<path:filename>")
def api_bridge_report_download(bridge_id, filename):
    auth = _require_token()
    if auth:
        return auth
    cfg = _config_for(bridge_id)
    if not cfg or "error" in cfg:
        return jsonify({"error": "配置不可用"}), 404
    out_dir = cfg.get("output_dir", "")
    safe = os.path.basename(filename)
    path = os.path.join(out_dir, safe)
    if not os.path.isfile(path) or not safe.lower().endswith(".docx"):
        return jsonify({"error": "文件不存在"}), 404
    return send_file(path, as_attachment=True, download_name=safe)


@app.route("/api/bridges/<bridge_id>/charts/<path:filename>")
def api_bridge_chart(bridge_id, filename):
    auth = _require_token()
    if auth:
        return auth
    cfg = _config_for(bridge_id)
    if not cfg or "error" in cfg:
        return jsonify({"error": "配置不可用"}), 404
    charts_dir = (cfg.get("charts") or {}).get("output_dir", "")
    safe = os.path.basename(filename)
    for base in (charts_dir, os.path.join(cfg.get("output_dir", ""), "charts")):
        path = os.path.join(base, safe)
        if os.path.isfile(path):
            return send_file(path)
    return jsonify({"error": "图片不存在"}), 404


@app.route("/api/bridges/<bridge_id>/run", methods=["POST"])
def api_bridge_run(bridge_id):
    auth = _require_token()
    if auth:
        return auth
    b = get_bridge(bridge_id, REGISTRY)
    cfg_path = resolve_bridge_config(bridge_id, REGISTRY)
    if not b or not cfg_path:
        return jsonify({"error": f"未找到桥梁 {bridge_id} 的配置"}), 404

    data = request.get_json(silent=True) or {}
    mode = data.get("mode") or "quarterly"
    if mode not in ("weekly", "monthly", "quarterly", "yearly", "manual"):
        return jsonify({"error": f"无效模式: {mode}"}), 400
    date = str(data.get("date") or "").strip()
    engine = str(data.get("engine") or "").strip() or None

    with _run_lock:
        st = _running.get(bridge_id)
        if st and st.get("running"):
            return jsonify({"error": "该桥已有任务正在运行", "started_at": st.get("started_at")}), 409
        st = {"running": True, "started_at": dt.datetime.now().isoformat(timespec="seconds")}
        _running[bridge_id] = st

    def _worker():
        cmd = [sys.executable, os.path.join(ROOT, "run_agent.py"),
               "--config", cfg_path, "--mode", mode]
        if date:
            cmd += ["--date", date]
        if engine:
            cmd += ["--engine", engine]
        st["cmd"] = " ".join(cmd)
        try:
            proc = subprocess.Popen(
                cmd, cwd=ROOT, env=_subprocess_env(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            st["pid"] = proc.pid
            out, _ = proc.communicate(timeout=3600)
            st["returncode"] = proc.returncode
            st["log_tail"] = out.decode("utf-8", errors="replace")[-4000:]
            st["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
            log.info("桥梁 %s 报告生成完成，返回码 %s", bridge_id, proc.returncode)
        except Exception as exc:  # noqa: BLE001
            st["error"] = str(exc)
            st["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
            log.exception("桥梁 %s 报告生成异常", bridge_id)
        finally:
            st["running"] = False

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return jsonify({"ok": True, "started": True, "bridge_id": bridge_id,
                    "mode": mode, "started_at": st["started_at"]})


@app.route("/api/bridges/<bridge_id>/scheduler")
def api_bridge_scheduler(bridge_id):
    """调度器状态 + 当前配置。"""
    auth = _require_token()
    if auth:
        return auth
    cfg = _config_for(bridge_id)
    if not cfg or "error" in cfg:
        return jsonify({"error": "配置不可用"}), 404
    st = _schedulers.get(bridge_id, {})
    return jsonify({
        "running": bool(st.get("proc") and st.get("proc").poll() is None),
        "pid": st.get("proc").pid if st.get("proc") and st.get("proc").poll() is None else None,
        "started_at": st.get("started_at"),
        "schedule": cfg.get("schedule", {}),
        "mode_text": {
            "weekly": "每周", "monthly": "每月", "quarterly": "每季度", "yearly": "每年",
        }.get((cfg.get("schedule") or {}).get("mode", ""), "未配置"),
    })


@app.route("/api/bridges/<bridge_id>/scheduler/start", methods=["POST"])
def api_bridge_scheduler_start(bridge_id):
    """启动常驻调度器（serve_scheduler.py --bridge <id>）。"""
    auth = _require_token()
    if auth:
        return auth
    cfg_path = _config_path_for(bridge_id)
    if not cfg_path:
        return jsonify({"error": f"未找到桥梁 {bridge_id} 的配置"}), 404
    st = _schedulers.get(bridge_id)
    if st and st.get("proc") and st["proc"].poll() is None:
        return jsonify({"error": "调度器已在运行", "pid": st["proc"].pid}), 409
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "serve_scheduler.py"), "--bridge", bridge_id],
        cwd=ROOT, env=_subprocess_env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _schedulers[bridge_id] = {"proc": proc, "started_at": dt.datetime.now().isoformat(timespec="seconds")}
    return jsonify({"ok": True, "pid": proc.pid})


@app.route("/api/bridges/<bridge_id>/scheduler/stop", methods=["POST"])
def api_bridge_scheduler_stop(bridge_id):
    """停止常驻调度器。"""
    auth = _require_token()
    if auth:
        return auth
    st = _schedulers.get(bridge_id)
    if not st or not st.get("proc") or st["proc"].poll() is not None:
        return jsonify({"error": "调度器未在运行"}), 404
    try:
        st["proc"].terminate()
        st["proc"].wait(timeout=10)
    except Exception:  # noqa: BLE001
        st["proc"].kill()
    return jsonify({"ok": True, "stopped_pid": st["proc"].pid})


@app.route("/api/bridges/<bridge_id>/run/status")
def api_bridge_run_status(bridge_id):
    auth = _require_token()
    if auth:
        return auth
    st = _running.get(bridge_id, {})
    cfg = _config_for(bridge_id)
    last_run = None
    if cfg and "error" not in cfg:
        p = os.path.join(cfg.get("output_dir", ""), "last_run.json")
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    last_run = json.load(f)
            except Exception:  # noqa: BLE001
                pass
    return jsonify({"running": st, "last_run": last_run})


@app.route("/api/bridges/<bridge_id>/log")
def api_bridge_log(bridge_id):
    auth = _require_token()
    if auth:
        return auth
    cfg = _config_for(bridge_id)
    if not cfg or "error" in cfg:
        return jsonify({"error": "配置不可用"}), 404
    name = request.args.get("name", "agent")
    lines = min(int(request.args.get("lines", 300)), 5000)
    candidates = {
        "agent": os.path.join(cfg.get("output_dir", ""), "agent.log"),
        "scheduler": os.path.join(cfg.get("output_dir", ""), "scheduler.log"),
        "web": os.path.join(ROOT, "outputs", "web.log"),
        "run": os.path.join(ROOT, "outputs", "web_run.log"),
    }
    path = candidates.get(name)
    if not path or not os.path.isfile(path):
        return jsonify({"log": "", "path": path or ""})
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        tail = f.readlines()[-lines:]
    return jsonify({"log": "".join(tail), "path": path, "lines": len(tail)})


@app.route("/api/preprocess/config")
def api_preprocess_config():
    """读取数据处理管道配置（秒级/日级/图库/统计值路径）。"""
    auth = _require_token()
    if auth:
        return auth
    if not os.path.isfile(PREPROCESS_CONFIG):
        return jsonify({"error": "未找到 preprocess/config.json"}), 404
    with open(PREPROCESS_CONFIG, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return jsonify(cfg)


@app.route("/api/preprocess/config", methods=["POST"])
def api_preprocess_config_save():
    """保存数据处理管道配置。"""
    auth = _require_token()
    if auth:
        return auth
    data = request.get_json(silent=True) or {}
    cfg = {}
    if os.path.isfile(PREPROCESS_CONFIG):
        with open(PREPROCESS_CONFIG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    for k in ("raw_data_dir", "daily_dir", "charts_dir", "stats_dir",
              "sensor_map_docx", "bridge_name"):
        if k in data and data[k] is not None:
            cfg[k] = str(data[k]).strip()
    os.makedirs(PREPROCESS_DIR, exist_ok=True)
    with open(PREPROCESS_CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/preprocess/run", methods=["POST"])
def api_preprocess_run():
    """启动数据处理管道（后台执行）。"""
    auth = _require_token()
    if auth:
        return auth
    st = _preprocess.get("proc")
    if st and st.poll() is None:
        return jsonify({"error": "数据处理管道已在运行", "pid": st.pid}), 409
    data = request.get_json(silent=True) or {}
    cmd = [sys.executable, os.path.join(PREPROCESS_DIR, "pipeline.py")]
    flags = {
        "raw": data.get("raw_data_dir"), "daily": data.get("daily_dir"),
        "charts": data.get("charts_dir"), "stats": data.get("stats_dir"),
        "sensor_map_docx": data.get("sensor_map_docx"),
    }
    for k, v in flags.items():
        if v:
            cmd += [f"--{k}", str(v)]
    if data.get("skip_preprocess"):
        cmd.append("--skip-preprocess")
    if data.get("skip_charts"):
        cmd.append("--skip-charts")
    proc = subprocess.Popen(cmd, cwd=ROOT, env=_subprocess_env(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _preprocess["proc"] = proc
    _preprocess["started_at"] = dt.datetime.now().isoformat(timespec="seconds")
    return jsonify({"ok": True, "pid": proc.pid})


@app.route("/api/preprocess/status")
def api_preprocess_status():
    """数据处理管道状态 + 日志尾部。"""
    auth = _require_token()
    if auth:
        return auth
    st = _preprocess.get("proc")
    running = bool(st and st.poll() is None)
    status = {}
    if os.path.isfile(PREPROCESS_STATUS):
        with open(PREPROCESS_STATUS, "r", encoding="utf-8") as f:
            status = json.load(f)
    tail = ""
    if os.path.isfile(PREPROCESS_LOG):
        with open(PREPROCESS_LOG, "r", encoding="utf-8", errors="replace") as f:
            tail = "".join(f.readlines()[-120:])
    return jsonify({
        "running": running,
        "pid": st.pid if st and running else None,
        "status": status,
        "log_tail": tail,
    })


@app.route("/api/hub/bridges")
def api_hub_bridges():
    auth = _require_token()
    if auth:
        return auth
    if WEB_MODE != "hub":
        return jsonify({"error": "当前不是 hub 模式（设置 REPORT_WEB_MODE=hub）"}), 400
    import requests
    out = []
    for b in list_bridges(REGISTRY):
        host = b.get("host", "")
        port = b.get("port", 8080)
        token = os.environ.get(b.get("token_env", ""), "") or WEB_TOKEN
        if not host:
            out.append({
                "id": b.get("id"),
                "name": b.get("name"),
                "url": "",
                "reachable": False,
                "error": "注册表中未配置 host",
            })
            continue
        url = f"http://{host}:{port}/api/status"
        item = {"id": b.get("id"), "name": b.get("name"), "url": url, "reachable": False}
        try:
            headers = {"X-Auth-Token": token} if token else {}
            r = requests.get(url, headers=headers, timeout=5)
            item["reachable"] = r.status_code == 200
            if r.ok:
                item["remote"] = r.json()
        except Exception as exc:  # noqa: BLE001
            item["error"] = str(exc)
        out.append(item)
    return jsonify({"bridges": out})


def main():
    host = os.environ.get("REPORT_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("REPORT_WEB_PORT", "8080"))
    log.info("报告智能体 Web 管理台启动: http://%s:%s  mode=%s  auth=%s",
             host, port, WEB_MODE, "on" if WEB_TOKEN else "off")
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
