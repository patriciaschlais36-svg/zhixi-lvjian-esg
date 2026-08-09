# -*- coding: utf-8 -*-
"""Evaluate blind quantitative validation task labels with unit-normalized values.

This extends evaluate_blind_quant_validation_tasks_v1.0.py for blind30/P0
task-table style files. It supports both human gold columns and auto-silver
columns, while making the claim level explicit:

- gold: held-out gold evaluation, only valid when gold_status is filled.
- silver: evidence/proxy audit signal, never final true accuracy.

The script evaluates presence (TP/FP/FN/TN) and quantitative value matching.
For values it reports both raw numeric accuracy and unit-normalized accuracy so
safe unit conversions such as 亿元 -> 万元 or 万吨 -> 吨 do not create false
mismatches.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


DISCLOSED_STATUS = {"disclosed", "已披露", "披露"}
NEGATIVE_STATUS = {"not_disclosed", "not_found", "not_applicable", "未披露", "未找到", "不适用"}
NUM_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def norm_status(value: str) -> str:
    return str(value or "").strip().lower()


def parse_number(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace(",", "").replace("，", "").replace(" ", "")
    match = NUM_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def clean_unit(value: str) -> str:
    text = str(value or "").strip().lower()
    replacements = {
        "（": "(",
        "）": ")",
        "％": "%",
        "／": "/",
        " ": "",
        "人民币": "元",
        "rmb": "元",
        "cny": "元",
        "co₂": "co2",
        "二氧化碳当量": "co2e",
        "吨二氧化碳当量": "吨co2e",
        "万吨二氧化碳当量": "万吨co2e",
        "吨标准煤": "吨标煤",
        "万吨标准煤": "万吨标煤",
        "立方米": "m3",
        "万立方米": "万m3",
        "兆瓦时": "mwh",
        "千瓦时": "kwh",
        "万千瓦时": "万kwh",
        "亿千瓦时": "亿kwh",
        "学时": "小时",
        "名": "人",
        "宗": "件",
        "起": "件",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def unit_class(unit: str, field_id: str = "") -> str:
    text = clean_unit(unit)
    if not text:
        return "blank"
    if "%" in text or "比例" in text or "占比" in text:
        return "percent"
    if "亿元" in text or "万元" in text or text == "元" or text.endswith("元"):
        return "money"
    if "co2e" in text or "tco2e" in text:
        return "co2_mass"
    if "万吨标煤" in text or "吨标煤" in text:
        return "standard_coal"
    if "万kwh" in text or "亿kwh" in text or "kwh" in text or "mwh" in text:
        return "electricity"
    if "万m3" in text or "m3" in text:
        return "water_volume"
    if "千克" in text or text == "kg":
        return "mass"
    if "万吨" in text or "吨" in text:
        if field_id.startswith("E_Q_00") or field_id in {"E_Q_012", "E_Q_013"}:
            return "mass"
        return "mass"
    if "小时/人" in text:
        return "avg_hours"
    if "小时" in text or "人时" in text:
        return "hours"
    if "人次" in text:
        return "person_time"
    if text == "人" or "人数" in text:
        return "person"
    if any(token in text for token in ("件", "次", "场", "项", "家")):
        return "count"
    return "other"


def to_base(value: str, unit: str, field_id: str = "") -> tuple[float | None, str, str]:
    number = parse_number(value)
    cls = unit_class(unit, field_id)
    text = clean_unit(unit)
    if number is None:
        return None, cls, text

    factor = 1.0
    if cls == "money":
        if "亿元" in text:
            factor = 10000.0
        elif text == "元" or (text.endswith("元") and "万元" not in text):
            factor = 0.0001
        return number * factor, cls, "万元"
    if cls in {"co2_mass", "mass"}:
        if "万吨" in text:
            factor = 10000.0
        elif "千克" in text or text == "kg":
            factor = 0.001
        return number * factor, cls, "吨"
    if cls == "standard_coal":
        if "万吨" in text:
            factor = 10000.0
        return number * factor, cls, "吨标煤"
    if cls == "electricity":
        if "亿kwh" in text:
            factor = 100000000.0
        elif "万kwh" in text:
            factor = 10000.0
        elif "mwh" in text:
            factor = 1000.0
        return number * factor, cls, "kwh"
    if cls == "water_volume":
        if "万m3" in text:
            factor = 10000.0
        return number * factor, cls, "m3"
    return number, cls, text or cls


def classes_compatible(left: str, right: str) -> bool:
    if left == right:
        return True
    groups = [
        {"co2_mass", "mass"},
        {"person", "person_time"},
        {"hours", "avg_hours"},
        {"count"},
        {"percent"},
        {"money"},
        {"standard_coal"},
        {"electricity"},
        {"water_volume"},
    ]
    return any(left in group and right in group for group in groups)


def raw_value_equal(candidate: str, label: str, rel_tol: float, abs_tol: float) -> bool:
    c = parse_number(candidate)
    g = parse_number(label)
    if c is None or g is None:
        return str(candidate or "").strip() == str(label or "").strip()
    return abs(c - g) <= max(abs_tol, abs(g) * rel_tol)


def unit_normalized_value_equal(
    candidate_value: str,
    candidate_unit: str,
    label_value: str,
    label_unit: str,
    field_id: str,
    rel_tol: float,
    abs_tol: float,
) -> tuple[bool, str, str, str]:
    candidate_base, candidate_cls, candidate_base_unit = to_base(candidate_value, candidate_unit, field_id)
    label_base, label_cls, label_base_unit = to_base(label_value, label_unit, field_id)
    if candidate_base is None or label_base is None:
        raw_ok = str(candidate_value or "").strip() == str(label_value or "").strip()
        return raw_ok, "", "", "non_numeric_raw_compare"

    if not classes_compatible(candidate_cls, label_cls):
        return (
            False,
            f"{candidate_base:g} {candidate_base_unit}",
            f"{label_base:g} {label_base_unit}",
            f"incompatible_unit_class:{candidate_cls}!={label_cls}",
        )

    if candidate_cls == "percent" or label_cls == "percent":
        variants = [
            (candidate_base, label_base),
            (candidate_base * 100, label_base),
            (candidate_base, label_base * 100),
        ]
        ok = any(abs(left - right) <= 0.5 for left, right in variants)
        return ok, f"{candidate_base:g} %", f"{label_base:g} %", "percent_normalized"

    ok = abs(candidate_base - label_base) <= max(abs_tol, abs(label_base) * rel_tol)
    if not ok and candidate_cls in {"person", "person_time", "count", "hours", "avg_hours"}:
        ok = abs(candidate_base - label_base) <= 1
    return (
        ok,
        f"{candidate_base:g} {candidate_base_unit}",
        f"{label_base:g} {label_base_unit}",
        "unit_normalized",
    )


def unit_equal_or_compatible(candidate_unit: str, label_unit: str, field_id: str) -> tuple[bool, str]:
    candidate = clean_unit(candidate_unit)
    label = clean_unit(label_unit)
    if not candidate or not label:
        return False, "blank_unit"
    if candidate == label or candidate in label or label in candidate:
        return True, "string_equal_or_contains"
    candidate_cls = unit_class(candidate_unit, field_id)
    label_cls = unit_class(label_unit, field_id)
    if classes_compatible(candidate_cls, label_cls):
        return True, f"compatible_unit_class:{candidate_cls}"
    return False, f"incompatible_unit_class:{candidate_cls}!={label_cls}"


def wilson(success: int, total: int, z: float = 1.96) -> dict[str, float]:
    if total <= 0:
        return {"point": 0.0, "low": 0.0, "high": 0.0}
    p = success / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return {
        "point": round(p, 6),
        "low": round(max(0.0, center - margin), 6),
        "high": round(min(1.0, center + margin), 6),
    }


def status_from_row(row: dict[str, str], source: str) -> str:
    if source == "gold":
        return norm_status(row.get("gold_status", ""))
    status = norm_status(row.get("silver_status", ""))
    if status == "incorrect_candidate" and row.get("silver_value", "").strip():
        return "disclosed"
    if status in {"disclosed", "not_disclosed", "not_applicable"}:
        return status
    return ""


def label_value_from_row(row: dict[str, str], source: str) -> str:
    return row.get("gold_value", "") if source == "gold" else row.get("silver_value", "")


def label_unit_from_row(row: dict[str, str], source: str) -> str:
    return row.get("gold_unit", "") if source == "gold" else row.get("silver_unit", "")


def pack_counts(bucket: Counter[str]) -> dict[str, Any]:
    tp = bucket["TP"]
    fp = bucket["FP"]
    fn = bucket["FN"]
    tn = bucket["TN"]
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "evaluated_rows": tp + fp + fn + tn,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
    }


def make_group_rows(groups: dict[str, dict[str, Counter[str]]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group_name, values in sorted(groups.items()):
        for name, counter in sorted(values.items()):
            rows.append({"group": group_name, "name": name, **pack_counts(counter)})
    return rows


def evaluate(
    rows: list[dict[str, str]],
    source: str,
    rel_tol: float,
    abs_tol: float,
    include_not_applicable: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    detail_rows: list[dict[str, Any]] = []
    presence_counts: Counter[str] = Counter()
    value_raw_ok = 0
    value_unit_normalized_ok = 0
    value_total = 0
    unit_ok = 0
    unit_total = 0
    label_status_counts: Counter[str] = Counter()
    original_silver_status_counts: Counter[str] = Counter()
    groups: dict[str, dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))

    for row in rows:
        label_status = status_from_row(row, source)
        original_silver_status_counts[norm_status(row.get("silver_status", "")) or "blank"] += 1
        label_status_counts[label_status or "unlabeled"] += 1
        if label_status == "not_applicable" and not include_not_applicable:
            continue
        if label_status not in {"disclosed", "not_disclosed", "not_applicable"}:
            continue

        candidate_found = row.get("candidate_status") == "candidate_found"
        label_disclosed = label_status == "disclosed"
        if label_disclosed and candidate_found:
            outcome = "TP"
        elif not label_disclosed and candidate_found:
            outcome = "FP"
        elif label_disclosed and not candidate_found:
            outcome = "FN"
        else:
            outcome = "TN"

        presence_counts[outcome] += 1
        for group_name, group_value in {
            "by_dimension": row.get("dimension", "") or "unknown",
            "by_diagnosis": row.get("diagnosis", "") or "unknown",
            "by_evidence_type": row.get("evidence_type_candidate", "") or "none",
            "by_auto_verification_status": row.get("auto_verification_status", "") or "unknown",
            "by_risk_bucket": row.get("risk_bucket", "") or "unknown",
            "by_evidence_guard_status": row.get("evidence_guard_status", "") or "none",
            "by_field_id": row.get("field_id", "") or "unknown",
        }.items():
            groups[group_name][group_value][outcome] += 1

        raw_match = ""
        unit_normalized_match = ""
        unit_match = ""
        candidate_value_normalized = ""
        label_value_normalized = ""
        value_match_reason = ""
        unit_match_reason = ""

        if label_disclosed and candidate_found:
            candidate_value = row.get("value_candidate", "")
            candidate_unit = row.get("unit_raw_candidate", "")
            label_value = label_value_from_row(row, source)
            label_unit = label_unit_from_row(row, source)
            field_id = row.get("field_id", "")

            value_total += 1
            raw_ok = raw_value_equal(candidate_value, label_value, rel_tol, abs_tol)
            unit_norm_ok, candidate_norm, label_norm, value_reason = unit_normalized_value_equal(
                candidate_value,
                candidate_unit,
                label_value,
                label_unit,
                field_id,
                rel_tol,
                abs_tol,
            )
            # If the raw value is already correct, keep value accuracy positive
            # for blank or unseen unit synonyms. Do not override an explicit
            # unit-class conflict; that is often a scope or metric mismatch.
            if raw_ok and not unit_norm_ok and "incompatible_unit_class" not in value_reason:
                unit_norm_ok = True
                value_reason = f"{value_reason};raw_match_override"

            unit_ok_flag, unit_reason = unit_equal_or_compatible(candidate_unit, label_unit, field_id)

            raw_match = "yes" if raw_ok else "no"
            unit_normalized_match = "yes" if unit_norm_ok else "no"
            unit_match = "yes" if unit_ok_flag else "no"
            candidate_value_normalized = candidate_norm
            label_value_normalized = label_norm
            value_match_reason = value_reason
            unit_match_reason = unit_reason
            value_raw_ok += 1 if raw_ok else 0
            value_unit_normalized_ok += 1 if unit_norm_ok else 0
            unit_total += 1
            unit_ok += 1 if unit_ok_flag else 0

        detail_rows.append(
            {
                "task_id": row.get("task_id", ""),
                "sample_id": row.get("sample_id", ""),
                "short_name": row.get("short_name", ""),
                "field_id": row.get("field_id", ""),
                "dimension": row.get("dimension", ""),
                "metric_name_cn": row.get("metric_name_cn", ""),
                "label_source": source,
                "claim_level": "held_out_gold_accuracy" if source == "gold" else "proxy_not_true_accuracy",
                "label_status": label_status,
                "original_silver_status": row.get("silver_status", ""),
                "candidate_status": row.get("candidate_status", ""),
                "presence_outcome": outcome,
                "candidate_value": row.get("value_candidate", ""),
                "label_value": label_value_from_row(row, source),
                "raw_value_match": raw_match,
                "unit_normalized_value_match": unit_normalized_match,
                "candidate_unit": row.get("unit_raw_candidate", ""),
                "label_unit": label_unit_from_row(row, source),
                "unit_match": unit_match,
                "candidate_value_normalized": candidate_value_normalized,
                "label_value_normalized": label_value_normalized,
                "value_match_reason": value_match_reason,
                "unit_match_reason": unit_match_reason,
                "diagnosis": row.get("diagnosis", ""),
                "risk_bucket": row.get("risk_bucket", ""),
                "auto_verification_status": row.get("auto_verification_status", ""),
                "evidence_guard_status": row.get("evidence_guard_status", ""),
                "evidence_guard_audit_status": row.get("evidence_guard_audit_status", ""),
                "evidence_type_candidate": row.get("evidence_type_candidate", ""),
                "source_page": row.get("source_page", ""),
                "source_text": row.get("source_text", ""),
            }
        )

    value_raw_ci = wilson(value_raw_ok, value_total)
    value_unit_norm_ci = wilson(value_unit_normalized_ok, value_total)
    unit_ci = wilson(unit_ok, unit_total)
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "script_version": "evaluate_blind_quant_validation_tasks_unit_normalized_v1.0",
        "label_source": source,
        "claim_level": "held_out_gold_accuracy" if source == "gold" else "proxy_not_true_accuracy",
        "evaluated_rows": sum(presence_counts.values()),
        "label_status_counts": dict(label_status_counts),
        "original_silver_status_counts": dict(original_silver_status_counts),
        "presence_counts": dict(presence_counts),
        **pack_counts(presence_counts),
        "raw_value_accuracy": {
            "ok": value_raw_ok,
            "total": value_total,
            **value_raw_ci,
        },
        "unit_normalized_value_accuracy": {
            "ok": value_unit_normalized_ok,
            "total": value_total,
            **value_unit_norm_ci,
        },
        "unit_accuracy": {
            "ok": unit_ok,
            "total": unit_total,
            **unit_ci,
        },
        "note": (
            "Silver-label evaluation is an evidence/proxy audit signal and must not be "
            "claimed as final held-out gold accuracy."
            if source == "silver"
            else "Gold evaluation is valid only for rows with completed independent gold labels."
        ),
    }
    group_rows = make_group_rows(groups)
    return detail_rows, group_rows, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks-csv", type=Path, required=True)
    parser.add_argument("--label-source", choices=["gold", "silver"], default="gold")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--run-id", default="blind_quant_unit_normalized_eval")
    parser.add_argument("--relative-tolerance", type=float, default=0.01)
    parser.add_argument("--absolute-tolerance", type=float, default=1e-6)
    parser.add_argument("--include-not-applicable", action="store_true")
    args = parser.parse_args()

    rows = load_rows(args.tasks_csv)
    details, groups, summary = evaluate(
        rows,
        args.label_source,
        args.relative_tolerance,
        args.absolute_tolerance,
        args.include_not_applicable,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    details_csv = args.out_dir / f"{args.run_id}_details.csv"
    groups_csv = args.out_dir / f"{args.run_id}_groups.csv"
    summary_json = args.out_dir / f"{args.run_id}_summary.json"
    report_md = args.out_dir / f"{args.run_id}_report.md"

    detail_fields = list(details[0].keys()) if details else [
        "task_id",
        "sample_id",
        "field_id",
        "label_source",
        "claim_level",
        "label_status",
        "candidate_status",
        "presence_outcome",
    ]
    group_fields = [
        "group",
        "name",
        "evaluated_rows",
        "tp",
        "fp",
        "fn",
        "tn",
        "precision",
        "recall",
        "f1",
    ]
    write_csv(details_csv, details, detail_fields)
    write_csv(groups_csv, groups, group_fields)
    summary.update(
        {
            "tasks_csv": str(args.tasks_csv),
            "details_csv": str(details_csv),
            "groups_csv": str(groups_csv),
            "summary_json": str(summary_json),
            "report_md": str(report_md),
        }
    )
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        f"# Blind Quant Unit-Normalized Evaluation ({args.label_source})",
        "",
        f"- claim_level: {summary['claim_level']}",
        f"- evaluated_rows: {summary['evaluated_rows']}",
        f"- TP/FP/FN/TN: {summary['presence_counts']}",
        f"- Precision / Recall / F1: {summary['precision']} / {summary['recall']} / {summary['f1']}",
        f"- Raw Value Accuracy: {summary['raw_value_accuracy']}",
        f"- Unit-Normalized Value Accuracy: {summary['unit_normalized_value_accuracy']}",
        f"- Unit Accuracy: {summary['unit_accuracy']}",
        "",
        summary["note"],
    ]
    report_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
