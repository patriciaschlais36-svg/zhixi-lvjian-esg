# -*- coding: utf-8 -*-
"""Claude Vision PDF 提取模块 v2.0

逐页渲染关键PDF页面为图片，分批传给Claude Vision提取指标。
"""

import json, os, sys, csv, base64, io, time, re
from pathlib import Path
from typing import Any

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
CLAUDE_BASE_URL = os.environ.get("CLAUDE_BASE_URL", "https://api.anthropic.com")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")

import pypdfium2 as pdfium

# ── 重用现有逻辑 ──
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["_V08C_IMPORT_MODE"] = "1"
import importlib.util as iu
_spec = iu.spec_from_file_location("v24base", str(Path(__file__).resolve().parent / "run_p0_pilot_extraction.py"))
_mod = iu.module_from_spec(_spec)
_src = open(Path(__file__).resolve().parent / "run_p0_pilot_extraction.py", encoding="utf-8").read()
_src = re.sub(r'if\s+__name__\s*==\s*["\']__main__["\']\s*:', 'if False and __name__ == "__main__":', _src)
exec(_src, _mod.__dict__)

from few_shot import get_few_shot_text

BASE_DIR = Path(__file__).resolve().parents[2]
INDICATOR_JSON = Path(os.environ.get(
    "ESG_INDICATOR_JSON",
    str(BASE_DIR / "算法源码" / "配置" / "ESG指标体系.json"),
))
DEFAULT_SAMPLE_JSON = BASE_DIR / "算法源码" / "示例清单" / "示例样本清单.json"
LEGACY_GOLD_SAMPLE_JSON = DEFAULT_SAMPLE_JSON
OUT_DIR = BASE_DIR / "运行产物" / "大模型视觉抽取"
VISION_DEBUG_DIR = OUT_DIR / "claude_vision_debug"


def extract_sample_claude(
    sample: dict,
    indicators: list[dict],
    quantitative_only: bool = True,
    dry_run: bool = False,
    max_pages: int = 12,
    batch_size: int = 5,
    target_fields: set[str] | None = None,
) -> list[dict]:
    """Render top pages as images, send to Claude Vision in batches of 5."""
    if quantitative_only:
        indicators = [i for i in indicators if i.get("metric_type") == "quantitative"]
    if target_fields:
        indicators = [i for i in indicators if i["field_id"] in target_fields]

    sid = sample["sample_id"]
    pdf_path = Path(sample["pdf_path"])
    if not pdf_path.exists():
        return _empty(sample, indicators)

    print(f"\nClaude Vision: {sid} {sample['short_name']} ({len(indicators)} indicators)")
    print(f"  Model: {CLAUDE_MODEL}")

    # ── RAG: score all pages ──
    page_score_fn = _mod.__dict__["page_score"]
    terms_for_indicator = _mod.__dict__["terms_for_indicator"]

    all_terms = sorted(set(t for ind in indicators for t in terms_for_indicator(ind)), key=len, reverse=True)

    # ── RAG: 如果有原生文本用 text，否则用 OCR 缓存 ──
    load_ocr = _mod.__dict__.get("load_ocr_payload_for_page")

    pdf = pdfium.PdfDocument(str(pdf_path))
    page_scores = []

    import re as _re
    for i in range(len(pdf)):
        text = ""
        # 1. Try native text
        try:
            tp = pdf[i].get_textpage()
            text = _mod.__dict__.get("normalize_text", lambda x: x)(tp.get_text_range() or "")
        except:
            pass

        # 2. Fallback to OCR cache
        if not text or len(text) < 30:
            if load_ocr:
                ocr = load_ocr(sid, i + 1)
                if ocr:
                    text = ocr.get("text", "")

        score, hits = page_score_fn(text, all_terms)
        if score > 0:
            page_scores.append((score, i, hits))
    page_scores.sort(key=lambda x: x[0], reverse=True)
    top_pages = page_scores[:max_pages]
    print(f"  Top pages: {[(p+1, round(s,0)) for s,p,_ in top_pages[:6]]}")

    if not top_pages:
        return _empty(sample, indicators)

    # ── Render pages to images ──
    page_imgs = []
    if dry_run:
        VISION_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    for score, idx, hits in top_pages:
        bitmap = pdf[idx].render(scale=2.0)
        buf = io.BytesIO()
        bitmap.to_pil().save(buf, format="PNG", optimize=True)
        png_bytes = buf.getvalue()
        if dry_run:
            debug_path = VISION_DEBUG_DIR / f"{sid}_page_{idx+1:03d}_score_{round(score)}.png"
            debug_path.write_bytes(png_bytes)
        page_imgs.append({
            "page": idx + 1,
            "b64": base64.b64encode(png_bytes).decode(),
            "bytes": len(png_bytes),
            "score": score,
            "terms": [h.split("~")[0] for h in hits[:5]],
        })

    if dry_run:
        plan = {
            "sample_id": sid,
            "short_name": sample["short_name"],
            "model": CLAUDE_MODEL,
            "indicator_count": len(indicators),
            "max_pages": max_pages,
            "batch_size": batch_size,
            "top_pages": [
                {
                    "page": p["page"],
                    "score": round(p["score"], 2),
                    "bytes": p["bytes"],
                    "terms": p["terms"],
                    "debug_image": str(VISION_DEBUG_DIR / f"{sid}_page_{p['page']:03d}_score_{round(p['score'])}.png"),
                }
                for p in page_imgs
            ],
            "batches": [
                [p["page"] for p in page_imgs[i:i + batch_size]]
                for i in range(0, len(page_imgs), batch_size)
            ],
        }
        plan_path = VISION_DEBUG_DIR / f"{sid}_dry_run_plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  DRY RUN: rendered {len(page_imgs)} pages, batches={plan['batches']}")
        print(f"  DRY RUN plan: {plan_path}")
        return _empty(sample, indicators)

    # ── Claude Vision extraction ──
    fs_text = get_few_shot_text([i["field_id"] for i in indicators])

    indicator_list = "\n".join(f"{i['field_id']}: {i['metric_name_cn']}（{i.get('unit_normalized','')}）" for i in indicators)

    import anthropic
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY, base_url=CLAUDE_BASE_URL, timeout=120)

    all_extractions = []
    extracted_fids = set()

    for batch_start in range(0, len(page_imgs), batch_size):
        batch = page_imgs[batch_start:batch_start + batch_size]
        batch_pages = [p["page"] for p in batch]

        remaining = [i for i in indicators if i["field_id"] not in extracted_fids]
        if not remaining:
            break

        remaining_text = "\n".join(f"{i['field_id']}: {i['metric_name_cn']}" for i in remaining)

        content = []
        for p in batch:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": p["b64"]}})
        content.append({"type": "text", "text": f"页面{batch_pages}。提取以下指标:\n{remaining_text}\n\n{fs_text}\n\n输出纯JSON数组，找不到的不要编造。"})

        try:
            r = client.messages.create(
                model=CLAUDE_MODEL, max_tokens=4096,
                system="ESG报告指标提取专家。只输出纯JSON数组，不输出markdown或解释文字。",
                messages=[{"role": "user", "content": content}],
            )
            raw = "".join(b.text for b in r.content if hasattr(b, "text"))
            clean = re.sub(r'```(?:json)?\s*|\s*```', '', raw).strip()
            try:
                data = json.loads(clean)
            except:
                m = re.search(r'\[[\s\S]*\]', clean)
                data = json.loads(m.group(0)) if m else None

            if isinstance(data, list):
                for ext in data:
                    fid = ext.get("field_id", "")
                    if fid and str(ext.get("value", "")).strip():
                        extracted_fids.add(fid)
                        all_extractions.append(ext)
                print(f"  Pages {batch_pages}: +{len(data)} extracted")
        except Exception as e:
            print(f"  Pages {batch_pages}: {type(e).__name__}: {str(e)[:100]}")

    print(f"  Total: {len(all_extractions)}")

    # ── Build records ──
    records = []
    for ext in all_extractions:
        fid = ext.get("field_id", "")
        ind = next((i for i in indicators if i["field_id"] == fid), None)
        if not ind:
            continue
        v = str(ext.get("value", "")) if ext.get("value") else ""
        records.append(_record(sample, ind, v, str(ext.get("unit_raw", "")), ext.get("confidence", 0.5), ext.get("reasoning", "")))
    for ind in indicators:
        if ind["field_id"] not in extracted_fids:
            records.append(_record(sample, ind, "", "", 0.1, "not found"))
    return records


def _record(sample, indicator, value, unit, confidence, reasoning):
    fid = indicator["field_id"]
    status = "candidate_found" if (value and value.strip()) else "no_candidate"
    return {
        "sample_id": sample["sample_id"], "stock_code": sample["stock_code"],
        "short_name": sample["short_name"], "report_type": sample["report_type"],
        "field_id": fid, "dimension": indicator["dimension"],
        "metric_name_cn": indicator["metric_name_cn"], "metric_type": indicator["metric_type"],
        "value_type": indicator.get("value_type", ""),
        "indicator_layer": indicator.get("indicator_layer", "core"),
        "candidate_status": status, "candidate_disclosure_class": "primary_disclosed" if status == "candidate_found" else "no_candidate",
        "candidate_rank": "1", "evidence_type_candidate": "claude_vision",
        "value_candidate": value, "unit_raw_candidate": unit,
        "value_standardized_candidate": "", "unit_standardized_candidate": "",
        "value_status": "exact_value_candidate" if value else "needs_value_review",
        "value_extraction_method": f"claude_vision_{CLAUDE_MODEL}",
        "source_page": "", "source_physical_page": "",
        "source_report_page_candidates": "", "source_text": "", "source_table_cell": "",
        "match_terms": "", "rule_score": round(confidence * 100, 2), "confidence_rule": confidence,
        "needs_llm_review": "no" if confidence >= 0.8 else "yes", "review_reason": reasoning,
        "recommended_next_status": "candidate_disclosed_review" if status == "candidate_found" else "not_found_review",
        "extractor_version": "claude_vision_v2.0", "pdf_path": sample["pdf_path"],
        "index_target_pages": "", "extraction_priority": indicator.get("extraction_priority", "P0"),
        "primary_indicator_id": indicator.get("primary_indicator_id", ""),
        "rating_role": indicator.get("rating_role", ""),
        "alternative_status_policy": indicator.get("alternative_status_policy", ""),
        "scoring_denominator_policy": indicator.get("scoring_denominator_policy", ""),
    }


def _empty(sample, indicators):
    return [_record(sample, i, "", "", 0.1, "Claude unavailable") for i in indicators]


def write_csv(records, path):
    if not records: return
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)
    found = sum(1 for r in records if r["candidate_status"] == "candidate_found")
    print(f"  Saved: {path} ({len(records)} rows, {found} found)")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", required=True)
    ap.add_argument(
        "--sample-json",
        type=Path,
        default=DEFAULT_SAMPLE_JSON,
        help="样本清单JSON；默认使用首批200样本RID清单，可传入首轮金标准样本清单做回归测试",
    )
    ap.add_argument("--dry-run", action="store_true", help="只渲染和生成批次计划，不调用 Claude API")
    ap.add_argument("--max-pages", type=int, default=12, help="最多传入/渲染的候选页数量")
    ap.add_argument("--batch-size", type=int, default=5, help="每批图片数量")
    ap.add_argument("--fields", type=str, default="", help="只提取指定指标，逗号分隔，如 G_Q_001,G_Q_002")
    ap.add_argument("--output", type=Path, help="非dry-run时的输出CSV路径；默认写入llm_extraction_v1.0目录")
    args = ap.parse_args()

    sample_json = args.sample_json
    if not sample_json.exists() and LEGACY_GOLD_SAMPLE_JSON.exists():
        sample_json = LEGACY_GOLD_SAMPLE_JSON
    samples = json.loads(sample_json.read_text(encoding="utf-8"))["samples"]
    sample = next((s for s in samples if s["sample_id"] == args.sample), None)
    if not sample:
        print(f"Sample {args.sample} not found"); sys.exit(1)

    indicators = json.loads(INDICATOR_JSON.read_text(encoding="utf-8"))["indicators"]
    target_fields = {x.strip() for x in args.fields.split(",") if x.strip()} or None
    records = extract_sample_claude(
        sample,
        indicators,
        dry_run=args.dry_run,
        max_pages=args.max_pages,
        batch_size=args.batch_size,
        target_fields=target_fields,
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if not args.dry_run:
        write_csv(records, args.output or (OUT_DIR / f"ClaudeVision_{args.sample}.csv"))
    else:
        print("  DRY RUN: skip writing extraction CSV")


if __name__ == "__main__":
    main()
