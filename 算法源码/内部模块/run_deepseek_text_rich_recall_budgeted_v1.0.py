# -*- coding: utf-8 -*-
"""Budget guard for DeepSeek text-rich low-coverage recall.

Default mode is dry-run. It estimates selected rows, batches, token-like volume,
and cost, then writes a plan and ledger without calling the API. Use --execute
only after reviewing the plan.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = Path(__file__).resolve().parent
DEFAULT_QUEUE = BASE_DIR / "评估测试" / "candidate_quality_v2.20_200samples_precision_gated" / "文本富集低覆盖DeepSeek召回队列_v2.20.csv"
DEFAULT_OUTPUT = BASE_DIR / "评估测试" / "candidate_quality_v2.20_200samples_precision_gated" / "deepseek文本富集召回结果_budget_guarded_v1.0.csv"
DEFAULT_LEDGER = BASE_DIR / "评估测试" / "deepseek_text_rich_recall_budget_guard_v1.0" / "deepseek_text_rich_recall_budget_ledger_v1.0.csv"
DEFAULT_LOG_DIR = BASE_DIR / "评估测试" / "deepseek_text_rich_recall_budget_guard_v1.0" / "logs"
DEFAULT_PLAN_DIR = BASE_DIR / "评估测试" / "deepseek_text_rich_recall_budget_guard_v1.0" / "plans"
RECALL_SCRIPT = SCRIPTS_DIR / "deepseek_text_rich_recall_v1.0.py"
CHARGE_STATUSES = {"executed", "execution_failed", "execution_timeout"}
LEDGER_FIELDS = [
    "timestamp",
    "run_id",
    "status",
    "queue_csv",
    "output_csv",
    "sample_id",
    "rows_selected",
    "rows_pending",
    "batch_size",
    "batches",
    "input_tokens_est",
    "output_tokens_est",
    "estimated_cost_usd",
    "spent_before_usd",
    "budget_usd",
    "execute",
    "plan_json",
    "log_path",
    "command",
    "message",
]


def now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def load_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in fields} for row in rows])


def append_ledger(path: Path, row: dict[str, Any]) -> None:
    rows = load_rows(path)
    rows.append({field: row.get(field, "") for field in LEDGER_FIELDS})
    write_csv(path, rows, LEDGER_FIELDS)


def charged_spend(path: Path) -> float:
    total = 0.0
    for row in load_rows(path):
        if row.get("status") in CHARGE_STATUSES:
            try:
                total += float(row.get("estimated_cost_usd") or 0)
            except ValueError:
                continue
    return total


def has_deepseek_key() -> bool:
    if os.environ.get("DEEPSEEK_API_KEY"):
        return True
    config_path = SCRIPTS_DIR / "api_config.json"
    if not config_path.exists():
        return False
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool(config.get("deepseek", {}).get("api_key"))


def has_anthropic_sdk() -> bool:
    try:
        import anthropic  # noqa: F401
    except Exception:
        return False
    return True


def output_done_keys(path: Path) -> set[tuple[str, str]]:
    return {(row.get("sample_id", ""), row.get("field_id", "")) for row in load_rows(path)}


def select_rows(args: argparse.Namespace) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rows = load_rows(args.queue_csv)
    if args.sample_id:
        rows = [row for row in rows if row.get("sample_id") == args.sample_id]
    if args.limit > 0:
        rows = rows[: args.limit]
    if args.resume:
        done = output_done_keys(args.output_csv)
        pending = [row for row in rows if (row.get("sample_id", ""), row.get("field_id", "")) not in done]
    else:
        pending = list(rows)
    return rows, pending


def estimate_chars(row: dict[str, str]) -> int:
    fields = [
        row.get("sample_id", ""),
        row.get("field_id", ""),
        row.get("metric_name_cn", ""),
        row.get("dimension", ""),
        row.get("unit_normalized", ""),
        row.get("aliases_cn", ""),
        row.get("page_hits", ""),
        row.get("llm_task", ""),
        row.get("evidence_snippet", "")[:2400],
    ]
    return sum(len(str(item)) for item in fields) + 900


def estimate_plan(
    rows: list[dict[str, str]],
    batch_size: int,
    max_output_tokens: int,
    input_cost: float,
    output_cost: float,
) -> dict[str, Any]:
    batches = math.ceil(len(rows) / max(batch_size, 1)) if rows else 0
    input_tokens = math.ceil(sum(estimate_chars(row) for row in rows) / 1.8)
    output_tokens = batches * max_output_tokens
    cost = input_tokens / 1_000_000 * input_cost + output_tokens / 1_000_000 * output_cost
    return {
        "rows_pending": len(rows),
        "batch_size": batch_size,
        "batches": batches,
        "input_tokens_est": input_tokens,
        "output_tokens_est": output_tokens,
        "estimated_cost_usd": round(cost, 6),
    }


def build_command(args: argparse.Namespace) -> list[str]:
    cmd = [
        sys.executable,
        str(RECALL_SCRIPT),
        "--queue",
        str(args.queue_csv),
        "--output",
        str(args.output_csv),
        "--limit",
        str(args.limit),
        "--batch-size",
        str(args.batch_size),
        "--evidence-char-limit",
        str(args.evidence_char_limit),
    ]
    if args.sample_id:
        cmd.extend(["--sample-id", args.sample_id])
    if args.resume:
        cmd.append("--resume")
    return cmd


def write_plan(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_log(path: Path, stdout: str, stderr: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("STDOUT\n======\n" + stdout + "\n\nSTDERR\n======\n" + stderr, encoding="utf-8")


def ledger_payload(
    args: argparse.Namespace,
    status: str,
    selected_count: int,
    estimate: dict[str, Any],
    spent_before: float,
    plan_json: Path,
    command: list[str],
    message: str,
    log_path: Path | None = None,
) -> dict[str, Any]:
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "run_id": args.run_id,
        "status": status,
        "queue_csv": str(args.queue_csv),
        "output_csv": str(args.output_csv),
        "sample_id": args.sample_id or "",
        "rows_selected": selected_count,
        "rows_pending": estimate["rows_pending"],
        "batch_size": estimate["batch_size"],
        "batches": estimate["batches"],
        "input_tokens_est": estimate["input_tokens_est"],
        "output_tokens_est": estimate["output_tokens_est"],
        "estimated_cost_usd": estimate["estimated_cost_usd"],
        "spent_before_usd": round(spent_before, 6),
        "budget_usd": args.budget_usd,
        "execute": args.execute,
        "plan_json": str(plan_json),
        "log_path": str(log_path) if log_path else "",
        "command": " ".join(command),
        "message": message,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-csv", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--plan-dir", type=Path, default=DEFAULT_PLAN_DIR)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--sample-id", default="")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--evidence-char-limit", type=int, default=2200)
    parser.add_argument("--budget-usd", type=float, default=10.0)
    parser.add_argument("--estimated-input-usd-per-1m", type=float, default=2.0)
    parser.add_argument("--estimated-output-usd-per-1m", type=float, default=8.0)
    parser.add_argument("--max-output-tokens-per-batch", type=int, default=2048)
    parser.add_argument("--timeout-sec", type=int, default=7200)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Actually call DeepSeek after budget checks")
    args = parser.parse_args()
    args.run_id = args.run_id or now_tag()

    selected, pending = select_rows(args)
    estimate = estimate_plan(
        pending,
        args.batch_size,
        args.max_output_tokens_per_batch,
        args.estimated_input_usd_per_1m,
        args.estimated_output_usd_per_1m,
    )
    command = build_command(args)
    spent_before = charged_spend(args.ledger)
    plan_json = args.plan_dir / f"deepseek_text_rich_recall_plan_{args.run_id}.json"
    plan_payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_id": args.run_id,
        "queue_csv": str(args.queue_csv),
        "output_csv": str(args.output_csv),
        "sample_id": args.sample_id or "",
        "rows_selected": len(selected),
        "execute": args.execute,
        "budget_usd": args.budget_usd,
        "spent_before_usd": round(spent_before, 6),
        "estimate": estimate,
        "command": command,
    }
    write_plan(plan_json, plan_payload)

    if not args.execute:
        message = "dry-run only; no API call"
        append_ledger(args.ledger, ledger_payload(args, "dry_run", len(selected), estimate, spent_before, plan_json, command, message))
        print(json.dumps({**plan_payload, "status": "dry_run", "message": message}, ensure_ascii=False, indent=2))
        return

    if not has_deepseek_key():
        message = "DeepSeek API key not configured"
        append_ledger(args.ledger, ledger_payload(args, "blocked_no_api_key", len(selected), estimate, spent_before, plan_json, command, message))
        raise SystemExit(message)

    if not has_anthropic_sdk():
        message = "anthropic SDK not installed in current Python runtime"
        append_ledger(args.ledger, ledger_payload(args, "blocked_missing_sdk", len(selected), estimate, spent_before, plan_json, command, message))
        raise SystemExit(message)

    projected = spent_before + estimate["estimated_cost_usd"]
    if projected > args.budget_usd:
        message = f"budget exceeded: projected={projected:.6f} > budget={args.budget_usd:.6f}"
        append_ledger(args.ledger, ledger_payload(args, "blocked_budget_exceeded", len(selected), estimate, spent_before, plan_json, command, message))
        raise SystemExit(message)

    log_path = args.log_dir / f"deepseek_text_rich_recall_{args.run_id}.log"
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=args.timeout_sec)
        write_log(log_path, result.stdout, result.stderr)
        status = "executed" if result.returncode == 0 else "execution_failed"
        message = f"returncode={result.returncode}"
        append_ledger(args.ledger, ledger_payload(args, status, len(selected), estimate, spent_before, plan_json, command, message, log_path))
        print(json.dumps({**plan_payload, "status": status, "message": message, "log_path": str(log_path)}, ensure_ascii=False, indent=2))
        if result.returncode != 0:
            raise SystemExit(result.returncode)
    except subprocess.TimeoutExpired as exc:
        write_log(log_path, exc.stdout or "", exc.stderr or "")
        message = f"execution timeout after {args.timeout_sec}s"
        append_ledger(args.ledger, ledger_payload(args, "execution_timeout", len(selected), estimate, spent_before, plan_json, command, message, log_path))
        raise SystemExit(message)


if __name__ == "__main__":
    main()
