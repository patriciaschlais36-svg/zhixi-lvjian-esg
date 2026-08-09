# -*- coding: utf-8 -*-
"""Build a self-contained static ESG extraction dashboard.

The output HTML embeds a compact JSON payload so it can be opened directly from
disk without a web server.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_COMPANY = BASE_DIR / "评估测试" / "esg_disclosure_scoring_v2.24" / "company_esg_disclosure_scores_v1.0.csv"
DEFAULT_DIMENSION = BASE_DIR / "评估测试" / "esg_disclosure_scoring_v2.24" / "company_dimension_scores_v1.0.csv"
DEFAULT_INDICATOR = BASE_DIR / "评估测试" / "esg_disclosure_scoring_v2.24" / "indicator_disclosure_scores_v1.0.csv"
DEFAULT_VERIFIED = BASE_DIR / "评估测试" / "auto_verification_v2.24" / "auto_verified_extraction_results_v1.0.csv"
DEFAULT_ISSUES = BASE_DIR / "评估测试" / "auto_verification_v2.24" / "auto_verification_issue_queue_v1.0.csv"
DEFAULT_OUT_DIR = BASE_DIR / "可视化系统" / "static_esg_dashboard_v2.24"


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def to_float(value: Any) -> float:
    try:
        return float(str(value or "0").replace(",", ""))
    except ValueError:
        return 0.0


def compact_indicator(row: dict[str, str]) -> dict[str, Any]:
    return {
        "sample_id": row.get("sample_id", ""),
        "field_id": row.get("field_id", ""),
        "dimension": row.get("dimension", ""),
        "metric_name_cn": row.get("metric_name_cn", ""),
        "metric_type": row.get("metric_type", ""),
        "priority": row.get("extraction_priority", ""),
        "status": row.get("candidate_status", ""),
        "verify": row.get("auto_verification_status", ""),
        "score": to_float(row.get("indicator_disclosure_score")),
        "value": row.get("value_candidate", ""),
        "unit": row.get("unit_raw_candidate", ""),
        "page": row.get("source_page", ""),
        "evidence": row.get("evidence_type_candidate", ""),
        "issues": row.get("auto_verification_issues", ""),
    }


def compact_issue(row: dict[str, str]) -> dict[str, Any]:
    return {
        "sample_id": row.get("sample_id", ""),
        "short_name": row.get("short_name", ""),
        "field_id": row.get("field_id", ""),
        "metric_name_cn": row.get("metric_name_cn", ""),
        "dimension": row.get("dimension", ""),
        "priority": row.get("extraction_priority", ""),
        "status": row.get("candidate_status", ""),
        "verify": row.get("auto_verification_status", ""),
        "score": to_float(row.get("auto_verification_score")),
        "value": row.get("value_candidate", ""),
        "unit": row.get("unit_raw_candidate", ""),
        "page": row.get("source_page", ""),
        "issues": row.get("auto_verification_issues", ""),
        "text": (row.get("source_text", "") or "")[:700],
    }


def build_payload(company_rows: list[dict[str, str]], dimension_rows: list[dict[str, str]], indicator_rows: list[dict[str, str]], issue_rows: list[dict[str, str]]) -> dict[str, Any]:
    companies = []
    for row in company_rows:
        companies.append(
            {
                "sample_id": row.get("sample_id", ""),
                "stock_code": row.get("stock_code", ""),
                "short_name": row.get("short_name", ""),
                "report_type": row.get("report_type", ""),
                "score": to_float(row.get("esg_disclosure_score")),
                "grade": row.get("esg_disclosure_grade", ""),
                "E": to_float(row.get("E_score")),
                "S": to_float(row.get("S_score")),
                "G": to_float(row.get("G_score")),
                "coverage": to_float(row.get("candidate_coverage")),
                "avg_verify": to_float(row.get("avg_verification_score")),
                "candidate_found": int(to_float(row.get("candidate_found"))),
                "no_candidate": int(to_float(row.get("no_candidate"))),
                "high": int(to_float(row.get("auto_verified_high"))),
                "medium": int(to_float(row.get("auto_verified_medium"))),
                "review": int(to_float(row.get("review_recommended"))),
                "risk": int(to_float(row.get("high_risk_auto_review"))),
                "blocked": int(to_float(row.get("blocked_by_precision_gate"))),
                "missing": int(to_float(row.get("not_extracted_needs_gold_or_recall_check"))),
            }
        )

    by_sample_indicators: dict[str, list[dict[str, Any]]] = {}
    for row in indicator_rows:
        by_sample_indicators.setdefault(row.get("sample_id", ""), []).append(compact_indicator(row))

    dimensions = [
        {
            "sample_id": row.get("sample_id", ""),
            "dimension": row.get("dimension", ""),
            "score": to_float(row.get("dimension_score")),
            "grade": row.get("dimension_grade", ""),
            "coverage": to_float(row.get("candidate_coverage")),
            "avg_verify": to_float(row.get("avg_verification_score")),
        }
        for row in dimension_rows
    ]

    issue_order = {
        "high_risk_auto_review": 0,
        "review_recommended": 1,
        "not_extracted_needs_gold_or_recall_check": 2,
    }
    issues = sorted(
        [compact_issue(row) for row in issue_rows],
        key=lambda row: (issue_order.get(row["verify"], 9), -row["score"], row["sample_id"], row["field_id"]),
    )[:1500]

    scores = [row["score"] for row in companies]
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "company_count": len(companies),
            "score_avg": round(sum(scores) / len(scores), 2) if scores else 0,
            "score_min": min(scores) if scores else 0,
            "score_max": max(scores) if scores else 0,
            "issue_rows_in_dashboard": len(issues),
        },
        "companies": companies,
        "dimensions": dimensions,
        "indicators_by_sample": by_sample_indicators,
        "issues": issues,
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>ESG 抽取核验与披露评分浏览器</title>
<style>
:root {
  --bg: #f6f7f9;
  --text: #18202a;
  --muted: #5d6878;
  --line: #d9dee7;
  --panel: #ffffff;
  --blue: #1f6feb;
  --green: #16833a;
  --red: #b42318;
  --amber: #a15c00;
  --violet: #6f42c1;
}
* { box-sizing: border-box; }
body { margin: 0; font-family: "Segoe UI", "Microsoft YaHei", Arial, sans-serif; background: var(--bg); color: var(--text); }
header { padding: 18px 24px 14px; border-bottom: 1px solid var(--line); background: #ffffff; position: sticky; top: 0; z-index: 5; }
h1 { margin: 0 0 8px; font-size: 22px; font-weight: 650; letter-spacing: 0; }
.sub { color: var(--muted); font-size: 13px; }
.toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-top: 14px; }
input, select, button { height: 34px; border: 1px solid var(--line); background: #fff; color: var(--text); padding: 0 10px; border-radius: 6px; font: inherit; }
button { cursor: pointer; }
button.active { background: var(--blue); color: white; border-color: var(--blue); }
main { padding: 18px 24px 28px; }
.metrics { display: grid; grid-template-columns: repeat(4, minmax(160px, 1fr)); gap: 12px; margin-bottom: 16px; }
.metric { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px; }
.metric b { display: block; font-size: 22px; margin-top: 5px; }
.layout { display: grid; grid-template-columns: minmax(460px, 1.2fr) minmax(360px, .8fr); gap: 16px; align-items: start; }
section { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; margin-bottom: 16px; }
section h2 { margin: 0; padding: 12px 14px; font-size: 15px; border-bottom: 1px solid var(--line); background: #fbfcfe; }
.table-wrap { overflow: auto; max-height: 520px; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th, td { padding: 7px 8px; border-bottom: 1px solid #edf0f4; text-align: left; vertical-align: top; white-space: nowrap; }
th { position: sticky; top: 0; background: #f8fafc; z-index: 1; color: #344054; font-weight: 650; }
tr:hover { background: #f5f9ff; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.grade { font-weight: 700; }
.A { color: var(--green); } .B { color: #287d3c; } .C { color: var(--blue); } .D { color: var(--amber); } .E { color: var(--red); }
.tag { display: inline-block; padding: 2px 6px; border-radius: 999px; border: 1px solid var(--line); background: #fff; font-size: 12px; }
.risk { color: var(--red); font-weight: 650; }
.ok { color: var(--green); font-weight: 650; }
.warn { color: var(--amber); font-weight: 650; }
.detail { padding: 12px 14px; border-bottom: 1px solid var(--line); display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; font-size: 13px; }
.detail div span { display: block; color: var(--muted); font-size: 12px; margin-bottom: 3px; }
.snippet { white-space: normal; line-height: 1.45; color: #344054; max-width: 560px; }
.tabs { display: flex; gap: 6px; padding: 10px 14px; border-bottom: 1px solid var(--line); }
@media (max-width: 1000px) { .layout { grid-template-columns: 1fr; } .metrics { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 620px) { main, header { padding-left: 12px; padding-right: 12px; } .metrics { grid-template-columns: 1fr; } .detail { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>ESG 抽取核验与披露评分浏览器</h1>
  <div class="sub">本页面展示自动抽取、机器核验、披露评分和风险队列；评分不是第三方 ESG 评级，也不是金标准精度。</div>
  <div class="toolbar">
    <input id="q" placeholder="搜索公司/代码/样本" />
    <select id="grade"><option value="">全部等级</option><option>A</option><option>B</option><option>C</option><option>D</option><option>E</option></select>
    <select id="dimension"><option value="">指标维度</option><option>E</option><option>S</option><option>G</option></select>
    <button id="sortScore" class="active">按评分</button>
    <button id="sortRisk">按风险</button>
  </div>
</header>
<main>
  <div class="metrics">
    <div class="metric">公司数<b id="mCompanies"></b></div>
    <div class="metric">平均披露评分<b id="mAvg"></b></div>
    <div class="metric">最高/最低<b id="mRange"></b></div>
    <div class="metric">展示风险行<b id="mIssues"></b></div>
  </div>
  <div class="layout">
    <div>
      <section>
        <h2>公司评分与核验概览</h2>
        <div class="table-wrap"><table id="companyTable"></table></div>
      </section>
      <section>
        <h2>选中公司指标明细</h2>
        <div id="companyDetail" class="detail"></div>
        <div class="tabs">
          <button class="active" data-filter="">全部</button>
          <button data-filter="candidate_found">已抽取</button>
          <button data-filter="no_candidate">未抽取</button>
          <button data-filter="high_risk_auto_review">高风险</button>
        </div>
        <div class="table-wrap"><table id="indicatorTable"></table></div>
      </section>
    </div>
    <div>
      <section>
        <h2>E/S/G 分项对比</h2>
        <div class="table-wrap"><table id="dimensionTable"></table></div>
      </section>
      <section>
        <h2>机器核验风险队列</h2>
        <div class="table-wrap"><table id="issueTable"></table></div>
      </section>
    </div>
  </div>
</main>
<script id="payload" type="application/json">__PAYLOAD__</script>
<script>
const data = JSON.parse(document.getElementById('payload').textContent);
let selected = data.companies[0]?.sample_id || '';
let indicatorFilter = '';
let sortMode = 'score';

const fmt = n => Number(n || 0).toFixed(2);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function setMetrics() {
  document.getElementById('mCompanies').textContent = data.summary.company_count;
  document.getElementById('mAvg').textContent = fmt(data.summary.score_avg);
  document.getElementById('mRange').textContent = `${fmt(data.summary.score_max)} / ${fmt(data.summary.score_min)}`;
  document.getElementById('mIssues').textContent = data.summary.issue_rows_in_dashboard;
}
function companyRows() {
  const q = document.getElementById('q').value.trim().toLowerCase();
  const g = document.getElementById('grade').value;
  let rows = data.companies.filter(r => (!g || r.grade === g) && (!q || `${r.sample_id} ${r.stock_code} ${r.short_name}`.toLowerCase().includes(q)));
  rows.sort((a,b) => sortMode === 'risk' ? (b.risk + b.blocked + b.missing) - (a.risk + a.blocked + a.missing) : b.score - a.score);
  return rows;
}
function renderCompanyTable() {
  const rows = companyRows();
  if (!rows.find(r => r.sample_id === selected) && rows[0]) selected = rows[0].sample_id;
  document.getElementById('companyTable').innerHTML = `
    <thead><tr><th>样本</th><th>公司</th><th class="num">评分</th><th>级</th><th class="num">E</th><th class="num">S</th><th class="num">G</th><th class="num">覆盖</th><th class="num">风险</th></tr></thead>
    <tbody>${rows.map(r => `<tr onclick="selected='${esc(r.sample_id)}'; renderAll();">
      <td>${esc(r.sample_id)}</td><td>${esc(r.short_name)}<br><span class="sub">${esc(r.stock_code)}</span></td>
      <td class="num">${fmt(r.score)}</td><td class="grade ${esc(r.grade)}">${esc(r.grade)}</td>
      <td class="num">${fmt(r.E)}</td><td class="num">${fmt(r.S)}</td><td class="num">${fmt(r.G)}</td>
      <td class="num">${(r.coverage*100).toFixed(1)}%</td><td class="num">${r.risk + r.blocked}</td>
    </tr>`).join('')}</tbody>`;
}
function renderDetail() {
  const c = data.companies.find(r => r.sample_id === selected) || data.companies[0];
  if (!c) return;
  document.getElementById('companyDetail').innerHTML = `
    <div><span>公司</span>${esc(c.sample_id)} ${esc(c.short_name)} (${esc(c.stock_code)})</div>
    <div><span>披露评分</span><b>${fmt(c.score)}</b> <span class="grade ${esc(c.grade)}">${esc(c.grade)}</span></div>
    <div><span>核验分/覆盖率</span>${fmt(c.avg_verify)} / ${(c.coverage*100).toFixed(1)}%</div>
    <div><span>可信候选</span><span class="ok">${c.high + c.medium}</span></div>
    <div><span>复核/高风险</span><span class="warn">${c.review}</span> / <span class="risk">${c.risk}</span></div>
    <div><span>门控/未抽取</span>${c.blocked} / ${c.missing}</div>`;
}
function renderIndicators() {
  const dim = document.getElementById('dimension').value;
  let rows = data.indicators_by_sample[selected] || [];
  rows = rows.filter(r => (!dim || r.dimension === dim) && (!indicatorFilter || r.status === indicatorFilter || r.verify === indicatorFilter));
  rows.sort((a,b) => a.dimension.localeCompare(b.dimension) || a.field_id.localeCompare(b.field_id));
  document.getElementById('indicatorTable').innerHTML = `
    <thead><tr><th>指标</th><th>维度</th><th>状态</th><th class="num">分</th><th>值</th><th>页</th><th>问题</th></tr></thead>
    <tbody>${rows.map(r => `<tr>
      <td>${esc(r.field_id)}<br>${esc(r.metric_name_cn)}</td><td>${esc(r.dimension)}</td>
      <td><span class="tag">${esc(r.verify)}</span><br>${esc(r.status)}</td><td class="num">${fmt(r.score)}</td>
      <td>${esc(r.value)} ${esc(r.unit)}</td><td>${esc(r.page)}</td><td class="snippet">${esc(r.issues)}</td>
    </tr>`).join('')}</tbody>`;
}
function renderDimensions() {
  let rows = data.dimensions.filter(r => r.sample_id === selected);
  document.getElementById('dimensionTable').innerHTML = `
    <thead><tr><th>维度</th><th class="num">评分</th><th>等级</th><th class="num">覆盖</th><th class="num">核验均分</th></tr></thead>
    <tbody>${rows.map(r => `<tr><td>${esc(r.dimension)}</td><td class="num">${fmt(r.score)}</td><td class="grade ${esc(r.grade)}">${esc(r.grade)}</td><td class="num">${(r.coverage*100).toFixed(1)}%</td><td class="num">${fmt(r.avg_verify)}</td></tr>`).join('')}</tbody>`;
}
function renderIssues() {
  const dim = document.getElementById('dimension').value;
  let rows = data.issues.filter(r => (!dim || r.dimension === dim));
  rows = rows.slice(0, 200);
  document.getElementById('issueTable').innerHTML = `
    <thead><tr><th>公司/指标</th><th>核验</th><th>值</th><th>问题与证据</th></tr></thead>
    <tbody>${rows.map(r => `<tr>
      <td>${esc(r.sample_id)} ${esc(r.short_name)}<br>${esc(r.field_id)} ${esc(r.metric_name_cn)}</td>
      <td><span class="${r.verify.includes('risk') ? 'risk' : 'warn'}">${esc(r.verify)}</span><br>${fmt(r.score)}</td>
      <td>${esc(r.value)} ${esc(r.unit)}<br>${esc(r.page)}</td>
      <td class="snippet">${esc(r.issues)}<br>${esc(r.text)}</td>
    </tr>`).join('')}</tbody>`;
}
function renderAll() { renderCompanyTable(); renderDetail(); renderIndicators(); renderDimensions(); renderIssues(); }
document.getElementById('q').addEventListener('input', renderAll);
document.getElementById('grade').addEventListener('change', renderAll);
document.getElementById('dimension').addEventListener('change', renderAll);
document.getElementById('sortScore').onclick = () => { sortMode='score'; document.getElementById('sortScore').classList.add('active'); document.getElementById('sortRisk').classList.remove('active'); renderAll(); };
document.getElementById('sortRisk').onclick = () => { sortMode='risk'; document.getElementById('sortRisk').classList.add('active'); document.getElementById('sortScore').classList.remove('active'); renderAll(); };
document.querySelectorAll('.tabs button').forEach(btn => btn.onclick = () => { document.querySelectorAll('.tabs button').forEach(b => b.classList.remove('active')); btn.classList.add('active'); indicatorFilter = btn.dataset.filter; renderIndicators(); });
setMetrics(); renderAll();
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company-csv", type=Path, default=DEFAULT_COMPANY)
    parser.add_argument("--dimension-csv", type=Path, default=DEFAULT_DIMENSION)
    parser.add_argument("--indicator-csv", type=Path, default=DEFAULT_INDICATOR)
    parser.add_argument("--verified-csv", type=Path, default=DEFAULT_VERIFIED)
    parser.add_argument("--issues-csv", type=Path, default=DEFAULT_ISSUES)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    payload = build_payload(
        load_rows(args.company_csv),
        load_rows(args.dimension_csv),
        load_rows(args.indicator_csv),
        load_rows(args.issues_csv),
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    data_json = args.out_dir / "dashboard_data_v1.0.json"
    html_path = args.out_dir / "ESG自动抽取核验评分展示_v1.0.html"
    summary_path = args.out_dir / "dashboard_summary_v1.0.json"
    write_json(data_json, payload)
    html_payload = html.escape(json.dumps(payload, ensure_ascii=False), quote=False)
    html_path.write_text(HTML_TEMPLATE.replace("__PAYLOAD__", html_payload), encoding="utf-8")
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "html": str(html_path),
        "data_json": str(data_json),
        "company_count": payload["summary"]["company_count"],
        "issue_rows_in_dashboard": payload["summary"]["issue_rows_in_dashboard"],
    }
    write_json(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
