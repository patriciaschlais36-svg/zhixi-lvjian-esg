# -*- coding: utf-8 -*-
"""Apply the non-write negative casebook as a conservative precision gate.

This script does not re-run extraction. It reads an existing candidate CSV and
blocks exact negative cases learned from DeepSeek review/recall probes.

Policy:
- Hard negative categories are converted to no_candidate.
- Ambiguous/structure categories are also blocked by default to protect
  precision, but keep a clear review reason and audit trail.
- correctable_but_not_negative is not blocked; it is flagged for modify review.
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


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = BASE_DIR / "算法方案" / "pilot_full_extraction_v2.15_200samples_pipeline_guarded" / "全量指标候选抽取结果_200份v2.19_deepseek_reviewed_Top500_R018_ocr.csv"
DEFAULT_CASEBOOK = BASE_DIR / "算法源码" / "配置" / "不可回写负样本库.csv"
DEFAULT_OUTPUT = BASE_DIR / "算法方案" / "pilot_full_extraction_v2.15_200samples_pipeline_guarded" / "全量指标候选抽取结果_200份v2.20_precision_gated.csv"

HARD_BLOCK_CATEGORIES = {
    "candidate_not_supported",
    "wrong_metric_or_context",
    "component_not_total",
    "ambiguous_needs_review",
    "project_or_case_value_not_company_total",
    "unit_or_value_type_mismatch",
    "index_reference_only",
    "ocr_noise_or_layout_error",
    "scope_boundary_mismatch",
}
FLAG_ONLY_CATEGORIES = {"correctable_but_not_negative"}

GATE_COLUMNS = [
    "precision_gate_status",
    "precision_gate_case_id",
    "precision_gate_category",
    "precision_gate_policy",
    "precision_gate_rule",
    "precision_gate_reason",
]


def norm(value: str) -> str:
    text = str(value or "")
    text = text.replace("，", ",").replace("％", "%")
    text = re.sub(r"\s+", "", text)
    return text


def norm_number_like(value: str) -> str:
    text = norm(value)
    text = text.replace(",", "")
    return text


def load_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def write_rows(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_audit(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = [
        "sample_id",
        "field_id",
        "old_status",
        "new_status",
        "old_value",
        "old_unit",
        "source_page",
        "case_id",
        "category",
        "policy",
        "action",
        "reason",
    ]
    write_rows(path, rows, fieldnames)


def ensure_columns(fieldnames: list[str]) -> list[str]:
    out = list(fieldnames)
    for col in GATE_COLUMNS:
        if col not in out:
            out.append(col)
    return out


def candidate_keys(row: dict[str, str]) -> list[tuple[str, str, str, str, str]]:
    sid = row.get("sample_id", "")
    fid = row.get("field_id", "")
    val = norm_number_like(row.get("value_candidate", ""))
    unit = norm(row.get("unit_raw_candidate", ""))
    page = norm(row.get("source_page", ""))
    return [
        (sid, fid, val, unit, page),
        (sid, fid, val, unit, ""),
        (sid, fid, val, "", page),
        (sid, fid, val, "", ""),
    ]


def case_keys(case: dict[str, str]) -> list[tuple[str, str, str, str, str]]:
    sid = case.get("sample_id", "")
    fid = case.get("field_id", "")
    val = norm_number_like(case.get("candidate_value", ""))
    unit = norm(case.get("candidate_unit", ""))
    page = norm(case.get("candidate_source_page", ""))
    if not sid or not fid:
        return []
    # Qualitative negative cases often have no value_candidate. Keep them
    # matchable only when a source page is present so the rule stays exact.
    if not val and not page:
        return []
    if not val:
        keys = [
            (sid, fid, val, unit, page),
            (sid, fid, val, "", page),
        ]
        dedup: list[tuple[str, str, str, str, str]] = []
        for key in keys:
            if key not in dedup:
                dedup.append(key)
        return dedup
    keys = [
        (sid, fid, val, unit, page),
        (sid, fid, val, unit, ""),
        (sid, fid, val, "", page),
        (sid, fid, val, "", ""),
    ]
    dedup: list[tuple[str, str, str, str, str]] = []
    for key in keys:
        if key not in dedup:
            dedup.append(key)
    return dedup


def load_casebook(path: Path, min_mode: str) -> dict[tuple[str, str, str, str, str], dict[str, str]]:
    rows, _ = load_rows(path)
    index: dict[tuple[str, str, str, str, str], dict[str, str]] = {}
    for case in rows:
        category = case.get("negative_category", "")
        if category in FLAG_ONLY_CATEGORIES and min_mode == "hard_only":
            continue
        if category not in HARD_BLOCK_CATEGORIES and category not in FLAG_ONLY_CATEGORIES:
            continue
        for key in case_keys(case):
            current = index.get(key)
            if not current:
                index[key] = case
                continue
            # Prefer exact source-page/unit cases over weaker fallback keys.
            if len(norm(current.get("candidate_source_page", ""))) < len(norm(case.get("candidate_source_page", ""))):
                index[key] = case
    return index


def find_case(row: dict[str, str], case_index: dict[tuple[str, str, str, str, str], dict[str, str]]) -> dict[str, str] | None:
    for key in candidate_keys(row):
        case = case_index.get(key)
        if case:
            return case
    return None


def apply_case(row: dict[str, str], case: dict[str, str], flag_only: bool) -> tuple[str, str]:
    category = case.get("negative_category", "")
    policy = case.get("auto_gate_policy", "")
    row["precision_gate_case_id"] = case.get("case_id", "")
    row["precision_gate_category"] = category
    row["precision_gate_policy"] = policy
    row["precision_gate_rule"] = case.get("do_not_write_rule", "")
    row["precision_gate_reason"] = case.get("reason", "")

    if flag_only or category in FLAG_ONLY_CATEGORIES:
        row["precision_gate_status"] = "flagged_for_modify_review"
        row["needs_llm_review"] = "yes"
        row["review_reason"] = "Precision gate提示需修正/复核：" + case.get("reason", "")
        return "flagged", row.get("candidate_status", "")

    old_status = row.get("candidate_status", "")
    row["precision_gate_status"] = "blocked"
    row["candidate_status"] = "no_candidate"
    row["candidate_disclosure_class"] = "blocked_by_precision_gate"
    row["value_status"] = "blocked_by_negative_casebook"
    row["needs_llm_review"] = "no"
    row["review_reason"] = "Precision gate拦截：" + case.get("reason", "")
    row["confidence_rule"] = "0.03"
    method = row.get("value_extraction_method", "")
    if "precision_gate_block_v1.0" not in method:
        row["value_extraction_method"] = (method + "+precision_gate_block_v1.0").strip("+")
    row["extractor_version"] = "v2.20_precision_gated"
    return "blocked", old_status


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--casebook", type=Path, default=DEFAULT_CASEBOOK)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--flag-only", action="store_true", help="Only flag matches, do not change candidate_status")
    parser.add_argument("--mode", choices=["hard_only", "include_modify"], default="hard_only")
    args = parser.parse_args()

    rows, fieldnames = load_rows(args.input)
    fieldnames = ensure_columns(fieldnames)
    case_index = load_casebook(args.casebook, args.mode)

    audit: list[dict[str, str]] = []
    counts: Counter[str] = Counter()
    categories: Counter[str] = Counter()

    for row in rows:
        if row.get("candidate_status") != "candidate_found":
            continue
        case = find_case(row, case_index)
        if not case:
            continue
        old_status = row.get("candidate_status", "")
        old_value = row.get("value_candidate", "")
        old_unit = row.get("unit_raw_candidate", "")
        action, before_status = apply_case(row, case, args.flag_only)
        counts[action] += 1
        categories[case.get("negative_category", "")] += 1
        audit.append(
            {
                "sample_id": row.get("sample_id", ""),
                "field_id": row.get("field_id", ""),
                "old_status": before_status or old_status,
                "new_status": row.get("candidate_status", ""),
                "old_value": old_value,
                "old_unit": old_unit,
                "source_page": row.get("source_page", ""),
                "case_id": case.get("case_id", ""),
                "category": case.get("negative_category", ""),
                "policy": case.get("auto_gate_policy", ""),
                "action": action,
                "reason": case.get("reason", ""),
            }
        )

    write_rows(args.output, rows, fieldnames)
    audit_path = args.output.with_name(args.output.stem + "_audit.csv")
    write_audit(audit_path, audit)
    summary_path = args.output.with_name(args.output.stem + "_summary.json")
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input": str(args.input),
        "casebook": str(args.casebook),
        "output": str(args.output),
        "audit": str(audit_path),
        "input_rows": len(rows),
        "case_index_keys": len(case_index),
        "matched_rows": len(audit),
        "action_counts": dict(counts),
        "category_counts": dict(categories),
        "flag_only": args.flag_only,
        "mode": args.mode,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
