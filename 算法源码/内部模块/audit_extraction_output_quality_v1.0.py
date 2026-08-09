# -*- coding: utf-8 -*-
"""Audit extraction CSV structural and evidence quality.

This script is read-only: it does not modify extraction results. It identifies
machine-detectable risks that can be used for auto verification, recall routing,
gold-label prioritization, and regression checks.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_EXTRACTION = (
    BASE_DIR
    / "算法方案"
    / "pilot_full_extraction_v2.15_200samples_pipeline_guarded"
    / "全量指标候选抽取结果_200份v2.20_precision_gated.csv"
)
DEFAULT_INDICATOR = BASE_DIR / "算法源码" / "配置" / "ESG指标体系.csv"
DEFAULT_OUT_DIR = BASE_DIR / "评估测试" / "extraction_output_quality_audit_v2.20"

REQUIRED_COLUMNS = [
    "sample_id",
    "field_id",
    "metric_name_cn",
    "candidate_status",
    "value_candidate",
    "unit_raw_candidate",
    "source_page",
    "source_text",
    "source_table_cell",
    "evidence_type_candidate",
    "confidence_rule",
    "needs_llm_review",
]

CURRENCY_TERMS = ("元", "万元", "亿元", "cny", "rmb", "人民币")
PERCENT_TERMS = ("%", "％", "百分比", "percent")
EMISSION_TERMS = ("co2", "tco2", "吨二氧化碳", "吨co", "二氧化碳")
ENERGY_TERMS = ("kwh", "mwh", "gwh", "千瓦时", "兆瓦时", "吉瓦时", "吨标煤")
COUNT_TERMS = ("人", "人次", "次", "起", "件", "项", "家", "名")
VALID_EVIDENCE_TYPES = {"native_table", "generic_kpi_year_table", "native_text", "ocr_text", "ocr_table", "chart", ""}


def load_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def fieldnames(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f).fieldnames or [])


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_float(value: Any) -> float | None:
    text = str(value or "").replace(",", "").replace("，", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_conf(value: Any) -> float | None:
    try:
        return float(str(value or "").strip())
    except ValueError:
        return None


def has_text(value: Any) -> bool:
    return bool(str(value or "").strip())


def normalize_unit(value: str) -> str:
    return str(value or "").strip().lower().replace("％", "%")


def contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = normalize_unit(text)
    return any(term.lower() in lowered for term in terms)


def unit_category(unit: str, value_type: str = "") -> str:
    lowered = normalize_unit(unit)
    if contains_any(lowered, PERCENT_TERMS) or value_type == "percentage":
        return "percentage"
    if contains_any(lowered, EMISSION_TERMS):
        return "emission"
    if contains_any(lowered, ENERGY_TERMS):
        return "energy"
    if contains_any(lowered, CURRENCY_TERMS):
        return "currency"
    if contains_any(lowered, COUNT_TERMS) or value_type == "integer":
        return "count"
    if not lowered:
        return "blank"
    return "other"


def split_units(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[;；,，、|]+", str(value or "")) if item.strip()]


def unit_explicitly_allowed(unit: str, indicator: dict[str, str]) -> bool:
    raw = normalize_unit(unit)
    if not raw:
        return False
    accepted = split_units(indicator.get("units_accepted_raw", ""))
    accepted.extend(split_units(indicator.get("unit_normalized", "")))
    for token in accepted:
        norm = normalize_unit(token)
        if norm and (raw == norm or raw in norm or norm in raw):
            return True
    return False


def load_indicator_map(path: Path) -> dict[str, dict[str, str]]:
    return {row.get("field_id", ""): row for row in load_rows(path)}


def add_issue(
    issues: list[dict[str, Any]],
    row: dict[str, str],
    severity: str,
    issue: str,
    message: str,
) -> None:
    issues.append(
        {
            "sample_id": row.get("sample_id", ""),
            "stock_code": row.get("stock_code", ""),
            "short_name": row.get("short_name", ""),
            "field_id": row.get("field_id", ""),
            "dimension": row.get("dimension", ""),
            "metric_name_cn": row.get("metric_name_cn", ""),
            "candidate_status": row.get("candidate_status", ""),
            "value_candidate": row.get("value_candidate", ""),
            "unit_raw_candidate": row.get("unit_raw_candidate", ""),
            "source_page": row.get("source_page", ""),
            "confidence_rule": row.get("confidence_rule", ""),
            "evidence_type_candidate": row.get("evidence_type_candidate", ""),
            "severity": severity,
            "issue": issue,
            "message": message,
            "review_hint": row.get("review_reason", ""),
            "source_text_preview": (row.get("source_text", "") or row.get("source_table_cell", ""))[:300],
        }
    )


def valid_page_text(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return bool(re.search(r"\d+", text))


def audit_row(row: dict[str, str], indicator: dict[str, str], issues: list[dict[str, Any]]) -> None:
    status = row.get("candidate_status", "")
    value = row.get("value_candidate", "")
    unit = row.get("unit_raw_candidate", "")
    source_text = row.get("source_text", "")
    source_cell = row.get("source_table_cell", "")
    evidence = row.get("evidence_type_candidate", "")

    if evidence not in VALID_EVIDENCE_TYPES:
        add_issue(issues, row, "warning", "unknown_evidence_type", "证据类型不在已知集合")

    if status == "candidate_found":
        expected_type = indicator.get("value_type", "") or row.get("value_type", "")
        quant_like = row.get("metric_type") == "quantitative" or expected_type in {"number", "integer", "percentage"}
        if quant_like and not has_text(value):
            add_issue(issues, row, "error", "quant_candidate_found_missing_value", "定量 candidate_found 行缺少 value_candidate")
        if not valid_page_text(row.get("source_page", "")):
            add_issue(issues, row, "error", "candidate_found_missing_source_page", "candidate_found 行缺少可解析页码")
        if not has_text(source_text) and not has_text(source_cell):
            add_issue(issues, row, "error", "candidate_found_missing_evidence_text", "candidate_found 行缺少 source_text/source_table_cell")
        conf = parse_conf(row.get("confidence_rule", ""))
        if conf is None:
            add_issue(issues, row, "warning", "missing_confidence_rule", "缺少 confidence_rule")
        elif conf < 0.60:
            add_issue(issues, row, "warning", "very_low_confidence_rule", "confidence_rule 低于 0.60")

        expected_unit = indicator.get("unit_normalized", "") or row.get("unit_standardized_candidate", "")
        expected_cat = unit_category(expected_unit, expected_type)
        raw_cat = unit_category(unit, row.get("value_type", ""))
        raw_unit_allowed = unit_explicitly_allowed(unit, indicator)
        numeric = parse_float(row.get("value_standardized_candidate") or value)

        if raw_cat == "currency" and not raw_unit_allowed and expected_cat not in {"currency", "blank", "other"}:
            add_issue(issues, row, "high", "currency_unit_for_non_currency_metric", "候选单位为金额，但指标期望不是金额")
        if raw_cat == "percentage" and not raw_unit_allowed and expected_cat not in {"percentage", "blank", "other"}:
            add_issue(issues, row, "high", "percentage_unit_for_non_percentage_metric", "候选单位为百分比，但指标期望不是百分比")
        if expected_cat == "currency" and not raw_unit_allowed and raw_cat not in {"currency", "blank", "other"}:
            add_issue(issues, row, "warning", "non_currency_unit_for_currency_metric", "金额型指标候选单位不是金额")
        if expected_cat == "emission" and not raw_unit_allowed and raw_cat == "energy":
            add_issue(issues, row, "high", "energy_unit_for_emission_metric", "排放指标候选单位疑似能源单位")
        if expected_cat == "energy" and not raw_unit_allowed and raw_cat == "emission":
            add_issue(issues, row, "high", "emission_unit_for_energy_metric", "能源指标候选单位疑似排放单位")
        if expected_cat in {"emission", "energy", "count", "percentage"} and raw_cat == "blank" and has_text(value):
            add_issue(issues, row, "warning", "expected_unit_but_raw_unit_blank", "指标期望单位明确，但候选原始单位为空")

        if numeric is not None:
            if numeric < 0 and expected_type in {"number", "integer", "percentage"}:
                add_issue(issues, row, "high", "negative_value_for_nonnegative_metric", "非负指标出现负数")
            if expected_cat == "percentage" and not (0 <= numeric <= 100) and not (0 <= numeric <= 1):
                add_issue(issues, row, "high", "percentage_out_of_range", "百分比指标数值不在 0-100 或 0-1 范围")
    else:
        if has_text(value) and row.get("precision_gate_status") != "blocked":
            add_issue(issues, row, "warning", "no_candidate_has_value_residue", "非 candidate_found 行存在候选值残留")
        if row.get("precision_gate_status") == "blocked" and status != "no_candidate":
            add_issue(issues, row, "warning", "blocked_status_not_no_candidate", "precision gate blocked 行 candidate_status 非 no_candidate")


def audit(rows: list[dict[str, str]], fieldnames_: list[str], indicators: dict[str, dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    missing_columns = [col for col in REQUIRED_COLUMNS if col not in fieldnames_]
    for col in missing_columns:
        issues.append(
            {
                "sample_id": "",
                "stock_code": "",
                "short_name": "",
                "field_id": "",
                "dimension": "",
                "metric_name_cn": "",
                "candidate_status": "",
                "value_candidate": "",
                "unit_raw_candidate": "",
                "source_page": "",
                "confidence_rule": "",
                "evidence_type_candidate": "",
                "severity": "error",
                "issue": "missing_required_column",
                "message": f"缺少必需列：{col}",
                "review_hint": "",
                "source_text_preview": "",
            }
        )

    key_counts: Counter[tuple[str, str]] = Counter((row.get("sample_id", ""), row.get("field_id", "")) for row in rows)
    for row in rows:
        key = (row.get("sample_id", ""), row.get("field_id", ""))
        if key_counts[key] > 1:
            add_issue(issues, row, "warning", "duplicate_sample_field_rows", "同一 sample_id + field_id 出现多行")
        indicator = indicators.get(row.get("field_id", ""), {})
        if not indicator:
            add_issue(issues, row, "error", "field_id_not_in_indicator_schema", "field_id 未在指标体系中找到")
        audit_row(row, indicator, issues)

    issue_counts = Counter(issue["issue"] for issue in issues)
    severity_counts = Counter(issue["severity"] for issue in issues)
    by_field = Counter(issue["field_id"] for issue in issues if issue.get("field_id"))
    by_sample = Counter(issue["sample_id"] for issue in issues if issue.get("sample_id"))
    by_status = Counter(row.get("candidate_status", "") or "blank" for row in rows)
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "row_count": len(rows),
        "sample_count": len({row.get("sample_id", "") for row in rows}),
        "field_count": len({row.get("field_id", "") for row in rows}),
        "missing_required_columns": missing_columns,
        "candidate_status_counts": dict(by_status),
        "issue_rows": len(issues),
        "severity_counts": dict(severity_counts),
        "issue_counts": dict(issue_counts),
        "top_issue_fields": dict(by_field.most_common(20)),
        "top_issue_samples": dict(by_sample.most_common(20)),
        "blocking_schema_ok": not missing_columns,
    }
    return issues, summary


def write_report(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# 抽取结果质量审计报告 v1.0",
        "",
        f"生成时间：{summary['generated_at']}",
        "",
        "## 总览",
        "",
        f"- 行数：{summary['row_count']}",
        f"- 样本数：{summary['sample_count']}",
        f"- 字段数：{summary['field_count']}",
        f"- 问题记录数：{summary['issue_rows']}",
        f"- Schema 是否完整：{summary['blocking_schema_ok']}",
        "",
        "## candidate_status 分布",
        "",
        "| status | count |",
        "|---|---:|",
    ]
    for key, value in sorted(summary["candidate_status_counts"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {key} | {value} |")
    lines.extend(["", "## 严重度", "", "| severity | count |", "|---|---:|"])
    for key, value in sorted(summary["severity_counts"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {key} | {value} |")
    lines.extend(["", "## 问题类型", "", "| issue | count |", "|---|---:|"])
    for key, value in sorted(summary["issue_counts"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {key} | {value} |")
    lines.extend(["", "## Top 字段", "", "| field_id | issue_count |", "|---|---:|"])
    for key, value in summary["top_issue_fields"].items():
        lines.append(f"| {key} | {value} |")
    lines.extend(["", "## 说明", ""])
    lines.append("- 本报告只做机器可发现风险审计，不修改 v2.20 主结果。")
    lines.append("- high/error 问题应优先进入规则泛化、DeepSeek 复核或金标验证队列。")
    lines.append("- 问题数量不是错误率，真实精度仍需金标评估。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--extraction-csv", type=Path, default=DEFAULT_EXTRACTION)
    parser.add_argument("--indicator-csv", type=Path, default=DEFAULT_INDICATOR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    rows = load_rows(args.extraction_csv)
    columns = fieldnames(args.extraction_csv)
    indicators = load_indicator_map(args.indicator_csv)
    issues, summary = audit(rows, columns, indicators)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    issues_csv = args.out_dir / "extraction_output_quality_issues_v1.0.csv"
    summary_json = args.out_dir / "extraction_output_quality_summary_v1.0.json"
    report_md = args.out_dir / "extraction_output_quality_report_v1.0.md"
    issue_fields = [
        "sample_id",
        "stock_code",
        "short_name",
        "field_id",
        "dimension",
        "metric_name_cn",
        "candidate_status",
        "value_candidate",
        "unit_raw_candidate",
        "source_page",
        "confidence_rule",
        "evidence_type_candidate",
        "severity",
        "issue",
        "message",
        "review_hint",
        "source_text_preview",
    ]
    write_csv(issues_csv, issues, issue_fields)
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(report_md, summary)
    print(json.dumps({
        "extraction_csv": str(args.extraction_csv),
        "out_dir": str(args.out_dir),
        "row_count": summary["row_count"],
        "issue_rows": summary["issue_rows"],
        "severity_counts": summary["severity_counts"],
        "report_md": str(report_md),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
