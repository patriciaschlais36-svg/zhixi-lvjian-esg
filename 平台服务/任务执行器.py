from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from 数据服务 import 数据服务, 当前时间, 稳定编号, 项目根目录


算法入口 = 项目根目录 / "算法源码" / "运行ESG指标抽取.py"
公开流水线标识 = "智析绿鉴可信抽取引擎"

验收状态映射 = {
    "auto_verified_high": "auto_verified_high",
    "auto_verified_medium": "auto_verified_medium",
    "review_recommended": "needs_review",
    "high_risk_auto_review": "needs_review",
    "blocked_by_precision_gate": "needs_review",
    "not_extracted_needs_gold_or_recall_check": "not_verified",
}


def _有限小数(值: str | None) -> float | None:
    if 值 is None or not str(值).strip():
        return None
    try:
        数值 = float(str(值).replace(",", "").strip())
    except ValueError:
        return None
    return 数值 if math.isfinite(数值) else None


def _正整数(值: str | None) -> int | None:
    try:
        数值 = int(str(值).strip())
    except (TypeError, ValueError):
        return None
    return 数值 if 数值 >= 1 else None


def _规范原文(文本: str | None) -> str:
    return (文本 or "").replace("\r\n", "\n").replace("\r", "\n")


def _读取CSV(路径: Path) -> tuple[list[str], list[dict[str, str]]]:
    with 路径.open("r", encoding="utf-8-sig", newline="") as 文件:
        读取器 = csv.DictReader(文件)
        return list(读取器.fieldnames or []), list(读取器)


def _页数(PDF路径: Path) -> int | None:
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(PDF路径), strict=False).pages)
    except Exception:
        try:
            import pdfplumber

            with pdfplumber.open(PDF路径) as PDF:
                return len(PDF.pages)
        except Exception:
            return None


class 任务执行器:
    def __init__(self, 服务: 数据服务, 轮询秒数: float = 1.0) -> None:
        self.服务 = 服务
        self.轮询秒数 = 轮询秒数
        self._停止事件 = threading.Event()
        self._唤醒事件 = threading.Event()
        self._线程: threading.Thread | None = None

    @property
    def 运行中(self) -> bool:
        return bool(self._线程 and self._线程.is_alive() and not self._停止事件.is_set())

    def 启动(self) -> None:
        if self._线程 and self._线程.is_alive():
            return
        self._停止事件.clear()
        self._线程 = threading.Thread(target=self._工作循环, name="ESG真实抽取任务执行器", daemon=True)
        self._线程.start()

    def 停止(self, 等待秒数: float = 5.0) -> None:
        self._停止事件.set()
        self._唤醒事件.set()
        if self._线程:
            self._线程.join(timeout=等待秒数)

    def 通知有新任务(self) -> None:
        self._唤醒事件.set()

    def _工作循环(self) -> None:
        while not self._停止事件.is_set():
            任务编号 = self._领取任务()
            if 任务编号:
                self._运行单任务(任务编号)
                continue
            self._唤醒事件.wait(self.轮询秒数)
            self._唤醒事件.clear()

    def _领取任务(self) -> str | None:
        with self.服务.写连接() as 连接:
            行 = 连接.execute(
                "SELECT job_id FROM analysis_job WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not 行:
                return None
            任务编号 = 行["job_id"]
            时间 = 当前时间()
            修改数 = 连接.execute(
                """
                UPDATE analysis_job
                   SET status='running', stage='准备报告与算法参数', progress=2,
                       started_at=COALESCE(started_at, ?), updated_at=?
                 WHERE job_id=? AND status='queued'
                """,
                (时间, 时间, 任务编号),
            ).rowcount
            return 任务编号 if 修改数 == 1 else None

    def _任务上下文(self, 任务编号: str) -> dict[str, Any] | None:
        with self.服务.读连接() as 连接:
            行 = 连接.execute(
                """
                SELECT j.*, r.report_year, r.primary_report_type, c.stock_code,
                       c.current_short_name, rv.original_file_name
                  FROM analysis_job j
                  JOIN report_version rv ON rv.report_version_id=j.report_version_id
                  JOIN report r ON r.report_id=rv.report_id
                  JOIN company c ON c.company_id=r.company_id
                 WHERE j.job_id=?
                """,
                (任务编号,),
            ).fetchone()
        return dict(行) if 行 else None

    def _更新进度(self, 任务编号: str, 进度: int, 阶段: str) -> None:
        with self.服务.写连接() as 连接:
            连接.execute(
                "UPDATE analysis_job SET progress=?, stage=?, updated_at=? WHERE job_id=? AND status='running'",
                (进度, 阶段, 当前时间(), 任务编号),
            )

    def _结束任务(
        self, 任务编号: str, 状态: str, 阶段: str, *,
        错误代码: str | None = None, 错误信息: str | None = None,
        日志摘要: str | None = None, 流水线版本: str | None = None,
    ) -> None:
        with self.服务.写连接() as 连接:
            连接.execute(
                """
                UPDATE analysis_job
                   SET status=?, stage=?, progress=100, error_code=?, error_message=?,
                       log_summary=?, pipeline_version=COALESCE(?, pipeline_version),
                       finished_at=?, updated_at=?
                 WHERE job_id=?
                """,
                (
                    状态, 阶段, 错误代码, 错误信息, 日志摘要,
                    流水线版本, 当前时间(), 当前时间(), 任务编号,
                ),
            )

    def _运行单任务(self, 任务编号: str) -> None:
        上下文 = self._任务上下文(任务编号)
        if not 上下文:
            return
        PDF路径 = self.服务.解析报告文件(上下文["report_version_id"])
        if not PDF路径:
            self._结束任务(
                任务编号, "failed", "未找到可读取的报告文件",
                错误代码="REPORT_FILE_UNAVAILABLE",
                错误信息="报告文件根目录未配置或文件已不可用。",
            )
            return

        输出目录 = (self.服务.路径.任务目录 / 任务编号).resolve()
        输出目录.mkdir(parents=True, exist_ok=True)
        日志路径 = 输出目录 / "流水线运行日志.txt"
        OCR缓存 = Path(
            os.environ.get("ESG_PLATFORM_OCR_CACHE_DIR", self.服务.路径.运行目录 / "OCR缓存")
        ).resolve()
        OCR缓存.mkdir(parents=True, exist_ok=True)
        命令 = [
            sys.executable, str(算法入口),
            "--报告", str(PDF路径),
            "--股票代码", 上下文["stock_code"],
            "--公司简称", 上下文["current_short_name"],
            "--报告年度", str(上下文["report_year"]),
            "--报告类型", 上下文["primary_report_type"],
            "--输出目录", str(输出目录),
            "--OCR缓存目录", str(OCR缓存),
            "--优先级", "P0",
            "--运行编号", 上下文["run_id"],
            "--执行",
        ]
        self._更新进度(任务编号, 5, "真实抽取流水线运行中")
        超时文本 = os.environ.get("ESG_PLATFORM_JOB_TIMEOUT_SECONDS", "").strip()
        超时秒数 = int(超时文本 or "7200")
        try:
            完成 = subprocess.run(
                命令,
                cwd=str(项目根目录),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=超时秒数,
                check=False,
            )
            日志路径.write_text(完成.stdout or "", encoding="utf-8")
        except subprocess.TimeoutExpired as 异常:
            日志 = (异常.stdout or "") if isinstance(异常.stdout, str) else ""
            日志路径.write_text(日志, encoding="utf-8")
            self._结束任务(
                任务编号, "failed", "抽取任务超时",
                错误代码="PIPELINE_TIMEOUT",
                错误信息=f"真实抽取流水线超过{超时秒数}秒未完成。",
                日志摘要=日志[-4000:],
            )
            return
        except OSError as 异常:
            self._结束任务(
                任务编号, "failed", "无法启动真实抽取流水线",
                错误代码="PIPELINE_START_FAILED", 错误信息=str(异常),
            )
            return

        if 完成.returncode != 0:
            self._结束任务(
                任务编号, "failed", "真实抽取流水线执行失败",
                错误代码="PIPELINE_NONZERO_EXIT",
                错误信息=f"算法进程退出码为{完成.returncode}。",
                日志摘要=(完成.stdout or "")[-4000:],
            )
            return

        self._更新进度(任务编号, 90, "校验并导入结构化结果")
        try:
            导入 = self.导入任务产物(任务编号, 输出目录, PDF路径)
        except Exception as 异常:
            self._结束任务(
                任务编号, "failed", "算法产物未通过导入校验",
                错误代码="ARTIFACT_IMPORT_FAILED", 错误信息=str(异常),
                日志摘要=(完成.stdout or "")[-4000:],
            )
            return

        状态 = "partial" if 导入["partial_reasons"] else "succeeded"
        阶段 = "分析完成，部分结果需复核" if 状态 == "partial" else "分析完成"
        self._结束任务(
            任务编号, 状态, 阶段,
            错误代码="PARTIAL_RESULT" if 状态 == "partial" else None,
            错误信息="；".join(导入["partial_reasons"]) if 状态 == "partial" else None,
            日志摘要=(完成.stdout or "")[-4000:],
            流水线版本=导入["pipeline_version"],
        )

    def 导入任务产物(self, 任务编号: str, 输出目录: Path, PDF路径: Path) -> dict[str, Any]:
        上下文 = self._任务上下文(任务编号)
        if not 上下文:
            raise ValueError("任务不存在")
        输出目录 = 输出目录.resolve()
        计划文件 = sorted(输出目录.rglob("pipeline_plan_*.json"), key=lambda x: x.stat().st_mtime_ns)
        if not 计划文件:
            raise ValueError("缺少流水线执行计划JSON")
        计划 = json.loads(计划文件[-1].read_text(encoding="utf-8-sig"))
        if 计划.get("execute") is not True or 计划.get("execution_success") is not True:
            raise ValueError("执行计划未证明真实流水线成功完成")
        必需失败 = [
            步骤.get("name", "unknown") for 步骤 in 计划.get("steps", [])
            if 步骤.get("required") and 步骤.get("status") != "OK"
        ]
        if 必需失败:
            raise ValueError(f"必需步骤失败：{','.join(必需失败)}")

        最终CSV = Path(str(计划.get("final_extraction_csv", ""))).resolve()
        try:
            最终CSV.relative_to(输出目录)
        except ValueError as 异常:
            raise ValueError("最终结果CSV不在任务输出目录内") from 异常
        if not 最终CSV.is_file():
            raise ValueError("缺少最终抽取CSV")

        表头, 行列表 = _读取CSV(最终CSV)
        必需列 = {
            "sample_id", "stock_code", "field_id", "candidate_status", "candidate_rank",
            "metric_type", "value_candidate", "value_standardized_candidate",
            "unit_raw_candidate", "unit_standardized_candidate", "confidence_rule",
            "source_physical_page", "source_report_page_candidates", "source_text",
            "source_table_cell", "evidence_type_candidate", "extractor_version",
        }
        缺列 = sorted(必需列 - set(表头))
        if 缺列:
            raise ValueError(f"最终CSV缺少字段：{','.join(缺列)}")
        if not 行列表:
            raise ValueError("最终抽取CSV为空")

        样本编号 = {行.get("sample_id", "") for 行 in 行列表}
        股票代码 = {行.get("stock_code", "") for 行 in 行列表}
        if len(样本编号) != 1 or 股票代码 != {上下文["stock_code"]}:
            raise ValueError("抽取结果样本身份与任务报告不一致")

        自动文件 = sorted(输出目录.rglob("auto_verified_extraction_results_v1.0.csv"))
        自动映射: dict[tuple[str, str, int], dict[str, str]] = {}
        部分原因: list[str] = []
        if 自动文件:
            _, 自动行 = _读取CSV(自动文件[-1])
            for 行 in 自动行:
                排名 = _正整数(行.get("candidate_rank"))
                if 排名:
                    自动映射[(行.get("sample_id", ""), 行.get("field_id", ""), 排名)] = 行
        else:
            部分原因.append("自动验收产物缺失，结果仅标记为未验收")

        with self.服务.读连接() as 连接:
            指标目录 = {
                行["indicator_id"]: dict(行)
                for 行 in 连接.execute("SELECT * FROM indicator_catalog")
            }
        最大页数 = _页数(PDF路径)
        if 最大页数 is None:
            部分原因.append("无法确认PDF总页数，证据页码未建立可点击跳转")

        待写结果: list[dict[str, Any]] = []
        已见键: set[tuple[str, str, int]] = set()
        for 行号, 行 in enumerate(行列表, start=2):
            指标编号 = (行.get("field_id") or "").strip()
            if 指标编号 not in 指标目录:
                raise ValueError(f"第{行号}行包含未知指标：{指标编号}")
            排名 = _正整数(行.get("candidate_rank"))
            if not 排名:
                raise ValueError(f"第{行号}行候选排名非法")
            键 = (行.get("sample_id", ""), 指标编号, 排名)
            if 键 in 已见键:
                raise ValueError(f"最终CSV存在重复结果键：{键}")
            已见键.add(键)

            候选状态 = (行.get("candidate_status") or "").strip()
            if 候选状态 not in {"candidate_found", "no_candidate", "not_applicable", "needs_review"}:
                raise ValueError(f"第{行号}行候选状态非法：{候选状态}")
            是有效候选 = 候选状态 in {"candidate_found", "needs_review"}
            原始值 = (行.get("value_candidate") or "").strip() or None if 是有效候选 else None
            原始单位 = (行.get("unit_raw_candidate") or "").strip() or None if 是有效候选 else None
            标准单位 = (行.get("unit_standardized_candidate") or "").strip() or None if 是有效候选 else None
            标准数值 = None
            if 是有效候选 and 指标目录[指标编号]["metric_type"] == "quantitative":
                标准数值 = _有限小数(行.get("value_standardized_candidate"))
                if (行.get("value_standardized_candidate") or "").strip() and 标准数值 is None:
                    部分原因.append(f"{指标编号}候选{排名}的标准化数值非法")

            置信度 = _有限小数(行.get("confidence_rule"))
            if 置信度 is not None and not 0 <= 置信度 <= 1:
                置信度 = None
                部分原因.append(f"{指标编号}候选{排名}的置信度超出范围")

            自动项 = 自动映射.get(键)
            自动原状态 = (自动项 or {}).get("auto_verification_status", "")
            验收状态 = 验收状态映射.get(自动原状态, "not_verified")
            if 自动项 is None:
                部分原因.append(f"{指标编号}候选{排名}缺少自动验收键")

            原文 = _规范原文(行.get("source_text"))
            表格单元 = _规范原文(行.get("source_table_cell"))
            物理页 = _正整数(行.get("source_physical_page"))
            页码有效 = bool(物理页 and 最大页数 and 物理页 <= 最大页数)
            证据: list[dict[str, Any]] = []
            if 是有效候选 and 原文.strip() and 页码有效:
                证据.append({
                    "page_no": 物理页,
                    "printed_page_label": (行.get("source_report_page_candidates") or "").strip() or None,
                    "source_text": 原文,
                    "evidence_type": (行.get("evidence_type_candidate") or "native_text").strip(),
                })
                if 表格单元.strip() and 表格单元 != 原文:
                    证据.append({
                        "page_no": 物理页,
                        "printed_page_label": (行.get("source_report_page_candidates") or "").strip() or None,
                        "source_text": 表格单元,
                        "evidence_type": "native_table_cell",
                    })
            elif 是有效候选:
                验收状态 = "needs_review"
                部分原因.append(f"{指标编号}候选{排名}缺少有效物理页或证据原文")

            内部抽取标识 = (行.get("extractor_version") or "unknown").strip()
            待写结果.append({
                "result_id": 稳定编号("result", f"{任务编号}|{指标编号}|{排名}"),
                "indicator_id": 指标编号,
                "candidate_rank": 排名,
                "candidate_status": 候选状态,
                "raw_value": 原始值,
                "normalized_value": 标准数值,
                "unit_raw": 原始单位,
                "unit_normalized": 标准单位,
                "confidence": 置信度,
                "verification_status": 验收状态,
                "pipeline_version": 公开流水线标识,
                "indicator_source_version": 指标目录[指标编号]["source_version"],
                "evidence": 证据,
                "verification_metadata": {
                    "original_status": 自动原状态 or None,
                    "score": (自动项 or {}).get("auto_verification_score") or None,
                    "issues": (自动项 or {}).get("auto_verification_issues") or None,
                    "rule_ids": (自动项 or {}).get("auto_verification_rule_ids") or None,
                    "verification_layer_version": (自动项 or {}).get("verification_layer_version") or None,
                    "internal_extractor_id": 内部抽取标识,
                },
            })

        时间 = 当前时间()
        with self.服务.写连接() as 连接:
            连接.execute(
                "DELETE FROM evidence_span WHERE result_id IN (SELECT result_id FROM extraction_result WHERE job_id=?)",
                (任务编号,),
            )
            连接.execute("DELETE FROM extraction_result WHERE job_id=?", (任务编号,))
            for 结果 in 待写结果:
                连接.execute(
                    """
                    INSERT INTO extraction_result(
                        result_id, job_id, report_version_id, indicator_id, report_year,
                        candidate_rank, candidate_status, raw_value, normalized_value,
                        unit_raw, unit_normalized, confidence, verification_status,
                        review_status, source_kind, pipeline_version,
                        indicator_source_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unreviewed',
                              'live_pipeline', ?, ?, ?)
                    """,
                    (
                        结果["result_id"], 任务编号, 上下文["report_version_id"],
                        结果["indicator_id"], 上下文["report_year"], 结果["candidate_rank"],
                        结果["candidate_status"], 结果["raw_value"], 结果["normalized_value"],
                        结果["unit_raw"], 结果["unit_normalized"], 结果["confidence"],
                        结果["verification_status"], 结果["pipeline_version"],
                        结果["indicator_source_version"], 时间,
                    ),
                )
                for 证据序号, 证据 in enumerate(结果["evidence"], start=1):
                    原文哈希 = hashlib.sha256(证据["source_text"].encode("utf-8")).hexdigest()
                    连接.execute(
                        """
                        INSERT INTO evidence_span(
                            evidence_id, result_id, report_version_id, page_no,
                            printed_page_label, source_text, evidence_type, bbox_json,
                            source_text_sha256, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                        """,
                        (
                            稳定编号("evidence", f"{结果['result_id']}|{证据序号}|{原文哈希}"),
                            结果["result_id"], 上下文["report_version_id"], 证据["page_no"],
                            证据["printed_page_label"], 证据["source_text"],
                            证据["evidence_type"], 原文哈希, 时间,
                        ),
                    )
                连接.execute(
                    "INSERT OR REPLACE INTO platform_event VALUES (?, 'verification_metadata', 'extraction_result', ?, ?, ?)",
                    (
                        稳定编号("event", f"verification_metadata|{结果['result_id']}"), 结果["result_id"],
                        json.dumps(结果["verification_metadata"], ensure_ascii=False, separators=(",", ":")),
                        时间,
                    ),
                )

        return {
            "result_count": len(待写结果),
            "evidence_count": sum(len(x["evidence"]) for x in 待写结果),
            "partial_reasons": sorted(set(部分原因)),
            "pipeline_version": 公开流水线标识,
        }
