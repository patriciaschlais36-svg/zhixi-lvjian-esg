from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path


平台服务目录 = Path(__file__).resolve().parents[1]
项目根目录 = 平台服务目录.parent
if str(平台服务目录) not in sys.path:
    sys.path.insert(0, str(平台服务目录))

from 数据服务 import 数据服务, 平台路径, 当前时间  # noqa: E402
from 任务执行器 import 任务执行器  # noqa: E402


def 文件哈希(路径: Path) -> str:
    摘要器 = hashlib.sha256()
    with 路径.open("rb") as 文件:
        while 数据 := 文件.read(1024 * 1024):
            摘要器.update(数据)
    return 摘要器.hexdigest()


def 定位验收报告与缓存() -> tuple[Path, Path]:
    回归目录 = 项目根目录 / "运行产物" / "迁移后冒烟5份"
    自动文件 = next(回归目录.rglob("auto_verified_extraction_results_v1.0.csv"))
    with 自动文件.open("r", encoding="utf-8-sig", newline="") as 文件:
        行 = next(x for x in csv.DictReader(文件) if x["sample_id"] == "R236")
    报告 = Path(行["pdf_path"])
    计划文件 = next(回归目录.rglob("pipeline_plan_*.json"))
    计划 = json.loads(计划文件.read_text(encoding="utf-8-sig"))
    基础步骤 = next(x for x in 计划["steps"] if x["name"] == "base_extraction")
    缓存 = Path(基础步骤["env"]["OCR_CACHE_DIR"])
    if not 报告.is_file() or not 缓存.is_dir():
        raise FileNotFoundError("既有回归报告或OCR缓存不可用")
    return 报告, 缓存


def 主程序() -> int:
    报告, 缓存 = 定位验收报告与缓存()
    证据路径 = 项目根目录 / "测试证据" / "平台真实链路验收.json"
    with tempfile.TemporaryDirectory(prefix="esg_platform_e2e_") as 临时文本:
        临时目录 = Path(临时文本)
        路径 = 平台路径(
            种子数据库=项目根目录 / "正式数据产物" / "三年报告元数据与P531哈希库.sqlite",
            运行目录=临时目录,
            运行数据库=临时目录 / "平台数据库.sqlite",
            上传目录=临时目录 / "上传报告",
            任务目录=临时目录 / "分析任务",
            指标文件=项目根目录 / "算法源码" / "配置" / "ESG指标体系.json",
        )
        服务 = 数据服务(路径)
        服务.初始化()
        上传 = 服务.登记上传并创建任务(
            报告,
            股票代码="600372",
            报告年份=2025,
            企业简称="中航机载",
            报告标题="中航机载2025年度可持续发展报告",
            原始文件名=报告.name,
            请求幂等键="platform-e2e-r236",
            最大字节=50 * 1024 * 1024,
        )
        执行器 = 任务执行器(服务)
        领取编号 = 执行器._领取任务()
        if 领取编号 != 上传["job_id"]:
            raise RuntimeError("排队任务未被唯一领取")

        原缓存环境 = os.environ.get("ESG_PLATFORM_OCR_CACHE_DIR")
        原超时环境 = os.environ.get("ESG_PLATFORM_JOB_TIMEOUT_SECONDS")
        os.environ["ESG_PLATFORM_OCR_CACHE_DIR"] = str(缓存)
        os.environ["ESG_PLATFORM_JOB_TIMEOUT_SECONDS"] = "7200"
        开始 = time.perf_counter()
        try:
            执行器._运行单任务(领取编号)
        finally:
            if 原缓存环境 is None:
                os.environ.pop("ESG_PLATFORM_OCR_CACHE_DIR", None)
            else:
                os.environ["ESG_PLATFORM_OCR_CACHE_DIR"] = 原缓存环境
            if 原超时环境 is None:
                os.environ.pop("ESG_PLATFORM_JOB_TIMEOUT_SECONDS", None)
            else:
                os.environ["ESG_PLATFORM_JOB_TIMEOUT_SECONDS"] = 原超时环境
        耗时 = round(time.perf_counter() - 开始, 3)

        任务 = 服务.任务详情(领取编号)
        with 服务.读连接() as 连接:
            结果统计 = dict(连接.execute(
                """
                SELECT COUNT(*) AS result_rows,
                       COUNT(DISTINCT indicator_id) AS indicator_slots,
                       SUM(candidate_status='candidate_found') AS candidate_rows,
                       SUM(candidate_status='no_candidate') AS no_candidate_rows,
                       SUM(verification_status='auto_verified_high') AS auto_high_rows,
                       SUM(verification_status='auto_verified_medium') AS auto_medium_rows,
                       SUM(verification_status='needs_review') AS needs_review_rows,
                       SUM(verification_status='not_verified') AS not_verified_rows,
                       SUM(review_status<>'unreviewed') AS non_unreviewed_rows
                  FROM extraction_result WHERE job_id=?
                """,
                (领取编号,),
            ).fetchone())
            证据行数 = 连接.execute(
                """
                SELECT COUNT(*) FROM evidence_span
                 WHERE result_id IN (SELECT result_id FROM extraction_result WHERE job_id=?)
                """,
                (领取编号,),
            ).fetchone()[0]
            残留值行数 = 连接.execute(
                """
                SELECT COUNT(*) FROM extraction_result
                 WHERE job_id=? AND candidate_status='no_candidate'
                   AND (raw_value IS NOT NULL OR normalized_value IS NOT NULL
                        OR unit_raw IS NOT NULL OR unit_normalized IS NOT NULL)
                """,
                (领取编号,),
            ).fetchone()[0]
            数据库完整性 = 连接.execute("PRAGMA integrity_check").fetchone()[0]
            外键错误数 = len(连接.execute("PRAGMA foreign_key_check").fetchall())

        任务目录 = 路径.任务目录 / 领取编号
        计划文件 = next(任务目录.rglob("pipeline_plan_*.json"), None)
        最终CSV = None
        if 计划文件:
            计划 = json.loads(计划文件.read_text(encoding="utf-8-sig"))
            候选路径 = Path(str(计划.get("final_extraction_csv", "")))
            最终CSV = 候选路径 if 候选路径.is_file() else None
        证据 = {
            "verification_name": "平台单报告真实端到端链路验收",
            "generated_at": 当前时间(),
            "scope": {
                "report_count": 1,
                "stock_code": "600372",
                "report_year": 2025,
                "priority": "P0",
                "llm_api_enabled": False,
                "existing_ocr_cache_configured": True,
                "ocr_cache_hit_verified": False,
            },
            "upload_and_queue": {
                "blob_deduplicated": 上传["deduplication"]["blob"],
                "report_version_deduplicated": 上传["deduplication"]["report_version"],
                "job_deduplicated": 上传["deduplication"]["job"],
                "pdf_eof_ok": 上传["pdf_eof_ok"],
                "single_claim_verified": 领取编号 == 上传["job_id"],
            },
            "execution": {
                "job_status": 任务["status"],
                "job_stage": 任务["stage"],
                "pipeline_version": 任务["pipeline_version"],
                "wall_time_seconds": 耗时,
                "error_code": 任务["error_code"],
            },
            "database_import": {
                **结果统计,
                "evidence_rows": 证据行数,
                "no_candidate_rows_with_stale_values": 残留值行数,
                "database_integrity": 数据库完整性,
                "foreign_key_error_count": 外键错误数,
            },
            "artifact_hashes": {
                "pipeline_plan_sha256": 文件哈希(计划文件) if 计划文件 else None,
                "final_extraction_csv_sha256": 文件哈希(最终CSV) if 最终CSV else None,
            },
            "claim_boundary": [
                "本验收只证明新增平台已真实调用统一算法入口并完成结果、证据入库。",
                "本验收配置了既有OCR缓存目录，但未独立证明发生缓存命中；运行时间不得解释为性能提升。",
                "自动验收等级是机器证据质量状态，不等于人工金标准确率。",
                "所有结果的人工复核状态保持unreviewed。",
            ],
        }
        if 任务["status"] not in {"succeeded", "partial"}:
            日志文件 = 任务目录 / "流水线运行日志.txt"
            日志尾部 = 日志文件.read_text(encoding="utf-8", errors="replace")[-8000:] if 日志文件.is_file() else ""
            失败步骤 = []
            if 计划文件:
                for 步骤 in 计划.get("steps", []):
                    if 步骤.get("status") not in {"OK", "SKIPPED"}:
                        步骤日志路径 = Path(str(步骤.get("log") or ""))
                        失败步骤.append({
                            "name": 步骤.get("name"),
                            "required": 步骤.get("required"),
                            "status": 步骤.get("status"),
                            "return_code": 步骤.get("return_code"),
                            "log": 步骤.get("log"),
                            "log_tail": 步骤日志路径.read_text(
                                encoding="utf-8", errors="replace"
                            )[-8000:] if 步骤日志路径.is_file() else "",
                            "postconditions": 步骤.get("postconditions"),
                        })
            print(json.dumps({
                "job_status": 任务["status"],
                "error_code": 任务["error_code"],
                "error_message": 任务["error_message"],
                "failed_steps": 失败步骤,
                "log_tail": 日志尾部,
            }, ensure_ascii=True, indent=2))
            raise RuntimeError(f"真实平台任务失败：{任务['error_code']} {任务['error_message']}")
        if 结果统计["result_rows"] <= 0 or 证据行数 <= 0:
            raise RuntimeError("真实平台任务未写入有效结果或证据")
        if 残留值行数 or 结果统计["non_unreviewed_rows"] or 外键错误数:
            raise RuntimeError("平台结果导入违反保守事实边界")
        证据路径.write_text(json.dumps(证据, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(证据, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(主程序())
