from __future__ import annotations

import csv
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


平台服务目录 = Path(__file__).resolve().parents[1]
if str(平台服务目录) not in sys.path:
    sys.path.insert(0, str(平台服务目录))

from 数据服务 import 数据服务, 平台路径, 当前时间, 项目根目录  # noqa: E402
from 任务执行器 import 任务执行器  # noqa: E402
from 平台接口 import 创建平台应用  # noqa: E402


class 数据服务测试(unittest.TestCase):
    def setUp(self) -> None:
        self.临时目录对象 = tempfile.TemporaryDirectory(prefix="esg_platform_test_")
        self.临时目录 = Path(self.临时目录对象.name)
        self.路径 = 平台路径(
            种子数据库=项目根目录 / "正式数据产物" / "平台公开演示数据库.sqlite",
            运行目录=self.临时目录,
            运行数据库=self.临时目录 / "平台数据库.sqlite",
            上传目录=self.临时目录 / "上传报告",
            任务目录=self.临时目录 / "分析任务",
            指标文件=项目根目录 / "算法源码" / "配置" / "ESG指标体系.json",
        )
        self.服务 = 数据服务(self.路径)
        self.服务.初始化()

    def tearDown(self) -> None:
        self.临时目录对象.cleanup()

    def _生成最小PDF(self, 名称: str = "测试报告.pdf") -> Path:
        路径 = self.临时目录 / 名称
        路径.write_bytes(b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n")
        return 路径

    def test_初始化保留种子事实并扩展年份(self) -> None:
        状态 = self.服务.就绪状态()
        self.assertTrue(状态["ready"])
        self.assertEqual(状态["companies"], 1428)
        self.assertEqual(状态["reports"], 3880)
        self.assertEqual(状态["indicators"], 80)
        with self.服务.读连接() as 连接:
            self.assertEqual(连接.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(连接.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            建表语句 = 连接.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='report'"
            ).fetchone()[0]
            self.assertIn("report_year BETWEEN 2000 AND 2100", 建表语句)
            self.assertEqual(len(连接.execute("PRAGMA foreign_key_check").fetchall()), 0)

    def test_重复初始化保持幂等(self) -> None:
        self.服务.初始化()
        状态 = self.服务.就绪状态()
        self.assertEqual(状态["reports"], 3880)
        self.assertEqual(状态["indicators"], 80)

    def test_公开演示种子包含三年可比结果且无本地路径(self) -> None:
        公开路径 = 平台路径(
            种子数据库=项目根目录 / "正式数据产物" / "平台公开演示数据库.sqlite",
            运行目录=self.临时目录 / "公开种子运行库",
            运行数据库=self.临时目录 / "公开种子运行库" / "平台数据库.sqlite",
            上传目录=self.临时目录 / "公开种子运行库" / "上传报告",
            任务目录=self.临时目录 / "公开种子运行库" / "分析任务",
            指标文件=项目根目录 / "算法源码" / "配置" / "ESG指标体系.json",
        )
        公开服务 = 数据服务(公开路径)
        公开服务.初始化()
        with 公开服务.读连接() as 连接:
            self.assertEqual(连接.execute("SELECT COUNT(*) FROM analysis_job").fetchone()[0], 15)
            self.assertEqual(连接.execute("SELECT COUNT(*) FROM extraction_result").fetchone()[0], 1164)
            self.assertEqual(连接.execute("SELECT COUNT(*) FROM evidence_span").fetchone()[0], 946)
            self.assertEqual(连接.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(len(连接.execute("PRAGMA foreign_key_check").fetchall()), 0)
            for 表, 列 in (
                ("analysis_job", "log_summary"),
                ("platform_event", "payload_json"),
                ("file_location", "relative_path"),
            ):
                self.assertEqual(
                    连接.execute(
                        f"SELECT COUNT(*) FROM {表} WHERE {列} LIKE '%D:\\%' OR {列} LIKE '%C:\\%'"
                    ).fetchone()[0],
                    0,
                )
            可比 = 连接.execute(
                """
                SELECT r.company_id, er.indicator_id
                  FROM extraction_result er
                  JOIN analysis_job j ON j.job_id=er.job_id
                  JOIN report_version rv ON rv.report_version_id=er.report_version_id
                  JOIN report r ON r.report_id=rv.report_id
                  JOIN indicator_catalog ic ON ic.indicator_id=er.indicator_id
                 WHERE j.status IN ('succeeded','partial') AND er.candidate_rank=1
                   AND er.candidate_status IN ('candidate_found','needs_review')
                   AND er.normalized_value IS NOT NULL AND er.unit_normalized IS NOT NULL
                   AND ic.metric_type='quantitative'
                 GROUP BY r.company_id, er.indicator_id
                HAVING COUNT(DISTINCT r.report_year)>=2
                   AND COUNT(DISTINCT er.unit_normalized)=1
                """
            ).fetchall()
            self.assertEqual(len(可比), 52)
        趋势 = 公开服务.趋势(可比[0][0], 可比[0][1])
        self.assertTrue(趋势["comparable"])
        self.assertGreaterEqual(len(趋势["points"]), 2)

    def test_可登记2026年报告并创建排队任务(self) -> None:
        结果 = self.服务.登记上传并创建任务(
            self._生成最小PDF(),
            股票代码="600001",
            报告年份=2026,
            企业简称="测试公司",
            报告标题="测试公司2026年度ESG报告",
            原始文件名="测试公司2026年度ESG报告.pdf",
        )
        self.assertTrue(结果["job_id"].startswith("job_"))
        self.assertTrue(结果["pdf_eof_ok"])
        任务 = self.服务.任务详情(结果["job_id"])
        self.assertEqual(任务["status"], "queued")
        self.assertEqual(self.服务.报告详情(结果["report_version_id"])["report_year"], 2026)
        self.assertTrue(self.服务.解析报告文件(结果["report_version_id"]).is_file())

    def test_相同文件冲突元数据被拒绝(self) -> None:
        文件 = self._生成最小PDF()
        self.服务.登记上传并创建任务(
            文件, 股票代码="600001", 报告年份=2026, 企业简称="甲公司",
            报告标题="甲公司2026年度ESG报告", 原始文件名=文件.name,
        )
        with self.assertRaisesRegex(ValueError, "另一公司或年份"):
            self.服务.登记上传并创建任务(
                文件, 股票代码="600002", 报告年份=2026, 企业简称="乙公司",
                报告标题="乙公司2026年度ESG报告", 原始文件名=文件.name,
            )

    def test_数据库拒绝跨报告结果(self) -> None:
        文件 = self._生成最小PDF()
        上传 = self.服务.登记上传并创建任务(
            文件, 股票代码="600001", 报告年份=2026, 企业简称="甲公司",
            报告标题="甲公司2026年度ESG报告", 原始文件名=文件.name,
        )
        with self.服务.读连接() as 连接:
            另一报告版本 = 连接.execute(
                "SELECT report_version_id FROM report_version WHERE report_version_id<>? LIMIT 1",
                (上传["report_version_id"],),
            ).fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            with self.服务.写连接() as 连接:
                连接.execute(
                    """
                    INSERT INTO extraction_result VALUES (
                        'result_cross_report', ?, ?, 'E_Q_001', 2026, 1,
                        'no_candidate', NULL, NULL, NULL, NULL, NULL,
                        'not_verified', 'unreviewed', 'live_pipeline',
                        'test_pipeline', 'v0.3', '2026-08-09T00:00:00Z'
                    )
                    """,
                    (上传["job_id"], 另一报告版本),
                )

    def test_未命中结果不得残留数值(self) -> None:
        文件 = self._生成最小PDF()
        上传 = self.服务.登记上传并创建任务(
            文件, 股票代码="600001", 报告年份=2026, 企业简称="甲公司",
            报告标题="甲公司2026年度ESG报告", 原始文件名=文件.name,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.服务.写连接() as 连接:
                连接.execute(
                    """
                    INSERT INTO extraction_result VALUES (
                        'result_stale_value', ?, ?, 'E_Q_001', 2026, 1,
                        'no_candidate', '100', 100.0, '吨', 'tCO2e', 0.9,
                        'not_verified', 'unreviewed', 'live_pipeline',
                        'test_pipeline', 'v0.3', '2026-08-09T00:00:00Z'
                    )
                    """,
                    (上传["job_id"], 上传["report_version_id"]),
                )

    def test_既有真实产物可保守导入且保持幂等(self) -> None:
        运行目录 = 项目根目录 / "运行产物" / "迁移后冒烟5份"
        if not 运行目录.is_dir():
            self.skipTest("公开仓库不附带本机回归运行产物；该用例仅在完整验收环境执行。")
        原计划路径 = next(运行目录.rglob("pipeline_plan_*.json"))
        原计划 = json.loads(原计划路径.read_text(encoding="utf-8-sig"))
        原最终CSV = Path(原计划["final_extraction_csv"])
        原自动CSV = next(运行目录.rglob("auto_verified_extraction_results_v1.0.csv"))

        夹具目录 = self.临时目录 / "导入夹具"
        夹具目录.mkdir()
        最终CSV = 夹具目录 / "最终抽取结果.csv"
        自动目录 = 夹具目录 / "自动验收"
        自动目录.mkdir()
        自动CSV = 自动目录 / "auto_verified_extraction_results_v1.0.csv"

        def 筛选样本(来源: Path, 目标: Path) -> tuple[int, str]:
            with 来源.open("r", encoding="utf-8-sig", newline="") as 输入:
                读取器 = csv.DictReader(输入)
                行列表 = [行 for 行 in 读取器 if 行["sample_id"] == "R236"]
                with 目标.open("w", encoding="utf-8-sig", newline="") as 输出:
                    写入器 = csv.DictWriter(输出, fieldnames=读取器.fieldnames)
                    写入器.writeheader()
                    写入器.writerows(行列表)
            return len(行列表), 行列表[0]["pdf_path"]

        结果行数, PDF文本路径 = 筛选样本(原最终CSV, 最终CSV)
        自动行数, _ = 筛选样本(原自动CSV, 自动CSV)
        self.assertEqual(结果行数, 自动行数)
        原计划["sample_ids"] = ["R236"]
        原计划["final_extraction_csv"] = str(最终CSV)
        (夹具目录 / "pipeline_plan_导入夹具.json").write_text(
            json.dumps(原计划, ensure_ascii=False), encoding="utf-8"
        )

        with self.服务.写连接() as 连接:
            上下文 = 连接.execute(
                """
                SELECT rv.report_version_id
                  FROM report r JOIN company c ON c.company_id=r.company_id
                  JOIN report_version rv ON rv.report_id=r.report_id AND rv.is_current=1
                 WHERE c.stock_code='600372' AND r.report_year=2025
                """
            ).fetchone()
            任务编号 = "job_existing_artifact_fixture"
            时间 = 当前时间()
            连接.execute(
                """
                INSERT INTO analysis_job(
                    job_id, report_version_id, run_id, status, stage, progress,
                    attempt, runner_mode, pipeline_version, created_at, started_at,
                    updated_at, request_key
                ) VALUES (?, ?, 'fixture_run', 'running', '导入测试', 90, 1,
                          'live_pipeline', 'fixture', ?, ?, ?, NULL)
                """,
                (任务编号, 上下文["report_version_id"], 时间, 时间, 时间),
            )

        执行器 = 任务执行器(self.服务)
        第一次 = 执行器.导入任务产物(任务编号, 夹具目录, Path(PDF文本路径))
        第二次 = 执行器.导入任务产物(任务编号, 夹具目录, Path(PDF文本路径))
        self.assertEqual(第一次["result_count"], 结果行数)
        self.assertEqual(第二次["result_count"], 结果行数)
        with self.服务.读连接() as 连接:
            self.assertEqual(
                连接.execute("SELECT COUNT(*) FROM extraction_result WHERE job_id=?", (任务编号,)).fetchone()[0],
                结果行数,
            )
            残留数值 = 连接.execute(
                """
                SELECT COUNT(*) FROM extraction_result
                 WHERE job_id=? AND candidate_status='no_candidate'
                   AND (raw_value IS NOT NULL OR normalized_value IS NOT NULL
                        OR unit_raw IS NOT NULL OR unit_normalized IS NOT NULL)
                """,
                (任务编号,),
            ).fetchone()[0]
            self.assertEqual(残留数值, 0)
            证据 = 连接.execute(
                """
                SELECT es.page_no, es.printed_page_label
                  FROM evidence_span es JOIN extraction_result er ON er.result_id=es.result_id
                 WHERE er.job_id=? AND er.indicator_id='E_Q_001'
                 ORDER BY er.candidate_rank LIMIT 1
                """,
                (任务编号,),
            ).fetchone()
            self.assertEqual(证据["page_no"], 37)
            self.assertEqual(证据["printed_page_label"], "70")
            self.assertEqual(
                连接.execute(
                    "SELECT COUNT(*) FROM platform_event WHERE event_type='verification_metadata' AND entity_id IN (SELECT result_id FROM extraction_result WHERE job_id=?)",
                    (任务编号,),
                ).fetchone()[0],
                结果行数,
            )

    def test_API健康查询上传幂等与文件范围请求(self) -> None:
        应用 = 创建平台应用(self.路径, 启动任务执行器=False)
        应用.config["TESTING"] = True
        客户端 = 应用.test_client()

        self.assertEqual(客户端.get("/api/v1/health").status_code, 200)
        就绪 = 客户端.get("/api/v1/readiness")
        self.assertEqual(就绪.status_code, 200)
        self.assertEqual(就绪.get_json()["data"]["reports"], 3880)
        企业 = 客户端.get("/api/v1/companies?q=600000&page_size=5").get_json()
        self.assertEqual(企业["meta"]["total"], 1)
        搜索 = 客户端.get("/api/v1/search?q=温室气体").get_json()["data"]
        self.assertGreaterEqual(len(搜索["indicators"]), 1)

        PDF内容 = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
        表单 = {
            "file": (io.BytesIO(PDF内容), "测试公司ESG报告.pdf"),
            "stock_code": "600001",
            "company_name": "测试公司",
            "report_year": "2026",
            "report_title": "测试公司2026年度ESG报告",
        }
        第一次 = 客户端.post(
            "/api/v1/reports", data=表单, content_type="multipart/form-data",
            headers={"Idempotency-Key": "api-upload-case-1"},
        )
        self.assertEqual(第一次.status_code, 202)
        第一次数据 = 第一次.get_json()["data"]
        self.assertFalse(第一次数据["deduplication"]["job"])

        第二次 = 客户端.post(
            "/api/v1/reports",
            data={
                **{k: v for k, v in 表单.items() if k != "file"},
                "file": (io.BytesIO(PDF内容), "测试公司ESG报告.pdf"),
            },
            content_type="multipart/form-data",
            headers={"Idempotency-Key": "api-upload-case-1"},
        )
        self.assertEqual(第二次.status_code, 202)
        第二次数据 = 第二次.get_json()["data"]
        self.assertEqual(第二次数据["job_id"], 第一次数据["job_id"])
        self.assertTrue(第二次数据["deduplication"]["job"])

        文件响应 = 客户端.get(
            f"/api/v1/reports/{第一次数据['report_version_id']}/file",
            headers={"Range": "bytes=0-4"},
        )
        self.assertEqual(文件响应.status_code, 206)
        self.assertEqual(文件响应.data, b"%PDF-")
        文件响应.close()

        错误上传 = 客户端.post(
            "/api/v1/reports",
            data={
                "file": (io.BytesIO(b"not a pdf"), "伪造.pdf"),
                "stock_code": "600001", "company_name": "测试公司",
                "report_year": "2026", "report_title": "伪造报告",
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(错误上传.status_code, 400)
        self.assertEqual(错误上传.get_json()["error"]["code"], "INVALID_ARGUMENT")


if __name__ == "__main__":
    unittest.main()
