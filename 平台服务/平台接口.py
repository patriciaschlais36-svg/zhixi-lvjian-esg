from __future__ import annotations

import atexit
import csv
import io
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from flask import Flask, Response, g, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from 数据服务 import 数据服务, 当前时间, 平台路径, 项目根目录
from 任务执行器 import 任务执行器


服务版本 = "1.0.0"


def _整数参数(名称: str, 默认值: int, 最小值: int, 最大值: int) -> int:
    文本 = request.args.get(名称, str(默认值))
    try:
        值 = int(文本)
    except ValueError as 异常:
        raise ValueError(f"参数{名称}必须为整数") from 异常
    if not 最小值 <= 值 <= 最大值:
        raise ValueError(f"参数{名称}必须在{最小值}至{最大值}之间")
    return 值


def 创建平台应用(
    路径: 平台路径 | None = None, *, 启动任务执行器: bool = True,
) -> Flask:
    前端目录 = 项目根目录 / "前端界面"
    应用 = Flask(
        __name__,
        static_folder=str(前端目录) if 前端目录.is_dir() else None,
        static_url_path="",
    )
    上传上限文本 = os.environ.get("ESG_PLATFORM_MAX_UPLOAD_BYTES", "").strip()
    应用.config["MAX_CONTENT_LENGTH"] = int(上传上限文本 or str(30 * 1024 * 1024))
    应用.config["JSON_AS_ASCII"] = False

    服务 = 数据服务(路径)
    服务.初始化()
    执行器 = 任务执行器(服务)
    if 启动任务执行器:
        执行器.启动()
        atexit.register(执行器.停止)
    应用.extensions["esg_data_service"] = 服务
    应用.extensions["esg_job_runner"] = 执行器

    def 成功(数据: Any, 状态码: int = 200, **元数据: Any) -> tuple[Response, int]:
        return jsonify({
            "data": 数据,
            "meta": {
                "request_id": g.request_id,
                "generated_at": 当前时间(),
                **元数据,
            },
        }), 状态码

    def 错误(代码: str, 消息: str, 状态码: int, 详情: Any = None) -> tuple[Response, int]:
        载荷: dict[str, Any] = {
            "error": {"code": 代码, "message": 消息},
            "meta": {"request_id": g.request_id, "generated_at": 当前时间()},
        }
        if 详情 is not None:
            载荷["error"]["details"] = 详情
        return jsonify(载荷), 状态码

    @应用.before_request
    def 建立请求编号() -> None:
        外部编号 = request.headers.get("X-Request-ID", "").strip()
        g.request_id = 外部编号[:100] if 外部编号 else uuid.uuid4().hex

    @应用.after_request
    def 增加安全响应头(响应: Response) -> Response:
        响应.headers["X-Content-Type-Options"] = "nosniff"
        响应.headers["X-Frame-Options"] = "SAMEORIGIN"
        响应.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        响应.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        允许来源 = {
            x.strip() for x in os.environ.get("ESG_ALLOWED_ORIGINS", "").split(",") if x.strip()
        }
        来源 = request.headers.get("Origin")
        if 来源 and 来源 in 允许来源:
            响应.headers["Access-Control-Allow-Origin"] = 来源
            响应.headers["Vary"] = "Origin"
        return 响应

    @应用.errorhandler(ValueError)
    def 处理参数错误(异常: ValueError) -> tuple[Response, int]:
        return 错误("INVALID_ARGUMENT", str(异常), 400)

    @应用.errorhandler(RequestEntityTooLarge)
    def 处理超大文件(_: RequestEntityTooLarge) -> tuple[Response, int]:
        return 错误("UPLOAD_TOO_LARGE", "上传文件超过平台允许的大小。", 413)

    @应用.errorhandler(HTTPException)
    def 处理HTTP异常(异常: HTTPException) -> tuple[Response, int]:
        return 错误(f"HTTP_{异常.code}", 异常.description, int(异常.code or 500))

    @应用.errorhandler(Exception)
    def 处理内部异常(异常: Exception) -> tuple[Response, int]:
        应用.logger.exception("请求处理失败：%s", 异常)
        return 错误("INTERNAL_ERROR", "服务处理失败，请使用请求编号查询运行日志。", 500)

    @应用.get("/")
    def 平台首页() -> Response | tuple[Response, int]:
        if 前端目录.is_dir() and (前端目录 / "index.html").is_file():
            return send_from_directory(前端目录, "index.html")
        return 错误("FRONTEND_UNAVAILABLE", "前端界面尚未构建。", 503)

    @应用.get("/api/v1")
    def 接口首页() -> tuple[Response, int]:
        return 成功({
            "name": "ESG智能数据提取与分析",
            "work": "智析绿鉴—面向上市公司ESG报告的多模态指标抽取与可信分析系统",
            "api": "/api/v1",
            "service_version": 服务版本,
        })

    @应用.get("/api/v1/health")
    def 健康检查() -> tuple[Response, int]:
        return 成功({"status": "ok", "service_version": 服务版本, "time": 当前时间()})

    @应用.get("/api/v1/readiness")
    def 就绪检查() -> tuple[Response, int]:
        检查 = 服务.就绪状态()
        检查["runner"] = {
            "enabled": 启动任务执行器,
            "running": 执行器.运行中 if 启动任务执行器 else False,
        }
        检查["storage"] = {
            "runtime_writable": os.access(服务.路径.运行目录, os.W_OK),
            "upload_writable": os.access(服务.路径.上传目录, os.W_OK),
        }
        检查["ready"] = bool(
            检查["ready"] and 检查["storage"]["runtime_writable"]
            and 检查["storage"]["upload_writable"]
            and (not 启动任务执行器 or 执行器.运行中)
        )
        return 成功(检查, 200 if 检查["ready"] else 503)

    @应用.get("/api/v1/summary")
    def 平台概览() -> tuple[Response, int]:
        return 成功(服务.概览())

    @应用.get("/api/v1/companies")
    def 查询企业() -> tuple[Response, int]:
        数据 = 服务.企业列表(
            关键词=request.args.get("q", "").strip(),
            页码=_整数参数("page", 1, 1, 100000),
            每页=_整数参数("page_size", 20, 1, 100),
        )
        return 成功(数据["items"], page=数据["page"], page_size=数据["page_size"], total=数据["total"])

    @应用.get("/api/v1/companies/<company_id>")
    def 查询企业详情(company_id: str) -> tuple[Response, int]:
        数据 = 服务.企业详情(company_id)
        return 成功(数据) if 数据 else 错误("COMPANY_NOT_FOUND", "未找到企业。", 404)

    @应用.get("/api/v1/reports")
    def 查询报告() -> tuple[Response, int]:
        年份文本 = request.args.get("year", "").strip()
        年份 = int(年份文本) if 年份文本 else None
        数据 = 服务.报告列表(
            企业编号=request.args.get("company_id") or None,
            年份=年份,
            关键词=request.args.get("q", "").strip(),
            页码=_整数参数("page", 1, 1, 100000),
            每页=_整数参数("page_size", 20, 1, 100),
        )
        return 成功(数据["items"], page=数据["page"], page_size=数据["page_size"], total=数据["total"])

    @应用.get("/api/v1/reports/<report_version_id>")
    def 查询报告详情(report_version_id: str) -> tuple[Response, int]:
        数据 = 服务.报告详情(report_version_id)
        return 成功(数据) if 数据 else 错误("REPORT_NOT_FOUND", "未找到报告版本。", 404)

    @应用.get("/api/v1/reports/<report_version_id>/file")
    def 获取报告文件(report_version_id: str) -> Response | tuple[Response, int]:
        数据 = 服务.报告详情(report_version_id)
        if not 数据:
            return 错误("REPORT_NOT_FOUND", "未找到报告版本。", 404)
        路径对象 = 服务.解析报告文件(report_version_id)
        if not 路径对象:
            return 错误("REPORT_FILE_UNAVAILABLE", "报告文件尚未挂载到当前服务。", 404)
        响应 = send_file(
            路径对象,
            mimetype="application/pdf",
            as_attachment=False,
            download_name=数据["original_file_name"],
            conditional=True,
            etag=数据["sha256"],
            max_age=3600,
        )
        响应.headers["Content-Disposition"] = (
            f"inline; filename=report.pdf; filename*=UTF-8''{quote(数据['original_file_name'])}"
        )
        return 响应

    @应用.post("/api/v1/reports")
    def 上传报告() -> tuple[Response, int]:
        上传文件 = request.files.get("file")
        if not 上传文件 or not 上传文件.filename:
            raise ValueError("必须上传PDF报告文件")
        原始文件名 = Path(上传文件.filename.replace("\\", "/")).name.strip()[:240]
        if not 原始文件名 or Path(原始文件名).suffix.lower() != ".pdf":
            raise ValueError("仅支持PDF文件")
        股票代码 = request.form.get("stock_code", "").strip()
        企业简称 = request.form.get("company_name", "").strip()
        报告标题 = request.form.get("report_title", "").strip()
        年份文本 = request.form.get("report_year", "").strip()
        try:
            报告年份 = int(年份文本)
        except ValueError as 异常:
            raise ValueError("报告年份必须为整数") from 异常

        暂存目录 = 服务.路径.运行目录 / "上传暂存"
        暂存目录.mkdir(parents=True, exist_ok=True)
        文件描述符, 暂存名称 = tempfile.mkstemp(prefix="upload_", suffix=".pdf", dir=暂存目录)
        os.close(文件描述符)
        暂存路径 = Path(暂存名称)
        try:
            上传文件.save(暂存路径)
            数据 = 服务.登记上传并创建任务(
                暂存路径,
                股票代码=股票代码,
                报告年份=报告年份,
                企业简称=企业简称,
                报告标题=报告标题,
                原始文件名=原始文件名,
                请求幂等键=request.headers.get("Idempotency-Key") or None,
                最大字节=应用.config["MAX_CONTENT_LENGTH"],
            )
        finally:
            暂存路径.unlink(missing_ok=True)
        执行器.通知有新任务()
        数据["status"] = 服务.任务详情(数据["job_id"])["status"]
        数据["links"] = {
            "job": f"/api/v1/jobs/{数据['job_id']}",
            "report": f"/api/v1/reports/{数据['report_version_id']}",
        }
        return 成功(数据, 202)

    @应用.get("/api/v1/jobs")
    def 查询任务() -> tuple[Response, int]:
        return 成功(服务.任务列表(request.args.get("status") or None, _整数参数("limit", 50, 1, 200)))

    @应用.get("/api/v1/jobs/<job_id>")
    def 查询任务详情(job_id: str) -> tuple[Response, int]:
        数据 = 服务.任务详情(job_id)
        return 成功(数据) if 数据 else 错误("JOB_NOT_FOUND", "未找到分析任务。", 404)

    @应用.get("/api/v1/indicators")
    def 查询指标() -> tuple[Response, int]:
        维度 = request.args.get("dimension") or None
        优先级 = request.args.get("priority") or None
        if 维度 and 维度 not in {"E", "S", "G"}:
            raise ValueError("dimension只能为E、S或G")
        if 优先级 and 优先级 not in {"P0", "P1", "P2"}:
            raise ValueError("priority只能为P0、P1或P2")
        return 成功(服务.指标列表(维度, 优先级))

    @应用.get("/api/v1/results")
    def 查询结果() -> tuple[Response, int]:
        数据 = 服务.结果列表(
            任务编号=request.args.get("job_id") or None,
            报告版本编号=request.args.get("report_version_id") or None,
            指标编号=request.args.get("indicator_id") or None,
            仅候选=request.args.get("candidate_only", "false").lower() in {"1", "true", "yes"},
        )
        for 项 in 数据:
            项["evidence_url"] = f"/api/v1/results/{项['result_id']}/evidence"
        return 成功(数据)

    @应用.get("/api/v1/results/<result_id>/evidence")
    def 查询结果证据(result_id: str) -> tuple[Response, int]:
        return 成功(服务.证据列表(result_id))

    @应用.get("/api/v1/evidence/<evidence_id>")
    def 查询证据详情(evidence_id: str) -> tuple[Response, int]:
        数据 = 服务.证据详情(evidence_id)
        if not 数据:
            return 错误("EVIDENCE_NOT_FOUND", "未找到证据。", 404)
        数据["pdf_url"] = f"/api/v1/reports/{数据['report_version_id']}/file#page={数据['page_no']}"
        return 成功(数据)

    @应用.get("/api/v1/trends")
    def 查询趋势() -> tuple[Response, int]:
        企业编号 = request.args.get("company_id", "").strip()
        指标编号 = request.args.get("indicator_id", "").strip()
        if not 企业编号 or not 指标编号:
            raise ValueError("company_id和indicator_id不能为空")
        return 成功(服务.趋势(企业编号, 指标编号))

    @应用.get("/api/v1/compare")
    def 查询对比() -> tuple[Response, int]:
        企业编号列表 = request.args.getlist("company_id")
        指标编号 = request.args.get("indicator_id", "").strip()
        年份 = _整数参数("year", 2025, 2000, 2100)
        if not 指标编号:
            raise ValueError("indicator_id不能为空")
        数据 = 服务.对比(企业编号列表, 指标编号, 年份)
        数据["comparison_basis"] = "用户选定企业、同报告年度、同标准化单位；未使用行业归属推断"
        return 成功(数据)

    @应用.get("/api/v1/search")
    def 全局搜索() -> tuple[Response, int]:
        关键词 = request.args.get("q", "").strip()
        if len(关键词) > 100:
            raise ValueError("搜索词不能超过100字符")
        return 成功(服务.搜索(关键词, _整数参数("limit", 20, 1, 100)))

    @应用.get("/api/v1/exports/results.csv")
    def 导出结果() -> Response:
        数据 = 服务.结果列表(
            报告版本编号=request.args.get("report_version_id") or None,
            指标编号=request.args.get("indicator_id") or None,
        )
        字段 = [
            "result_id", "report_version_id", "indicator_id", "metric_name_cn",
            "report_year", "candidate_rank", "candidate_status", "raw_value",
            "normalized_value", "unit_raw", "unit_normalized", "confidence",
            "verification_status", "review_status", "source_kind", "pipeline_version",
        ]
        缓冲 = io.StringIO(newline="")
        缓冲.write("\ufeff")
        写入器 = csv.DictWriter(缓冲, fieldnames=字段, extrasaction="ignore")
        写入器.writeheader()
        写入器.writerows(数据)
        return Response(
            缓冲.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=ESG_extraction_results.csv"},
        )

    return 应用


if __name__ == "__main__":
    from waitress import serve

    主机 = os.environ.get("ESG_PLATFORM_HOST", "").strip() or "127.0.0.1"
    端口 = int(os.environ.get("ESG_PLATFORM_PORT", "").strip() or "8000")
    serve(创建平台应用(), host=主机, port=端口, threads=8)
