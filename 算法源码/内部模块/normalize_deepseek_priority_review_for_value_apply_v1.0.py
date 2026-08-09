# -*- coding: utf-8 -*-
"""Normalize priority-review DeepSeek output for value-review guarded apply.

The priority-review script emits llm_decision/llm_corrected_value columns.
The guarded value-apply script expects llm_review_decision/llm_better_value.
This bridge keeps the production policy conservative: only `modify` rows with
parseable better values can be accepted downstream; reject-only rows remain
auditable but are not applied by value-review writeback.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def load_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def write_rows(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def normalize_row(row: dict[str, str], idx: int) -> dict[str, str]:
    decision = (row.get("llm_decision") or "").strip().lower()
    if decision == "modify":
        review_decision = "better_value_in_context"
        auto_fix = "replace_value"
    elif decision == "reject":
        review_decision = "reject_only_not_applied"
        auto_fix = "reject_candidate_review_only"
    else:
        review_decision = decision or "unknown"
        auto_fix = "none"
    return {
        "queue_id": row.get("queue_id") or f"priority_review_{idx:04d}",
        "sample_id": row.get("sample_id", ""),
        "field_id": row.get("field_id", ""),
        "metric_name": row.get("metric_name_cn") or row.get("metric_name", ""),
        "candidate_value": row.get("value_candidate", ""),
        "candidate_unit_raw": row.get("unit_raw_candidate", ""),
        "candidate_page": row.get("source_page", ""),
        "llm_review_decision": review_decision,
        "llm_auto_fix_action": auto_fix,
        "llm_confidence": row.get("llm_confidence", ""),
        "llm_better_value": row.get("llm_corrected_value", ""),
        "llm_better_unit": row.get("llm_corrected_unit", ""),
        "llm_better_page": row.get("llm_corrected_page") or row.get("source_page", ""),
        "llm_reason": row.get("llm_reason", ""),
        "source_llm_decision": row.get("llm_decision", ""),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    args = parser.parse_args()

    rows, _ = load_rows(args.input_csv)
    out_rows = [normalize_row(row, idx) for idx, row in enumerate(rows, 1)]
    fields = [
        "queue_id",
        "sample_id",
        "field_id",
        "metric_name",
        "candidate_value",
        "candidate_unit_raw",
        "candidate_page",
        "llm_review_decision",
        "llm_auto_fix_action",
        "llm_confidence",
        "llm_better_value",
        "llm_better_unit",
        "llm_better_page",
        "llm_reason",
        "source_llm_decision",
    ]
    write_rows(args.output_csv, out_rows, fields)
    print(f"normalized_rows={len(out_rows)} output={args.output_csv}")


if __name__ == "__main__":
    main()
