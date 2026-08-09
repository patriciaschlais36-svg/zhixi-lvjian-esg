#!/usr/bin/env python3
"""上传明确标记的非评估PDF并验收公网任务链路。"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests


def 主函数() -> int:
    解析器 = argparse.ArgumentParser(description="验收智析绿鉴公网上传与真实任务链路")
    解析器.add_argument("基础地址")
    解析器.add_argument("测试PDF", type=Path)
    解析器.add_argument("--输出", type=Path)
    解析器.add_argument("--等待秒数", type=int, default=300)
    参数 = 解析器.parse_args()

    if not 参数.测试PDF.is_file():
        raise FileNotFoundError(参数.测试PDF)

    基础地址 = 参数.基础地址.rstrip("/")
    开始 = time.perf_counter()
    with 参数.测试PDF.open("rb") as 文件:
        响应 = requests.post(
            f"{基础地址}/api/v1/reports",
            files={"file": (参数.测试PDF.name, 文件, "application/pdf")},
            data={
                "stock_code": "999999",
                "company_name": "Online Smoke Test Company",
                "report_year": "2026",
                "report_type": "ESG",
                "report_title": "Online upload smoke test - non-evaluation sample",
            },
            headers={"Idempotency-Key": f"public-smoke-{uuid.uuid4().hex}"},
            timeout=90,
        )
    上传耗时 = round((time.perf_counter() - 开始) * 1000, 3)
    上传数据 = 响应.json()
    记录 = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": 基础地址,
        "scope": "非评估样本的公网上传、任务执行与结果入库功能验收，不参与准确率计算",
        "upload": {"status": 响应.status_code, "elapsed_ms": 上传耗时},
        "job": None,
        "result_count": None,
        "evidence_count": None,
    }

    if 响应.status_code != 202:
        记录["upload"]["response"] = 上传数据
        成功 = False
    else:
        任务编号 = 上传数据["data"]["job_id"]
        截止 = time.monotonic() + 参数.等待秒数
        任务 = None
        while time.monotonic() < 截止:
            任务响应 = requests.get(f"{基础地址}/api/v1/jobs/{任务编号}", timeout=60)
            任务响应.raise_for_status()
            任务 = 任务响应.json()["data"]
            if 任务["status"] in {"succeeded", "failed", "timed_out"}:
                break
            time.sleep(2)
        记录["job"] = {
            "job_id": 任务编号,
            "status": 任务["status"] if 任务 else "polling_timeout",
            "progress": 任务.get("progress") if 任务 else None,
            "error_code": 任务.get("error_code") if 任务 else None,
        }
        结果响应 = requests.get(
            f"{基础地址}/api/v1/results", params={"job_id": 任务编号}, timeout=60
        )
        结果响应.raise_for_status()
        结果列表 = 结果响应.json()["data"]
        记录["result_count"] = len(结果列表)
        证据数量 = 0
        for 结果 in 结果列表:
            证据响应 = requests.get(
                f"{基础地址}/api/v1/results/{结果['result_id']}/evidence", timeout=60
            )
            证据响应.raise_for_status()
            证据数量 += len(证据响应.json()["data"])
        记录["evidence_count"] = 证据数量
        成功 = 记录["job"]["status"] == "succeeded"

    记录["passed"] = 成功
    输出文本 = json.dumps(记录, ensure_ascii=False, indent=2)
    print(输出文本)
    if 参数.输出:
        参数.输出.parent.mkdir(parents=True, exist_ok=True)
        参数.输出.write_text(输出文本 + "\n", encoding="utf-8")
    return 0 if 成功 else 1


if __name__ == "__main__":
    raise SystemExit(主函数())
