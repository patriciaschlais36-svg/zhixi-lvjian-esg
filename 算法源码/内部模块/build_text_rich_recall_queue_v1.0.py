# -*- coding: utf-8 -*-
"""Build a DeepSeek-ready recall queue for text-rich but low-coverage samples."""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_INDICATOR_JSON = BASE_DIR / "算法源码" / "配置" / "ESG指标体系.json"
DEFAULT_QUAL_RULES = BASE_DIR / "算法源码" / "配置" / "定性指标披露规则.csv"

GENERIC_ESG_CUES = [
    "关键绩效", "ESG绩效", "可持续发展", "环境", "社会", "治理", "员工",
    "排放", "能源", "用水", "董事会", "独立董事", "培训", "安全", "公益",
    "供应商", "反腐败", "合规", "绩效指标", "指标索引",
]


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_csv_map(path: Path, key: str) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return {row.get(key, ""): row for row in csv.DictReader(f)}


def load_indicators(path: Path) -> dict[str, dict[str, Any]]:
    data = load_json(path)
    return {str(item["field_id"]): item for item in data.get("indicators", [])}


def split_terms(value: str) -> list[str]:
    terms = []
    for part in re.split(r"[;；,，/、\s]+", value or ""):
        part = part.strip()
        if len(part) >= 2 and part not in terms:
            terms.append(part)
    return terms


def indicator_terms(indicator: dict[str, Any]) -> list[str]:
    terms = [indicator.get("metric_name_cn", "")]
    terms.extend(split_terms(indicator.get("aliases_cn", "")))
    terms.extend(split_terms(indicator.get("subtopic_cn", "")))
    return [term for term in dict.fromkeys(t for t in terms if t)]


def page_text_file(page_text_dir: Path, sample_id: str) -> Path | None:
    matches = sorted(page_text_dir.glob(f"{sample_id}_*_page_text.json"))
    if matches:
        return matches[0]
    direct = page_text_dir / f"{sample_id}_page_text.json"
    return direct if direct.exists() else None


def load_pages(page_text_dir: Path, sample_id: str) -> list[dict[str, Any]]:
    path = page_text_file(page_text_dir, sample_id)
    if not path:
        return []
    payload = load_json(path)
    return payload.get("pages", [])


def normalized_text(page: dict[str, Any]) -> str:
    text = page.get("text", "")
    if isinstance(text, list):
        text = " ".join(str(item) for item in text)
    return re.sub(r"\s+", " ", str(text or "")).strip()


def snippet(text: str, terms: list[str], width: int) -> str:
    positions = [text.find(term) for term in terms if term and text.find(term) >= 0]
    if not positions:
        positions = [text.find(term) for term in GENERIC_ESG_CUES if text.find(term) >= 0]
    pos = min(positions) if positions else 0
    start = max(0, pos - width // 2)
    end = min(len(text), pos + width // 2)
    return text[start:end]


def score_page(text: str, terms: list[str]) -> int:
    score = 0
    for term in terms:
        if term and term in text:
            score += 10 + min(len(term), 8)
    for cue in GENERIC_ESG_CUES:
        if cue in text:
            score += 2
    return score


def field_priority(row: dict[str, str], indicator: dict[str, Any]) -> int:
    """Prioritize likely evaluation gaps before truncating a per-sample queue."""
    priority = {"P0": 300, "P1": 120, "P2": 40}.get(
        indicator.get("extraction_priority", row.get("extraction_priority", "")),
        60,
    )
    metric_type = indicator.get("metric_type", row.get("metric_type", ""))
    if metric_type == "qualitative":
        priority += 60
    if indicator.get("review_risk_level", "") == "high":
        priority += 35
    if row.get("needs_llm_review", "").lower() == "yes":
        priority += 25
    if indicator.get("indicator_layer", row.get("indicator_layer", "")) == "core":
        priority += 20
    if indicator.get("scoring_role", row.get("scoring_role", "")) == "risk_event":
        priority += 50
    if indicator.get("scoring_role", row.get("scoring_role", "")) == "management_presence":
        priority += 30
    if indicator.get("rating_role", row.get("rating_role", "")) == "disclosure_quality":
        priority += 40
    return priority


def priority_reason(row: dict[str, str], indicator: dict[str, Any]) -> str:
    reasons = []
    for key in ["extraction_priority", "metric_type", "review_risk_level", "indicator_layer", "scoring_role", "rating_role"]:
        value = indicator.get(key, row.get(key, ""))
        if value:
            reasons.append(f"{key}={value}")
    if row.get("needs_llm_review", "").lower() == "yes":
        reasons.append("needs_llm_review=yes")
    return ";".join(reasons)


def choose_balanced(candidates: list[tuple[int, dict[str, str], dict[str, Any]]], limit: int) -> list[tuple[int, dict[str, str], dict[str, Any]]]:
    """Round-robin dimensions after priority sorting so one prefix cannot dominate."""
    if limit <= 0 or len(candidates) <= limit:
        return sorted(candidates, key=lambda item: (-item[0], item[1].get("field_id", "")))

    buckets: dict[str, list[tuple[int, dict[str, str], dict[str, Any]]]] = {}
    for item in sorted(candidates, key=lambda item: (-item[0], item[1].get("field_id", ""))):
        dim = item[2].get("dimension", item[1].get("dimension", "")) or "?"
        buckets.setdefault(dim, []).append(item)

    dimension_order = ["E", "S", "G"] + sorted(dim for dim in buckets if dim not in {"E", "S", "G"})
    selected: list[tuple[int, dict[str, str], dict[str, Any]]] = []
    while len(selected) < limit and any(buckets.get(dim) for dim in dimension_order):
        for dim in dimension_order:
            bucket = buckets.get(dim, [])
            if bucket and len(selected) < limit:
                selected.append(bucket.pop(0))
    return selected


def build_item(
    row: dict[str, str],
    indicator: dict[str, Any],
    qual_rule: dict[str, str],
    pages: list[dict[str, Any]],
    snippet_width: int,
) -> dict[str, str]:
    terms = indicator_terms(indicator)
    scored = []
    for page in pages:
        text = normalized_text(page)
        if not text:
            continue
        score = score_page(text, terms)
        if score > 0:
            scored.append((score, page, text))
    scored.sort(key=lambda item: item[0], reverse=True)
    top = scored[:3]
    if top:
        evidence = "\n\n".join(
            f"[page {item[1].get('page', item[1].get('page_num', ''))}] {snippet(item[2], terms, snippet_width)}"
            for item in top
        )
        pages_hit = ";".join(str(item[1].get("page", item[1].get("page_num", ""))) for item in top)
        score = str(top[0][0])
    else:
        text_pages = [(page, normalized_text(page)) for page in pages if normalized_text(page)]
        text_pages.sort(key=lambda item: len(item[1]), reverse=True)
        fallback = text_pages[:2]
        evidence = "\n\n".join(
            f"[page {page.get('page', page.get('page_num', ''))}] {snippet(text, terms, snippet_width)}"
            for page, text in fallback
        )
        pages_hit = ";".join(str(page.get("page", page.get("page_num", ""))) for page, _ in fallback)
        score = "0"
    return {
        "sample_id": row.get("sample_id", ""),
        "stock_code": row.get("stock_code", ""),
        "short_name": row.get("short_name", ""),
        "field_id": row.get("field_id", ""),
        "metric_name_cn": indicator.get("metric_name_cn", row.get("metric_name_cn", "")),
        "dimension": indicator.get("dimension", row.get("dimension", "")),
        "unit_normalized": indicator.get("unit_normalized", ""),
        "aliases_cn": indicator.get("aliases_cn", ""),
        "qualitative_minimum_acceptance": qual_rule.get("minimum_acceptance", ""),
        "qualitative_positive_evidence_cues": qual_rule.get("positive_evidence_cues", ""),
        "qualitative_reject_if_only": qual_rule.get("reject_if_only", ""),
        "qualitative_rule_version": qual_rule.get("rule_version", ""),
        "page_hits": pages_hit,
        "retrieval_score": score,
        "evidence_snippet": evidence,
        "llm_task": "判断该文本富集低覆盖样本是否披露该指标；若披露，返回原始值、原始单位、页码；若未披露，说明缺失；若发现新别名，给出alias_suggestion。",
    }


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else [
        "sample_id", "stock_code", "short_name", "field_id", "metric_name_cn",
        "dimension", "unit_normalized", "aliases_cn", "qualitative_minimum_acceptance",
        "qualitative_positive_evidence_cues", "qualitative_reject_if_only", "qualitative_rule_version", "page_hits",
        "retrieval_score", "evidence_snippet", "llm_task",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for idx, row in enumerate(rows, 1):
            payload = {
                "custom_id": f"recall_{idx}_{row['sample_id']}_{row['field_id']}",
                "sample_id": row["sample_id"],
                "field_id": row["field_id"],
                "messages": [
                    {
                        "role": "system",
                        "content": "你是ESG报告低覆盖召回专家，只能基于给定文本判断披露状态，并优先保留报告原始数值和单位。",
                    },
                    {
                        "role": "user",
                        "content": (
                            f"公司：{row['short_name']}（{row['stock_code']}）\n"
                            f"指标：{row['field_id']} {row['metric_name_cn']}\n"
                            f"标准单位：{row['unit_normalized']}\n"
                            f"已有别名：{row['aliases_cn']}\n"
                            f"最低认可证据：{row.get('qualitative_minimum_acceptance', '')}\n"
                            f"正向证据提示：{row.get('qualitative_positive_evidence_cues', '')}\n"
                            f"不认可边界：{row.get('qualitative_reject_if_only', '')}\n"
                            "请输出JSON：{disclosure_status,value,unit_raw,source_page,confidence,alias_suggestion,reason}。\n\n"
                            f"候选文本：\n{row['evidence_snippet']}"
                        ),
                    },
                ],
            }
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnosis-csv", type=Path, required=True)
    parser.add_argument("--extraction-csv", type=Path, required=True)
    parser.add_argument("--page-text-dir", type=Path, required=True)
    parser.add_argument("--indicator-json", type=Path, default=DEFAULT_INDICATOR_JSON)
    parser.add_argument("--qualitative-rules-csv", type=Path, default=DEFAULT_QUAL_RULES)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--output-jsonl", type=Path)
    parser.add_argument("--actions", default="alias_dictionary_and_deepseek_recall")
    parser.add_argument("--max-fields-per-sample", type=int, default=24)
    parser.add_argument("--snippet-width", type=int, default=1600)
    args = parser.parse_args()

    actions = {item.strip() for item in args.actions.split(",") if item.strip()}
    diagnosis_rows = load_rows(args.diagnosis_csv)
    target_samples = [
        row["sample_id"]
        for row in diagnosis_rows
        if row.get("recommended_action") in actions
    ]
    target_set = set(target_samples)
    indicators = load_indicators(args.indicator_json)
    qualitative_rules = load_csv_map(args.qualitative_rules_csv, "field_id")
    extraction_rows = load_rows(args.extraction_csv)

    rows: list[dict[str, str]] = []
    per_sample_count: dict[str, int] = {}
    page_cache: dict[str, list[dict[str, Any]]] = {}
    candidates_by_sample: dict[str, list[tuple[int, dict[str, str], dict[str, Any]]]] = {}
    for row in extraction_rows:
        sid = row.get("sample_id", "")
        if sid not in target_set:
            continue
        if row.get("candidate_status") != "no_candidate":
            continue
        indicator = indicators.get(row.get("field_id", ""))
        if not indicator:
            continue
        candidates_by_sample.setdefault(sid, []).append((field_priority(row, indicator), row, indicator))

    for sid, candidates in candidates_by_sample.items():
        if sid not in page_cache:
            page_cache[sid] = load_pages(args.page_text_dir, sid)
        selected = choose_balanced(candidates, args.max_fields_per_sample)
        for priority, row, indicator in selected:
            item = build_item(row, indicator, qualitative_rules.get(row.get("field_id", ""), {}), page_cache[sid], args.snippet_width)
            item["queue_priority_score"] = str(priority)
            item["queue_priority_reason"] = priority_reason(row, indicator)
            rows.append(item)
        per_sample_count[sid] = len(selected)

    write_csv(args.output_csv, rows)
    output_jsonl = args.output_jsonl or args.output_csv.with_suffix(".jsonl")
    write_jsonl(output_jsonl, rows)
    summary = {
        "diagnosis_csv": str(args.diagnosis_csv),
        "extraction_csv": str(args.extraction_csv),
        "output_csv": str(args.output_csv),
        "output_jsonl": str(output_jsonl),
        "target_sample_count": len(target_samples),
        "queue_rows": len(rows),
        "per_sample_count": per_sample_count,
        "selection_policy": "priority_sorted_dimension_balanced",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
