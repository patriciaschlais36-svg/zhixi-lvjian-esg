# -*- coding: utf-8 -*-
"""Build a focused validation queue from high-risk extraction audit issues.

The output CSV is compatible with llm_review_priority_queue_v1.0.py and the
budgeted DeepSeek guard. This script is read-only and does not call any API.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ISSUES = BASE_DIR / "评估测试" / "extraction_output_quality_audit_v2.20" / "extraction_output_quality_issues_v1.0.csv"
DEFAULT_EXTRACTION = (
    BASE_DIR
    / "算法方案"
    / "pilot_full_extraction_v2.15_200samples_pipeline_guarded"
    / "全量指标候选抽取结果_200份v2.20_precision_gated.csv"
)
DEFAULT_INDICATOR = BASE_DIR / "算法源码" / "配置" / "ESG指标体系.csv"
DEFAULT_OUT_DIR = BASE_DIR / "评估测试" / "high_risk_validation_queue_v2.30"

ISSUE_WEIGHTS = {
    "percentage_out_of_range": 98,
    "negative_value_for_nonnegative_metric": 96,
    "currency_unit_for_non_currency_metric": 94,
    "percentage_unit_for_non_percentage_metric": 92,
}


def load_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def make_key(row: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (
        row.get("sample_id", ""),
        row.get("field_id", ""),
        row.get("value_candidate", ""),
        row.get("unit_raw_candidate", ""),
        row.get("source_page", ""),
    )


def extraction_index(rows: list[dict[str, str]]) -> dict[tuple[str, str, str, str, str], dict[str, str]]:
    index: dict[tuple[str, str, str, str, str], dict[str, str]] = {}
    for row in rows:
        key = make_key(row)
        if key not in index:
            index[key] = row
    return index


def indicator_map(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {row.get("field_id", ""): row for row in rows}


def route_for(issues: list[str]) -> str:
    if any(issue in issues for issue in ["currency_unit_for_non_currency_metric", "percentage_unit_for_non_percentage_metric"]):
        return "precision_rule_candidate_then_deepseek"
    if any(issue in issues for issue in ["percentage_out_of_range", "negative_value_for_nonnegative_metric"]):
        return "range_rule_candidate_then_deepseek"
    return "deepseek_priority_review"


def hint_for(issues: list[str], indicator: dict[str, str]) -> str:
    parts: list[str] = []
    expected_unit = indicator.get("unit_normalized", "")
    expected_type = indicator.get("value_type", "")
    if expected_unit:
        parts.append(f"expected_unit={expected_unit}")
    if expected_type:
        parts.append(f"expected_value_type={expected_type}")
    if "currency_unit_for_non_currency_metric" in issues:
        parts.append("候选单位是金额，需确认是否把投入/营收/罚款等金额误当成非金额指标。")
    if "percentage_out_of_range" in issues:
        parts.append("百分比候选超出常规范围，需确认是否误抽年份、人数、金额或原文口径。")
    if "negative_value_for_nonnegative_metric" in issues:
        parts.append("非负指标出现负数，需确认是否为减少量、同比变化、扣减项或 OCR 噪声。")
    if "percentage_unit_for_non_percentage_metric" in issues:
        parts.append("候选单位是百分比，需确认是否把比例误当成数量/金额/总量。")
    return "；".join(parts)


def build_queue(issue_rows: list[dict[str, str]], extraction_rows: list[dict[str, str]], indicators: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    high_issues = [row for row in issue_rows if row.get("severity") == "high"]
    grouped: dict[tuple[str, str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in high_issues:
        grouped[make_key(row)].append(row)

    source_index = extraction_index(extraction_rows)
    queue: list[dict[str, Any]] = []
    for key, rows in sorted(grouped.items()):
        base = source_index.get(key, rows[0])
        issues = sorted({row.get("issue", "") for row in rows if row.get("issue", "")})
        issue_weight = max(ISSUE_WEIGHTS.get(issue, 80) for issue in issues) if issues else 80
        indicator = indicators.get(base.get("field_id", ""), {})
        conf_text = base.get("confidence_rule", "")
        try:
            conf = float(conf_text or 0)
        except ValueError:
            conf = 0.0
        risk_score = min(100, issue_weight + (8 if conf < 0.75 else 0))
        review_reason = "；".join(
            [
                "high_risk_output_quality_audit",
                ",".join(issues),
                hint_for(issues, indicator),
                base.get("review_reason", ""),
            ]
        ).strip("；")
        queue.append(
            {
                "sample_id": base.get("sample_id", ""),
                "stock_code": base.get("stock_code", ""),
                "short_name": base.get("short_name", ""),
                "field_id": base.get("field_id", ""),
                "dimension": base.get("dimension", ""),
                "metric_name_cn": base.get("metric_name_cn", ""),
                "value_candidate": base.get("value_candidate", ""),
                "unit_raw_candidate": base.get("unit_raw_candidate", ""),
                "confidence_rule": base.get("confidence_rule", ""),
                "risk_score": risk_score,
                "evidence_type_candidate": base.get("evidence_type_candidate", ""),
                "value_extraction_method": base.get("value_extraction_method", ""),
                "source_page": base.get("source_page", ""),
                "review_reason": review_reason,
                "source_text": base.get("source_text", "") or base.get("source_table_cell", "") or rows[0].get("source_text_preview", ""),
                "high_risk_issues": ",".join(issues),
                "suggested_route": route_for(issues),
                "expected_unit": indicator.get("unit_normalized", ""),
                "expected_value_type": indicator.get("value_type", ""),
            }
        )
    queue.sort(key=lambda row: (-float(row.get("risk_score") or 0), row.get("sample_id", ""), row.get("field_id", "")))
    return queue


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for idx, row in enumerate(rows, 1):
            payload = {
                "task_id": f"HIGH_RISK_{idx:04d}",
                "sample_id": row.get("sample_id", ""),
                "field_id": row.get("field_id", ""),
                "metric_name_cn": row.get("metric_name_cn", ""),
                "candidate_value": row.get("value_candidate", ""),
                "candidate_unit": row.get("unit_raw_candidate", ""),
                "source_page": row.get("source_page", ""),
                "high_risk_issues": row.get("high_risk_issues", ""),
                "expected_unit": row.get("expected_unit", ""),
                "expected_value_type": row.get("expected_value_type", ""),
                "instruction": "判断候选值、单位、页码和证据是否支持该 ESG 指标；若不支持，请给出 reject/needs_review 原因。",
                "evidence": str(row.get("source_text", ""))[:1800],
            }
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def write_report(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# High 风险候选验证队列 v1.0",
        "",
        f"生成时间：{summary['generated_at']}",
        "",
        "## 总览",
        "",
        f"- high issue 行数：{summary['high_issue_rows']}",
        f"- 去重候选数：{summary['queue_rows']}",
        f"- 样本数：{summary['sample_count']}",
        f"- 字段数：{summary['field_count']}",
        "",
        "## issue 分布",
        "",
        "| issue | count |",
        "|---|---:|",
    ]
    for key, value in sorted(summary["issue_counts"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {key} | {value} |")
    lines.extend(["", "## 路由建议", "", "| route | count |", "|---|---:|"])
    for key, value in sorted(summary["route_counts"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {key} | {value} |")
    lines.extend(["", "## 说明", ""])
    lines.append("- 队列兼容 `llm_review_priority_queue_v1.0.py` 和 `run_deepseek_review_budgeted_v1.0.py`。")
    lines.append("- 本脚本不调用 API，也不改写主结果。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issues-csv", type=Path, default=DEFAULT_ISSUES)
    parser.add_argument("--extraction-csv", type=Path, default=DEFAULT_EXTRACTION)
    parser.add_argument("--indicator-csv", type=Path, default=DEFAULT_INDICATOR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    issue_rows = load_rows(args.issues_csv)
    extraction_rows = load_rows(args.extraction_csv)
    indicators = indicator_map(load_rows(args.indicator_csv))
    queue = build_queue(issue_rows, extraction_rows, indicators)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    queue_csv = args.out_dir / "high_risk_validation_queue_v1.0.csv"
    queue_jsonl = args.out_dir / "high_risk_validation_queue_v1.0.jsonl"
    summary_json = args.out_dir / "high_risk_validation_queue_summary_v1.0.json"
    report_md = args.out_dir / "high_risk_validation_queue_report_v1.0.md"

    fields = [
        "sample_id", "stock_code", "short_name", "field_id", "dimension",
        "metric_name_cn", "value_candidate", "unit_raw_candidate",
        "confidence_rule", "risk_score", "evidence_type_candidate",
        "value_extraction_method", "source_page", "review_reason", "source_text",
        "high_risk_issues", "suggested_route", "expected_unit", "expected_value_type",
    ]
    write_csv(queue_csv, queue, fields)
    write_jsonl(queue_jsonl, queue)
    issue_counts = Counter()
    for row in queue:
        for issue in str(row.get("high_risk_issues", "")).split(","):
            if issue:
                issue_counts[issue] += 1
    route_counts = Counter(row.get("suggested_route", "") for row in queue)
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "issues_csv": str(args.issues_csv),
        "extraction_csv": str(args.extraction_csv),
        "high_issue_rows": sum(1 for row in issue_rows if row.get("severity") == "high"),
        "queue_rows": len(queue),
        "sample_count": len({row.get("sample_id", "") for row in queue}),
        "field_count": len({row.get("field_id", "") for row in queue}),
        "issue_counts": dict(issue_counts),
        "route_counts": dict(route_counts),
        "queue_csv": str(queue_csv),
        "queue_jsonl": str(queue_jsonl),
        "report_md": str(report_md),
    }
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(report_md, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
