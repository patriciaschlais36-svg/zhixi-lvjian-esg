"use strict";

const API_ROOT = "/api/v1";
const TERMINAL_STATES = new Set(["succeeded", "partial", "failed"]);
const STATUS_LABELS = {
  queued: "等待处理",
  running: "分析中",
  succeeded: "分析完成",
  partial: "部分完成",
  failed: "分析失败",
  candidate_found: "已定位",
  no_candidate: "未定位",
  not_applicable: "不适用",
  auto_high: "自动高可信",
  auto_medium: "自动中可信",
  auto_verified_high: "自动高可信",
  auto_verified_medium: "自动中可信",
  needs_review: "建议复核",
  not_verified: "未自动核验",
  unreviewed: "未人工复核",
  accepted: "已通过文件校验",
  review: "待文件复核",
};

const state = {
  summary: null,
  indicators: [],
  companies: [],
  companyPage: 1,
  companyQuery: "",
  companyTotal: 0,
  companyPageSize: 20,
  pollTimer: null,
  activeJob: null,
};

const byId = (id) => document.getElementById(id);
const qsa = (selector, root = document) => [...root.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatNumber(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: digits }).format(Number(value));
}

function formatDate(value) {
  if (!value) return "—";
  let date = new Date(value);
  if (Number.isNaN(date.getTime())) date = new Date(String(value).replace(" ", "T"));
  return Number.isNaN(date.getTime()) ? escapeHtml(value) : date.toLocaleString("zh-CN", { hour12: false });
}

function statusLabel(value) {
  return STATUS_LABELS[value] || value || "—";
}

function statusClass(value) {
  if (["succeeded", "candidate_found", "auto_high", "auto_verified_high", "accepted"].includes(value)) return "is-success";
  if (["partial", "auto_medium", "auto_verified_medium", "running", "queued"].includes(value)) return "is-warning";
  if (["failed", "needs_review", "review"].includes(value)) return "is-danger";
  return "is-muted";
}

function badge(value) {
  return `<span class="status-badge ${statusClass(value)}">${escapeHtml(statusLabel(value))}</span>`;
}

function renderMessage(target, title, detail = "", type = "empty") {
  if (!target) return;
  target.innerHTML = `<div class="state-panel state-${escapeHtml(type)}"><strong>${escapeHtml(title)}</strong>${detail ? `<span>${escapeHtml(detail)}</span>` : ""}</div>`;
}

function notify(message, type = "info") {
  let host = byId("toastHost");
  if (!host) {
    host = document.createElement("div");
    host.id = "toastHost";
    host.className = "toast-host";
    document.body.appendChild(host);
  }
  const item = document.createElement("div");
  item.className = `toast toast-${type}`;
  item.textContent = message;
  host.appendChild(item);
  window.setTimeout(() => item.remove(), 4200);
}

async function api(path, options = {}) {
  const response = await fetch(`${API_ROOT}${path}`, {
    ...options,
    headers: { Accept: "application/json", ...(options.headers || {}) },
  });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    const message = payload?.error?.message || `请求失败（${response.status}）`;
    const error = new Error(message);
    error.code = payload?.error?.code || "HTTP_ERROR";
    error.status = response.status;
    throw error;
  }
  return payload || { data: null, meta: {} };
}

function setView(viewName) {
  qsa("[data-view]").forEach((button) => {
    const active = button.dataset.view === viewName;
    button.classList.toggle("active", active);
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
  });
  qsa("[data-page]").forEach((page) => {
    const active = page.dataset.page === viewName;
    page.hidden = !active;
    page.classList.toggle("active", active);
    page.classList.toggle("is-active", active);
  });
  history.replaceState(null, "", `#${viewName}`);
  if (viewName === "companies") loadCompanies();
  if (viewName === "indicators") renderIndicators();
  if (viewName === "pipeline") loadJobs();
}

function canvasContext(canvas) {
  if (!(canvas instanceof HTMLCanvasElement)) return null;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(280, Math.round(rect.width || canvas.parentElement?.clientWidth || 500));
  const height = Math.max(220, Math.round(rect.height || 260));
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

function drawEmptyChart(canvas, message) {
  const ready = canvasContext(canvas);
  if (!ready) return;
  const { ctx, width, height } = ready;
  ctx.fillStyle = "#f6f8fa";
  ctx.fillRect(0, 0, width, height);
  ctx.fillStyle = "#68737d";
  ctx.font = "14px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(message, width / 2, height / 2);
}

function drawBarChart(canvas, rows, { labelKey, valueKey, color = "#087f5b" } = {}) {
  if (!rows?.length) return drawEmptyChart(canvas, "暂无可视化数据");
  const ready = canvasContext(canvas);
  if (!ready) return;
  const { ctx, width, height } = ready;
  const margin = { top: 22, right: 18, bottom: 44, left: 48 };
  const chartW = width - margin.left - margin.right;
  const chartH = height - margin.top - margin.bottom;
  const maxValue = Math.max(...rows.map((row) => Number(row[valueKey]) || 0), 1);
  const gap = Math.max(5, chartW / Math.max(rows.length, 1) * 0.24);
  const barW = Math.max(8, (chartW - gap * (rows.length + 1)) / rows.length);

  ctx.strokeStyle = "#dbe2e7";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i += 1) {
    const y = margin.top + (chartH * i) / 4;
    ctx.beginPath();
    ctx.moveTo(margin.left, y);
    ctx.lineTo(width - margin.right, y);
    ctx.stroke();
    ctx.fillStyle = "#68737d";
    ctx.font = "11px system-ui, sans-serif";
    ctx.textAlign = "right";
    ctx.fillText(formatNumber(maxValue * (1 - i / 4)), margin.left - 7, y + 4);
  }

  rows.forEach((row, index) => {
    const value = Number(row[valueKey]) || 0;
    const x = margin.left + gap + index * (barW + gap);
    const barH = (value / maxValue) * chartH;
    const y = margin.top + chartH - barH;
    ctx.fillStyle = Array.isArray(color) ? color[index % color.length] : color;
    ctx.fillRect(x, y, barW, barH);
    ctx.fillStyle = "#263238";
    ctx.font = "11px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(String(row[labelKey]), x + barW / 2, height - 21);
    if (rows.length <= 12) ctx.fillText(formatNumber(value), x + barW / 2, Math.max(14, y - 6));
  });
}

function drawDonutChart(canvas, rows, { labelKey, valueKey, colors } = {}) {
  if (!rows?.length || rows.every((row) => !Number(row[valueKey]))) return drawEmptyChart(canvas, "暂无任务状态数据");
  const ready = canvasContext(canvas);
  if (!ready) return;
  const { ctx, width, height } = ready;
  const total = rows.reduce((sum, row) => sum + (Number(row[valueKey]) || 0), 0);
  const radius = Math.min(width * 0.23, height * 0.33);
  const centerX = Math.min(width * 0.35, radius + 28);
  const centerY = height / 2;
  let angle = -Math.PI / 2;
  rows.forEach((row, index) => {
    const portion = (Number(row[valueKey]) || 0) / total;
    ctx.beginPath();
    ctx.strokeStyle = colors[index % colors.length];
    ctx.lineWidth = Math.max(18, radius * 0.32);
    ctx.arc(centerX, centerY, radius, angle, angle + portion * Math.PI * 2);
    ctx.stroke();
    angle += portion * Math.PI * 2;
  });
  ctx.fillStyle = "#172126";
  ctx.textAlign = "center";
  ctx.font = "600 23px system-ui, sans-serif";
  ctx.fillText(formatNumber(total), centerX, centerY + 2);
  ctx.fillStyle = "#68737d";
  ctx.font = "12px system-ui, sans-serif";
  ctx.fillText("任务总量", centerX, centerY + 22);
  const legendX = Math.max(centerX + radius + 45, width * 0.58);
  rows.forEach((row, index) => {
    const y = 42 + index * 28;
    ctx.fillStyle = colors[index % colors.length];
    ctx.fillRect(legendX, y - 9, 10, 10);
    ctx.fillStyle = "#3a454b";
    ctx.textAlign = "left";
    ctx.font = "12px system-ui, sans-serif";
    ctx.fillText(`${statusLabel(row[labelKey])}  ${formatNumber(row[valueKey])}`, legendX + 17, y);
  });
}

function drawLineChart(canvas, points) {
  if (!points?.length) return drawEmptyChart(canvas, "暂无满足可比条件的跨年数据");
  const ready = canvasContext(canvas);
  if (!ready) return;
  const { ctx, width, height } = ready;
  const margin = { top: 30, right: 26, bottom: 44, left: 70 };
  const chartW = width - margin.left - margin.right;
  const chartH = height - margin.top - margin.bottom;
  const values = points.map((point) => Number(point.normalized_value));
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (min === max) { min -= Math.abs(min || 1) * 0.1; max += Math.abs(max || 1) * 0.1; }
  const pad = (max - min) * 0.12;
  min -= pad; max += pad;
  const xAt = (index) => margin.left + (points.length === 1 ? chartW / 2 : (chartW * index) / (points.length - 1));
  const yAt = (value) => margin.top + chartH - ((value - min) / (max - min)) * chartH;

  ctx.strokeStyle = "#dbe2e7";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i += 1) {
    const y = margin.top + (chartH * i) / 4;
    ctx.beginPath(); ctx.moveTo(margin.left, y); ctx.lineTo(width - margin.right, y); ctx.stroke();
    ctx.fillStyle = "#68737d"; ctx.textAlign = "right"; ctx.font = "11px system-ui, sans-serif";
    ctx.fillText(formatNumber(max - ((max - min) * i) / 4, 2), margin.left - 8, y + 4);
  }
  ctx.beginPath();
  points.forEach((point, index) => {
    const x = xAt(index), y = yAt(Number(point.normalized_value));
    if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = "#087f5b"; ctx.lineWidth = 3; ctx.stroke();
  points.forEach((point, index) => {
    const x = xAt(index), y = yAt(Number(point.normalized_value));
    ctx.fillStyle = "#ffffff"; ctx.strokeStyle = "#087f5b"; ctx.lineWidth = 3;
    ctx.beginPath(); ctx.arc(x, y, 5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    ctx.fillStyle = "#263238"; ctx.textAlign = "center"; ctx.font = "12px system-ui, sans-serif";
    ctx.fillText(String(point.report_year), x, height - 19);
    ctx.fillText(formatNumber(point.normalized_value, 2), x, y - 12);
  });
}

async function checkService() {
  const target = byId("serviceStatus");
  try {
    const { data } = await api("/readiness");
    if (target) target.innerHTML = `<span class="service-dot is-online"></span><span>服务就绪</span><small>${formatNumber(data.reports)} 份报告</small>`;
  } catch (error) {
    if (target) target.innerHTML = `<span class="service-dot is-offline"></span><span>服务不可用</span>`;
    notify(error.message, "error");
  }
}

function renderSummaryKpis(summary) {
  const target = byId("summaryKpis");
  if (!target) return;
  const rows = [
    ["企业主体", summary.company_count, "家"],
    ["报告版本", summary.report_count, "份"],
    ["指标目录", summary.indicator_count, "项"],
    ["证据片段", summary.evidence_count, "条"],
  ];
  target.innerHTML = rows.map(([label, value, unit]) => `
    <article class="kpi-item"><span>${escapeHtml(label)}</span><strong>${formatNumber(value)}</strong><small>${escapeHtml(unit)}</small></article>
  `).join("");
}

function renderRecentReports(reports) {
  const target = byId("recentReports");
  if (!target) return;
  if (!reports.length) return renderMessage(target, "暂无报告记录");
  target.innerHTML = `<div class="table-scroll"><table><thead><tr><th>证券代码</th><th>企业</th><th>年度</th><th>报告</th><th>文件校验</th><th></th></tr></thead><tbody>${reports.map((item) => `
    <tr><td class="mono">${escapeHtml(item.stock_code)}</td><td>${escapeHtml(item.current_short_name)}</td><td>${item.report_year}</td><td>${escapeHtml(item.canonical_title)}</td><td>${badge(item.verification_status)}</td><td><a class="text-link" href="${API_ROOT}/reports/${encodeURIComponent(item.report_version_id)}/file" target="_blank" rel="noopener">查看PDF</a></td></tr>
  `).join("")}</tbody></table></div>`;
}

async function loadOverview() {
  const target = byId("summaryKpis");
  if (target) renderMessage(target, "正在载入概览", "", "loading");
  try {
    const [summaryResponse, reportsResponse, indicatorResponse] = await Promise.all([
      api("/summary"), api("/reports?page=1&page_size=8"), api("/indicators"),
    ]);
    state.summary = summaryResponse.data;
    state.indicators = indicatorResponse.data;
    renderSummaryKpis(state.summary);
    renderRecentReports(reportsResponse.data);
    drawBarChart(byId("yearChart"), state.summary.report_years, { labelKey: "year", valueKey: "reports", color: "#087f5b" });
    drawDonutChart(byId("jobChart"), state.summary.job_statuses, { labelKey: "status", valueKey: "count", colors: ["#2f9e75", "#f2b134", "#d9485f", "#4c78a8"] });
    const dimensions = ["E", "S", "G"].map((dimension) => ({
      dimension,
      count: state.indicators.filter((item) => item.dimension === dimension).length,
    }));
    drawBarChart(byId("indicatorChart"), dimensions, { labelKey: "dimension", valueKey: "count", color: ["#2f9e75", "#4c78a8", "#9c6ade"] });
    populateAnalysisControls();
  } catch (error) {
    renderMessage(target, "概览载入失败", error.message, "error");
  }
}

function uploadModeLabel(data) {
  if (!data?.deduplication) return "真实抽取任务";
  const hits = Object.entries(data.deduplication).filter(([, value]) => value).map(([key]) => ({ blob: "文件", report_version: "报告版本", job: "任务" }[key]));
  return hits.length ? `复用${hits.join("、")}` : "新建真实抽取任务";
}

function renderJobProgress(job, uploadData = null) {
  const target = byId("uploadResult");
  if (!target) return;
  const summary = job.result_summary || {};
  target.innerHTML = `
    <section class="job-progress ${statusClass(job.status)}">
      <div class="job-progress-head"><div><span class="eyebrow">任务状态</span><h3>${escapeHtml(statusLabel(job.status))}</h3></div>${badge(job.status)}</div>
      <div class="progress-track"><span style="width:${Math.min(100, Math.max(0, Number(job.progress) || 0))}%"></span></div>
      <div class="job-facts">
        <span><b>${formatNumber(job.progress)}%</b> 完成度</span><span><b>${formatNumber(summary.result_count)}</b> 结果行</span><span><b>${formatNumber(summary.candidate_count)}</b> 候选行</span><span><b>${formatNumber(summary.review_count)}</b> 建议复核</span>
      </div>
      <p>${escapeHtml(job.stage || "等待处理")}</p>
      ${uploadData ? `<p class="subtle">${escapeHtml(uploadModeLabel(uploadData))}；文件指纹 ${escapeHtml(uploadData.sha256?.slice(0, 16) || "—")}…</p>` : ""}
      ${job.error_message ? `<div class="inline-error">${escapeHtml(job.error_message)}</div>` : ""}
      <div id="jobResultTable"></div>
    </section>`;
}

async function renderJobResults(jobId) {
  const target = byId("jobResultTable");
  if (!target) return;
  try {
    const { data } = await api(`/results?job_id=${encodeURIComponent(jobId)}`);
    if (!data.length) return renderMessage(target, "任务已结束，但没有可展示的结果行");
    target.innerHTML = `<div class="table-scroll compact-table"><table><thead><tr><th>维度</th><th>指标</th><th>候选状态</th><th>值</th><th>单位</th><th>自动核验</th><th>证据</th></tr></thead><tbody>${data.slice(0, 120).map((item) => `
      <tr><td>${escapeHtml(item.dimension)}</td><td>${escapeHtml(item.metric_name_cn)}</td><td>${badge(item.candidate_status)}</td><td>${escapeHtml(item.raw_value ?? "—")}</td><td>${escapeHtml(item.unit_normalized || item.unit_raw || "—")}</td><td>${badge(item.verification_status)}</td><td>${formatNumber(item.evidence_count)}</td></tr>
    `).join("")}</tbody></table></div>`;
  } catch (error) {
    renderMessage(target, "结果读取失败", error.message, "error");
  }
}

async function pollJob(jobId, uploadData) {
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  try {
    const { data: job } = await api(`/jobs/${encodeURIComponent(jobId)}`);
    state.activeJob = job;
    renderJobProgress(job, uploadData);
    if (TERMINAL_STATES.has(job.status)) {
      if (["succeeded", "partial"].includes(job.status)) await renderJobResults(jobId);
      await Promise.all([loadOverview(), loadJobs()]);
      return;
    }
    state.pollTimer = window.setTimeout(() => pollJob(jobId, uploadData), 2000);
  } catch (error) {
    renderMessage(byId("uploadResult"), "任务状态读取失败", error.message, "error");
  }
}

async function submitUpload(event) {
  event.preventDefault();
  const file = byId("reportFile")?.files?.[0];
  if (!file) return notify("请选择PDF报告。", "error");
  if (!file.name.toLowerCase().endsWith(".pdf")) return notify("仅支持PDF报告。", "error");
  const form = new FormData();
  form.append("file", file);
  form.append("stock_code", byId("uploadStockCode")?.value.trim() || "");
  form.append("company_name", byId("uploadCompanyName")?.value.trim() || "");
  form.append("report_year", byId("uploadReportYear")?.value || "");
  form.append("report_title", byId("uploadReportTitle")?.value.trim() || "");
  const button = byId("runUploadAnalysis");
  if (button) { button.disabled = true; button.dataset.originalText = button.textContent; button.textContent = "正在登记…"; }
  renderMessage(byId("uploadResult"), "正在校验并登记报告", "通过后将进入真实抽取队列。", "loading");
  try {
    const requestKey = globalThis.crypto?.randomUUID?.() || `web-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const { data } = await api("/reports", { method: "POST", body: form, headers: { "Idempotency-Key": requestKey } });
    const mode = byId("uploadModeLabel");
    if (mode) mode.textContent = uploadModeLabel(data);
    notify("报告已进入真实抽取队列。", "success");
    await pollJob(data.job_id, data);
  } catch (error) {
    renderMessage(byId("uploadResult"), "报告登记失败", error.message, "error");
  } finally {
    if (button) { button.disabled = false; button.textContent = button.dataset.originalText || "提交并分析"; }
  }
}

async function loadCompanies(page = state.companyPage) {
  const target = byId("companyTable");
  if (!target) return;
  renderMessage(target, "正在载入企业库", "", "loading");
  try {
    const query = new URLSearchParams({ q: state.companyQuery, page: String(page), page_size: String(state.companyPageSize) });
    const response = await api(`/companies?${query}`);
    state.companies = response.data;
    state.companyPage = response.meta.page;
    state.companyTotal = response.meta.total;
    if (!response.data.length) return renderMessage(target, "未检索到企业", "请调整证券代码或企业简称。", "empty");
    target.innerHTML = `<div class="table-scroll"><table><thead><tr><th>证券代码</th><th>企业简称</th><th>报告数</th><th>年度范围</th><th>抽取结果</th><th></th></tr></thead><tbody>${response.data.map((item) => `
      <tr><td class="mono">${escapeHtml(item.stock_code)}</td><td>${escapeHtml(item.current_short_name)}</td><td>${formatNumber(item.report_count)}</td><td>${item.first_year || "—"}–${item.latest_year || "—"}</td><td>${formatNumber(item.result_count)}</td><td><button class="table-action" data-company-id="${escapeHtml(item.company_id)}">详情</button></td></tr>
    `).join("")}</tbody></table></div>`;
    qsa("[data-company-id]", target).forEach((button) => button.addEventListener("click", () => loadCompanyDetail(button.dataset.companyId)));
    renderCompanyPager();
  } catch (error) {
    renderMessage(target, "企业库载入失败", error.message, "error");
  }
}

function renderCompanyPager() {
  const target = byId("companyPager");
  if (!target) return;
  const pages = Math.max(1, Math.ceil(state.companyTotal / state.companyPageSize));
  target.innerHTML = `<button type="button" data-page-action="prev" ${state.companyPage <= 1 ? "disabled" : ""}>上一页</button><span>第 ${state.companyPage} / ${pages} 页，共 ${formatNumber(state.companyTotal)} 家</span><button type="button" data-page-action="next" ${state.companyPage >= pages ? "disabled" : ""}>下一页</button>`;
  target.querySelector('[data-page-action="prev"]')?.addEventListener("click", () => loadCompanies(state.companyPage - 1));
  target.querySelector('[data-page-action="next"]')?.addEventListener("click", () => loadCompanies(state.companyPage + 1));
}

async function loadCompanyDetail(companyId) {
  const target = byId("companyDetail");
  if (!target) return;
  renderMessage(target, "正在载入企业详情", "", "loading");
  try {
    const { data } = await api(`/companies/${encodeURIComponent(companyId)}`);
    target.innerHTML = `<div class="detail-head"><div><span class="mono">${escapeHtml(data.stock_code)}</span><h3>${escapeHtml(data.current_short_name)}</h3></div><span>${data.reports.length} 个报告年度</span></div>
      <div class="report-timeline">${data.reports.map((report) => `<article><strong>${report.report_year}</strong><div><b>${escapeHtml(report.canonical_title)}</b><span>${badge(report.verification_status)} · ${formatNumber(report.result_count)} 条结果</span></div><a href="${API_ROOT}/reports/${encodeURIComponent(report.report_version_id)}/file" target="_blank" rel="noopener">PDF</a></article>`).join("") || "<p>暂无报告。</p>"}</div>`;
  } catch (error) {
    renderMessage(target, "企业详情载入失败", error.message, "error");
  }
}

function renderIndicators() {
  const target = byId("indicatorTable");
  if (!target || !state.indicators.length) return;
  const dimension = byId("indicatorDimension")?.value || "";
  const priority = byId("indicatorPriority")?.value || "";
  const rows = state.indicators.filter((item) => (!dimension || item.dimension === dimension) && (!priority || item.extraction_priority === priority));
  if (!rows.length) return renderMessage(target, "当前筛选下没有指标");
  target.innerHTML = `<div class="table-scroll"><table><thead><tr><th>编号</th><th>维度</th><th>指标名称</th><th>类型</th><th>优先级</th><th>标准单位</th><th>定义</th></tr></thead><tbody>${rows.map((item) => `
    <tr><td class="mono">${escapeHtml(item.indicator_id)}</td><td><span class="dimension-mark dimension-${escapeHtml(item.dimension)}">${escapeHtml(item.dimension)}</span></td><td>${escapeHtml(item.metric_name_cn)}</td><td>${item.metric_type === "quantitative" ? "定量" : "定性"}</td><td>${escapeHtml(item.extraction_priority)}</td><td>${escapeHtml(item.unit_normalized || "—")}</td><td class="definition-cell">${escapeHtml(item.definition || "—")}</td></tr>
  `).join("")}</tbody></table></div>`;
}

async function populateAnalysisControls() {
  if (!state.indicators.length) return;
  try {
    const first = await api("/companies?page=1&page_size=100");
    const pageCount = Math.ceil((first.meta.total || first.data.length) / 100);
    const remaining = pageCount > 1
      ? await Promise.all(Array.from({ length: pageCount - 1 }, (_, index) => api(`/companies?page=${index + 2}&page_size=100`)))
      : [];
    const data = [first, ...remaining].flatMap((response) => response.data);
    const companySelect = byId("analysisCompany");
    const compare = byId("compareCompanies");
    const indicatorSelect = byId("analysisIndicator");
    const companyOptions = data.map((item) => `<option value="${escapeHtml(item.company_id)}">${escapeHtml(item.stock_code)} ${escapeHtml(item.current_short_name)}</option>`).join("");
    if (companySelect) companySelect.innerHTML = `<option value="">选择企业</option>${companyOptions}`;
    if (compare) compare.innerHTML = companyOptions;
    const quantitative = state.indicators.filter((item) => item.metric_type === "quantitative");
    if (indicatorSelect) indicatorSelect.innerHTML = `<option value="">选择定量指标</option>${quantitative.map((item) => `<option value="${escapeHtml(item.indicator_id)}">${escapeHtml(item.indicator_id)} ${escapeHtml(item.metric_name_cn)}</option>`).join("")}`;
  } catch (error) {
    notify(`分析筛选项载入失败：${error.message}`, "error");
  }
}

async function loadTrend() {
  const companyId = byId("analysisCompany")?.value;
  const indicatorId = byId("analysisIndicator")?.value;
  const meta = byId("trendMeta");
  if (!companyId || !indicatorId) {
    drawEmptyChart(byId("trendChart"), "请选择企业与定量指标");
    if (meta) meta.textContent = "趋势仅展示至少两个年份且标准化单位一致的数据。";
    return;
  }
  try {
    const { data } = await api(`/trends?company_id=${encodeURIComponent(companyId)}&indicator_id=${encodeURIComponent(indicatorId)}`);
    drawLineChart(byId("trendChart"), data.points);
    if (meta) meta.textContent = data.comparable ? `${data.points.length} 个年度，单位：${data.points[0]?.unit_normalized || "—"}` : data.reason;
  } catch (error) {
    drawEmptyChart(byId("trendChart"), "趋势数据读取失败");
    if (meta) meta.textContent = error.message;
  }
}

async function runCompare() {
  const companyIds = [...(byId("compareCompanies")?.selectedOptions || [])].map((option) => option.value);
  const indicatorId = byId("analysisIndicator")?.value;
  const year = byId("analysisYear")?.value || "2025";
  const target = byId("compareResult");
  if (companyIds.length < 2 || !indicatorId) return renderMessage(target, "请选择至少两家企业和一个定量指标", "", "empty");
  try {
    const query = new URLSearchParams({ indicator_id: indicatorId, year });
    companyIds.forEach((id) => query.append("company_id", id));
    const { data } = await api(`/compare?${query}`);
    if (!data.comparable) return renderMessage(target, "当前选择不可比较", data.reason, "empty");
    target.innerHTML = `<div class="comparison-bars">${data.items.map((item) => `<article><div><strong>${escapeHtml(item.stock_code)} ${escapeHtml(item.current_short_name)}</strong><span>${formatNumber(item.normalized_value, 3)} ${escapeHtml(item.unit_normalized)}</span></div><meter min="0" max="${Math.max(...data.items.map((row) => Number(row.normalized_value) || 0), 1)}" value="${Number(item.normalized_value) || 0}"></meter><small>${statusLabel(item.verification_status)} · 置信度 ${formatNumber(item.confidence, 3)}</small></article>`).join("")}</div><p class="subtle">${escapeHtml(data.comparison_basis)}</p>`;
  } catch (error) {
    renderMessage(target, "企业对比读取失败", error.message, "error");
  }
}

function renderSearchResults(data, target) {
  const groups = [
    ["企业", data.companies || [], (item) => `${item.stock_code} ${item.current_short_name}`],
    ["报告", data.reports || [], (item) => `${item.report_year} ${item.current_short_name} ${item.canonical_title}`],
    ["指标", data.indicators || [], (item) => `${item.indicator_id} ${item.metric_name_cn}`],
    ["证据", data.evidence || [], (item) => `${item.report_year} ${item.current_short_name} · ${item.metric_name_cn}`],
  ];
  if (!groups.some(([, items]) => items.length)) return renderMessage(target, "没有匹配结果");
  target.innerHTML = groups.filter(([, items]) => items.length).map(([title, items, label]) => `<section class="search-group"><h3>${title}<span>${items.length}</span></h3>${items.map((item) => {
    const attrs = item.company_id ? `data-open-company="${escapeHtml(item.company_id)}"` : item.evidence_id ? `data-open-evidence="${escapeHtml(item.evidence_id)}"` : item.report_version_id ? `data-open-report="${escapeHtml(item.report_version_id)}"` : `data-open-indicator="${escapeHtml(item.indicator_id)}"`;
    return `<button type="button" ${attrs}><strong>${escapeHtml(label(item))}</strong>${item.source_text_preview ? `<span>${escapeHtml(item.source_text_preview)}</span>` : ""}</button>`;
  }).join("")}</section>`).join("");
  qsa("[data-open-company]", target).forEach((button) => button.addEventListener("click", () => { setView("companies"); loadCompanyDetail(button.dataset.openCompany); closeGlobalSearch(); }));
  qsa("[data-open-report]", target).forEach((button) => button.addEventListener("click", () => window.open(`${API_ROOT}/reports/${encodeURIComponent(button.dataset.openReport)}/file`, "_blank", "noopener")));
  qsa("[data-open-evidence]", target).forEach((button) => button.addEventListener("click", () => openEvidence(button.dataset.openEvidence)));
  qsa("[data-open-indicator]", target).forEach((button) => button.addEventListener("click", () => { setView("indicators"); const input = byId("indicatorDimension"); if (input) input.value = ""; renderIndicators(); closeGlobalSearch(); }));
}

async function runEvidenceSearch() {
  const query = byId("evidenceSearch")?.value.trim() || "";
  const target = byId("evidenceResults");
  if (!query) return renderMessage(target, "输入关键词检索证据原文", "支持企业、指标和报告正文关键词。", "empty");
  renderMessage(target, "正在检索证据库", "", "loading");
  try {
    const { data } = await api(`/search?q=${encodeURIComponent(query)}&limit=50`);
    renderSearchResults({ companies: [], reports: [], indicators: [], evidence: data.evidence }, target);
  } catch (error) {
    renderMessage(target, "证据检索失败", error.message, "error");
  }
}

async function openEvidence(evidenceId) {
  try {
    const { data } = await api(`/evidence/${encodeURIComponent(evidenceId)}`);
    const target = byId("evidenceResults") || byId("searchResults");
    setView("evidence");
    if (target) target.innerHTML = `<article class="evidence-detail"><header><div><span class="mono">${escapeHtml(data.stock_code)}</span><h3>${escapeHtml(data.current_short_name)} · ${data.report_year}</h3></div><a class="primary-link" href="${escapeHtml(data.pdf_url)}" target="_blank" rel="noopener">定位原文页</a></header><dl><div><dt>指标编号</dt><dd>${escapeHtml(data.indicator_id)}</dd></div><div><dt>物理页码</dt><dd>${formatNumber(data.page_no)}</dd></div><div><dt>报告印刷页码</dt><dd>${escapeHtml(data.printed_page_label || "—")}</dd></div><div><dt>证据类型</dt><dd>${escapeHtml(data.evidence_type)}</dd></div></dl><blockquote>${escapeHtml(data.source_text)}</blockquote><small>文本指纹：${escapeHtml(data.source_text_sha256)}</small></article>`;
    closeGlobalSearch();
  } catch (error) {
    notify(`证据读取失败：${error.message}`, "error");
  }
}

async function loadJobs() {
  const target = byId("jobTable");
  if (!target) return;
  renderMessage(target, "正在载入任务队列", "", "loading");
  try {
    const { data } = await api("/jobs?limit=100");
    if (!data.length) return renderMessage(target, "尚无在线分析任务", "上传报告后将在此显示。", "empty");
    target.innerHTML = `<div class="table-scroll"><table><thead><tr><th>任务编号</th><th>状态</th><th>阶段</th><th>进度</th><th>创建时间</th><th>完成时间</th></tr></thead><tbody>${data.map((job) => `<tr><td class="mono">${escapeHtml(job.job_id.slice(0, 18))}</td><td>${badge(job.status)}</td><td>${escapeHtml(job.stage)}</td><td><div class="mini-progress"><span style="width:${Number(job.progress) || 0}%"></span></div><small>${formatNumber(job.progress)}%</small></td><td>${formatDate(job.created_at)}</td><td>${formatDate(job.finished_at)}</td></tr>`).join("")}</tbody></table></div>`;
  } catch (error) {
    renderMessage(target, "任务队列载入失败", error.message, "error");
  }
}

let globalSearchTimer = null;
async function runGlobalSearch() {
  const input = byId("globalSearch");
  const target = byId("searchResults");
  const query = input?.value.trim() || "";
  if (!target) return;
  if (!query) { target.hidden = true; target.classList.remove("open"); target.innerHTML = ""; return; }
  target.hidden = false;
  target.classList.add("open");
  renderMessage(target, "正在检索", "", "loading");
  try {
    const { data } = await api(`/search?q=${encodeURIComponent(query)}&limit=8`);
    renderSearchResults(data, target);
  } catch (error) {
    renderMessage(target, "检索失败", error.message, "error");
  }
}

function closeGlobalSearch() {
  const target = byId("searchResults");
  const input = byId("globalSearch");
  if (target) { target.hidden = true; target.classList.remove("open"); target.innerHTML = ""; }
  if (input) input.value = "";
}

function bindEvents() {
  qsa("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  byId("uploadForm")?.addEventListener("submit", submitUpload);
  byId("reportFile")?.addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    const label = byId("uploadFileName");
    if (label) label.textContent = file ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} MB` : "尚未选择文件";
  });
  byId("companySearch")?.addEventListener("input", (event) => {
    window.clearTimeout(event.currentTarget._timer);
    event.currentTarget._timer = window.setTimeout(() => { state.companyQuery = event.target.value.trim(); loadCompanies(1); }, 280);
  });
  byId("indicatorDimension")?.addEventListener("change", renderIndicators);
  byId("indicatorPriority")?.addEventListener("change", renderIndicators);
  byId("analysisCompany")?.addEventListener("change", loadTrend);
  byId("analysisIndicator")?.addEventListener("change", loadTrend);
  byId("compareBtn")?.addEventListener("click", runCompare);
  byId("evidenceSearch")?.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); runEvidenceSearch(); } });
  byId("evidenceSearchBtn")?.addEventListener("click", runEvidenceSearch);
  byId("refreshJobs")?.addEventListener("click", loadJobs);
  byId("globalSearch")?.addEventListener("input", () => { window.clearTimeout(globalSearchTimer); globalSearchTimer = window.setTimeout(runGlobalSearch, 250); });
  byId("globalSearch")?.addEventListener("keydown", (event) => { if (event.key === "Escape") closeGlobalSearch(); });
  byId("searchClear")?.addEventListener("click", closeGlobalSearch);
  byId("exportBtn")?.addEventListener("click", () => { window.location.href = `${API_ROOT}/exports/results.csv`; });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".global-search")) {
      byId("searchResults")?.setAttribute("hidden", "");
      byId("searchResults")?.classList.remove("open");
    }
  });
  window.addEventListener("resize", () => {
    window.clearTimeout(window._chartResizeTimer);
    window._chartResizeTimer = window.setTimeout(() => {
      if (state.summary) {
        drawBarChart(byId("yearChart"), state.summary.report_years, { labelKey: "year", valueKey: "reports", color: "#087f5b" });
        drawDonutChart(byId("jobChart"), state.summary.job_statuses, { labelKey: "status", valueKey: "count", colors: ["#2f9e75", "#f2b134", "#d9485f", "#4c78a8"] });
        const dimensions = ["E", "S", "G"].map((dimension) => ({ dimension, count: state.indicators.filter((item) => item.dimension === dimension).length }));
        drawBarChart(byId("indicatorChart"), dimensions, { labelKey: "dimension", valueKey: "count", color: ["#2f9e75", "#4c78a8", "#9c6ade"] });
      }
      loadTrend();
    }, 180);
  });
}

async function initialize() {
  bindEvents();
  const requested = location.hash.slice(1);
  const view = ["overview", "upload", "companies", "indicators", "analysis", "evidence", "pipeline"].includes(requested) ? requested : "overview";
  setView(view);
  await Promise.all([checkService(), loadOverview()]);
  if (view === "companies") await loadCompanies();
  if (view === "pipeline") await loadJobs();
  drawEmptyChart(byId("trendChart"), "请选择企业与定量指标");
  renderMessage(byId("evidenceResults"), "输入关键词检索证据原文", "支持企业、指标和报告正文关键词。", "empty");
}

document.addEventListener("DOMContentLoaded", initialize);
