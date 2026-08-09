from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter


算法目录 = Path(__file__).resolve().parents[1]
入口路径 = 算法目录 / "运行ESG指标抽取.py"
模块规格 = importlib.util.spec_from_file_location("esg_public_cli", 入口路径)
入口模块 = importlib.util.module_from_spec(模块规格)
assert 模块规格.loader is not None
模块规格.loader.exec_module(入口模块)


class 统一入口测试(unittest.TestCase):
    def setUp(self) -> None:
        self.临时目录对象 = tempfile.TemporaryDirectory(prefix="esg_cli_test_")
        self.临时目录 = Path(self.临时目录对象.name)
        self.PDF路径 = self.临时目录 / "单页报告.pdf"
        写入器 = PdfWriter()
        写入器.add_blank_page(width=595, height=842)
        with self.PDF路径.open("wb") as 文件:
            写入器.write(文件)

    def tearDown(self) -> None:
        self.临时目录对象.cleanup()

    def test_新上传报告清单包含真实页数(self) -> None:
        清单路径, 样本编号 = 入口模块.build_sample_manifest(
            [self.PDF路径], self.临时目录 / "输入清单.json",
            "600001", "测试公司", 2026, "ESG",
        )
        样本 = json.loads(清单路径.read_text(encoding="utf-8"))["samples"][0]
        self.assertEqual(样本编号, ["U001"])
        self.assertEqual(样本["page_count"], 1)
        self.assertEqual(len(样本["source_file_sha256"]), 64)

    def test_外部清单缺页数时生成规范化副本(self) -> None:
        原清单 = self.临时目录 / "外部清单.json"
        原清单.write_text(json.dumps({"samples": [{
            "sample_id": "X001", "stock_code": "600001", "short_name": "测试公司",
            "report_year": "2026", "report_type": "ESG", "pdf_path": str(self.PDF路径),
        }]}, ensure_ascii=False), encoding="utf-8")
        新清单, 编号 = 入口模块.normalize_sample_manifest(
            原清单, self.临时目录 / "规范化清单.json"
        )
        self.assertEqual(编号, ["X001"])
        self.assertEqual(json.loads(新清单.read_text(encoding="utf-8"))["samples"][0]["page_count"], 1)


if __name__ == "__main__":
    unittest.main()
