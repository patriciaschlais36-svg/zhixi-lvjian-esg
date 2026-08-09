from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Any


项目根目录 = Path(__file__).resolve().parents[2]
平台服务目录 = 项目根目录 / "平台服务"
if str(平台服务目录) not in sys.path:
    sys.path.insert(0, str(平台服务目录))

from 数据服务 import 数据服务, 平台路径, 当前时间  # noqa: E402
from 平台接口 import 创建平台应用  # noqa: E402


def 表单(内容: bytes, 文件名: str = "测试报告.pdf", **覆盖: str) -> dict[str, Any]:
    数据: dict[str, Any] = {
        "file": (io.BytesIO(内容), 文件名),
        "stock_code": "600001",
        "company_name": "测试公司",
        "report_year": "2026",
        "report_title": "测试公司2026年度ESG报告",
    }
    数据.update(覆盖)
    return 数据


def 记录响应(名称: str, 响应, 预期状态: int, 预期代码: str | None = None) -> dict[str, Any]:
    载荷 = 响应.get_json(silent=True) or {}
    代码 = (载荷.get("error") or {}).get("code")
    通过 = 响应.status_code == 预期状态 and (预期代码 is None or 代码 == 预期代码)
    return {
        "case": 名称,
        "status_code": 响应.status_code,
        "error_code": 代码,
        "expected_status": 预期状态,
        "expected_error_code": 预期代码,
        "passed": 通过,
    }


def main() -> None:
    正常PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
    另一PDF = b"%PDF-1.4\n% second\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
    截断PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n"
    with tempfile.TemporaryDirectory(prefix="esg_abnormal_acceptance_") as 临时目录文本:
        临时目录 = Path(临时目录文本)
        路径 = 平台路径(
            种子数据库=项目根目录 / "正式数据产物" / "平台公开演示数据库.sqlite",
            运行目录=临时目录,
            运行数据库=临时目录 / "平台数据库.sqlite",
            上传目录=临时目录 / "上传报告",
            任务目录=临时目录 / "分析任务",
            指标文件=项目根目录 / "算法源码" / "配置" / "ESG指标体系.json",
        )
        服务 = 数据服务(路径)
        应用 = 创建平台应用(路径, 启动任务执行器=False)
        应用.config.update(TESTING=True)
        客户端 = 应用.test_client()
        结果: list[dict[str, Any]] = []

        响应 = 客户端.post("/api/v1/reports", data=表单(b"not-pdf"), content_type="multipart/form-data")
        结果.append(记录响应("伪造PDF文件头", 响应, 400, "INVALID_ARGUMENT"))

        响应 = 客户端.post("/api/v1/reports", data=表单(正常PDF, "测试报告.txt"), content_type="multipart/form-data")
        结果.append(记录响应("错误扩展名", 响应, 400, "INVALID_ARGUMENT"))

        响应 = 客户端.post("/api/v1/reports", data=表单(正常PDF, stock_code="60001"), content_type="multipart/form-data")
        结果.append(记录响应("股票代码长度错误", 响应, 400, "INVALID_ARGUMENT"))

        响应 = 客户端.post("/api/v1/reports", data=表单(正常PDF, report_year="1999"), content_type="multipart/form-data")
        结果.append(记录响应("报告年份越界", 响应, 400, "INVALID_ARGUMENT"))

        响应 = 客户端.post("/api/v1/reports", data=表单(正常PDF, report_title=""), content_type="multipart/form-data")
        结果.append(记录响应("报告标题为空", 响应, 400, "INVALID_ARGUMENT"))

        响应 = 客户端.post("/api/v1/reports", data=表单(截断PDF), content_type="multipart/form-data")
        截断数据 = (响应.get_json(silent=True) or {}).get("data") or {}
        截断记录 = 记录响应("缺少PDF尾标记时降级登记", 响应, 202)
        截断记录["pdf_eof_ok"] = 截断数据.get("pdf_eof_ok")
        截断记录["passed"] = 截断记录["passed"] and 截断数据.get("pdf_eof_ok") is False
        结果.append(截断记录)

        首次 = 客户端.post(
            "/api/v1/reports", data=表单(正常PDF), content_type="multipart/form-data",
            headers={"Idempotency-Key": "abnormal-idempotency-case"},
        )
        结果.append(记录响应("合法PDF登记", 首次, 202))
        冲突 = 客户端.post(
            "/api/v1/reports", data=表单(另一PDF), content_type="multipart/form-data",
            headers={"Idempotency-Key": "abnormal-idempotency-case"},
        )
        结果.append(记录响应("同一幂等键绑定不同文件", 冲突, 400, "INVALID_ARGUMENT"))

        元数据冲突 = 客户端.post(
            "/api/v1/reports", data=表单(正常PDF, stock_code="600002"), content_type="multipart/form-data"
        )
        结果.append(记录响应("相同文件绑定不同公司", 元数据冲突, 400, "INVALID_ARGUMENT"))

        长检索 = 客户端.get("/api/v1/search?q=" + "A" * 101)
        结果.append(记录响应("超长检索词", 长检索, 400, "INVALID_ARGUMENT"))

        注入文本 = "%' OR 1=1 --"
        注入响应 = 客户端.get("/api/v1/search", query_string={"q": 注入文本})
        注入记录 = 记录响应("检索参数化防注入", 注入响应, 200)
        注入记录["database_integrity"] = 服务.就绪状态()["database_integrity"]
        注入记录["passed"] = 注入记录["passed"] and 注入记录["database_integrity"] == "ok"
        结果.append(注入记录)

        未知接口 = 客户端.get("/api/v1/not-found")
        结果.append(记录响应("未知接口统一错误", 未知接口, 404))

        首次数据 = 首次.get_json()["data"]
        with 服务.写连接() as 连接:
            连接.execute(
                "UPDATE file_location SET relative_path='../越界.pdf' WHERE report_version_id=? AND root_code='UPLOADS'",
                (首次数据["report_version_id"],),
            )
        路径防护 = 服务.解析报告文件(首次数据["report_version_id"])
        结果.append({
            "case": "报告文件路径越界防护",
            "resolved_path": str(路径防护) if 路径防护 else None,
            "expected": None,
            "passed": 路径防护 is None,
        })

        应用.config["MAX_CONTENT_LENGTH"] = 1024
        超大响应 = 客户端.post(
            "/api/v1/reports",
            data=表单(b"%PDF-1.4\n" + b"X" * 4096 + b"\n%%EOF\n", stock_code="600003"),
            content_type="multipart/form-data",
        )
        结果.append(记录响应("超过上传上限", 超大响应, 413, "UPLOAD_TOO_LARGE"))

        with 服务.读连接() as 连接:
            完整性 = 连接.execute("PRAGMA integrity_check").fetchone()[0]
            外键错误 = len(连接.execute("PRAGMA foreign_key_check").fetchall())
        汇总 = {
            "purpose": "验证新增动态平台的异常输入、幂等、参数化查询与路径边界，不重复算法基础抽取实验",
            "generated_at": 当前时间(),
            "case_count": len(结果),
            "passed_count": sum(x["passed"] for x in 结果),
            "failed_count": sum(not x["passed"] for x in 结果),
            "database_integrity": 完整性,
            "foreign_key_errors": 外键错误,
            "cases": 结果,
        }
        输出目录 = 项目根目录 / "测试证据" / "异常输入与接口防护"
        输出目录.mkdir(parents=True, exist_ok=True)
        输出 = 输出目录 / "平台异常输入验收结果.json"
        输出.write_text(json.dumps(汇总, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(汇总, ensure_ascii=False, indent=2))
        if 汇总["failed_count"] or 完整性 != "ok" or 外键错误:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
