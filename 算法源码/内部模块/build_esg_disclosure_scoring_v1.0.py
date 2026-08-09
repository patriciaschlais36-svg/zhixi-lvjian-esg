# -*- coding: utf-8 -*-
"""Build transparent ESG disclosure and machine-verification scores.

The score is not a proprietary ESG rating. It measures disclosure completeness,
source quality, and automatic verification risk based on the extracted dataset.
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
DEFAULT_VERIFIED = BASE_DIR / "评估测试" / "auto_verification_v2.24" / "auto_verified_extraction_results_v1.0.csv"
DEFAULT_INDICATOR = BASE_DIR / "算法源码" / "配置" / "ESG指标体系.csv"
DEFAULT_OUT_DIR = BASE_DIR / "评估测试" / "esg_disclosure_scoring_v2.24"


STATUS_FACTOR = {
    "auto_verified_high": 1.00,
    "auto_verified_medium": 0.82,
    "review_recommended": 0.55,
    "high_risk_auto_review": 0.20,
    "blocked_by_precision_gate": 0.00,
    "not_extracted_needs_gold_or_recall_check": 0.00,
}

PRIORITY_WEIGHT = {"P0": 1.50, "P1": 1.00, "P2": 0.60}


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value or "").strip())
    except ValueError:
        return default


def parse_rank(value: Any, default: int = 9999) -> int:
    try:
        return int(float(str(value or "").strip()))
    except ValueError:
        return default


def load_indicator_map(path: Path) -> dict[str, dict[str, str]]:
    return {row.get("field_id", ""): row for row in load_rows(path)}


def scoring_row_key(row: dict[str, str]) -> tuple[str, str]:
    return row.get("sample_id", ""), row.get("field_id", "")


def best_scoring_row(rows: list[dict[str, str]]) -> dict[str, str]:
    return max(
        rows,
        key=lambda row: (
            1 if row.get("candidate_status") == "candidate_found" else 0,
            STATUS_FACTOR.get(row.get("auto_verification_status", ""), 0.0),
            parse_float(row.get("auto_verification_score"), 0.0),
            parse_float(row.get("confidence_rule"), 0.0),
            -parse_rank(row.get("candidate_rank")),
        ),
    )


def collapse_to_field_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], dict[str, int]]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[scoring_row_key(row)].append(row)
    collapsed = [best_scoring_row(group) for group in grouped.values()]
    return collapsed, {
        "source_rows": len(rows),
        "field_rows": len(collapsed),
        "multi_candidate_groups": sum(1 for group in grouped.values() if len(group) > 1),
        "deduped_rows": len(rows) - len(collapsed),
    }


def indicator_weight(ind: dict[str, str], row: dict[str, str]) -> float:
    priority = row.get("extraction_priority") or ind.get("extraction_priority", "")
    materiality = parse_float(ind.get("materiality_weight_default"), 1.0) or 1.0
    return PRIORITY_WEIGHT.get(priority, 1.0) * materiality


def disclosure_points(row: dict[str, str]) -> float:
    if row.get("candidate_status") != "candidate_found":
        return 0.0
    return STATUS_FACTOR.get(row.get("auto_verification_status", ""), 0.35)


def grade(score: float) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "E"


def build_scores(rows: list[dict[str, str]], indicators: dict[str, dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    rows, resolution_summary = collapse_to_field_rows(rows)
    indicator_rows: list[dict[str, Any]] = []
    dim_bucket: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: defaultdict(float))
    company_bucket: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(float))
    status_by_company: dict[str, Counter[str]] = defaultdict(Counter)

    for row in rows:
        fid = row.get("field_id", "")
        ind = indicators.get(fid, {})
        sample_id = row.get("sample_id", "")
        dim = row.get("dimension", "") or ind.get("dimension", "")
        weight = indicator_weight(ind, row)
        factor = disclosure_points(row)
        points = weight * factor
        score = round(factor * 100, 2)
        vscore = parse_float(row.get("auto_verification_score"), 0.0)
        status = row.get("auto_verification_status", "")

        indicator_rows.append(
            {
                "sample_id": sample_id,
                "stock_code": row.get("stock_code", ""),
                "short_name": row.get("short_name", ""),
                "field_id": fid,
                "dimension": dim,
                "metric_name_cn": row.get("metric_name_cn", ""),
                "metric_type": row.get("metric_type", ""),
                "extraction_priority": row.get("extraction_priority", ""),
                "weight": round(weight, 4),
                "candidate_status": row.get("candidate_status", ""),
                "auto_verification_status": status,
                "auto_verification_score": row.get("auto_verification_score", ""),
                "indicator_disclosure_score": score,
                "weighted_points": round(points, 4),
                "max_points": round(weight, 4),
                "value_candidate": row.get("value_candidate", ""),
                "unit_raw_candidate": row.get("unit_raw_candidate", ""),
                "source_page": row.get("source_page", ""),
                "evidence_type_candidate": row.get("evidence_type_candidate", ""),
                "auto_verification_issues": row.get("auto_verification_issues", ""),
            }
        )

        db = dim_bucket[(sample_id, dim)]
        db["weighted_points"] += points
        db["max_points"] += weight
        db["indicator_count"] += 1
        if row.get("candidate_status") == "candidate_found":
            db["candidate_found"] += 1
            db["verification_score_sum"] += vscore
            db["verification_score_count"] += 1
        else:
            db["no_candidate"] += 1

        cb = company_bucket[sample_id]
        cb["weighted_points"] += points
        cb["max_points"] += weight
        cb["indicator_count"] += 1
        cb["verification_score_sum"] += vscore if row.get("candidate_status") == "candidate_found" else 0
        cb["verification_score_count"] += 1 if row.get("candidate_status") == "candidate_found" else 0
        cb["candidate_found"] += 1 if row.get("candidate_status") == "candidate_found" else 0
        cb["no_candidate"] += 1 if row.get("candidate_status") != "candidate_found" else 0
        cb["stock_code"] = row.get("stock_code", "")
        cb["short_name"] = row.get("short_name", "")
        cb["report_type"] = row.get("report_type", "")
        status_by_company[sample_id][status] += 1

    dimension_rows: list[dict[str, Any]] = []
    for (sample_id, dim), bucket in sorted(dim_bucket.items()):
        max_points = bucket["max_points"] or 1.0
        score = 100 * bucket["weighted_points"] / max_points
        dimension_rows.append(
            {
                "sample_id": sample_id,
                "dimension": dim,
                "dimension_score": round(score, 2),
                "dimension_grade": grade(score),
                "weighted_points": round(bucket["weighted_points"], 4),
                "max_points": round(bucket["max_points"], 4),
                "indicator_count": int(bucket["indicator_count"]),
                "candidate_found": int(bucket["candidate_found"]),
                "no_candidate": int(bucket["no_candidate"]),
                "candidate_coverage": round(bucket["candidate_found"] / bucket["indicator_count"], 4) if bucket["indicator_count"] else 0,
                "avg_verification_score": round(bucket["verification_score_sum"] / bucket["verification_score_count"], 2) if bucket["verification_score_count"] else 0,
            }
        )

    dim_scores: dict[str, dict[str, float]] = defaultdict(dict)
    for row in dimension_rows:
        dim_scores[row["sample_id"]][row["dimension"]] = row["dimension_score"]

    company_rows: list[dict[str, Any]] = []
    for sample_id, bucket in sorted(company_bucket.items()):
        max_points = bucket["max_points"] or 1.0
        weighted_score = 100 * bucket["weighted_points"] / max_points
        e = dim_scores[sample_id].get("E", 0.0)
        s = dim_scores[sample_id].get("S", 0.0)
        g = dim_scores[sample_id].get("G", 0.0)
        balanced_score = (e + s + g) / 3
        final_score = 0.65 * balanced_score + 0.35 * weighted_score
        status_counts = status_by_company[sample_id]
        company_rows.append(
            {
                "sample_id": sample_id,
                "stock_code": bucket["stock_code"],
                "short_name": bucket["short_name"],
                "report_type": bucket["report_type"],
                "esg_disclosure_score": round(final_score, 2),
                "esg_disclosure_grade": grade(final_score),
                "balanced_dimension_score": round(balanced_score, 2),
                "weighted_indicator_score": round(weighted_score, 2),
                "E_score": round(e, 2),
                "S_score": round(s, 2),
                "G_score": round(g, 2),
                "indicator_count": int(bucket["indicator_count"]),
                "candidate_found": int(bucket["candidate_found"]),
                "no_candidate": int(bucket["no_candidate"]),
                "candidate_coverage": round(bucket["candidate_found"] / bucket["indicator_count"], 4) if bucket["indicator_count"] else 0,
                "avg_verification_score": round(bucket["verification_score_sum"] / bucket["verification_score_count"], 2) if bucket["verification_score_count"] else 0,
                "auto_verified_high": status_counts.get("auto_verified_high", 0),
                "auto_verified_medium": status_counts.get("auto_verified_medium", 0),
                "review_recommended": status_counts.get("review_recommended", 0),
                "high_risk_auto_review": status_counts.get("high_risk_auto_review", 0),
                "blocked_by_precision_gate": status_counts.get("blocked_by_precision_gate", 0),
                "not_extracted_needs_gold_or_recall_check": status_counts.get("not_extracted_needs_gold_or_recall_check", 0),
            }
        )
    company_rows.sort(key=lambda row: row["esg_disclosure_score"], reverse=True)

    scores = [row["esg_disclosure_score"] for row in company_rows]
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "company_count": len(company_rows),
        "indicator_rows": len(indicator_rows),
        "field_resolution": resolution_summary,
        "score_avg": round(sum(scores) / len(scores), 2) if scores else 0,
        "score_min": min(scores) if scores else 0,
        "score_max": max(scores) if scores else 0,
        "grade_counts": dict(Counter(row["esg_disclosure_grade"] for row in company_rows)),
        "method_note": "Transparent disclosure completeness + automatic verification quality score; not a proprietary ESG rating and not a gold-label accuracy metric.",
    }
    return company_rows, dimension_rows, indicator_rows, summary


def write_report(path: Path, company_rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    top = company_rows[:10]
    bottom = list(reversed(company_rows[-10:])) if company_rows else []
    lines = [
        "# ESG 披露评分报告 v1.0",
        "",
        f"生成时间：{summary['generated_at']}",
        "",
        "## 说明",
        "",
        "- 该评分衡量披露完整性、证据质量与自动核验风险，不是第三方 ESG 评级复刻。",
        "- 当前评分不依赖人工金标；金标完成后可校准权重和核验阈值。",
        "- 缺失披露不会被视为算法错误，但会降低披露完整性得分。",
        "",
        "## 总览",
        "",
        f"- 公司数：{summary['company_count']}",
        f"- 指标行数：{summary['indicator_rows']}",
        f"- 平均分：{summary['score_avg']}",
        f"- 最低分：{summary['score_min']}",
        f"- 最高分：{summary['score_max']}",
        f"- 等级分布：{summary['grade_counts']}",
        "",
        "## Top 10",
        "",
        "| sample | company | score | grade | E | S | G | coverage |",
        "|---|---|---:|---|---:|---:|---:|---:|",
    ]
    for row in top:
        lines.append(
            f"| {row['sample_id']} | {row['short_name']} | {row['esg_disclosure_score']} | {row['esg_disclosure_grade']} | "
            f"{row['E_score']} | {row['S_score']} | {row['G_score']} | {row['candidate_coverage']} |"
        )
    lines.extend(["", "## Bottom 10", "", "| sample | company | score | grade | E | S | G | coverage |", "|---|---|---:|---|---:|---:|---:|---:|"])
    for row in bottom:
        lines.append(
            f"| {row['sample_id']} | {row['short_name']} | {row['esg_disclosure_score']} | {row['esg_disclosure_grade']} | "
            f"{row['E_score']} | {row['S_score']} | {row['G_score']} | {row['candidate_coverage']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verified-csv", type=Path, default=DEFAULT_VERIFIED)
    parser.add_argument("--indicator-csv", type=Path, default=DEFAULT_INDICATOR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    company_rows, dimension_rows, indicator_rows, summary = build_scores(
        load_rows(args.verified_csv),
        load_indicator_map(args.indicator_csv),
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    company_csv = args.out_dir / "company_esg_disclosure_scores_v1.0.csv"
    dimension_csv = args.out_dir / "company_dimension_scores_v1.0.csv"
    indicator_csv = args.out_dir / "indicator_disclosure_scores_v1.0.csv"
    summary_json = args.out_dir / "esg_disclosure_scoring_summary_v1.0.json"
    report_md = args.out_dir / "esg_disclosure_scoring_report_v1.0.md"

    write_csv(company_csv, company_rows, list(company_rows[0].keys()) if company_rows else [])
    write_csv(dimension_csv, dimension_rows, list(dimension_rows[0].keys()) if dimension_rows else [])
    write_csv(indicator_csv, indicator_rows, list(indicator_rows[0].keys()) if indicator_rows else [])
    summary.update(
        {
            "company_csv": str(company_csv),
            "dimension_csv": str(dimension_csv),
            "indicator_csv": str(indicator_csv),
            "summary_json": str(summary_json),
            "report_md": str(report_md),
        }
    )
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(report_md, company_rows, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
