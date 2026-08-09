# -*- coding: utf-8 -*-
"""Run per-sample guarded extraction regression.

This wrapper prevents one problematic PDF from blocking a whole regression
batch. It launches run_full_extraction_v0.9.py once per sample, applies a
timeout, writes per-sample logs, and records a status CSV.
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SAMPLE_JSON = BASE_DIR / "算法源码" / "示例清单" / "示例样本清单.json"
DEFAULT_EXTRACTOR = SCRIPT_DIR / "run_full_extraction_v0.9.py"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def parse_samples(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


def samples_from_diagnosis(path: Path, actions: set[str]) -> list[str]:
    samples: list[str] = []
    seen: set[str] = set()
    for row in read_csv(path):
        sid = row.get("sample_id", "").strip()
        action = row.get("recommended_action", "").strip()
        if sid and sid not in seen and action in actions:
            samples.append(sid)
            seen.add(sid)
    return samples


def write_status(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "sample_id",
        "status",
        "returncode",
        "elapsed_sec",
        "out_dir",
        "csv_path",
        "stdout_path",
        "stderr_path",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def run_one(sample_id: str, args: argparse.Namespace) -> dict[str, Any]:
    out_dir = args.out_root / sample_id
    out_dir.mkdir(parents=True, exist_ok=True)
    label = f"{sample_id}_{args.run_label_suffix}"
    csv_path = out_dir / f"全量指标候选抽取结果_{label}.csv"
    stdout_path = out_dir / f"{sample_id}_stdout.log"
    stderr_path = out_dir / f"{sample_id}_stderr.log"

    if csv_path.exists() and not args.force:
        return {
            "sample_id": sample_id,
            "status": "skip_exists",
            "returncode": "",
            "elapsed_sec": 0,
            "out_dir": str(out_dir),
            "csv_path": str(csv_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }

    env = os.environ.copy()
    env["SAMPLE_JSON_PATH"] = str(args.sample_json)
    env["PILOT_SAMPLE_IDS"] = sample_id
    env["PILOT_OUT_DIR"] = str(out_dir)
    env["PILOT_RUN_LABEL"] = label
    env["PILOT_PRIORITY"] = args.priority

    started = time.time()
    status = "ok"
    returncode: int | str = ""
    with stdout_path.open("w", encoding="utf-8", errors="replace") as stdout, stderr_path.open(
        "w", encoding="utf-8", errors="replace"
    ) as stderr:
        proc = subprocess.Popen(
            [sys.executable, str(args.extractor)],
            cwd=str(SCRIPT_DIR),
            env=env,
            stdout=stdout,
            stderr=stderr,
            text=True,
        )
        try:
            returncode = proc.wait(timeout=args.timeout_sec)
            if returncode != 0 or not csv_path.exists():
                status = "failed"
        except subprocess.TimeoutExpired:
            status = "timeout"
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    elapsed = round(time.time() - started, 2)
    return {
        "sample_id": sample_id,
        "status": status,
        "returncode": returncode,
        "elapsed_sec": elapsed,
        "out_dir": str(out_dir),
        "csv_path": str(csv_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", default="")
    parser.add_argument("--diagnosis-csv", type=Path)
    parser.add_argument("--actions", default="run_ocr_then_regression,force_ocr_then_regression")
    parser.add_argument("--out-root", type=Path, required=True)
    parser.add_argument("--status-csv", type=Path, required=True)
    parser.add_argument("--sample-json", type=Path, default=DEFAULT_SAMPLE_JSON)
    parser.add_argument("--extractor", type=Path, default=DEFAULT_EXTRACTOR)
    parser.add_argument("--run-label-suffix", default="guarded_regression")
    parser.add_argument("--priority", default="all")
    parser.add_argument("--timeout-sec", type=int, default=900)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sample_ids = parse_samples(args.samples)
    if args.diagnosis_csv:
        actions = {item.strip() for item in args.actions.split(",") if item.strip()}
        sample_ids.extend(samples_from_diagnosis(args.diagnosis_csv, actions))

    deduped: list[str] = []
    seen: set[str] = set()
    for sid in sample_ids:
        if sid not in seen:
            deduped.append(sid)
            seen.add(sid)

    rows: list[dict[str, Any]] = []
    if args.dry_run:
        for sid in deduped:
            rows.append(
                {
                    "sample_id": sid,
                    "status": "dry_run",
                    "returncode": "",
                    "elapsed_sec": 0,
                    "out_dir": str(args.out_root / sid),
                    "csv_path": "",
                    "stdout_path": "",
                    "stderr_path": "",
                }
            )
        write_status(args.status_csv, rows)
        print({"target_count": len(deduped), "status_csv": str(args.status_csv)})
        return

    for sid in deduped:
        print(f"Guarded extraction regression: {sid}", flush=True)
        rows.append(run_one(sid, args))
        write_status(args.status_csv, rows)

    counts: dict[str, int] = {}
    for row in rows:
        status = str(row["status"])
        counts[status] = counts.get(status, 0) + 1
    print({"target_count": len(deduped), "status_counts": counts, "status_csv": str(args.status_csv)})


if __name__ == "__main__":
    main()
