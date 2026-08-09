# -*- coding: utf-8 -*-
"""DeepSeek 优先复核队列审查器 v1.0。

用途：
  - 读取 candidate_quality_v1.0/优先复核候选队列_v1.0.csv。
  - 优先调用 DeepSeek Anthropic-compatible API 复核候选值、单位与证据是否匹配。
  - 输出结构化复核结果，不默认改写主候选 CSV。

Claude Vision 仅用于图片页兜底，本脚本不调用 Claude。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parents[2]
DEFAULT_QUEUE = BASE / "评估测试" / "candidate_quality_v1.0" / "优先复核候选队列_v1.0.csv"
DEFAULT_OUT = BASE / "评估测试" / "candidate_quality_v1.0" / "deepseek优先复核结果_v1.0.csv"
INDICATOR_JSON = Path(os.environ.get(
    "ESG_INDICATOR_JSON",
    str(BASE / "算法源码" / "配置" / "ESG指标体系.json"),
))


SYSTEM_PROMPT = """你是ESG报告定量指标复核专家。你的任务不是重新抽取整份报告，而是判断给定候选值是否被证据文本支持。

请严格遵守：
1. 只根据证据文本判断，不要使用外部知识。
2. 核对指标名称、候选值、单位、年份/报告期、上下文口径是否匹配。
3. 如果候选值是绿色债券项目减排量、案例值、单项项目值，而指标需要公司总量，应 reject 或 needs_review。
4. 如果证据明确支持候选值，decision=accept。
5. 如果证据支持但候选值或单位可明显修正，decision=modify，并给出 corrected_value/corrected_unit。
6. 如果证据不支持、指标口径不符、年份不明、单位跨量纲冲突，decision=reject 或 needs_review。
7. 抽取层必须保留报告原始数值和原始单位，不要把“万元”换算为“元/CNY”，不要把“亿千瓦时”换算为“千瓦时”。如果只是标准化单位建议，请在 reason 说明，但 corrected_value/corrected_unit 仍使用报告原文口径。
8. 输出必须是纯 JSON 数组，不要 Markdown。"""


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


def load_indicator_map() -> dict[str, dict[str, Any]]:
    data = json.loads(INDICATOR_JSON.read_text(encoding="utf-8"))
    return {str(item.get("field_id")): item for item in data.get("indicators", [])}


def build_user_prompt(batch: list[dict[str, str]], indicator_map: dict[str, dict[str, Any]]) -> str:
    items = []
    for idx, row in enumerate(batch, 1):
        ind = indicator_map.get(row.get("field_id", ""), {})
        items.append(
            {
                "review_id": f"R{idx}",
                "sample_id": row.get("sample_id", ""),
                "field_id": row.get("field_id", ""),
                "metric_name": row.get("metric_name_cn", ""),
                "definition": ind.get("definition", ""),
                "expected_unit": ind.get("unit_normalized", ""),
                "aliases": ind.get("aliases_cn", ""),
                "candidate_value": row.get("value_candidate", ""),
                "candidate_unit": row.get("unit_raw_candidate", ""),
                "source_page": row.get("source_page", ""),
                "method": row.get("value_extraction_method", ""),
                "evidence": row.get("source_text", "")[:1400],
            }
        )
    schema = [
        {
            "review_id": "R1",
            "decision": "accept|modify|reject|needs_review",
            "corrected_value": "",
            "corrected_unit": "",
            "confidence": 0.0,
            "reason": "一句话说明证据是否支持候选",
        }
    ]
    return (
        "请复核以下候选。输出 JSON 数组，长度必须与输入条数一致，review_id 对应输入。\n\n"
        f"输入候选：\n{json.dumps(items, ensure_ascii=False, indent=2)}\n\n"
        f"输出格式示例：\n{json.dumps(schema, ensure_ascii=False)}"
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
        data = data.get("reviews") or data.get("results") or [data]
    return data if isinstance(data, list) else []


def call_deepseek(batch: list[dict[str, str]], indicator_map: dict[str, dict[str, Any]], max_retries: int = 1) -> list[dict[str, Any]]:
    api_key, base_url, model = load_api_config()
    if not api_key:
        raise RuntimeError("DeepSeek API key not configured")
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
    prompt = SYSTEM_PROMPT + "\n\n" + build_user_prompt(batch, indicator_map)
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


def merge_reviews(batch: list[dict[str, str]], reviews: list[dict[str, Any]]) -> list[dict[str, str]]:
    by_id = {str(item.get("review_id", "")): item for item in reviews}
    out: list[dict[str, str]] = []
    for idx, row in enumerate(batch, 1):
        rid = f"R{idx}"
        review = by_id.get(rid, {})
        out.append(
            {
                "sample_id": row.get("sample_id", ""),
                "field_id": row.get("field_id", ""),
                "metric_name_cn": row.get("metric_name_cn", ""),
                "value_candidate": row.get("value_candidate", ""),
                "unit_raw_candidate": row.get("unit_raw_candidate", ""),
                "confidence_rule": row.get("confidence_rule", ""),
                "risk_score": row.get("risk_score", ""),
                "source_page": row.get("source_page", ""),
                "value_extraction_method": row.get("value_extraction_method", ""),
                "llm_decision": str(review.get("decision", "needs_review")),
                "llm_corrected_value": str(review.get("corrected_value", "")),
                "llm_corrected_unit": str(review.get("corrected_unit", "")),
                "llm_confidence": str(review.get("confidence", "")),
                "llm_reason": str(review.get("reason", "")),
            }
        )
    return out


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = [
        "sample_id",
        "field_id",
        "metric_name_cn",
        "value_candidate",
        "unit_raw_candidate",
        "confidence_rule",
        "risk_score",
        "source_page",
        "value_extraction_method",
        "llm_decision",
        "llm_corrected_value",
        "llm_corrected_unit",
        "llm_confidence",
        "llm_reason",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    queue = load_rows(args.queue)
    if args.limit > 0:
        queue = queue[: args.limit]
    indicator_map = load_indicator_map()

    existing: list[dict[str, str]] = []
    done_keys: set[tuple[str, str, str, str]] = set()
    if args.resume and args.output.exists():
        existing = load_rows(args.output)
        for row in existing:
            done_keys.add((row.get("sample_id", ""), row.get("field_id", ""), row.get("value_candidate", ""), row.get("source_page", "")))

    pending = [
        row
        for row in queue
        if (row.get("sample_id", ""), row.get("field_id", ""), row.get("value_candidate", ""), row.get("source_page", "")) not in done_keys
    ]

    results = list(existing)
    for start in range(0, len(pending), args.batch_size):
        batch = pending[start : start + args.batch_size]
        print(f"Review batch {start // args.batch_size + 1}: {len(batch)} items", flush=True)
        reviews = call_deepseek(batch, indicator_map)
        results.extend(merge_reviews(batch, reviews))
        write_csv(args.output, results)
        time.sleep(0.3)

    counts: dict[str, int] = {}
    for row in results:
        counts[row.get("llm_decision", "")] = counts.get(row.get("llm_decision", ""), 0) + 1
    print(
        json.dumps(
            {
                "queue": str(args.queue),
                "output": str(args.output),
                "reviewed": len(results),
                "decision_counts": counts,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
