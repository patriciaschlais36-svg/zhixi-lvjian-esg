# -*- coding: utf-8 -*-
"""
ESG 报告指标索引页反向定位模块 v0.9

功能：
1. 解析 OCR 后的指标索引页，提取（章节名称 → 报告页码）映射
2. 将章节主题映射到 ESG 指标 field_id
3. 提供目标页码建议，供抽取器优先检索

设计原则：
- 使用 OCR JSON 中的 x 坐标分离索引表（右栏）和正文（左栏）
- 不依赖 LLM，完全基于规则和模糊匹配
- 可复现、可审计
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    from rapidfuzz import fuzz
except ImportError:
    fuzz = None


# ── 指标 → 索引页章节主题词映射 ──────────────────────────────
# 每个 field_id 对应一组在索引页中可能出现的章节名称关键词

INDICATOR_INDEX_TOPIC_MAP: dict[str, list[str]] = {
    # 环境 — 气候与排放
    "E_Q_001": ["温室气体排放", "碳排放", "应对气候变化", "气候", "排放"],
    "E_Q_002": ["范围一", "直接排放", "温室气体排放", "排放"],
    "E_Q_003": ["范围二", "间接排放", "能源间接", "温室气体排放", "排放"],
    "E_Q_004": ["范围三", "价值链排放", "供应链排放"],
    "E_Q_005": ["排放强度", "碳排放强度", "温室气体排放强度"],
    "E_Q_006": ["能源消耗", "能源利用", "能源管理", "综合能耗", "节能"],
    "E_Q_007": ["电力", "用电", "外购电力", "能源消耗", "能源利用"],
    "E_Q_008": ["可再生能源", "绿电", "清洁能源", "能源结构"],
    "E_Q_009": ["水资源", "用水", "取水", "水资源利用"],
    "E_Q_010": ["循环用水", "回用水", "中水", "重复用水", "水资源"],
    "E_Q_011": ["废水排放", "污水", "排水", "水污染物", "污染物排放"],
    "E_Q_012": ["废弃物", "固体废物", "固废", "一般废弃物", "无害废弃物", "污染物排放"],
    "E_Q_013": ["危险废弃物", "危废", "有害废弃物", "危险废物", "废弃物"],
    "E_Q_014": ["一般废弃物", "无害废弃物", "非危险废弃物", "固体废物", "废弃物"],
    "E_Q_015": ["环保投入", "环保投资", "节能环保投入", "环境投入", "环保支出"],
    "E_Q_016": ["主要污染物", "SO2", "NOx", "COD", "氨氮", "颗粒物", "大气污染物"],
    "E_Q_017": ["绿色金融", "绿色贷款", "绿色债券", "绿色收入", "绿色业务"],
    "E_Q_018": ["减排", "碳减排", "避免排放", "减排量", "温室气体"],
    "E_Q_019": ["碳配额", "碳交易", "碳资产", "CCER"],
    "E_Q_020": ["气候投入", "低碳转型投入", "气候相关资本", "转型投资"],

    # 环境 — 定性（治理与管理）
    "E_T_001": ["气候变化战略", "气候战略", "低碳战略", "绿色发展战略", "双碳战略", "应对气候变化"],
    "E_T_002": ["碳中和目标", "碳达峰", "减排目标", "双碳目标", "气候目标"],
    "E_T_003": ["气候风险", "物理风险", "转型风险", "气候风险管理", "气候相关风险"],
    "E_T_004": ["情景分析", "气候情景", "压力测试"],
    "E_T_005": ["环境管理", "环境管理体系", "ISO14001", "环保管理", "环境合规"],
    "E_T_006": ["生物多样性", "生态保护", "生态修复", "自然保护"],
    "E_T_007": ["循环经济", "资源循环", "回收利用", "绿色包装"],
    "E_T_008": ["绿色供应链", "绿色采购", "供应商环保", "供应链环境"],
    "E_T_009": ["污染防治", "废气治理", "废水治理", "固废治理", "排放达标", "污染物排放"],
    "E_T_010": ["水资源管理", "节水", "水风险", "污水处理"],

    # 社会 — 定量
    "S_Q_001": ["员工", "雇员", "员工总数", "员工发展与权益保障"],
    "S_Q_002": ["女性员工", "女性比例", "性别", "多元化", "员工"],
    "S_Q_003": ["女性管理", "女性高管", "管理层女性"],
    "S_Q_004": ["培训", "培训总时长", "培训小时", "员工培训", "员工发展与权益保障"],
    "S_Q_005": ["人均培训", "培训时长", "员工培训"],
    "S_Q_006": ["培训覆盖", "培训率", "培训人数", "员工培训"],
    "S_Q_007": ["员工流失", "离职率", "流失率"],
    "S_Q_008": ["工伤", "工伤率", "安全", "安全生产", "职业健康"],
    "S_Q_009": ["工亡", "死亡事故", "安全", "安全生产", "职业健康"],
    "S_Q_010": ["安全培训", "安全", "职业健康"],
    "S_Q_011": ["研发投入", "研发费用", "创新", "创新驱动"],
    "S_Q_012": ["研发人员", "研发", "创新", "创新驱动"],
    "S_Q_013": ["供应商", "供应链安全与责任"],
    "S_Q_014": ["供应商ESG", "供应商审核", "供应商评估", "供应链"],
    "S_Q_015": ["客户投诉", "投诉", "产品和服务安全与质量"],
    "S_Q_016": ["投诉解决", "客户满意", "客户"],
    "S_Q_017": ["公益", "捐赠", "社会贡献", "慈善", "社区", "乡村振兴"],

    # 社会 — 定性
    "S_T_001": ["员工满意", "员工敬业", "员工", "员工发展与权益保障"],
    "S_T_002": ["职业健康安全", "安全生产", "安全", "员工"],
    "S_T_003": ["员工权益", "劳动权益", "员工", "员工发展与权益保障"],
    "S_T_004": ["供应链管理", "供应商", "供应链安全与责任"],
    "S_T_005": ["产品责任", "产品安全", "质量", "产品和服务安全与质量"],
    "S_T_006": ["客户服务", "客户关系", "客户"],
    "S_T_007": ["社区关系", "社区", "乡村振兴", "乡村振兴与社区共建"],
    "S_T_008": ["数据安全", "隐私", "信息安全"],

    # 治理 — 定量
    "G_Q_001": ["董事会人数", "董事会规模", "董事", "公司治理"],
    "G_Q_002": ["独立董事", "独董", "董事", "公司治理"],
    "G_Q_003": ["独立董事比例", "独董占比", "董事", "公司治理"],
    "G_Q_004": ["女性董事", "女性董事人数", "董事", "公司治理"],
    "G_Q_005": ["女性董事比例", "女性董事占比", "董事", "公司治理"],
    "G_Q_006": ["董事会会议", "董事会", "公司治理"],
    "G_Q_007": ["股东大会", "股东", "公司治理"],
    "G_Q_008": ["投资者沟通", "投资者关系", "信息披露与投资者关系"],
    "G_Q_009": ["反腐败培训", "廉洁培训", "反腐", "合规经营与商业道德"],
    "G_Q_010": ["腐败案件", "贪污", "贿赂", "违规", "合规经营与商业道德"],
    "G_Q_011": ["监事会", "监事"],

    # 治理 — 定性
    "G_T_001": ["ESG治理", "可持续发展治理", "治理架构"],
    "G_T_002": ["董事会ESG", "董事会监督", "董事会可持续发展", "公司治理"],
    "G_T_003": ["利益相关方", "利益相关方沟通", "利益相关者"],
    "G_T_004": ["内控", "合规管理", "内部控制", "合规", "风险治理"],
    "G_T_005": ["商业道德", "反腐", "反贿赂", "反贪污", "廉洁", "合规经营与商业道德"],
    "G_T_006": ["风险管理", "风险治理", "风险管控", "风险治理与管控"],
    "G_T_007": ["信息披露", "信息披露与投资者关系"],
    "G_T_008": ["税务", "税收"],
    "G_T_009": ["重大性议题", "实质性议题", "实质性议题分析"],
    "G_T_010": ["党建", "党建引领"],
}


def parse_index_page_from_ocr_json(ocr_json_path: Path) -> list[dict[str, Any]]:
    """
    从 OCR JSON 文件中解析指标索引页。

    使用 x 坐标分离右栏索引表（x > 900）和左栏正文。
    索引表结构：维度 | 章节目录 | 上交所指示 | GRI标准 | 页码

    Returns:
        [{"section_name": "水资源利用", "report_page": 32, "dimension": "环境"}, ...]
    """
    try:
        payload = json.loads(ocr_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return []

    lines = payload.get("lines") or []
    if not lines:
        # Fallback: parse plain text
        text = payload.get("ocr_text", "")
        return _parse_index_from_plain_text(text)

    # ── 第1步：分离右栏（索引表）行 ──
    right_lines: list[dict[str, Any]] = []
    for line in lines:
        box = line.get("box")
        if not box:
            continue
        # 右栏 x 起点 > 900
        if isinstance(box[0], (list, tuple)) and box[0][0] > 900:
            right_lines.append(line)

    if not right_lines:
        return []

    # ── 第2步：按 y 坐标排序右栏行 ──
    right_lines.sort(key=lambda ln: ln["box"][0][1] if isinstance(ln["box"][0], (list, tuple)) else 0)

    # ── 第3步：识别维度标签 ──
    current_dimension = ""
    dimension_labels = {"环境": "E", "社会": "S", "治理": "G",
                        "可持续": "G", "经济": "G"}

    # ── 第4步：提取页码行 ──
    entries: list[dict[str, Any]] = []
    # 收集最近几行的上下文（用于构建完整章节名）
    context_buffer: list[str] = []

    for line in right_lines:
        text = str(line.get("text", "")).strip()
        if not text:
            continue

        box = line.get("box")
        x_center = (box[0][0] + box[1][0]) / 2 if isinstance(box[0], (list, tuple)) else 0
        y_pos = box[0][1] if isinstance(box[0], (list, tuple)) else 0

        # 检测维度标签（x 在 1000-1060 范围）
        if 1000 <= x_center <= 1065:
            for label, dim in dimension_labels.items():
                if label in text:
                    current_dimension = dim
                    break

        # 检测页码（x 在 1670-1700 范围，文本为纯数字）
        if 1660 <= x_center <= 1710:
            if re.fullmatch(r"\d{2,3}", text):
                page_num = int(text)
                if 1 <= page_num <= 200:
                    # 章节名来自 context_buffer 中最近的非 GRI/非纯数字行
                    section_name = _extract_section_name(context_buffer)
                    if section_name:
                        entries.append({
                            "section_name": section_name,
                            "report_page": page_num,
                            "dimension": current_dimension,
                            "index_raw": " | ".join(context_buffer[-5:]),
                        })
                    context_buffer = []
                    continue

        # 收集上下文
        if not re.fullmatch(r"\d{2,3}", text) and "GRI" not in text:
            context_buffer.append(text)
        else:
            context_buffer.append(text)

    # ── 第5步：去重（同一章节名取第一个出现的页码） ──
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for entry in entries:
        key = entry["section_name"]
        if key not in seen:
            seen.add(key)
            deduped.append(entry)

    return deduped


def _extract_section_name(context: list[str]) -> str:
    """从上下文 buffer 中提取最佳章节名称。

    优先使用 章节目录 列（x ~1080-1200）的内容作为章节名，
    避免跨列合并造成的噪音。
    """
    # 过滤掉 GRI 行、纯数字、维度标签
    candidates = []
    for text in context:
        text = text.strip()
        if not text:
            continue
        if re.fullmatch(r"\d{2,3}", text):
            continue
        if "GRI" in text and len(text) < 80:
            continue
        if text in {"环境", "社会", "治理", "可持续", "经济", "维度", "章节目录",
                     "上交所指示", "GRI标准", "页码", "指标索引", "附录"}:
            continue
        if len(text) >= 2:
            candidates.append(text)

    if not candidates:
        return ""

    # 去重：如果某个候选是另一个候选的子串，保留较长的
    filtered: list[str] = []
    for c in sorted(candidates, key=len, reverse=True):
        if not any(c in existing and c != existing for existing in filtered):
            filtered.append(c)
    candidates = sorted(filtered, key=len, reverse=True)

    # 返回最长的候选（但不要太长，超过16字可能是合并错误）
    best = candidates[0]
    if len(best) > 16:
        # 尝试拆分为较短的第一个有意义片段
        # 常见模式：用"与""和"或空格分隔
        parts = re.split(r"[与和、，,]", best)
        if parts and len(parts[0]) >= 2:
            return parts[0].strip()
    return best


def _parse_index_from_plain_text(text: str) -> list[dict[str, Any]]:
    """从纯文本中解析索引页（fallback 方法，精度较低）。"""
    entries: list[dict[str, Any]] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # 检测标题行
    if not any(cue in text for cue in ["指标索引", "内容索引", "GRI标准"]):
        return entries

    # 查找包含页码的模式：章节名...数字
    for line in lines:
        # 跳过明显的正文行（太长的行）
        if len(line) > 120:
            continue
        # 模式：非数字文本 + 2-3位数字结尾
        match = re.search(r"(.+?)[\s,;，]+(\d{2,3})\s*$", line)
        if match:
            section = match.group(1).strip()
            page = int(match.group(2))
            if 1 <= page <= 200:
                # 清理 GRI 标注
                section = re.sub(r"GRI\d+[：:][^,，;；]*", "", section).strip()
                if section:
                    entries.append({
                        "section_name": section,
                        "report_page": page,
                        "dimension": "",
                        "index_raw": line,
                    })

    return entries


def map_index_to_indicators(
    index_entries: list[dict[str, Any]],
    indicator_map: dict[str, dict[str, Any]],
    min_score: int = 75,
) -> dict[str, list[dict[str, Any]]]:
    """
    将索引条目映射到 ESG 指标 field_id。

    使用模糊匹配（rapidfuzz）将章节名匹配到 INDICATOR_INDEX_TOPIC_MAP 中的关键词。

    Returns:
        {field_id: [{"section_name": ..., "report_page": 37, "score": 92}, ...]}
    """
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for entry in index_entries:
        section = entry["section_name"]
        for field_id, topics in INDICATOR_INDEX_TOPIC_MAP.items():
            if field_id not in indicator_map:
                continue
            best_score = 0
            best_topic = ""
            for topic in topics:
                if fuzz is not None:
                    score = fuzz.partial_ratio(topic, section)
                else:
                    # Fallback: simple substring match
                    score = 100 if topic in section else (85 if any(t in section for t in topic) else 0)
                if score > best_score:
                    best_score = score
                    best_topic = topic
            if best_score >= min_score:
                result[field_id].append({
                    "section_name": section,
                    "report_page": entry["report_page"],
                    "dimension": entry.get("dimension", ""),
                    "matched_topic": best_topic,
                    "score": best_score,
                })

    return dict(result)


def build_page_offset_map(
    pdf_payload: dict[str, Any],
    sample_id: str,
    ocr_json_dir: Path,
) -> dict[str, int]:
    """
    为给定样本推断 报告页码 → PDF 物理页码 的映射。

    通过检查已有页面的 report_page_candidates 来推断偏移量。
    """
    offset_map: dict[int, int] = {}  # report_page -> physical_page
    offsets: list[int] = []

    for page in pdf_payload.get("pages", []):
        physical = int(page.get("page", 0))
        report_candidates = page.get("report_page_candidates", [])
        if isinstance(report_candidates, str):
            report_candidates = [rc.strip() for rc in report_candidates.split(";") if rc.strip()]
        for rc in report_candidates:
            try:
                rp = int(rc)
                if 1 <= rp <= 200:
                    offset_map[rp] = physical
                    offsets.append(physical - rp)
            except ValueError:
                continue

    # 如果偏移量一致，推断未映射的页码
    if offsets:
        from collections import Counter
        most_common_offset = Counter(offsets).most_common(1)[0][0]
        # 用最常见偏移量填充缺失的映射
        for rp in range(1, 200):
            if rp not in offset_map:
                predicted = rp + most_common_offset
                if 1 <= predicted <= 400:
                    offset_map[rp] = predicted

    return offset_map


def resolve_index_target_pages(
    pdf_payload: dict[str, Any],
    sample_id: str,
    indicator_map: dict[str, dict[str, Any]],
    ocr_json_dir: Path,
    ocr_text_dir: Path,
) -> dict[str, list[int]]:
    """
    主入口：解析索引页，返回每个指标的推荐目标物理页列表。

    流程：
    1. 扫描 PDF payload 中所有 OCR 页面，找到索引页
    2. 解析索引页 → 提取 (章节名, 报告页码) 映射
    3. 将章节名匹配到 ESG 指标 field_id
    4. 将报告页码映射到 PDF 物理页码
    5. 返回 {field_id: [physical_page, ...]}

    Returns:
        {field_id: [physical_page_1, physical_page_2, ...]}
    """
    # ── 第1步：找索引页 ──
    index_entries_all: list[dict[str, Any]] = []
    for page in pdf_payload.get("pages", []):
        text = page.get("text", "")
        if not any(cue in text for cue in ["指标索引", "内容索引", "GRI标准"]):
            continue
        if page.get("text_source") != "ocr_text":
            continue

        physical_page = int(page.get("page", 0))
        # 尝试读取 OCR JSON
        json_path = ocr_json_dir / f"{sample_id}_page_{physical_page:03d}_ocr.json"
        if json_path.exists():
            entries = parse_index_page_from_ocr_json(json_path)
        else:
            entries = _parse_index_from_plain_text(text)

        if entries:
            index_entries_all.extend(entries)

    if not index_entries_all:
        return {}

    # ── 第2步：映射章节 → 指标 ──
    field_to_entries = map_index_to_indicators(index_entries_all, indicator_map)

    # ── 第3步：构建页码偏移映射 ──
    offset_map = build_page_offset_map(pdf_payload, sample_id, ocr_json_dir)

    # ── 第4步：转换报告页码 → 物理页码 ──
    result: dict[str, list[int]] = {}
    for field_id, entries in field_to_entries.items():
        physical_pages: list[int] = []
        for entry in entries:
            rp = entry["report_page"]
            if rp in offset_map:
                physical_pages.append(offset_map[rp])
            # 也尝试邻域（±2页）
            for delta in [-1, 1, -2, 2]:
                if rp + delta in offset_map:
                    pp = offset_map[rp + delta]
                    if pp not in physical_pages:
                        physical_pages.append(pp)
        if physical_pages:
            result[field_id] = sorted(set(physical_pages))

    return result


def index_resolver_summary(
    target_pages: dict[str, list[int]],
    indicator_map: dict[str, dict[str, Any]],
) -> str:
    """生成可读的索引解析摘要。"""
    lines = ["## 索引页反向定位摘要", ""]
    lines.append(f"| field_id | 指标名称 | 目标物理页 | 说明 |")
    lines.append("| --- | --- | --- | --- |")
    for field_id, pages in sorted(target_pages.items()):
        indicator = indicator_map.get(field_id, {})
        name = indicator.get("metric_name_cn", field_id)
        pages_str = ",".join(str(p) for p in pages)
        layer = indicator.get("indicator_layer", "")
        lines.append(f"| {field_id} | {name} | {pages_str} | {layer} |")
    return "\n".join(lines)


# ── 自测 ──
if __name__ == "__main__":
    import sys

    base = Path(__file__).resolve().parents[2]
    ocr_json_dir = base / "运行缓存" / "OCR" / "ocr_page_json"
    ocr_text_dir = base / "运行缓存" / "OCR" / "ocr_pages"
    indicator_json = base / "算法源码" / "配置" / "ESG指标体系.json"

    # 加载指标
    indicators = json.loads(indicator_json.read_text(encoding="utf-8"))
    indicator_map = {item["field_id"]: item for item in indicators["indicators"]}

    # 测试 GL020
    gl020_json = ocr_json_dir / "GL020_page_042_ocr.json"
    if gl020_json.exists():
        print("=== GL020 索引页解析 ===")
        entries = parse_index_page_from_ocr_json(gl020_json)
        print(f"解析到 {len(entries)} 个索引条目：")
        for e in entries:
            print(f"  {e['section_name']} → 报告页 {e['report_page']} [{e.get('dimension', '')}]")

        print(f"\n=== 指标映射（min_score=75）===")
        mapped = map_index_to_indicators(entries, indicator_map, min_score=75)
        for field_id, matches in sorted(mapped.items()):
            for m in matches:
                print(f"  {field_id} {indicator_map.get(field_id, {}).get('metric_name_cn', '')}"
                      f" ← '{m['section_name']}' (报告页{m['report_page']}, 得分{m['score']})")
    else:
        print(f"GL020 索引页 OCR 不存在: {gl020_json}")
        sys.exit(1)
