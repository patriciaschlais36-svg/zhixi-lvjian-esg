# -*- coding: utf-8 -*-
"""Diagnose low-coverage ESG extraction samples and route next actions.

The script distinguishes image/scanned PDFs from text-rich rule gaps so large
batches can trigger OCR only when it is actually useful.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SAMPLE_JSON = BASE_DIR / "算法源码" / "示例清单" / "示例样本清单.json"
DEFAULT_OCR_JSON_DIR = BASE_DIR / "运行缓存" / "OCR" / "ocr_page_json"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sample_map(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    payload = load_json(path)
    samples = payload.get("samples", payload if isinstance(payload, list) else [])
    return {str(item.get("sample_id", "")): item for item in samples if item.get("sample_id")}


def page_text_file(page_text_dir: Path, sample_id: str) -> Path | None:
    matches = sorted(page_text_dir.glob(f"{sample_id}_*_page_text.json"))
    if matches:
        return matches[0]
    direct = page_text_dir / f"{sample_id}_page_text.json"
    return direct if direct.exists() else None


def page_text_stats(page_text_dir: Path, sample_id: str) -> dict[str, Any]:
    path = page_text_file(page_text_dir, sample_id)
    if not path:
        return {"page_text_file": "", "page_count_text": 0, "native_chars": 0, "avg_native_chars": 0}
    payload = load_json(path)
    pages = payload.get("pages", [])
    native_chars = 0
    cid_count = 0
    for page in pages:
        text = page.get("text", "")
        if isinstance(text, list):
            text = " ".join(str(item) for item in text)
        text = str(text or "")
        native_chars += len(text)
        cid_count += len(re.findall(r"\(cid:\d+\)", text))
    count = len(pages)
    cid_ratio = (cid_count * 8) / max(1, native_chars)
    return {
        "page_text_file": str(path),
        "page_count_text": count,
        "native_chars": native_chars,
        "avg_native_chars": round(native_chars / max(1, count), 2),
        "cid_count": cid_count,
        "cid_ratio": round(cid_ratio, 4),
    }


def ocr_page_count(ocr_json_dir: Path, sample_id: str) -> int:
    if not ocr_json_dir.exists():
        return 0
    return sum(1 for _ in ocr_json_dir.glob(f"{sample_id}_page_*_ocr.json"))


def classify(found: int, no_candidate: int, avg_chars: float, ocr_pages: int, cid_ratio: float, args: argparse.Namespace) -> tuple[str, str]:
    total_pairs = found + no_candidate
    coverage = found / total_pairs if total_pairs else 0.0
    if cid_ratio >= args.cid_ratio_threshold and found <= args.low_found_threshold:
        if ocr_pages > 0:
            return "encoded_text_layer_garbled_with_ocr_cache", "rerun_extraction_with_ocr_cache"
        return "encoded_text_layer_garbled", "force_ocr_then_regression"
    text_poor = avg_chars <= args.image_avg_chars
    text_rich = avg_chars >= args.text_rich_avg_chars
    very_low = found <= args.low_found_threshold or coverage <= args.low_coverage_rate

    if found == 0 and text_poor:
        return "image_pdf_zero_coverage", "run_ocr_then_regression"
    if very_low and text_poor:
        if ocr_pages > 0:
            return "image_pdf_low_coverage_with_ocr_cache", "rerun_extraction_with_ocr_cache"
        return "image_pdf_low_coverage", "run_ocr_then_regression"
    if found == 0 and text_rich:
        return "text_rich_zero_coverage", "alias_dictionary_and_deepseek_recall"
    if very_low and text_rich:
        return "text_rich_low_coverage", "alias_dictionary_and_deepseek_recall"
    if very_low:
        return "low_coverage_uncertain", "inspect_pdf_quality_then_route"
    return "normal_or_review_later", "none"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quality-json", type=Path, required=True)
    parser.add_argument("--page-text-dir", type=Path, required=True)
    parser.add_argument("--sample-json", type=Path, default=DEFAULT_SAMPLE_JSON)
    parser.add_argument("--ocr-json-dir", type=Path, default=DEFAULT_OCR_JSON_DIR)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--low-found-threshold", type=int, default=30)
    parser.add_argument("--low-coverage-rate", type=float, default=0.35)
    parser.add_argument("--image-avg-chars", type=float, default=20.0)
    parser.add_argument("--text-rich-avg-chars", type=float, default=300.0)
    parser.add_argument("--cid-ratio-threshold", type=float, default=0.15)
    args = parser.parse_args()

    quality = load_json(args.quality_json)
    by_sample = quality.get("by_sample", {})
    samples = sample_map(args.sample_json)

    rows: list[dict[str, Any]] = []
    for sid, stats in sorted(by_sample.items()):
        found = int(stats.get("candidate_found") or 0)
        no_candidate = int(stats.get("no_candidate") or 0)
        page_stats = page_text_stats(args.page_text_dir, sid)
        ocr_pages = ocr_page_count(args.ocr_json_dir, sid)
        category, action = classify(
            found,
            no_candidate,
            float(page_stats["avg_native_chars"]),
            ocr_pages,
            float(page_stats["cid_ratio"]),
            args,
        )
        meta = samples.get(sid, {})
        total_pairs = found + no_candidate
        rows.append(
            {
                "sample_id": sid,
                "stock_code": meta.get("stock_code", ""),
                "short_name": meta.get("short_name", ""),
                "candidate_found": found,
                "no_candidate": no_candidate,
                "coverage_rate": round(found / total_pairs, 4) if total_pairs else 0.0,
                "page_count_manifest": meta.get("page_count", ""),
                "page_count_text": page_stats["page_count_text"],
                "native_chars": page_stats["native_chars"],
                "avg_native_chars": page_stats["avg_native_chars"],
                "cid_count": page_stats["cid_count"],
                "cid_ratio": page_stats["cid_ratio"],
                "ocr_cache_pages": ocr_pages,
                "diagnosis": category,
                "recommended_action": action,
                "pdf_path": meta.get("pdf_path", ""),
                "page_text_file": page_stats["page_text_file"],
            }
        )

    rows.sort(key=lambda r: (str(r["recommended_action"]), float(r["coverage_rate"]), float(r["avg_native_chars"])))
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else []
    with args.output_csv.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    summary: dict[str, Any] = {
        "quality_json": str(args.quality_json),
        "page_text_dir": str(args.page_text_dir),
        "output_csv": str(args.output_csv),
        "sample_count": len(rows),
        "diagnosis_counts": {},
        "action_counts": {},
    }
    for row in rows:
        summary["diagnosis_counts"][row["diagnosis"]] = summary["diagnosis_counts"].get(row["diagnosis"], 0) + 1
        summary["action_counts"][row["recommended_action"]] = summary["action_counts"].get(row["recommended_action"], 0) + 1

    output_json = args.output_json or args.output_csv.with_suffix(".json")
    output_json.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
