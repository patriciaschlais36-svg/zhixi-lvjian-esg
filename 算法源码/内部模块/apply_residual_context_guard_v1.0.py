# -*- coding: utf-8 -*-
"""Apply conservative residual context guards after unit/year guards.

This guard does not use gold labels. It targets a small set of production-safe
patterns exposed by residual proxy errors:

- unsupported zero case-count candidates for corruption/compliance cases;
- table-row units where the extractor dropped an intensity denominator;
- percentage metrics where a nearby count was captured instead of the percent;
- greenhouse-gas target-progress rows where a footnote number was captured.

Default mode is audit-only unless --output-csv is provided.
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


SCRIPT_VERSION = "apply_residual_context_guard_v1.0"
NUM_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")

CORRUPTION_TOKENS = ("腐败", "贪腐", "贪污", "商业贿赂", "廉洁", "违纪违法", "舞弊")
ZERO_TOKENS = ("0", "零", "未发生", "无", "没有", "不存在")


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


def has_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def append_reason(row: dict[str, str], reason: str) -> None:
    row["review_reason"] = ((row.get("review_reason", "") + " | ") if row.get("review_reason") else "") + reason


def block_candidate(row: dict[str, str], status: str, reason: str) -> dict[str, str]:
    out = dict(row)
    out["candidate_status"] = "no_candidate"
    out["candidate_disclosure_class"] = "no_candidate"
    out["value_status"] = status
    out["recommended_next_status"] = "residual_context_guard_review"
    out["needs_llm_review"] = "yes"
    out["precision_gate_status"] = "blocked"
    if "precision_gate_category" in out:
        out["precision_gate_category"] = "residual_context_mismatch"
    if "precision_gate_rule" in out:
        out["precision_gate_rule"] = SCRIPT_VERSION
    append_reason(out, reason)
    if "precision_gate_reason" in out:
        out["precision_gate_reason"] = out.get("precision_gate_reason") or reason
    return out


def extract_intensity_unit(row: dict[str, str]) -> str:
    text = row.get("source_text", "")
    metric = row.get("metric_name_cn", "")
    if "强度" not in metric:
        return ""
    row_matches = re.findall(r"table_[^:]{0,40}:\s*([^|]+)\|\s*([^|]+)\|", text)
    for row_name, unit in row_matches:
        unit = unit.strip()
        if "强度" in row_name and ("/" in unit or "万元" in unit or "营收" in unit or "产值" in unit):
            return unit
    return ""


def extract_independent_director_percent(row: dict[str, str]) -> str:
    text = row.get("source_text", "")
    if row.get("field_id") != "G_Q_003":
        return ""
    if "独立董事" not in text or "占比" not in text:
        return ""
    window_match = re.search(r"独立董事.{0,80}?占比.{0,80}", text)
    window = window_match.group(0) if window_match else text
    percent_values = re.findall(r"(\d+(?:\.\d+)?)\s*%", window)
    if len(percent_values) == 1:
        value = percent_values[0]
        try:
            number = float(value)
        except ValueError:
            return ""
        if 0 <= number <= 100:
            return parse_number(value)
    return ""


def extract_ghg_total_progress(row: dict[str, str]) -> str:
    text = row.get("source_text", "")
    if row.get("field_id") != "E_Q_001":
        return ""
    if "温室气体" not in text or "总排放量进展" not in text:
        return ""
    match = re.search(r"总排放量进展[（(]?\s*20\d{2}\s*年?\s*[）)]?\s*(?:不超过)?\s*(-?\d+(?:,\d{3})*(?:\.\d+)?)", text)
    if not match:
        return ""
    return parse_number(match.group(1))


def has_standalone_waste_total(text: str) -> bool:
    for match in re.finditer(r"废弃物产生总量\s*(?:\||吨|千克|kg|[0-9])", text, flags=re.IGNORECASE):
        prefix = text[max(0, match.start() - 8) : match.start()]
        if not any(token in prefix for token in ("有害", "无害", "一般")):
            return True
    return False


def decide(row: dict[str, str]) -> tuple[str, str, dict[str, str]]:
    if row.get("candidate_status") != "candidate_found":
        return "kept", "", dict(row)

    field_id = row.get("field_id", "")
    source = row.get("source_text", "")
    value = row.get("value_candidate", "")
    unit = row.get("unit_raw_candidate", "")

    if field_id == "G_Q_010" and numbers_equal(value, "0"):
        if not (has_any(source, CORRUPTION_TOKENS) and has_any(source, ZERO_TOKENS)):
            reason = "residual_context_guard blocked unsupported zero case-count candidate without explicit corruption-case zero evidence"
            return "blocked_unsupported_zero_case_count", reason, block_candidate(row, "residual_context_guard_blocked", reason)

    if field_id == "S_Q_005" and ("人均薪酬" in source or "薪酬" in source) and "培训" not in source[: max(source.find("人均薪酬"), 0)]:
        reason = "residual_context_guard blocked training-hours candidate sourced from compensation context"
        return "blocked_training_metric_from_compensation_context", reason, block_candidate(row, "residual_context_guard_blocked", reason)

    if field_id in {"E_Q_002", "E_Q_003"} and ("直接减少的温室气体排放量" in source or "减少的温室气体排放量" in source):
        reason = "residual_context_guard blocked emission-scope candidate sourced from avoided/reduced-emission context"
        return "blocked_emission_scope_from_reduction_context", reason, block_candidate(row, "residual_context_guard_blocked", reason)

    if field_id == "E_Q_012" and ("一般固废" in source or "无害废弃物" in source or "有害废弃物" in source):
        if not has_standalone_waste_total(source):
            reason = "residual_context_guard blocked waste-total candidate sourced only from waste subcategory context"
            return "blocked_waste_total_from_subcategory_context", reason, block_candidate(row, "residual_context_guard_blocked", reason)

    corrected = dict(row)
    intensity_unit = extract_intensity_unit(row)
    if intensity_unit and "/" not in str(unit) and intensity_unit != unit:
        corrected["unit_raw_candidate"] = intensity_unit
        corrected["unit_standardized_candidate"] = intensity_unit
        corrected["residual_context_guard_status"] = "corrected_unit_from_table_row"
        corrected["residual_context_guard_reason"] = f"table row unit includes denominator: {intensity_unit}"
        corrected["residual_context_guard_original_unit_raw_candidate"] = unit
        append_reason(corrected, f"residual_context_guard corrected unit {unit} -> {intensity_unit}")
        return "corrected_unit_from_table_row", corrected["residual_context_guard_reason"], corrected

    percent = extract_independent_director_percent(row)
    if percent and not numbers_equal(percent, value):
        original = value
        corrected["value_candidate"] = percent
        corrected["value_standardized_candidate"] = percent
        corrected["unit_raw_candidate"] = "%"
        corrected["unit_standardized_candidate"] = "%"
        corrected["residual_context_guard_status"] = "corrected_percent_from_occupancy_context"
        corrected["residual_context_guard_reason"] = "independent-director ratio row contains explicit percentage"
        corrected["residual_context_guard_original_value_candidate"] = original
        append_reason(corrected, f"residual_context_guard corrected value {original} -> {percent}")
        return "corrected_percent_from_occupancy_context", corrected["residual_context_guard_reason"], corrected

    progress = extract_ghg_total_progress(row)
    if progress and not numbers_equal(progress, value):
        original = value
        corrected["value_candidate"] = progress
        corrected["value_standardized_candidate"] = progress
        corrected["residual_context_guard_status"] = "corrected_value_from_ghg_total_progress"
        corrected["residual_context_guard_reason"] = "GHG total-progress row contains actual report-year total"
        corrected["residual_context_guard_original_value_candidate"] = original
        append_reason(corrected, f"residual_context_guard corrected value {original} -> {progress}")
        return "corrected_value_from_ghg_total_progress", corrected["residual_context_guard_reason"], corrected

    return "kept", "", dict(row)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--audit-csv", type=Path, required=True)
    parser.add_argument("--summary-json", type=Path, required=True)
    parser.add_argument("--report-md", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path)
    args = parser.parse_args()

    rows, fields = load_csv(args.input_csv)
    extra_fields = [
        "residual_context_guard_status",
        "residual_context_guard_reason",
        "residual_context_guard_original_value_candidate",
        "residual_context_guard_original_unit_raw_candidate",
    ]
    output_fields = list(fields)
    for field in extra_fields:
        if field not in output_fields:
            output_fields.append(field)

    out_rows: list[dict[str, str]] = []
    audit_rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for row in rows:
        status, reason, out = decide(row)
        counts[status] += 1
        if status == "kept":
            out["residual_context_guard_status"] = "kept"
            out["residual_context_guard_reason"] = ""
            out["residual_context_guard_original_value_candidate"] = ""
            out["residual_context_guard_original_unit_raw_candidate"] = ""
        else:
            out.setdefault("residual_context_guard_status", status)
            out.setdefault("residual_context_guard_reason", reason)
        out_rows.append(out)
        if status != "kept":
            audit_rows.append(
                {
                    "sample_id": row.get("sample_id", ""),
                    "short_name": row.get("short_name", ""),
                    "field_id": row.get("field_id", ""),
                    "metric_name_cn": row.get("metric_name_cn", ""),
                    "status": status,
                    "reason": reason,
                    "old_candidate_status": row.get("candidate_status", ""),
                    "old_value_candidate": row.get("value_candidate", ""),
                    "old_unit_raw_candidate": row.get("unit_raw_candidate", ""),
                    "new_candidate_status": out.get("candidate_status", ""),
                    "new_value_candidate": out.get("value_candidate", ""),
                    "new_unit_raw_candidate": out.get("unit_raw_candidate", ""),
                    "source_page": row.get("source_page", ""),
                    "source_text": " ".join(row.get("source_text", "").split())[:700],
                }
            )

    if args.output_csv:
        write_csv(args.output_csv, out_rows, output_fields)
    audit_fields = [
        "sample_id", "short_name", "field_id", "metric_name_cn", "status", "reason",
        "old_candidate_status", "old_value_candidate", "old_unit_raw_candidate",
        "new_candidate_status", "new_value_candidate", "new_unit_raw_candidate",
        "source_page", "source_text",
    ]
    write_csv(args.audit_csv, audit_rows, audit_fields)
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "script_version": SCRIPT_VERSION,
        "input_csv": str(args.input_csv),
        "output_csv": str(args.output_csv or ""),
        "input_rows": len(rows),
        "changed_rows": len(audit_rows),
        "status_counts": dict(counts),
        "note": "Conservative residual context guard; does not use gold labels and does not prove true accuracy.",
    }
    args.summary_json.parent.mkdir(parents=True, exist_ok=True)
    args.summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Residual Context Guard Report",
        "",
        f"- generated_at: {summary['generated_at']}",
        f"- input_rows: {summary['input_rows']}",
        f"- changed_rows: {summary['changed_rows']}",
        "",
        "## Status Counts",
        "",
        "| status | count |",
        "|---|---:|",
    ]
    for key, value in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {key} | {value} |")
    lines.extend(["", "## Boundary", "", "- This guard does not use gold labels and does not prove true accuracy."])
    args.report_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
