# -*- coding: utf-8 -*-
"""Automated guarded ESG batch pipeline v2.0.

Default mode is dry-run: write a reproducible command plan without running
heavy extraction/OCR/LLM work. Use --execute to run stages in order.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPTS_DIR = Path(__file__).resolve().parent
ALGORITHM_DIR = SCRIPTS_DIR.parent
BASE_DIR = ALGORITHM_DIR.parent
CONFIG_DIR = ALGORITHM_DIR / "配置"
DEFAULT_SAMPLE_JSON = ALGORITHM_DIR / "示例清单" / "示例样本清单.json"
DEFAULT_PLAN_DIR = BASE_DIR / "运行产物" / "执行计划"
DEFAULT_NEGATIVE_CASEBOOK = CONFIG_DIR / "不可回写负样本库.csv"
DEFAULT_INDICATOR = CONFIG_DIR / "ESG指标体系.csv"
DEFAULT_INDICATOR_JSON = CONFIG_DIR / "ESG指标体系.json"
DEFAULT_RULE_FLAGS = CONFIG_DIR / "精度门控标记.csv"
DEFAULT_QUALITATIVE_RULES = CONFIG_DIR / "定性指标披露规则.csv"
DEFAULT_OCR_CACHE_DIR = BASE_DIR / "运行缓存" / "OCR"
LATEST_PLACEHOLDER = "<latest_extraction_csv>"
GATED_PLACEHOLDER = "<precision_gated_csv>"


def now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def split_sample_ids(value: str) -> list[str]:
    ids: list[str] = []
    for part in (value or "").replace("；", ",").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part and part.upper().startswith("R"):
            start, end = part.split("-", 1)
            prefix = "".join(ch for ch in start if not ch.isdigit())
            a = int("".join(ch for ch in start if ch.isdigit()))
            b = int("".join(ch for ch in end if ch.isdigit()))
            for i in range(a, b + 1):
                sid = f"{prefix}{i:03d}"
                if sid not in ids:
                    ids.append(sid)
        elif part not in ids:
            ids.append(part)
    return ids


def cmd_text(command: list[str], env: dict[str, str] | None = None) -> str:
    env_prefix = ""
    if env:
        env_prefix = " ".join(f"{k}={v}" for k, v in env.items()) + " "
    return env_prefix + " ".join(command)


def run_command(command: list[str], env: dict[str, str] | None, timeout_sec: int) -> subprocess.CompletedProcess[str]:
    env_vars = os.environ.copy()
    if env:
        env_vars.update(env)
    return subprocess.run(
        command,
        cwd=str(SCRIPTS_DIR),
        env=env_vars,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_sec,
    )


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(text)


def latest_csv(directory: Path, pattern: str = "全量指标候选抽取结果_*.csv") -> Path | None:
    matches = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def normalize_path_text(value: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(value)))


def csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def validate_sample_manifest(path: Path, sample_ids: list[str]) -> None:
    if not path.exists():
        raise ValueError(f"Sample manifest does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read sample manifest {path}: {exc}") from exc
    samples = payload.get("samples")
    if not isinstance(samples, list):
        raise ValueError(f"Sample manifest must contain a samples list: {path}")
    manifest_ids = {
        str(item.get("sample_id", "")).strip()
        for item in samples
        if isinstance(item, dict) and str(item.get("sample_id", "")).strip()
    }
    missing = [sample_id for sample_id in sample_ids if sample_id not in manifest_ids]
    if missing:
        raise ValueError(
            "Sample IDs are absent from the selected manifest: "
            + ", ".join(missing)
            + f". Manifest: {path}"
        )


def ocr_samples_from_diagnosis(path: Path) -> list[str]:
    actions = {"run_ocr_then_regression", "force_ocr_then_regression"}
    return [row.get("sample_id", "") for row in csv_rows(path) if row.get("recommended_action") in actions]


def status(ok: bool) -> str:
    return "OK" if ok else "FAILED"


class Pipeline:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.sample_ids = split_sample_ids(args.samples)
        self.run_id = args.run_id or now_tag()
        if args.out_root:
            self.out_root = args.out_root
        elif args.input_csv:
            self.out_root = args.input_csv.parent
        else:
            self.out_root = BASE_DIR / "运行产物" / self.run_id / "抽取结果"
        self.quality_dir = args.quality_dir or (BASE_DIR / "运行产物" / self.run_id / "质量审计")
        self.auto_verification_dir = args.auto_verification_dir or (BASE_DIR / "运行产物" / self.run_id / "自动验收")
        self.scoring_dir = args.scoring_dir or (BASE_DIR / "运行产物" / self.run_id / "披露评分")
        self.dashboard_dir = args.dashboard_dir or (BASE_DIR / "运行产物" / self.run_id / "展示数据")
        self.blind_quant_eval_dir = args.blind_quant_eval_dir or (self.quality_dir / "blind_quant_unit_normalized_eval_v1.0")
        self.execute_sample_quant_deepseek = args.execute_deepseek or args.execute_sample_quant_deepseek
        self.execute_text_rich_deepseek = args.execute_deepseek or args.execute_text_rich_deepseek
        self.execute_priority_deepseek = args.execute_deepseek or args.execute_priority_deepseek
        self.sample_quant_deepseek_active = bool(args.execute and self.execute_sample_quant_deepseek)
        self.text_rich_deepseek_active = bool(args.execute and self.execute_text_rich_deepseek)
        self.priority_deepseek_active = bool(args.execute and self.execute_priority_deepseek)
        self.plan_dir = args.plan_dir
        self.plan_json = self.plan_dir / f"pipeline_plan_{self.run_id}.json"
        self.plan_md = self.plan_dir / f"pipeline_plan_{self.run_id}.md"
        self.log_dir = self.quality_dir / "pipeline_logs"
        self.steps: list[dict[str, Any]] = []
        self.current_csv: Path | None = args.input_csv
        self.final_extraction_csv: str = str(args.input_csv) if args.input_csv else ""
        self.execution_success: bool | None = None
        self.started_at = datetime.now()
        self.started_monotonic = time.perf_counter()
        self.completed_at: datetime | None = None
        self.duration_seconds: float | None = None

    def add_step(
        self,
        name: str,
        command: list[str],
        env: dict[str, str] | None = None,
        timeout_sec: int = 600,
        required: bool = True,
        note: str = "",
        postconditions: list[dict[str, str]] | None = None,
    ) -> None:
        self.steps.append({
            "name": name,
            "command": command,
            "env": env or {},
            "timeout_sec": timeout_sec,
            "required": required,
            "note": note,
            "command_text": cmd_text(command, env),
            "status": "planned",
            "log": "",
            "started_at": "",
            "finished_at": "",
            "duration_seconds": None,
            "return_code": None,
            "postconditions": postconditions or [],
        })

    def check_postconditions(self, step: dict[str, Any]) -> tuple[bool, list[str]]:
        messages: list[str] = []
        ok = True
        for cond in step.get("postconditions", []):
            cond_type = cond.get("type")
            if cond_type == "exists":
                path = Path(cond["path"])
                if path.exists():
                    messages.append(f"OK exists: {path}")
                else:
                    ok = False
                    messages.append(f"FAILED missing: {path}")
                continue
            if cond_type == "json_field_equals":
                path = Path(cond["path"])
                field = cond["field"]
                expected = cond.get("expected", "")
                if not path.exists():
                    ok = False
                    messages.append(f"FAILED missing json: {path}")
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    ok = False
                    messages.append(f"FAILED invalid json: {path} ({exc})")
                    continue
                actual = str(payload.get(field, ""))
                if normalize_path_text(actual) == normalize_path_text(expected):
                    messages.append(f"OK {path.name}.{field}: {actual}")
                else:
                    ok = False
                    messages.append(f"FAILED {path.name}.{field}: expected {expected}, actual {actual}")
                continue
            ok = False
            messages.append(f"FAILED unknown postcondition: {cond}")
        return ok, messages

    def infer_final_extraction_csv(self) -> str:
        for step in reversed(self.steps):
            if step.get("name") != "auto_verification_layer":
                continue
            command = step.get("command", [])
            if "--extraction-csv" in command:
                idx = command.index("--extraction-csv") + 1
                if idx < len(command):
                    return str(command[idx])
        return self.final_extraction_csv

    def build_plan(self) -> None:
        ids_text = ",".join(self.sample_ids)
        if not self.current_csv:
            env = {
                "ESG_PROJECT_ROOT": str(BASE_DIR),
                "SAMPLE_JSON_PATH": str(self.args.sample_json),
                "ESG_INDICATOR_JSON": str(self.args.indicator_json),
                "OCR_CACHE_DIR": str(self.args.ocr_cache_dir),
                "PILOT_SAMPLE_IDS": ids_text,
                "PILOT_OUT_DIR": str(self.out_root),
                "PILOT_RUN_LABEL": self.args.run_label,
                "PILOT_PRIORITY": self.args.priority,
            }
            self.add_step(
                "base_extraction",
                [sys.executable, str(SCRIPTS_DIR / "run_full_extraction_v0.9.py")],
                env=env,
                timeout_sec=self.args.extraction_timeout_sec,
                note="基础抽取：规则引擎、表格、OCR缓存读取和通用KPI抽取。",
            )
        else:
            self.add_step(
                "use_existing_input_csv",
                [sys.executable, "-c", f"print(r'{self.current_csv}')"],
                timeout_sec=30,
                required=False,
                note="使用已有候选CSV作为流水线输入。",
            )

        if self.args.skip_negative_gate:
            quality_input = str(self.current_csv) if self.current_csv else LATEST_PLACEHOLDER
        else:
            if self.current_csv and "precision_gated" in self.current_csv.stem:
                quality_input = str(self.current_csv)
                self.add_step(
                    "use_existing_precision_gated_csv",
                    [sys.executable, "-c", f"print(r'{self.current_csv}')"],
                    timeout_sec=30,
                    required=False,
                    note="输入CSV已是precision gated版本，跳过负样本门控。",
                )
            else:
                gated_output = (
                    str(self.current_csv.with_name(self.current_csv.stem + "_precision_gated.csv"))
                    if self.current_csv
                    else GATED_PLACEHOLDER
                )
                self.add_step(
                    "negative_precision_gate",
                    [
                        sys.executable,
                        str(SCRIPTS_DIR / "apply_negative_precision_gate_v1.0.py"),
                        "--input",
                        str(self.current_csv) if self.current_csv else LATEST_PLACEHOLDER,
                        "--casebook",
                        str(self.args.negative_casebook),
                        "--output",
                        gated_output,
                    ],
                    timeout_sec=900,
                    note="使用不可回写负样本库做自动precision gate，再进入质量报告。",
                )
                quality_input = gated_output

        if not self.args.skip_sample_quant_reconcile:
            sample_quant_dir = self.quality_dir / "sample_quant_reconcile_v2.46"
            sample_quant_risk_csv = sample_quant_dir / "sample_quant_reconcile_risk_scores_v1.0.csv"
            sample_quant_selected_txt = sample_quant_dir / "selected_sample_ids_v1.0.txt"
            sample_quant_reconciled_csv = sample_quant_dir / "sample_quant_reconciled_guarded_v1.0.csv"
            sample_quant_reconciled_audit = sample_quant_dir / "sample_quant_reconciled_guarded_audit_v1.0.csv"
            sample_quant_eq006_csv = sample_quant_dir / "sample_quant_reconciled_eq006_only_v1.0.csv"
            sample_quant_eq006_audit = sample_quant_dir / "sample_quant_reconciled_eq006_only_audit_v1.0.csv"
            self.add_step(
                "sample_quant_reconcile_risk_select",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "select_sample_quant_reconcile_risk_v1.0.py"),
                    "--candidate-csv",
                    str(quality_input),
                    "--out-csv",
                    str(sample_quant_risk_csv),
                    "--summary-json",
                    str(sample_quant_dir / "sample_quant_reconcile_risk_summary_v1.0.json"),
                    "--sample-id-txt",
                    str(sample_quant_selected_txt),
                ],
                timeout_sec=600,
                note="Select high-risk samples for sample-level quantitative DeepSeek reconciliation using candidate-only risk features.",
            )
            sample_reconcile_cmd = [
                sys.executable,
                str(SCRIPTS_DIR / "run_deepseek_sample_quant_reconcile_v1.0.py"),
                "--candidate-csv",
                str(quality_input),
                "--indicator-csv",
                str(self.args.indicator_csv),
                "--out-dir",
                str(sample_quant_dir),
                "--run-id",
                f"{self.run_id}_sample_quant",
                "--page-text-dir",
                str(self.out_root / "extracted_page_text"),
                "--report-contexts-per-field",
                "2",
                "--report-context-radius",
                "520",
                "--context-limit",
                "60000",
                "--execute-limit",
                str(self.args.sample_quant_reconcile_limit),
                "--budget-usd",
                str(self.args.sample_quant_reconcile_budget_usd),
                "--min-apply-confidence",
                str(self.args.sample_quant_reconcile_min_confidence),
            ]
            if not self.args.sample_quant_reconcile_all_samples:
                sample_reconcile_cmd.extend(["--sample-id-file", str(sample_quant_selected_txt)])
            if self.sample_quant_deepseek_active:
                sample_reconcile_cmd.extend([
                    "--execute",
                    "--apply",
                    "--apply-output",
                    str(sample_quant_reconciled_csv),
                    "--apply-audit-csv",
                    str(sample_quant_reconciled_audit),
                ])
            self.add_step(
                "deepseek_sample_quant_reconcile",
                sample_reconcile_cmd,
                timeout_sec=self.args.deepseek_timeout_sec,
                required=self.sample_quant_deepseek_active,
                note="Sample-level quantitative reconciliation for high-risk reports; API/apply flags are added only when both --execute and --execute-sample-quant-deepseek or --execute-deepseek are set.",
            )
            if self.sample_quant_deepseek_active:
                self.add_step(
                    "sample_quant_eq006_conflict_promoter",
                    [
                        sys.executable,
                        str(SCRIPTS_DIR / "apply_quantitative_conflict_promoter_v1.0.py"),
                        "--input",
                        str(sample_quant_reconciled_csv),
                        "--output",
                        str(sample_quant_eq006_csv),
                        "--audit-csv",
                        str(sample_quant_eq006_audit),
                        "--summary-json",
                        str(sample_quant_dir / "sample_quant_reconciled_eq006_only_summary_v1.0.json"),
                        "--min-margin",
                        "18",
                        "--only-field-id",
                        "E_Q_006",
                    ],
                    timeout_sec=600,
                    note="Post-DeepSeek narrow E_Q_006 promotion for explicit total energy rows only.",
                )
                quality_input = str(sample_quant_eq006_csv)

        self.add_step(
            "quality_report",
            [
                sys.executable,
                str(SCRIPTS_DIR / "build_candidate_quality_reports_v1.0.py"),
                "--input",
                str(quality_input),
                "--out-dir",
                str(self.quality_dir),
            ],
            timeout_sec=600,
            note="生成候选来源分布、低置信队列和优先复核队列。",
        )

        output_quality_dir = self.quality_dir / "extraction_output_quality_audit_v1.0"
        output_quality_issues = output_quality_dir / "extraction_output_quality_issues_v1.0.csv"
        self.add_step(
            "extraction_output_quality_audit",
            [
                sys.executable,
                str(SCRIPTS_DIR / "audit_extraction_output_quality_v1.0.py"),
                "--extraction-csv",
                str(quality_input),
                "--indicator-csv",
                str(self.args.indicator_csv),
                "--out-dir",
                str(output_quality_dir),
            ],
            timeout_sec=600,
            required=False,
            note="只读审计输出质量，识别单位错配、数值越界、多候选等机器可发现风险。",
        )

        high_risk_dir = self.quality_dir / "high_risk_validation_queue_v1.0"
        self.add_step(
            "high_risk_validation_queue",
            [
                sys.executable,
                str(SCRIPTS_DIR / "build_high_risk_validation_queue_v1.0.py"),
                "--issues-csv",
                str(output_quality_issues),
                "--extraction-csv",
                str(quality_input),
                "--indicator-csv",
                str(self.args.indicator_csv),
                "--out-dir",
                str(high_risk_dir),
            ],
            timeout_sec=300,
            required=False,
            note="把 high 风险审计项转成 DeepSeek/规则验证优先队列，不调用API。",
        )

        field_resolution_dir = self.quality_dir / "field_level_candidate_resolution_v1.0"
        self.add_step(
            "field_level_candidate_resolution",
            [
                sys.executable,
                str(SCRIPTS_DIR / "build_field_level_candidate_resolution_v1.0.py"),
                "--extraction-csv",
                str(quality_input),
                "--indicator-csv",
                str(self.args.indicator_csv),
                "--issues-csv",
                str(output_quality_issues),
                "--out-dir",
                str(field_resolution_dir),
            ],
            timeout_sec=600,
            required=False,
            note="按 sample_id+field_id 生成 field-level 多候选归并建议，不改写主结果。",
        )

        diagnosis_csv = self.quality_dir / "低覆盖样本诊断_v1.1.csv"
        year_audit_dir = self.quality_dir / "quantitative_year_alignment_audit_v1.0"
        self.add_step(
            "quantitative_year_alignment_audit",
            [
                sys.executable,
                str(SCRIPTS_DIR / "build_gold_conflict_year_audit_v1.0.py"),
                "--extraction-csv",
                str(quality_input),
                "--indicator-csv",
                str(self.args.indicator_csv),
                "--out-dir",
                str(year_audit_dir),
                "--skip-gold-conflict",
                "--global-year-limit",
                str(self.args.year_audit_limit),
            ],
            timeout_sec=600,
            required=False,
            note="Read-only audit for quantitative multi-year table column alignment; no gold labels required.",
        )

        if self.args.gold_eval_details_csv and self.args.gold_label_csv:
            gold_conflict_dir = self.quality_dir / "gold_conflict_year_audit_v1.0"
            self.add_step(
                "gold_conflict_year_audit",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_gold_conflict_year_audit_v1.0.py"),
                    "--details-csv",
                    str(self.args.gold_eval_details_csv),
                    "--gold-csv",
                    str(self.args.gold_label_csv),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--out-dir",
                    str(gold_conflict_dir),
                    "--global-year-limit",
                    str(self.args.year_audit_limit),
                ],
                timeout_sec=600,
                required=False,
                note="Read-only gold conflict audit; separates likely gold/status conflicts from extraction defects.",
            )

        self.add_step(
            "low_coverage_diagnosis",
            [
                sys.executable,
                str(SCRIPTS_DIR / "diagnose_low_coverage_samples_v1.0.py"),
                "--quality-json",
                str(self.quality_dir / "候选来源分布_v1.0.json"),
                "--page-text-dir",
                str(self.out_root / "extracted_page_text"),
                "--output-csv",
                str(diagnosis_csv),
            ],
            timeout_sec=300,
            note="强制运行低覆盖诊断，不只看candidate_found。",
        )

        self.add_step(
            "guarded_ocr",
            [
                sys.executable,
                str(SCRIPTS_DIR / "run_guarded_ocr_from_diagnosis_v1.0.py"),
                "--diagnosis-csv",
                str(diagnosis_csv),
                "--output-csv",
                str(self.quality_dir / "guarded_ocr_run_v2.0.csv"),
                "--log-dir",
                str(self.quality_dir / "guarded_ocr_logs_v2.0"),
                "--sample-json",
                str(self.args.sample_json),
                "--ocr-json-dir",
                str(self.args.ocr_cache_dir / "ocr_page_json"),
                "--timeout-sec",
                str(self.args.ocr_timeout_sec),
            ],
            timeout_sec=max(self.args.ocr_timeout_sec * max(1, len(self.sample_ids)), 600),
            required=False,
            note="仅对诊断需要OCR/强制OCR的样本逐样本限时处理。",
        )

        regression_status = self.quality_dir / "guarded_extraction_regression_v2.0.csv"
        self.add_step(
            "guarded_ocr_regression",
            [
                sys.executable,
                str(SCRIPTS_DIR / "run_guarded_extraction_regression_v1.0.py"),
                "--diagnosis-csv",
                str(diagnosis_csv),
                "--actions",
                "run_ocr_then_regression,force_ocr_then_regression",
                "--out-root",
                str(self.out_root / "ocr_regression_per_sample"),
                "--status-csv",
                str(regression_status),
                "--sample-json",
                str(self.args.sample_json),
                "--timeout-sec",
                str(self.args.regression_timeout_sec),
            ],
            env={
                "ESG_PROJECT_ROOT": str(BASE_DIR),
                "ESG_INDICATOR_JSON": str(self.args.indicator_json),
                "OCR_CACHE_DIR": str(self.args.ocr_cache_dir),
            },
            timeout_sec=max(self.args.regression_timeout_sec * max(1, len(self.sample_ids)), 600),
            required=False,
            note="OCR完成后对相关样本做样本级回归，避免单PDF拖死整批。",
        )

        self.add_step(
            "text_rich_recall_queue",
            [
                sys.executable,
                str(SCRIPTS_DIR / "build_text_rich_recall_queue_v1.0.py"),
                "--diagnosis-csv",
                str(diagnosis_csv),
                "--extraction-csv",
                str(quality_input),
                "--page-text-dir",
                str(self.out_root / "extracted_page_text"),
                "--output-csv",
                str(self.quality_dir / "文本富集低覆盖DeepSeek召回队列_v2.0.csv"),
                "--qualitative-rules-csv",
                str(self.args.qualitative_rules_csv),
                "--max-fields-per-sample",
                str(self.args.text_rich_max_fields_per_sample),
            ],
            timeout_sec=600,
            required=False,
            note="文本可读但低覆盖样本先生成DeepSeek召回队列，不直接调用Claude。",
        )

        self.add_step(
            "deepseek_text_rich_recall",
            [
                sys.executable,
                str(SCRIPTS_DIR / "run_deepseek_text_rich_recall_budgeted_v1.0.py"),
                "--queue-csv",
                str(self.quality_dir / "文本富集低覆盖DeepSeek召回队列_v2.0.csv"),
                "--output-csv",
                str(self.quality_dir / "deepseek文本富集召回结果_v2.0.csv"),
                "--ledger",
                str(self.quality_dir / "deepseek_text_rich_recall_budget_ledger_v1.0.csv"),
                "--log-dir",
                str(self.quality_dir / "deepseek_text_rich_recall_budget_logs_v1.0"),
                "--plan-dir",
                str(self.quality_dir / "deepseek_text_rich_recall_budget_plans_v1.0"),
                "--budget-usd",
                str(self.args.deepseek_budget_usd),
                "--limit",
                str(self.args.text_rich_recall_limit),
                "--batch-size",
                str(self.args.text_rich_recall_batch_size),
                "--resume",
            ] + (["--execute"] if self.text_rich_deepseek_active else []),
            timeout_sec=self.args.deepseek_timeout_sec,
            required=self.text_rich_deepseek_active,
            note="文本富集低覆盖样本的DeepSeek召回预算守卫；API标志只在同时设置--execute和--execute-text-rich-deepseek或--execute-deepseek时加入。",
        )

        self.add_step(
            "deepseek_text_rich_recall_triage",
            [
                sys.executable,
                str(SCRIPTS_DIR / "triage_deepseek_recall_results_v1.0.py"),
                "--recall-csv",
                str(self.quality_dir / "deepseek文本富集召回结果_v2.0.csv"),
                "--queue-csv",
                str(self.quality_dir / "文本富集低覆盖DeepSeek召回队列_v2.0.csv"),
                "--indicator-csv",
                str(self.args.indicator_csv),
                "--qualitative-rules-csv",
                str(self.args.qualitative_rules_csv),
                "--out-dir",
                str(self.quality_dir / "deepseek_text_rich_recall_triage_v1.0"),
            ],
            timeout_sec=300,
            required=False,
            note="对DeepSeek text-rich召回结果做safe/review/reject三分流；不回写主结果。",
        )

        if self.text_rich_deepseek_active:
            text_rich_safe_csv = self.quality_dir / "deepseek_text_rich_recall_triage_v1.0" / "safe_recall_candidates_v1.0.csv"
            text_rich_applied_csv = self.quality_dir / "deepseek_text_rich_safe_recall_applied_guarded_v1.0.csv"
            self.add_step(
                "deepseek_text_rich_safe_recall_apply",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "apply_deepseek_recall_safe_candidates_whatif_v1.0.py"),
                    "--base-csv",
                    str(quality_input),
                    "--safe-candidates-csv",
                    str(text_rich_safe_csv),
                    "--output-csv",
                    str(text_rich_applied_csv),
                    "--guarded-production-apply",
                ],
                timeout_sec=600,
                note="Apply only triaged safe text-rich recall candidates with evidence snippets; uncertain rows stay out of production output.",
            )
            quality_input = str(text_rich_applied_csv)
            self.add_step(
                "quality_report_after_text_rich_safe_recall",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_candidate_quality_reports_v1.0.py"),
                    "--input",
                    str(quality_input),
                    "--out-dir",
                    str(self.quality_dir),
                ],
                timeout_sec=600,
                note="Refresh candidate quality reports after guarded text-rich recall writeback so downstream scoring uses final output.",
            )
            self.add_step(
                "extraction_output_quality_audit_after_text_rich_safe_recall",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "audit_extraction_output_quality_v1.0.py"),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--out-dir",
                    str(output_quality_dir),
                ],
                timeout_sec=600,
                required=False,
                note="Refresh output quality audit after guarded text-rich recall writeback.",
            )
            self.add_step(
                "high_risk_validation_queue_after_text_rich_safe_recall",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_high_risk_validation_queue_v1.0.py"),
                    "--issues-csv",
                    str(output_quality_issues),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--out-dir",
                    str(high_risk_dir),
                ],
                timeout_sec=300,
                required=False,
                note="Refresh high-risk validation queue after guarded text-rich recall writeback.",
            )
            self.add_step(
                "field_level_candidate_resolution_after_text_rich_safe_recall",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_field_level_candidate_resolution_v1.0.py"),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--issues-csv",
                    str(output_quality_issues),
                    "--out-dir",
                    str(field_resolution_dir),
                ],
                timeout_sec=600,
                required=False,
                note="Refresh field-level candidate resolution after guarded text-rich recall writeback.",
            )
            self.add_step(
                "low_coverage_diagnosis_after_text_rich_safe_recall",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "diagnose_low_coverage_samples_v1.0.py"),
                    "--quality-json",
                    str(self.quality_dir / "候选来源分布_v1.0.json"),
                    "--page-text-dir",
                    str(self.out_root / "extracted_page_text"),
                    "--output-csv",
                    str(diagnosis_csv),
                ],
                timeout_sec=300,
                note="Refresh low-coverage diagnosis after guarded text-rich recall writeback.",
            )
            self.add_step(
                "quantitative_year_alignment_audit_after_text_rich_safe_recall",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_gold_conflict_year_audit_v1.0.py"),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--out-dir",
                    str(year_audit_dir),
                    "--skip-gold-conflict",
                    "--global-year-limit",
                    str(self.args.year_audit_limit),
                ],
                timeout_sec=600,
                required=False,
                note="Refresh quantitative year-alignment audit after guarded text-rich recall writeback.",
            )

        self.add_step(
            "deepseek_priority_review",
            [
                sys.executable,
                str(SCRIPTS_DIR / "run_deepseek_review_budgeted_v1.0.py"),
                "--queue-csv",
                str(self.quality_dir / "优先复核候选队列_v1.0.csv"),
                "--output-csv",
                str(self.quality_dir / "deepseek优先复核结果_v2.0.csv"),
                "--ledger",
                str(self.quality_dir / "deepseek_budget_ledger_v1.0.csv"),
                "--log-dir",
                str(self.quality_dir / "deepseek_budget_logs_v1.0"),
                "--plan-dir",
                str(self.quality_dir / "deepseek_budget_plans_v1.0"),
                "--budget-usd",
                str(self.args.deepseek_budget_usd),
                "--limit",
                str(self.args.deepseek_limit),
                "--batch-size",
                str(self.args.deepseek_batch_size),
                "--resume",
            ],
            timeout_sec=self.args.deepseek_timeout_sec,
            required=False,
            note="对高风险/低置信候选做DeepSeek优先复核；API标志只在同时设置--execute和--execute-priority-deepseek或--execute-deepseek时加入。",
        )

        if self.priority_deepseek_active:
            high_risk_review_dir = self.quality_dir / "deepseek_high_risk_value_review_v1.0"
            high_risk_review_csv = high_risk_review_dir / "deepseek_high_risk_value_review_results_v1.0.csv"
            high_risk_review_normalized_csv = high_risk_review_dir / "deepseek_high_risk_value_review_results_normalized_v1.0.csv"
            high_risk_review_applied_csv = self.quality_dir / "deepseek_high_risk_value_reviewed_guarded_v1.0.csv"
            self.add_step(
                "deepseek_high_risk_value_review",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "run_deepseek_review_budgeted_v1.0.py"),
                    "--queue-csv",
                    str(high_risk_dir / "high_risk_validation_queue_v1.0.csv"),
                    "--output-csv",
                    str(high_risk_review_csv),
                    "--ledger",
                    str(high_risk_review_dir / "deepseek_high_risk_value_review_ledger_v1.0.csv"),
                    "--log-dir",
                    str(high_risk_review_dir / "logs"),
                    "--plan-dir",
                    str(high_risk_review_dir / "plans"),
                    "--run-id",
                    f"{self.run_id}_high_risk_value_review",
                    "--limit",
                    str(self.args.high_risk_review_limit),
                    "--batch-size",
                    str(self.args.deepseek_batch_size),
                    "--budget-usd",
                    str(self.args.deepseek_budget_usd),
                    "--resume",
                    "--execute",
                ],
                timeout_sec=self.args.deepseek_timeout_sec,
                note="Execute DeepSeek review on machine-detected high-risk output-quality candidates.",
            )
            self.add_step(
                "normalize_deepseek_high_risk_value_review",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "normalize_deepseek_priority_review_for_value_apply_v1.0.py"),
                    "--input-csv",
                    str(high_risk_review_csv),
                    "--output-csv",
                    str(high_risk_review_normalized_csv),
                ],
                timeout_sec=300,
                note="Normalize high-risk DeepSeek review output to guarded value-apply contract.",
            )
            self.add_step(
                "apply_deepseek_high_risk_value_review",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "apply_deepseek_value_review_results_v1.0.py"),
                    "--base-csv",
                    str(quality_input),
                    "--review-csv",
                    str(high_risk_review_normalized_csv),
                    "--output-csv",
                    str(high_risk_review_applied_csv),
                    "--audit-csv",
                    str(high_risk_review_dir / "deepseek_high_risk_value_review_apply_audit_v1.0.csv"),
                    "--summary-json",
                    str(high_risk_review_dir / "deepseek_high_risk_value_review_apply_summary_v1.0.json"),
                    "--min-confidence",
                    str(self.args.high_risk_review_min_confidence),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                ],
                timeout_sec=600,
                note="Apply only high-confidence parseable replacement values; reject-only rows remain audit evidence.",
            )
            quality_input = str(high_risk_review_applied_csv)
            self.add_step(
                "quality_report_after_high_risk_value_review",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_candidate_quality_reports_v1.0.py"),
                    "--input",
                    str(quality_input),
                    "--out-dir",
                    str(self.quality_dir),
                ],
                timeout_sec=600,
                note="Refresh candidate quality reports after guarded high-risk value review writeback.",
            )
            self.add_step(
                "extraction_output_quality_audit_after_high_risk_value_review",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "audit_extraction_output_quality_v1.0.py"),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--out-dir",
                    str(output_quality_dir),
                ],
                timeout_sec=600,
                required=False,
                note="Refresh output quality audit after guarded high-risk value review writeback.",
            )
            self.add_step(
                "high_risk_validation_queue_after_high_risk_value_review",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_high_risk_validation_queue_v1.0.py"),
                    "--issues-csv",
                    str(output_quality_issues),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--out-dir",
                    str(high_risk_dir),
                ],
                timeout_sec=300,
                required=False,
                note="Refresh high-risk validation queue after guarded high-risk value review writeback.",
            )
            self.add_step(
                "field_level_candidate_resolution_after_high_risk_value_review",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_field_level_candidate_resolution_v1.0.py"),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--issues-csv",
                    str(output_quality_issues),
                    "--out-dir",
                    str(field_resolution_dir),
                ],
                timeout_sec=600,
                required=False,
                note="Refresh field-level candidate resolution after guarded high-risk value review writeback.",
            )
            self.add_step(
                "low_coverage_diagnosis_after_high_risk_value_review",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "diagnose_low_coverage_samples_v1.0.py"),
                    "--quality-json",
                    str(self.quality_dir / "候选来源分布_v1.0.json"),
                    "--page-text-dir",
                    str(self.out_root / "extracted_page_text"),
                    "--output-csv",
                    str(diagnosis_csv),
                ],
                timeout_sec=300,
                note="Refresh low-coverage diagnosis after guarded high-risk value review writeback.",
            )
            self.add_step(
                "quantitative_year_alignment_audit_after_high_risk_value_review",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_gold_conflict_year_audit_v1.0.py"),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--out-dir",
                    str(year_audit_dir),
                    "--skip-gold-conflict",
                    "--global-year-limit",
                    str(self.args.year_audit_limit),
                ],
                timeout_sec=600,
                required=False,
                note="Refresh quantitative year-alignment audit after guarded high-risk value review writeback.",
            )

        if not self.args.skip_unit_scope_guard:
            unit_guard_dir = self.quality_dir / "unit_scope_guard_v1.0"
            unit_guarded_csv = self.quality_dir / "unit_scope_guarded_extraction_v1.0.csv"
            self.add_step(
                "unit_scope_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "apply_unit_scope_guard_v1.0.py"),
                    "--input-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--audit-csv",
                    str(unit_guard_dir / "unit_scope_guard_audit_v1.0.csv"),
                    "--summary-json",
                    str(unit_guard_dir / "unit_scope_guard_summary_v1.0.json"),
                    "--report-md",
                    str(unit_guard_dir / "unit_scope_guard_report_v1.0.md"),
                    "--output-csv",
                    str(unit_guarded_csv),
                ],
                timeout_sec=600,
                note="Conservative unit/scope guard before final verification; blocks clear unit-family and intensity-denominator mismatches.",
            )
            quality_input = str(unit_guarded_csv)
            self.add_step(
                "quality_report_after_unit_scope_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_candidate_quality_reports_v1.0.py"),
                    "--input",
                    str(quality_input),
                    "--out-dir",
                    str(self.quality_dir),
                ],
                timeout_sec=600,
                note="Refresh candidate quality reports after unit/scope guard.",
            )
            self.add_step(
                "extraction_output_quality_audit_after_unit_scope_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "audit_extraction_output_quality_v1.0.py"),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--out-dir",
                    str(output_quality_dir),
                ],
                timeout_sec=600,
                required=False,
                note="Refresh output-quality audit after unit/scope guard.",
            )
            self.add_step(
                "high_risk_validation_queue_after_unit_scope_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_high_risk_validation_queue_v1.0.py"),
                    "--issues-csv",
                    str(output_quality_issues),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--out-dir",
                    str(high_risk_dir),
                ],
                timeout_sec=300,
                required=False,
                note="Refresh high-risk validation queue after unit/scope guard.",
            )
            self.add_step(
                "field_level_candidate_resolution_after_unit_scope_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_field_level_candidate_resolution_v1.0.py"),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--issues-csv",
                    str(output_quality_issues),
                    "--out-dir",
                    str(field_resolution_dir),
                ],
                timeout_sec=600,
                required=False,
                note="Refresh field-level candidate resolution after unit/scope guard.",
            )
            self.add_step(
                "low_coverage_diagnosis_after_unit_scope_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "diagnose_low_coverage_samples_v1.0.py"),
                    "--quality-json",
                    str(self.quality_dir / "候选来源分布_v1.0.json"),
                    "--page-text-dir",
                    str(self.out_root / "extracted_page_text"),
                    "--output-csv",
                    str(diagnosis_csv),
                ],
                timeout_sec=300,
                note="Refresh low-coverage diagnosis after unit/scope guard.",
            )
            self.add_step(
                "quantitative_year_alignment_audit_after_unit_scope_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_gold_conflict_year_audit_v1.0.py"),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--out-dir",
                    str(year_audit_dir),
                    "--skip-gold-conflict",
                    "--global-year-limit",
                    str(self.args.year_audit_limit),
                ],
                timeout_sec=600,
                required=False,
                note="Refresh quantitative year-alignment audit after unit/scope guard.",
            )

        if not self.args.skip_year_alignment_guard:
            year_guard_dir = self.quality_dir / "year_alignment_guard_v1.0"
            year_guarded_csv = self.quality_dir / "year_alignment_guarded_extraction_v1.0.csv"
            year_guard_input = str(quality_input)
            year_guard_summary = year_guard_dir / "year_alignment_guard_summary_v1.0.json"
            self.add_step(
                "year_alignment_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "apply_year_alignment_guard_v1.0.py"),
                    "--input-csv",
                    year_guard_input,
                    "--year-audit-csv",
                    str(year_audit_dir / "quantitative_year_mismatch_candidates_v1.0.csv"),
                    "--audit-csv",
                    str(year_guard_dir / "year_alignment_guard_audit_v1.0.csv"),
                    "--summary-json",
                    str(year_guard_summary),
                    "--report-md",
                    str(year_guard_dir / "year_alignment_guard_report_v1.0.md"),
                    "--output-csv",
                    str(year_guarded_csv),
                ],
                timeout_sec=600,
                required=True,
                note="Conservative target-year table guard after unit/scope guard; also blocks stale old-year duplicates from re-entering downstream selection.",
                postconditions=[
                    {"type": "exists", "path": str(year_guarded_csv)},
                    {"type": "json_field_equals", "path": str(year_guard_summary), "field": "input_csv", "expected": year_guard_input},
                    {"type": "json_field_equals", "path": str(year_guard_summary), "field": "output_csv", "expected": str(year_guarded_csv)},
                ],
            )
            quality_input = str(year_guarded_csv)
            self.add_step(
                "quality_report_after_year_alignment_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_candidate_quality_reports_v1.0.py"),
                    "--input",
                    str(quality_input),
                    "--out-dir",
                    str(self.quality_dir),
                ],
                timeout_sec=600,
                note="Refresh candidate quality reports after year-alignment guard.",
            )
            self.add_step(
                "extraction_output_quality_audit_after_year_alignment_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "audit_extraction_output_quality_v1.0.py"),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--out-dir",
                    str(output_quality_dir),
                ],
                timeout_sec=600,
                required=False,
                note="Refresh output-quality audit after year-alignment guard.",
            )
            self.add_step(
                "high_risk_validation_queue_after_year_alignment_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_high_risk_validation_queue_v1.0.py"),
                    "--issues-csv",
                    str(output_quality_issues),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--out-dir",
                    str(high_risk_dir),
                ],
                timeout_sec=300,
                required=False,
                note="Refresh high-risk validation queue after year-alignment guard.",
            )
            self.add_step(
                "field_level_candidate_resolution_after_year_alignment_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_field_level_candidate_resolution_v1.0.py"),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--issues-csv",
                    str(output_quality_issues),
                    "--out-dir",
                    str(field_resolution_dir),
                ],
                timeout_sec=600,
                required=False,
                note="Refresh field-level candidate resolution after year-alignment guard.",
            )
            self.add_step(
                "low_coverage_diagnosis_after_year_alignment_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "diagnose_low_coverage_samples_v1.0.py"),
                    "--quality-json",
                    str(self.quality_dir / "候选来源分布_v1.0.json"),
                    "--page-text-dir",
                    str(self.out_root / "extracted_page_text"),
                    "--output-csv",
                    str(diagnosis_csv),
                ],
                timeout_sec=300,
                note="Refresh low-coverage diagnosis after year-alignment guard.",
            )
            self.add_step(
                "quantitative_year_alignment_audit_after_year_alignment_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_gold_conflict_year_audit_v1.0.py"),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--out-dir",
                    str(year_audit_dir),
                    "--skip-gold-conflict",
                    "--global-year-limit",
                    str(self.args.year_audit_limit),
                ],
                timeout_sec=600,
                required=False,
                note="Refresh quantitative year-alignment audit after year guard so auto verification consumes the final mismatch map.",
            )

        if not self.args.skip_residual_context_guard:
            residual_guard_dir = self.quality_dir / "residual_context_guard_v1.0"
            residual_guarded_csv = self.quality_dir / "residual_context_guarded_extraction_v1.0.csv"
            residual_guard_input = str(quality_input)
            residual_guard_summary = residual_guard_dir / "residual_context_guard_summary_v1.0.json"
            self.add_step(
                "residual_context_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "apply_residual_context_guard_v1.0.py"),
                    "--input-csv",
                    residual_guard_input,
                    "--audit-csv",
                    str(residual_guard_dir / "residual_context_guard_audit_v1.0.csv"),
                    "--summary-json",
                    str(residual_guard_summary),
                    "--report-md",
                    str(residual_guard_dir / "residual_context_guard_report_v1.0.md"),
                    "--output-csv",
                    str(residual_guarded_csv),
                ],
                timeout_sec=600,
                required=True,
                note="Conservative residual context guard for unsupported zero case counts, metric subcategory leakage, dropped intensity denominators, and count-vs-percent mixups.",
                postconditions=[
                    {"type": "exists", "path": str(residual_guarded_csv)},
                    {"type": "json_field_equals", "path": str(residual_guard_summary), "field": "input_csv", "expected": residual_guard_input},
                    {"type": "json_field_equals", "path": str(residual_guard_summary), "field": "output_csv", "expected": str(residual_guarded_csv)},
                ],
            )
            quality_input = str(residual_guarded_csv)
            self.final_extraction_csv = str(residual_guarded_csv)
            self.add_step(
                "quality_report_after_residual_context_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_candidate_quality_reports_v1.0.py"),
                    "--input",
                    str(quality_input),
                    "--out-dir",
                    str(self.quality_dir),
                ],
                timeout_sec=600,
                note="Refresh candidate quality reports after residual context guard.",
                postconditions=[
                    {"type": "exists", "path": str(self.quality_dir / "候选来源分布_v1.0.json")},
                    {"type": "exists", "path": str(self.quality_dir / "低置信与需复核候选队列_v1.0.csv")},
                ],
            )
            self.add_step(
                "extraction_output_quality_audit_after_residual_context_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "audit_extraction_output_quality_v1.0.py"),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--out-dir",
                    str(output_quality_dir),
                ],
                timeout_sec=600,
                required=True,
                note="Refresh output-quality audit after residual context guard.",
                postconditions=[
                    {"type": "exists", "path": str(output_quality_dir / "extraction_output_quality_summary_v1.0.json")},
                    {"type": "exists", "path": str(output_quality_issues)},
                ],
            )
            self.add_step(
                "high_risk_validation_queue_after_residual_context_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_high_risk_validation_queue_v1.0.py"),
                    "--issues-csv",
                    str(output_quality_issues),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--out-dir",
                    str(high_risk_dir),
                ],
                timeout_sec=300,
                required=True,
                note="Refresh high-risk validation queue after residual context guard.",
                postconditions=[
                    {"type": "exists", "path": str(high_risk_dir / "high_risk_validation_queue_summary_v1.0.json")},
                    {"type": "exists", "path": str(high_risk_dir / "high_risk_validation_queue_v1.0.csv")},
                ],
            )
            self.add_step(
                "field_level_candidate_resolution_after_residual_context_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_field_level_candidate_resolution_v1.0.py"),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--issues-csv",
                    str(output_quality_issues),
                    "--out-dir",
                    str(field_resolution_dir),
                ],
                timeout_sec=600,
                required=True,
                note="Refresh field-level candidate resolution after residual context guard.",
                postconditions=[
                    {"type": "exists", "path": str(field_resolution_dir / "field_level_resolution_summary_v1.0.json")},
                    {"type": "exists", "path": str(field_resolution_dir / "field_level_resolution_candidates_v1.0.csv")},
                ],
            )
            self.add_step(
                "low_coverage_diagnosis_after_residual_context_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "diagnose_low_coverage_samples_v1.0.py"),
                    "--quality-json",
                    str(self.quality_dir / "候选来源分布_v1.0.json"),
                    "--page-text-dir",
                    str(self.out_root / "extracted_page_text"),
                    "--output-csv",
                    str(diagnosis_csv),
                ],
                timeout_sec=300,
                note="Refresh low-coverage diagnosis after residual context guard.",
                postconditions=[
                    {"type": "exists", "path": str(diagnosis_csv)},
                ],
            )
            self.add_step(
                "quantitative_year_alignment_audit_after_residual_context_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_gold_conflict_year_audit_v1.0.py"),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--out-dir",
                    str(year_audit_dir),
                    "--skip-gold-conflict",
                    "--global-year-limit",
                    str(self.args.year_audit_limit),
                ],
                timeout_sec=600,
                required=True,
                note="Refresh quantitative year-alignment audit after residual context guard.",
                postconditions=[
                    {"type": "exists", "path": str(year_audit_dir / "gold_conflict_year_audit_summary_v1.0.json")},
                    {"type": "exists", "path": str(year_audit_dir / "quantitative_year_mismatch_candidates_v1.0.csv")},
                ],
            )

        if not self.args.skip_high_risk_numeric_guard:
            numeric_guard_dir = self.quality_dir / "high_risk_numeric_guard_v1.0"
            numeric_guarded_csv = self.quality_dir / "high_risk_numeric_guarded_extraction_v1.0.csv"
            numeric_guard_input = str(quality_input)
            numeric_guard_summary = numeric_guard_dir / "high_risk_numeric_guard_summary_v1.0.json"
            self.add_step(
                "high_risk_numeric_adjudication_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "apply_high_risk_numeric_adjudication_guard_v1.0.py"),
                    "--input-csv",
                    numeric_guard_input,
                    "--high-risk-queue-csv",
                    str(high_risk_dir / "high_risk_validation_queue_v1.0.csv"),
                    "--audit-csv",
                    str(numeric_guard_dir / "high_risk_numeric_guard_audit_v1.0.csv"),
                    "--summary-json",
                    str(numeric_guard_summary),
                    "--report-md",
                    str(numeric_guard_dir / "high_risk_numeric_guard_report_v1.0.md"),
                    "--output-csv",
                    str(numeric_guarded_csv),
                ],
                timeout_sec=600,
                required=True,
                note="Conservative high-risk numeric/year adjudication guard for target-year columns, percent range leaks, total-vs-submetric confusion, and GRI/index/CID numeric noise.",
                postconditions=[
                    {"type": "exists", "path": str(numeric_guarded_csv)},
                    {"type": "json_field_equals", "path": str(numeric_guard_summary), "field": "input_csv", "expected": numeric_guard_input},
                    {"type": "json_field_equals", "path": str(numeric_guard_summary), "field": "output_csv", "expected": str(numeric_guarded_csv)},
                ],
            )
            quality_input = str(numeric_guarded_csv)
            self.final_extraction_csv = str(numeric_guarded_csv)
            self.add_step(
                "quality_report_after_high_risk_numeric_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_candidate_quality_reports_v1.0.py"),
                    "--input",
                    str(quality_input),
                    "--out-dir",
                    str(self.quality_dir),
                ],
                timeout_sec=600,
                required=True,
                note="Refresh candidate quality reports after high-risk numeric guard.",
                postconditions=[
                    {"type": "exists", "path": str(self.quality_dir / "候选来源分布_v1.0.json")},
                    {"type": "exists", "path": str(self.quality_dir / "低置信与需复核候选队列_v1.0.csv")},
                ],
            )
            self.add_step(
                "extraction_output_quality_audit_after_high_risk_numeric_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "audit_extraction_output_quality_v1.0.py"),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--out-dir",
                    str(output_quality_dir),
                ],
                timeout_sec=600,
                required=True,
                note="Refresh output-quality audit after high-risk numeric guard.",
                postconditions=[
                    {"type": "exists", "path": str(output_quality_dir / "extraction_output_quality_summary_v1.0.json")},
                    {"type": "exists", "path": str(output_quality_issues)},
                ],
            )
            self.add_step(
                "high_risk_validation_queue_after_high_risk_numeric_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_high_risk_validation_queue_v1.0.py"),
                    "--issues-csv",
                    str(output_quality_issues),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--out-dir",
                    str(high_risk_dir),
                ],
                timeout_sec=300,
                required=True,
                note="Refresh high-risk validation queue after high-risk numeric guard.",
                postconditions=[
                    {"type": "exists", "path": str(high_risk_dir / "high_risk_validation_queue_summary_v1.0.json")},
                    {"type": "exists", "path": str(high_risk_dir / "high_risk_validation_queue_v1.0.csv")},
                ],
            )
            self.add_step(
                "field_level_candidate_resolution_after_high_risk_numeric_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_field_level_candidate_resolution_v1.0.py"),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--issues-csv",
                    str(output_quality_issues),
                    "--out-dir",
                    str(field_resolution_dir),
                ],
                timeout_sec=600,
                required=True,
                note="Refresh field-level candidate resolution after high-risk numeric guard.",
                postconditions=[
                    {"type": "exists", "path": str(field_resolution_dir / "field_level_resolution_summary_v1.0.json")},
                    {"type": "exists", "path": str(field_resolution_dir / "field_level_resolution_candidates_v1.0.csv")},
                ],
            )
            self.add_step(
                "low_coverage_diagnosis_after_high_risk_numeric_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "diagnose_low_coverage_samples_v1.0.py"),
                    "--quality-json",
                    str(self.quality_dir / "候选来源分布_v1.0.json"),
                    "--page-text-dir",
                    str(self.out_root / "extracted_page_text"),
                    "--output-csv",
                    str(diagnosis_csv),
                ],
                timeout_sec=300,
                required=True,
                note="Refresh low-coverage diagnosis after high-risk numeric guard.",
                postconditions=[
                    {"type": "exists", "path": str(diagnosis_csv)},
                ],
            )
            self.add_step(
                "quantitative_year_alignment_audit_after_high_risk_numeric_guard",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "build_gold_conflict_year_audit_v1.0.py"),
                    "--extraction-csv",
                    str(quality_input),
                    "--indicator-csv",
                    str(self.args.indicator_csv),
                    "--out-dir",
                    str(year_audit_dir),
                    "--skip-gold-conflict",
                    "--global-year-limit",
                    str(self.args.year_audit_limit),
                ],
                timeout_sec=600,
                required=True,
                note="Refresh quantitative year-alignment audit after high-risk numeric guard.",
                postconditions=[
                    {"type": "exists", "path": str(year_audit_dir / "gold_conflict_year_audit_summary_v1.0.json")},
                    {"type": "exists", "path": str(year_audit_dir / "quantitative_year_mismatch_candidates_v1.0.csv")},
                ],
            )

        claude_queue = self.quality_dir / "claude_vision_fallback_queue_v1.0.csv"
        ensure_claude_queue_code = (
            "from pathlib import Path\n"
            f"p=Path(r'''{claude_queue}''')\n"
            "p.parent.mkdir(parents=True, exist_ok=True)\n"
            "if not p.exists():\n"
            "    p.write_text('sample_id,field_id,fallback_reason\\n', encoding='utf-8-sig')\n"
            "print(p)\n"
        )
        self.add_step(
            "ensure_empty_claude_fallback_queue",
            [sys.executable, "-c", ensure_claude_queue_code],
            timeout_sec=30,
            required=False,
            note="Create an empty Claude fallback queue when no complex image-table cases were generated, so the budget dry-run exits cleanly.",
        )

        self.add_step(
            "claude_budgeted_fallback_dry_run",
            [
                sys.executable,
                str(SCRIPTS_DIR / "run_claude_vision_budgeted_v1.0.py"),
                "--queue-csv",
                str(claude_queue),
                "--budget-usd",
                str(self.args.claude_budget_usd),
                "--max-pages",
                str(self.args.claude_max_pages),
                "--batch-size",
                str(self.args.claude_batch_size),
                "--resume",
            ],
            timeout_sec=600,
            required=False,
            note="只有存在自动生成的复杂图片表格兜底队列时才规划Claude；默认不执行API。",
        )

        self.final_extraction_csv = str(quality_input)
        verified_csv = self.auto_verification_dir / "auto_verified_extraction_results_v1.0.csv"
        issue_csv = self.auto_verification_dir / "auto_verification_issue_queue_v1.0.csv"
        company_csv = self.scoring_dir / "company_esg_disclosure_scores_v1.0.csv"
        dimension_csv = self.scoring_dir / "company_dimension_scores_v1.0.csv"
        indicator_score_csv = self.scoring_dir / "indicator_disclosure_scores_v1.0.csv"
        self.add_step(
            "auto_verification_layer",
            [
                sys.executable,
                str(SCRIPTS_DIR / "build_auto_verification_layer_v1.0.py"),
                "--extraction-csv",
                str(quality_input),
                "--indicator-csv",
                str(self.args.indicator_csv),
                "--diagnosis-csv",
                str(diagnosis_csv),
                "--rule-flags-csv",
                str(self.args.rule_flags_csv),
                "--year-audit-csv",
                str(year_audit_dir / "quantitative_year_mismatch_candidates_v1.0.csv"),
                "--out-dir",
                str(self.auto_verification_dir),
            ],
            timeout_sec=900,
            note="生成机器自动核验状态、核验分、问题队列和规则风险标记；不宣称真实精度。",
        )
        self.add_step(
            "esg_disclosure_scoring",
            [
                sys.executable,
                str(SCRIPTS_DIR / "build_esg_disclosure_scoring_v1.0.py"),
                "--verified-csv",
                str(verified_csv),
                "--indicator-csv",
                str(self.args.indicator_csv),
                "--out-dir",
                str(self.scoring_dir),
            ],
            timeout_sec=600,
            note="基于披露完整性、证据质量和自动核验风险生成透明ESG披露评分。",
        )
        self.add_step(
            "static_dashboard",
            [
                sys.executable,
                str(SCRIPTS_DIR / "build_static_dashboard_data_v1.0.py"),
                "--company-csv",
                str(company_csv),
                "--dimension-csv",
                str(dimension_csv),
                "--indicator-csv",
                str(indicator_score_csv),
                "--verified-csv",
                str(verified_csv),
                "--issues-csv",
                str(issue_csv),
                "--out-dir",
                str(self.dashboard_dir),
            ],
            timeout_sec=600,
            note="生成可直接打开的静态HTML展示页，展示公司、指标、核验问题和ESG披露评分。",
        )

        if self.args.blind_quant_tasks_csv:
            self.add_step(
                "blind_quant_unit_normalized_eval",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "evaluate_blind_quant_validation_tasks_unit_normalized_v1.0.py"),
                    "--tasks-csv",
                    str(self.args.blind_quant_tasks_csv),
                    "--label-source",
                    str(self.args.blind_quant_label_source),
                    "--out-dir",
                    str(self.blind_quant_eval_dir),
                    "--run-id",
                    f"{self.run_id}_blind_quant_{self.args.blind_quant_label_source}_unit_normalized_eval",
                ],
                timeout_sec=600,
                required=False,
                note="可选验收：对 blind/P0 定量任务表做单位归一评估；silver 仅为代理审计信号，gold 需独立标注。",
            )

    def execute(self) -> bool:
        if not self.args.execute:
            self.execution_success = None
            return True

        for index, step in enumerate(self.steps):
            if LATEST_PLACEHOLDER in step["command_text"] or GATED_PLACEHOLDER in step["command_text"]:
                if not self.current_csv:
                    self.current_csv = latest_csv(self.out_root)
                if not self.current_csv:
                    step["status"] = "skipped_waiting_for_input_csv"
                    if step["required"]:
                        self._mark_remaining_steps_skipped(index)
                        self.execution_success = False
                        return False
                    continue
                gated_csv = self.current_csv
                if "precision_gated" not in gated_csv.stem:
                    gated_csv = gated_csv.with_name(gated_csv.stem + "_precision_gated.csv")
                step["command"] = [
                    str(self.current_csv) if part == LATEST_PLACEHOLDER else
                    str(gated_csv) if part == GATED_PLACEHOLDER else
                    part
                    for part in step["command"]
                ]
                step["command_text"] = cmd_text(step["command"], step["env"])
            try:
                step_started_at = datetime.now()
                step_started_monotonic = time.perf_counter()
                step["started_at"] = step_started_at.isoformat(timespec="seconds")
                result = run_command(step["command"], step["env"], step["timeout_sec"])
                step["finished_at"] = datetime.now().isoformat(timespec="seconds")
                step["duration_seconds"] = round(time.perf_counter() - step_started_monotonic, 3)
                step["return_code"] = result.returncode
                log_path = self.log_dir / f"{step['name']}.log"
                write_text(log_path, "STDOUT\n======\n" + result.stdout + "\n\nSTDERR\n======\n" + result.stderr)
                step["log"] = str(log_path)
                step["status"] = status(result.returncode == 0)
                if result.returncode == 0 and step.get("postconditions"):
                    post_ok, post_messages = self.check_postconditions(step)
                    append_text(
                        log_path,
                        "\n\nPOSTCONDITIONS\n==============\n" + "\n".join(post_messages) + "\n",
                    )
                    if not post_ok:
                        step["status"] = "POSTCONDITION_FAILED"
                        if step["required"]:
                            self._mark_remaining_steps_skipped(index)
                            self.execution_success = False
                            return False
                        continue
                if step["name"] == "base_extraction" and result.returncode == 0:
                    self.current_csv = latest_csv(self.out_root)
                if step["name"] == "negative_precision_gate" and result.returncode == 0:
                    if "--output" in step["command"]:
                        out_idx = step["command"].index("--output") + 1
                        self.current_csv = Path(step["command"][out_idx])
                if result.returncode != 0 and step["required"]:
                    self._mark_remaining_steps_skipped(index)
                    self.execution_success = False
                    return False
            except subprocess.TimeoutExpired as exc:
                step["finished_at"] = datetime.now().isoformat(timespec="seconds")
                step["duration_seconds"] = round(time.perf_counter() - step_started_monotonic, 3)
                log_path = self.log_dir / f"{step['name']}_timeout.log"
                write_text(log_path, "STDOUT\n======\n" + (exc.stdout or "") + "\n\nSTDERR\n======\n" + (exc.stderr or ""))
                step["log"] = str(log_path)
                step["status"] = "TIMEOUT"
                if step["required"]:
                    self._mark_remaining_steps_skipped(index)
                    self.execution_success = False
                    return False

        self.execution_success = all(
            step["status"] == "OK"
            for step in self.steps
            if step["required"]
        )
        return self.execution_success

    def _mark_remaining_steps_skipped(self, failed_index: int) -> None:
        for downstream in self.steps[failed_index + 1:]:
            if downstream["status"] == "planned":
                downstream["status"] = "SKIPPED_UPSTREAM_FAILURE"

    def write_plan(self) -> None:
        self.completed_at = datetime.now()
        self.duration_seconds = round(time.perf_counter() - self.started_monotonic, 3)
        self.final_extraction_csv = self.infer_final_extraction_csv()
        payload = {
            "run_id": self.run_id,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "completed_at": self.completed_at.isoformat(timespec="seconds"),
            "duration_seconds": self.duration_seconds,
            "execute": self.args.execute,
            "execution_success": self.execution_success,
            "sample_ids": self.sample_ids,
            "sample_json": str(self.args.sample_json),
            "out_root": str(self.out_root),
            "quality_dir": str(self.quality_dir),
            "auto_verification_dir": str(self.auto_verification_dir),
            "scoring_dir": str(self.scoring_dir),
            "dashboard_dir": str(self.dashboard_dir),
            "blind_quant_eval_dir": str(self.blind_quant_eval_dir),
            "blind_quant_tasks_csv": str(self.args.blind_quant_tasks_csv) if self.args.blind_quant_tasks_csv else "",
            "blind_quant_label_source": self.args.blind_quant_label_source,
            "deepseek_execute_modes": {
                "global_execute_deepseek_requested": bool(self.args.execute_deepseek),
                "sample_quant_requested": bool(self.execute_sample_quant_deepseek),
                "text_rich_recall_requested": bool(self.execute_text_rich_deepseek),
                "priority_review_requested": bool(self.execute_priority_deepseek),
                "sample_quant_active": bool(self.sample_quant_deepseek_active),
                "text_rich_recall_active": bool(self.text_rich_deepseek_active),
                "priority_review_active": bool(self.priority_deepseek_active),
            },
            "current_csv": str(self.current_csv) if self.current_csv else "",
            "final_extraction_csv": self.final_extraction_csv,
            "steps": self.steps,
        }
        self.plan_json.parent.mkdir(parents=True, exist_ok=True)
        self.plan_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            "# ESG自动批处理流水线计划 v2.0",
            "",
            f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"运行ID：`{self.run_id}`",
            f"模式：`{'execute' if self.args.execute else 'dry-run'}`",
            f"样本数：{len(self.sample_ids)}",
            f"输出目录：`{self.out_root}`",
            f"质量目录：`{self.quality_dir}`",
            f"自动核验目录：`{self.auto_verification_dir}`",
            f"ESG披露评分目录：`{self.scoring_dir}`",
            f"静态展示目录：`{self.dashboard_dir}`",
            f"blind定量评估目录：`{self.blind_quant_eval_dir}`",
            f"最终抽取CSV：`{self.final_extraction_csv}`",
            "",
            "## 步骤",
            "",
            "| # | 步骤 | 状态 | 必需 | 说明 |",
            "|---:|---|---|---|---|",
        ]
        for idx, step in enumerate(self.steps, 1):
            lines.append(f"| {idx} | `{step['name']}` | {step['status']} | {step['required']} | {step['note']} |")
        lines.extend(["", "## 命令", ""])
        for idx, step in enumerate(self.steps, 1):
            lines.extend([f"### {idx}. {step['name']}", "", "```powershell", step["command_text"], "```", ""])
        self.plan_md.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", required=True, help="Comma list or range, e.g. R001-R100")
    parser.add_argument("--sample-json", type=Path, default=DEFAULT_SAMPLE_JSON)
    parser.add_argument("--input-csv", type=Path, help="Use existing extraction CSV and skip base extraction")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--out-root", type=Path)
    parser.add_argument("--quality-dir", type=Path)
    parser.add_argument("--plan-dir", type=Path, default=DEFAULT_PLAN_DIR)
    parser.add_argument("--negative-casebook", type=Path, default=DEFAULT_NEGATIVE_CASEBOOK)
    parser.add_argument("--indicator-csv", type=Path, default=DEFAULT_INDICATOR)
    parser.add_argument("--indicator-json", type=Path, default=DEFAULT_INDICATOR_JSON)
    parser.add_argument("--rule-flags-csv", type=Path, default=DEFAULT_RULE_FLAGS)
    parser.add_argument("--qualitative-rules-csv", type=Path, default=DEFAULT_QUALITATIVE_RULES)
    parser.add_argument("--ocr-cache-dir", type=Path, default=DEFAULT_OCR_CACHE_DIR)
    parser.add_argument("--gold-eval-details-csv", type=Path)
    parser.add_argument("--gold-label-csv", type=Path)
    parser.add_argument("--year-audit-limit", type=int, default=0, help="0 means no limit")
    parser.add_argument("--auto-verification-dir", type=Path)
    parser.add_argument("--scoring-dir", type=Path)
    parser.add_argument("--dashboard-dir", type=Path)
    parser.add_argument("--blind-quant-tasks-csv", type=Path)
    parser.add_argument("--blind-quant-label-source", choices=["gold", "silver"], default="gold")
    parser.add_argument("--blind-quant-eval-dir", type=Path)
    parser.add_argument("--skip-negative-gate", action="store_true")
    parser.add_argument("--skip-unit-scope-guard", action="store_true")
    parser.add_argument("--skip-year-alignment-guard", action="store_true")
    parser.add_argument("--skip-residual-context-guard", action="store_true")
    parser.add_argument("--skip-high-risk-numeric-guard", action="store_true")
    parser.add_argument("--run-label", default="ESG指标自动抽取")
    parser.add_argument("--priority", default="all")
    parser.add_argument("--deepseek-limit", type=int, default=500)
    parser.add_argument("--deepseek-batch-size", type=int, default=5)
    parser.add_argument("--deepseek-budget-usd", type=float, default=10.0)
    parser.add_argument("--text-rich-recall-limit", type=int, default=700)
    parser.add_argument("--text-rich-max-fields-per-sample", type=int, default=24)
    parser.add_argument("--text-rich-recall-batch-size", type=int, default=4)
    parser.add_argument("--skip-sample-quant-reconcile", action="store_true")
    parser.add_argument("--sample-quant-reconcile-limit", type=int, default=80)
    parser.add_argument("--sample-quant-reconcile-budget-usd", type=float, default=10.0)
    parser.add_argument("--sample-quant-reconcile-min-confidence", type=float, default=0.90)
    parser.add_argument(
        "--sample-quant-reconcile-all-samples",
        action="store_true",
        help="Run report-level quantitative reconciliation for every input sample instead of only the risk selector output.",
    )
    parser.add_argument("--high-risk-review-limit", type=int, default=80)
    parser.add_argument("--high-risk-review-min-confidence", type=float, default=0.90)
    parser.add_argument("--execute-deepseek", action="store_true", help="Allow budgeted DeepSeek API calls during --execute")
    parser.add_argument("--execute-sample-quant-deepseek", action="store_true", help="Allow only sample-level quantitative DeepSeek reconciliation during --execute")
    parser.add_argument("--execute-text-rich-deepseek", action="store_true", help="Allow only text-rich low-coverage DeepSeek recall during --execute")
    parser.add_argument("--execute-priority-deepseek", action="store_true", help="Allow only high-risk priority DeepSeek review during --execute")
    parser.add_argument("--claude-budget-usd", type=float, default=30.0)
    parser.add_argument("--claude-max-pages", type=int, default=4)
    parser.add_argument("--claude-batch-size", type=int, default=2)
    parser.add_argument("--extraction-timeout-sec", type=int, default=7200)
    parser.add_argument("--ocr-timeout-sec", type=int, default=2400)
    parser.add_argument("--regression-timeout-sec", type=int, default=900)
    parser.add_argument("--deepseek-timeout-sec", type=int, default=7200)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    path_arguments = (
        "sample_json", "input_csv", "out_root", "quality_dir", "plan_dir",
        "negative_casebook", "indicator_csv", "indicator_json", "rule_flags_csv",
        "qualitative_rules_csv", "gold_eval_details_csv", "gold_label_csv",
        "auto_verification_dir", "scoring_dir", "dashboard_dir",
        "blind_quant_tasks_csv", "blind_quant_eval_dir", "ocr_cache_dir",
    )
    for argument_name in path_arguments:
        value = getattr(args, argument_name, None)
        if value is not None:
            setattr(args, argument_name, value.resolve())

    requested_sample_ids = split_sample_ids(args.samples)
    if not requested_sample_ids:
        parser.error("--samples must resolve to at least one sample ID")
    try:
        validate_sample_manifest(args.sample_json, requested_sample_ids)
    except ValueError as exc:
        parser.error(str(exc))

    pipeline = Pipeline(args)
    pipeline.build_plan()
    execution_success = pipeline.execute()
    pipeline.write_plan()
    print(json.dumps({
        "run_id": pipeline.run_id,
        "execute": args.execute,
        "sample_count": len(pipeline.sample_ids),
        "plan_json": str(pipeline.plan_json),
        "plan_md": str(pipeline.plan_md),
        "out_root": str(pipeline.out_root),
        "quality_dir": str(pipeline.quality_dir),
        "auto_verification_dir": str(pipeline.auto_verification_dir),
        "scoring_dir": str(pipeline.scoring_dir),
        "dashboard_dir": str(pipeline.dashboard_dir),
        "blind_quant_eval_dir": str(pipeline.blind_quant_eval_dir),
    }, ensure_ascii=False, indent=2))
    if args.execute and not execution_success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
