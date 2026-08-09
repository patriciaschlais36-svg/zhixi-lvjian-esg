# -*- coding: utf-8 -*-
"""Apply conservative target-year corrections from year-alignment audit.

This script consumes the diagnostic output from
build_gold_conflict_year_audit_v1.0.py and applies only high-confidence,
table-grounded target-year replacements. It intentionally rejects broad or
scope-ambiguous rows, because choosing the correct year does not fix a wrong
metric row.

It does not use gold labels and does not estimate true accuracy.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_VERSION = "apply_year_alignment_guard_v1.1_stale_duplicate_block"
NUM_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")

SAFE_EVIDENCE_TYPES = {"native_table", "generic_kpi_year_table", "ocr_table"}
SCOPE_UNSAFE_MARKERS = [
    "其中",
    "园区",
    "男员工",
    "女员工",
    "女性员工",
    "男性员工",
    "管理层",
    "经营管理人员",
    "按性别",
    "按职级",
    "按年龄",
    "少数民族",
    "残疾",
    "类别3",
    "类别 3",
    "范围三",
    "范畴三",
    "部分",
    "分类",
]

FIELD_SPECIFIC_UNSAFE = {
    "E_Q_001": ["范围一", "范围1", "直接温室气体", "范围二", "范围2", "间接温室气体"],
    "E_Q_002": ["范围三", "范畴三", "类别3", "类别 3", "总量（范围一+范围二）", "范围二"],
    "E_Q_003": ["范围三", "范畴三", "范围一", "直接温室气体"],
    "E_Q_012": ["一般固废", "无害废弃物"],
    "S_Q_001": ["占员工总数", "女性员工", "男性员工", "少数民族", "残疾"],
    "S_Q_004": ["人均培训", "按性别", "按职级", "男员工", "女员工", "管理层"],
    "S_Q_005": ["按性别", "按职级", "男员工", "女员工", "管理层"],
    "S_Q_013": ["其中"],
}

FIELD_REQUIRED_MARKERS = {
    "G_Q_001": ["董事"],
    "G_Q_006": ["董事会"],
    "G_Q_007": ["股东"],
    "E_Q_015": ["环保投入"],
    "E_Q_007": ["用电"],
    "S_Q_017": ["公益", "捐"],
    "S_Q_001": ["员工总数", "总人数", "正式员工总数"],
    "S_Q_012": ["研发", "科技研发"],
}


def load_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_number(value: Any) -> str:
    text = str(value or "").replace(",", "")
    match = NUM_RE.search(text)
    if not match:
        return ""
    try:
        number = float(match.group(0))
    except ValueError:
        return ""
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f"{number:.10f}".rstrip("0").rstrip(".")


def numbers_equal(left: Any, right: Any) -> bool:
    left_num = parse_number(left)
    right_num = parse_number(right)
    if not left_num or not right_num:
        return False
    return abs(float(left_num) - float(right_num)) <= max(1e-6, abs(float(right_num)) * 1e-9)


def compact(value: str, limit: int = 260) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def has_any(text: str, markers: list[str]) -> bool:
    return any(marker and marker in text for marker in markers)


def safe_decision(row: dict[str, str]) -> tuple[bool, str]:
    field_id = row.get("field_id", "")
    evidence = row.get("evidence_type_candidate", "")
    metric_window = row.get("metric_window", "")
    source_text = row.get("source_text", "")
    text = f"{metric_window} {source_text}"
    report_year = row.get("report_year", "")
    candidate_year = row.get("candidate_value_year", "")
    suggested = row.get("suggested_value", "") or row.get("target_year_value", "")
    candidate_value = row.get("value_candidate", "")

    if evidence not in SAFE_EVIDENCE_TYPES:
        return False, "non_table_evidence"
    if not report_year or not candidate_year or candidate_year == report_year:
        return False, "no_non_target_year_mapping"
    if not parse_number(suggested):
        return False, "missing_suggested_value"
    if numbers_equal(candidate_value, suggested):
        return False, "candidate_already_target_value"
    if "header:" not in source_text or "table_" not in source_text:
        return False, "not_structured_table_cache"
    if has_any(text, FIELD_SPECIFIC_UNSAFE.get(field_id, [])):
        return False, "field_specific_scope_risk"
    if has_any(text, SCOPE_UNSAFE_MARKERS):
        return False, "scope_or_category_marker"
    required = FIELD_REQUIRED_MARKERS.get(field_id, [])
    if required and not has_any(text, required):
        return False, "required_metric_marker_missing"
    return True, "safe_target_year_replacement"


def row_key(row: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (
        row.get("sample_id", ""),
        row.get("field_id", ""),
        parse_number(row.get("value_candidate", "")),
        row.get("source_page", ""),
        row.get("evidence_type_candidate", ""),
    )


def duplicate_group_key(row: dict[str, str]) -> tuple[str, str, str]:
    return row.get("sample_id", ""), row.get("field_id", ""), row.get("source_page", "")


def build_audit(year_rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], dict[tuple[str, str, str, str, str], dict[str, str]], dict[str, Any]]:
    audit_rows: list[dict[str, Any]] = []
    safe_by_key: dict[tuple[str, str, str, str, str], dict[str, str]] = {}
    decision_counts: Counter[str] = Counter()
    safe_fields: Counter[str] = Counter()
    for row in year_rows:
        ok, reason = safe_decision(row)
        decision_counts[reason] += 1
        if ok:
            safe_by_key[row_key(row)] = row
            safe_fields[row.get("field_id", "")] += 1
        audit_rows.append(
            {
                "case_id": row.get("case_id", ""),
                "sample_id": row.get("sample_id", ""),
                "short_name": row.get("short_name", ""),
                "field_id": row.get("field_id", ""),
                "metric_name_cn": row.get("metric_name_cn", ""),
                "value_candidate": row.get("value_candidate", ""),
                "unit_raw_candidate": row.get("unit_raw_candidate", ""),
                "source_page": row.get("source_page", ""),
                "evidence_type_candidate": row.get("evidence_type_candidate", ""),
                "report_year": row.get("report_year", ""),
                "candidate_value_year": row.get("candidate_value_year", ""),
                "target_year_value": row.get("target_year_value", ""),
                "suggested_value": row.get("suggested_value", ""),
                "parsed_row_values_by_year": row.get("parsed_row_values_by_year", ""),
                "matched_metric_term": row.get("matched_metric_term", ""),
                "year_alignment_guard_status": "safe" if ok else "skipped",
                "year_alignment_guard_reason": reason,
                "metric_window": compact(row.get("metric_window", ""), 420),
            }
        )
    summary = {
        "year_audit_rows": len(year_rows),
        "safe_rows": len(safe_by_key),
        "decision_counts": dict(decision_counts),
        "safe_fields": dict(safe_fields.most_common(50)),
    }
    return audit_rows, safe_by_key, summary


def apply_guard(
    extraction_rows: list[dict[str, str]],
    fields: list[str],
    safe_by_key: dict[tuple[str, str, str, str, str], dict[str, str]],
) -> tuple[list[dict[str, str]], dict[str, Any], list[str]]:
    extra_fields = [
        "year_alignment_guard_status",
        "year_alignment_guard_reason",
        "year_alignment_original_value_candidate",
        "year_alignment_candidate_value_year",
        "year_alignment_target_report_year",
        "year_alignment_target_value",
        "year_alignment_parsed_row_values_by_year",
        "year_alignment_source_case_id",
    ]
    output_fields = list(fields)
    for field in extra_fields:
        if field not in output_fields:
            output_fields.append(field)

    output_rows: list[dict[str, str]] = []
    corrected = 0
    stale_blocked = 0
    unmatched_safe_keys = set(safe_by_key)
    stale_candidates_by_group: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for safe in safe_by_key.values():
        stale_candidates_by_group.setdefault(duplicate_group_key(safe), []).append(safe)

    for row in extraction_rows:
        out = dict(row)
        key = row_key(row)
        safe = safe_by_key.get(key)
        if safe:
            corrected += 1
            unmatched_safe_keys.discard(key)
            original_value = row.get("value_candidate", "")
            target_value = safe.get("suggested_value", "") or safe.get("target_year_value", "")
            out["value_candidate"] = target_value
            if "value_standardized_candidate" in out:
                out["value_standardized_candidate"] = target_value
            out["year_alignment_guard_status"] = "corrected"
            out["year_alignment_guard_reason"] = "safe_target_year_replacement"
            out["year_alignment_original_value_candidate"] = original_value
            out["year_alignment_candidate_value_year"] = safe.get("candidate_value_year", "")
            out["year_alignment_target_report_year"] = safe.get("report_year", "")
            out["year_alignment_target_value"] = target_value
            out["year_alignment_parsed_row_values_by_year"] = safe.get("parsed_row_values_by_year", "")
            out["year_alignment_source_case_id"] = safe.get("case_id", "")
            reason = (
                f"year_alignment_guard corrected {original_value} from {safe.get('candidate_value_year','')} "
                f"to {target_value} for report year {safe.get('report_year','')}"
            )
            out["review_reason"] = ((row.get("review_reason", "") + " | ") if row.get("review_reason") else "") + reason
        else:
            stale_safe = None
            if row.get("candidate_status") == "candidate_found":
                for candidate_safe in stale_candidates_by_group.get(duplicate_group_key(row), []):
                    old_value = candidate_safe.get("value_candidate", "")
                    target_value = candidate_safe.get("suggested_value", "") or candidate_safe.get("target_year_value", "")
                    if numbers_equal(row.get("value_candidate", ""), old_value) and not numbers_equal(row.get("value_candidate", ""), target_value):
                        stale_safe = candidate_safe
                        break
            if stale_safe:
                stale_blocked += 1
                old_value = row.get("value_candidate", "")
                target_value = stale_safe.get("suggested_value", "") or stale_safe.get("target_year_value", "")
                out["candidate_status"] = "no_candidate"
                out["precision_gate_status"] = "blocked"
                out["recommended_next_status"] = "year_alignment_stale_duplicate_review"
                out["needs_llm_review"] = "yes"
                out["year_alignment_guard_status"] = "stale_duplicate_blocked"
                out["year_alignment_guard_reason"] = "same sample/field/page kept an old-year duplicate after safe target-year correction"
                out["year_alignment_original_value_candidate"] = old_value
                out["year_alignment_candidate_value_year"] = stale_safe.get("candidate_value_year", "")
                out["year_alignment_target_report_year"] = stale_safe.get("report_year", "")
                out["year_alignment_target_value"] = target_value
                out["year_alignment_parsed_row_values_by_year"] = stale_safe.get("parsed_row_values_by_year", "")
                out["year_alignment_source_case_id"] = stale_safe.get("case_id", "")
                reason = (
                    f"year_alignment_guard blocked stale duplicate {old_value} from "
                    f"{stale_safe.get('candidate_value_year','')} after target-year correction to {target_value}"
                )
                out["review_reason"] = ((row.get("review_reason", "") + " | ") if row.get("review_reason") else "") + reason
            else:
                out["year_alignment_guard_status"] = "kept"
                out["year_alignment_guard_reason"] = ""
                out["year_alignment_original_value_candidate"] = ""
                out["year_alignment_candidate_value_year"] = ""
                out["year_alignment_target_report_year"] = ""
                out["year_alignment_target_value"] = ""
                out["year_alignment_parsed_row_values_by_year"] = ""
                out["year_alignment_source_case_id"] = ""
        output_rows.append(out)

    summary = {
        "input_rows": len(extraction_rows),
        "corrected_rows": corrected,
        "stale_duplicate_blocked_rows": stale_blocked,
        "unmatched_safe_keys": len(unmatched_safe_keys),
    }
    return output_rows, summary, output_fields


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--year-audit-csv", type=Path, required=True)
    parser.add_argument("--audit-csv", type=Path, required=True)
    parser.add_argument("--summary-json", type=Path, required=True)
    parser.add_argument("--report-md", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path)
    args = parser.parse_args()

    extraction_rows, fields = load_csv(args.input_csv)
    year_rows, _ = load_csv(args.year_audit_csv)
    audit_rows, safe_by_key, audit_summary = build_audit(year_rows)
    output_rows: list[dict[str, str]] = []
    apply_summary: dict[str, Any] = {"input_rows": len(extraction_rows), "corrected_rows": 0, "unmatched_safe_keys": 0}
    output_fields = fields
    if args.output_csv:
        output_rows, apply_summary, output_fields = apply_guard(extraction_rows, fields, safe_by_key)
        write_csv(args.output_csv, output_rows, output_fields)

    audit_fields = [
        "case_id",
        "sample_id",
        "short_name",
        "field_id",
        "metric_name_cn",
        "value_candidate",
        "unit_raw_candidate",
        "source_page",
        "evidence_type_candidate",
        "report_year",
        "candidate_value_year",
        "target_year_value",
        "suggested_value",
        "parsed_row_values_by_year",
        "matched_metric_term",
        "year_alignment_guard_status",
        "year_alignment_guard_reason",
        "metric_window",
    ]
    write_csv(args.audit_csv, audit_rows, audit_fields)
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "script_version": SCRIPT_VERSION,
        "input_csv": str(args.input_csv),
        "year_audit_csv": str(args.year_audit_csv),
        "audit_csv": str(args.audit_csv),
        "output_csv": str(args.output_csv or ""),
        **audit_summary,
        **apply_summary,
        "note": "Conservative target-year guard; not a gold-label evaluation.",
    }
    args.summary_json.parent.mkdir(parents=True, exist_ok=True)
    args.summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Year Alignment Guard Report",
        "",
        f"- generated_at: {summary['generated_at']}",
        f"- year_audit_rows: {summary['year_audit_rows']}",
        f"- safe_rows: {summary['safe_rows']}",
        f"- corrected_rows: {summary['corrected_rows']}",
        f"- output_csv: `{summary['output_csv']}`",
        "",
        "## Decision Counts",
        "",
        "| reason | count |",
        "|---|---:|",
    ]
    for key, value in sorted(summary["decision_counts"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {key} | {value} |")
    lines.extend(["", "## Boundary", "", "- This guard does not use gold labels and does not prove true accuracy."])
    args.report_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
