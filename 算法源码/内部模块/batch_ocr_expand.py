# -*- coding: utf-8 -*-
"""批量 OCR 扩展：对图片型 PDF 全页运行 RapidOCR，补全缺失页面。

用法：
  python batch_ocr_expand.py --sample GL010    # 单个样本
  python batch_ocr_expand.py --all-image       # 所有图片型PDF
  python batch_ocr_expand.py --sample R056 --force  # 原生文本为CID乱码时强制OCR
"""

import json, os, sys, time
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[2]
SAMPLE_JSON = Path(os.environ.get(
    "SAMPLE_JSON_PATH",
    str(BASE_DIR / "算法源码" / "示例清单" / "示例样本清单.json"),
))
OCR_DIR = Path(os.environ.get("OCR_CACHE_DIR", str(BASE_DIR / "运行缓存" / "OCR")))
OCR_JSON_DIR = OCR_DIR / "ocr_page_json"
OCR_TEXT_DIR = OCR_DIR / "ocr_pages"

# 已知图片型 PDF（原生文本严重不足）
IMAGE_PDF_SAMPLES = {
    "GL010", "GL014", "GL020", "GL007", "GL011", "GL024", "GL025",
    "GL026", "GL027", "GL028", "GL029",
}

NATIVE_TEXT_MIN_CHARS = 80  # 低于此阈值的页面走OCR


def needs_ocr(page_text: str) -> bool:
    """判断页面是否需要OCR。"""
    return len(page_text or "") < NATIVE_TEXT_MIN_CHARS


def get_existing_ocr_pages(sample_id: str) -> set[int]:
    """获取已有的OCR缓存页码。"""
    pages = set()
    for f in OCR_JSON_DIR.iterdir():
        if f.stem.startswith(f"{sample_id}_page_"):
            try:
                page_num = int(f.stem.split("_page_")[1].split("_")[0])
                pages.add(page_num)
            except ValueError:
                pass
    return pages


def ocr_page(pdf_path: str, page_index: int, engine) -> dict[str, Any] | None:
    """对单页PDF进行OCR。"""
    import pypdfium2 as pdfium
    try:
        pdf = pdfium.PdfDocument(pdf_path)
        page = pdf[page_index]
        bitmap = page.render(scale=2.0)
        img = bitmap.to_pil()
        output = engine(img)
        texts = output.txts if hasattr(output, 'txts') else []

        if not texts:
            return None

        scores = output.scores if hasattr(output, 'scores') else [0.0] * len(texts)
        ocr_lines = []
        for i, t in enumerate(texts):
            line_text = str(t).strip()
            if line_text:
                score = float(scores[i]) if i < len(scores) else 0.0
                ocr_lines.append({"text": line_text, "confidence": score})

        if not ocr_lines:
            return None

        full_text = " ".join(str(l["text"]) for l in ocr_lines)
        avg_score = sum(l["confidence"] for l in ocr_lines) / len(ocr_lines)
        elapse = output.elapse if hasattr(output, 'elapse') else 0.0

        return {
            "page": page_index + 1,
            "ocr_text": full_text,
            "line_count": len(ocr_lines),
            "char_count": len(full_text),
            "avg_score": round(avg_score, 4),
            "report_page_candidates": [],
            "lines": ocr_lines,
            "elapsed_sec": round(elapse, 3),
            "native_diagnosis": "batch_ocr_expand",
            "sample_id": Path(pdf_path).stem.split("_")[0],
        }
    except Exception as e:
        print(f"    Page {page_index+1} OCR error: {e}")
        return None


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=str, help="Single sample ID")
    parser.add_argument("--all-image", action="store_true", help="All image-type PDFs")
    parser.add_argument("--pages", type=str, help="Specific page numbers (comma-separated)")
    parser.add_argument("--force", action="store_true", help="OCR selected pages even when native text is non-empty")
    args = parser.parse_args()

    samples = json.loads(SAMPLE_JSON.read_text(encoding="utf-8"))["samples"]
    sample_map = {s["sample_id"]: s for s in samples}

    if args.sample:
        targets = [args.sample]
    elif args.all_image:
        targets = sorted(IMAGE_PDF_SAMPLES & set(sample_map.keys()))
    else:
        print("Usage: --sample GL010 | --all-image")
        sys.exit(1)

    # 初始化 RapidOCR
    print("Initializing RapidOCR...")
    from rapidocr import RapidOCR
    engine = RapidOCR()
    print("Ready.\n")

    OCR_JSON_DIR.mkdir(parents=True, exist_ok=True)
    OCR_TEXT_DIR.mkdir(parents=True, exist_ok=True)

    import pdfplumber

    total_pages = 0
    total_ocr = 0

    for sid in targets:
        sample = sample_map[sid]
        pdf_path = sample["pdf_path"]
        existing = get_existing_ocr_pages(sid)

        print(f"{sid} {sample['short_name']}: checking pages...")

        # 扫描需要OCR的页面
        pages_to_ocr = []
        with pdfplumber.open(pdf_path) as pdf:
            for i in range(len(pdf.pages)):
                page_num = i + 1
                if page_num in existing:
                    continue  # 已有缓存
                if args.pages:
                    target_pages = {int(p.strip()) for p in args.pages.split(",")}
                    if page_num not in target_pages:
                        continue

                text = pdf.pages[i].extract_text() or ""
                if args.force or needs_ocr(text):
                    pages_to_ocr.append(i)

        if not pages_to_ocr:
            print(f"  All pages covered ({len(existing)} OCR pages)")
            continue

        print(f"  {len(pages_to_ocr)} pages need OCR (existing: {len(existing)})")

        for i in pages_to_ocr:
            page_num = i + 1
            result = ocr_page(pdf_path, i, engine)
            if result:
                # 保存JSON
                json_path = OCR_JSON_DIR / f"{sid}_page_{page_num:03d}_ocr.json"
                json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

                # 保存纯文本
                text_path = OCR_TEXT_DIR / f"{sid}_page_{page_num:03d}_ocr.txt"
                text_path.write_text(result["ocr_text"], encoding="utf-8")

                total_ocr += 1
                print(f"    Page {page_num}: {result['char_count']} chars, score={result['avg_score']:.4f}")

            total_pages += 1

    print(f"\nDone: {total_ocr} pages OCR'd across {len(targets)} samples")


if __name__ == "__main__":
    main()
