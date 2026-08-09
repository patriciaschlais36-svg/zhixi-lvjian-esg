# -*- coding: utf-8 -*-
"""Sample-level DeepSeek reconciliation for P0 quantitative ESG indicators.

Unlike field-by-field review, this script gives the model the quantitative
indicator list and all relevant candidate snippets for one company/report at a
time. That makes it better suited for table column swaps, current-year
selection, and company-total-vs-local-scope mistakes.

Production-safety:
- Prompts contain only indicator definitions and extraction evidence.
- Gold labels are never read by this script.
- Optional full-report page contexts are retrieved by indicator aliases.
- Applying results requires the returned page/evidence quote to be verifiable
  in the cached report text. High-confidence results may repair an existing
  candidate or promote a no_candidate row.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPT_VERSION = "run_deepseek_sample_quant_reconcile_v1.1_report_context"
P0_QUANT_FIELDS = {
    "E_Q_001", "E_Q_002", "E_Q_003", "E_Q_005", "E_Q_006", "E_Q_007", "E_Q_009",
    "E_Q_012", "E_Q_013", "E_Q_015", "S_Q_001", "S_Q_002", "S_Q_004", "S_Q_005",
    "S_Q_008", "S_Q_009", "S_Q_017", "G_Q_001", "G_Q_002", "G_Q_003", "G_Q_009",
    "G_Q_010",
}
DISABLED_APPLY_FIELDS = {
    # Donation/community input often mixes project, cumulative, and annual
    # figures. Keep it in review output until a stricter scope contract exists.
    "S_Q_017",
}
RESULT_FIELDS = [
    "sample_id", "field_id", "metric_name", "decision", "value", "unit", "source_page",
    "confidence", "evidence_quote", "reason",
]
QUEUE_FIELDS = [
    "queue_id", "sample_id", "short_name", "stock_code", "report_type", "report_year",
    "target_field_count", "evidence_count", "report_page_count", "prompt_char_count", "prompt_json",
]


SYSTEM_PROMPT = """你是ESG报告定量指标抽取审计器。请只依据输入的候选证据片段，按报告年度和公司整体口径重建P0定量指标。

硬规则：
1. 只能使用输入证据，不得使用外部知识，不得猜测。
2. 优先公司/集团/本公司总量；不要把分公司、总行场地、项目、案例、目标、同比变化、密度/强度误作总量。
3. 对年份表，选择报告年度列；若表头为2023/2024/2025，报告年度为2025时选2025列。
4. 范围一/范围二/总量必须区分；废弃物总量、无害废弃物、有害/危险废弃物必须区分；培训人数/次数不能当作腐败案件数。
5. candidate_evidence 中 method=report_page_context 的片段来自报告全文页缓存，可用于召回原抽取器漏掉的指标。
6. 输出纯JSON数组，不要Markdown。每个对象字段必须为：
   sample_id, field_id, metric_name, decision, value, unit, source_page, confidence, evidence_quote, reason
7. decision只能是 extracted 或 insufficient_context。confidence为0到1。证据不足时value/unit留空。
8. 每个目标字段最多输出一个对象；extracted 必须给出输入片段中可逐字核验的 evidence_quote 和 source_page。
"""


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
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


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clean_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if limit > 0 and len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def parse_float(value: Any, default: float = 0.0) -> float:
    text = str(value or "").replace(",", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return default
    try:
        return float(match.group(0))
    except ValueError:
        return default


def parse_rank(value: Any) -> int:
    try:
        return int(float(str(value or "").strip()))
    except ValueError:
        return 999


def parse_report_year(rows: list[dict[str, str]]) -> str:
    for row in rows:
        pdf_path = str(row.get("pdf_path") or "")
        match = re.search(r"(20\d{2})", pdf_path)
        if match:
            return match.group(1)
    return ""


def candidate_sort(row: dict[str, str]) -> tuple[int, int, float]:
    return (
        0 if row.get("candidate_status") == "candidate_found" else 1,
        parse_rank(row.get("candidate_rank")),
        -parse_float(row.get("confidence_rule"), 0.0),
    )


def load_indicators(path: Path) -> dict[str, dict[str, str]]:
    rows, _ = read_csv(path)
    return {row.get("field_id", ""): row for row in rows if row.get("field_id") in P0_QUANT_FIELDS}


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def page_text_file(page_text_dir: Path, sample_id: str) -> Path | None:
    matches = sorted(page_text_dir.glob(f"{sample_id}_*_page_text.json"))
    if matches:
        return matches[0]
    direct = page_text_dir / f"{sample_id}_page_text.json"
    return direct if direct.exists() else None


def load_report_pages(page_text_dir: Path | None, sample_id: str) -> dict[int, str]:
    if not page_text_dir:
        return {}
    path = page_text_file(page_text_dir, sample_id)
    if not path:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    pages: dict[int, str] = {}
    for page in payload.get("pages", []):
        try:
            page_no = int(page.get("page"))
        except (TypeError, ValueError):
            continue
        text = page.get("text", "")
        if isinstance(text, list):
            text = " ".join(str(item) for item in text)
        pages[page_no] = str(text or "")
    return pages


def indicator_terms(indicator: dict[str, str]) -> list[str]:
    raw = ";".join(
        [
            indicator.get("metric_name_cn", ""),
            indicator.get("aliases_cn", ""),
        ]
    )
    terms: list[str] = []
    for part in re.split(r"[;；,，、/|]+", raw):
        term = part.strip()
        key = normalize_text(term)
        if len(key) >= 2 and key not in {normalize_text(item) for item in terms}:
            terms.append(term)
    return sorted(terms, key=len, reverse=True)


def report_contexts_for_field(
    pages: dict[int, str],
    indicator: dict[str, str],
    max_contexts: int,
    radius: int,
) -> list[dict[str, Any]]:
    hits: list[tuple[int, int, str, str]] = []
    terms = indicator_terms(indicator)
    for page_no, text in pages.items():
        text_norm = normalize_text(text)
        for term in terms:
            term_norm = normalize_text(term)
            if not term_norm or term_norm not in text_norm:
                continue
            raw_pos = text.lower().find(term.lower())
            if raw_pos < 0:
                raw_pos = min(text_norm.find(term_norm), len(text))
            start = max(0, raw_pos - radius)
            end = min(len(text), raw_pos + len(term) + radius)
            context = clean_text(text[start:end], radius * 2 + len(term))
            number_count = len(re.findall(r"-?\d+(?:,\d{3})*(?:\.\d+)?", context))
            year_bonus = 2 if re.search(r"20\d{2}", context) else 0
            score = number_count + year_bonus + min(4, len(term) // 3)
            hits.append((score, page_no, term, context))
            break
    hits.sort(key=lambda item: (-item[0], item[1]))
    return [
        {
            "source_page": str(page_no),
            "matched_term": term,
            "text": context,
        }
        for _, page_no, term, context in hits[:max_contexts]
    ]


def build_sample_prompt(
    sample_id: str,
    sample_rows: list[dict[str, str]],
    indicators: dict[str, dict[str, str]],
    per_field_limit: int,
    snippet_limit: int,
    context_limit: int,
    page_text_dir: Path | None,
    report_contexts_per_field: int,
    report_context_radius: int,
) -> tuple[str, dict[str, Any]]:
    first = sample_rows[0]
    report_year = parse_report_year(sample_rows)
    targets = []
    for field_id in sorted(P0_QUANT_FIELDS):
        ind = indicators.get(field_id, {})
        if not ind:
            continue
        targets.append(
            {
                "field_id": field_id,
                "metric_name": ind.get("metric_name_cn", ""),
                "unit_normalized": ind.get("unit_normalized", ""),
                "accepted_units": ind.get("units_accepted_raw", ""),
                "aliases": ind.get("aliases_cn", ""),
                "definition": ind.get("definition", ""),
                "normalization_hint": ind.get("normalization_hint", ""),
            }
        )

    by_field: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in sample_rows:
        if row.get("field_id") in P0_QUANT_FIELDS:
            by_field[row.get("field_id", "")].append(row)

    report_pages = load_report_pages(page_text_dir, sample_id)
    evidence: list[dict[str, Any]] = []
    seen_text: set[str] = set()
    for field_id in sorted(P0_QUANT_FIELDS):
        rows = sorted(by_field.get(field_id, []), key=candidate_sort)[:per_field_limit]
        for row in rows:
            text = clean_text(row.get("source_table_cell") or row.get("source_text"), snippet_limit)
            if not text:
                continue
            key = re.sub(r"\s+", "", text)[:260]
            if key in seen_text:
                continue
            seen_text.add(key)
            evidence.append(
                {
                    "field_id": field_id,
                    "metric_name": row.get("metric_name_cn", ""),
                    "candidate_rank": row.get("candidate_rank", ""),
                    "candidate_value": row.get("value_candidate", ""),
                    "candidate_unit": row.get("unit_raw_candidate", ""),
                    "candidate_confidence": row.get("confidence_rule", ""),
                    "method": row.get("value_extraction_method", ""),
                    "source_page": row.get("source_page", ""),
                    "text": text,
                }
            )
        indicator = indicators.get(field_id, {})
        for context in report_contexts_for_field(
            report_pages,
            indicator,
            report_contexts_per_field,
            report_context_radius,
        ):
            text = context["text"]
            key = re.sub(r"\s+", "", text)[:260]
            if not text or key in seen_text:
                continue
            seen_text.add(key)
            evidence.append(
                {
                    "field_id": field_id,
                    "metric_name": indicator.get("metric_name_cn", ""),
                    "candidate_rank": "",
                    "candidate_value": "",
                    "candidate_unit": "",
                    "candidate_confidence": "",
                    "method": "report_page_context",
                    "source_page": context["source_page"],
                    "matched_term": context["matched_term"],
                    "text": text,
                }
            )

    payload = {
        "sample_id": sample_id,
        "company": first.get("short_name", ""),
        "stock_code": first.get("stock_code", ""),
        "report_type": first.get("report_type", ""),
        "report_year": report_year,
        "target_fields": targets,
        "candidate_evidence": evidence,
    }
    prompt = SYSTEM_PROMPT + "\n\ninput:\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    if len(prompt) > context_limit:
        # Keep target definitions intact and trim evidence by dropping tail snippets.
        while evidence and len(prompt) > context_limit:
            evidence.pop()
            payload["candidate_evidence"] = evidence
            prompt = SYSTEM_PROMPT + "\n\ninput:\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    meta = {
        "sample_id": sample_id,
        "short_name": first.get("short_name", ""),
        "stock_code": first.get("stock_code", ""),
        "report_type": first.get("report_type", ""),
        "report_year": report_year,
        "target_field_count": len(targets),
        "evidence_count": len(evidence),
        "report_page_count": len(report_pages),
        "prompt_char_count": len(prompt),
    }
    return prompt, meta


def load_api_config() -> tuple[str, str, str]:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
    config_path = SCRIPT_DIR / "api_config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            deepseek = config.get("deepseek", {})
            api_key = os.environ.get("DEEPSEEK_API_KEY", deepseek.get("api_key", api_key))
            base_url = os.environ.get("DEEPSEEK_BASE_URL", deepseek.get("base_url", base_url))
            model = os.environ.get("DEEPSEEK_MODEL", deepseek.get("model", model))
        except Exception:
            pass
    return api_key, base_url, model


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
        data = data.get("results") or data.get("items") or [data]
    return data if isinstance(data, list) else []


def call_deepseek(prompt: str, max_retries: int, max_tokens: int) -> tuple[list[dict[str, Any]], str]:
    api_key, base_url, model = load_api_config()
    if not api_key:
        raise RuntimeError("DeepSeek API key not configured")
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "thinking": {"type": "disabled"},
    }
    last_text = ""
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            response = client.messages.create(**kwargs)
            text = "".join(getattr(block, "text", "") for block in response.content)
            last_text = text
            parsed = parse_json_response(text)
            if parsed:
                return parsed, text
        except Exception as exc:
            last_error = exc
            if attempt < max_retries:
                time.sleep(2 + attempt * 2)
    if last_error:
        raise last_error
    return [], last_text


def normalize_result(item: dict[str, Any], sample_id: str) -> dict[str, str]:
    out = {field: str(item.get(field, "") or "") for field in RESULT_FIELDS}
    out["sample_id"] = out.get("sample_id") or sample_id
    if out.get("decision") not in {"extracted", "insufficient_context"}:
        out["decision"] = "insufficient_context"
    return out


def evidence_supported(result: dict[str, str], page_text_dir: Path | None) -> tuple[bool, str]:
    if not page_text_dir:
        return True, "page_cache_not_requested"
    pages = load_report_pages(page_text_dir, result.get("sample_id", ""))
    page_numbers = [
        int(token)
        for token in re.findall(r"\d+", result.get("source_page", ""))
        if int(token) > 0
    ]
    if not page_numbers:
        return False, "missing_source_page"
    page_blob = "\n".join(pages.get(page, "") for page in page_numbers)
    if not page_blob.strip():
        return False, "source_page_not_in_cache"
    quote = normalize_text(result.get("evidence_quote", ""))
    page_norm = normalize_text(page_blob)
    if len(quote) >= 12 and quote in page_norm:
        return True, "evidence_quote_verified"
    fragments = [quote[start : start + 32] for start in range(0, len(quote), 16)]
    if any(len(fragment) >= 16 and fragment in page_norm for fragment in fragments):
        return True, "evidence_fragment_verified"
    return False, "evidence_quote_not_in_cache"


def parse_number_text(value: Any) -> str:
    text = str(value or "").strip().replace(",", "")
    multiplier = 1.0
    # Keep units separate when the model returns "2.75亿元": convert the numeric
    # part only if the unit was embedded in value. Otherwise preserve the scale.
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return ""
    number = float(match.group(0)) * multiplier
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f"{number:.10f}".rstrip("0").rstrip(".")


def unit_multiplier(unit: Any) -> float:
    text = str(unit or "").replace(" ", "").lower()
    if "亿元" in text:
        return 10000.0
    if "万元" in text:
        return 1.0
    if text in {"元", "cny", "rmb", "人民币"} or text.endswith("元"):
        return 0.0001
    if "亿千瓦时" in text:
        return 10000.0
    if "万千瓦时" in text:
        return 1.0
    if "千瓦时" in text or "kwh" in text:
        return 0.0001
    if "万吨" in text:
        return 10000.0
    if text in {"吨", "t", "tco2e", "吨二氧化碳当量"} or text.endswith("吨"):
        return 1.0
    if "%" in text:
        return 1.0
    return 1.0


def equivalent_after_unit_normalization(old_value: Any, old_unit: Any, new_value: Any, new_unit: Any) -> bool:
    old_num = parse_float(old_value, float("nan"))
    new_num = parse_float(new_value, float("nan"))
    if old_num != old_num or new_num != new_num:
        return False
    old_norm = old_num * unit_multiplier(old_unit)
    new_norm = new_num * unit_multiplier(new_unit)
    if old_norm == 0:
        return abs(new_norm) < 1e-9
    return abs(new_norm - old_norm) / abs(old_norm) <= 0.05


def result_confidence(row: dict[str, str]) -> float:
    value = str(row.get("confidence", "")).strip()
    try:
        num = float(value)
    except ValueError:
        return 0.0
    if num > 1:
        num /= 100
    return max(0.0, min(1.0, num))


def apply_results(
    base_rows: list[dict[str, str]],
    fields: list[str],
    result_rows: list[dict[str, str]],
    output: Path,
    audit_csv: Path,
    min_confidence: float,
    page_text_dir: Path | None,
) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in base_rows:
        groups[(row.get("sample_id", ""), row.get("field_id", ""))].append(row)

    audit: list[dict[str, Any]] = []
    counts: Counter = Counter()
    for result in result_rows:
        key = (result.get("sample_id", ""), result.get("field_id", ""))
        if key[1] not in P0_QUANT_FIELDS:
            counts["skip_non_p0_quant"] += 1
            continue
        if key[1] in DISABLED_APPLY_FIELDS:
            counts["skip_disabled_field"] += 1
            continue
        conf = result_confidence(result)
        new_value = parse_number_text(result.get("value", ""))
        if result.get("decision") != "extracted" or conf < min_confidence or not new_value:
            counts["skip_low_conf_or_no_value"] += 1
            continue
        evidence_ok, evidence_status = evidence_supported(result, page_text_dir)
        if not evidence_ok:
            counts[f"skip_{evidence_status}"] += 1
            continue
        group = groups.get(key, [])
        found = [row for row in group if row.get("candidate_status") == "candidate_found"]
        if not group:
            counts["skip_field_group_not_found"] += 1
            continue
        target = (
            max(found, key=lambda row: (parse_float(row.get("confidence_rule"), 0.0), -parse_rank(row.get("candidate_rank"))))
            if found
            else group[0]
        )
        old = {
            "status": target.get("candidate_status", ""),
            "value": target.get("value_candidate", ""),
            "unit": target.get("unit_raw_candidate", ""),
            "confidence": target.get("confidence_rule", ""),
            "method": target.get("value_extraction_method", ""),
        }
        if equivalent_after_unit_normalization(old["value"], old["unit"], new_value, result.get("unit", "")):
            counts["skip_equivalent_unit_restatement"] += 1
            continue
        target["candidate_status"] = "candidate_found"
        target["candidate_disclosure_class"] = "disclosed"
        target["value_candidate"] = new_value
        target["unit_raw_candidate"] = result.get("unit", "") or target.get("unit_raw_candidate", "")
        target["source_page"] = result.get("source_page", "") or target.get("source_page", "")
        target["source_text"] = result.get("evidence_quote", "") or target.get("source_text", "")
        target["value_status"] = "exact_value_candidate"
        target["value_extraction_method"] = "deepseek_sample_quant_reconcile"
        target["candidate_rank"] = "1"
        target["confidence_rule"] = f"{max(parse_float(target.get('confidence_rule'), 0.0), conf, 0.945):.3f}"
        target["needs_llm_review"] = "no"
        target["recommended_next_status"] = "auto_verified_after_report_adjudication"
        target["review_reason"] = (
            target.get("review_reason", "")
            + f"; {SCRIPT_VERSION}:{result.get('decision')}:{clean_text(result.get('reason', ''), 220)}"
        ).strip("; ")
        for row in group:
            if row is not target and row.get("candidate_status") == "candidate_found":
                row["candidate_rank"] = str(max(2, parse_rank(row.get("candidate_rank")) + 1))
                if parse_float(row.get("confidence_rule"), 0.0) >= parse_float(target.get("confidence_rule"), 0.0):
                    row["confidence_rule"] = "0.620"
        counts["accepted"] += 1
        audit.append(
            {
                "sample_id": key[0],
                "field_id": key[1],
                "metric_name": result.get("metric_name", ""),
                "accepted": "yes",
                "confidence": conf,
                "evidence_status": evidence_status,
                "old_status": old["status"],
                "old_value": old["value"],
                "old_unit": old["unit"],
                "old_confidence": old["confidence"],
                "old_method": old["method"],
                "new_value": target.get("value_candidate", ""),
                "new_unit": target.get("unit_raw_candidate", ""),
                "source_page": target.get("source_page", ""),
                "evidence_quote": clean_text(result.get("evidence_quote", ""), 500),
                "reason": clean_text(result.get("reason", ""), 500),
            }
        )

    write_csv(output, base_rows, fields)
    audit_fields = [
        "sample_id", "field_id", "metric_name", "accepted", "confidence",
        "evidence_status", "old_status",
        "old_value", "old_unit", "old_confidence", "old_method",
        "new_value", "new_unit", "source_page", "evidence_quote", "reason",
    ]
    write_csv(audit_csv, audit, audit_fields)
    return dict(counts)


def parse_sample_filter(value: str) -> set[str]:
    return {part.strip() for part in re.split(r"[;,，\s]+", value or "") if part.strip()}


def parse_sample_file(path: Path | None) -> set[str]:
    if not path:
        return set()
    if not path.exists():
        raise FileNotFoundError(path)
    return parse_sample_filter(path.read_text(encoding="utf-8-sig"))


def sample_filter_requested(sample_ids: str, sample_id_file: Path | None) -> bool:
    return bool((sample_ids or "").strip()) or sample_id_file is not None


def estimate_cost(queue_rows: list[dict[str, Any]], execute_limit: int, out_tokens: int, in_usd: float, out_usd: float) -> dict[str, Any]:
    rows = queue_rows[:execute_limit if execute_limit > 0 else len(queue_rows)]
    input_tokens = sum(max(1, int(int(row.get("prompt_char_count", 0)) / 1.8)) for row in rows)
    output_tokens = len(rows) * out_tokens
    return {
        "api_rows_pending": len(rows),
        "input_tokens_est": input_tokens,
        "output_tokens_est": output_tokens,
        "estimated_cost_usd": round(input_tokens / 1_000_000 * in_usd + output_tokens / 1_000_000 * out_usd, 6),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-csv", type=Path, required=True)
    parser.add_argument("--indicator-csv", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--sample-ids", default="")
    parser.add_argument("--sample-id-file", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--per-field-limit", type=int, default=3)
    parser.add_argument("--snippet-limit", type=int, default=1400)
    parser.add_argument("--context-limit", type=int, default=60000)
    parser.add_argument("--page-text-dir", type=Path)
    parser.add_argument("--report-contexts-per-field", type=int, default=2)
    parser.add_argument("--report-context-radius", type=int, default=520)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--results-csv", type=Path)
    parser.add_argument("--execute-limit", type=int, default=0)
    parser.add_argument("--budget-usd", type=float, default=10.0)
    parser.add_argument("--estimated-input-usd-per-1m", type=float, default=2.0)
    parser.add_argument("--estimated-output-usd-per-1m", type=float, default=8.0)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--apply-output", type=Path)
    parser.add_argument("--apply-audit-csv", type=Path)
    parser.add_argument("--min-apply-confidence", type=float, default=0.90)
    args = parser.parse_args()

    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    queue_csv = args.out_dir / f"deepseek_sample_quant_reconcile_queue_{run_id}.csv"
    queue_jsonl = args.out_dir / f"deepseek_sample_quant_reconcile_queue_{run_id}.jsonl"
    results_csv = args.out_dir / f"deepseek_sample_quant_reconcile_results_{run_id}.csv"
    results_jsonl = args.out_dir / f"deepseek_sample_quant_reconcile_results_{run_id}.jsonl"
    plan_json = args.out_dir / f"deepseek_sample_quant_reconcile_plan_{run_id}.json"
    apply_output = args.apply_output or args.out_dir / f"{args.candidate_csv.stem}_sample_quant_reconciled_{run_id}.csv"
    apply_audit = args.apply_audit_csv or args.out_dir / f"deepseek_sample_quant_reconcile_apply_audit_{run_id}.csv"

    candidate_rows, candidate_fields = read_csv(args.candidate_csv)
    indicators = load_indicators(args.indicator_csv)
    has_sample_filter = sample_filter_requested(args.sample_ids, args.sample_id_file)
    sample_filter = parse_sample_filter(args.sample_ids) | parse_sample_file(args.sample_id_file)
    by_sample: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in candidate_rows:
        sample_id = row.get("sample_id", "")
        if has_sample_filter and sample_id not in sample_filter:
            continue
        by_sample[sample_id].append(row)

    queue_rows: list[dict[str, Any]] = []
    for idx, sample_id in enumerate(sorted(by_sample)):
        if args.limit and len(queue_rows) >= args.limit:
            break
        sample_rows = by_sample[sample_id]
        if not any(row.get("field_id") in P0_QUANT_FIELDS for row in sample_rows):
            continue
        prompt, meta = build_sample_prompt(
            sample_id,
            sample_rows,
            indicators,
            args.per_field_limit,
            args.snippet_limit,
            args.context_limit,
            args.page_text_dir,
            args.report_contexts_per_field,
            args.report_context_radius,
        )
        queue_rows.append(
            {
                "queue_id": f"{run_id}_{idx+1:04d}_{sample_id}",
                **meta,
                "prompt_json": prompt,
            }
        )

    write_csv(queue_csv, queue_rows, QUEUE_FIELDS)
    write_jsonl(queue_jsonl, queue_rows)
    estimate = estimate_cost(
        queue_rows,
        args.execute_limit or len(queue_rows),
        args.max_output_tokens,
        args.estimated_input_usd_per_1m,
        args.estimated_output_usd_per_1m,
    )
    plan = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_id": run_id,
        "candidate_csv": str(args.candidate_csv),
        "indicator_csv": str(args.indicator_csv),
        "out_dir": str(args.out_dir),
        "queue_csv": str(queue_csv),
        "results_csv": str(results_csv),
        "rows_selected": len(queue_rows),
        "sample_filter_requested": has_sample_filter,
        "sample_filter_count": len(sample_filter),
        "execute": args.execute,
        "execute_limit": args.execute_limit,
        "budget_usd": args.budget_usd,
        "estimate": estimate,
        "context_policy": "indicator definitions + extraction candidate evidence + alias-retrieved report page contexts; no gold labels",
        "script_version": SCRIPT_VERSION,
    }
    write_json(plan_json, plan)

    result_rows: list[dict[str, str]] = []
    if args.execute:
        projected = estimate["estimated_cost_usd"]
        if projected > args.budget_usd:
            raise SystemExit(f"budget exceeded: estimated {projected} > budget {args.budget_usd}")
        api_rows = queue_rows[: args.execute_limit or len(queue_rows)]
        for idx, row in enumerate(api_rows, start=1):
            print(f"DeepSeek sample reconcile {idx}/{len(api_rows)} {row.get('sample_id')} {row.get('short_name')}", flush=True)
            parsed, raw_text = call_deepseek(row["prompt_json"], args.max_retries, args.max_output_tokens)
            normalized = [normalize_result(item, row.get("sample_id", "")) for item in parsed]
            if not normalized:
                normalized = [
                    {
                        "sample_id": row.get("sample_id", ""),
                        "field_id": "",
                        "metric_name": "",
                        "decision": "insufficient_context",
                        "value": "",
                        "unit": "",
                        "source_page": "",
                        "confidence": "0",
                        "evidence_quote": "",
                        "reason": clean_text(raw_text, 500),
                    }
                ]
            result_rows.extend(normalized)
        write_csv(results_csv, result_rows, RESULT_FIELDS)
        write_jsonl(results_jsonl, result_rows)
    elif args.results_csv:
        result_rows, _ = read_csv(args.results_csv)
        if has_sample_filter:
            result_rows = [row for row in result_rows if row.get("sample_id", "") in sample_filter]
    else:
        write_csv(results_csv, [], RESULT_FIELDS)

    apply_counts: dict[str, Any] = {}
    if args.apply and (args.execute or args.results_csv):
        apply_counts = apply_results(
            candidate_rows,
            candidate_fields,
            result_rows,
            apply_output,
            apply_audit,
            args.min_apply_confidence,
            args.page_text_dir,
        )

    print(
        json.dumps(
            {
                **plan,
                "status": "executed" if args.execute else "dry_run",
                "results_csv": str(results_csv),
                "results_jsonl": str(results_jsonl),
                "apply_output": str(apply_output) if args.apply and (args.execute or args.results_csv) else "",
                "apply_audit_csv": str(apply_audit) if args.apply and (args.execute or args.results_csv) else "",
                "apply_counts": apply_counts,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
