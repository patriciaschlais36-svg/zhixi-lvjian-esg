from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


脚本路径 = Path(__file__).resolve().parents[1] / "工具" / "ESG报告合规采集器.py"
模块规格 = importlib.util.spec_from_file_location("合规采集器", 脚本路径)
模块 = importlib.util.module_from_spec(模块规格)
assert 模块规格 and 模块规格.loader
sys.modules[模块规格.name] = 模块
模块规格.loader.exec_module(模块)


class 合规采集器测试(unittest.TestCase):
    def setUp(self) -> None:
        self.临时目录对象 = tempfile.TemporaryDirectory(prefix="esg_collector_test_")
        self.临时目录 = Path(self.临时目录对象.name)

    def tearDown(self) -> None:
        self.临时目录对象.cleanup()

    def _写清单(self, 内容: str) -> Path:
        路径 = self.临时目录 / "采集清单.csv"
        路径.write_text(内容, encoding="utf-8-sig")
        return 路径

    def test_读取有效清单并规范报告类型(self) -> None:
        路径 = self._写清单(
            "stock_code,company_name,report_year,report_title,report_type,source_url,source_notice_id\n"
            "600000,示例公司,2025,示例公司可持续发展报告,sd,https://example.org/report.pdf,N001\n"
        )
        项目 = 模块.读取采集清单(路径)[0]
        self.assertEqual(项目.报告类型, "SD")
        self.assertEqual(项目.来源公告编号, "N001")

    def test_计划校验不访问网络(self) -> None:
        路径 = self._写清单(
            "stock_code,company_name,report_year,report_title,source_url\n"
            "600000,示例公司,2025,示例公司ESG报告,https://example.org/report.pdf\n"
        )
        返回码 = 模块.main([str(路径), "--允许域名", "example.org"])
        self.assertEqual(返回码, 0)

    def test_拒绝非允许域名(self) -> None:
        with self.assertRaisesRegex(ValueError, "显式允许列表"):
            模块.校验网址边界(
                "https://files.example.net/report.pdf", {"example.org"}, 解析DNS=False,
            )

    def test_拒绝内网与非HTTPS地址(self) -> None:
        with self.assertRaisesRegex(ValueError, "禁止采集"):
            模块.校验网址边界("https://127.0.0.1/report.pdf", {"127.0.0.1"}, 解析DNS=False)
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            模块.校验网址边界("http://example.org/report.pdf", {"example.org"}, 解析DNS=False)


if __name__ == "__main__":
    unittest.main()
