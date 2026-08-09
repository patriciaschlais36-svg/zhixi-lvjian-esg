# -*- coding: utf-8 -*-
"""Run OCR for diagnosed image/scan samples with per-sample timeout and logs."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SAMPLE_JSON = BASE_DIR / "算法源码" / "示例清单" / "示例样本清单.json"
DEFAULT_OCR_JSON_DIR = BASE_DIR / "运行缓存" / "OCR" / "ocr_page_json"


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def ocr_page_count(ocr_json_dir: Path, sample_id: str) -> int:
    if not ocr_json_dir.exists():
        return 0
    return sum(1 for _ in ocr_json_dir.glob(f"{sample_id}_page_*_ocr.json"))


def choose_targets(rows: list[dict[str, str]], actions: set[str], limit: int | None) -> list[dict[str, str]]:
    seen: set[str] = set()
    targets: list[dict[str, str]] = []
    for row in rows:
        sid = row.get("sample_id", "").strip()
        if not sid or sid in seen:
            continue
        if row.get("recommended_action", "").strip() in actions:
            targets.append(row)
            seen.add(sid)
        if limit and len(targets) >= limit:
            break
    return targets


def run_one(row: dict[str, str], args: argparse.Namespace, env: dict[str, str]) -> dict[str, Any]:
    sample_id = row["sample_id"]
    action = row.get("recommended_action", "")
    log_path = args.log_dir / f"{sample_id}_ocr.log"
    before = ocr_page_count(args.ocr_json_dir, sample_id)
    cmd = [
        sys.executable,
        str(args.ocr_script),
        "--sample",
        sample_id,
    ]
    if action in args.force_actions_set:
        cmd.append("--force")
    started = time.time()
    status = "ok"
    status_note = ""
    returncode: int | None = None
    stdout = ""
    stderr = ""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(SCRIPT_DIR),
            env=env,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.timeout_sec,
        )
        returncode = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
        if proc.returncode != 0:
            status = "failed"
    except subprocess.TimeoutExpired as exc:
        status = "timeout"
        status_note = f"subprocess_timeout_sec={args.timeout_sec}"
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
    elapsed = round(time.time() - started, 2)
    after = ocr_page_count(args.ocr_json_dir, sample_id)
    new_pages = after - before
    if status == "timeout" and new_pages > 0 and "Done:" in str(stdout):
        status = "ok"
        returncode = 0
        status_note = f"timeout_corrected_after_complete_log; subprocess_timeout_sec={args.timeout_sec}"
    log_path.write_text(
        "\n".join(
            [
                f"sample_id={sample_id}",
                f"status={status}",
                f"status_note={status_note}",
                f"returncode={returncode}",
                f"elapsed_sec={elapsed}",
                f"ocr_pages_before={before}",
                f"ocr_pages_after={after}",
                "",
                "STDOUT:",
                str(stdout),
                "",
                "STDERR:",
                str(stderr),
            ]
        ),
        encoding="utf-8",
    )
    return {
        "sample_id": sample_id,
        "recommended_action": action,
        "status": status,
        "status_note": status_note,
        "returncode": returncode,
        "elapsed_sec": elapsed,
        "ocr_pages_before": before,
        "ocr_pages_after": after,
        "ocr_pages_new": new_pages,
        "log_path": str(log_path),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else [
        "sample_id", "status", "status_note", "returncode", "elapsed_sec",
        "ocr_pages_before", "ocr_pages_after", "ocr_pages_new", "log_path",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnosis-csv", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--sample-json", type=Path, default=DEFAULT_SAMPLE_JSON)
    parser.add_argument("--ocr-json-dir", type=Path, default=DEFAULT_OCR_JSON_DIR)
    parser.add_argument("--ocr-script", type=Path, default=SCRIPT_DIR / "batch_ocr_expand.py")
    parser.add_argument("--actions", default="run_ocr_then_regression,force_ocr_then_regression")
    parser.add_argument("--force-actions", default="force_ocr_then_regression")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout-sec", type=int, default=2400)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = read_rows(args.diagnosis_csv)
    actions = {item.strip() for item in args.actions.split(",") if item.strip()}
    args.force_actions_set = {item.strip() for item in args.force_actions.split(",") if item.strip()}
    targets = choose_targets(rows, actions, args.limit)

    args.log_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["SAMPLE_JSON_PATH"] = str(args.sample_json)
    env["OCR_CACHE_DIR"] = str(args.ocr_json_dir.parent)

    if args.dry_run:
        result_rows = [
            {
                "sample_id": row["sample_id"],
                "recommended_action": row.get("recommended_action", ""),
                "status": "dry_run",
                "status_note": "",
                "returncode": "",
                "elapsed_sec": 0,
                "ocr_pages_before": ocr_page_count(args.ocr_json_dir, row["sample_id"]),
                "ocr_pages_after": ocr_page_count(args.ocr_json_dir, row["sample_id"]),
                "ocr_pages_new": 0,
                "log_path": "",
            }
            for row in targets
        ]
    else:
        result_rows = []
        for row in targets:
            sid = row["sample_id"]
            print(f"OCR guarded run: {sid}")
            result_rows.append(run_one(row, args, env))
            write_csv(args.output_csv, result_rows)

    write_csv(args.output_csv, result_rows)
    summary = {
        "diagnosis_csv": str(args.diagnosis_csv),
        "output_csv": str(args.output_csv),
        "target_count": len(targets),
        "status_counts": {},
    }
    for row in result_rows:
        status = row["status"]
        summary["status_counts"][status] = summary["status_counts"].get(status, 0) + 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
