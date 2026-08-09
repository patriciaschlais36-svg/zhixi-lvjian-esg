# -*- coding: utf-8 -*-
"""Build a read-only field-level candidate resolution audit.

This script groups extraction rows by sample_id + field_id and proposes a
deterministic winner plus an ambiguity status. It does not edit extraction CSV.
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
DEFAULT_EXTRACTION = (
    BASE_DIR
    / "算法方案"
    / "pilot_full_extraction_v2.15_200samples_pipeline_guarded"
    / "全量指标候选抽取结果_200份v2.20_precision_gated.csv"
)
DEFAULT_INDICATOR = BASE_DIR / "算法源码" / "配置" / "ESG指标体系.csv"
DEFAULT_ISSUES = BASE_DIR / "评估测试" / "extraction_output_quality_audit_v2.20" / "extraction_output_quality_issues_v1.0.csv"
DEFAULT_OUT_DIR = BASE_DIR / "评估测试" / "field_level_candidate_resolution_v2.30"

EVIDENCE_WEIGHTS = {
    "native_table": 16,
    "generic_kpi_year_table": 16,
    "native_text": 12,
    "ocr_table": 10,
    "ocr_text": 8,
    "chart": 6,
}
HIGH_RISK_PENALTY = 35
WARNING_PENALTY = 4


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


def parse_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value or "").strip())
    except ValueError:
        return default


def parse_int(value: Any, default: int = 999) -> int:
    try:
        return int(float(str(value or "").strip()))
    except ValueError:
        return default


def group_key(row: dict[str, str]) -> tuple[str, str]:
    return row.get("sample_id", ""), row.get("field_id", "")


def candidate_key(row: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (
        row.get("sample_id", ""),
        row.get("field_id", ""),
        row.get("value_candidate", ""),
        row.get("unit_raw_candidate", ""),
        row.get("source_page", ""),
    )


def issue_maps(rows: list[dict[str, str]]) -> tuple[dict[tuple[str, str, str, str, str], list[str]], dict[tuple[str, str], list[str]]]:
    by_candidate: dict[tuple[str, str, str, str, str], list[str]] = defaultdict(list)
    by_group: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in rows:
        issue = row.get("issue", "")
        severity = row.get("severity", "")
        if severity in {"high", "error", "warning"} and issue:
            by_candidate[candidate_key(row)].append(f"{severity}:{issue}")
            by_group[group_key(row)].append(f"{severity}:{issue}")
    return by_candidate, by_group


def indicator_map(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {row.get("field_id", ""): row for row in rows}


def score_candidate(row: dict[str, str], candidate_issues: list[str]) -> int:
    if row.get("candidate_status") != "candidate_found":
        return 20 if row.get("precision_gate_status") == "blocked" else 10
    conf = parse_float(row.get("confidence_rule"), 0.55)
    score = int(round(conf * 70))
    score += EVIDENCE_WEIGHTS.get(row.get("evidence_type_candidate", ""), 4)
    score += max(0, 8 - parse_int(row.get("candidate_rank"), 8))
    if row.get("source_page"):
        score += 4
    if row.get("source_text") or row.get("source_table_cell"):
        score += 4
    if row.get("precision_gate_status") == "blocked":
        score -= 60
    if row.get("needs_llm_review", "").lower() == "yes":
        score -= 6
    if any(item.startswith("high:") or item.startswith("error:") for item in candidate_issues):
        score -= HIGH_RISK_PENALTY
    score -= min(20, sum(1 for item in candidate_issues if item.startswith("warning:")) * WARNING_PENALTY)
    return max(0, min(100, score))


def unique_found_signature(row: dict[str, str]) -> tuple[str, str, str]:
    return row.get("value_candidate", ""), row.get("unit_raw_candidate", ""), row.get("source_page", "")


def resolve_group(
    key: tuple[str, str],
    rows: list[dict[str, str]],
    candidate_issues: dict[tuple[str, str, str, str, str], list[str]],
    group_issues: list[str],
    indicators: dict[str, dict[str, str]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    scored: list[tuple[int, dict[str, str], list[str]]] = []
    for row in rows:
        issues = candidate_issues.get(candidate_key(row), [])
        scored.append((score_candidate(row, issues), row, issues))
    scored.sort(key=lambda item: (-item[0], parse_int(item[1].get("candidate_rank"), 999)))
    best_score, best, best_issues = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else -1
    found_rows = [row for row in rows if row.get("candidate_status") == "candidate_found"]
    found_signatures = {unique_found_signature(row) for row in found_rows}
    high_group = any(item.startswith("high:") or item.startswith("error:") for item in group_issues)
    best_high = any(item.startswith("high:") or item.startswith("error:") for item in best_issues)
    margin = best_score - second_score if second_score >= 0 else best_score

    if not found_rows:
        resolution_status = "field_no_candidate"
    elif best_high:
        resolution_status = "needs_high_risk_validation"
    elif len(found_signatures) == 1 and best_score >= 72:
        resolution_status = "auto_resolved_single_value"
    elif margin >= 12 and best_score >= 72:
        resolution_status = "auto_resolved_by_score_margin"
    else:
        resolution_status = "needs_field_level_resolution"

    indicator = indicators.get(key[1], {})
    group_row = {
        "sample_id": key[0],
        "field_id": key[1],
        "dimension": best.get("dimension", indicator.get("dimension", "")),
        "metric_name_cn": best.get("metric_name_cn", indicator.get("metric_name_cn", "")),
        "candidate_rows": len(rows),
        "candidate_found_rows": len(found_rows),
        "unique_found_candidates": len(found_signatures),
        "best_score": best_score,
        "second_score": second_score if second_score >= 0 else "",
        "score_margin": margin,
        "resolution_status": resolution_status,
        "best_candidate_status": best.get("candidate_status", ""),
        "best_value_candidate": best.get("value_candidate", ""),
        "best_unit_raw_candidate": best.get("unit_raw_candidate", ""),
        "best_source_page": best.get("source_page", ""),
        "best_evidence_type": best.get("evidence_type_candidate", ""),
        "best_confidence_rule": best.get("confidence_rule", ""),
        "best_needs_llm_review": best.get("needs_llm_review", ""),
        "best_high_or_error_issue_count": sum(1 for item in best_issues if item.startswith("high:") or item.startswith("error:")),
        "group_issue_count": len(group_issues),
        "group_issues": ";".join(sorted(set(group_issues))[:12]),
    }

    candidate_rows: list[dict[str, Any]] = []
    for score, row, issues in scored:
        candidate_rows.append(
            {
                "sample_id": key[0],
                "field_id": key[1],
                "metric_name_cn": row.get("metric_name_cn", ""),
                "candidate_status": row.get("candidate_status", ""),
                "candidate_rank": row.get("candidate_rank", ""),
                "resolution_score": score,
                "value_candidate": row.get("value_candidate", ""),
                "unit_raw_candidate": row.get("unit_raw_candidate", ""),
                "source_page": row.get("source_page", ""),
                "evidence_type_candidate": row.get("evidence_type_candidate", ""),
                "confidence_rule": row.get("confidence_rule", ""),
                "needs_llm_review": row.get("needs_llm_review", ""),
                "precision_gate_status": row.get("precision_gate_status", ""),
                "candidate_issues": ";".join(sorted(set(issues))[:12]),
                "source_text_preview": (row.get("source_text", "") or row.get("source_table_cell", ""))[:300],
            }
        )
    return group_row, candidate_rows


def write_report(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Field-level 多候选归并审计 v1.0",
        "",
        f"生成时间：{summary['generated_at']}",
        "",
        "## 总览",
        "",
        f"- field groups：{summary['field_groups']}",
        f"- candidate rows：{summary['candidate_rows']}",
        f"- 多候选 groups：{summary['multi_candidate_groups']}",
        f"- 需要 high 风险验证：{summary['needs_high_risk_validation']}",
        f"- 需要 field-level 归并复核：{summary['needs_field_level_resolution']}",
        "",
        "## resolution_status 分布",
        "",
        "| status | count |",
        "|---|---:|",
    ]
    for key, value in sorted(summary["resolution_status_counts"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {key} | {value} |")
    lines.extend(["", "## 说明", ""])
    lines.append("- 本报告只给出归并建议，不修改主结果。")
    lines.append("- 真实评估时应以 field-level 输出为口径，避免一字段多候选影响 Precision。")
    lines.append("- high 风险候选应优先进入验证队列，再考虑自动归并。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--extraction-csv", type=Path, default=DEFAULT_EXTRACTION)
    parser.add_argument("--indicator-csv", type=Path, default=DEFAULT_INDICATOR)
    parser.add_argument("--issues-csv", type=Path, default=DEFAULT_ISSUES)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    extraction_rows = load_rows(args.extraction_csv)
    indicators = indicator_map(load_rows(args.indicator_csv))
    by_candidate, by_group = issue_maps(load_rows(args.issues_csv))
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in extraction_rows:
        groups[group_key(row)].append(row)

    group_rows: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    for key, rows in sorted(groups.items()):
        group_row, details = resolve_group(key, rows, by_candidate, by_group.get(key, []), indicators)
        group_rows.append(group_row)
        candidate_rows.extend(details)

    status_counts = Counter(row["resolution_status"] for row in group_rows)
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "extraction_csv": str(args.extraction_csv),
        "issues_csv": str(args.issues_csv),
        "field_groups": len(group_rows),
        "candidate_rows": len(candidate_rows),
        "multi_candidate_groups": sum(1 for row in group_rows if int(row["candidate_rows"]) > 1),
        "needs_high_risk_validation": status_counts.get("needs_high_risk_validation", 0),
        "needs_field_level_resolution": status_counts.get("needs_field_level_resolution", 0),
        "resolution_status_counts": dict(status_counts),
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    group_csv = args.out_dir / "field_level_resolution_summary_v1.0.csv"
    candidates_csv = args.out_dir / "field_level_resolution_candidates_v1.0.csv"
    summary_json = args.out_dir / "field_level_resolution_summary_v1.0.json"
    report_md = args.out_dir / "field_level_resolution_report_v1.0.md"
    group_fields = [
        "sample_id", "field_id", "dimension", "metric_name_cn",
        "candidate_rows", "candidate_found_rows", "unique_found_candidates",
        "best_score", "second_score", "score_margin", "resolution_status",
        "best_candidate_status", "best_value_candidate", "best_unit_raw_candidate",
        "best_source_page", "best_evidence_type", "best_confidence_rule",
        "best_needs_llm_review", "best_high_or_error_issue_count",
        "group_issue_count", "group_issues",
    ]
    candidate_fields = [
        "sample_id", "field_id", "metric_name_cn", "candidate_status",
        "candidate_rank", "resolution_score", "value_candidate",
        "unit_raw_candidate", "source_page", "evidence_type_candidate",
        "confidence_rule", "needs_llm_review", "precision_gate_status",
        "candidate_issues", "source_text_preview",
    ]
    write_csv(group_csv, group_rows, group_fields)
    write_csv(candidates_csv, candidate_rows, candidate_fields)
    summary.update({
        "group_csv": str(group_csv),
        "candidates_csv": str(candidates_csv),
        "summary_json": str(summary_json),
        "report_md": str(report_md),
    })
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(report_md, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
