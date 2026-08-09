# -*- coding: utf-8 -*-
"""Apply triaged DeepSeek safe recall candidates to an extraction copy.

This is a what-if utility. It never edits the source extraction CSV and should
not be treated as production recall until gold or automatic evidence checks are
strong enough.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_BASE = (
    BASE_DIR
    / "算法方案"
    / "pilot_full_extraction_v2.15_200samples_pipeline_guarded"
    / "全量指标候选抽取结果_200份v2.20_precision_gated.csv"
)
DEFAULT_SAFE = (
    BASE_DIR
    / "评估测试"
    / "deepseek_text_rich_recall_triage_v2.34"
    / "safe_recall_candidates_v1.0.csv"
)
DEFAULT_OUTPUT = (
    BASE_DIR
    / "算法方案"
    / "pilot_full_extraction_v2.15_200samples_pipeline_guarded"
    / "全量指标候选抽取结果_200份v2.34_deepseek_recall_safe_whatif.csv"
)

WHATIF_COLUMNS = [
    "recall_patch_status",
    "recall_patch_source",
    "recall_patch_confidence",
    "recall_patch_reason",
    "recall_patch_evidence",
]


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


def key(row: dict[str, str]) -> tuple[str, str]:
    return row.get("sample_id", ""), row.get("field_id", "")


def load_safe(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    safe: dict[tuple[str, str], dict[str, str]] = {}
    for row in load_rows(path)[0]:
        if row.get("triage_status") not in {"safe_recall_candidate", "human_validated_safe_recall_candidate"}:
            continue
        item_key = key(row)
        existing = safe.get(item_key)
        if not existing or float(row.get("llm_confidence") or 0) > float(existing.get("llm_confidence") or 0):
            safe[item_key] = row
    return safe


def patch_row(row: dict[str, str], safe: dict[str, str], guarded_production: bool) -> dict[str, str]:
    out = dict(row)
    metric_type = row.get("metric_type", safe.get("metric_type", ""))
    value = safe.get("llm_value", "")
    unit = safe.get("llm_unit_raw", "")
    evidence = safe.get("source_evidence_snippet", "") or safe.get("llm_reason", "")
    method_suffix = "safe_guarded" if guarded_production else "safe_whatif"
    value_status = "llm_recalled_guarded" if guarded_production else "llm_recalled_whatif"
    needs_review = "no" if guarded_production else "yes"
    review_reason = (
        "DeepSeek text-rich safe recall; auto evidence triage passed"
        if guarded_production
        else "what-if DeepSeek safe recall candidate; requires evidence audit before production writeback"
    )
    next_status = "auto_verified_after_safe_recall" if guarded_production else "review_before_production_apply"
    if metric_type == "qualitative" and not value:
        value = evidence
    out.update(
        {
            "candidate_status": "candidate_found",
            "candidate_disclosure_class": "deepseek_text_rich_recall",
            "candidate_rank": "1",
            "evidence_type_candidate": "deepseek_text_rich_recall",
            "value_candidate": value,
            "unit_raw_candidate": unit,
            "value_standardized_candidate": value,
            "unit_standardized_candidate": unit or row.get("unit_standardized_candidate", ""),
            "value_status": value_status,
            "value_extraction_method": f"deepseek_text_rich_recall_v2.34_{method_suffix}",
            "source_page": safe.get("llm_source_page", ""),
            "source_text": evidence,
            "source_table_cell": "",
            "confidence_rule": safe.get("llm_confidence", ""),
            "needs_llm_review": needs_review,
            "review_reason": review_reason,
            "recommended_next_status": next_status,
            "extractor_version": f"v2.34_deepseek_recall_{method_suffix}",
            "recall_patch_status": f"patched_safe_candidate_{'guarded' if guarded_production else 'whatif'}",
            "recall_patch_source": "deepseek_text_rich_recall_triage_v2.34",
            "recall_patch_confidence": safe.get("llm_confidence", ""),
            "recall_patch_reason": safe.get("llm_reason", ""),
            "recall_patch_evidence": evidence,
        }
    )
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-csv", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--safe-candidates-csv", type=Path, default=DEFAULT_SAFE)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--guarded-production-apply",
        action="store_true",
        help="Mark triaged safe recall candidates as automatically applied with evidence snippets.",
    )
    args = parser.parse_args()

    rows, fields = load_rows(args.base_csv)
    safe = load_safe(args.safe_candidates_csv)
    patched_rows: list[dict[str, str]] = []
    audit: list[dict[str, Any]] = []
    already_found = 0
    patched = 0
    handled_keys: set[tuple[str, str]] = set()

    for row in rows:
        item_key = key(row)
        candidate = safe.get(item_key)
        if not candidate:
            patched_rows.append(row)
            continue
        if item_key in handled_keys:
            patched_rows.append(row)
            audit.append(
                {
                    "sample_id": item_key[0],
                    "field_id": item_key[1],
                    "metric_name_cn": row.get("metric_name_cn", candidate.get("metric_name_cn", "")),
                    "action": "skip_duplicate_key_after_first_patch",
                    "llm_value": candidate.get("llm_value", ""),
                    "llm_confidence": candidate.get("llm_confidence", ""),
                    "llm_source_page": candidate.get("llm_source_page", ""),
                    "llm_reason": candidate.get("llm_reason", ""),
                }
            )
            continue
        if row.get("candidate_status") == "candidate_found":
            already_found += 1
            handled_keys.add(item_key)
            patched_rows.append(row)
            audit.append(
                {
                    "sample_id": item_key[0],
                    "field_id": item_key[1],
                    "action": "skip_already_candidate_found",
                    "llm_confidence": candidate.get("llm_confidence", ""),
                    "llm_source_page": candidate.get("llm_source_page", ""),
                }
            )
            continue
        patched_row = patch_row(row, candidate, args.guarded_production_apply)
        patched_rows.append(patched_row)
        patched += 1
        handled_keys.add(item_key)
        action = "patched_safe_candidate_guarded" if args.guarded_production_apply else "patched_safe_candidate_whatif"
        audit.append(
            {
                "sample_id": item_key[0],
                "field_id": item_key[1],
                "metric_name_cn": row.get("metric_name_cn", candidate.get("metric_name_cn", "")),
                "action": action,
                "llm_value": candidate.get("llm_value", ""),
                "llm_confidence": candidate.get("llm_confidence", ""),
                "llm_source_page": candidate.get("llm_source_page", ""),
                "llm_reason": candidate.get("llm_reason", ""),
                "source_evidence_snippet": candidate.get("source_evidence_snippet", ""),
            }
        )

    fields_out = list(fields)
    for col in WHATIF_COLUMNS:
        if col not in fields_out:
            fields_out.append(col)
    write_rows(args.output_csv, patched_rows, fields_out)
    audit_path = args.output_csv.with_name(args.output_csv.stem + "_audit.csv")
    audit_fields = [
        "sample_id",
        "field_id",
        "metric_name_cn",
        "action",
        "llm_value",
        "llm_confidence",
        "llm_source_page",
        "llm_reason",
        "source_evidence_snippet",
    ]
    write_rows(audit_path, audit, audit_fields)
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "base_csv": str(args.base_csv),
        "safe_candidates_csv": str(args.safe_candidates_csv),
        "output_csv": str(args.output_csv),
        "audit_csv": str(audit_path),
        "safe_candidate_keys": len(safe),
        "patched": patched,
        "already_candidate_found": already_found,
        "candidate_status_counts": dict(Counter(row.get("candidate_status", "") for row in patched_rows)),
        "guarded_production_apply": args.guarded_production_apply,
        "policy": (
            "triaged_safe_recall_guarded_auto_writeback"
            if args.guarded_production_apply
            else "what_if_only_no_production_writeback"
        ),
    }
    summary_path = args.output_csv.with_name(args.output_csv.stem + "_summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
