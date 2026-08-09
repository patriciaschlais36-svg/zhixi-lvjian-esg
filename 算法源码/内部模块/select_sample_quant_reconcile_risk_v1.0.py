# -*- coding: utf-8 -*-
"""Select samples for sample-level quantitative DeepSeek reconciliation.

The selector uses only extraction-candidate risk features. It does not read
gold labels. The output sample_id list can be fed to
run_deepseek_sample_quant_reconcile_v1.0.py.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


P0_QUANT_FIELDS = {
    "E_Q_001", "E_Q_002", "E_Q_003", "E_Q_005", "E_Q_006", "E_Q_007", "E_Q_009",
    "E_Q_012", "E_Q_013", "E_Q_015", "S_Q_001", "S_Q_002", "S_Q_004", "S_Q_005",
    "S_Q_008", "S_Q_009", "S_Q_017", "G_Q_001", "G_Q_002", "G_Q_003", "G_Q_009",
    "G_Q_010",
}


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def parse_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value or "").strip())
    except ValueError:
        return default


def sample_features(sample_id: str, rows: list[dict[str, str]]) -> dict[str, Any]:
    # Risk is a sample-level property. Non-P0 quantitative candidates also
    # reveal table density, conflicts, OCR layout risk, and LLM override risk,
    # so they are included in scoring. Downstream reconciliation still only
    # rewrites P0 quantitative fields.
    found = [
        row
        for row in rows
        if row.get("metric_type") == "quantitative"
        and row.get("candidate_status") == "candidate_found"
    ]
    by_field: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in found:
        by_field[row.get("field_id", "")].append(row)

    fields_found = len(by_field)
    multi_candidate_fields = sum(1 for values in by_field.values() if len(values) > 1)
    max_per_field = max([len(values) for values in by_field.values()] or [0])
    low_conf_rows = sum(1 for row in found if parse_float(row.get("confidence_rule"), 0.0) < 0.7)
    very_low_conf_rows = sum(1 for row in found if parse_float(row.get("confidence_rule"), 0.0) < 0.55)
    needs_review_rows = sum(1 for row in found if str(row.get("needs_llm_review", "")).strip().lower() == "yes")
    methods = [str(row.get("value_extraction_method", "")).lower() for row in found]
    table_rows = sum(1 for method in methods if "table" in method or "ocr" in method)
    llm_or_deepseek_rows = sum(1 for method in methods if "llm" in method or "deepseek" in method)
    blank_unit_rows = sum(1 for row in found if not str(row.get("unit_raw_candidate", "") or "").strip())

    risk_score = 0
    if multi_candidate_fields >= 8:
        risk_score += 2
    if low_conf_rows >= 8:
        risk_score += 2
    if needs_review_rows >= 18:
        risk_score += 2
    if table_rows >= 20:
        risk_score += 2
    if llm_or_deepseek_rows >= 8:
        risk_score += 1
    if max_per_field >= 4:
        risk_score += 1
    if fields_found >= 22:
        risk_score += 1

    high_structural_risk = risk_score >= 8
    many_llm_overrides = llm_or_deepseek_rows >= 10 and len(found) >= 20
    conflict_heavy_not_low_conf = needs_review_rows >= 40 and multi_candidate_fields >= 12 and low_conf_rows < 8
    selected = high_structural_risk or many_llm_overrides or conflict_heavy_not_low_conf

    reasons = []
    if high_structural_risk:
        reasons.append("risk_score_ge_8")
    if many_llm_overrides:
        reasons.append("many_llm_or_deepseek_rows")
    if conflict_heavy_not_low_conf:
        reasons.append("conflict_heavy_not_low_conf")

    return {
        "sample_id": sample_id,
        "short_name": next((row.get("short_name", "") for row in rows if row.get("short_name")), ""),
        "stock_code": next((row.get("stock_code", "") for row in rows if row.get("stock_code")), ""),
        "found_rows": len(found),
        "fields_found": fields_found,
        "multi_candidate_fields": multi_candidate_fields,
        "max_per_field": max_per_field,
        "low_conf_rows": low_conf_rows,
        "very_low_conf_rows": very_low_conf_rows,
        "needs_review_rows": needs_review_rows,
        "table_rows": table_rows,
        "llm_or_deepseek_rows": llm_or_deepseek_rows,
        "blank_unit_rows": blank_unit_rows,
        "risk_score": risk_score,
        "selected_for_sample_reconcile": "yes" if selected else "no",
        "selection_reasons": ";".join(reasons),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-csv", type=Path, required=True)
    parser.add_argument("--out-csv", type=Path, required=True)
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--sample-id-txt", type=Path)
    args = parser.parse_args()

    rows, _ = read_csv(args.candidate_csv)
    by_sample: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_sample[row.get("sample_id", "")].append(row)

    risk_rows = [sample_features(sample_id, sample_rows) for sample_id, sample_rows in sorted(by_sample.items()) if sample_id]
    fields = [
        "sample_id", "short_name", "stock_code", "found_rows", "fields_found",
        "multi_candidate_fields", "max_per_field", "low_conf_rows", "very_low_conf_rows",
        "needs_review_rows", "table_rows", "llm_or_deepseek_rows", "blank_unit_rows",
        "risk_score", "selected_for_sample_reconcile", "selection_reasons",
    ]
    write_csv(args.out_csv, risk_rows, fields)
    selected = [row["sample_id"] for row in risk_rows if row["selected_for_sample_reconcile"] == "yes"]
    if args.sample_id_txt:
        args.sample_id_txt.parent.mkdir(parents=True, exist_ok=True)
        args.sample_id_txt.write_text(",".join(selected), encoding="utf-8")

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_csv": str(args.candidate_csv),
        "out_csv": str(args.out_csv),
        "sample_id_txt": str(args.sample_id_txt or ""),
        "sample_count": len(risk_rows),
        "selected_count": len(selected),
        "selected_sample_ids": selected,
        "reason_counts": dict(Counter(reason for row in risk_rows for reason in str(row["selection_reasons"]).split(";") if reason)),
        "policy": "candidate-risk features only; no gold labels",
    }
    if args.summary_json:
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        args.summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
