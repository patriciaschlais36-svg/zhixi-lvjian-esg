# -*- coding: utf-8 -*-
"""Audit gold/extraction conflicts and multi-year table value alignment.

The script is read-only. It does not change extraction results or gold labels.

Why this exists:
- A gold row can say "not_disclosed" while a report table clearly contains a
  metric row and a target-year value. Treating that as a normal FP would train
  the precision gate in the wrong direction.
- Many ESG KPI tables contain 2023/2024/2025 columns. If the extractor chooses
  the 2024 value in a 2025 report, the field is found but value/year alignment
  is wrong. That should drive table-year parsing repair, not blanket blocking.
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
DEFAULT_DETAILS = (
    BASE_DIR
    / "评估测试"
    / "extended_gold_evaluation_v2.31c_goldproposal_gate_P0_R029_reviewed_v1.2_whatif"
    / "extended_gold_evaluation_details_v1.0.csv"
)
DEFAULT_GOLD = (
    BASE_DIR
    / "数据集与标注"
    / "gold_label_plan"
    / "扩展金标P0指标标注子任务_用户核验v1.2_R029复核更新.csv"
)
DEFAULT_EXTRACTION = (
    BASE_DIR
    / "算法方案"
    / "pilot_full_extraction_v2.15_200samples_pipeline_guarded"
    / "全量指标候选抽取结果_200份v2.31c_goldproposal_gate_whatif.csv"
)
DEFAULT_INDICATORS = BASE_DIR / "算法源码" / "配置" / "ESG指标体系.csv"
DEFAULT_OUT_DIR = BASE_DIR / "评估测试" / "gold_conflict_year_audit_v2.36"


YEAR_RE = re.compile(r"20\d{2}")
NUM_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")
HEADER_RE = re.compile(
    r"(?:单位|unit)?\s*((?:20\d{2}\s*年?\s*){2,5})",
    flags=re.IGNORECASE,
)
BAD_ALIAS_TOKENS = {"", "不涉及", "无", "其他", "ESG", "CSR"}


def load_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def compact_text(value: str, limit: int = 800) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def normalize_number(value: Any) -> str:
    text = str(value or "").replace(",", "").strip()
    match = NUM_RE.search(text)
    if not match:
        return ""
    try:
        number = float(match.group(0).replace(",", ""))
    except ValueError:
        return ""
    return f"{number:.10g}"


def is_same_number(left: Any, right: Any, tolerance: float = 1e-6) -> bool:
    left_norm = normalize_number(left)
    right_norm = normalize_number(right)
    if not left_norm or not right_norm:
        return False
    return abs(float(left_norm) - float(right_norm)) <= tolerance


def split_aliases(row: dict[str, str]) -> list[str]:
    raw_values = [
        row.get("metric_name_cn", ""),
        row.get("aliases_cn", ""),
        row.get("metric_name_en", ""),
        row.get("aliases_en", ""),
    ]
    terms: list[str] = []
    for raw in raw_values:
        for term in re.split(r"[;；,，/、|]", str(raw or "")):
            term = term.strip()
            if term and term not in BAD_ALIAS_TOKENS and len(term) >= 2:
                terms.append(term)
                if "投入" in term:
                    terms.append(term.replace("投入", "投资"))
                if term == "人均培训时长":
                    terms.extend(["员工接受培训的人均时长", "员工培训人均时长", "人均时长"])
    # Prefer longer terms. Short aliases such as "环保投入" are still useful
    # but should not hide exact metric names.
    deduped = sorted(set(terms), key=lambda item: (-len(item), item))
    return deduped


def indicator_index(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {row.get("field_id", ""): row for row in rows if row.get("field_id")}


def row_index(rows: list[dict[str, str]]) -> dict[tuple[str, str], dict[str, str]]:
    return {(row.get("sample_id", ""), row.get("field_id", "")): row for row in rows}


def extraction_sort_key(row: dict[str, str]) -> tuple[int, float, int]:
    status_score = 1 if row.get("candidate_status") == "candidate_found" else 0
    try:
        confidence = float(row.get("confidence_rule", "") or 0)
    except ValueError:
        confidence = 0.0
    rank = str(row.get("candidate_rank", "") or "").strip()
    rank_score = 1 if rank in {"", "1", "1.0"} else 0
    return status_score, confidence, rank_score


def extraction_groups(rows: list[dict[str, str]]) -> dict[tuple[str, str], list[dict[str, str]]]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in rows:
        key = (row.get("sample_id", ""), row.get("field_id", ""))
        groups.setdefault(key, []).append(row)
    for key in groups:
        groups[key].sort(key=extraction_sort_key, reverse=True)
    return groups


def parse_report_year(row: dict[str, str]) -> str:
    for field in ("report_year", "pdf_path", "source_text", "report_type"):
        text = str(row.get(field, "") or "")
        match = re.search(r"[_\-](20\d{2})[_\-#]", text)
        if match:
            return match.group(1)
    for field in ("pdf_path", "source_text"):
        text = str(row.get(field, "") or "")
        years = YEAR_RE.findall(text)
        if years:
            # In local filenames the report year is usually the first year.
            return years[0]
    return ""


def extract_header_years(text: str, before_pos: int | None = None) -> list[str]:
    found: list[tuple[int, list[str]]] = []
    for match in HEADER_RE.finditer(text):
        if before_pos is not None:
            if match.end() > before_pos:
                continue
            if before_pos - match.start() > 1200:
                continue
        years = YEAR_RE.findall(match.group(1))
        if len(years) >= 2:
            found.append((match.start(), years))
    if found:
        # Prefer the nearest preceding header; ties go to the longer year run.
        return max(found, key=lambda item: (item[0], len(item[1])))[1]

    # Fallback: detect a compact run of nearby years.
    years = []
    for match in YEAR_RE.finditer(text):
        if before_pos is not None and match.end() > before_pos:
            continue
        years.append((match.group(0), match.start()))
    for i, (year, start) in enumerate(years):
        run = [year]
        last_pos = start
        for next_year, next_pos in years[i + 1 : i + 5]:
            if next_pos - last_pos > 40:
                break
            run.append(next_year)
            last_pos = next_pos
        if len(run) >= 2:
            if before_pos is not None and before_pos - start > 1200:
                continue
            return run
    return []


def fuzzy_term_in_text(term: str, text: str) -> bool:
    if term in text:
        return True
    # A small Chinese substring fallback catches 投入/投资, 管理/管控 variants
    # without bringing in a heavy tokenizer.
    if len(term) >= 4:
        for size in range(min(len(term), 6), 2, -1):
            for i in range(0, len(term) - size + 1):
                piece = term[i : i + size]
                if piece in text:
                    return True
    return False


def find_metric_window(text: str, terms: list[str]) -> tuple[str, str, int]:
    best_term = ""
    best_pos = -1
    for term in terms:
        pos = text.find(term)
        if pos >= 0 and (best_pos < 0 or pos < best_pos):
            best_term = term
            best_pos = pos
    if best_pos < 0:
        # Try fuzzy pieces and keep the first direct occurrence.
        for term in terms:
            if len(term) < 4:
                continue
            for size in range(min(len(term), 6), 2, -1):
                for i in range(0, len(term) - size + 1):
                    piece = term[i : i + size]
                    pos = text.find(piece)
                    if pos >= 0 and (best_pos < 0 or pos < best_pos):
                        best_term = piece
                        best_pos = pos
    if best_pos < 0:
        return "", "", -1

    window = text[best_pos : best_pos + 220]
    return best_term, window, best_pos


def parse_row_values(metric_window: str, years: list[str]) -> list[str]:
    values = [match.group(0).replace(",", "") for match in NUM_RE.finditer(metric_window)]
    # Years can leak into the window if the metric occurrence is near a repeated
    # header. Drop year-like tokens first.
    values = [value for value in values if value not in years and not re.fullmatch(r"20\d{2}", value)]
    if len(values) < len(years):
        return []
    return values[: len(years)]


def analyze_year_alignment(
    row: dict[str, str],
    indicator: dict[str, str],
    report_year: str,
) -> dict[str, Any]:
    text = compact_text(row.get("source_text", "") or row.get("source_table_cell", ""), 5000)
    terms = split_aliases(indicator)
    if not text or not terms:
        return {}

    matched_term, metric_window, metric_pos = find_metric_window(text, terms)
    if not metric_window:
        return {}

    years = extract_header_years(text, before_pos=metric_pos)
    if len(years) < 2 or report_year not in years:
        return {}

    row_values = parse_row_values(metric_window, years)
    if len(row_values) != len(years):
        return {}

    algo_value = row.get("value_candidate", "")
    candidate_index = next((idx for idx, value in enumerate(row_values) if is_same_number(value, algo_value)), None)
    target_index = years.index(report_year)
    target_value = row_values[target_index]

    result: dict[str, Any] = {
        "header_years": ";".join(years),
        "target_report_year": report_year,
        "matched_metric_term": matched_term,
        "parsed_row_values_by_year": ";".join(f"{year}:{value}" for year, value in zip(years, row_values)),
        "target_year_value": target_value,
        "metric_window": compact_text(metric_window, 420),
    }

    if candidate_index is not None:
        result["candidate_value_year"] = years[candidate_index]
        result["candidate_value_in_row"] = row_values[candidate_index]
        if years[candidate_index] != report_year and not is_same_number(target_value, algo_value):
            result["year_alignment_issue"] = "candidate_value_maps_to_non_target_year"
            result["suggested_value"] = target_value
        else:
            result["year_alignment_issue"] = "candidate_value_matches_target_year"
            result["suggested_value"] = ""
    else:
        result["candidate_value_year"] = ""
        result["candidate_value_in_row"] = ""
        result["year_alignment_issue"] = "candidate_value_not_found_in_parsed_metric_row"
        result["suggested_value"] = target_value
    return result


def get_best_row_for_detail(
    detail: dict[str, str],
    grouped: dict[tuple[str, str], list[dict[str, str]]],
) -> dict[str, str]:
    rows = grouped.get((detail.get("sample_id", ""), detail.get("field_id", "")), [])
    if not rows:
        return {}
    algo_value = normalize_number(detail.get("algo_value", ""))
    if algo_value:
        for row in rows:
            if row.get("candidate_status") == "candidate_found" and is_same_number(row.get("value_candidate", ""), algo_value):
                return row
    for row in rows:
        if row.get("candidate_status") == "candidate_found":
            return row
    return rows[0]


def classify_conflict(
    detail: dict[str, str],
    extraction: dict[str, str],
    indicator: dict[str, str],
    year_info: dict[str, Any],
) -> tuple[str, str, int]:
    outcome = detail.get("outcome", "")
    gold_status = detail.get("gold_status", "")
    algo_status = detail.get("algo_status", "")
    metric_type = detail.get("metric_type", "") or indicator.get("metric_type", "")
    text = extraction.get("source_text", "") or extraction.get("source_table_cell", "")
    terms = split_aliases(indicator)
    has_metric_cue = any(fuzzy_term_in_text(term, text) for term in terms)

    if (
        outcome == "FP"
        and gold_status in {"not_disclosed", "not_applicable", "not_found"}
        and algo_status == "candidate_found"
        and metric_type == "quantitative"
    ):
        if year_info.get("year_alignment_issue") == "candidate_value_maps_to_non_target_year":
            return "algorithm_year_column_mismatch_possible", "fix_table_year_resolution_before_precision_gate", 98
        if year_info.get("target_year_value"):
            return "gold_negative_but_target_year_metric_value_present", "review_gold_status_or_gold_value", 96
        if has_metric_cue:
            return "gold_negative_but_metric_evidence_present", "review_gold_or_build_narrow_negative_case", 88
        return "ordinary_quantitative_fp", "precision_gate_candidate", 75

    if (
        outcome == "FP"
        and gold_status in {"not_disclosed", "not_applicable", "not_found"}
        and algo_status == "candidate_found"
        and metric_type == "qualitative"
    ):
        if has_metric_cue:
            return "gold_negative_but_qualitative_evidence_present", "qualitative_rule_review_or_gold_review", 82
        return "ordinary_qualitative_fp", "precision_gate_candidate", 72

    if outcome == "TP" and metric_type == "quantitative":
        if year_info.get("year_alignment_issue") == "candidate_value_maps_to_non_target_year":
            return "tp_but_year_column_mismatch_possible", "value_year_accuracy_review", 94
        if detail.get("value_match") == "no":
            return "tp_value_mismatch", "value_normalization_or_year_review", 90

    if outcome == "FN":
        return "gold_positive_algo_missing", "recall_repair", 80

    return "", "", 0


def build_conflict_rows(
    details: list[dict[str, str]],
    gold: dict[tuple[str, str], dict[str, str]],
    grouped: dict[tuple[str, str], list[dict[str, str]]],
    indicators: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for detail in details:
        if detail.get("outcome") not in {"FP", "FN", "TP"}:
            continue
        field_id = detail.get("field_id", "")
        sample_id = detail.get("sample_id", "")
        indicator = indicators.get(field_id, {})
        gold_row = gold.get((sample_id, field_id), {})
        extraction = get_best_row_for_detail(detail, grouped)
        report_year = parse_report_year(extraction) or parse_report_year(gold_row)
        year_info = {}
        if extraction and (detail.get("metric_type") or indicator.get("metric_type")) == "quantitative":
            year_info = analyze_year_alignment(extraction, indicator, report_year)
        conflict_type, suggested_action, priority = classify_conflict(detail, extraction, indicator, year_info)
        if not conflict_type:
            continue

        row = {
            "case_id": f"GOLD_CONFLICT_{len(rows) + 1:04d}",
            "priority": priority,
            "sample_id": sample_id,
            "field_id": field_id,
            "short_name": detail.get("short_name", "") or gold_row.get("short_name", "") or extraction.get("short_name", ""),
            "metric_name_cn": detail.get("metric_name_cn", "") or indicator.get("metric_name_cn", ""),
            "metric_type": detail.get("metric_type", "") or indicator.get("metric_type", ""),
            "outcome": detail.get("outcome", ""),
            "value_match": detail.get("value_match", ""),
            "gold_status": detail.get("gold_status", "") or gold_row.get("gold_status", ""),
            "gold_value": detail.get("gold_value", "") or gold_row.get("gold_value", ""),
            "gold_unit_raw": detail.get("gold_unit_raw", "") or gold_row.get("gold_unit_raw", ""),
            "gold_source_page": gold_row.get("gold_source_page", ""),
            "gold_source_text": compact_text(gold_row.get("gold_source_text", ""), 500),
            "algo_status": detail.get("algo_status", "") or extraction.get("candidate_status", ""),
            "algo_value": detail.get("algo_value", "") or extraction.get("value_candidate", ""),
            "algo_unit": detail.get("algo_unit", "") or extraction.get("unit_raw_candidate", ""),
            "algo_source_page": extraction.get("source_page", ""),
            "algo_evidence_type": detail.get("algo_evidence_type", "") or extraction.get("evidence_type_candidate", ""),
            "algo_confidence": detail.get("algo_confidence", "") or extraction.get("confidence_rule", ""),
            "precision_gate_status": detail.get("precision_gate_status", "") or extraction.get("precision_gate_status", ""),
            "conflict_type": conflict_type,
            "suggested_action": suggested_action,
            "report_year": report_year,
            "header_years": year_info.get("header_years", ""),
            "candidate_value_year": year_info.get("candidate_value_year", ""),
            "target_year_value": year_info.get("target_year_value", ""),
            "suggested_value": year_info.get("suggested_value", ""),
            "parsed_row_values_by_year": year_info.get("parsed_row_values_by_year", ""),
            "matched_metric_term": year_info.get("matched_metric_term", ""),
            "metric_window": year_info.get("metric_window", ""),
            "algo_source_text": compact_text(extraction.get("source_text", "") or extraction.get("source_table_cell", ""), 800),
        }
        rows.append(row)
    rows.sort(key=lambda item: (-int(item.get("priority") or 0), item.get("sample_id", ""), item.get("field_id", "")))
    return rows


def build_global_year_rows(
    extraction_rows: list[dict[str, str]],
    indicators: dict[str, dict[str, str]],
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for extraction in extraction_rows:
        if extraction.get("candidate_status") != "candidate_found":
            continue
        indicator = indicators.get(extraction.get("field_id", ""), {})
        if indicator.get("metric_type") != "quantitative":
            continue
        report_year = parse_report_year(extraction)
        if not report_year:
            continue
        year_info = analyze_year_alignment(extraction, indicator, report_year)
        if year_info.get("year_alignment_issue") != "candidate_value_maps_to_non_target_year":
            continue
        rows.append(
            {
                "case_id": f"YEAR_AUDIT_{len(rows) + 1:05d}",
                "sample_id": extraction.get("sample_id", ""),
                "field_id": extraction.get("field_id", ""),
                "short_name": extraction.get("short_name", ""),
                "metric_name_cn": indicator.get("metric_name_cn", "") or extraction.get("metric_name_cn", ""),
                "value_candidate": extraction.get("value_candidate", ""),
                "unit_raw_candidate": extraction.get("unit_raw_candidate", ""),
                "source_page": extraction.get("source_page", ""),
                "evidence_type_candidate": extraction.get("evidence_type_candidate", ""),
                "confidence_rule": extraction.get("confidence_rule", ""),
                "report_year": report_year,
                "header_years": year_info.get("header_years", ""),
                "candidate_value_year": year_info.get("candidate_value_year", ""),
                "target_year_value": year_info.get("target_year_value", ""),
                "suggested_value": year_info.get("suggested_value", ""),
                "parsed_row_values_by_year": year_info.get("parsed_row_values_by_year", ""),
                "matched_metric_term": year_info.get("matched_metric_term", ""),
                "metric_window": year_info.get("metric_window", ""),
                "source_text": compact_text(extraction.get("source_text", "") or extraction.get("source_table_cell", ""), 800),
            }
        )
        if limit and len(rows) >= limit:
            break
    return rows


def write_report(path: Path, summary: dict[str, Any], conflict_rows: list[dict[str, Any]]) -> None:
    lines = [
        "# Gold Conflict And Year Alignment Audit v1.0",
        "",
        f"Generated: {summary['generated_at']}",
        "",
        "## Summary",
        "",
        f"- Evaluation detail rows: {summary['input_detail_rows']}",
        f"- Conflict/review rows: {summary['conflict_review_rows']}",
        f"- Global quantitative year mismatch candidates: {summary['global_year_mismatch_rows']}",
        "",
        "## Conflict Types",
        "",
        "| type | count |",
        "|---|---:|",
    ]
    for key, value in sorted(summary["conflict_type_counts"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {key} | {value} |")

    lines.extend(["", "## Top Review Rows", "", "| priority | sample | field | outcome | type | algo | target-year hint |", "|---:|---|---|---|---|---|---|"])
    for row in conflict_rows[:15]:
        algo = f"{row.get('algo_value','')} {row.get('algo_unit','')}".strip()
        target = row.get("parsed_row_values_by_year", "") or row.get("target_year_value", "")
        lines.append(
            f"| {row.get('priority','')} | {row.get('sample_id','')} | {row.get('field_id','')} | "
            f"{row.get('outcome','')} | {row.get('conflict_type','')} | {algo} | {target} |"
        )

    lines.extend(
        [
            "",
            "## How To Use",
            "",
            "- Rows marked `algorithm_year_column_mismatch_possible` should repair table-year resolution first.",
            "- Rows marked `gold_negative_but_target_year_metric_value_present` should not be converted into negative precision rules until the gold row is checked.",
            "- This audit is diagnostic only; it does not overwrite gold labels or extraction outputs.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--details-csv", type=Path, default=DEFAULT_DETAILS)
    parser.add_argument("--gold-csv", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--extraction-csv", type=Path, default=DEFAULT_EXTRACTION)
    parser.add_argument("--indicator-csv", type=Path, default=DEFAULT_INDICATORS)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--global-year-limit", type=int, default=0, help="0 means no limit")
    parser.add_argument(
        "--skip-gold-conflict",
        action="store_true",
        help="Only run extraction-side quantitative year audit; useful for unlabeled production batches.",
    )
    args = parser.parse_args()

    extraction_rows = load_rows(args.extraction_csv)
    indicator_rows = load_rows(args.indicator_csv)

    indicators = indicator_index(indicator_rows)
    grouped = extraction_groups(extraction_rows)

    details: list[dict[str, str]] = []
    gold_map: dict[tuple[str, str], dict[str, str]] = {}
    if not args.skip_gold_conflict:
        details = load_rows(args.details_csv)
        gold_map = row_index(load_rows(args.gold_csv))
    conflict_rows = build_conflict_rows(details, gold_map, grouped, indicators) if details else []
    global_year_rows = build_global_year_rows(extraction_rows, indicators, args.global_year_limit)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    conflict_fields = [
        "case_id",
        "priority",
        "sample_id",
        "field_id",
        "short_name",
        "metric_name_cn",
        "metric_type",
        "outcome",
        "value_match",
        "gold_status",
        "gold_value",
        "gold_unit_raw",
        "gold_source_page",
        "gold_source_text",
        "algo_status",
        "algo_value",
        "algo_unit",
        "algo_source_page",
        "algo_evidence_type",
        "algo_confidence",
        "precision_gate_status",
        "conflict_type",
        "suggested_action",
        "report_year",
        "header_years",
        "candidate_value_year",
        "target_year_value",
        "suggested_value",
        "parsed_row_values_by_year",
        "matched_metric_term",
        "metric_window",
        "algo_source_text",
    ]
    year_fields = [
        "case_id",
        "sample_id",
        "field_id",
        "short_name",
        "metric_name_cn",
        "value_candidate",
        "unit_raw_candidate",
        "source_page",
        "evidence_type_candidate",
        "confidence_rule",
        "report_year",
        "header_years",
        "candidate_value_year",
        "target_year_value",
        "suggested_value",
        "parsed_row_values_by_year",
        "matched_metric_term",
        "metric_window",
        "source_text",
    ]

    conflict_csv = args.out_dir / "gold_conflict_review_queue_v1.0.csv"
    year_csv = args.out_dir / "quantitative_year_mismatch_candidates_v1.0.csv"
    summary_json = args.out_dir / "gold_conflict_year_audit_summary_v1.0.json"
    report_md = args.out_dir / "gold_conflict_year_audit_report_v1.0.md"

    write_csv(conflict_csv, conflict_rows, conflict_fields)
    write_csv(year_csv, global_year_rows, year_fields)

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "details_csv": str(args.details_csv),
        "gold_csv": str(args.gold_csv),
        "extraction_csv": str(args.extraction_csv),
        "indicator_csv": str(args.indicator_csv),
        "input_detail_rows": len(details),
        "input_extraction_rows": len(extraction_rows),
        "conflict_review_rows": len(conflict_rows),
        "global_year_mismatch_rows": len(global_year_rows),
        "conflict_type_counts": dict(Counter(row.get("conflict_type", "") for row in conflict_rows)),
        "suggested_action_counts": dict(Counter(row.get("suggested_action", "") for row in conflict_rows)),
        "top_conflict_rows": [
            {
                "case_id": row.get("case_id", ""),
                "priority": row.get("priority", ""),
                "sample_id": row.get("sample_id", ""),
                "field_id": row.get("field_id", ""),
                "outcome": row.get("outcome", ""),
                "conflict_type": row.get("conflict_type", ""),
                "algo_value": row.get("algo_value", ""),
                "candidate_value_year": row.get("candidate_value_year", ""),
                "target_year_value": row.get("target_year_value", ""),
            }
            for row in conflict_rows[:20]
        ],
        "outputs": {
            "conflict_csv": str(conflict_csv),
            "year_csv": str(year_csv),
            "summary_json": str(summary_json),
            "report_md": str(report_md),
        },
    }
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(report_md, summary, conflict_rows)

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
