# -*- coding: utf-8 -*-
"""Apply production-safe DeepSeek value review results as an auditable what-if.

The script is intentionally conservative:
- It never reads gold labels.
- It only accepts high-confidence replacements with a parseable better_value.
- It does not apply reject-only decisions, because dropping candidates can hurt
  recall and should go through a separate precision gate.
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


NUM_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")
SCRIPT_VERSION = "apply_deepseek_value_review_results_v1.1"


ACCEPT_DECISIONS = {"better_value_in_context", "wrong_year", "wrong_metric", "wrong_unit"}
def load_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_confidence(value: str) -> float:
    text = str(value or "").strip().lower()
    if not text:
        return 0.0
    if text in {"high", "高"}:
        return 0.9
    if text in {"medium", "med", "中"}:
        return 0.7
    if text in {"low", "低"}:
        return 0.4
    try:
        number = float(text)
    except ValueError:
        return 0.0
    if number > 1:
        number /= 100
    return max(0.0, min(1.0, number))


def parse_rank(value: str, default: int = 9999) -> int:
    try:
        return int(float(str(value or "").strip()))
    except ValueError:
        return default


def parse_float(value: str, default: float = 0.0) -> float:
    try:
        return float(str(value or "").strip())
    except ValueError:
        return default


def parse_number(value: str) -> str:
    text = str(value or "").strip().replace("，", ",")
    multiplier = 1.0
    if "亿" in text:
        multiplier = 100000000.0
    elif "万" in text:
        multiplier = 10000.0
    match = NUM_RE.search(text)
    if not match:
        return ""
    try:
        number = float(match.group(0).replace(",", "")) * multiplier
    except ValueError:
        return ""
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f"{number:.10f}".rstrip("0").rstrip(".")


def compact_text(value: str, limit: int = 600) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def key(row: dict[str, str]) -> tuple[str, str]:
    return row.get("sample_id", ""), row.get("field_id", "")


def load_indicator_map(path: Path | None) -> dict[str, dict[str, str]]:
    if not path:
        return {}
    if not path.exists():
        raise FileNotFoundError(path)
    rows, _ = load_csv(path)
    return {row.get("field_id", ""): row for row in rows}


def normalize_unit_text(value: str) -> str:
    text = re.sub(r"\s+", "", str(value or "").lower())
    replacements = {
        "\uff05": "%",
        "tco2e": "\u5428co2e",
        "t-co2e": "\u5428co2e",
        "\u4e8c\u6c27\u5316\u78b3\u5f53\u91cf": "co2e",
        "\u78b3\u5f53\u91cf": "co2e",
        "\u516c\u5428": "\u5428",
        "\u5343\u74e6\u65f6": "kwh",
        "\u5343\u74e6\u6642": "kwh",
        "\u7acb\u65b9\u7c73": "m3",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def unit_family(unit: str) -> tuple[str, ...]:
    unit = normalize_unit_text(unit)
    if not unit:
        return ("missing",)
    if "%" in unit or "\u767e\u5206\u6bd4" in unit:
        return ("ratio", "percent")
    if "co2" in unit:
        if "\u4e07\u5428" in unit:
            return ("co2e", "wan_tonne")
        if "\u5343\u514b" in unit or "kg" in unit:
            return ("co2e", "kg")
        return ("co2e", "tonne")
    if "gwh" in unit:
        return ("energy", "gwh")
    if "mwh" in unit:
        return ("energy", "mwh")
    if "kwh" in unit:
        return ("energy", "wan_kwh" if "\u4e07" in unit else "kwh")
    if "m3" in unit:
        return ("volume", "wan_m3" if "\u4e07" in unit else "m3")
    if "\u4ebf\u5143" in unit:
        return ("money", "yi_yuan")
    if "\u4e07\u5143" in unit:
        return ("money", "wan_yuan")
    if "\u5143" in unit:
        return ("money", "yuan")
    if "\u4e07\u5428" in unit:
        return ("mass", "wan_tonne")
    if "\u5428" in unit:
        return ("mass", "tonne")
    if "\u4e07\u4eba" in unit:
        return ("person", "wan_person")
    if any(marker in unit for marker in ["\u4eba", "\u540d", "\u4f4d"]):
        return ("person", "person")
    if "\u5c0f\u65f6" in unit or "hour" in unit:
        return ("time", "hour")
    if "\u6b21" in unit:
        return ("count", "event")
    if "\u4ef6" in unit:
        return ("count", "case")
    if "\u5bb6" in unit:
        return ("count", "org")
    return ("literal", unit)


def unit_signature(unit: str) -> tuple[Any, ...]:
    unit = normalize_unit_text(unit)
    if not unit:
        return ("missing",)
    if "/" in unit:
        numerator, denominator = unit.split("/", 1)
        return ("rate", unit_family(numerator), unit_family(denominator))
    if "\u6bcf" in unit:
        numerator, denominator = unit.split("\u6bcf", 1)
        return ("rate", unit_family(numerator), unit_family(denominator))
    return unit_family(unit)


def units_compatible(unit: str, accepted: str) -> bool:
    return unit_signature(unit) == unit_signature(accepted)


def split_accepted_units(indicator: dict[str, str]) -> list[str]:
    raw = ";".join(
        [
            indicator.get("units_accepted_raw", ""),
            indicator.get("unit_normalized", ""),
        ]
    )
    return [normalize_unit_text(part) for part in re.split(r"[;；,，]", raw) if part.strip()]


def unit_matches_indicator(unit: str, indicator: dict[str, str]) -> bool:
    unit_norm = normalize_unit_text(unit)
    if not unit_norm:
        return True
    for token in split_accepted_units(indicator):
        if not token:
            continue
        if token in unit_norm or unit_norm in token:
            return True
    return False


def looks_rate_or_intensity_unit(unit: str) -> bool:
    unit_norm = normalize_unit_text(unit)
    return any(marker in unit_norm for marker in ["/", "／", "每", "单位", "营收", "万元"])


def indicator_allows_intensity(indicator: dict[str, str]) -> bool:
    text = normalize_unit_text(
        " ".join(
            [
                indicator.get("metric_name_cn", ""),
                indicator.get("definition", ""),
                indicator.get("unit_normalized", ""),
                indicator.get("units_accepted_raw", ""),
            ]
        )
    )
    return any(marker in text for marker in ["强度", "比例", "率", "per", "/"])


def split_accepted_units(indicator: dict[str, str]) -> list[str]:
    raw = ";".join(
        [
            indicator.get("units_accepted_raw", ""),
            indicator.get("unit_normalized", ""),
        ]
    )
    return [normalize_unit_text(part) for part in re.split(r"[;,\|\uFF1B\uFF0C\u3001]+", raw) if part.strip()]


def unit_matches_indicator(unit: str, indicator: dict[str, str]) -> bool:
    unit_norm = normalize_unit_text(unit)
    if not unit_norm:
        return True
    for token in split_accepted_units(indicator):
        if token and units_compatible(unit_norm, token):
            return True
    return False


def looks_rate_or_intensity_unit(unit: str) -> bool:
    unit_norm = normalize_unit_text(unit)
    return any(marker in unit_norm for marker in ["/", "\u6bcf", "\u5355\u4f4d", "\u8425\u6536"])


def indicator_allows_intensity(indicator: dict[str, str]) -> bool:
    text = normalize_unit_text(
        " ".join(
            [
                indicator.get("metric_name_cn", ""),
                indicator.get("definition", ""),
                indicator.get("unit_normalized", ""),
                indicator.get("units_accepted_raw", ""),
            ]
        )
    )
    return any(marker in text for marker in ["\u5f3a\u5ea6", "\u6bd4\u4f8b", "\u7387", "per", "/"])


def validate_review_unit(review: dict[str, str], indicators: dict[str, dict[str, str]]) -> tuple[bool, str, int]:
    indicator = indicators.get(review.get("field_id", ""))
    unit = review.get("llm_better_unit", "")
    if not indicator or not unit:
        return True, "unit_not_checked", 0
    if unit_matches_indicator(unit, indicator):
        return True, "unit_matches_indicator", 20
    if looks_rate_or_intensity_unit(unit) and not indicator_allows_intensity(indicator):
        return False, "unit_scope_mismatch", -100
    return False, "unit_unmatched_rejected", -100


def candidate_sort(row: dict[str, str]) -> tuple[int, float, int]:
    return (
        1 if row.get("candidate_status") == "candidate_found" else 0,
        parse_float(row.get("confidence_rule", ""), 0.0),
        -parse_rank(row.get("candidate_rank", "")),
    )


def find_target_row(review: dict[str, str], rows: list[dict[str, str]]) -> dict[str, str] | None:
    target_value = parse_number(review.get("candidate_value", ""))
    target_unit = str(review.get("candidate_unit_raw", "") or "").strip()
    for row in rows:
        if row.get("candidate_status") != "candidate_found":
            continue
        row_value = parse_number(row.get("value_candidate", ""))
        row_unit = str(row.get("unit_raw_candidate", "") or "").strip()
        if target_value and row_value == target_value and (not target_unit or row_unit == target_unit):
            return row
    return None


def group_already_has_better_value(review: dict[str, str], rows: list[dict[str, str]]) -> bool:
    better_value = parse_number(review.get("llm_better_value", ""))
    better_unit = normalize_unit_text(review.get("llm_better_unit", ""))
    if not better_value:
        return False
    for row in rows:
        if row.get("candidate_status") != "candidate_found":
            continue
        row_value = parse_number(row.get("value_candidate", ""))
        row_unit = normalize_unit_text(row.get("unit_raw_candidate", ""))
        same_unit = not better_unit or row_unit == better_unit or units_compatible(row_unit, better_unit)
        already_promoted = parse_rank(row.get("candidate_rank", "")) == 1 or row.get("llm_review_source") == "deepseek_value_review"
        if row_value == better_value and same_unit and already_promoted:
            return True
    return False


def accepted_review(review: dict[str, str], min_confidence: float) -> tuple[bool, str, float, str]:
    decision = str(review.get("llm_review_decision", "")).strip()
    conf = parse_confidence(review.get("llm_confidence", ""))
    better_value = parse_number(review.get("llm_better_value", ""))
    if decision not in ACCEPT_DECISIONS:
        return False, "decision_not_replaceable", conf, better_value
    if conf < min_confidence:
        return False, "confidence_below_threshold", conf, better_value
    if not better_value:
        return False, "missing_parseable_better_value", conf, better_value
    return True, "accepted", conf, better_value


def apply_reviews(
    base_rows: list[dict[str, str]],
    review_rows: list[dict[str, str]],
    min_confidence: float,
    indicators: dict[str, dict[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], Counter]:
    indicators = indicators or {}
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in base_rows:
        groups[key(row)].append(row)

    audit: list[dict[str, Any]] = []
    counts: Counter = Counter()
    reviews_by_key: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for review in review_rows:
        reviews_by_key[key(review)].append(review)

    for review_key, keyed_reviews in reviews_by_key.items():
        selectable: list[dict[str, Any]] = []
        for review in keyed_reviews:
            ok, reason, conf, better_value = accepted_review(review, min_confidence)
            if not ok:
                counts[reason] += 1
                audit.append({**audit_base(review), "accepted": "no", "skip_reason": reason, "normalized_confidence": conf})
                continue
            unit_ok, unit_reason, unit_score = validate_review_unit(review, indicators)
            if not unit_ok:
                counts[unit_reason] += 1
                audit.append(
                    {
                        **audit_base(review),
                        "accepted": "no",
                        "skip_reason": unit_reason,
                        "unit_validation": unit_reason,
                        "normalized_confidence": conf,
                    }
                )
                continue
            if group_already_has_better_value(review, groups.get(review_key, [])):
                counts["already_applied_same_value"] += 1
                audit.append(
                    {
                        **audit_base(review),
                        "accepted": "no",
                        "skip_reason": "already_applied_same_value",
                        "unit_validation": unit_reason,
                        "normalized_confidence": conf,
                    }
                )
                continue
            target = find_target_row(review, groups.get(review_key, []))
            if not target:
                counts["target_row_not_found"] += 1
                audit.append({**audit_base(review), "accepted": "no", "skip_reason": "target_row_not_found", "normalized_confidence": conf})
                continue
            selectable.append(
                {
                    "review": review,
                    "target": target,
                    "conf": conf,
                    "better_value": better_value,
                    "unit_validation": unit_reason,
                    "selection_score": round(conf * 100 + unit_score, 4),
                }
            )

        if not selectable:
            continue
        best = max(
            selectable,
            key=lambda item: (
                item["selection_score"],
                item["conf"],
                -parse_rank(item["target"].get("candidate_rank", "")),
                item["review"].get("queue_id", ""),
            ),
        )
        selected_queue_id = best["review"].get("queue_id", "")
        for item in selectable:
            if item is best:
                continue
            counts["superseded_by_better_review"] += 1
            audit.append(
                {
                    **audit_base(item["review"]),
                    "accepted": "no",
                    "skip_reason": "superseded_by_better_review",
                    "unit_validation": item["unit_validation"],
                    "normalized_confidence": item["conf"],
                    "selection_score": item["selection_score"],
                    "selected_queue_id": selected_queue_id,
                }
            )

        review = best["review"]
        target = best["target"]
        conf = best["conf"]
        better_value = best["better_value"]
        old = {
            "value": target.get("value_candidate", ""),
            "unit": target.get("unit_raw_candidate", ""),
            "rank": target.get("candidate_rank", ""),
            "confidence": target.get("confidence_rule", ""),
        }
        target["value_candidate"] = better_value
        if review.get("llm_better_unit"):
            target["unit_raw_candidate"] = review.get("llm_better_unit", "")
        if review.get("llm_better_page"):
            target["source_page"] = review.get("llm_better_page", "")
        target["value_status"] = "exact_value_candidate"
        target["value_extraction_method"] = "deepseek_value_review"
        target["candidate_rank"] = "1"
        target["confidence_rule"] = str(max(parse_float(target.get("confidence_rule", ""), 0.0), conf, 0.94))
        target["needs_llm_review"] = "no"
        note = f"{SCRIPT_VERSION}:{review.get('queue_id', '')}:{review.get('llm_review_decision', '')}"
        target["review_reason"] = (target.get("review_reason", "") + " | " + note).strip(" |")
        target["llm_review_decision"] = review.get("llm_review_decision", "")
        target["llm_review_confidence"] = str(conf)
        target["llm_review_reason"] = compact_text(review.get("llm_reason", ""), 500)
        target["llm_review_source"] = "deepseek_value_review"
        target["extractor_version"] = (target.get("extractor_version", "") + f"+{SCRIPT_VERSION}").strip("+")

        for row in groups.get(review_key, []):
            if row is target:
                continue
            old_rank = parse_rank(row.get("candidate_rank", ""), 999)
            row["candidate_rank"] = str(max(2, old_rank + 1 if old_rank < 999 else 9))
            if parse_float(row.get("confidence_rule", ""), 0.0) >= parse_float(target.get("confidence_rule", ""), 0.0):
                row["confidence_rule"] = "0.62"
            row["needs_llm_review"] = row.get("needs_llm_review", "") or "yes"

        counts["accepted"] += 1
        audit.append(
            {
                **audit_base(review),
                "accepted": "yes",
                "skip_reason": "",
                "unit_validation": best["unit_validation"],
                "normalized_confidence": conf,
                "selection_score": best["selection_score"],
                "selected_queue_id": selected_queue_id,
                "old_value": old["value"],
                "old_unit": old["unit"],
                "new_value": target.get("value_candidate", ""),
                "new_unit": target.get("unit_raw_candidate", ""),
                "old_rank": old["rank"],
                "old_confidence": old["confidence"],
                "new_confidence": target.get("confidence_rule", ""),
            }
        )
    return audit, counts


def audit_base(review: dict[str, str]) -> dict[str, Any]:
    return {
        "queue_id": review.get("queue_id", ""),
        "sample_id": review.get("sample_id", ""),
        "field_id": review.get("field_id", ""),
        "metric_name": review.get("metric_name", ""),
        "candidate_value": review.get("candidate_value", ""),
        "candidate_unit_raw": review.get("candidate_unit_raw", ""),
        "llm_review_decision": review.get("llm_review_decision", ""),
        "llm_auto_fix_action": review.get("llm_auto_fix_action", ""),
        "llm_confidence": review.get("llm_confidence", ""),
        "llm_better_value": review.get("llm_better_value", ""),
        "llm_better_unit": review.get("llm_better_unit", ""),
        "llm_better_page": review.get("llm_better_page", ""),
        "llm_reason": compact_text(review.get("llm_reason", ""), 800),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-csv", type=Path, required=True)
    parser.add_argument("--review-csv", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--audit-csv", type=Path)
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--min-confidence", type=float, default=0.85)
    parser.add_argument("--indicator-csv", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base_rows, base_fields = load_csv(args.base_csv)
    review_rows, _ = load_csv(args.review_csv)
    indicators = load_indicator_map(args.indicator_csv)
    audit_rows, counts = apply_reviews(base_rows, review_rows, args.min_confidence, indicators)

    output_fields = list(base_fields)
    for field in ["llm_review_decision", "llm_review_confidence", "llm_review_reason", "llm_review_source"]:
        if field not in output_fields:
            output_fields.append(field)
    audit_fields = [
        "queue_id",
        "sample_id",
        "field_id",
        "metric_name",
        "candidate_value",
        "candidate_unit_raw",
        "llm_review_decision",
        "llm_auto_fix_action",
        "llm_confidence",
        "normalized_confidence",
        "llm_better_value",
        "llm_better_unit",
        "llm_better_page",
        "accepted",
        "skip_reason",
        "unit_validation",
        "selection_score",
        "selected_queue_id",
        "old_value",
        "old_unit",
        "new_value",
        "new_unit",
        "old_rank",
        "old_confidence",
        "new_confidence",
        "llm_reason",
    ]
    audit_csv = args.audit_csv or args.output_csv.with_name(args.output_csv.stem + "_audit.csv")
    summary_json = args.summary_json or args.output_csv.with_name(args.output_csv.stem + "_summary.json")
    write_csv(args.output_csv, base_rows, output_fields)
    write_csv(audit_csv, audit_rows, audit_fields)
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "script_version": SCRIPT_VERSION,
        "base_csv": str(args.base_csv),
        "review_csv": str(args.review_csv),
        "output_csv": str(args.output_csv),
        "audit_csv": str(audit_csv),
        "review_rows": len(review_rows),
        "indicator_csv": str(args.indicator_csv) if args.indicator_csv else "",
        "counts": dict(counts),
        "min_confidence": args.min_confidence,
    }
    write_json(summary_json, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
