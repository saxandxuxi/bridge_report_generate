
function $(id) { return document.getElementById(id); }
var STATE = { bridge: null, bridges: [], pollTimer: null };

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function token() { return localStorage.getItem("report_token") || ""; }
function saveToken() {
  localStorage.setItem("report_token", $("tokenInput").value.trim());
  showMsg("令牌已保存", "ok");
}

function api(path, opts) {
  opts = opts || {};
  var headers = { "Content-Type": "application/json" };
  var t = token();
  if (t) headers["X-Auth-Token"] = t;
  if (opts.headers) { for (var k in opts.headers) headers[k] = opts.headers[k]; }
  return fetch(path, Object.assign({}, opts, { headers: headers }))
    .then(function (res) {
      if (res.status === 401) { showMsg("未授权：请检查令牌", "err"); throw new Error("unauthorized"); }
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok && data.error) throw new Error(data.error);
        return data;
      });
    });
}

function showMsg(text, type) {
  var m = $("msg");
  m.className = "msg " + (type || "");
  m.textContent = text;
  setTimeout(function () { m.className = "msg"; }, 5000);
}

function showTab(name) {
  var btns = document.querySelectorAll("nav button");
  for (var i = 0; i < btns.length; i++) btns[i].classList.toggle("active", btns[i].dataset.tab === name);
  var secs = document.querySelectorAll("main > section");
  for (var j = 0; j < secs.length; j++) secs[j].style.display = "none";
  var el = $("tab-" + name);
  if (el) el.style.display = "block";
  if (!currentBridgeId()) { loadCurrentBridge(); return; }
  if (name === "run") { loadRunTemplates(); checkPeriod(); }
  if (name === "download") loadReports();
  if (name === "upload") { $("srBridgeHint").textContent = "上传到当前桥：" + (STATE.bridge ? STATE.bridge.name : ""); resetUploadPanel(); }
  if (name === "preprocess") loadPreprocessConfig();
  if (name === "config") loadConfig();
  if (name === "scheduler") loadScheduler();
  if (name === "review") loadReview();
  if (name === "log") loadLog();
}

function currentBridgeId() {
  return STATE.bridge ? STATE.bridge.id : "";
}

function loadRunTemplates() {
  var bridgeId = currentBridgeId();
  if (!bridgeId) return;
  api("/api/bridges/" + bridgeId + "/templates").then(function (d) {
    var sel = $("runTemplate");
    var cur = sel.value;
    sel.innerHTML = '<option value="">默认模板（当前配置' +
      (d.current ? "：" + esc(d.current) : "") + "）</option>";
    (d.templates || []).forEach(function (t) {
      var o = document.createElement("option");
      o.value = t.path;
      o.textContent = t.name + (t.current ? "（当前）" : "");
      sel.appendChild(o);
    });
    if (cur) sel.value = cur;
  }).catch(function () {});
}

function renderCurrentCard() {
  var b = STATE.bridge;
  var box = $("currentBridgeCard");
  if (!b) { box.innerHTML = '<p class="muted">本机尚未登记桥梁，请在上方选择“新桥注册”完成登记。</p>'; return; }
  var dot = b.config_ok ? "ok" : "bad";
  var last = "尚无运行记录";
  if (b.last_run) {
    last = (b.last_run.period && b.last_run.period.label ? b.last_run.period.label : "—") +
      " · 未填充单元格 " + (b.last_run.missing_cells || 0);
  }
  var h = '<div class="cur-card"><h2><span class="dot ' + dot + '"></span>' + esc(b.name) + '</h2><div class="kv">';
  h += '<b>桥 ID</b><span>' + esc(b.id) + '</span>';
  h += '<b>配置</b><span>' + (b.config_ok ? "正常" : esc(b.config_error || "缺失")) + '</span>';
  h += '<b>真实数据</b><span>' + (b.bridge_data ? "已启用" : "未启用") + '</span>';
  h += '<b>报告数</b><span>' + (b.report_count || 0) + '</span>';
  h += '<b>最近报告</b><span>' + esc(b.latest_report || "—") + '</span>';
  h += '<b>上次运行</b><span>' + esc(last) + '</span>';
  h += '</div><div class="row" style="margin-top:12px;margin-bottom:0">';
  h += '<button onclick="showTab(\'run\')">生成报告</button>';
  h += '<button class="ghost" onclick="showTab(\'download\')">下载 / 预览</button>';
  h += '<button class="ghost" onclick="showTab(\'review\')">审查问题</button>';
  h += '<button class="ghost" onclick="showTab(\'config\')">配置管理</button>';
  h += '</div></div>';
  box.innerHTML = h;
}

function loadCurrentBridge() {
  api("/api/current-bridge").then(function (data) {
    STATE.bridges = data.bridges || [];
    STATE.bridge = data.bridge || null;
    var sel = $("curBridge");
    sel.innerHTML = "";
    (data.bridges || []).forEach(function (b) {
      var o = document.createElement("option");
      o.value = b.id;
      o.textContent = b.name;
      sel.appendChild(o);
    });
    if (data.bridge) sel.value = data.bridge.id;
    $("curBridgeName").textContent = data.bridge ? data.bridge.id : "（未登记）";
    $("curBridgeStatus").textContent = data.bridge ? (data.bridge.config_ok ? "配置正常" : "配置缺失") : "";
    $("curBridgeStatus").className = "badge " + (data.bridge && data.bridge.config_ok ? "on" : "");
    renderCurrentCard();
    if (!data.bridge) {
      $("curMode").value = "new";
      $("curBridge").style.display = "none";
      showRegister();
    }
    var active = document.querySelector("nav button.active");
    if (active) showTab(active.dataset.tab);
  }).catch(function (e) { showMsg(String(e), "err"); });
}

function onModeChange() {
  if ($("curMode").value === "new") {
    $("curBridge").style.display = "none";
    showRegister();
  } else {
    $("curBridge").style.display = "";
    closeRegister();
  }
}

function onBridgeSwitch() {
  var id = $("curBridge").value;
  if (!id || id === currentBridgeId()) return;
  api("/api/bridges/switch", {
    method: "POST", body: JSON.stringify({ bridge_id: id })
  }).then(function (d) {
    showMsg("已切换当前桥：" + d.bridge.name, "ok");
    loadCurrentBridge();
  }).catch(function (e) { showMsg(String(e), "err"); });
}

function showRegister() {
  $("regMask").classList.add("show");
  fillRegTemplates();
  fillRegLlmProviders();
}

function closeRegister() {
  $("regMask").classList.remove("show");
  $("regStatus").textContent = "";
}

function fillRegTemplates() {
  var bridgeId = currentBridgeId();
  var sel = $("regTemplate");
  sel.innerHTML = '<option value="">（上传模板文件优先，此项可选）</option>';
  if (!bridgeId) return;
  api("/api/bridges/" + bridgeId + "/templates").then(function (d) {
    (d.templates || []).forEach(function (t) {
      var o = document.createElement("option");
      o.value = t.path;
      o.textContent = t.name;
      sel.appendChild(o);
    });
  }).catch(function () {});
}

function fillRegLlmProviders() {
  var sel = $("regLlmProvider");
  sel.innerHTML = '<option value="">请选择供应商</option>';
  Object.keys(LLM_PROVIDERS).forEach(function (k) {
    var p = LLM_PROVIDERS[k];
    var o = document.createElement("option");
    o.value = k;
    o.textContent = p.label + "（" + p.model + "）";
    sel.appendChild(o);
  });
}

function submitRegister() {
  var name = $("regName").value.trim();
  if (!name) { showMsg("请填写桥名", "err"); return; }
  var fd = new FormData();
  fd.append("bridge_name", name);
  fd.append("raw_data_dir", $("regRaw").value.trim());
  fd.append("daily_dir", $("regDaily").value.trim());
  fd.append("stats_dir", $("regStats").value.trim());
  fd.append("charts_dir", $("regCharts").value.trim());
  fd.append("template", $("regTemplate").value || "");
  fd.append("llm_provider", $("regLlmProvider").value);
  fd.append("llm_api_key", $("regLlmKey").value.trim());
  fd.append("llm_model", $("regLlmModel").value.trim());
  fd.append("schedule_mode", $("regScheduleMode").value);
  fd.append("schedule_start_date", $("regScheduleStart").value || "");
  var files = [
    ["regMapDocx", "sensor_map_docx"],
    ["regSourceReport", "source_report"],
    ["regTemplateFile", "template_file"]
  ];
  files.forEach(function (pair) {
    var f = $(pair[0]).files[0];
    if (f) fd.append(pair[1], f);
  });
  var headers = {};
  var t = token();
  if (t) headers["X-Auth-Token"] = t;
  $("regStatus").textContent = "注册中…";
  fetch("/api/bridges/register", { method: "POST", body: fd, headers: headers })
    .then(function (res) { return res.json(); })
    .then(function (d) {
      if (d.error) throw new Error(d.error);
      showMsg("新桥已注册：" + d.bridge_name + "（" + d.bridge_id + "），配置文件已生成", "ok");
      $("regStatus").textContent = "";
      $("regMask").classList.remove("show");
      $("curMode").value = "existing";
      $("curBridge").style.display = "";
      loadCurrentBridge();
    })
    .catch(function (e) { showMsg(String(e), "err"); $("regStatus").textContent = ""; });
}

function onModeChange() {
  var mode = $("runMode").value;
  var quarterly = mode === "quarterly";
  var yearly = mode === "yearly";
  var manual = mode === "manual";
  $("runQuarter").style.display = quarterly ? "" : "none";
  $("runYear").style.display = (quarterly || yearly) ? "" : "none";
  $("runEnd").style.display = (quarterly || yearly) ? "none" : "";
  $("runStart").style.display = manual ? "" : "none";
  if (yearly) {
    $("runYear").value = defaultYear();
  } else if (quarterly) {
    var _dq = defaultQuarter();
    $("runQuarter").value = _dq.quarter;
    $("runYear").value = _dq.year;
  }
  checkPeriod();
}
function defaultQuarter() {
  // 默认取最近一个已完整结束的季度（如 8 月默认第 2 季度、1 月默认去年第 4 季度）
  var now = new Date();
  var y = now.getFullYear();
  var cur = Math.floor(now.getMonth() / 3);   // 0 基当前季度
  var q = cur === 0 ? 4 : cur;                 // 已结束的季度号
  if (cur === 0) y -= 1;
  return { quarter: String(q), year: String(y) };
}
function defaultYear() {
  // 最近一个已完整结束的年份（如 2026 年 8 月 -> 2025）
  return String(new Date().getFullYear() - 1);
}
$("runMode").addEventListener("change", onModeChange);
$("runEnd").addEventListener("change", checkPeriod);
$("runQuarter").value = defaultQuarter().quarter;
$("runYear").value = defaultYear();
onModeChange();

function checkPeriod() {
  var bridgeId = currentBridgeId();
  if (!bridgeId) return;
  var mode = $("runMode").value;
  var url = "/api/bridges/" + bridgeId + "/period?mode=" + encodeURIComponent(mode);
  if (mode === "quarterly") {
    url += "&quarter=" + encodeURIComponent($("runQuarter").value || "1") +
           "&year=" + encodeURIComponent($("runYear").value || String(new Date().getFullYear()));
  } else if (mode === "yearly") {
    url += "&year=" + encodeURIComponent($("runYear").value || defaultYear());
  } else if (mode === "manual" && $("runStart").value && $("runEnd").value) {
    url += "&start=" + $("runStart").value + "&end=" + $("runEnd").value;
  } else if ($("runEnd").value) {
    url += "&date=" + $("runEnd").value;
  }
  api(url).then(function (p) {
    $("periodInfo").innerHTML =
      "周期: <b>" + esc(p.label || "—") + "</b>（" + esc(p.start) + " ~ " + esc(p.end) + "）<br>" +
      "图库: " + esc(p.charts_dir) + "　" + (p.charts_exists ? "✔ 已存在" : "✘ 缺失") + "<br>" +
      "统计值: " + esc(p.stats_dir) + "　" + (p.stats_exists ? "✔ 已存在" : "✘ 缺失") + "<br>" +
      (p.data_ready ? '<span class="green">数据就绪，将跳过预处理直接生成报告</span>'
                    : '<span class="yellow">该周期图库/统计值缺失，勾选“自动预处理”会先跑数据处理</span>');
  }).catch(function (e) {
    $("periodInfo").innerHTML = '<span class="err">' + esc(String(e)) + "</span>";
  });
}

function startRun() {
  var bridgeId = currentBridgeId();
  var body = {
    mode: $("runMode").value,
    date: $("runEnd").value || "",
    template: $("runTemplate").value || ""
  };
  if (body.mode === "quarterly") {
    body.quarter = $("runQuarter").value || "1";
    body.year = $("runYear").value || String(new Date().getFullYear());
  } else if (body.mode === "yearly") {
    body.year = $("runYear").value || defaultYear();
  }
  if (body.mode === "manual") body.start = $("runStart").value || "";
  body.auto_preprocess = $("runAutoPre").checked;
  $("runResult").innerHTML = "";
  $("runStatus").textContent = "正在启动…";
  api("/api/bridges/" + bridgeId + "/run", { method: "POST", body: JSON.stringify(body) })
    .then(function (r) {
      showMsg("已启动 " + bridgeId + " 的报告生成", "ok");
      $("runStatus").textContent = "已启动，等待完成…";
      pollRun(bridgeId);
    })
    .catch(function (e) { showMsg(String(e), "err"); $("runStatus").textContent = ""; });
}

function pollRun(bridgeId) {
  if (STATE.pollTimer) clearInterval(STATE.pollTimer);
  STATE.pollTimer = setInterval(function () {
    api("/api/bridges/" + bridgeId + "/run/status").then(function (st) {
      var running = st.running && st.running.running;
      if (!running) {
        clearInterval(STATE.pollTimer);
        STATE.pollTimer = null;
        if (st.running && st.running.error) {
          $("runStatus").textContent = "失败: " + st.running.error;
          $("runResult").innerHTML = "<pre>" + esc(JSON.stringify({
            error: st.running.error,
            period: st.running.period,
            charts_dir: st.running.charts_dir,
            stats_dir: st.running.stats_dir,
            preprocess: st.running.preprocess,
            log_tail: st.running.log_tail || st.running.pipeline_log_tail || ""
          }, null, 2)) + "</pre>";
        } else {
          $("runStatus").textContent = "完成";
          if (st.running && st.running.period_mismatch) {
            $("runStatus").textContent = "完成（⚠ 报告期不一致）";
            showMsg(st.running.period_mismatch, "err");
          }
        }
        if (st.last_run && !(st.running && st.running.error)) {
          var s = {
            output: st.last_run.output,
            period: st.last_run.period,
            days: st.last_run.days,
            pending_charts: st.last_run.pending_charts ? st.last_run.pending_charts.length : 0,
            missing_cells: st.last_run.missing_cells ? st.last_run.missing_cells.length : 0,
            chart_gaps: st.last_run.chart_gaps ? st.last_run.chart_gaps.length : 0
          };
          $("runResult").innerHTML = "<pre>" + esc(JSON.stringify(s, null, 2)) + "</pre>";
        }
        loadCurrentBridge();
        loadReports();
        checkPeriod();
      } else {
        $("runStatus").textContent = "运行中…（PID " + (st.running.pid || "?") + "）";
      }
    }).catch(function () { clearInterval(STATE.pollTimer); STATE.pollTimer = null; });
  }, 4000);
}

function loadReports() {
  var bridgeId = currentBridgeId();
  if (!bridgeId) return;
  var tbody = $("reportTable").querySelector("tbody");
  tbody.innerHTML = "";
  api("/api/bridges/" + bridgeId + "/reports").then(function (r) {
    if (!r.reports.length) { tbody.innerHTML = '<tr><td colspan="4" class="muted">暂无报告</td></tr>'; return; }
    var q = token() ? "?token=" + encodeURIComponent(token()) : "";
    r.reports.forEach(function (f) {
      var tr = document.createElement("tr");
      tr.innerHTML = "<td>" + esc(f.name) + "</td><td>" + Math.round(f.size / 1024) + " KB</td><td>" + esc(f.mtime) + "</td>" +
        '<td><button class="ghost" style="padding:3px 9px" onclick="previewReport(\'' + bridgeId + '\',\'' + esc(f.name).replace(/'/g, "\\'") + '\')">预览</button> ' +
        '<a href="/api/bridges/' + bridgeId + '/reports/' + encodeURIComponent(f.name) + q + '">下载</a></td>';
      tbody.appendChild(tr);
    });
  }).catch(function (e) { showMsg(String(e), "err"); });
}

function previewReport(bridgeId, name) {
  $("pvMask").classList.add("show");
  $("pvTitle").textContent = name;
  $("pvCount").textContent = "";
  $("pvDoc").innerHTML = '<p class="muted">加载中…</p>';
  $("pvSide").innerHTML = "";
  api("/api/bridges/" + bridgeId + "/reports/" + encodeURIComponent(name) + "/preview")
    .then(function (r) {
      $("pvDoc").innerHTML = r.html || '<p class="muted">（无内容）</p>';
      $("pvCount").textContent = r.needs_human_count ? "需人工处理 " + r.needs_human_count + " 处" : "无人工待办";
      var side = "";
      (r.issues || []).forEach(function (i, idx) {
        side += '<div class="issue-item"><span class="t">[' + esc(i.type) + ']</span> ' +
          esc(i.detail || "") + "</div>";
      });
      $("pvSide").innerHTML = side || '<p class="muted">无人工待办问题</p>';
    })
    .catch(function (e) {
      $("pvDoc").innerHTML = '<p class="muted">预览失败：' + esc(String(e)) + "</p>";
    });
}

function closePreview() {
  $("pvMask").classList.remove("show");
  $("pvDoc").innerHTML = "";
  $("pvSide").innerHTML = "";
}

function loadPreprocessConfig() {
  api("/api/preprocess/config").then(function (c) {
    $("ppRaw").value = c.raw_data_dir || "";
    $("ppDaily").value = c.daily_dir || "";
    $("ppCharts").value = c.charts_dir || "";
    $("ppStats").value = c.stats_dir || "";
    $("ppMapDocx").value = c.sensor_map_docx || "";
    $("ppBridge").value = c.bridge_name || "";
    if (!$("ppBridge").value && STATE.bridge) $("ppBridge").value = STATE.bridge.name;
    $("ppStart").value = c.start || "";
    $("ppEnd").value = c.end || "";
    refreshPreprocess();
  }).catch(function (e) { showMsg(String(e), "err"); });
}

function savePreprocessConfig() {
  if (!$("ppRaw").value.trim() || !$("ppDaily").value.trim()
      || !$("ppBridge").value.trim()) {
    showMsg("秒级数据目录、日级数据目录、桥名为必填项", "err");
    return false;
  }
  var body = {
    raw_data_dir: $("ppRaw").value.trim(),
    daily_dir: $("ppDaily").value.trim(),
    charts_dir: $("ppCharts").value.trim(),
    stats_dir: $("ppStats").value.trim(),
    sensor_map_docx: $("ppMapDocx").value.trim(),
    bridge_name: $("ppBridge").value.trim()
  };
  api("/api/preprocess/config", { method: "POST", body: JSON.stringify(body) })
    .then(function (r) {
      var msg = "预处理配置已保存";
      if (r.bridge_name) msg += "，已自动更新桥配置：" + r.bridge_name;
      if (r.bridge_config) msg += "（" + r.bridge_config + "）";
      showMsg(msg, "ok");
    })
    .catch(function (e) { showMsg(String(e), "err"); });
  return true;
}

function runPreprocess() {
  if (!savePreprocessConfig()) return;
  var body = {
    raw_data_dir: $("ppRaw").value.trim(),
    daily_dir: $("ppDaily").value.trim(),
    charts_dir: $("ppCharts").value.trim(),
    stats_dir: $("ppStats").value.trim(),
    sensor_map_docx: $("ppMapDocx").value.trim(),
    bridge_name: $("ppBridge").value.trim(),
    start: $("ppStart").value || "",
    end: $("ppEnd").value || ""
  };
  api("/api/preprocess/run", { method: "POST", body: JSON.stringify(body) })
    .then(function (r) {
      showMsg("数据处理已启动（PID " + r.pid + "）", "ok");
      pollPreprocess();
    })
    .catch(function (e) { showMsg(String(e), "err"); });
}

function refreshPreprocess() {
  api("/api/preprocess/status").then(function (s) {
    var st = s.status || {};
    $("ppStatus").innerHTML = s.running
      ? '<span class="yellow">处理中…（PID ' + s.pid + '，步骤：' + esc(st.step || "?") + '）</span>'
      : (st.error ? '<span class="red">上次失败：' + esc(st.error) + '</span>'
                  : '<span class="muted">空闲（上次完成：' + esc(st.finished_at || "—") + '）</span>');
    if (s.log_tail) $("ppLog").textContent = s.log_tail;
  }).catch(function () {});
}

function pollPreprocess() {
  if (STATE.pollTimer) clearInterval(STATE.pollTimer);
  STATE.pollTimer = setInterval(function () {
    api("/api/preprocess/status").then(function (s) {
      refreshPreprocess();
      if (!s.running) {
        clearInterval(STATE.pollTimer);
        STATE.pollTimer = null;
        showMsg("数据处理" + (s.status && s.status.error ? "失败：" + s.status.error : "完成"), s.status && s.status.error ? "err" : "ok");
      }
    }).catch(function () { clearInterval(STATE.pollTimer); STATE.pollTimer = null; });
  }, 5000);
}

function loadConfig() {
  var bridgeId = currentBridgeId();
  if (!bridgeId) return;
  api("/api/bridges/" + bridgeId + "/config").then(function (c) {
    var bd = c.bridge_data || {};
    $("cfgStatsDir").value = bd.stats_dir || "";
    $("cfgChartsDir").value = bd.charts_dir || "";
    $("cfgSensorMap").value = bd.sensor_map || "";
    $("cfgNameDict").value = bd.name_dict || "";
    $("cfgTemplate").value = c.template || "";
    $("cfgNamePrefix").value = (c.report || {}).name_prefix || "";
    var llm = c.llm || {};
    $("cfgLlmKey").value = "";
    $("cfgLlmProvider").value = llm.provider || "";
    $("cfgLlmModel").value = llm.model || "";
    loadLlmProviders(llm.provider || "");
    if (llm.provider && !llm.model) applyLlmProvider(llm.provider);
    showMsg("配置已加载", "ok");
  }).catch(function (e) { showMsg(String(e), "err"); });
}

var LLM_PROVIDERS = {
  qwen: { label: "通义千问 QWEN", model: "qwen-plus" },
  zhipu: { label: "智谱 GLM", model: "glm-4-flash" },
  deepseek: { label: "DeepSeek", model: "deepseek-chat" },
  moonshot: { label: "Kimi / Moonshot", model: "moonshot-v1-8k" }
};

function loadLlmProviders(current) {
  var sel = $("cfgLlmProvider");
  sel.innerHTML = '<option value="">请选择供应商</option>';
  Object.keys(LLM_PROVIDERS).forEach(function (k) {
    var p = LLM_PROVIDERS[k];
    var opt = document.createElement("option");
    opt.value = k;
    opt.textContent = p.label + "（" + p.model + "）";
    sel.appendChild(opt);
  });
  if (current) sel.value = current;
}

function applyLlmProvider(prov) {
  var p = LLM_PROVIDERS[prov];
  if (p) $("cfgLlmModel").value = p.model;
}
$("cfgLlmProvider").addEventListener("change", function () {
  applyLlmProvider($("cfgLlmProvider").value);
});

function testLlm() {
  var bridgeId = currentBridgeId();
  if (!bridgeId) return;
  $("llmTestResult").textContent = "测试中…";
  api("/api/bridges/" + bridgeId + "/llm/test", {
    method: "POST",
    body: JSON.stringify({
      provider: $("cfgLlmProvider").value,
      api_key: $("cfgLlmKey").value.trim(),
      model: $("cfgLlmModel").value.trim()
    })
  }).then(function (r) {
    if (r && r.ok) {
      $("llmTestResult").textContent = "连接成功：模型回复 " + esc(r.reply || "OK");
      $("llmTestResult").className = "muted green";
    } else if (r && r.error) {
      $("llmTestResult").textContent = "连接失败：" + esc(r.error);
      $("llmTestResult").className = "muted red";
    } else {
      $("llmTestResult").textContent =
        "服务器返回异常（多半是 web/app.py 版本过旧、缺少 llm/test 接口，请同步并重启 web）";
      $("llmTestResult").className = "muted red";
    }
  }).catch(function (e) {
    $("llmTestResult").textContent = "连接失败：" + String((e && e.message) || e);
    $("llmTestResult").className = "muted red";
  });
}

function testDataPaths() {
  var bridgeId = currentBridgeId();
  if (!bridgeId) return;
  $("dataTestResult").textContent = "测试中…";
  api("/api/bridges/" + bridgeId + "/data").then(function (d) {
    var parts = [];
    function fmt(x, label) {
      parts.push(label + (x.ok ? " ✓" : " ✗"));
    }
    fmt(d.stats_dir, "统计值");
    fmt(d.charts_dir, "图库");
    fmt(d.sensor_map, "对照表");
    fmt(d.name_dict, "名称字典");
    var extra = "";
    if (d.stats_dir.ok) extra += "（JSON " + d.stats_dir.json_files + " 个）";
    if (d.charts_dir.ok) extra += "（传感器 " + d.charts_dir.sensor_dirs + " 个）";
    $("dataTestResult").textContent = parts.join(" | ") + extra;
  }).catch(function (e) { showMsg(String(e), "err"); $("dataTestResult").textContent = ""; });
}

function saveConfig() {
  var bridgeId = currentBridgeId();
  if (!bridgeId) return;
  var _llm = {
    provider: $("cfgLlmProvider").value,
    model: $("cfgLlmModel").value.trim(),
    enabled: true
  };
  var _k = $("cfgLlmKey").value.trim();
  if (_k) _llm.api_key = _k;   // 只有填了才写，避免空框覆盖已保存的 Key
  var body = {
    paths: {
      stats_dir: $("cfgStatsDir").value.trim(),
      charts_dir: $("cfgChartsDir").value.trim(),
      sensor_map: $("cfgSensorMap").value.trim(),
      name_dict: $("cfgNameDict").value.trim()
    },
    report: { name_prefix: $("cfgNamePrefix").value.trim() },
    llm: _llm
  };
  api("/api/bridges/" + bridgeId + "/config", { method: "POST", body: JSON.stringify(body) })
    .then(function () { showMsg("配置已保存", "ok"); })
    .catch(function (e) { showMsg(String(e), "err"); });
}

function uploadTemplate() {
  var bridgeId = currentBridgeId();
  var f = $("tplFile").files[0];
  if (!bridgeId || !f) { showMsg("请选择模板文件", "err"); return; }
  var fd = new FormData();
  fd.append("file", f);
  var t = token();
  var headers = {};
  if (t) headers["X-Auth-Token"] = t;
  fetch("/api/bridges/" + bridgeId + "/template", { method: "POST", body: fd, headers: headers })
    .then(function (res) { return res.json(); })
    .then(function (d) {
      if (d.error) throw new Error(d.error);
      showMsg("模板已上传：" + d.template, "ok");
      loadConfig();
    })
    .catch(function (e) { showMsg(String(e), "err"); });
}

function resetUploadPanel() {
  $("srActions").style.display = "none";
  $("parseStatus").textContent = "";
  $("srStatus").textContent = "";
}

function uploadSourceReport() {
  var bridgeId = currentBridgeId();
  var f = $("srFile").files[0];
  if (!bridgeId || !f) { showMsg("请选择成品报告文件（当前桥：" + (STATE.bridge ? STATE.bridge.name : "") + "）", "err"); return; }
  var fd = new FormData();
  fd.append("file", f);
  fd.append("bridge_target", "same");
  var headers = {};
  var t = token();
  if (t) headers["X-Auth-Token"] = t;
  $("srStatus").textContent = "上传中…";
  fetch("/api/bridges/" + bridgeId + "/source-report", { method: "POST", body: fd, headers: headers })
    .then(function (res) { return res.json(); })
    .then(function (d) {
      if (d.error) throw new Error(d.error);
      showMsg("成品报告已上传", "ok");
      $("srSaved").textContent = "已保存：" + d.source_report +
        (d.template ? "　当前模版：" + d.template : "（当前桥尚无模版，可②重新解析或③上传模版）");
      $("srActions").style.display = "";
      $("srStatus").textContent = "完成";
    })
    .catch(function (e) { showMsg(String(e), "err"); $("srStatus").textContent = ""; });
}

function currentUploadBridge() {
  return currentBridgeId();
}

function useExistingTemplate() {
  var bridgeId = currentUploadBridge();
  if (!bridgeId) return;
  showTab("run");
  startRun();
}

function parseNewTemplate() {
  var bridgeId = currentUploadBridge();
  if (!bridgeId) return;
  $("parseStatus").textContent = "正在解析成品报告生成新模版（LLM 识别，可能需要几分钟）…";
  api("/api/bridges/" + bridgeId + "/template/parse", { method: "POST", body: "{}" })
    .then(function (r) {
      showMsg("模板解析已启动", "ok");
      pollParse(bridgeId);
    })
    .catch(function (e) { showMsg(String(e), "err"); $("parseStatus").textContent = ""; });
}

function pollParse(bridgeId) {
  var timer = setInterval(function () {
    api("/api/bridges/" + bridgeId + "/parse/status").then(function (s) {
      if (s.running) {
        $("parseStatus").textContent = "解析中…（PID " + (s.pid || "?") + "）";
        return;
      }
      clearInterval(timer);
      if (s.error) {
        $("parseStatus").innerHTML = '<span class="red">' + esc(s.error) + "</span>";
        if (s.log_tail) $("parseStatus").innerHTML += "<pre>" + esc(s.log_tail.slice(-1500)) + "</pre>";
      } else {
        $("parseStatus").innerHTML = '<span class="green">解析完成，详见下方结果</span>';
        loadParseResult(bridgeId);
        loadConfig();
        loadRunTemplates();
      }
    }).catch(function () { clearInterval(timer); });
  }, 5000);
}

function loadParseResult(bridgeId) {
  api("/api/bridges/" + bridgeId + "/parse/result").then(function (r) {
    if (!r.template && !r.analysis) {
      $("parseResult").style.display = "none";
      return;
    }
    var h = '<div class="panel" style="margin:0"><h3>解析结果</h3><div class="kv">';
    if (r.template) {
      h += '<b>新模版</b><span>' + esc(r.template.name) +
        '　<a href="' + r.template.download + '" target="_blank">下载</a></span>';
    }
    if (r.analysis) {
      var s = r.analysis.summary || {};
      var num = s.numbers || {};
      var img = s.images || {};
      h += '<b>数字</b><span>替换 ' + (num.replace || 0) + ' / 保留 ' + (num.keep || 0) +
        (num.review ? ' / 待确认 ' + num.review : '') + '</span>';
      h += '<b>图片</b><span>替换 ' + (img.replace || 0) + ' / 保留 ' + (img.keep || 0) + '</span>';
      h += '<b>图表占位</b><span>' + (r.analysis.chart_texts || 0) + ' 处</span>';
      h += '<b>data 占位</b><span>' + (r.analysis.data_values || 0) + ' 个</span>';
      if (s.chart_texts != null) h += '<b>图表文本</b><span>' + s.chart_texts + ' 处</span>';
      if (s.llm) {
        h += '<b>LLM</b><span>missed ' + (s.llm.missed || 0) + ' / wrong ' +
          (s.llm.wrong || 0) + (s.llm.complete ? ' / 完整' : ' / 不完整') + '</span>';
      }
      h += '<b>分析JSON</b><span><a href="' + r.analysis_download + '" target="_blank">下载</a></span>';
    }
    if (r.status && r.status.log_tail) {
      h += '<b>日志尾部</b><span><pre style="max-height:180px">' +
        esc(r.status.log_tail.slice(-1200)) + "</pre></span>";
    }
    h += "</div></div>";
    $("parseResult").innerHTML = h;
    $("parseResult").style.display = "";
  }).catch(function () {});
}

function uploadTemplateLocal() {
  var bridgeId = currentUploadBridge();
  var f = $("tplFile2").files[0];
  if (!bridgeId || !f) { showMsg("请选择本地解析模版文件", "err"); return; }
  var fd = new FormData();
  fd.append("file", f);
  var headers = {};
  var t = token();
  if (t) headers["X-Auth-Token"] = t;
  fetch("/api/bridges/" + bridgeId + "/template", { method: "POST", body: fd, headers: headers })
    .then(function (res) { return res.json(); })
    .then(function (d) {
      if (d.error) throw new Error(d.error);
      showMsg("本地模版已上传：" + d.template, "ok");
      $("parseStatus").innerHTML = '<span class="green">当前模版：' + esc(d.template) + "</span>";
      loadConfig();
    })
    .catch(function (e) { showMsg(String(e), "err"); });
}

function uploadSensorMapDocx() {
  var bridgeId = currentBridgeId();
  var f = $("smapFile").files[0];
  if (!f) { showMsg("请选择测点编号表格文件", "err"); return; }
  var fd = new FormData();
  fd.append("file", f);
  fd.append("mode", $("smapMode").value);
  var headers = {};
  var t = token();
  if (t) headers["X-Auth-Token"] = t;
  $("smapStatus").textContent = "解析中…";
  $("smapLog").textContent = "—";
  fetch("/api/bridges/" + (bridgeId || "chishi") + "/sensor-map-docx", { method: "POST", body: fd, headers: headers })
    .then(function (res) { return res.json(); })
    .then(function (d) {
      if (d.error) {
        showMsg(d.error, "err");
        $("smapStatus").textContent = "失败";
        if (d.log) $("smapLog").textContent = d.log;
        return;
      }
      showMsg("对照表已生成（" + d.mode + "）", "ok");
      $("smapStatus").textContent = "完成";
      $("smapLog").textContent = d.log || "";
    })
    .catch(function (e) { showMsg(String(e), "err"); $("smapStatus").textContent = ""; });
}

function loadScheduler() {
  var bridgeId = currentBridgeId();
  if (!bridgeId) return;
  api("/api/bridges/" + bridgeId + "/scheduler").then(function (s) {
    $("schStatus").innerHTML = s.running
      ? '<span class="yellow">调度器运行中（PID ' + s.pid + '，' + esc(s.started_at || "") + '）</span>'
      : '<span class="muted">调度器未运行</span>';
    $("schMode").value = s.schedule.mode || "quarterly";
    $("schDay").value = s.schedule.day_of_month || 1;
    $("schHour").value = s.schedule.hour || 8;
    $("schMinute").value = s.schedule.minute || 0;
    $("schStart").value = s.schedule.start_date || "";
  }).catch(function (e) { showMsg(String(e), "err"); });
}

function saveSchedule() {
  var bridgeId = currentBridgeId();
  if (!bridgeId) return;
  var body = {
    schedule: {
      mode: $("schMode").value,
      day_of_month: parseInt($("schDay").value, 10) || 1,
      hour: parseInt($("schHour").value, 10) || 8,
      minute: parseInt($("schMinute").value, 10) || 0,
      start_date: $("schStart").value || ""
    }
  };
  api("/api/bridges/" + bridgeId + "/config", { method: "POST", body: JSON.stringify(body) })
    .then(function () { showMsg("调度设置已保存", "ok"); })
    .catch(function (e) { showMsg(String(e), "err"); });
}

function startScheduler() {
  var bridgeId = currentBridgeId();
  if (!bridgeId) return;
  api("/api/bridges/" + bridgeId + "/scheduler/start", { method: "POST", body: "{}" })
    .then(function (r) { showMsg("调度器已启动（PID " + r.pid + "）", "ok"); loadScheduler(); })
    .catch(function (e) { showMsg(String(e), "err"); });
}

function stopScheduler() {
  var bridgeId = currentBridgeId();
  if (!bridgeId) return;
  api("/api/bridges/" + bridgeId + "/scheduler/stop", { method: "POST", body: "{}" })
    .then(function () { showMsg("调度器已停止", "ok"); loadScheduler(); })
    .catch(function (e) { showMsg(String(e), "err"); });
}

function loadLog() {
  var bridgeId = currentBridgeId();
  if (!bridgeId) return;
  api("/api/bridges/" + bridgeId + "/log?name=" + $("logName").value + "&lines=" + $("logLines").value)
    .then(function (r) { $("logBody").textContent = r.log || "（空）"; })
    .catch(function (e) { showMsg(String(e), "err"); });
}

function loadReview() {
  var bridgeId = currentBridgeId();
  if (!bridgeId) return;
  api("/api/bridges/" + bridgeId + "/review").then(function (r) {
    var h = "";
    h += "<p><b>LLM 审查：</b>" +
      (r.llm_available
        ? '<span class="green">已启用（审查时调用大模型）</span>'
        : '<span class="yellow">未启用（未配置 API Key，审查跳过）</span>') + "</p>";
    var lr = r.last_run || {};
    var rep = lr.repair || {};
    h += "<p>最近一次生成：<b>" + esc((lr.period && lr.period.label) || "—") + "</b>　" +
      "自动纠正 <b>" + (rep.auto_fixed || 0) + "</b> 处　" +
      "需人工处理 <b>" + (rep.manual_needed || 0) + "</b> 处　" +
      "大模型审查 " +
      (rep.llm_called ? (rep.llm_ok ? "已调用并返回结果" : "已调用但无返回") : "未调用") + "</p>";

    h += '<h3 style="font-size:14px;margin:8px 0">报告审查（LLM）</h3>';
    var rv = lr.review;
    if (!rv) {
      h += '<p class="muted">暂无（未生成报告或 LLM 未启用）</p>';
    } else {
      var issues = rv.issues || [];
      h += "<p>" + (issues.length
        ? '<span class="yellow">发现 ' + issues.length + " 处问题</span>"
        : '<span class="green">未发现问题</span>') + "</p>";
      if (issues.length) {
        h += '<table><thead><tr><th>类型</th><th>说明</th></tr></thead><tbody>' +
          issues.map(function (i) {
            return "<tr><td><code>" + esc(i.type || "other") + "</code></td><td>" +
              esc(i.detail || "") + "</td></tr>";
          }).join("") + "</tbody></table>";
      }
      if (rv.raw) {
        h += "<details><summary>原始回复</summary><pre>" + esc(rv.raw) + "</pre></details>";
      }
    }

    h += '<h3 style="font-size:14px;margin:8px 0">确定性体检（self_check）</h3>';
    var sc = lr.self_check || [];
    h += "<p>" + (sc.length
      ? '<span class="yellow">发现 ' + sc.length + " 处</span>"
      : '<span class="green">未发现问题</span>') + "</p>";
    if (sc.length) {
      h += '<table><thead><tr><th>类型</th><th>说明</th></tr></thead><tbody>' +
        sc.map(function (i) {
          return "<tr><td><code>" + esc(i.type || "other") + "</code></td><td>" +
            esc(i.detail || "") + "</td></tr>";
        }).join("") + "</tbody></table>";
    }

    if (r.report_reviews && r.report_reviews.length) {
      h += '<h3 style="font-size:14px;margin:8px 0">报告审查文件</h3>';
      h += r.report_reviews.map(function (f) {
        return '<p class="muted">' + esc(f.name) + "　" + esc(f.mtime) + "　" +
          (f.ok ? "无问题" : "发现问题 " + (f.issues || []).length + " 处") +
          '　<a href="/api/bridges/' + bridgeId + '/review-file?name=' +
          encodeURIComponent(f.name) + '">下载 JSON</a></p>';
      }).join("");
    }

    if (r.template_reviews && r.template_reviews.length) {
      h += '<h3 style="font-size:14px;margin:8px 0">模板审查</h3>';
      h += r.template_reviews.map(function (f) {
        var issues = f.issues || [];
        var body = '<p class="muted">' + esc(f.name) + "　" + esc(f.mtime) + "　" +
          (issues.length ? "发现问题 " + issues.length + " 处" : "无问题") +
          '　<a href="/api/bridges/' + bridgeId + '/review-file?name=' +
          encodeURIComponent(f.name) + '">下载 JSON</a></p>';
        if (issues.length) {
          body += '<table><thead><tr><th>类型</th><th>说明</th></tr></thead><tbody>' +
            issues.map(function (i) {
              return "<tr><td><code>" + esc(i.type || "other") + "</code></td><td>" +
                esc(i.detail || "") + "</td></tr>";
            }).join("") + "</tbody></table>";
        }
        return body;
      }).join("");
    }
    $("revBody").innerHTML = h || '<p class="muted">暂无审查数据</p>';
  }).catch(function (e) { showMsg(String(e), "err"); $("revBody").innerHTML = ""; });
}

api("/api/status").then(function (st) {
  $("modeBadge").textContent = "独立桥服务器模式";
  $("authBadge").textContent = st.auth_required ? "已启用令牌鉴权" : "未鉴权（本机）";
  $("tokenInput").value = token();
  loadCurrentBridge();
}).catch(function () { loadCurrentBridge(); });
