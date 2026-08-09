#!/usr/bin/env python3
"""检查智析绿鉴算法运行所需的模块、源码与配置，不读取真实密钥。"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import sys
from pathlib import Path


ALGORITHM_DIR = Path(__file__).resolve().parent
MODULE_DIR = ALGORITHM_DIR / "内部模块"
CONFIG_DIR = ALGORITHM_DIR / "配置"

CORE_PACKAGES = ("pdfplumber", "openpyxl", "rapidfuzz")
OCR_PACKAGES = ("pypdfium2", "rapidocr")
LLM_PACKAGES = ("requests", "anthropic")
REQUIRED_FILES = (
    MODULE_DIR / "run_automated_batch_pipeline_v2.0.py",
    MODULE_DIR / "run_full_extraction_v0.9.py",
    MODULE_DIR / "run_p0_pilot_extraction.py",
    MODULE_DIR / "index_page_resolver.py",
    CONFIG_DIR / "ESG指标体系.json",
    CONFIG_DIR / "ESG指标体系.csv",
    CONFIG_DIR / "不可回写负样本库.csv",
    CONFIG_DIR / "定性指标披露规则.csv",
    CONFIG_DIR / "精度门控标记.csv",
)


def package_state(distribution_name: str) -> dict[str, str | bool]:
    module_name = distribution_name.replace("-", "_")
    available = importlib.util.find_spec(module_name) is not None
    version = ""
    if available:
        try:
            version = importlib.metadata.version(distribution_name)
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
    return {"available": available, "version": version}


def main() -> int:
    core = {name: package_state(name) for name in CORE_PACKAGES}
    ocr = {name: package_state(name) for name in OCR_PACKAGES}
    llm = {name: package_state(name) for name in LLM_PACKAGES}
    missing_files = [str(path.relative_to(ALGORITHM_DIR)) for path in REQUIRED_FILES if not path.is_file()]
    payload = {
        "python": {"executable": sys.executable, "version": sys.version.split()[0]},
        "core_packages": core,
        "ocr_packages": ocr,
        "llm_packages": llm,
        "required_file_count": len(REQUIRED_FILES),
        "missing_files": missing_files,
        "api_credentials": {
            "deepseek_configured": bool(os.environ.get("DEEPSEEK_API_KEY")),
            "claude_configured": bool(os.environ.get("CLAUDE_API_KEY")),
        },
    }
    core_ok = all(item["available"] for item in core.values())
    files_ok = not missing_files
    payload["core_ready"] = core_ok and files_ok
    payload["ocr_ready"] = all(item["available"] for item in ocr.values())
    payload["llm_ready"] = all(item["available"] for item in llm.values())
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["core_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
