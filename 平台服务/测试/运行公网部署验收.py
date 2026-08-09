#!/usr/bin/env python3
"""对已部署的智析绿鉴实例执行只读公网验收。"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def 请求(基础地址: str, 路径: str) -> dict[str, Any]:
    地址 = f"{基础地址.rstrip('/')}{路径}"
    开始 = time.perf_counter()
    try:
        with urllib.request.urlopen(地址, timeout=60) as 响应:
            内容 = 响应.read()
            状态码 = 响应.status
            响应头 = dict(响应.headers.items())
    except urllib.error.HTTPError as 异常:
        内容 = 异常.read()
        状态码 = 异常.code
        响应头 = dict(异常.headers.items())
    耗时毫秒 = round((time.perf_counter() - 开始) * 1000, 3)
    文本 = 内容.decode("utf-8", errors="replace")
    try:
        数据 = json.loads(文本)
    except json.JSONDecodeError:
        数据 = None
    return {
        "url": 地址,
        "status": 状态码,
        "elapsed_ms": 耗时毫秒,
        "headers": 响应头,
        "text": 文本,
        "json": 数据,
    }


def 主函数() -> int:
    解析器 = argparse.ArgumentParser(description="执行智析绿鉴公网部署只读验收")
    解析器.add_argument("基础地址", help="例如 https://example.onrender.com")
    解析器.add_argument("--输出", type=Path, help="可选的JSON输出路径")
    参数 = 解析器.parse_args()

    结果: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": 参数.基础地址.rstrip("/"),
        "scope": "公网功能与接口可用性验收，不是指标抽取准确率评估",
        "checks": [],
    }

    def 检查(名称: str, 条件: bool, 详情: Any) -> None:
        结果["checks"].append({"name": 名称, "passed": bool(条件), "details": 详情})

    首页 = 请求(参数.基础地址, "/")
    检查("首页可访问", 首页["status"] == 200 and "智析绿鉴" in 首页["text"], {
        "status": 首页["status"], "elapsed_ms": 首页["elapsed_ms"]
    })
    小写响应头 = {键.lower(): 值 for 键, 值 in 首页["headers"].items()}
    检查("基础安全响应头", all(键 in 小写响应头 for 键 in (
        "x-content-type-options", "x-frame-options", "referrer-policy", "permissions-policy"
    )), {键: 小写响应头.get(键) for 键 in (
        "x-content-type-options", "x-frame-options", "referrer-policy", "permissions-policy"
    )})

    接口结果 = {}
    for 名称, 路径 in (
        ("健康检查", "/api/v1/health"),
        ("就绪检查", "/api/v1/readiness"),
        ("平台概览", "/api/v1/summary"),
        ("企业列表", "/api/v1/companies?page_size=5"),
        ("报告列表", "/api/v1/reports?page_size=5"),
        ("指标目录", "/api/v1/indicators"),
        ("任务列表", "/api/v1/jobs?limit=5"),
        ("全局检索", "/api/v1/search?q=" + urllib.parse.quote("温室气体")),
        ("候选结果", "/api/v1/results?candidate_only=true"),
        ("结果导出", "/api/v1/exports/results.csv"),
    ):
        响应 = 请求(参数.基础地址, 路径)
        接口结果[名称] = 响应
        检查(名称, 响应["status"] == 200, {
            "status": 响应["status"], "elapsed_ms": 响应["elapsed_ms"]
        })

    概览 = 接口结果["平台概览"]["json"]["data"]
    检查("公开演示数据规模", all(概览.get(键, 0) > 0 for 键 in (
        "company_count", "report_count", "indicator_count", "result_count", "evidence_count"
    )), 概览)

    候选 = 接口结果["候选结果"]["json"]["data"]
    首条结果 = 候选[0]
    证据 = 请求(参数.基础地址, 首条结果["evidence_url"])
    检查("结果证据链", 证据["status"] == 200 and len(证据["json"]["data"]) > 0, {
        "status": 证据["status"], "evidence_count": len(证据["json"]["data"])
    })

    报告 = 请求(参数.基础地址, f"/api/v1/reports/{首条结果['report_version_id']}")
    报告数据 = 报告["json"]["data"]
    趋势参数 = urllib.parse.urlencode({
        "company_id": 报告数据["company_id"],
        "indicator_id": 首条结果["indicator_id"],
    })
    趋势 = 请求(参数.基础地址, f"/api/v1/trends?{趋势参数}")
    趋势数据 = 趋势["json"]["data"]
    检查("跨年度趋势", 趋势["status"] == 200, {
        "status": 趋势["status"],
        "point_count": len(趋势数据.get("points", [])),
        "comparable": 趋势数据.get("comparable"),
    })

    企业 = 接口结果["企业列表"]["json"]["data"]
    对比参数 = [("company_id", 项["company_id"]) for 项 in 企业[:2]] + [
        ("indicator_id", 首条结果["indicator_id"]),
        ("year", str(首条结果["report_year"])),
    ]
    对比 = 请求(参数.基础地址, "/api/v1/compare?" + urllib.parse.urlencode(对比参数))
    检查("企业对比", 对比["status"] == 200, {
        "status": 对比["status"],
        "comparison_basis": 对比["json"]["data"].get("comparison_basis"),
    })

    异常参数 = 请求(参数.基础地址, "/api/v1/indicators?dimension=X")
    检查("异常参数拒绝", 异常参数["status"] == 400, {"status": 异常参数["status"]})

    通过数 = sum(1 for 项 in 结果["checks"] if 项["passed"])
    结果["summary"] = {
        "passed": 通过数,
        "total": len(结果["checks"]),
        "all_passed": 通过数 == len(结果["checks"]),
    }

    输出文本 = json.dumps(结果, ensure_ascii=False, indent=2)
    print(输出文本)
    if 参数.输出:
        参数.输出.parent.mkdir(parents=True, exist_ok=True)
        参数.输出.write_text(输出文本 + "\n", encoding="utf-8")
    return 0 if 结果["summary"]["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(主函数())
