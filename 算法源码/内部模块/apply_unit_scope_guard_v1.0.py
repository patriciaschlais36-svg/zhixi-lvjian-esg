# -*- coding: utf-8 -*-
"""Conservative unit/scope guard for quantitative ESG extraction candidates.

The guard is designed for production safety:

- Default behavior is audit-only unless --output-csv is provided.
- It only blocks candidate_found rows when the candidate unit is clearly
  incompatible with the indicator's accepted unit family.
- It treats intensity denominators as material. For example, an emissions
  intensity metric that expects "tCO2e/万元" must not accept plain "tCO2e".

This script does not use gold labels and does not estimate true accuracy. It
adds auditable rule evidence that can be used before candidate quality reports,
auto verification, scoring, and dashboard generation.
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


SCRIPT_VERSION = "apply_unit_scope_guard_v1.0"

BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_INDICATOR = BASE_DIR / "算法源码" / "配置" / "ESG指标体系.csv"

UNIT_SPLIT_RE = re.compile(r"[;；,，、|]+")


def load_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def norm_text(value: str) -> str:
    text = str(value or "").strip().lower()
    replacements = {
        "（": "(",
        "）": ")",
        "／": "/",
        "％": "%",
        "每": "/",
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
        " ": "",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def split_units(value: str) -> list[str]:
    units: list[str] = []
    for part in UNIT_SPLIT_RE.split(str(value or "")):
        part = part.strip()
        if part:
            units.append(part)
    return units


def accepted_units(indicator: dict[str, str]) -> list[str]:
    units = split_units(indicator.get("units_accepted_raw", ""))
    normalized = indicator.get("unit_normalized", "").strip()
    if normalized:
        units.append(normalized)
    return units


def has_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def has_denominator(text: str) -> bool:
    return "/" in text or "per" in text or "单位" in text or "强度" in text


def unit_family(unit: str) -> str:
    text = norm_text(unit)
    if not text:
        return "blank"

    denominator = has_denominator(text)
    is_percent = "%" in text or "比例" in text or "占比" in text or "率" == text
    is_money = has_any(text, ("万元", "亿元", "元"))
    is_emission = has_any(text, ("co2", "tco2", "吨co2e", "碳"))
    is_energy = has_any(text, ("kwh", "mwh", "gwh", "吉焦", "gj", "吨标煤"))
    is_water = has_any(text, ("m3", "吨水"))
    is_mass = has_any(text, ("万吨", "吨", "kg", "千克"))
    is_hours = "小时" in text or "人时" in text
    is_person = "人" in text and not is_hours
    is_count = has_any(text, ("件", "次", "场", "项", "家"))

    if denominator:
        if is_emission:
            return "emission_intensity"
        if is_energy:
            return "energy_intensity"
        if is_water:
            return "water_intensity"
        if is_money:
            return "money_intensity"
        if is_hours:
            return "hours_intensity"
        if is_mass:
            return "mass_intensity"
        return "ratio_or_intensity"
    if is_percent:
        return "percentage"
    if is_emission:
        return "emission_mass"
    if is_energy:
        return "energy"
    if is_water:
        return "water_volume"
    if is_money:
        return "money"
    if is_hours:
        return "hours"
    if is_person:
        return "person"
    if is_count:
        return "count"
    if is_mass:
        return "mass"
    return "other"


def raw_unit_allowed(candidate_unit: str, units: list[str]) -> bool:
    raw = norm_text(candidate_unit)
    if not raw:
        return False
    for unit in units:
        norm = norm_text(unit)
        if norm and (raw == norm or raw in norm or norm in raw):
            return True
    return False


def compatible_family(candidate: str, expected: str) -> bool:
    if candidate == expected:
        return True
    groups = [
        {"emission_mass", "mass"},
        {"person", "count"},
        {"hours", "hours_intensity"},
        {"percentage", "ratio_or_intensity"},
        {"money"},
        {"energy"},
        {"water_volume"},
    ]
    return any(candidate in group and expected in group for group in groups)


def expectation_profile(indicator: dict[str, str]) -> dict[str, Any]:
    units = accepted_units(indicator)
    families = sorted({unit_family(unit) for unit in units if unit_family(unit) != "blank"})
    return {
        "accepted_units": units,
        "accepted_families": families,
        "expects_intensity": any(family.endswith("_intensity") or family == "ratio_or_intensity" for family in families),
        "expects_only_intensity": bool(families)
        and all(family.endswith("_intensity") or family == "ratio_or_intensity" for family in families),
    }


def should_block(row: dict[str, str], indicator: dict[str, str]) -> tuple[bool, str, str, str, str]:
    status = row.get("candidate_status", "")
    metric_type = row.get("metric_type", indicator.get("metric_type", ""))
    if status != "candidate_found" or metric_type != "quantitative":
        return False, "not_applicable", "", "", ""

    unit = row.get("unit_raw_candidate", "")
    profile = expectation_profile(indicator)
    families = profile["accepted_families"]
    unit_family_candidate = unit_family(unit)
    accepted_units_text = ";".join(profile["accepted_units"])
    accepted_family_text = ";".join(families)

    if not families:
        return False, "no_indicator_unit_profile", unit_family_candidate, accepted_family_text, accepted_units_text
    if raw_unit_allowed(unit, profile["accepted_units"]):
        return False, "unit_explicitly_allowed", unit_family_candidate, accepted_family_text, accepted_units_text
    if unit_family_candidate == "blank":
        return False, "blank_unit_audit_only", unit_family_candidate, accepted_family_text, accepted_units_text

    if profile["expects_only_intensity"] and not (
        unit_family_candidate.endswith("_intensity") or unit_family_candidate == "ratio_or_intensity"
    ):
        return True, "missing_denominator_for_intensity_metric", unit_family_candidate, accepted_family_text, accepted_units_text

    if not any(compatible_family(unit_family_candidate, family) for family in families):
        return True, "incompatible_unit_family", unit_family_candidate, accepted_family_text, accepted_units_text

    return False, "compatible_unit_family", unit_family_candidate, accepted_family_text, accepted_units_text


def guarded_row(row: dict[str, str], reason: str, candidate_family: str, expected_families: str, accepted_units_text: str) -> dict[str, str]:
    out = dict(row)
    previous_status = row.get("candidate_status", "")
    out["unit_scope_guard_status"] = "blocked"
    out["unit_scope_guard_reason"] = reason
    out["unit_scope_guard_candidate_family"] = candidate_family
    out["unit_scope_guard_expected_families"] = expected_families
    out["unit_scope_guard_accepted_units"] = accepted_units_text
    out["unit_scope_guard_original_candidate_status"] = previous_status
    out["unit_scope_guard_original_value_candidate"] = row.get("value_candidate", "")
    out["unit_scope_guard_original_unit_raw_candidate"] = row.get("unit_raw_candidate", "")
    out["candidate_status"] = "no_candidate"
    out["candidate_disclosure_class"] = "no_candidate"
    out["value_status"] = "unit_scope_guard_blocked"
    out["recommended_next_status"] = "unit_scope_guard_review"
    out["needs_llm_review"] = "yes"
    out["review_reason"] = (
        (row.get("review_reason", "") + " | ") if row.get("review_reason") else ""
    ) + f"unit_scope_guard blocked: {reason}; candidate_family={candidate_family}; expected={expected_families}"
    if "precision_gate_status" in out:
        out["precision_gate_status"] = out.get("precision_gate_status") or "blocked"
    if "precision_gate_category" in out:
        out["precision_gate_category"] = out.get("precision_gate_category") or "unit_scope_mismatch"
    if "precision_gate_rule" in out:
        out["precision_gate_rule"] = out.get("precision_gate_rule") or SCRIPT_VERSION
    if "precision_gate_reason" in out:
        out["precision_gate_reason"] = out.get("precision_gate_reason") or out["review_reason"]
    return out


def pass_row(row: dict[str, str], reason: str, candidate_family: str, expected_families: str, accepted_units_text: str) -> dict[str, str]:
    out = dict(row)
    out["unit_scope_guard_status"] = "kept"
    out["unit_scope_guard_reason"] = reason
    out["unit_scope_guard_candidate_family"] = candidate_family
    out["unit_scope_guard_expected_families"] = expected_families
    out["unit_scope_guard_accepted_units"] = accepted_units_text
    out["unit_scope_guard_original_candidate_status"] = row.get("candidate_status", "")
    out["unit_scope_guard_original_value_candidate"] = ""
    out["unit_scope_guard_original_unit_raw_candidate"] = ""
    return out


def audit_and_apply(
    rows: list[dict[str, str]],
    fieldnames: list[str],
    indicators: dict[str, dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, Any], list[str]]:
    audit_rows: list[dict[str, Any]] = []
    output_rows: list[dict[str, str]] = []
    counts: Counter[str] = Counter()
    by_reason: Counter[str] = Counter()
    by_field: Counter[str] = Counter()
    by_family_pair: Counter[str] = Counter()

    extra_fields = [
        "unit_scope_guard_status",
        "unit_scope_guard_reason",
        "unit_scope_guard_candidate_family",
        "unit_scope_guard_expected_families",
        "unit_scope_guard_accepted_units",
        "unit_scope_guard_original_candidate_status",
        "unit_scope_guard_original_value_candidate",
        "unit_scope_guard_original_unit_raw_candidate",
    ]
    output_fields = list(fieldnames)
    for field in extra_fields:
        if field not in output_fields:
            output_fields.append(field)

    for row in rows:
        indicator = indicators.get(row.get("field_id", ""), {})
        block, reason, candidate_family, expected_families, accepted_units_text = should_block(row, indicator)
        status = "blocked" if block else "kept"
        counts[status] += 1
        by_reason[reason] += 1
        family_pair = f"{candidate_family}->{expected_families}"
        by_family_pair[family_pair] += 1
        if block:
            by_field[row.get("field_id", "")] += 1
        audit_rows.append(
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
                "unit_scope_guard_status": status,
                "unit_scope_guard_reason": reason,
                "candidate_family": candidate_family,
                "expected_families": expected_families,
                "accepted_units": accepted_units_text,
                "source_text_preview": (row.get("source_text", "") or row.get("source_table_cell", ""))[:260],
            }
        )
        output_rows.append(
            guarded_row(row, reason, candidate_family, expected_families, accepted_units_text)
            if block
            else pass_row(row, reason, candidate_family, expected_families, accepted_units_text)
        )

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "script_version": SCRIPT_VERSION,
        "row_count": len(rows),
        "candidate_found_rows": sum(1 for row in rows if row.get("candidate_status") == "candidate_found"),
        "blocked_rows": counts["blocked"],
        "kept_rows": counts["kept"],
        "guard_status_counts": dict(counts),
        "reason_counts": dict(by_reason),
        "blocked_by_field": dict(by_field.most_common(50)),
        "family_pair_counts": dict(by_family_pair.most_common(50)),
        "note": "This is a conservative unit/scope guard. It is not a gold-label evaluation and does not estimate true accuracy.",
    }
    return audit_rows, output_rows, summary, output_fields


def write_report(path: Path, summary: dict[str, Any], audit_csv: Path, output_csv: Path | None) -> None:
    lines = [
        "# Unit Scope Guard Report",
        "",
        f"- generated_at: {summary['generated_at']}",
        f"- script_version: {summary['script_version']}",
        f"- row_count: {summary['row_count']}",
        f"- candidate_found_rows: {summary['candidate_found_rows']}",
        f"- blocked_rows: {summary['blocked_rows']}",
        f"- kept_rows: {summary['kept_rows']}",
        f"- audit_csv: `{audit_csv}`",
        f"- output_csv: `{output_csv or ''}`",
        "",
        "## Reason Counts",
        "",
        "| reason | count |",
        "|---|---:|",
    ]
    for key, value in sorted(summary["reason_counts"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {key} | {value} |")
    lines.extend(["", "## Blocked By Field", "", "| field_id | count |", "|---|---:|"])
    for key, value in summary["blocked_by_field"].items():
        lines.append(f"| {key} | {value} |")
    lines.extend(["", "## Boundary", ""])
    lines.append("- This report is a rule audit/guard, not a true gold accuracy result.")
    lines.append("- Blocked rows should be inspected through source evidence or DeepSeek review before broadening the rule.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--indicator-csv", type=Path, default=DEFAULT_INDICATOR)
    parser.add_argument("--audit-csv", type=Path, required=True)
    parser.add_argument("--summary-json", type=Path, required=True)
    parser.add_argument("--report-md", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, help="Optional guarded extraction CSV. If omitted, audit-only.")
    args = parser.parse_args()

    rows, fields = load_rows(args.input_csv)
    indicator_rows, _ = load_rows(args.indicator_csv)
    indicators = {row.get("field_id", ""): row for row in indicator_rows}
    audit_rows, output_rows, summary, output_fields = audit_and_apply(rows, fields, indicators)
    args.audit_csv.parent.mkdir(parents=True, exist_ok=True)
    audit_fields = [
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
        "unit_scope_guard_status",
        "unit_scope_guard_reason",
        "candidate_family",
        "expected_families",
        "accepted_units",
        "source_text_preview",
    ]
    write_csv(args.audit_csv, audit_rows, audit_fields)
    if args.output_csv:
        write_csv(args.output_csv, output_rows, output_fields)
        summary["output_csv"] = str(args.output_csv)
    else:
        summary["output_csv"] = ""
    summary.update(
        {
            "input_csv": str(args.input_csv),
            "indicator_csv": str(args.indicator_csv),
            "audit_csv": str(args.audit_csv),
            "summary_json": str(args.summary_json),
            "report_md": str(args.report_md),
        }
    )
    args.summary_json.parent.mkdir(parents=True, exist_ok=True)
    args.summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(args.report_md, summary, args.audit_csv, args.output_csv)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
