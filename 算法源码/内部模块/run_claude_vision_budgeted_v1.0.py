# -*- coding: utf-8 -*-
"""Budget-guarded Claude Vision fallback runner.

This wrapper keeps Claude Vision as the last automated fallback:
1. Always run claude_vision_extract.py --dry-run first.
2. Estimate cost from the rendered page plan.
3. Refuse execution if the ledger would exceed the configured budget.
4. Refuse text-rich low-coverage cases unless explicitly overridden.
5. Only call the API when --execute is provided.

The estimate is intentionally configurable because gateway pricing may differ.
Use the ledger as an audit trail, not as an invoice substitute.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = Path(__file__).resolve().parent
CLAUDE_SCRIPT = SCRIPTS_DIR / "claude_vision_extract.py"
DEFAULT_SAMPLE_JSON = BASE_DIR / "数据集与标注" / "gold_label_plan" / "首批200样本清单_RID.json"
DEFAULT_LEDGER = BASE_DIR / "评估测试" / "claude_vision_budget_ledger_v1.0.csv"
DEFAULT_LOG_DIR = BASE_DIR / "评估测试" / "claude_vision_budget_logs_v1.0"
DEFAULT_OUT_DIR = BASE_DIR / "算法方案" / "claude_vision_budgeted_v1.0"
PLAN_DIR = BASE_DIR / "算法方案" / "llm_extraction_v1.0" / "claude_vision_debug"


LEDGER_FIELDS = [
    "timestamp",
    "sample_id",
    "fields",
    "fallback_reason",
    "source_rows",
    "max_pages",
    "batch_size",
    "planned_pages",
    "planned_batches",
    "estimated_cost_usd",
    "budget_limit_usd",
    "budget_spent_before_usd",
    "budget_spent_after_usd",
    "status",
    "dry_run_plan",
    "output_csv",
    "dry_run_log",
    "execute_log",
    "command",
    "message",
]

CHARGE_STATUSES = {"executed", "execution_failed", "execution_timeout"}
TEXT_RICH_BLOCK_MARKERS = [
    "text_rich_low_coverage",
    "text_rich_zero_coverage",
    "alias_dictionary_and_deepseek_recall",
    "文本富集",
]
ALLOWED_EXECUTION_MARKERS = [
    "ocr_deepseek_failed_image_table",
    "ocr_table_structure_failed",
    "complex_image_table",
    "image_table",
    "vision_fallback",
    "claude_fallback",
    "图片表格",
    "复杂图片",
    "双页",
    "跨页表格",
    "ocr表格结构失败",
]


@dataclass
class VisionTask:
    sample_id: str
    fields: list[str]
    fallback_reason: str
    max_pages: int
    batch_size: int
    source_rows: int = 1

    @property
    def field_key(self) -> str:
        return ",".join(sorted(self.fields))


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def split_fields(value: str) -> list[str]:
    fields: list[str] = []
    for raw in (value or "").replace("；", ",").replace(";", ",").split(","):
        item = raw.strip()
        if item and item not in fields:
            fields.append(item)
    return fields


def parse_int(value: str, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_ledger_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({key: str(row.get(key, "")) for key in LEDGER_FIELDS})


def charged_spend(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = 0.0
    for row in load_rows(path):
        if row.get("status") not in CHARGE_STATUSES:
            continue
        try:
            total += float(row.get("estimated_cost_usd") or 0)
        except ValueError:
            pass
    return round(total, 4)


def executed_keys(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    keys: set[tuple[str, str]] = set()
    for row in load_rows(path):
        if row.get("status") == "executed":
            keys.add((row.get("sample_id", ""), row.get("fields", "")))
    return keys


def task_reason(row: dict[str, str]) -> str:
    parts = [
        row.get("fallback_reason", ""),
        row.get("reason", ""),
        row.get("recommended_action", ""),
        row.get("diagnosis_type", ""),
        row.get("classification", ""),
    ]
    return " | ".join(part for part in parts if part).strip()


def load_queue_tasks(path: Path, default_max_pages: int, default_batch_size: int) -> list[VisionTask]:
    rows = load_rows(path)
    grouped: dict[tuple[str, str, int, int], VisionTask] = {}
    for row in rows:
        sid = row.get("sample_id", "").strip()
        if not sid:
            continue
        fields = (
            split_fields(row.get("fields", ""))
            or split_fields(row.get("field_ids", ""))
            or split_fields(row.get("field_id", ""))
        )
        reason = task_reason(row)
        max_pages = parse_int(row.get("max_pages", ""), default_max_pages)
        batch_size = parse_int(row.get("batch_size", ""), default_batch_size)
        key = (sid, reason, max_pages, batch_size)
        task = grouped.get(key)
        if not task:
            task = VisionTask(sid, [], reason, max_pages, batch_size, 0)
            grouped[key] = task
        for field in fields:
            if field not in task.fields:
                task.fields.append(field)
        task.source_rows += 1
    return list(grouped.values())


def direct_task(args: argparse.Namespace) -> list[VisionTask]:
    if not args.sample:
        return []
    return [
        VisionTask(
            sample_id=args.sample,
            fields=split_fields(args.fields),
            fallback_reason=args.fallback_reason or "manual_development_dry_run",
            max_pages=args.max_pages,
            batch_size=args.batch_size,
        )
    ]


def output_path(out_dir: Path, task: VisionTask) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    digest_src = task.field_key or "all_fields"
    digest = hashlib.md5(digest_src.encode("utf-8")).hexdigest()[:10]
    return out_dir / f"ClaudeVision_{task.sample_id}_{digest}.csv"


def write_log(log_dir: Path, task: VisionTask, phase: str, stdout: str, stderr: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = log_dir / f"{stamp}_{task.sample_id}_{phase}.log"
    path.write_text(
        "STDOUT\n======\n" + (stdout or "") + "\n\nSTDERR\n======\n" + (stderr or ""),
        encoding="utf-8",
    )
    return path


def build_command(
    task: VisionTask,
    sample_json: Path,
    dry_run: bool,
    out_csv: Path | None = None,
) -> list[str]:
    cmd = [
        sys.executable,
        str(CLAUDE_SCRIPT),
        "--sample",
        task.sample_id,
        "--sample-json",
        str(sample_json),
        "--max-pages",
        str(task.max_pages),
        "--batch-size",
        str(task.batch_size),
    ]
    if task.fields:
        cmd.extend(["--fields", task.field_key])
    if dry_run:
        cmd.append("--dry-run")
    elif out_csv:
        cmd.extend(["--output", str(out_csv)])
    return cmd


def run_command(cmd: list[str], timeout_sec: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(SCRIPTS_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_sec,
    )


def recent_plan(task: VisionTask, started_at: float) -> tuple[Path, dict[str, Any] | None]:
    path = PLAN_DIR / f"{task.sample_id}_dry_run_plan.json"
    if not path.exists():
        return path, None
    if path.stat().st_mtime < started_at - 2:
        return path, None
    try:
        return path, json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return path, None


def estimate_cost(plan: dict[str, Any] | None, per_page: float, per_batch: float) -> tuple[int, int, float]:
    if not plan:
        return 0, 0, 0.0
    pages = len(plan.get("top_pages") or [])
    batches = len(plan.get("batches") or [])
    return pages, batches, round(pages * per_page + batches * per_batch, 4)


def safety_block(task: VisionTask, args: argparse.Namespace) -> str:
    reason = (task.fallback_reason or "").lower()
    if args.execute and not args.allow_text_rich:
        if any(marker.lower() in reason for marker in TEXT_RICH_BLOCK_MARKERS):
            return "blocked: text-rich low coverage must use DeepSeek recall before Claude Vision"
    if args.execute and not task.fields and not args.allow_all_fields:
        return "blocked: execution requires explicit fields unless --allow-all-fields is set"
    if args.execute and not args.allow_unsafe:
        if not any(marker.lower() in reason for marker in ALLOWED_EXECUTION_MARKERS):
            return "blocked: fallback reason is not an approved Claude Vision trigger"
    return ""


def append_record(
    args: argparse.Namespace,
    task: VisionTask,
    status: str,
    planned_pages: int,
    planned_batches: int,
    estimate: float,
    spent_before: float,
    dry_run_plan: Path,
    out_csv: Path | None,
    dry_log: Path | None,
    exec_log: Path | None,
    command: list[str],
    message: str,
) -> None:
    spent_after = spent_before + estimate if status in CHARGE_STATUSES else spent_before
    write_ledger_row(
        args.ledger,
        {
            "timestamp": now_text(),
            "sample_id": task.sample_id,
            "fields": task.field_key,
            "fallback_reason": task.fallback_reason,
            "source_rows": task.source_rows,
            "max_pages": task.max_pages,
            "batch_size": task.batch_size,
            "planned_pages": planned_pages,
            "planned_batches": planned_batches,
            "estimated_cost_usd": f"{estimate:.4f}",
            "budget_limit_usd": f"{args.budget_usd:.2f}",
            "budget_spent_before_usd": f"{spent_before:.4f}",
            "budget_spent_after_usd": f"{spent_after:.4f}",
            "status": status,
            "dry_run_plan": str(dry_run_plan) if dry_run_plan else "",
            "output_csv": str(out_csv) if out_csv else "",
            "dry_run_log": str(dry_log) if dry_log else "",
            "execute_log": str(exec_log) if exec_log else "",
            "command": " ".join(command),
            "message": message,
        },
    )


def process_task(task: VisionTask, args: argparse.Namespace) -> str:
    block = safety_block(task, args)
    out_csv = output_path(args.out_dir, task)
    dry_cmd = build_command(task, args.sample_json, dry_run=True)
    started = time.time()
    try:
        dry_result = run_command(dry_cmd, args.timeout_sec)
        dry_log = write_log(args.log_dir, task, "dry_run", dry_result.stdout, dry_result.stderr)
    except subprocess.TimeoutExpired as exc:
        dry_log = write_log(args.log_dir, task, "dry_run_timeout", exc.stdout or "", exc.stderr or "")
        append_record(args, task, "dry_run_timeout", 0, 0, 0.0, charged_spend(args.ledger), Path(""), None, dry_log, None, dry_cmd, "dry-run timeout")
        return "dry_run_timeout"

    plan_path, plan = recent_plan(task, started)
    pages, batches, estimate = estimate_cost(plan, args.estimated_cost_per_page_usd, args.estimated_cost_per_batch_usd)
    spent_before = charged_spend(args.ledger)

    if dry_result.returncode != 0:
        append_record(args, task, "dry_run_failed", pages, batches, estimate, spent_before, plan_path, None, dry_log, None, dry_cmd, "dry-run failed")
        return "dry_run_failed"
    if not plan or pages <= 0:
        append_record(args, task, "no_pages_planned", pages, batches, estimate, spent_before, plan_path, None, dry_log, None, dry_cmd, "no pages selected by RAG plan")
        return "no_pages_planned"
    if block:
        append_record(args, task, "safety_blocked", pages, batches, estimate, spent_before, plan_path, None, dry_log, None, dry_cmd, block)
        return "safety_blocked"
    if spent_before + estimate > args.budget_usd:
        append_record(args, task, "budget_blocked", pages, batches, estimate, spent_before, plan_path, None, dry_log, None, dry_cmd, "estimated budget would be exceeded")
        return "budget_blocked"
    if not args.execute:
        append_record(args, task, "dry_run_only", pages, batches, estimate, spent_before, plan_path, None, dry_log, None, dry_cmd, "use --execute to call Claude Vision")
        return "dry_run_only"

    exec_cmd = build_command(task, args.sample_json, dry_run=False, out_csv=out_csv)
    try:
        exec_result = run_command(exec_cmd, args.timeout_sec)
        exec_log = write_log(args.log_dir, task, "execute", exec_result.stdout, exec_result.stderr)
        status = "executed" if exec_result.returncode == 0 else "execution_failed"
        message = "ok" if exec_result.returncode == 0 else f"returncode={exec_result.returncode}"
        append_record(args, task, status, pages, batches, estimate, spent_before, plan_path, out_csv, dry_log, exec_log, exec_cmd, message)
        return status
    except subprocess.TimeoutExpired as exc:
        exec_log = write_log(args.log_dir, task, "execute_timeout", exc.stdout or "", exc.stderr or "")
        append_record(args, task, "execution_timeout", pages, batches, estimate, spent_before, plan_path, out_csv, dry_log, exec_log, exec_cmd, "execution timeout")
        return "execution_timeout"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-csv", type=Path, help="Claude fallback queue CSV; supports sample_id, field_id/fields, fallback_reason")
    parser.add_argument("--sample", help="Single sample id for development or targeted fallback")
    parser.add_argument("--fields", default="", help="Comma-separated field ids for single-sample mode")
    parser.add_argument("--fallback-reason", default="", help="Reason for single-sample mode")
    parser.add_argument("--sample-json", type=Path, default=DEFAULT_SAMPLE_JSON)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--budget-usd", type=float, default=30.0)
    parser.add_argument("--estimated-cost-per-page-usd", type=float, default=0.15)
    parser.add_argument("--estimated-cost-per-batch-usd", type=float, default=0.05)
    parser.add_argument("--max-pages", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--timeout-sec", type=int, default=900)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Actually call Claude Vision after dry-run and budget checks")
    parser.add_argument("--allow-unsafe", action="store_true", help="Allow execution without approved fallback reason")
    parser.add_argument("--allow-text-rich", action="store_true", help="Allow text-rich low coverage execution; not recommended")
    parser.add_argument("--allow-all-fields", action="store_true", help="Allow executing all quantitative fields when no field list is provided")
    args = parser.parse_args()

    tasks: list[VisionTask] = []
    if args.queue_csv:
        tasks.extend(load_queue_tasks(args.queue_csv, args.max_pages, args.batch_size))
    tasks.extend(direct_task(args))
    if args.limit > 0:
        tasks = tasks[: args.limit]
    if args.resume:
        done = executed_keys(args.ledger)
        tasks = [task for task in tasks if (task.sample_id, task.field_key) not in done]

    if not tasks:
        print("No Claude Vision fallback tasks found.")
        return

    counts: dict[str, int] = {}
    for task in tasks:
        status = process_task(task, args)
        counts[status] = counts.get(status, 0) + 1

    summary = {
        "tasks": len(tasks),
        "status_counts": counts,
        "ledger": str(args.ledger),
        "charged_estimate_usd": charged_spend(args.ledger),
        "budget_usd": args.budget_usd,
        "execute": args.execute,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
