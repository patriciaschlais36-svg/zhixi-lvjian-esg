"use strict";

const API_ROOT = "/api/v1";
const TERMINAL_STATES = new Set(["succeeded", "partial", "failed"]);
const CHART_COLORS = {
  primary: "#087f5b",
  grid: "#dce6e1",
  text: "#66756f",
  ink: "#102f26",
  dimensions: ["#087f5b", "#28739a", "#8a6231"],
  statuses: ["#087f5b", "#c28a30", "#bd4e55", "#477d94"],
};
const STATUS_LABELS = {
  queued: "等待处理",
  running: "分析中",
  succeeded: "分析完成",
  partial: "部分完成",
  failed: "分析失败",
  candidate_found: "已定位",
  no_candidate: "未定位",
  not_applicable: "不适用",
  auto_high: "机器高等级",
  auto_medium: "机器中等级",
  auto_verified_high: "机器高等级",
  auto_verified_medium: "机器中等级",
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
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "暂无";
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: digits }).format(Number(value));
}

function formatDate(value) {
  if (!value) return "暂无";
  let date = new Date(value);
  if (Number.isNaN(date.getTime())) date = new Date(String(value).replace(" ", "T"));
  return Number.isNaN(date.getTime()) ? escapeHtml(value) : date.toLocaleString("zh-CN", { hour12: false });
}

function statusLabel(value) {
  return STATUS_LABELS[value] || value || "暂无";
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

function reportFileControl(report, label = "PDF") {
  if (!report?.file_available) return '<span class="file-unavailable">原始PDF未挂载</span>';
  return `<a class="text-link" href="${API_ROOT}/reports/${encodeURIComponent(report.report_version_id)}/file" target="_blank" rel="noopener">${escapeHtml(label)}</a>`;
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
  document.body.dataset.activeView = viewName;
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
  const hero = byId("siteHero");
  const appShell = document.querySelector(".app-shell");
  if (appShell && (!hero || window.scrollY >= hero.offsetHeight - 8)) appShell.scrollIntoView({ behavior: "auto", block: "start" });
  if (viewName === "companies") loadCompanies();
  if (viewName === "indicators") renderIndicators();
  if (viewName === "pipeline") loadJobs();
}

function closeSiteHeroMenu() {
  const menu = byId("siteHeroMenu");
  const trigger = byId("siteHeroMenuOpen");
  if (!menu || !trigger) return;
  menu.hidden = true;
  menu.classList.remove("is-open");
  trigger.setAttribute("aria-expanded", "false");
  document.body.classList.remove("site-menu-open");
}

function openSiteHeroMenu() {
  const menu = byId("siteHeroMenu");
  const trigger = byId("siteHeroMenuOpen");
  if (!menu || !trigger) return;
  menu.hidden = false;
  menu.classList.add("is-open");
  trigger.setAttribute("aria-expanded", "true");
  document.body.classList.add("site-menu-open");
}

function enterWorkspace(viewName) {
  setView(viewName);
  closeSiteHeroMenu();
  document.querySelector(".app-shell")?.scrollIntoView({
    behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
    block: "start",
  });
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
  ctx.fillStyle = "#f5f8f6";
  ctx.fillRect(0, 0, width, height);
  ctx.strokeStyle = "#d6e1dc";
  ctx.setLineDash([5, 6]);
  ctx.strokeRect(18.5, 18.5, width - 37, height - 37);
  ctx.setLineDash([]);
  ctx.fillStyle = CHART_COLORS.text;
  ctx.font = "13px 'Segoe UI', 'Microsoft YaHei UI', sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(message, width / 2, height / 2);
}

function roundedRectPath(ctx, x, y, width, height, radius) {
  const r = Math.min(radius, width / 2, height / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + width - r, y);
  ctx.quadraticCurveTo(x + width, y, x + width, y + r);
  ctx.lineTo(x + width, y + height - r);
  ctx.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
  ctx.lineTo(x + r, y + height);
  ctx.quadraticCurveTo(x, y + height, x, y + height - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

function drawBarChart(canvas, rows, { labelKey, valueKey, color = CHART_COLORS.primary } = {}) {
  if (!rows?.length) return drawEmptyChart(canvas, "暂无可视化数据");
  const ready = canvasContext(canvas);
  if (!ready) return;
  const { ctx, width, height } = ready;
  const margin = { top: 34, right: 18, bottom: 44, left: 54 };
  const chartW = width - margin.left - margin.right;
  const chartH = height - margin.top - margin.bottom;
  const maxValue = Math.max(...rows.map((row) => Number(row[valueKey]) || 0), 1);
  const axisMax = maxValue * 1.14;
  const slotW = chartW / rows.length;
  const barW = Math.max(18, Math.min(62, slotW * 0.46));

  ctx.strokeStyle = CHART_COLORS.grid;
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i += 1) {
    const y = margin.top + (chartH * i) / 4;
    ctx.setLineDash(i === 4 ? [] : [3, 5]);
    ctx.beginPath();
    ctx.moveTo(margin.left, y);
    ctx.lineTo(width - margin.right, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = CHART_COLORS.text;
    ctx.font = "10px 'Segoe UI', 'Microsoft YaHei UI', sans-serif";
    ctx.textAlign = "right";
    ctx.fillText(formatNumber(axisMax * (1 - i / 4)), margin.left - 8, y + 4);
  }

  rows.forEach((row, index) => {
    const value = Number(row[valueKey]) || 0;
    const x = margin.left + index * slotW + (slotW - barW) / 2;
    const barH = (value / axisMax) * chartH;
    const y = margin.top + chartH - barH;
    roundedRectPath(ctx, x, y, barW, barH, Math.min(9, barW / 3));
    ctx.fillStyle = Array.isArray(color) ? color[index % color.length] : color;
    ctx.fill();
    ctx.fillStyle = CHART_COLORS.ink;
    ctx.font = "600 11px 'Segoe UI', 'Microsoft YaHei UI', sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(String(row[labelKey]), x + barW / 2, height - 20);
    if (rows.length <= 12) {
      ctx.font = "700 12px 'Segoe UI', 'Microsoft YaHei UI', sans-serif";
      ctx.fillText(formatNumber(value), x + barW / 2, Math.max(17, y - 9));
    }
  });
}

function drawSegmentChart(canvas, rows, { labelKey, valueKey, colors } = {}) {
  if (!rows?.length || rows.every((row) => !Number(row[valueKey]))) return drawEmptyChart(canvas, "暂无指标结构数据");
  const ready = canvasContext(canvas);
  if (!ready) return;
  const { ctx, width, height } = ready;
  const total = rows.reduce((sum, row) => sum + (Number(row[valueKey]) || 0), 0);
  const left = 28;
  const top = Math.max(46, height * 0.28);
  const barWidth = width - left * 2;
  const barHeight = 28;
  let offset = left;

  rows.forEach((row, index) => {
    const value = Number(row[valueKey]) || 0;
    const widthPart = barWidth * value / total;
    ctx.fillStyle = colors[index % colors.length];
    roundedRectPath(ctx, offset, top, widthPart + (index === 0 || index === rows.length - 1 ? 0 : 1), barHeight, index === 0 || index === rows.length - 1 ? 8 : 0);
    ctx.fill();
    offset += widthPart;
  });

  rows.forEach((row, index) => {
    const columnWidth = barWidth / rows.length;
    const x = left + index * columnWidth;
    const value = Number(row[valueKey]) || 0;
    ctx.fillStyle = colors[index % colors.length];
    ctx.fillRect(x, top + 72, 18, 3);
    ctx.fillStyle = CHART_COLORS.text;
    ctx.font = "600 11px 'Microsoft YaHei UI', sans-serif";
    ctx.textAlign = "left";
    ctx.fillText(String(row[labelKey]), x, top + 57);
    ctx.fillStyle = CHART_COLORS.ink;
    ctx.font = "700 24px Bahnschrift, 'Microsoft YaHei UI', sans-serif";
    ctx.fillText(formatNumber(value), x, top + 102);
    ctx.fillStyle = CHART_COLORS.text;
    ctx.font = "10px 'Microsoft YaHei UI', sans-serif";
    ctx.fillText(`${formatNumber(value / total * 100, 1)}%`, x, top + 121);
  });
}

function drawDonutChart(canvas, rows, { labelKey, valueKey, colors } = {}) {
  if (!rows?.length || rows.every((row) => !Number(row[valueKey]))) return drawEmptyChart(canvas, "暂无任务状态数据");
  const ready = canvasContext(canvas);
  if (!ready) return;
  const { ctx, width, height } = ready;
  const total = rows.reduce((sum, row) => sum + (Number(row[valueKey]) || 0), 0);
  const compact = width < 430;
  const radius = Math.min(width * (compact ? 0.18 : 0.21), height * (compact ? 0.24 : 0.31));
  const centerX = compact ? width / 2 : Math.min(width * 0.34, radius + 34);
  const centerY = compact ? height * 0.38 : height / 2;
  const lineWidth = Math.max(16, radius * 0.3);
  ctx.beginPath();
  ctx.strokeStyle = "#e7eeeb";
  ctx.lineWidth = lineWidth;
  ctx.arc(centerX, centerY, radius, 0, Math.PI * 2);
  ctx.stroke();
  let angle = -Math.PI / 2;
  rows.forEach((row, index) => {
    const portion = (Number(row[valueKey]) || 0) / total;
    const sweep = portion * Math.PI * 2;
    const gap = rows.length > 1 ? Math.min(0.035, sweep * 0.15) : 0;
    ctx.beginPath();
    ctx.strokeStyle = colors[index % colors.length];
    ctx.lineWidth = lineWidth;
    ctx.lineCap = "round";
    ctx.arc(centerX, centerY, radius, angle + gap, angle + sweep - gap);
    ctx.stroke();
    angle += sweep;
  });
  ctx.lineCap = "butt";
  ctx.fillStyle = CHART_COLORS.ink;
  ctx.textAlign = "center";
  ctx.font = "700 25px 'Segoe UI', 'Microsoft YaHei UI', sans-serif";
  ctx.fillText(formatNumber(total), centerX, centerY + 1);
  ctx.fillStyle = CHART_COLORS.text;
  ctx.font = "10px 'Segoe UI', 'Microsoft YaHei UI', sans-serif";
  ctx.fillText("任务总量", centerX, centerY + 19);
  const legendX = Math.max(centerX + radius + 40, width * 0.56);
  rows.forEach((row, index) => {
    const x = compact ? 20 + (index % 2) * Math.max(130, width / 2 - 12) : legendX;
    const y = compact ? height - 48 + Math.floor(index / 2) * 22 : 45 + index * 29;
    ctx.fillStyle = colors[index % colors.length];
    ctx.beginPath();
    ctx.arc(x + 5, y - 4, 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = CHART_COLORS.ink;
    ctx.textAlign = "left";
    ctx.font = "11px 'Segoe UI', 'Microsoft YaHei UI', sans-serif";
    ctx.fillText(`${statusLabel(row[labelKey])}  ${formatNumber(row[valueKey])}`, x + 17, y);
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

  ctx.strokeStyle = CHART_COLORS.grid;
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i += 1) {
    const y = margin.top + (chartH * i) / 4;
    ctx.setLineDash(i === 4 ? [] : [3, 5]);
    ctx.beginPath(); ctx.moveTo(margin.left, y); ctx.lineTo(width - margin.right, y); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = CHART_COLORS.text; ctx.textAlign = "right"; ctx.font = "10px 'Segoe UI', 'Microsoft YaHei UI', sans-serif";
    ctx.fillText(formatNumber(max - ((max - min) * i) / 4, 2), margin.left - 8, y + 4);
  }
  ctx.fillStyle = CHART_COLORS.warning || "#8c641f";
  ctx.textAlign = "left";
  ctx.font = "600 10px 'Segoe UI', 'Microsoft YaHei UI', sans-serif";
  ctx.fillText("纵轴按数据区间缩放", margin.left, 15);
  ctx.beginPath();
  points.forEach((point, index) => {
    const x = xAt(index), y = yAt(Number(point.normalized_value));
    if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.lineTo(xAt(points.length - 1), margin.top + chartH);
  ctx.lineTo(xAt(0), margin.top + chartH);
  ctx.closePath();
  ctx.fillStyle = "rgba(10, 124, 92, 0.08)";
  ctx.fill();
  ctx.beginPath();
  points.forEach((point, index) => {
    const x = xAt(index), y = yAt(Number(point.normalized_value));
    if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = CHART_COLORS.primary; ctx.lineWidth = 3; ctx.lineCap = "round"; ctx.lineJoin = "round"; ctx.stroke();
  points.forEach((point, index) => {
    const x = xAt(index), y = yAt(Number(point.normalized_value));
    ctx.fillStyle = "#fffefb"; ctx.strokeStyle = CHART_COLORS.primary; ctx.lineWidth = 3;
    ctx.beginPath(); ctx.arc(x, y, 5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    ctx.fillStyle = CHART_COLORS.ink; ctx.textAlign = "center"; ctx.font = "11px 'Segoe UI', 'Microsoft YaHei UI', sans-serif";
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
    ["企业主体", summary.company_count, "家", "上市公司数据主体"],
    ["报告版本", summary.report_count, "份", "覆盖 2023 至 2025 年"],
    ["指标目录", summary.indicator_count, "项", "环境、社会与治理"],
    ["证据片段", summary.evidence_count, "条", "可回溯来源文本"],
  ];
  target.innerHTML = rows.map(([label, value, unit, foot]) => `
    <article class="kpi-item"><span class="kpi-label">${escapeHtml(label)}</span><div class="kpi-value"><strong>${formatNumber(value)}</strong><small>${escapeHtml(unit)}</small></div><span class="kpi-foot">${escapeHtml(foot)}</span></article>
  `).join("");
}

function renderHeroRuntime(summary) {
  qsa("[data-runtime='reports']").forEach((item) => { item.textContent = `${formatNumber(summary.report_count)} 份`; });
  qsa("[data-runtime='evidence']").forEach((item) => { item.textContent = `${formatNumber(summary.evidence_count)} 条`; });
}

function renderScopeArchitecture(indicators) {
  const target = byId("scopeArchitecture")?.querySelector(".scope-levels");
  if (!target) return;
  const total = indicators.length;
  const p0 = indicators.filter((item) => item.extraction_priority === "P0").length;
  const p0Quant = indicators.filter((item) => item.extraction_priority === "P0" && item.metric_type === "quantitative").length;
  const quantitative = indicators.filter((item) => item.metric_type === "quantitative").length;
  const qualitative = indicators.filter((item) => item.metric_type === "qualitative").length;
  const levels = [
    ["完整指标体系", total, `定量 ${quantitative} / 定性 ${qualitative}`],
    ["P0 默认抽取层", p0, "在线任务优先处理"],
    ["P0 定量验证子集", p0Quant, "固定字段泛化验收"],
  ];
  target.innerHTML = levels.map(([label, value, note], index) => `
    <article style="--scope-ratio:${Math.max(16, value / total * 100)}%"><span>${escapeHtml(label)}</span><strong>${formatNumber(value)}<small> 项</small></strong><p>${escapeHtml(note)}</p>${index < levels.length - 1 ? '<i aria-hidden="true"></i>' : ""}</article>
  `).join("");
}

function renderDimensionLegend(dimensions) {
  const target = byId("dimensionLegend");
  if (!target) return;
  const names = { E: "环境", S: "社会", G: "治理" };
  target.innerHTML = dimensions.map((item) => `
    <article><span class="dimension-mark dimension-${escapeHtml(item.dimension)}">${escapeHtml(item.dimension)}</span><div><strong>${escapeHtml(names[item.dimension])}</strong><small>${formatNumber(item.count)} 项指标</small></div></article>
  `).join("");
}

function renderRecentReports(reports) {
  const target = byId("recentReports");
  if (!target) return;
  if (!reports.length) return renderMessage(target, "暂无报告记录");
  target.innerHTML = `<div class="table-scroll"><table><thead><tr><th>证券代码</th><th>企业</th><th>年度</th><th>报告</th><th>文件校验</th><th></th></tr></thead><tbody>${reports.map((item) => `
    <tr><td class="mono">${escapeHtml(item.stock_code)}</td><td>${escapeHtml(item.current_short_name)}</td><td>${item.report_year}</td><td>${escapeHtml(item.canonical_title)}</td><td>${badge(item.verification_status)}</td><td>${reportFileControl(item, "查看PDF")}</td></tr>
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
    renderHeroRuntime(state.summary);
    renderScopeArchitecture(state.indicators);
    renderRecentReports(reportsResponse.data);
    drawBarChart(byId("yearChart"), state.summary.report_years, { labelKey: "year", valueKey: "reports", color: CHART_COLORS.primary });
    drawDonutChart(byId("jobChart"), state.summary.job_statuses, { labelKey: "status", valueKey: "count", colors: CHART_COLORS.statuses });
    const dimensions = ["E", "S", "G"].map((dimension) => ({
      dimension,
      count: state.indicators.filter((item) => item.dimension === dimension).length,
    }));
    drawSegmentChart(byId("indicatorChart"), dimensions, { labelKey: "dimension", valueKey: "count", colors: CHART_COLORS.dimensions });
    renderDimensionLegend(dimensions);
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
      ${uploadData ? `<p class="subtle">${escapeHtml(uploadModeLabel(uploadData))}；文件指纹 ${escapeHtml(uploadData.sha256?.slice(0, 16) || "暂无")}…</p>` : ""}
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
      <tr><td>${escapeHtml(item.dimension)}</td><td>${escapeHtml(item.metric_name_cn)}</td><td>${badge(item.candidate_status)}</td><td>${escapeHtml(item.raw_value ?? "暂无")}</td><td>${escapeHtml(item.unit_normalized || item.unit_raw || "暂无")}</td><td>${badge(item.verification_status)}</td><td>${formatNumber(item.evidence_count)}</td></tr>
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
  form.append("report_type", byId("uploadReportType")?.value || "ESG");
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
      <tr><td class="mono">${escapeHtml(item.stock_code)}</td><td>${escapeHtml(item.current_short_name)}</td><td>${formatNumber(item.report_count)}</td><td>${item.first_year || "暂无"} 至 ${item.latest_year || "暂无"}</td><td>${formatNumber(item.result_count)}</td><td><button class="table-action" data-company-id="${escapeHtml(item.company_id)}">详情</button></td></tr>
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
      <div class="report-timeline">${data.reports.map((report) => `<article><strong>${report.report_year}</strong><div><b>${escapeHtml(report.canonical_title)}</b><span>${badge(report.verification_status)} · ${formatNumber(report.result_count)} 条结果</span></div>${reportFileControl(report)}</article>`).join("") || "<p>暂无报告。</p>"}</div>`;
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
    <tr><td class="mono">${escapeHtml(item.indicator_id)}</td><td><span class="dimension-mark dimension-${escapeHtml(item.dimension)}">${escapeHtml(item.dimension)}</span></td><td>${escapeHtml(item.metric_name_cn)}</td><td>${item.metric_type === "quantitative" ? "定量" : "定性"}</td><td>${escapeHtml(item.extraction_priority)}</td><td>${escapeHtml(item.unit_normalized || "暂无")}</td><td class="definition-cell">${escapeHtml(item.definition || "暂无")}</td></tr>
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
    if (compare) {
      compare.innerHTML = companyOptions;
      renderCompareSelection();
    }
    const quantitative = state.indicators.filter((item) => item.metric_type === "quantitative");
    if (indicatorSelect) indicatorSelect.innerHTML = `<option value="">选择定量指标</option>${quantitative.map((item) => `<option value="${escapeHtml(item.indicator_id)}">${escapeHtml(item.indicator_id)} ${escapeHtml(item.metric_name_cn)}</option>`).join("")}`;
  } catch (error) {
    notify(`分析筛选项载入失败：${error.message}`, "error");
  }
}

function renderCompareSelection() {
  const select = byId("compareCompanies");
  const target = byId("compareSelectionSummary");
  if (!select || !target) return;
  const selected = [...select.selectedOptions];
  if (!selected.length) {
    target.innerHTML = "<span>尚未选择企业</span>";
    return;
  }
  target.innerHTML = `<strong>已选 ${selected.length} 家</strong><div>${selected.slice(0, 4).map((option) => `<span>${escapeHtml(option.textContent)}</span>`).join("")}${selected.length > 4 ? `<span>+${selected.length - 4}</span>` : ""}</div>`;
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
    if (meta) meta.textContent = data.comparable ? `${data.points.length} 个年度，单位：${data.points[0]?.unit_normalized || "暂无"}` : data.reason;
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
    const maxValue = Math.max(...data.items.map((row) => Number(row.normalized_value) || 0), 1);
    target.innerHTML = `<p class="boundary-note"><strong>仅对比您选择的企业，非行业排名。</strong>机器等级与置信度用于证据质量分层，所有结果均可继续回到原文核验。</p><div class="comparison-bars">${data.items.map((item) => `<article><div><strong>${escapeHtml(item.stock_code)} ${escapeHtml(item.current_short_name)}</strong><span>${formatNumber(item.normalized_value, 3)} ${escapeHtml(item.unit_normalized)}</span></div><div class="comparison-scale" aria-hidden="true"><i style="width:${Math.max(2, (Number(item.normalized_value) || 0) / maxValue * 100)}%"></i></div><small>${statusLabel(item.verification_status)} / 置信度 ${formatNumber(item.confidence, 3)}</small></article>`).join("")}</div><p class="subtle">${escapeHtml(data.comparison_basis)}</p>`;
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
    const unavailableReport = item.report_version_id && item.file_available === false;
    const attrs = item.company_id ? `data-open-company="${escapeHtml(item.company_id)}"` : item.evidence_id ? `data-open-evidence="${escapeHtml(item.evidence_id)}"` : item.report_version_id ? (unavailableReport ? 'disabled aria-disabled="true"' : `data-open-report="${escapeHtml(item.report_version_id)}"`) : `data-open-indicator="${escapeHtml(item.indicator_id)}"`;
    const detail = unavailableReport ? "原始PDF未挂载" : item.source_text_preview || "";
    return `<button type="button" ${attrs}><strong>${escapeHtml(label(item))}</strong>${detail ? `<span>${escapeHtml(detail)}</span>` : ""}</button>`;
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
    if (target) target.innerHTML = `<article class="evidence-detail"><header><div><span class="mono">${escapeHtml(data.stock_code)}</span><h3>${escapeHtml(data.current_short_name)} · ${data.report_year}</h3></div>${data.pdf_available ? `<a class="primary-link" href="${escapeHtml(data.pdf_url)}" target="_blank" rel="noopener">定位原文页</a>` : '<span class="file-unavailable">公开种子未附原始PDF</span>'}</header><dl><div><dt>指标编号</dt><dd>${escapeHtml(data.indicator_id)}</dd></div><div><dt>物理页码</dt><dd>${formatNumber(data.page_no)}</dd></div><div><dt>报告印刷页码</dt><dd>${escapeHtml(data.printed_page_label || "暂无")}</dd></div><div><dt>证据类型</dt><dd>${escapeHtml(data.evidence_type)}</dd></div></dl><blockquote>${escapeHtml(data.source_text)}</blockquote><small>文本指纹：${escapeHtml(data.source_text_sha256)}</small></article>`;
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
  qsa("[data-landing-view]").forEach((button) => button.addEventListener("click", () => enterWorkspace(button.dataset.landingView)));
  byId("siteHeroMenuOpen")?.addEventListener("click", openSiteHeroMenu);
  byId("siteHeroMenuClose")?.addEventListener("click", closeSiteHeroMenu);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeSiteHeroMenu(); });
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
  byId("compareCompanies")?.addEventListener("change", renderCompareSelection);
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
        drawBarChart(byId("yearChart"), state.summary.report_years, { labelKey: "year", valueKey: "reports", color: CHART_COLORS.primary });
        drawDonutChart(byId("jobChart"), state.summary.job_statuses, { labelKey: "status", valueKey: "count", colors: CHART_COLORS.statuses });
        const dimensions = ["E", "S", "G"].map((dimension) => ({ dimension, count: state.indicators.filter((item) => item.dimension === dimension).length }));
        drawSegmentChart(byId("indicatorChart"), dimensions, { labelKey: "dimension", valueKey: "count", colors: CHART_COLORS.dimensions });
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
