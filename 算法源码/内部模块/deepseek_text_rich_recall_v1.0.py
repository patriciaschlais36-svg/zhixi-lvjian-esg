# -*- coding: utf-8 -*-
"""DeepSeek recall for text-rich low-coverage ESG samples."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[2]


SYSTEM_PROMPT = """你是ESG报告低覆盖召回专家。任务是基于给定文本片段判断某个未命中指标是否其实已经披露。

严格规则：
1. 只能使用给定文本，不得使用外部知识。
2. 若文本明确披露指标，返回 disclosed，并保留报告原始数值和原始单位，不做单位换算。
3. 若文本只支持相近概念、案例值、项目值或口径不一致，返回 not_found 或 needs_review。
4. 若文本显示指标对该公司不适用，返回 not_applicable，并说明依据。
5. 若发现报告对该指标使用了新叫法，在 alias_suggestion 中给出。
6. 输出必须是纯 JSON 数组，不要 Markdown。"""


def load_api_config() -> tuple[str, str, str]:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
    config_path = Path(__file__).resolve().parent / "api_config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            ds = config.get("deepseek", {})
            api_key = os.environ.get("DEEPSEEK_API_KEY", ds.get("api_key", api_key))
            base_url = os.environ.get("DEEPSEEK_BASE_URL", ds.get("base_url", base_url))
            model = os.environ.get("DEEPSEEK_MODEL", ds.get("model", model))
        except Exception:
            pass
    return api_key, base_url, model


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def build_prompt(batch: list[dict[str, str]], evidence_char_limit: int = 2200) -> str:
    items = []
    for idx, row in enumerate(batch, 1):
        items.append(
            {
                "review_id": f"R{idx}",
                "sample_id": row.get("sample_id", ""),
                "company": row.get("short_name", ""),
                "stock_code": row.get("stock_code", ""),
                "field_id": row.get("field_id", ""),
                "metric_name": row.get("metric_name_cn", ""),
                "dimension": row.get("dimension", ""),
                "expected_unit": row.get("unit_normalized", ""),
                "known_aliases": row.get("aliases_cn", ""),
                "minimum_acceptance": row.get("qualitative_minimum_acceptance", ""),
                "positive_evidence_cues": row.get("qualitative_positive_evidence_cues", ""),
                "reject_if_only": row.get("qualitative_reject_if_only", ""),
                "page_hits": row.get("page_hits", ""),
                "evidence": row.get("evidence_snippet", "")[:evidence_char_limit],
            }
        )
    schema = [
        {
            "review_id": "R1",
            "disclosure_status": "disclosed|not_found|not_applicable|needs_review",
            "value": "",
            "unit_raw": "",
            "source_page": "",
            "confidence": 0.0,
            "alias_suggestion": "",
            "reason": "一句话说明判断依据",
        }
    ]
    return (
        "请逐项判断以下未命中指标是否可从文本中召回。若 minimum_acceptance / reject_if_only 不为空，必须按这些边界判断；"
        "文本只满足 reject_if_only 或只出现相近治理/项目/口号时，不要返回 disclosed。输出JSON数组，长度必须与输入一致。\n\n"
        f"输入：\n{json.dumps(items, ensure_ascii=False, indent=2)}\n\n"
        f"输出格式：\n{json.dumps(schema, ensure_ascii=False)}"
    )


def parse_json_response(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    block = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if block:
        text = block.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", text)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict):
        data = data.get("results") or data.get("reviews") or [data]
    return data if isinstance(data, list) else []


def call_deepseek(batch: list[dict[str, str]], max_retries: int = 1, evidence_char_limit: int = 2200) -> list[dict[str, Any]]:
    api_key, base_url, model = load_api_config()
    if not api_key:
        raise RuntimeError("DeepSeek API key not configured")
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
    prompt = SYSTEM_PROMPT + "\n\n" + build_prompt(batch, evidence_char_limit)
    kwargs = {
        "model": model,
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": prompt}],
        "thinking": {"type": "disabled"},
    }
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            response = client.messages.create(**kwargs)
            text = ""
            for block in response.content:
                if hasattr(block, "text") and block.text:
                    text += block.text
            parsed = parse_json_response(text)
            if parsed:
                return parsed
        except Exception as exc:
            last_error = exc
            if attempt < max_retries:
                time.sleep(2 + attempt * 2)
    if last_error:
        raise last_error
    return []


def merge(batch: list[dict[str, str]], reviews: list[dict[str, Any]]) -> list[dict[str, str]]:
    by_id = {str(item.get("review_id", "")): item for item in reviews}
    rows = []
    for idx, row in enumerate(batch, 1):
        review = by_id.get(f"R{idx}", {})
        rows.append(
            {
                "sample_id": row.get("sample_id", ""),
                "stock_code": row.get("stock_code", ""),
                "short_name": row.get("short_name", ""),
                "field_id": row.get("field_id", ""),
                "metric_name_cn": row.get("metric_name_cn", ""),
                "page_hits": row.get("page_hits", ""),
                "retrieval_score": row.get("retrieval_score", ""),
                "llm_status": str(review.get("disclosure_status", "needs_review")),
                "llm_value": str(review.get("value", "")),
                "llm_unit_raw": str(review.get("unit_raw", "")),
                "llm_source_page": str(review.get("source_page", "")),
                "llm_confidence": str(review.get("confidence", "")),
                "alias_suggestion": str(review.get("alias_suggestion", "")),
                "llm_reason": str(review.get("reason", "")),
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "sample_id", "stock_code", "short_name", "field_id", "metric_name_cn",
        "page_hits", "retrieval_score", "llm_status", "llm_value",
        "llm_unit_raw", "llm_source_page", "llm_confidence",
        "alias_suggestion", "llm_reason",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-id")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--evidence-char-limit", type=int, default=2200)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    queue = load_rows(args.queue)
    if args.sample_id:
        queue = [row for row in queue if row.get("sample_id") == args.sample_id]
    if args.limit > 0:
        queue = queue[: args.limit]

    existing: list[dict[str, str]] = []
    done: set[tuple[str, str]] = set()
    if args.resume and args.output.exists():
        existing = load_rows(args.output)
        done = {(row.get("sample_id", ""), row.get("field_id", "")) for row in existing}
    pending = [row for row in queue if (row.get("sample_id", ""), row.get("field_id", "")) not in done]

    results = list(existing)
    for start in range(0, len(pending), args.batch_size):
        batch = pending[start : start + args.batch_size]
        print(f"Recall batch {start // args.batch_size + 1}: {len(batch)} items", flush=True)
        reviews = call_deepseek(batch, evidence_char_limit=args.evidence_char_limit)
        results.extend(merge(batch, reviews))
        write_csv(args.output, results)
        time.sleep(0.3)

    counts: dict[str, int] = {}
    for row in results:
        counts[row.get("llm_status", "")] = counts.get(row.get("llm_status", ""), 0) + 1
    print(json.dumps({"output": str(args.output), "rows": len(results), "status_counts": counts}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
