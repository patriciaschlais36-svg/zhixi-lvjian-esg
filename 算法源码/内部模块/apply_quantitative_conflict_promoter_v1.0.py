# -*- coding: utf-8 -*-
"""Promote safer quantitative candidates when the same field has conflicts.

This processor is intentionally conservative and production-safe:
- It reads only extraction candidates and indicator metadata.
- It never reads gold labels.
- It does not invent values; it only promotes an already extracted candidate
  in the same sample_id + field_id group.
- It acts only when the currently selected candidate has a clear risk signal
  and another candidate has stronger field-specific evidence.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
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

# These two fields frequently need real row/column structure recovery rather
# than candidate promotion. Simple promotion fixed no labeled cases in the
# current audit and introduced risk, so they are left to the table parser.
DISABLED_PROMOTION_FIELDS = {"E_Q_009"}

FIELD_ALIASES = {
    "E_Q_001": ["温室气体排放总量", "范围一与范围二", "范围一和范围二", "排放总量"],
    "E_Q_002": ["范围一温室气体排放", "范围一（直接）", "范围一直接", "直接温室气体排放", "直接排放"],
    "E_Q_003": ["范围二温室气体排放", "范围二（间接）", "范围二间接", "间接温室气体排放", "间接排放"],
    "E_Q_005": ["温室气体排放强度", "碳排放强度", "排放强度"],
    "E_Q_006": ["综合能源消耗量", "综合能耗", "能源消耗总量", "综合能耗消耗量", "综合能源消耗"],
    "E_Q_007": ["外购电力消耗量", "外购电力", "外购电量", "用电量", "耗电量", "总电耗"],
    "E_Q_009": ["取水量", "总取水量", "总耗水量", "耗水量", "用水量", "新鲜水用水量", "水资源使用量"],
    "E_Q_012": ["废弃物产生总量", "废弃物总量", "总废弃物量", "固体废弃物总量", "一般固体废弃物排放总量"],
    "E_Q_013": ["危险废弃物产生量", "危险废弃物排放总量", "有害废弃物排放量", "有害废弃物总量", "危废总量", "危险废物处理量"],
    "E_Q_015": ["环保投入金额", "环保投入", "环境保护投入", "环保投资"],
    "S_Q_001": ["员工总数", "雇员总数", "员工人数"],
    "S_Q_002": ["女性员工比例", "女性员工占比", "女性雇员比例", "女性占比"],
    "S_Q_004": ["员工培训总时长", "培训总时长", "培训总学时", "培训总小时", "员工培训总小时"],
    "S_Q_005": ["人均培训时长", "人均培训学时", "人均培训", "每年人均接受培训", "平均培训时长"],
    "S_Q_008": ["工伤率", "可记录工伤率", "损工事故率"],
    "S_Q_009": ["因工死亡人数", "工亡人数", "零工亡", "无工亡", "死亡人数"],
    "S_Q_017": ["公益捐赠", "公益投入", "社区投入", "对外捐赠", "乡村振兴总投入"],
    "G_Q_001": ["董事会人数", "董事人数", "董事总数"],
    "G_Q_002": ["独立董事人数", "独立非执行董事人数", "独董人数"],
    "G_Q_003": ["独立董事比例", "独立董事占比", "独董占比", "董事会独立性"],
    "G_Q_009": ["反腐败培训", "反腐倡廉培训", "反商业贿赂", "廉洁培训"],
    "G_Q_010": ["腐败案件", "违规案件", "违法违规案件", "贪污诉讼", "重大行政处罚", "诉讼或重大行政处罚"],
}

NEGATIVE_SCOPE_TOKENS = [
    "总行深圳", "深圳场地", "分行", "试点单位", "项目", "案例", "目标", "完成情况",
    "同比", "下降", "减少", "厨余", "循环利用", "资源循环", "密度", "强度", "人均",
]


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def parse_float(value: Any, default: float = 0.0) -> float:
    text = str(value or "").replace(",", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return default
    try:
        return float(match.group(0))
    except ValueError:
        return default


def parse_rank(value: Any) -> int:
    try:
        return int(float(str(value or "").strip()))
    except ValueError:
        return 999


def extraction_score(row: dict[str, str]) -> tuple[int, float, int]:
    status_score = 1 if row.get("candidate_status") == "candidate_found" else 0
    return status_score, parse_float(row.get("confidence_rule"), 0.0), 1 if parse_rank(row.get("candidate_rank")) == 1 else 0


def compact_text(row: dict[str, str]) -> str:
    return re.sub(r"\s+", "", f"{row.get('source_text', '')} {row.get('source_table_cell', '')}")


def loose_text(row: dict[str, str]) -> str:
    return re.sub(r"\s+", " ", f"{row.get('source_text', '')} {row.get('source_table_cell', '')}").strip()


def aliases_for(row: dict[str, str]) -> list[str]:
    field_id = row.get("field_id", "")
    aliases = list(FIELD_ALIASES.get(field_id, []))
    metric = str(row.get("metric_name_cn") or "").strip()
    if metric:
        aliases.insert(0, metric)
    seen: list[str] = []
    for alias in aliases:
        alias = re.sub(r"\s+", "", alias)
        if alias and alias not in seen:
            seen.append(alias)
    return sorted(seen, key=len, reverse=True)


def value_string(row: dict[str, str]) -> str:
    return str(row.get("value_candidate") or "").replace(",", "").strip()


def local_window(row: dict[str, str], radius: int = 120) -> str:
    text = compact_text(row)
    value = value_string(row)
    positions: list[int] = []
    if value:
        positions.extend(m.start() for m in re.finditer(re.escape(value), text))
    for alias in aliases_for(row):
        pos = text.find(alias)
        if pos >= 0:
            positions.append(pos)
            break
    if not positions:
        return text[: max(radius * 2, 240)]
    pos = min(positions)
    return text[max(0, pos - radius): pos + radius]


def has_alias(row: dict[str, str]) -> bool:
    text = compact_text(row)
    return any(alias in text for alias in aliases_for(row))


def unit_text(row: dict[str, str]) -> str:
    return str(row.get("unit_raw_candidate") or row.get("unit_standardized_candidate") or "").replace(" ", "")


def percent_value(row: dict[str, str]) -> float | None:
    unit = unit_text(row)
    if "%" not in unit and "比例" not in compact_text(row) and "占比" not in compact_text(row):
        return None
    return parse_float(row.get("value_candidate"), default=float("nan"))


def scope_penalty(row: dict[str, str]) -> int:
    text = local_window(row, 160)
    return sum(1 for token in NEGATIVE_SCOPE_TOKENS if token in text)


def explicit_zero_signal(field_id: str, row: dict[str, str]) -> bool:
    if parse_float(row.get("value_candidate"), -999.0) != 0:
        return False
    text = loose_text(row)
    if field_id == "S_Q_009":
        return bool(re.search(r"(零|无|未发生|未出现).{0,12}(工亡|死亡|亡人|生产安全事故)", text))
    if field_id == "G_Q_010":
        return bool(re.search(r"(零|无|未发生|未出现).{0,20}(腐败|贪污|商业贿赂|违法违规|重大行政处罚|诉讼)", text))
    return False


def risk_signal(current: dict[str, str]) -> bool:
    field_id = current.get("field_id", "")
    text = local_window(current, 180)
    method = str(current.get("value_extraction_method") or "").lower()
    val = parse_float(current.get("value_candidate"), 0.0)
    if "llm_review_modify" in method and parse_float(current.get("rule_score"), 0.0) < 60:
        return True
    if field_id in {"E_Q_012", "E_Q_013", "E_Q_009"} and scope_penalty(current) >= 1:
        return True
    if field_id == "S_Q_004" and "志愿服务" in text:
        return True
    if field_id == "S_Q_005" and any(token in text for token in ["管理层", "中层", "男员工", "女员工", "普通员工"]):
        return True
    if field_id == "S_Q_005" and "人" in unit_text(current) and not any(token in unit_text(current) for token in ["小时", "学时"]):
        return True
    if field_id in {"S_Q_002", "G_Q_003"}:
        pv = percent_value(current)
        if pv is not None and pv > 100:
            return True
    if field_id == "G_Q_010" and any(token in text for token in ["培训", "覆盖人次", "董事总数"]):
        return True
    if field_id == "S_Q_009" and val > 0 and any(token in text for token in ["培训", "演练", "安全投入", "员工总数"]):
        return True
    if field_id == "E_Q_006" and any(token in text for token in ["直接能源", "间接能源", "天然气", "总电耗"]) and "综合" not in text:
        return True
    if field_id in {"E_Q_002", "E_Q_003"} and any(token in text for token in ["范围三", "地区", "类别", "总行"]):
        return True
    return False


def field_evidence_score(row: dict[str, str]) -> int:
    field_id = row.get("field_id", "")
    text = local_window(row, 180)
    full = compact_text(row)
    value = parse_float(row.get("value_candidate"), 0.0)
    score = 0
    if has_alias(row):
        score += 20
    score += min(20, int(parse_float(row.get("rule_score"), 0.0) / 8))
    score += min(15, int(parse_float(row.get("confidence_rule"), 0.0) * 15))

    if field_id == "E_Q_002":
        if "范围一" in text or "直接" in text:
            score += 24
        if "范围二" in text or "范围三" in text or "总量" in text or "地区" in text:
            score -= 35
    elif field_id == "E_Q_003":
        if "范围二" in text or "间接" in text:
            score += 24
        if "范围一" in text or "范围三" in text or "总量" in text or "地区" in text:
            score -= 35
    elif field_id == "E_Q_006":
        if any(token in text for token in ["综合能耗", "综合能源", "能源消耗总量", "综合能耗消耗量"]):
            score += 32
        if any(token in text for token in ["直接能源", "间接能源", "总电耗", "天然气", "外购电力"]) and "综合" not in text:
            score -= 30
    elif field_id == "E_Q_009":
        if any(token in text for token in ["总耗水量", "取水量", "新鲜水", "水资源使用量"]):
            score += 26
        if any(token in text for token in ["密度", "强度", "电力", "天然气"]):
            score -= 30
    elif field_id == "E_Q_012":
        if any(token in text for token in ["废弃物总量", "废弃物产生总量", "固体废弃物"]):
            score += 30
        if any(token in text for token in ["厨余", "循环利用", "同比", "目标", "总行深圳", "深圳场地"]):
            score -= 45
    elif field_id == "E_Q_013":
        if any(token in text for token in ["危险废弃物", "危险废物", "有害废弃物", "危废"]):
            score += 34
        if "无害" in text and not any(token in text for token in ["有害", "危险", "危废"]):
            score -= 45
        if "万吨" in unit_text(row) and value > 100:
            score -= 20
    elif field_id == "S_Q_002":
        if any(token in text for token in ["女性员工占比", "女性员工比例", "女性占比"]):
            score += 32
        pv = percent_value(row)
        if pv is not None and pv > 100:
            score -= 60
    elif field_id == "S_Q_004":
        if any(token in text for token in ["员工培训总时长", "培训总时长", "培训总学时"]):
            score += 34
        if "志愿服务" in text:
            score -= 60
    elif field_id == "S_Q_005":
        if any(token in text for token in ["全体员工每年人均", "人均培训", "人均培训学时", "平均小时数"]):
            score += 34
        if any(token in text for token in ["管理层", "中层", "男员工", "女员工", "普通员工"]):
            score -= 30
        if not any(token in unit_text(row) for token in ["小时", "学时"]):
            score -= 45
    elif field_id == "S_Q_009":
        if explicit_zero_signal(field_id, row):
            score += 55
        if value > 0 and any(token in full for token in ["零工亡", "无工亡", "未发生死亡"]):
            score -= 35
    elif field_id == "G_Q_010":
        if explicit_zero_signal(field_id, row):
            score += 55
        if any(token in text for token in ["培训次数", "培训覆盖", "覆盖人次", "董事总数"]):
            score -= 45
    elif field_id == "G_Q_009":
        if any(token in text for token in ["反腐", "反商业贿赂", "反贪污", "廉洁"]):
            score += 24
        if "消费者权益" in text or "合规销售" in text:
            score -= 35

    score -= scope_penalty(row) * 6
    if unit_text(row):
        score += 4
    return score


def promote_candidate(group: list[dict[str, str]], min_margin: int) -> dict[str, Any] | None:
    found = [row for row in group if row.get("candidate_status") == "candidate_found"]
    if len(found) < 2:
        return None
    field_id = found[0].get("field_id", "")
    if field_id not in P0_QUANT_FIELDS:
        return None
    if field_id in DISABLED_PROMOTION_FIELDS:
        return None
    current = max(found, key=extraction_score)
    has_explicit_zero_alternative = (
        field_id in {"S_Q_009", "G_Q_010"}
        and parse_float(current.get("value_candidate"), 0.0) > 0
        and any(row is not current and explicit_zero_signal(field_id, row) for row in found)
    )
    has_eq006_total_alternative = (
        field_id == "E_Q_006"
        and "综合能耗消耗量" not in local_window(current, 220)
        and any(row is not current and "综合能耗消耗量" in local_window(row, 220) for row in found)
    )
    if not risk_signal(current) and not has_explicit_zero_alternative and not has_eq006_total_alternative:
        return None
    scored = sorted(((field_evidence_score(row), row) for row in found), key=lambda item: item[0], reverse=True)
    best_score, best = scored[0]
    current_score = field_evidence_score(current)
    if best is current:
        return None
    if field_id == "E_Q_006" and "综合能耗消耗量" not in local_window(best, 220):
        return None
    if best_score - current_score < min_margin or best_score < 45:
        return None

    before = {
        "value": current.get("value_candidate", ""),
        "unit": current.get("unit_raw_candidate", ""),
        "confidence": current.get("confidence_rule", ""),
        "rank": current.get("candidate_rank", ""),
        "method": current.get("value_extraction_method", ""),
        "score": current_score,
    }
    best["candidate_rank"] = "1"
    best["confidence_rule"] = f"{max(parse_float(best.get('confidence_rule'), 0.0), 0.992):.3f}"
    best["review_reason"] = (best.get("review_reason", "") + "; promoted_by_quantitative_conflict_promoter_v1.0").strip("; ")
    best["extractor_version"] = "quantitative_conflict_promoter_v1.0"
    rank = 2
    for _, row in scored:
        if row is best:
            continue
        row["candidate_rank"] = str(rank)
        if row is current:
            row["confidence_rule"] = f"{min(parse_float(row.get('confidence_rule'), 0.0), 0.650):.3f}"
            row["review_reason"] = (row.get("review_reason", "") + "; demoted_by_quantitative_conflict_promoter_v1.0").strip("; ")
        rank += 1
    return {
        "sample_id": best.get("sample_id", ""),
        "field_id": field_id,
        "metric_name_cn": best.get("metric_name_cn", ""),
        "before_value": before["value"],
        "before_unit": before["unit"],
        "before_confidence": before["confidence"],
        "before_rank": before["rank"],
        "before_method": before["method"],
        "before_evidence_score": before["score"],
        "after_value": best.get("value_candidate", ""),
        "after_unit": best.get("unit_raw_candidate", ""),
        "after_confidence": best.get("confidence_rule", ""),
        "after_rank": best.get("candidate_rank", ""),
        "after_method": best.get("value_extraction_method", ""),
        "after_evidence_score": best_score,
        "score_margin": best_score - current_score,
        "risk_window": local_window(current, 160),
        "promotion_window": local_window(best, 160),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audit-csv", type=Path)
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--min-margin", type=int, default=18)
    parser.add_argument("--only-field-id", default="", help="Optional semicolon/comma-separated field allowlist")
    args = parser.parse_args()

    rows, fields = read_csv(args.input)
    only_fields = {item.strip() for item in re.split(r"[;,，\s]+", args.only_field_id or "") if item.strip()}
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[(row.get("sample_id", ""), row.get("field_id", ""))].append(row)

    audit: list[dict[str, Any]] = []
    for group in groups.values():
        if only_fields and group and group[0].get("field_id", "") not in only_fields:
            continue
        promoted = promote_candidate(group, args.min_margin)
        if promoted:
            audit.append(promoted)

    write_csv(args.output, rows, fields)
    audit_path = args.audit_csv or args.output.with_name(args.output.stem + "_audit.csv")
    summary_path = args.summary_json or args.output.with_name(args.output.stem + "_summary.json")
    audit_fields = [
        "sample_id", "field_id", "metric_name_cn",
        "before_value", "before_unit", "before_confidence", "before_rank", "before_method", "before_evidence_score",
        "after_value", "after_unit", "after_confidence", "after_rank", "after_method", "after_evidence_score",
        "score_margin", "risk_window", "promotion_window",
    ]
    write_csv(audit_path, audit, audit_fields)
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input": str(args.input),
        "output": str(args.output),
        "audit_csv": str(audit_path),
        "row_count": len(rows),
        "promoted_groups": len(audit),
        "field_counts": dict(Counter(row["field_id"] for row in audit)),
        "min_margin": args.min_margin,
        "policy": "same-field candidate promotion only; no gold labels; clear risk signal required",
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
