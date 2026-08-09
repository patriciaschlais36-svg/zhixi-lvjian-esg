# -*- coding: utf-8 -*-
"""Triage DeepSeek recall results before any result is allowed to affect outputs.

This script is intentionally conservative. It separates model recall results
into safe candidates, review-needed cases, and rejected/contradictory cases.
It never modifies the main extraction CSV.
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
DEFAULT_INDICATOR = BASE_DIR / "算法源码" / "配置" / "ESG指标体系.csv"
DEFAULT_RECALL = (
    BASE_DIR
    / "评估测试"
    / "candidate_quality_v2.20_200samples_precision_gated"
    / "deepseek文本富集召回结果_v2.34_priority_balanced.csv"
)
DEFAULT_QUEUE = (
    BASE_DIR
    / "评估测试"
    / "candidate_quality_v2.20_200samples_precision_gated"
    / "文本富集低覆盖DeepSeek召回队列_v2.34_priority_balanced.csv"
)
DEFAULT_QUAL_RULES = BASE_DIR / "算法源码" / "配置" / "定性指标披露规则.csv"
DEFAULT_OUT_DIR = BASE_DIR / "评估测试" / "deepseek_text_rich_recall_triage_v2.34"

NEGATIVE_REASON_CUES = [
    "但未",
    "未明确",
    "未披露",
    "未提供",
    "未给出",
    "无法",
    "不能",
    "不等同",
    "仅提及",
    "只是",
]

QUALITATIVE_SUPPORT_TERMS = {
    "E_T_005": ["环境管理制度", "环境保护管理", "环保管理", "ISO14001", "环保责任", "环境保护专职", "环境管理体系"],
    "E_T_009": ["污染", "废水", "污水", "废气", "固废", "危废", "噪声", "粉尘", "VOCs", "排放", "环保设施", "治理"],
    "S_T_001": ["劳动合同", "社会保险", "薪酬", "工资", "奖金", "福利", "公积金", "职工权益", "员工权益", "工会"],
    "G_T_003": ["风险管理", "风险", "内控", "内部控制", "防控", "三道防线", "四位一体"],
    "G_T_004": ["内控", "内部控制", "合规", "审计", "监督", "自我评价"],
    "G_T_005": ["反腐", "反贿赂", "廉政", "廉洁", "不正当竞争", "举报", "商业道德", "监督检查"],
}

STRICT_POSITIVE_CUE_FIELDS = {
    "E_T_001",
    "E_T_003",
    "G_T_001",
    "G_T_002",
    "G_T_009",
    "S_T_005",
    "S_T_010",
}

QUALITATIVE_SUPPORT_TERMS.update(
    {
        "G_T_001": [
            "治理架构",
            "公司治理",
            "董事会下设",
            "专门委员会",
            "审计委员会",
            "战略决策委员会",
            "战略发展委员会",
            "薪酬与考核委员会",
            "提名委员会",
            "关联交易控制",
        ],
        "G_T_002": [
            "董事会下设",
            "审计委员会",
            "监督职能",
            "监督效能",
            "承接监督",
            "可持续发展",
            "社会责任",
            "规范运作",
        ],
        "G_T_003": [
            "风险管理",
            "内部监管体系",
            "内部控制",
            "内控",
            "四位一体",
            "风险防控",
            "流程管控",
        ],
        "G_T_004": [
            "内控",
            "内部控制",
            "合规",
            "合规部门",
            "风险管控",
            "经营规范",
            "内部管理和控制制度",
        ],
        "G_T_005": [
            "反腐",
            "反贪",
            "廉政",
            "廉洁",
            "公平竞争",
            "反不正当竞争",
            "监督检查",
            "招标采购",
        ],
        "E_T_005": [
            "环境管理体系",
            "环境保护管理制度",
            "环保管理制度",
            "环境保护专职管理部门",
            "环保管理",
            "环保责任制",
            "环保管理制度和责任制",
        ],
    }
)


def load_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_float(value: str) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def indicator_map(path: Path) -> dict[str, dict[str, str]]:
    return {row.get("field_id", ""): row for row in load_rows(path)}


def row_key(row: dict[str, str]) -> tuple[str, str]:
    return row.get("sample_id", ""), row.get("field_id", "")


def queue_map(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    out: dict[tuple[str, str], dict[str, str]] = {}
    for row in load_rows(path):
        key = row_key(row)
        existing = out.get(key)
        if not existing or len(row.get("evidence_snippet", "")) > len(existing.get("evidence_snippet", "")):
            out[key] = row
    return out


def qualitative_rule_map(path: Path) -> dict[str, dict[str, str]]:
    return {row.get("field_id", ""): row for row in load_rows(path)}


def has_negative_reason(reason: str) -> bool:
    return any(cue in (reason or "") for cue in NEGATIVE_REASON_CUES)


def split_terms(value: str) -> list[str]:
    terms: list[str] = []
    for part in re.split(r"[;；、,，\s]+", value or ""):
        part = part.strip()
        if len(part) >= 2 and part not in terms:
            terms.append(part)
    return terms


def qualitative_hits(field_id: str, rule: dict[str, str], evidence: str) -> list[str]:
    terms = split_terms(rule.get("positive_evidence_cues", ""))
    terms.extend(QUALITATIVE_SUPPORT_TERMS.get(field_id, []))
    hits = [term for term in dict.fromkeys(terms) if term and term in evidence]
    return hits


def qualitative_rule_issues(
    row: dict[str, str],
    rule: dict[str, str],
    queue_row: dict[str, str],
) -> tuple[list[str], list[str], str]:
    field_id = row.get("field_id", "")
    evidence = queue_row.get("evidence_snippet", "")
    if not rule:
        return [], [], evidence
    hits = qualitative_hits(field_id, rule, evidence)
    issues: list[str] = []
    if not evidence:
        issues.append("missing_queue_evidence_snippet")
    if field_id in STRICT_POSITIVE_CUE_FIELDS and not hits:
        issues.append("strict_qualitative_positive_cue_missing")
    elif not hits:
        issues.append("qualitative_support_cue_missing")
    return issues, hits, evidence


def triage_row(
    row: dict[str, str],
    indicator: dict[str, str],
    min_confidence: float,
    queue_row: dict[str, str],
    qual_rule: dict[str, str],
) -> dict[str, Any]:
    status = row.get("llm_status", "")
    confidence = parse_float(row.get("llm_confidence", "")) or 0.0
    metric_type = indicator.get("metric_type", "")
    value = row.get("llm_value", "").strip()
    source_page = row.get("llm_source_page", "").strip()
    reason = row.get("llm_reason", "").strip()
    rule_issues, rule_hits, evidence = qualitative_rule_issues(row, qual_rule, queue_row) if metric_type == "qualitative" else ([], [], queue_row.get("evidence_snippet", ""))

    issues: list[str] = []
    if status != "disclosed":
        if status == "needs_review":
            triage_status = "model_needs_review"
        else:
            triage_status = "not_recalled"
        return {
            **row,
            "metric_type": metric_type,
            "triage_status": triage_status,
            "triage_issues": "",
            "triage_confidence": confidence,
            "source_evidence_snippet": evidence,
            "qualitative_rule_hits": ";".join(rule_hits),
            "qualitative_rule_version": qual_rule.get("rule_version", ""),
        }

    if confidence < min_confidence:
        issues.append("low_llm_confidence")
    if not source_page:
        issues.append("missing_source_page")
    if has_negative_reason(reason):
        issues.append("reason_contains_negative_cue")
    if metric_type == "quantitative" and not value:
        issues.append("missing_value_for_quantitative")
    if metric_type == "quantitative" and value and parse_float(value) is None:
        issues.append("non_numeric_value_for_quantitative")
    if metric_type == "qualitative" and len(value) < 8 and len(reason) < 30:
        issues.append("thin_qualitative_evidence")
    issues.extend(rule_issues)

    if not issues:
        triage_status = "safe_recall_candidate"
    elif any(issue in issues for issue in ["reason_contains_negative_cue", "missing_value_for_quantitative", "non_numeric_value_for_quantitative"]):
        triage_status = "reject_or_recheck"
    else:
        triage_status = "review_before_apply"

    return {
        **row,
        "metric_type": metric_type,
        "triage_status": triage_status,
        "triage_issues": ";".join(issues),
        "triage_confidence": confidence,
        "source_evidence_snippet": evidence,
        "qualitative_rule_hits": ";".join(rule_hits),
        "qualitative_rule_version": qual_rule.get("rule_version", ""),
    }


def write_report(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# DeepSeek text-rich 召回结果三分流报告 v1.0",
        "",
        f"生成时间：{summary['generated_at']}",
        "",
        "## 说明",
        "",
        "- 本报告不回写主结果，只对 DeepSeek 召回结果做保守分流。",
        "- `safe_recall_candidate` 仍建议在小样本上抽查后再接入生产回写。",
        "- `reject_or_recheck` 常见原因是模型答案自相矛盾，例如状态为 disclosed 但 reason 写着“未明确披露”。",
        "",
        "## 状态分布",
        "",
        "| triage_status | count |",
        "|---|---:|",
    ]
    for key, count in summary["triage_counts"].items():
        lines.append(f"| {key} | {count} |")
    lines.extend(["", "## 问题码", "", "| issue | count |", "|---|---:|"])
    for key, count in summary["issue_counts"].items():
        lines.append(f"| {key} | {count} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recall-csv", type=Path, default=DEFAULT_RECALL)
    parser.add_argument("--queue-csv", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--indicator-csv", type=Path, default=DEFAULT_INDICATOR)
    parser.add_argument("--qualitative-rules-csv", type=Path, default=DEFAULT_QUAL_RULES)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--min-confidence", type=float, default=0.80)
    args = parser.parse_args()

    indicators = indicator_map(args.indicator_csv)
    queues = queue_map(args.queue_csv)
    qual_rules = qualitative_rule_map(args.qualitative_rules_csv)
    rows = [
        triage_row(
            row,
            indicators.get(row.get("field_id", ""), {}),
            args.min_confidence,
            queues.get(row_key(row), {}),
            qual_rules.get(row.get("field_id", ""), {}),
        )
        for row in load_rows(args.recall_csv)
    ]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    triage_csv = args.out_dir / "deepseek_text_rich_recall_triage_v1.0.csv"
    safe_csv = args.out_dir / "safe_recall_candidates_v1.0.csv"
    review_csv = args.out_dir / "review_before_apply_v1.0.csv"
    summary_json = args.out_dir / "deepseek_text_rich_recall_triage_summary_v1.0.json"
    report_md = args.out_dir / "deepseek_text_rich_recall_triage_report_v1.0.md"

    fieldnames = list(rows[0].keys()) if rows else []
    write_csv(triage_csv, rows, fieldnames)
    write_csv(safe_csv, [row for row in rows if row["triage_status"] == "safe_recall_candidate"], fieldnames)
    write_csv(review_csv, [row for row in rows if row["triage_status"] in {"review_before_apply", "reject_or_recheck", "model_needs_review"}], fieldnames)

    issue_counts: Counter[str] = Counter()
    for row in rows:
        for issue in str(row.get("triage_issues", "")).split(";"):
            if issue:
                issue_counts[issue] += 1
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "recall_csv": str(args.recall_csv),
        "row_count": len(rows),
        "triage_counts": dict(Counter(row["triage_status"] for row in rows)),
        "issue_counts": dict(issue_counts),
        "triage_csv": str(triage_csv),
        "safe_csv": str(safe_csv),
        "review_csv": str(review_csv),
        "summary_json": str(summary_json),
        "report_md": str(report_md),
    }
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(report_md, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
