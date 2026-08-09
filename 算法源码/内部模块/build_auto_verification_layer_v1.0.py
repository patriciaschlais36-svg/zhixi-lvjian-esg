# -*- coding: utf-8 -*-
"""Build an automatic verification layer for ESG extraction candidates.

This script is deliberately conservative: it does not claim true accuracy and it
does not change extraction results. It adds machine-checkable verification
signals that can drive review queues, scoring, reporting, and dashboards.
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
DEFAULT_DIAGNOSIS = BASE_DIR / "评估测试" / "candidate_quality_v2.20_200samples_precision_gated" / "低覆盖样本诊断_v1.1.csv"
DEFAULT_RULE_FLAGS = (
    BASE_DIR
    / "评估测试"
    / "generalized_precision_gate_v2.22_flag_audit"
    / "generalized_precision_gate_conservative_candidates_v1.0.csv"
)
DEFAULT_QUAL_RULES = BASE_DIR / "算法源码" / "配置" / "定性指标披露规则.csv"
DEFAULT_OUT_DIR = BASE_DIR / "评估测试" / "auto_verification_v2.24"


def load_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
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


def norm_key(row: dict[str, str]) -> tuple[str, str]:
    return row.get("sample_id", ""), row.get("field_id", "")


def load_indicator_map(path: Path) -> dict[str, dict[str, str]]:
    return {row.get("field_id", ""): row for row in load_rows(path)}


def load_diagnosis_map(path: Path) -> dict[str, dict[str, str]]:
    return {row.get("sample_id", ""): row for row in load_rows(path)}


def load_rule_flag_map(path: Path) -> dict[tuple[str, str], list[dict[str, str]]]:
    out: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in load_rows(path):
        out[norm_key(row)].append(row)
    return out


def normalize_number_key(value: Any) -> str:
    parsed = parse_float(value)
    if parsed is None:
        return str(value or "").strip()
    return f"{parsed:.10g}"


def load_year_audit_map(path: Path | None) -> dict[tuple[str, str, str], list[dict[str, str]]]:
    out: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    if not path:
        return out
    for row in load_rows(path):
        key = (
            row.get("sample_id", ""),
            row.get("field_id", ""),
            normalize_number_key(row.get("value_candidate", "")),
        )
        out[key].append(row)
    return out


def load_qualitative_rule_map(path: Path) -> dict[str, dict[str, str]]:
    return {row.get("field_id", ""): row for row in load_rows(path)}


def evidence_weight(evidence_type: str) -> int:
    return {
        "native_table": 16,
        "generic_kpi_year_table": 16,
        "native_text": 12,
        "ocr_text": 8,
        "chart": 6,
    }.get(evidence_type or "", 4)


def source_pages(row: dict[str, str]) -> list[str]:
    text = row.get("source_page", "")
    return [item.strip() for item in re.split(r"[;,，、\s]+", text) if item.strip()]


def qualitative_coverage_level(row: dict[str, str], indicator: dict[str, str], qual_rule: dict[str, str]) -> str:
    if row.get("candidate_status") != "candidate_found":
        return "report_level_check_required"
    if not qual_rule:
        return ""
    text = (row.get("source_text", "") or row.get("source_table_cell", "") or "").strip()
    pages = source_pages(row)
    if not row.get("source_page", "") or not text:
        return "missing_provenance"
    lowered = text.lower()
    if "gri" in lowered and ("索引" in text or "index" in lowered) and len(text) < 120:
        return "index_reference_only"
    if len(text) < 35:
        return "too_short_single_snippet"
    if len(pages) >= 2:
        return "multi_page_evidence"
    if row.get("evidence_type_candidate") in {"native_table", "generic_kpi_year_table"} and indicator.get("metric_type") == "quantitative":
        return "table_cell_grounded"
    return "single_snippet_evidence"


def qualitative_rule_issues(row: dict[str, str], qual_rule: dict[str, str], coverage_level: str) -> list[str]:
    if not qual_rule:
        return []
    issues: list[str] = []
    if coverage_level == "index_reference_only":
        issues.append("qualitative_index_reference_only")
    elif coverage_level == "too_short_single_snippet":
        issues.append("qualitative_evidence_too_short")
    elif coverage_level == "single_snippet_evidence":
        issues.append("qualitative_single_snippet_needs_scope_check")
    elif coverage_level == "report_level_check_required" and row.get("candidate_status") != "candidate_found":
        issues.append("qualitative_report_level_check_required")
    return issues


def priority_weight(priority: str) -> int:
    return {"P0": 10, "P1": 6, "P2": 3}.get(priority or "", 4)


def parse_range(valid_range: str) -> tuple[float | None, float | None]:
    text = str(valid_range or "").strip()
    if not text:
        return None, None
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", text)]
    if ">=0" in text or "≥0" in text:
        return 0.0, None
    if len(nums) >= 2:
        return nums[0], nums[1]
    if len(nums) == 1 and ("<=" in text or "≤" in text):
        return None, nums[0]
    return None, None


def range_issues(row: dict[str, str], indicator: dict[str, str]) -> list[str]:
    issues: list[str] = []
    if row.get("candidate_status") != "candidate_found":
        return issues
    value = parse_float(row.get("value_standardized_candidate") or row.get("value_candidate"))
    if value is None:
        return issues
    metric_type = indicator.get("value_type", "") or row.get("value_type", "")
    unit = (row.get("unit_standardized_candidate") or indicator.get("unit_normalized", "") or "").replace("％", "%")
    if value < 0 and metric_type in {"number", "integer", "percentage"}:
        issues.append("negative_value_for_nonnegative_metric")
    if (metric_type == "percentage" or unit == "%") and not (0 <= value <= 100):
        # The evaluator accepts 0-1 vs 0-100, so do not flag 0-1.
        if not (0 <= value <= 1):
            issues.append("percentage_out_of_0_100")
    lo, hi = parse_range(indicator.get("valid_range", ""))
    if lo is not None and value < lo:
        issues.append("below_valid_range")
    if hi is not None and value > hi:
        issues.append("above_valid_range")
    return issues


def build_issue_list(
    row: dict[str, str],
    indicator: dict[str, str],
    diagnosis: dict[str, str],
    rule_flags: list[dict[str, str]],
    qual_rule: dict[str, str],
    year_audit_rows: list[dict[str, str]],
) -> list[str]:
    issues: list[str] = []
    status = row.get("candidate_status", "")
    source_text = row.get("source_text", "")
    if status == "candidate_found":
        if not row.get("source_page", ""):
            issues.append("missing_source_page")
        if not source_text and not row.get("source_table_cell", ""):
            issues.append("missing_source_text_or_cell")
        conf = parse_float(row.get("confidence_rule"))
        if conf is not None and conf < 0.70:
            issues.append("low_rule_confidence")
        if row.get("evidence_type_candidate") == "ocr_text":
            issues.append("ocr_text_evidence")
        if rule_flags:
            issues.append("generalized_precision_rule_flag")
        if year_audit_rows:
            issues.append("quantitative_year_column_mismatch_possible")
        issues.extend(range_issues(row, indicator))
        if row.get("needs_llm_review", "").lower() == "yes":
            issues.append("needs_llm_review")
    else:
        if row.get("precision_gate_status") == "blocked":
            issues.append("blocked_by_precision_gate")
        else:
            issues.append("no_candidate")

    if diagnosis.get("diagnosis") == "text_rich_low_coverage":
        issues.append("sample_text_rich_low_coverage")
    elif diagnosis.get("diagnosis"):
        issues.append("sample_" + diagnosis.get("diagnosis", ""))
    coverage_level = qualitative_coverage_level(row, indicator, qual_rule)
    issues.extend(qualitative_rule_issues(row, qual_rule, coverage_level))
    return list(dict.fromkeys(issues))


def verification_score(
    row: dict[str, str],
    indicator: dict[str, str],
    issues: list[str],
    rule_flags: list[dict[str, str]],
) -> int:
    if row.get("candidate_status") != "candidate_found":
        if "blocked_by_precision_gate" in issues:
            return 70
        return 35

    conf = parse_float(row.get("confidence_rule"))
    conf_points = int(round((conf if conf is not None else 0.55) * 45))
    score = 25 + conf_points + evidence_weight(row.get("evidence_type_candidate", "")) + priority_weight(indicator.get("extraction_priority", row.get("extraction_priority", "")))

    penalties = {
        "missing_source_page": 18,
        "missing_source_text_or_cell": 18,
        "low_rule_confidence": 12,
        "ocr_text_evidence": 5,
        "generalized_precision_rule_flag": 22,
        "percentage_out_of_0_100": 20,
        "negative_value_for_nonnegative_metric": 20,
        "below_valid_range": 14,
        "above_valid_range": 14,
        "sample_text_rich_low_coverage": 8,
        "needs_llm_review": 4,
        "qualitative_index_reference_only": 24,
        "qualitative_evidence_too_short": 18,
        "qualitative_single_snippet_needs_scope_check": 8,
        "qualitative_report_level_check_required": 6,
        "quantitative_year_column_mismatch_possible": 28,
    }
    for issue in issues:
        score -= penalties.get(issue, 0)
    if any(flag.get("severity") == "high" for flag in rule_flags):
        score -= 10
    return max(0, min(100, score))


def verification_status(row: dict[str, str], issues: list[str], score: int) -> str:
    if row.get("candidate_status") != "candidate_found":
        if "blocked_by_precision_gate" in issues:
            return "blocked_by_precision_gate"
        return "not_extracted_needs_gold_or_recall_check"
    if "generalized_precision_rule_flag" in issues or "quantitative_year_column_mismatch_possible" in issues or any(issue.endswith("_out_of_0_100") for issue in issues):
        return "high_risk_auto_review"
    if "qualitative_index_reference_only" in issues or "qualitative_evidence_too_short" in issues:
        return "high_risk_auto_review"
    if score >= 86 and not any(issue in issues for issue in ["ocr_text_evidence", "sample_text_rich_low_coverage"]):
        return "auto_verified_high"
    if score >= 72:
        return "auto_verified_medium"
    return "review_recommended"


def build_verified_rows(
    extraction_rows: list[dict[str, str]],
    indicators: dict[str, dict[str, str]],
    diagnosis_map: dict[str, dict[str, str]],
    rule_map: dict[tuple[str, str], list[dict[str, str]]],
    qualitative_rules: dict[str, dict[str, str]],
    year_audit_map: dict[tuple[str, str, str], list[dict[str, str]]],
) -> list[dict[str, Any]]:
    verified: list[dict[str, Any]] = []
    for row in extraction_rows:
        indicator = indicators.get(row.get("field_id", ""), {})
        diagnosis = diagnosis_map.get(row.get("sample_id", ""), {})
        rule_flags = rule_map.get(norm_key(row), [])
        qual_rule = qualitative_rules.get(row.get("field_id", ""), {})
        year_rows = year_audit_map.get(
            (row.get("sample_id", ""), row.get("field_id", ""), normalize_number_key(row.get("value_candidate", ""))),
            [],
        )
        year_row = year_rows[0] if year_rows else {}
        coverage_level = qualitative_coverage_level(row, indicator, qual_rule)
        issues = build_issue_list(row, indicator, diagnosis, rule_flags, qual_rule, year_rows)
        score = verification_score(row, indicator, issues, rule_flags)
        rules = sorted({flag.get("rule_id", "") for flag in rule_flags if flag.get("rule_id", "")})
        categories = sorted({flag.get("negative_category", "") for flag in rule_flags if flag.get("negative_category", "")})
        out = dict(row)
        out.update(
            {
                "auto_verification_status": verification_status(row, issues, score),
                "auto_verification_score": score,
                "auto_verification_issues": ";".join(issues),
                "auto_verification_rule_ids": ";".join(rules),
                "auto_verification_rule_categories": ";".join(categories),
                "evidence_coverage_level": coverage_level,
                "qualitative_rule_version": qual_rule.get("rule_version", ""),
                "qualitative_minimum_acceptance": qual_rule.get("minimum_acceptance", ""),
                "qualitative_reject_if_only": qual_rule.get("reject_if_only", ""),
                "sample_quality_diagnosis": diagnosis.get("diagnosis", ""),
                "sample_recommended_action": diagnosis.get("recommended_action", ""),
                "year_alignment_candidate_year": year_row.get("candidate_value_year", ""),
                "year_alignment_target_value": year_row.get("target_year_value", ""),
                "year_alignment_suggested_value": year_row.get("suggested_value", ""),
                "year_alignment_parsed_values": year_row.get("parsed_row_values_by_year", ""),
                "year_alignment_matched_metric_term": year_row.get("matched_metric_term", ""),
                "verification_layer_version": "auto_verification_v1.1",
            }
        )
        verified.append(out)
    return verified


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = Counter(row["auto_verification_status"] for row in rows)
    issue_counts: Counter[str] = Counter()
    for row in rows:
        for issue in str(row.get("auto_verification_issues", "")).split(";"):
            if issue:
                issue_counts[issue] += 1
    by_dimension: dict[str, Counter[str]] = defaultdict(Counter)
    coverage_counts = Counter(row.get("evidence_coverage_level", "") for row in rows if row.get("evidence_coverage_level", ""))
    for row in rows:
        by_dimension[row.get("dimension", "")][row["auto_verification_status"]] += 1
    scores = [int(row["auto_verification_score"]) for row in rows if row.get("candidate_status") == "candidate_found"]
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "row_count": len(rows),
        "candidate_found": sum(1 for row in rows if row.get("candidate_status") == "candidate_found"),
        "no_candidate": sum(1 for row in rows if row.get("candidate_status") != "candidate_found"),
        "status_counts": dict(status_counts),
        "issue_counts": dict(issue_counts.most_common(30)),
        "evidence_coverage_counts": dict(coverage_counts),
        "by_dimension": {dim: dict(counter) for dim, counter in sorted(by_dimension.items())},
        "candidate_score_avg": round(sum(scores) / len(scores), 2) if scores else 0,
        "candidate_score_min": min(scores) if scores else 0,
        "candidate_score_max": max(scores) if scores else 0,
    }


def write_report(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# 自动核验层报告 v1.0",
        "",
        f"生成时间：{summary['generated_at']}",
        "",
        "## 说明",
        "",
        "- 自动核验层不等同于人工金标精度，不用于宣称真实 accuracy。",
        "- 它用于生产流程中的机器审计、风险分层、评分折扣、展示和复核排序。",
        "- 人工金标完成后，可用金标反向校准阈值和规则权重。",
        "",
        "## 总览",
        "",
        f"- 总行数：{summary['row_count']}",
        f"- candidate_found：{summary['candidate_found']}",
        f"- no_candidate/blocked：{summary['no_candidate']}",
        f"- 候选平均核验分：{summary['candidate_score_avg']}",
        "",
        "## 自动核验状态",
        "",
        "| status | count |",
        "|---|---:|",
    ]
    for key, count in summary["status_counts"].items():
        lines.append(f"| {key} | {count} |")
    lines.extend(["", "## 主要问题码", "", "| issue | count |", "|---|---:|"])
    for key, count in summary["issue_counts"].items():
        lines.append(f"| {key} | {count} |")
    lines.extend(["", "## 证据覆盖等级", "", "| coverage_level | count |", "|---|---:|"])
    for key, count in summary.get("evidence_coverage_counts", {}).items():
        lines.append(f"| {key} | {count} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--extraction-csv", type=Path, default=DEFAULT_EXTRACTION)
    parser.add_argument("--indicator-csv", type=Path, default=DEFAULT_INDICATOR)
    parser.add_argument("--diagnosis-csv", type=Path, default=DEFAULT_DIAGNOSIS)
    parser.add_argument("--rule-flags-csv", type=Path, default=DEFAULT_RULE_FLAGS)
    parser.add_argument("--qualitative-rules-csv", type=Path, default=DEFAULT_QUAL_RULES)
    parser.add_argument("--year-audit-csv", type=Path)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    verified = build_verified_rows(
        load_rows(args.extraction_csv),
        load_indicator_map(args.indicator_csv),
        load_diagnosis_map(args.diagnosis_csv),
        load_rule_flag_map(args.rule_flags_csv),
        load_qualitative_rule_map(args.qualitative_rules_csv),
        load_year_audit_map(args.year_audit_csv),
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    verified_csv = args.out_dir / "auto_verified_extraction_results_v1.0.csv"
    issue_csv = args.out_dir / "auto_verification_issue_queue_v1.0.csv"
    summary_json = args.out_dir / "auto_verification_summary_v1.0.json"
    report_md = args.out_dir / "auto_verification_report_v1.0.md"

    fieldnames = list(verified[0].keys()) if verified else []
    write_csv(verified_csv, verified, fieldnames)
    issue_rows = [
        row
        for row in verified
        if row.get("auto_verification_status") in {"high_risk_auto_review", "review_recommended", "not_extracted_needs_gold_or_recall_check"}
    ]
    issue_fields = [
        "sample_id",
        "stock_code",
        "short_name",
        "field_id",
        "metric_name_cn",
        "dimension",
        "metric_type",
        "extraction_priority",
        "candidate_status",
        "value_candidate",
        "unit_raw_candidate",
        "source_page",
        "evidence_type_candidate",
        "confidence_rule",
        "auto_verification_status",
        "auto_verification_score",
        "auto_verification_issues",
        "auto_verification_rule_ids",
        "evidence_coverage_level",
        "qualitative_rule_version",
        "qualitative_minimum_acceptance",
        "qualitative_reject_if_only",
        "sample_quality_diagnosis",
        "sample_recommended_action",
        "year_alignment_candidate_year",
        "year_alignment_target_value",
        "year_alignment_suggested_value",
        "year_alignment_parsed_values",
        "year_alignment_matched_metric_term",
        "source_text",
    ]
    write_csv(issue_csv, issue_rows, issue_fields)
    summary = summarize(verified)
    summary.update(
        {
            "verified_csv": str(verified_csv),
            "issue_csv": str(issue_csv),
            "summary_json": str(summary_json),
            "report_md": str(report_md),
        }
    )
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(report_md, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
