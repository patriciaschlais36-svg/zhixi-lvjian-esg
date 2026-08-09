from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


项目根目录 = Path(__file__).resolve().parents[2]
平台目录 = 项目根目录 / "平台服务"
if str(平台目录) not in sys.path:
    sys.path.insert(0, str(平台目录))

from 数据服务 import 数据服务, 当前时间, 平台路径, 稳定编号  # noqa: E402
from 任务执行器 import 任务执行器  # noqa: E402


def 参数解析() -> argparse.Namespace:
    解析器 = argparse.ArgumentParser(description="构建三年可比结果演示种子")
    解析器.add_argument("--公司数", type=int, default=5)
    解析器.add_argument("--重新运行失败任务", action="store_true")
    解析器.add_argument(
        "--证据目录", type=Path,
        default=项目根目录 / "测试证据" / "三年可比演示种子",
    )
    解析器.add_argument(
        "--公开种子数据库", type=Path,
        default=项目根目录 / "正式数据产物" / "平台公开演示数据库.sqlite",
    )
    return 解析器.parse_args()


def 选择报告(服务: 数据服务, 公司数: int) -> list[dict[str, Any]]:
    if not 1 <= 公司数 <= 20:
        raise ValueError("公司数必须在1至20之间")
    with 服务.读连接() as 连接:
        公司 = [dict(行) for 行 in 连接.execute(
            """
            SELECT dcm.member_rank, c.company_id, c.stock_code, c.current_short_name
              FROM dataset_company_member dcm
              JOIN dataset_snapshot ds ON ds.snapshot_id=dcm.snapshot_id
              JOIN company c ON c.company_id=dcm.company_id
             WHERE ds.snapshot_label='P177'
             ORDER BY c.stock_code
             LIMIT ?
            """,
            (公司数,),
        )]
        报告: list[dict[str, Any]] = []
        for 企业 in 公司:
            年度报告 = [dict(行) for 行 in 连接.execute(
                """
                SELECT cs.report_year, cs.canonical_report_version_id AS report_version_id,
                       r.canonical_title, r.primary_report_type, fl.root_code, fl.relative_path
                  FROM coverage_slot cs
                  JOIN report r ON r.report_id=cs.canonical_report_id
                  JOIN file_location fl ON fl.report_version_id=cs.canonical_report_version_id
                                      AND fl.is_available=1
                 WHERE cs.company_id=? AND cs.coverage_status='present'
                   AND cs.report_year IN (2023, 2024, 2025)
                 ORDER BY cs.report_year
                """,
                (企业["company_id"],),
            )]
            if len(年度报告) != 3:
                raise RuntimeError(f"{企业['stock_code']}未形成三个完整年度报告")
            for 项 in 年度报告:
                项.update(企业)
                报告.append(项)
    return 报告


def 创建任务(服务: 数据服务, 报告: dict[str, Any], 重新运行失败任务: bool) -> str:
    请求键 = f"三年可比演示种子|{报告['report_version_id']}|P0"
    任务编号 = 稳定编号("job", 请求键)
    运行编号 = 稳定编号("run", 请求键)
    时间 = 当前时间()
    with 服务.写连接() as 连接:
        已有 = 连接.execute("SELECT status FROM analysis_job WHERE job_id=?", (任务编号,)).fetchone()
        if 已有 and 已有["status"] == "failed" and 重新运行失败任务:
            连接.execute("DELETE FROM evidence_span WHERE result_id IN (SELECT result_id FROM extraction_result WHERE job_id=?)", (任务编号,))
            连接.execute("DELETE FROM extraction_result WHERE job_id=?", (任务编号,))
            连接.execute("DELETE FROM analysis_job WHERE job_id=?", (任务编号,))
            已有 = None
        if not 已有:
            连接.execute(
                """
                INSERT INTO analysis_job(
                    job_id, report_version_id, run_id, status, stage, progress,
                    attempt, runner_mode, pipeline_version, created_at, updated_at,
                    request_key
                ) VALUES (?, ?, ?, 'queued', '等待三年可比演示抽取', 0, 1,
                          'live_pipeline', '智析绿鉴可信抽取引擎', ?, ?, ?)
                """,
                (任务编号, 报告["report_version_id"], 运行编号, 时间, 时间, 请求键),
            )
    return 任务编号


def 统计结果(服务: 数据服务, 任务编号列表: list[str]) -> dict[str, Any]:
    占位符 = ",".join("?" for _ in 任务编号列表)
    with 服务.读连接() as 连接:
        任务 = [dict(行) for 行 in 连接.execute(
            f"""
            SELECT j.job_id, j.report_version_id, j.status, j.stage, j.created_at,
                   j.started_at, j.finished_at, j.error_code, j.error_message,
                   c.stock_code, c.current_short_name, r.report_year,
                   COUNT(DISTINCT er.result_id) AS result_count,
                   COUNT(DISTINCT CASE WHEN er.candidate_status IN ('candidate_found','needs_review')
                                       THEN er.result_id END) AS candidate_count,
                   COUNT(DISTINCT es.evidence_id) AS evidence_count
              FROM analysis_job j
              JOIN report_version rv ON rv.report_version_id=j.report_version_id
              JOIN report r ON r.report_id=rv.report_id
              JOIN company c ON c.company_id=r.company_id
              LEFT JOIN extraction_result er ON er.job_id=j.job_id
              LEFT JOIN evidence_span es ON es.result_id=er.result_id
             WHERE j.job_id IN ({占位符})
             GROUP BY j.job_id
             ORDER BY c.stock_code, r.report_year
            """,
            任务编号列表,
        )]
        可比组合 = [dict(行) for 行 in 连接.execute(
            f"""
            SELECT r.company_id, c.stock_code, c.current_short_name, er.indicator_id,
                   ic.metric_name_cn, COUNT(DISTINCT r.report_year) AS year_count,
                   MIN(r.report_year) AS first_year, MAX(r.report_year) AS latest_year,
                   MIN(er.unit_normalized) AS unit_normalized
              FROM extraction_result er
              JOIN analysis_job j ON j.job_id=er.job_id
              JOIN report_version rv ON rv.report_version_id=er.report_version_id
              JOIN report r ON r.report_id=rv.report_id
              JOIN company c ON c.company_id=r.company_id
              JOIN indicator_catalog ic ON ic.indicator_id=er.indicator_id
             WHERE er.job_id IN ({占位符})
               AND j.status IN ('succeeded','partial')
               AND er.candidate_rank=1
               AND er.candidate_status IN ('candidate_found','needs_review')
               AND er.normalized_value IS NOT NULL
               AND er.unit_normalized IS NOT NULL
               AND ic.metric_type='quantitative'
             GROUP BY r.company_id, er.indicator_id
            HAVING COUNT(DISTINCT r.report_year)>=2
               AND COUNT(DISTINCT er.unit_normalized)=1
             ORDER BY c.stock_code, er.indicator_id
            """,
            任务编号列表,
        )]
        数据库检查 = 连接.execute("PRAGMA integrity_check").fetchone()[0]
        外键错误 = len(连接.execute("PRAGMA foreign_key_check").fetchall())
    def 解析时间(文本: str | None) -> datetime | None:
        return datetime.fromisoformat(文本.replace("Z", "+00:00")) if 文本 else None

    持续时间: list[float] = []
    for 项 in 任务:
        开始, 结束 = 解析时间(项.get("started_at")), 解析时间(项.get("finished_at"))
        项["duration_seconds"] = round((结束 - 开始).total_seconds(), 3) if 开始 and 结束 else None
        if 项["duration_seconds"] is not None:
            持续时间.append(项["duration_seconds"])
    排序持续时间 = sorted(持续时间)
    开始列表 = [解析时间(x.get("started_at")) for x in 任务 if x.get("started_at")]
    结束列表 = [解析时间(x.get("finished_at")) for x in 任务 if x.get("finished_at")]
    计时统计 = {
        "count": len(持续时间),
        "min_seconds": min(持续时间) if 持续时间 else None,
        "median_seconds": round(statistics.median(持续时间), 3) if 持续时间 else None,
        "mean_seconds": round(statistics.mean(持续时间), 3) if 持续时间 else None,
        "p95_seconds": 排序持续时间[max(0, (95 * len(排序持续时间) + 99) // 100 - 1)] if 持续时间 else None,
        "max_seconds": max(持续时间) if 持续时间 else None,
        "sum_job_seconds": round(sum(持续时间), 3) if 持续时间 else None,
        "batch_wall_seconds": round((max(结束列表) - min(开始列表)).total_seconds(), 3) if 开始列表 and 结束列表 else None,
    }
    return {
        "jobs": 任务,
        "comparable_series": 可比组合,
        "summary": {
            "job_count": len(任务),
            "successful_or_partial_jobs": sum(x["status"] in {"succeeded", "partial"} for x in 任务),
            "failed_jobs": sum(x["status"] == "failed" for x in 任务),
            "result_count": sum(x["result_count"] or 0 for x in 任务),
            "candidate_count": sum(x["candidate_count"] or 0 for x in 任务),
            "evidence_count": sum(x["evidence_count"] or 0 for x in 任务),
            "comparable_series_count": len(可比组合),
            "companies_with_comparable_series": len({x["stock_code"] for x in 可比组合}),
            "database_integrity": 数据库检查,
            "foreign_key_errors": 外键错误,
            "recorded_timing": 计时统计,
        },
    }


def 写公开数据库(服务: 数据服务, 目标: Path) -> dict[str, Any]:
    目标 = 目标.resolve()
    目标.parent.mkdir(parents=True, exist_ok=True)
    临时 = 目标.with_suffix(".构建中.sqlite")
    临时.unlink(missing_ok=True)
    源连接 = sqlite3.connect(服务.路径.运行数据库)
    目标连接 = sqlite3.connect(临时)
    try:
        源连接.backup(目标连接)
        目标连接.execute("UPDATE analysis_job SET log_summary=NULL")
        目标连接.execute("DELETE FROM platform_event WHERE event_type='service_initialized'")
        目标连接.execute(
            """
            UPDATE platform_event
               SET payload_json='{"provenance":"algorithm_output","automatic_verification":"machine_rule"}'
             WHERE event_type='verification_metadata'
            """
        )
        目标连接.commit()
        目标连接.execute("VACUUM")
        完整性 = 目标连接.execute("PRAGMA integrity_check").fetchone()[0]
        外键错误 = len(目标连接.execute("PRAGMA foreign_key_check").fetchall())
        文本列命中 = 0
        for 表 in ("analysis_job", "platform_event", "file_location"):
            列 = [行[1] for 行 in 目标连接.execute(f"PRAGMA table_info({表})") if str(行[2]).upper() == "TEXT"]
            for 列名 in 列:
                文本列命中 += 目标连接.execute(
                    f"SELECT COUNT(*) FROM {表} WHERE {列名} LIKE '%D:\\%' OR {列名} LIKE '%C:\\%'"
                ).fetchone()[0]
    finally:
        源连接.close()
        目标连接.close()
    if 完整性 != "ok" or 外键错误 or 文本列命中:
        临时.unlink(missing_ok=True)
        raise RuntimeError(f"公开种子验收失败：integrity={完整性}, foreign_keys={外键错误}, local_paths={文本列命中}")
    临时.replace(目标)
    摘要器 = hashlib.sha256()
    with 目标.open("rb") as 文件:
        while 数据 := 文件.read(1024 * 1024):
            摘要器.update(数据)
    return {
        "path": str(目标.relative_to(项目根目录)),
        "size_bytes": 目标.stat().st_size,
        "sha256": 摘要器.hexdigest(),
        "database_integrity": 完整性,
        "foreign_key_errors": 外键错误,
        "local_path_hits": 文本列命中,
    }


def main() -> None:
    参数 = 参数解析()
    if not os.environ.get("ESG_REPORT_ROOT_SSE_ESG_2023_2025"):
        raise RuntimeError("必须通过ESG_REPORT_ROOT_SSE_ESG_2023_2025配置报告根目录")
    服务 = 数据服务(平台路径.从环境变量())
    服务.初始化()
    执行器 = 任务执行器(服务)
    报告 = 选择报告(服务, 参数.公司数)
    任务编号列表: list[str] = []
    运行记录: list[dict[str, Any]] = []
    总开始 = time.perf_counter()

    for 序号, 报告项 in enumerate(报告, start=1):
        任务编号 = 创建任务(服务, 报告项, 参数.重新运行失败任务)
        任务编号列表.append(任务编号)
        执行前 = 服务.任务详情(任务编号)
        开始 = time.perf_counter()
        if 执行前 and 执行前["status"] in {"queued", "running"}:
            if 执行前["status"] == "queued":
                with 服务.写连接() as 连接:
                    连接.execute(
                        "UPDATE analysis_job SET status='running', stage='三年可比演示抽取运行中', progress=1, started_at=?, updated_at=? WHERE job_id=?",
                        (当前时间(), 当前时间(), 任务编号),
                    )
            执行器._运行单任务(任务编号)
        结束后 = 服务.任务详情(任务编号)
        运行记录.append({
            "sequence": 序号,
            "job_id": 任务编号,
            "stock_code": 报告项["stock_code"],
            "company_name": 报告项["current_short_name"],
            "report_year": 报告项["report_year"],
            "report_version_id": 报告项["report_version_id"],
            "status_before": 执行前["status"] if 执行前 else None,
            "status_after": 结束后["status"] if 结束后 else None,
            "executed_this_invocation": bool(执行前 and 执行前["status"] in {"queued", "running"}),
            "orchestration_seconds_this_invocation": round(time.perf_counter() - 开始, 3),
        })
        print(f"[{序号}/{len(报告)}] {报告项['stock_code']} {报告项['report_year']} -> {结束后['status']}", flush=True)

    统计 = 统计结果(服务, 任务编号列表)
    公开数据库 = 写公开数据库(服务, 参数.公开种子数据库)
    证据 = {
        "purpose": "三年趋势与用户选定企业对比功能演示种子，不作为准确率评估样本",
        "selection_rule": "P177中按证券代码升序排列的前N家公司，每家公司固定选择2023、2024、2025三个覆盖状态为present的报告",
        "model_api_enabled": False,
        "priority": "P0",
        "generated_at": 当前时间(),
        "orchestration_seconds_this_invocation": round(time.perf_counter() - 总开始, 3),
        "selected_reports": 报告,
        "run_records": 运行记录,
        **统计,
        "public_seed_database": 公开数据库,
    }
    参数.证据目录.mkdir(parents=True, exist_ok=True)
    输出 = 参数.证据目录 / "三年可比演示种子验收结果.json"
    输出.write_text(json.dumps(证据, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(证据["summary"], ensure_ascii=False, indent=2))
    if 统计["summary"]["failed_jobs"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
