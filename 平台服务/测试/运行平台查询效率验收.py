from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any


项目根目录 = Path(__file__).resolve().parents[2]
平台服务目录 = 项目根目录 / "平台服务"
if str(平台服务目录) not in sys.path:
    sys.path.insert(0, str(平台服务目录))

from 数据服务 import 数据服务, 平台路径, 当前时间  # noqa: E402
from 平台接口 import 创建平台应用  # noqa: E402


def 百分位(值: list[float], 比例: int) -> float:
    排序 = sorted(值)
    索引 = max(0, (比例 * len(排序) + 99) // 100 - 1)
    return 排序[索引]


def 测量(客户端, 地址: str, 次数: int = 30, 预热: int = 3) -> dict[str, Any]:
    for _ in range(预热):
        响应 = 客户端.get(地址)
        if 响应.status_code != 200:
            raise RuntimeError(f"预热请求失败：{地址} -> {响应.status_code}")
        响应.close()
    耗时: list[float] = []
    响应字节 = 0
    for _ in range(次数):
        开始 = time.perf_counter_ns()
        响应 = 客户端.get(地址)
        结束 = time.perf_counter_ns()
        if 响应.status_code != 200:
            raise RuntimeError(f"基准请求失败：{地址} -> {响应.status_code}")
        响应字节 = len(响应.data)
        响应.close()
        耗时.append((结束 - 开始) / 1_000_000)
    return {
        "path": 地址,
        "iterations": 次数,
        "response_bytes": 响应字节,
        "min_ms": round(min(耗时), 3),
        "median_ms": round(statistics.median(耗时), 3),
        "mean_ms": round(statistics.mean(耗时), 3),
        "p95_ms": round(百分位(耗时, 95), 3),
        "max_ms": round(max(耗时), 3),
    }


def main() -> None:
    种子数据库 = 项目根目录 / "正式数据产物" / "平台公开演示数据库.sqlite"
    with tempfile.TemporaryDirectory(prefix="esg_query_benchmark_") as 临时目录文本:
        临时目录 = Path(临时目录文本)
        路径 = 平台路径(
            种子数据库=种子数据库,
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
        with 服务.读连接() as 连接:
            企业 = [行[0] for 行 in 连接.execute(
                "SELECT company_id FROM company WHERE stock_code IN ('600000','600004') ORDER BY stock_code"
            )]
            任务编号 = 连接.execute("SELECT job_id FROM analysis_job ORDER BY created_at LIMIT 1").fetchone()[0]
            报告版本编号 = 连接.execute(
                "SELECT report_version_id FROM analysis_job WHERE job_id=?", (任务编号,)
            ).fetchone()[0]

        接口 = [
            ("平台概览", "/api/v1/summary", 30),
            ("企业分页", "/api/v1/companies?page=1&page_size=20", 30),
            ("企业精确检索", "/api/v1/companies?q=600000&page_size=20", 30),
            ("报告分页", "/api/v1/reports?page=1&page_size=20", 30),
            ("80项指标目录", "/api/v1/indicators", 30),
            ("证据全文检索", "/api/v1/search?q=温室气体&limit=50", 30),
            ("三年趋势", f"/api/v1/trends?company_id={企业[0]}&indicator_id=E_Q_009", 30),
            ("两企业对比", f"/api/v1/compare?company_id={企业[0]}&company_id={企业[1]}&indicator_id=E_Q_009&year=2025", 30),
            ("单任务结果", f"/api/v1/results?job_id={任务编号}", 30),
            ("单报告结果导出", f"/api/v1/exports/results.csv?report_version_id={报告版本编号}", 10),
        ]

        tracemalloc.start()
        开始 = time.perf_counter()
        结果 = [{"name": 名称, **测量(客户端, 地址, 次数)} for 名称, 地址, 次数 in 接口]
        总耗时 = time.perf_counter() - 开始
        当前分配, 峰值分配 = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        with 服务.读连接() as 连接:
            完整性 = 连接.execute("PRAGMA integrity_check").fetchone()[0]
            外键错误 = len(连接.execute("PRAGMA foreign_key_check").fetchall())
            数据量 = {
                "companies": 连接.execute("SELECT COUNT(*) FROM company").fetchone()[0],
                "reports": 连接.execute("SELECT COUNT(*) FROM report").fetchone()[0],
                "indicators": 连接.execute("SELECT COUNT(*) FROM indicator_catalog").fetchone()[0],
                "jobs": 连接.execute("SELECT COUNT(*) FROM analysis_job").fetchone()[0],
                "results": 连接.execute("SELECT COUNT(*) FROM extraction_result").fetchone()[0],
                "evidence": 连接.execute("SELECT COUNT(*) FROM evidence_span").fetchone()[0],
            }
        汇总 = {
            "purpose": "测量公开演示种子上的进程内查询性能，不含网络传输、浏览器渲染和算法抽取耗时",
            "generated_at": 当前时间(),
            "method": {
                "client": "Flask test client, single process, sequential requests",
                "warmup_iterations_per_endpoint": 3,
                "timed_iterations_default": 30,
                "csv_export_iterations": 10,
                "timing_clock": "time.perf_counter_ns",
                "memory_scope": "tracemalloc追踪的Python分配内存，不等同于进程RSS",
            },
            "dataset_counts": 数据量,
            "database_size_bytes": 路径.运行数据库.stat().st_size,
            "benchmark_wall_seconds": round(总耗时, 3),
            "python_traced_current_bytes": 当前分配,
            "python_traced_peak_bytes": 峰值分配,
            "database_integrity": 完整性,
            "foreign_key_errors": 外键错误,
            "endpoints": 结果,
        }
        输出目录 = 项目根目录 / "测试证据" / "平台查询效率"
        输出目录.mkdir(parents=True, exist_ok=True)
        输出 = 输出目录 / "平台查询效率验收结果.json"
        输出.write_text(json.dumps(汇总, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(汇总, ensure_ascii=False, indent=2))
        if 完整性 != "ok" or 外键错误:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
