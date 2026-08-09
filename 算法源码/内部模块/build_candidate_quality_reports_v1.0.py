# -*- coding: utf-8 -*-
"""生成候选来源分布与低置信复核队列。"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path


BASE = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = (
    BASE
    / "算法方案"
    / "pilot_full_extraction_v2.4_30samples"
    / "全量指标候选抽取结果_30份v2.5_generic_kpi_engineered.csv"
)
DEFAULT_OUT_DIR = BASE / "评估测试" / "candidate_quality_v1.0"


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def safe_float(value: str) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_distribution(rows: list[dict[str, str]]) -> dict[str, object]:
    status = Counter(row.get("candidate_status", "") for row in rows)
    evidence = Counter(row.get("evidence_type_candidate", "") for row in rows if row.get("candidate_status") == "candidate_found")
    method = Counter(row.get("value_extraction_method", "") for row in rows if row.get("candidate_status") == "candidate_found")
    dimension = Counter(row.get("dimension", "") for row in rows if row.get("candidate_status") == "candidate_found")

    by_sample: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        sid = row.get("sample_id", "")
        by_sample[sid][row.get("candidate_status", "")] += 1

    return {
        "input_rows": len(rows),
        "candidate_status": dict(status),
        "evidence_type_candidate": dict(evidence.most_common()),
        "value_extraction_method_top30": dict(method.most_common(30)),
        "dimension_found": dict(dimension),
        "by_sample": {sid: dict(counter) for sid, counter in sorted(by_sample.items())},
    }


def build_low_conf_queue(rows: list[dict[str, str]], threshold: float) -> list[dict[str, str]]:
    queue: list[dict[str, str]] = []
    for row in rows:
        if row.get("candidate_status") != "candidate_found":
            continue
        conf = safe_float(row.get("confidence_rule", ""))
        needs_review = row.get("needs_llm_review") == "yes"
        if conf >= threshold and not needs_review:
            continue
        queue.append(
            {
                "sample_id": row.get("sample_id", ""),
                "field_id": row.get("field_id", ""),
                "dimension": row.get("dimension", ""),
                "metric_name_cn": row.get("metric_name_cn", ""),
                "value_candidate": row.get("value_candidate", ""),
                "unit_raw_candidate": row.get("unit_raw_candidate", ""),
                "confidence_rule": row.get("confidence_rule", ""),
                "needs_llm_review": row.get("needs_llm_review", ""),
                "evidence_type_candidate": row.get("evidence_type_candidate", ""),
                "value_extraction_method": row.get("value_extraction_method", ""),
                "source_page": row.get("source_page", ""),
                "review_reason": row.get("review_reason", ""),
                "source_text": row.get("source_text", "")[:1200],
            }
        )
    return sorted(queue, key=lambda r: (r["sample_id"], safe_float(r["confidence_rule"])))


def build_priority_queue(rows: list[dict[str, str]], threshold: float, max_records: int) -> list[dict[str, str]]:
    """构建更适合 LLM/人工的优先复核队列。

    全量 needs_llm_review 队列会很大；优先队列聚焦数值型、低置信、单位/口径风险更高的候选。
    """
    scored: list[tuple[float, dict[str, str]]] = []
    for row in rows:
        if row.get("candidate_status") != "candidate_found":
            continue
        if row.get("metric_type") != "quantitative":
            continue
        conf = safe_float(row.get("confidence_rule", ""))
        method = row.get("value_extraction_method", "")
        evidence = row.get("evidence_type_candidate", "")
        reason = row.get("review_reason", "")
        risk = 0.0
        if conf < threshold:
            risk += (threshold - conf) * 10
        if row.get("needs_llm_review") == "yes":
            risk += 1.0
        if any(token in method for token in ["generic_kpi", "numeric_calibration", "derived"]):
            risk += 1.5
        if evidence in {"ocr_text", "generic_kpi_year_table", "numeric_calibrated", "derived_indicator"}:
            risk += 1.0
        if not row.get("unit_raw_candidate"):
            risk += 0.5
        if "单位" in reason or "口径" in reason or "verify" in reason:
            risk += 0.5
        if risk <= 0:
            continue
        scored.append(
            (
                -risk,
                {
                    "sample_id": row.get("sample_id", ""),
                    "field_id": row.get("field_id", ""),
                    "dimension": row.get("dimension", ""),
                    "metric_name_cn": row.get("metric_name_cn", ""),
                    "value_candidate": row.get("value_candidate", ""),
                    "unit_raw_candidate": row.get("unit_raw_candidate", ""),
                    "confidence_rule": row.get("confidence_rule", ""),
                    "risk_score": f"{risk:.2f}",
                    "evidence_type_candidate": evidence,
                    "value_extraction_method": method,
                    "source_page": row.get("source_page", ""),
                    "review_reason": reason,
                    "source_text": row.get("source_text", "")[:1200],
                },
            )
        )
    return [item for _, item in sorted(scored, key=lambda pair: (pair[0], pair[1]["sample_id"]))[:max_records]]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--threshold", type=float, default=0.92)
    parser.add_argument("--max-priority", type=int, default=300)
    args = parser.parse_args()

    rows = load_rows(args.input)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    distribution = build_distribution(rows)
    dist_json = args.out_dir / "候选来源分布_v1.0.json"
    dist_json.write_text(json.dumps(distribution, ensure_ascii=False, indent=2), encoding="utf-8")

    dist_rows = []
    for group_name in ["candidate_status", "evidence_type_candidate", "value_extraction_method_top30", "dimension_found"]:
        for key, count in distribution[group_name].items():
            dist_rows.append({"group": group_name, "name": key, "count": str(count)})
    dist_csv = args.out_dir / "候选来源分布_v1.0.csv"
    write_csv(dist_csv, dist_rows, ["group", "name", "count"])

    queue = build_low_conf_queue(rows, args.threshold)
    queue_csv = args.out_dir / "低置信与需复核候选队列_v1.0.csv"
    write_csv(
        queue_csv,
        queue,
        [
            "sample_id",
            "field_id",
            "dimension",
            "metric_name_cn",
            "value_candidate",
            "unit_raw_candidate",
            "confidence_rule",
            "needs_llm_review",
            "evidence_type_candidate",
            "value_extraction_method",
            "source_page",
            "review_reason",
            "source_text",
        ],
    )

    priority = build_priority_queue(rows, args.threshold, args.max_priority)
    priority_csv = args.out_dir / "优先复核候选队列_v1.0.csv"
    write_csv(
        priority_csv,
        priority,
        [
            "sample_id",
            "field_id",
            "dimension",
            "metric_name_cn",
            "value_candidate",
            "unit_raw_candidate",
            "confidence_rule",
            "risk_score",
            "evidence_type_candidate",
            "value_extraction_method",
            "source_page",
            "review_reason",
            "source_text",
        ],
    )

    print(
        json.dumps(
            {
                "input": str(args.input),
                "distribution_json": str(dist_json),
                "distribution_csv": str(dist_csv),
                "low_conf_queue_csv": str(queue_csv),
                "low_conf_queue_count": len(queue),
                "priority_queue_csv": str(priority_csv),
                "priority_queue_count": len(priority),
                "threshold": args.threshold,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
