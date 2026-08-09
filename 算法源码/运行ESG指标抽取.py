#!/usr/bin/env python3
"""智析绿鉴统一命令行入口。

默认只生成可复核执行计划；显式传入 --执行 后才运行 PDF 抽取、守卫、
自动验收、披露评分和展示数据构建。大模型调用还需单独启用并提供环境变量。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence


ALGORITHM_DIR = Path(__file__).resolve().parent
PROJECT_DIR = ALGORITHM_DIR.parent
PIPELINE = ALGORITHM_DIR / "内部模块" / "run_automated_batch_pipeline_v2.0.py"
CONFIG_DIR = ALGORITHM_DIR / "配置"
DEFAULT_OCR_CACHE = PROJECT_DIR / "运行缓存" / "OCR"

REPORT_NAME_RE = re.compile(
    r"^(?P<stock_code>\d{6})_(?P<report_year>20\d{2})_"
    r"(?P<tag_block>(?:#[A-Za-z0-9]+)+)_(?P<short_name>[^_]+)_.*\.pdf$",
    re.IGNORECASE,
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def pdf_page_count(path: Path) -> int:
    try:
        from pypdf import PdfReader

        count = len(PdfReader(str(path), strict=False).pages)
    except Exception:
        try:
            import pdfplumber

            with pdfplumber.open(path) as pdf:
                count = len(pdf.pages)
        except Exception as exc:
            raise ValueError(f"无法读取PDF页数：{path.name}") from exc
    if count < 1:
        raise ValueError(f"PDF不包含可读取页面：{path.name}")
    return count


def infer_report_metadata(path: Path) -> dict[str, str] | None:
    match = REPORT_NAME_RE.match(path.name)
    if not match:
        return None
    data = match.groupdict()
    tags = data.pop("tag_block").strip("#").split("#")
    data["report_type"] = "+".join(tags)
    return data


def build_sample_manifest(
    report_paths: list[Path],
    output_path: Path,
    stock_code: str | None,
    short_name: str | None,
    report_year: int | None,
    report_type: str,
) -> tuple[Path, list[str]]:
    if len(report_paths) > 1 and any((stock_code, short_name, report_year)):
        raise ValueError("批量报告不得共用单一公司元数据；请使用规范文件名或直接提供样本清单")

    samples: list[dict[str, Any]] = []
    sample_ids: list[str] = []
    for index, raw_path in enumerate(report_paths, start=1):
        path = raw_path.resolve()
        if not path.is_file() or path.suffix.lower() != ".pdf":
            raise ValueError(f"报告不存在或不是 PDF: {raw_path}")
        inferred = infer_report_metadata(path)
        if inferred is None:
            if len(report_paths) != 1 or not (stock_code and short_name and report_year):
                raise ValueError(
                    f"文件名无法识别公司与年度: {path.name}；"
                    "单文件请补充 --股票代码、--公司简称、--报告年度"
                )
            inferred = {
                "stock_code": stock_code,
                "short_name": short_name,
                "report_year": str(report_year),
                "report_type": report_type,
            }
        sample_id = f"U{index:03d}"
        sample_ids.append(sample_id)
        samples.append(
            {
                "sample_id": sample_id,
                "subset_rank": index,
                "stock_code": inferred["stock_code"],
                "short_name": inferred["short_name"],
                "report_type": inferred["report_type"],
                "report_year": inferred["report_year"],
                "file_name": path.name,
                "file_size_mb": round(path.stat().st_size / 1024 / 1024, 3),
                "page_count": pdf_page_count(path),
                "pdf_path": str(path),
                "source_file_sha256": file_sha256(path),
                "sampling_reason": "用户提交报告",
                "recommended_route": "由自动流水线判定",
            }
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps({"samples": samples}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output_path, sample_ids


def read_manifest_ids(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    samples = payload.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("样本清单必须包含非空 samples 数组")
    ids = [str(item.get("sample_id", "")).strip() for item in samples]
    if any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("样本清单中的 sample_id 不能为空或重复")
    return ids


def normalize_sample_manifest(path: Path, output_path: Path) -> tuple[Path, list[str]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    samples = payload.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("样本清单必须包含非空 samples 数组")
    ids: list[str] = []
    normalized: list[dict[str, Any]] = []
    required = ("sample_id", "stock_code", "short_name", "report_year", "report_type", "pdf_path")
    for index, raw in enumerate(samples, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"样本清单第{index}项必须为对象")
        item = dict(raw)
        missing = [key for key in required if not str(item.get(key, "")).strip()]
        if missing:
            raise ValueError(f"样本清单第{index}项缺少字段：{','.join(missing)}")
        sample_id = str(item["sample_id"]).strip()
        if sample_id in ids:
            raise ValueError(f"样本清单包含重复 sample_id：{sample_id}")
        pdf_path = Path(str(item["pdf_path"])).resolve()
        if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
            raise ValueError(f"样本PDF不存在或格式不正确：{pdf_path.name}")
        item["sample_id"] = sample_id
        item["stock_code"] = str(item["stock_code"]).strip()
        item["short_name"] = str(item["short_name"]).strip()
        item["report_year"] = str(item["report_year"]).strip()
        item["report_type"] = str(item["report_type"]).strip()
        item["pdf_path"] = str(pdf_path)
        item["file_name"] = str(item.get("file_name") or pdf_path.name)
        item["file_size_mb"] = round(pdf_path.stat().st_size / 1024 / 1024, 3)
        item["page_count"] = pdf_page_count(pdf_path)
        item["source_file_sha256"] = str(item.get("source_file_sha256") or file_sha256(pdf_path))
        ids.append(sample_id)
        normalized.append(item)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps({**payload, "samples": normalized}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output_path, ids


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行ESG报告指标抽取与可信分析流水线")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--报告", dest="reports", type=Path, action="append")
    source.add_argument("--样本清单", dest="sample_manifest", type=Path)
    parser.add_argument("--股票代码")
    parser.add_argument("--公司简称")
    parser.add_argument("--报告年度", type=int)
    parser.add_argument("--报告类型", default="ESG")
    parser.add_argument("--输出目录", type=Path)
    parser.add_argument("--OCR缓存目录", type=Path, default=DEFAULT_OCR_CACHE)
    parser.add_argument("--优先级", choices=("P0", "P1", "P2", "all"), default="P0")
    parser.add_argument("--执行", action="store_true")
    parser.add_argument("--启用DeepSeek", action="store_true")
    parser.add_argument("--运行编号", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    run_id = args.运行编号 or datetime.now().strftime("run_%Y%m%d_%H%M%S")
    output_root = (args.输出目录 or PROJECT_DIR / "运行产物" / run_id).resolve()

    try:
        if args.sample_manifest:
            manifest, sample_ids = normalize_sample_manifest(
                args.sample_manifest.resolve(), output_root / "输入清单_规范化.json"
            )
        else:
            manifest, sample_ids = build_sample_manifest(
                args.reports,
                output_root / "输入清单.json",
                args.股票代码,
                args.公司简称,
                args.报告年度,
                args.报告类型,
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    command = [
        sys.executable,
        str(PIPELINE),
        "--samples",
        ",".join(sample_ids),
        "--sample-json",
        str(manifest),
        "--run-id",
        run_id,
        "--priority",
        args.优先级,
        "--ocr-cache-dir",
        str(args.OCR缓存目录.resolve()),
        "--out-root",
        str(output_root / "抽取结果"),
        "--quality-dir",
        str(output_root / "质量审计"),
        "--plan-dir",
        str(output_root / "执行计划"),
        "--auto-verification-dir",
        str(output_root / "自动验收"),
        "--scoring-dir",
        str(output_root / "披露评分"),
        "--dashboard-dir",
        str(output_root / "展示数据"),
        "--negative-casebook",
        str(CONFIG_DIR / "不可回写负样本库.csv"),
        "--indicator-csv",
        str(CONFIG_DIR / "ESG指标体系.csv"),
        "--indicator-json",
        str(CONFIG_DIR / "ESG指标体系.json"),
        "--rule-flags-csv",
        str(CONFIG_DIR / "精度门控标记.csv"),
        "--qualitative-rules-csv",
        str(CONFIG_DIR / "定性指标披露规则.csv"),
        "--skip-sample-quant-reconcile",
    ]
    if args.执行:
        command.append("--execute")
    if args.启用DeepSeek:
        command.remove("--skip-sample-quant-reconcile")
        command.append("--execute-deepseek")

    result = subprocess.run(command, cwd=str(PROJECT_DIR), check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
