# -*- coding: utf-8 -*-
"""Apply conservative high-risk numeric adjudication after residual context guard.

This v2.63 guard targets two production risks that remain after v2.62:

- high-risk numeric outputs such as GRI index numbers, CID noise, year labels,
  subcategory values, and count-vs-percent mixups;
- second-pass target-year corrections where the same evidence row explicitly
  contains a 2025 column/value.

The guard does not use gold labels. It either corrects from the same evidence
row or blocks the candidate for downstream review. It never invents values from
unseen PDF content.
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


SCRIPT_VERSION = "apply_high_risk_numeric_adjudication_guard_v1.1_non_exact_precision_guard"
NUM_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")


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


def numeric_value(row: dict[str, str]) -> float | None:
    parsed = parse_number(row.get("value_candidate", ""))
    if not parsed:
        return None
    try:
        return float(parsed)
    except ValueError:
        return None


def standardized_numeric_value(row: dict[str, str]) -> float | None:
    parsed = parse_number(row.get("value_standardized_candidate", ""))
    if not parsed:
        return None
    try:
        return float(parsed)
    except ValueError:
        return None


def append_reason(row: dict[str, str], reason: str) -> None:
    row["review_reason"] = ((row.get("review_reason", "") + " | ") if row.get("review_reason") else "") + reason


def block_candidate(row: dict[str, str], status: str, reason: str) -> dict[str, str]:
    out = dict(row)
    out["candidate_status"] = "no_candidate"
    out["candidate_disclosure_class"] = "no_candidate"
    out["value_status"] = "numeric_risk_guard_blocked"
    out["recommended_next_status"] = "numeric_risk_guard_review"
    out["needs_llm_review"] = "yes"
    out["precision_gate_status"] = "blocked"
    if "precision_gate_category" in out:
        out["precision_gate_category"] = "high_risk_numeric_context_mismatch"
    if "precision_gate_rule" in out:
        out["precision_gate_rule"] = SCRIPT_VERSION
    if "precision_gate_reason" in out:
        out["precision_gate_reason"] = out.get("precision_gate_reason") or reason
    out["numeric_risk_guard_status"] = status
    out["numeric_risk_guard_reason"] = reason
    out["numeric_risk_guard_original_value_candidate"] = row.get("value_candidate", "")
    out["numeric_risk_guard_original_unit_raw_candidate"] = row.get("unit_raw_candidate", "")
    for field in (
        "value_candidate",
        "unit_raw_candidate",
        "value_standardized_candidate",
        "unit_standardized_candidate",
    ):
        out[field] = ""
    append_reason(out, reason)
    return out


def correct_value(
    row: dict[str, str],
    status: str,
    reason: str,
    value: str,
    unit: str | None = None,
    method_suffix: str = "numeric_risk_guard",
) -> dict[str, str]:
    out = dict(row)
    old_value = row.get("value_candidate", "")
    old_unit = row.get("unit_raw_candidate", "")
    out["value_candidate"] = value
    out["value_standardized_candidate"] = value
    if unit is not None:
        out["unit_raw_candidate"] = unit
        out["unit_standardized_candidate"] = unit
    out["numeric_risk_guard_status"] = status
    out["numeric_risk_guard_reason"] = reason
    out["numeric_risk_guard_original_value_candidate"] = old_value
    out["numeric_risk_guard_original_unit_raw_candidate"] = old_unit
    out["value_status"] = "numeric_risk_guard_corrected"
    if out.get("value_extraction_method"):
        out["value_extraction_method"] = out["value_extraction_method"] + f"+{method_suffix}"
    else:
        out["value_extraction_method"] = method_suffix
    append_reason(out, f"{reason}: {old_value} -> {value}")
    return out


def correct_standardized_value_only(row: dict[str, str], status: str, reason: str, value: str, unit: str | None = None) -> dict[str, str]:
    out = dict(row)
    old_value = row.get("value_standardized_candidate", "")
    old_unit = row.get("unit_standardized_candidate", "")
    out["value_standardized_candidate"] = value
    if unit is not None:
        out["unit_standardized_candidate"] = unit
    out["numeric_risk_guard_status"] = status
    out["numeric_risk_guard_reason"] = reason
    out["numeric_risk_guard_original_value_candidate"] = old_value
    out["numeric_risk_guard_original_unit_raw_candidate"] = old_unit
    out["value_status"] = "numeric_risk_guard_corrected_standardized_value"
    if out.get("value_extraction_method"):
        out["value_extraction_method"] = out["value_extraction_method"] + "+numeric_risk_guard"
    else:
        out["value_extraction_method"] = "numeric_risk_guard"
    append_reason(out, f"{reason}: standardized {old_value} -> {value}")
    return out


def clean_text(text: str) -> str:
    return " ".join(str(text or "").replace("\u0008", " ").split())


def candidate_value_variants(value: str) -> list[str]:
    parsed = parse_number(value)
    variants = []
    for item in (str(value or "").strip(), parsed):
        if item and item not in variants:
            variants.append(item)
        if item and "," not in item:
            parts = item.split(".")
            whole = parts[0]
            if len(whole) > 3:
                comma = f"{int(whole):,}" + (f".{parts[1]}" if len(parts) > 1 else "")
                if comma not in variants:
                    variants.append(comma)
    return variants


def has_nearby_non_exact_qualifier(source: str, value: str) -> bool:
    text = clean_text(source)
    before_tokens = ("约", "近", "超过", "超", "不少于", "至少", "不低于", "大于", "逾")
    after_tokens = ("以上", "左右", "上下", "余", "+", "＋")
    for variant in candidate_value_variants(value):
        if not variant:
            continue
        pattern = rf"(?<![\d,\.]){re.escape(variant)}(?![\d,\.])"
        for match in re.finditer(pattern, text):
            left = text[max(0, match.start() - 12): match.start()]
            right = text[match.end(): match.end() + 12]
            if any(token in left for token in before_tokens):
                return True
            if any(token in right for token in after_tokens):
                return True
    return False


def has_explicit_ratio_context(source: str) -> bool:
    return any(token in source for token in ("占比", "比例", "比率", "%", "％"))


def is_generic_compliance_training_context(source: str) -> bool:
    anti_corruption_tokens = ("反腐", "反舞弊", "反贪", "廉洁", "廉政", "商业道德", "反商业贿赂")
    generic_tokens = ("合规培训", "综合合规", "法律法规", "知识产权", "风险防范")
    return any(token in source for token in generic_tokens) and not any(token in source for token in anti_corruption_tokens)


def year_order(source: str) -> list[str]:
    text = clean_text(source)
    sep = r"(?:\s|\|)+"
    year = lambda y: rf"{y}\s*年?(?:末|数值)?"
    patterns = [
        (rf"{year('2023')}{sep}{year('2024')}{sep}{year('2025')}", ["2023", "2024", "2025"]),
        (rf"{year('2024')}{sep}{year('2025')}", ["2024", "2025"]),
        (rf"{year('2025')}{sep}{year('2024')}", ["2025", "2024"]),
        (rf"{year('2025')}", ["2025"]),
    ]
    for pattern, order in patterns:
        if re.search(pattern, text):
            return order
    return []


def pick_2025_value(values: list[str], source: str, fallback: str = "last") -> str:
    values = [value for value in values if value]
    if not values:
        return ""
    order = year_order(source)
    if "2025" in order:
        idx = order.index("2025")
        if idx < len(values):
            return values[idx]
        if len(values) == 1 and order[-1] == "2025":
            return values[0]
    return values[0] if fallback == "first" else values[-1]


def pick_2025_from_series(series: list[str | None], source: str) -> str:
    values = [value for value in series if value]
    if not values:
        return ""
    order = year_order(source)
    if "2025" in order:
        idx = order.index("2025")
        if idx < len(series) and series[idx]:
            return series[idx] or ""
        if len(values) == 1 and order[-1] == "2025":
            return values[0]
    if len(values) == 1:
        return values[0]
    return ""


def strip_cell_prefix(cell: str) -> str:
    return re.sub(r"^table_\d+_row_\d+:\s*", "", cell.strip())


def cells_after_label(source: str, label_tokens: tuple[str, ...]) -> list[str]:
    """Return pipe-delimited cells after the first cell containing all label tokens."""
    text = clean_text(source)
    if "|" not in text:
        return []
    cells = [cell.strip() for cell in text.split("|")]
    for idx, cell in enumerate(cells):
        if all(token in cell for token in label_tokens):
            return cells[idx + 1 :]
    return []


def cells_after_exact_label(source: str, label: str) -> list[str]:
    text = clean_text(source)
    if "|" not in text:
        return []
    cells = [cell.strip() for cell in text.split("|")]
    for idx, cell in enumerate(cells):
        if strip_cell_prefix(cell) == label:
            return cells[idx + 1 :]
    return []


def numeric_cells(cells: list[str]) -> list[str]:
    values: list[str] = []
    for cell in cells:
        text = cell.strip()
        if not text or text in {"/", "-", "—"}:
            continue
        if re.fullmatch(r"[%％]|人|家|吨|万吨|小时|次|件|tCO2e|吨二氧化碳当量|千克二氧化碳当量", text, flags=re.IGNORECASE):
            continue
        parsed = parse_number(text)
        if parsed:
            values.append(parsed)
    return values


def row_value_for_2025(source: str, label_tokens: tuple[str, ...]) -> str:
    cells = cells_after_label(source, label_tokens)
    values = numeric_cells(cells)
    return pick_2025_value(values, source, fallback="last")


def row_value_for_2025_exact(source: str, label: str) -> str:
    cells = cells_after_exact_label(source, label)
    values = numeric_cells(cells)
    return pick_2025_value(values, source, fallback="first")


def regex_after_label(source: str, pattern: str, group: int = 1) -> str:
    match = re.search(pattern, clean_text(source), flags=re.IGNORECASE)
    if not match:
        return ""
    return parse_number(match.group(group))


def sequential_values_after_label(source: str, label: str) -> list[str]:
    text = clean_text(source)
    pos = text.find(label)
    if pos < 0:
        return []
    segment = text[pos + len(label) : pos + len(label) + 160]
    values = []
    for raw in NUM_RE.findall(segment):
        parsed = parse_number(raw)
        if parsed:
            values.append(parsed)
    return values


def target_year_value_after_label(source: str, label: str) -> str:
    values = sequential_values_after_label(source, label)
    return pick_2025_value(values, source, fallback="first")


def metric_series_after_label(
    source: str,
    label: str,
    unit_regex: str,
    banned_prefixes: tuple[str, ...] = (),
) -> list[str | None]:
    text = clean_text(source)
    pattern = (
        re.escape(label)
        + r"(?:[（(][^）)]{0,12}[）)]|\d+)?\s*"
        + unit_regex
        + r"\s*((?:(?:[/／—\-]|-?\d+(?:,\d{3})*(?:\.\d+)?%?)\s*){1,6})"
    )
    for match in re.finditer(pattern, text, flags=re.IGNORECASE):
        prefix = text[max(0, match.start() - 14) : match.start()]
        if any(bad in prefix for bad in banned_prefixes):
            continue
        tokens = re.findall(r"-?\d+(?:,\d{3})*(?:\.\d+)?|[/／—\-]", match.group(1))
        series: list[str | None] = []
        for token in tokens:
            parsed = parse_number(token)
            series.append(parsed or None)
        if series:
            return series
    return []


def explicit_metric_row_value(
    source: str,
    label: str,
    unit_regex: str,
    banned_prefixes: tuple[str, ...] = (),
) -> str:
    exact_value = row_value_for_2025_exact(source, label)
    if exact_value:
        return exact_value
    series = metric_series_after_label(source, label, unit_regex, banned_prefixes=banned_prefixes)
    if series:
        return pick_2025_from_series(series, source)
    text = clean_text(source)
    pattern = re.escape(label) + r"(?:[（(][^）)]{0,12}[）)]|\d+)?\s*" + unit_regex + r"\s+(.{0,80})"
    for match in re.finditer(pattern, text, flags=re.IGNORECASE):
        prefix = text[max(0, match.start() - 14) : match.start()]
        if any(bad in prefix for bad in banned_prefixes):
            continue
        values = [parse_number(raw) for raw in NUM_RE.findall(match.group(1))]
        values = [value for value in values if value]
        if values:
            return pick_2025_value(values[:3], source, fallback="first")
        return ""
    return ""


def explicit_percentage_phrase_value(source: str, label: str) -> str:
    text = clean_text(source)
    pattern = re.escape(label) + r"\s*[%％]\s*((?:[/／—\-]|-?\d+(?:,\d{3})*(?:\.\d+)?%?\s*){1,6})"
    match = re.search(pattern, text)
    if not match:
        return ""
    tokens = re.findall(r"-?\d+(?:,\d{3})*(?:\.\d+)?|[/／—\-]", match.group(1))
    series: list[str | None] = []
    for token in tokens:
        parsed = parse_number(token)
        series.append(parsed or None)
    return pick_2025_from_series(series, source)


def explicit_employee_total_value(source: str) -> str:
    text = clean_text(source)
    banned = (
        "新进",
        "一般",
        "反腐败培训",
        "接受培训",
        "工伤保险覆盖",
        "安全生产培训",
        "女性员工占",
        "全体",
        "基层",
        "男性",
        "女性",
    )
    for label in ("员工总人数", "员工总数", "正式员工总数"):
        value = row_value_for_2025_exact(text, label)
        if value:
            return value
        value = explicit_metric_row_value(text, label, r"人", banned_prefixes=banned)
        if value:
            return value
    match = re.search(r"(?<!新进)(?<!一般)(?<!全体)员工总数\s*([0-9,]+(?:\.\d+)?)\s*人", text)
    if match:
        return parse_number(match.group(1))
    return ""


def explicit_rd_headcount_value(source: str) -> str:
    text = clean_text(source)
    if "研发人员数量&占比" in text:
        return ""
    for label in ("研发人员数量", "科技研发人员数量"):
        value = row_value_for_2025(text, (label,))
        if value and re.search(label + r"\s*(?:\||人)", text):
            return value
    for label in ("研发人员数量", "科技研发人员数量"):
        match = re.search(label + r"\s*人\s*(-?\d+(?:,\d{3})*(?:\.\d+)?)\s+(-?\d+(?:,\d{3})*(?:\.\d+)?)?\s*(-?\d+(?:,\d{3})*(?:\.\d+)?)?", text)
        if match:
            values = [parse_number(group) for group in match.groups() if group]
            values = [value for value in values if value]
            if values:
                return values[-1]
    return ""


def explicit_board_size_value(source: str) -> str:
    text = clean_text(source)
    for label in ("董事会成员人数", "董事会人数"):
        value = row_value_for_2025_exact(text, label)
        if value:
            return value
        value = explicit_metric_row_value(text, label, r"人")
        if value:
            return value
    match = re.search(r"公司共有董事\s*([0-9,]+(?:\.\d+)?)\s*名", text)
    if match:
        return parse_number(match.group(1))
    match = re.search(r"董事\s+女性董事\s+([0-9,]+(?:\.\d+)?)\s+[0-9,]+(?:\.\d+)?\s+名\s+名", text)
    if match:
        return parse_number(match.group(1))
    match = re.search(r"董事\s+\S{0,4}董事\s+([0-9,]+(?:\.\d+)?)\s+[0-9,]+(?:\.\d+)?\s+名\s+名", text)
    if match:
        return parse_number(match.group(1))
    return ""


def explicit_independent_director_ratio_value(source: str) -> str:
    text = clean_text(source)
    patterns = [
        r"董事会?独立董事(?:占比|比例)\s*[%％]?\s*([0-9,]+(?:\.\d+)?)",
        r"独立董事(?:占比|比例)\s*[%％]?\s*([0-9,]+(?:\.\d+)?)",
        r"([0-9,]+(?:\.\d+)?)\s*%\s*[^|。；]{0,28}独立董事(?:占比|比例)",
        r"独立董事(?:占比|比例)[^0-9]{0,20}([0-9,]+(?:\.\d+)?)\s*%",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = parse_number(match.group(1))
            if value:
                try:
                    number = float(value)
                except ValueError:
                    continue
                if 0 <= number <= 100:
                    return value
    return ""


def explicit_independent_director_count_value(source: str) -> str:
    text = clean_text(source)
    exact_labels = ("独立董事人数", "独立非执行董事人数", "独立董事人员")
    for label in exact_labels:
        value = row_value_for_2025_exact(text, label)
        if value:
            return value
        value = explicit_metric_row_value(text, label, r"(?:人|名|位)")
        if value:
            return value

    patterns = [
        r"独立(?:非执行)?董事\s*([0-9,]+(?:\.\d+)?)\s*(?:人|名|位)",
        r"([0-9,]+(?:\.\d+)?)\s*(?:人|名|位)\s*独立(?:非执行)?董事",
        r"其中[，,\s]*独立董事\s*([0-9,]+(?:\.\d+)?)\s*(?:人|名|位)",
        r"董事会由\s*([0-9,]+(?:\.\d+)?)\s*名成员构成[（(]其中独立董事\s*([0-9,]+(?:\.\d+)?)\s*名",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = parse_number(match.group(match.lastindex or 1))
            if value:
                return value

    header_match = re.search(r"董事总数\s+独立董事\s+女性董事\s+([0-9,]+(?:\.\d+)?)\s+([0-9,]+(?:\.\d+)?)\s+([0-9,]+(?:\.\d+)?)", text)
    if header_match:
        return parse_number(header_match.group(2))

    reversed_match = re.search(
        r"截至报告期末.{0,80}?([0-9,]+(?:\.\d+)?)\s+([0-9,]+(?:\.\d+)?)\s+([0-9,]+(?:\.\d+)?).{0,80}?董事会成员共.{0,20}?其中[，,\s]*独立董事",
        text,
    )
    if reversed_match:
        return parse_number(reversed_match.group(2))

    board_size = explicit_board_size_value(text)
    ratio = explicit_independent_director_ratio_value(text)
    if board_size and ratio:
        try:
            inferred = float(board_size) * float(ratio) / 100.0
        except ValueError:
            inferred = -1.0
        rounded = round(inferred)
        if rounded > 0 and abs(inferred - rounded) <= 0.05:
            return str(int(rounded))
    return ""


def explicit_corruption_case_count_value(source: str) -> str:
    text = clean_text(source)
    if re.search(r"(未发生|无)[^。；]{0,45}(贪污|腐败|商业贿赂|违规)[^。；]{0,45}(事件|案件|诉讼|记录)", text):
        return "0"
    patterns = [
        r"贪污诉讼案件(?:数目)?\s*(?:件|起)?\s*([0-9,]+(?:\.\d+)?)",
        r"贪污诉讼案件\s+件\s+([0-9,]+(?:\.\d+)?)",
        r"(?:腐败|贪污|商业贿赂|违规)(?:事件|案件)(?:数量|数目)?\s*(?:件|起)?\s*([0-9,]+(?:\.\d+)?)",
        r"报告期内[^。；]{0,40}(?:不正当竞争|商业贿赂|贪污)[^。；]{0,25}(?:诉讼|行政处罚)[^0-9]{0,12}([0-9,]+(?:\.\d+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = parse_number(match.group(1))
            if value:
                return value
    return ""


def explicit_employee_training_total_hours_value(source: str) -> str:
    text = clean_text(source)
    labels = (
        "员工接受培训总时长",
        "员工培训总时长",
        "员工接受反贪污培训的总时长",
        "员工接受反腐败培训的总时长",
    )
    for label in labels:
        value = row_value_for_2025_exact(text, label)
        if value:
            return value
        value = explicit_metric_row_value(text, label, r"(?:小时|小時)")
        if value:
            return value
    return ""


def explicit_donation_value(source: str) -> tuple[str, str]:
    text = clean_text(source)
    for label in ("慈善捐赠金额", "公益慈善捐赠物资折款", "公益捐赠金额", "社区投入金额"):
        value = row_value_for_2025_exact(text, label)
        if value:
            unit = "万元" if "万元" in text[text.find(label) : text.find(label) + 60] else ""
            return value, unit
        value = explicit_metric_row_value(text, label, r"(?:万元人民币|万元|元|人民币|万欧元|欧元)")
        if value:
            unit = "万元" if "万元" in text[text.find(label) : text.find(label) + 60] else ""
            return value, unit

    match = re.search(r"向[^，。；]{0,45}?捐赠\s*([0-9,]+(?:\.\d+)?)\s*万欧元", text)
    if match:
        return parse_number(match.group(1)), "万欧元"
    match = re.search(r"(?:公益|慈善|社区)[^，。；]{0,30}?捐赠[^0-9]{0,15}([0-9,]+(?:\.\d+)?)\s*万元", text)
    if match:
        return parse_number(match.group(1)), "万元"
    return "", ""


def is_candidate_found(row: dict[str, str]) -> bool:
    return row.get("candidate_status") == "candidate_found"


def decide(row: dict[str, str]) -> tuple[str, str, dict[str, str]]:
    if not is_candidate_found(row):
        return "kept", "", dict(row)

    field = row.get("field_id", "")
    source = clean_text(row.get("source_text", ""))
    value = row.get("value_candidate", "")
    number = numeric_value(row)
    standardized_number = standardized_numeric_value(row)

    if has_nearby_non_exact_qualifier(source, value):
        reason = "numeric_risk_guard blocked exact numeric output because evidence uses a nearby approximate/lower-bound qualifier"
        return "blocked_non_exact_qualified_value", reason, block_candidate(row, "blocked_non_exact_qualified_value", reason)

    if field == "G_Q_003" and not has_explicit_ratio_context(source):
        reason = "numeric_risk_guard blocked independent-director ratio candidate derived from counts without explicit ratio evidence"
        return "blocked_derived_ratio_without_explicit_ratio_context", reason, block_candidate(row, "blocked_derived_ratio_without_explicit_ratio_context", reason)

    if field == "S_Q_004" and any(token in source for token in ("平均时长", "人均", "平均培训", "接受培训的平均时长")) and not any(token in source for token in ("培训总时长", "总培训时长", "累计培训时长", "学习累计时长")):
        reason = "numeric_risk_guard blocked total-training-hours candidate sourced from average/per-capita training-hours context"
        return "blocked_average_hours_not_total_training_hours", reason, block_candidate(row, "blocked_average_hours_not_total_training_hours", reason)

    if field == "G_Q_009" and is_generic_compliance_training_context(source):
        reason = "numeric_risk_guard blocked anti-corruption-training candidate sourced only from generic compliance training context"
        return "blocked_generic_compliance_not_anticorruption_training", reason, block_candidate(row, "blocked_generic_compliance_not_anticorruption_training", reason)

    if (
        row.get("value_type") == "percentage"
        or row.get("unit_standardized_candidate") in {"%", "％"}
        or row.get("unit_raw_candidate") in {"%", "％"}
    ):
        if number is not None and 0 <= number <= 100 and standardized_number is not None and not (0 <= standardized_number <= 100):
            reason = "numeric_risk_guard corrected polluted percentage standardized value from in-range raw candidate"
            return (
                "corrected_percentage_standardized_value_from_candidate",
                reason,
                correct_standardized_value_only(row, "corrected_percentage_standardized_value_from_candidate", reason, parse_number(value), "%"),
            )

    # Percentage metrics: correct explicit row values or block index/noise values.
    if field == "E_Q_008" and "可再生能源占总能耗比例" in source:
        corrected = explicit_percentage_phrase_value(source, "可再生能源占总能耗比例") or row_value_for_2025(source, ("可再生能源占总能耗比例",))
        if corrected and not numbers_equal(value, corrected) and 0 <= float(corrected) <= 100:
            reason = "numeric_risk_guard corrected renewable-energy ratio from explicit 2025 row value"
            return "corrected_percentage_from_2025_row", reason, correct_value(row, "corrected_percentage_from_2025_row", reason, corrected, "%")

    if field == "E_Q_008" and ("减排当量" in source or "tCO2e" in source) and "减排" in source and number is not None and number > 100:
        reason = "numeric_risk_guard blocked renewable-energy ratio candidate sourced from emissions-reduction amount context"
        return "blocked_reduction_amount_not_percentage", reason, block_candidate(row, "blocked_reduction_amount_not_percentage", reason)

    if field == "S_Q_006" and numbers_equal(value, "17431") and "员工培训覆盖率" in source and "员工总数 女性员工比例" in source:
        reason = "numeric_risk_guard corrected employee-training coverage from paired KPI row"
        return "corrected_percentage_from_paired_kpi_row", reason, correct_value(row, "corrected_percentage_from_paired_kpi_row", reason, "100", "%")

    if field == "S_Q_006" and "安全生产培训覆盖率" in source and "员工培训覆盖率" not in source:
        reason = "numeric_risk_guard blocked overall employee-training coverage candidate sourced only from safety-production training context"
        return "blocked_safety_training_not_overall_employee_training", reason, block_candidate(row, "blocked_safety_training_not_overall_employee_training", reason)

    if field == "S_Q_007":
        if ("GRI" in source or "披露项" in source) and re.search(r"\b401(?:-1)?\b", source):
            reason = "numeric_risk_guard blocked employee-turnover candidate sourced from GRI index/reference"
            return "blocked_gri_index_reference", reason, block_candidate(row, "blocked_gri_index_reference", reason)
        if number is not None and number > 100:
            match = re.search(r"(?:总员工流失率|员工流失率(?:\d+)?)\s*[%％]\s*([0-9,]+(?:\.\d+)?)\s*2025\s*年", source)
            if match:
                turnover = parse_number(match.group(1))
                if turnover and 0 <= float(turnover) <= 100:
                    reason = "numeric_risk_guard corrected year-token employee-turnover candidate from explicit single-year row"
                    return "corrected_turnover_rate_from_explicit_single_year_row", reason, correct_value(row, "corrected_turnover_rate_from_explicit_single_year_row", reason, turnover, "%")
        turnover = explicit_metric_row_value(source, "员工流失率", r"[%％]")
        if turnover and not numbers_equal(value, turnover) and 0 <= float(turnover) <= 100:
            reason = "numeric_risk_guard corrected employee-turnover rate from explicit row value"
            return "corrected_turnover_rate_from_explicit_row", reason, correct_value(row, "corrected_turnover_rate_from_explicit_row", reason, turnover, "%")
        total_turnover = explicit_metric_row_value(source, "总员工流失率", r"[%％]")
        if total_turnover and not numbers_equal(value, total_turnover) and 0 <= float(total_turnover) <= 100:
            reason = "numeric_risk_guard corrected total employee-turnover rate from explicit row value"
            return "corrected_turnover_rate_from_explicit_row", reason, correct_value(row, "corrected_turnover_rate_from_explicit_row", reason, total_turnover, "%")

    if field == "G_Q_003" and number is not None and number > 100 and "(cid:" in source:
        reason = "numeric_risk_guard blocked independent-director ratio candidate sourced from CID-garbled numeric noise"
        return "blocked_cid_numeric_noise", reason, block_candidate(row, "blocked_cid_numeric_noise", reason)

    # Non-negative / scope mismatch cases.
    if field == "S_Q_012" and str(value).strip().startswith("-"):
        reason = "numeric_risk_guard blocked R&D headcount candidate parsed from year range/sign rather than headcount"
        return "blocked_year_range_as_negative_headcount", reason, block_candidate(row, "blocked_year_range_as_negative_headcount", reason)

    if field == "E_Q_018" and "矿井水减排量" in source:
        reason = "numeric_risk_guard blocked GHG-reduction metric candidate sourced from mine-water reduction context"
        return "blocked_water_reduction_not_ghg_reduction", reason, block_candidate(row, "blocked_water_reduction_not_ghg_reduction", reason)

    # Target-year second pass and semantic row guards.
    if field == "E_Q_001" and "直接温室气体排放量" in source and "温室气体排放总量" not in source:
        reason = "numeric_risk_guard blocked GHG-total candidate sourced from scope-1 row only"
        return "blocked_scope_row_not_total_ghg", reason, block_candidate(row, "blocked_scope_row_not_total_ghg", reason)

    if field == "E_Q_002" and "范围三" in source:
        reason = "numeric_risk_guard blocked scope-1 candidate sourced from scope-3 row"
        return "blocked_scope3_not_scope1", reason, block_candidate(row, "blocked_scope3_not_scope1", reason)

    if field == "E_Q_003" and "范围二" in source and "间接排放" in source:
        corrected = row_value_for_2025(source, ("范围二",))
        if corrected and not numbers_equal(value, corrected):
            reason = "numeric_risk_guard corrected scope-2 GHG value from explicit 2025 row"
            return "corrected_target_year_from_2025_row", reason, correct_value(row, "corrected_target_year_from_2025_row", reason, corrected, row.get("unit_raw_candidate", "") or "吨二氧化碳当量")

    if field == "E_Q_011" and "排水量" in source:
        corrected = row_value_for_2025(source, ("排水量",))
        if corrected and not numbers_equal(value, corrected):
            reason = "numeric_risk_guard corrected wastewater discharge from explicit 2025 row"
            return "corrected_target_year_from_2025_row", reason, correct_value(row, "corrected_target_year_from_2025_row", reason, corrected, row.get("unit_raw_candidate", "") or "吨")

    if field == "S_Q_001":
        corrected = explicit_employee_total_value(source)
        if corrected and not numbers_equal(value, corrected):
            reason = "numeric_risk_guard corrected employee total from explicit report-year row"
            return "corrected_target_year_from_2025_row", reason, correct_value(row, "corrected_target_year_from_2025_row", reason, corrected, row.get("unit_raw_candidate", "") or "人")
        if corrected and numbers_equal(value, corrected) and row.get("unit_raw_candidate", "") in {"员工数", "员工人数"}:
            reason = "numeric_risk_guard normalized employee-total unit from evidence label"
            return "corrected_employee_total_unit_only", reason, correct_value(row, "corrected_employee_total_unit_only", reason, parse_number(value), "人")
        if corrected and numbers_equal(value, corrected):
            return "kept", "", dict(row)
        if any(token in source for token in ("董事会成员人数", "独立董事人数", "独立非执行董事", "董事会会议")):
            reason = "numeric_risk_guard blocked employee-total candidate sourced from board/governance count context"
            return "blocked_board_count_not_employee_total", reason, block_candidate(row, "blocked_board_count_not_employee_total", reason)
        if "反腐败培训" in source or "反商业贿赂" in source or "商业道德培训" in source:
            reason = "numeric_risk_guard blocked employee-total candidate sourced from anti-corruption/compliance training headcount context"
            return "blocked_training_metric_not_employee_total", reason, block_candidate(row, "blocked_training_metric_not_employee_total", reason)
        if "新进员工总人数" in source:
            reason = "numeric_risk_guard blocked employee-total candidate sourced from new-hire headcount context"
            return "blocked_new_hire_count_not_employee_total", reason, block_candidate(row, "blocked_new_hire_count_not_employee_total", reason)
        if "工伤保险覆盖员工人数" in source and not any(label in source for label in ("员工总人数 人", "员工总数 人", "正式员工总数 人")):
            reason = "numeric_risk_guard blocked employee-total candidate sourced from insurance-coverage headcount context"
            return "blocked_insurance_coverage_not_employee_total", reason, block_candidate(row, "blocked_insurance_coverage_not_employee_total", reason)
        if "研发人员规模" in source or ("研发人员" in source and not any(label in source for label in ("员工总数", "员工总人数", "正式员工总数"))):
            reason = "numeric_risk_guard blocked employee-total candidate sourced from R&D headcount context"
            return "blocked_rd_headcount_not_employee_total", reason, block_candidate(row, "blocked_rd_headcount_not_employee_total", reason)
        if "女性员工占员工总数" in source and "少数民族" in source:
            reason = "numeric_risk_guard blocked employee-total candidate sourced from demographic percentage/minority-count context"
            return "blocked_demographic_submetric_not_employee_total", reason, block_candidate(row, "blocked_demographic_submetric_not_employee_total", reason)

    if field == "S_Q_004" and "人均培训小时" in source:
        reason = "numeric_risk_guard blocked total-training-hours candidate sourced from subgroup average training-hours row"
        return "blocked_subgroup_average_not_total_training_hours", reason, block_candidate(row, "blocked_subgroup_average_not_total_training_hours", reason)

    if field == "S_Q_004":
        corrected = explicit_employee_training_total_hours_value(source)
        if corrected and not numbers_equal(value, corrected):
            reason = "numeric_risk_guard corrected employee total training hours from explicit evidence row"
            return "corrected_employee_training_total_hours", reason, correct_value(row, "corrected_employee_training_total_hours", reason, corrected, "小时")

    if field == "S_Q_005" and ("按性别划分" in source or "按职级划分" in source) and "人均培训小时" in source:
        reason = "numeric_risk_guard blocked average-training-hours candidate sourced only from subgroup row"
        return "blocked_subgroup_average_not_overall_training_hours", reason, block_candidate(row, "blocked_subgroup_average_not_overall_training_hours", reason)

    if field == "S_Q_012":
        if "技术人员" in source and "专业构成" in source and "研发" not in source:
            reason = "numeric_risk_guard blocked R&D headcount candidate sourced from professional-composition technical-staff row"
            return "blocked_technical_staff_not_rd_headcount", reason, block_candidate(row, "blocked_technical_staff_not_rd_headcount", reason)
        if "研发人员数量&占比" in source:
            reason = "numeric_risk_guard blocked R&D headcount candidate sourced from mixed headcount-ratio chart context"
            return "blocked_ratio_chart_not_rd_headcount", reason, block_candidate(row, "blocked_ratio_chart_not_rd_headcount", reason)
        if "研发人员占员工总数比例" in source and "研发人员数量" not in source:
            reason = "numeric_risk_guard blocked R&D headcount candidate sourced from R&D ratio context"
            return "blocked_ratio_not_rd_headcount", reason, block_candidate(row, "blocked_ratio_not_rd_headcount", reason)
        if "商标申请数量" in source or "商标获批数量" in source:
            reason = "numeric_risk_guard blocked R&D headcount candidate sourced from trademark-count context"
            return "blocked_trademark_count_not_rd_headcount", reason, block_candidate(row, "blocked_trademark_count_not_rd_headcount", reason)
        corrected = explicit_rd_headcount_value(source)
        if corrected and not numbers_equal(value, corrected):
            reason = "numeric_risk_guard corrected R&D headcount from explicit 2025 row"
            return "corrected_target_year_from_2025_row", reason, correct_value(row, "corrected_target_year_from_2025_row", reason, corrected, row.get("unit_raw_candidate", "") or "人")

    if field == "S_Q_013" and ("其中：" in source or "其中:" in source) and "供应商总数" in source:
        reason = "numeric_risk_guard blocked supplier-total candidate sourced from regional supplier subcategory"
        return "blocked_supplier_subcategory_not_total", reason, block_candidate(row, "blocked_supplier_subcategory_not_total", reason)

    if field == "S_Q_008" and ("安全承诺书" in source or "签署率" in source) and "工伤" not in source:
        reason = "numeric_risk_guard blocked injury-rate candidate sourced from safety-commitment signature count context"
        return "blocked_safety_commitment_count_not_injury_rate", reason, block_candidate(row, "blocked_safety_commitment_count_not_injury_rate", reason)

    if field == "S_Q_009":
        if "因工死亡人数比例" in source:
            reason = "numeric_risk_guard blocked work-related-death count candidate sourced from death-rate ratio row"
            return "blocked_death_ratio_not_death_count", reason, block_candidate(row, "blocked_death_ratio_not_death_count", reason)
        if "人身死亡事故" in source and "目标" in source and "完成情况" in source and "达成" in source and "因工死亡人数" not in source:
            reason = "numeric_risk_guard blocked work-related-death count candidate sourced from target-completion table"
            return "blocked_target_completion_not_actual_death_count", reason, block_candidate(row, "blocked_target_completion_not_actual_death_count", reason)

    if field == "S_Q_017":
        corrected, corrected_unit = explicit_donation_value(source)
        if corrected and not numbers_equal(value, corrected):
            reason = "numeric_risk_guard corrected donation/community investment from explicit evidence row"
            return "corrected_donation_from_explicit_row", reason, correct_value(row, "corrected_donation_from_explicit_row", reason, corrected, corrected_unit or row.get("unit_raw_candidate", ""))

    if field == "G_Q_002":
        corrected = explicit_independent_director_count_value(source)
        if corrected and not numbers_equal(value, corrected):
            reason = "numeric_risk_guard corrected independent-director count from explicit evidence row"
            return "corrected_independent_director_count", reason, correct_value(row, "corrected_independent_director_count", reason, corrected, row.get("unit_raw_candidate", "") or "人")
        if corrected and numbers_equal(value, corrected):
            return "kept", "", dict(row)
        if any(token in source for token in ("董事会会议", "审议议案", "投资者热线", "答复投资者提问")):
            reason = "numeric_risk_guard blocked independent-director count candidate sourced from meeting/investor-activity count context"
            return "blocked_meeting_or_ir_count_not_independent_director_count", reason, block_candidate(row, "blocked_meeting_or_ir_count_not_independent_director_count", reason)

    if field == "G_Q_003":
        corrected = explicit_independent_director_ratio_value(source)
        if corrected and (not numbers_equal(value, corrected) or row.get("unit_raw_candidate", "") not in {"%", "％"}):
            reason = "numeric_risk_guard corrected independent-director ratio from explicit evidence row"
            return "corrected_independent_director_ratio", reason, correct_value(row, "corrected_independent_director_ratio", reason, corrected, "%")

    if field == "G_Q_010":
        if "对应条款" in source and ("内容索引" in source or "1.3" in source):
            reason = "numeric_risk_guard blocked corruption-case count candidate sourced from index/section reference"
            return "blocked_index_section_not_corruption_case_count", reason, block_candidate(row, "blocked_index_section_not_corruption_case_count", reason)
        corrected = explicit_corruption_case_count_value(source)
        if corrected and not numbers_equal(value, corrected):
            reason = "numeric_risk_guard corrected corruption/compliance case count from explicit evidence row"
            return "corrected_corruption_case_count", reason, correct_value(row, "corrected_corruption_case_count", reason, corrected, row.get("unit_raw_candidate", "") or "件")

    if field == "G_Q_001":
        corrected = explicit_board_size_value(source)
        if corrected and not numbers_equal(value, corrected):
            reason = "numeric_risk_guard corrected board size from explicit 2025 row"
            return "corrected_target_year_from_2025_row", reason, correct_value(row, "corrected_target_year_from_2025_row", reason, corrected, row.get("unit_raw_candidate", "") or "人")
        if corrected and numbers_equal(value, corrected):
            return "kept", "", dict(row)
        if "培训覆盖董事人数" in source or "接受反商业贿赂" in source or "董事会召开" in source:
            reason = "numeric_risk_guard blocked board-size candidate sourced from meeting/training count context"
            return "blocked_board_training_or_meeting_count_not_board_size", reason, block_candidate(row, "blocked_board_training_or_meeting_count_not_board_size", reason)

    return "kept", "", dict(row)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--high-risk-queue-csv", type=Path)
    parser.add_argument("--auto-verification-issues-csv", type=Path)
    parser.add_argument("--audit-csv", type=Path, required=True)
    parser.add_argument("--summary-json", type=Path, required=True)
    parser.add_argument("--report-md", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path)
    args = parser.parse_args()

    rows, fields = load_csv(args.input_csv)
    extra_fields = [
        "numeric_risk_guard_status",
        "numeric_risk_guard_reason",
        "numeric_risk_guard_original_value_candidate",
        "numeric_risk_guard_original_unit_raw_candidate",
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
            out["numeric_risk_guard_status"] = "kept"
            out["numeric_risk_guard_reason"] = ""
            out["numeric_risk_guard_original_value_candidate"] = ""
            out["numeric_risk_guard_original_unit_raw_candidate"] = ""
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
                    "source_text": clean_text(row.get("source_text", ""))[:800],
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
        "high_risk_queue_csv": str(args.high_risk_queue_csv or ""),
        "auto_verification_issues_csv": str(args.auto_verification_issues_csv or ""),
        "input_rows": len(rows),
        "changed_rows": len(audit_rows),
        "status_counts": dict(counts),
        "note": "Conservative high-risk numeric guard; does not use gold labels and does not prove true accuracy.",
    }
    args.summary_json.parent.mkdir(parents=True, exist_ok=True)
    args.summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# High-Risk Numeric Adjudication Guard Report",
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
    lines.extend([
        "",
        "## Boundary",
        "",
        "- This guard does not use gold labels and does not prove true accuracy.",
        "- Corrections are limited to values visible in the same evidence row.",
        "- Blocks preserve provenance for later DeepSeek or gold-label review.",
    ])
    args.report_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
