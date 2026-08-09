# -*- coding: utf-8 -*-
"""ESG LLM 提取 Few-shot 示例库。"""

import json
from pathlib import Path

# 精选 few-shot 示例（从53个金标中提取，覆盖E/S/G各维度）
FEW_SHOT_EXAMPLES = [
    # E维度示例
    {"field_id": "E_Q_001", "metric": "温室气体排放总量", "value": "935989", "unit": "吨",
     "reasoning": "文本标注'温室气体排放总量 吨 935989'，取2025年第一个数"},
    {"field_id": "E_Q_005", "metric": "温室气体排放强度", "value": "0.203", "unit": "吨CO2e/人民币万元营收",
     "reasoning": "强度指标，取'碳排放强度 0.203'，不是总量935989"},
    {"field_id": "E_Q_009", "metric": "取水量", "value": "972.84", "unit": "万吨",
     "reasoning": "取水量/用水量指标，取绝对值而非强度值"},
    {"field_id": "E_Q_012", "metric": "废弃物产生总量", "value": "48813", "unit": "吨",
     "reasoning": "总废弃物量取'总废弃物量 吨 48813'，不是强度值"},
    {"field_id": "E_Q_013", "metric": "危险废弃物产生量", "value": "12781", "unit": "吨",
     "reasoning": "危废取'有害/危险废弃物'对应行，不是总废弃物量"},
    # S维度示例
    {"field_id": "S_Q_001", "metric": "员工总数", "value": "40603", "unit": "人",
     "reasoning": "取'员工总数/员工人数'的绝对值，不是流失率或百分比"},
    {"field_id": "S_Q_004", "metric": "员工培训总时长", "value": "1572425", "unit": "人时",
     "reasoning": "培训总时长取总量，不是人均值"},
    {"field_id": "S_Q_005", "metric": "人均培训时长", "value": "51", "unit": "小时",
     "reasoning": "人均指标取'人均培训时长 51 小时'，不是总时长"},
    {"field_id": "S_Q_008", "metric": "工伤率", "value": "0.168", "unit": "（比率）",
     "reasoning": "工伤率取比率值，如'百万工时可记录工伤率 0.168'"},
    {"field_id": "S_Q_009", "metric": "因工死亡人数", "value": "0", "unit": "人",
     "reasoning": "工亡人数取'因工亡故人数 0'，0也是有效值"},
    # G维度示例
    {"field_id": "G_Q_001", "metric": "董事会人数", "value": "12", "unit": "人",
     "reasoning": "取'董事会由12名董事组成'中的总数，不是子分类人数"},
    {"field_id": "G_Q_002", "metric": "独立董事人数", "value": "4", "unit": "人",
     "reasoning": "取'独立非执行董事 4人'"},
    {"field_id": "G_Q_009", "metric": "反腐败培训", "value": "12", "unit": "场",
     "reasoning": "反腐败/廉洁培训取培训场次或人次"},
    {"field_id": "G_Q_010", "metric": "腐败案件数量", "value": "0", "unit": "件",
     "reasoning": "案件数量为0是有效披露"},
]

FEW_SHOT_BY_FIELD = {ex["field_id"]: ex for ex in FEW_SHOT_EXAMPLES}


def get_few_shot_text(field_ids: list[str]) -> str:
    """为指定指标ID列表生成 few-shot 提示文本。"""
    selected = []
    for fid in field_ids:
        if fid in FEW_SHOT_BY_FIELD:
            ex = FEW_SHOT_BY_FIELD[fid]
            selected.append(ex)

    if not selected:
        return ""

    lines = ["## 参考示例（类似指标的金标提取结果）"]
    for ex in selected[:2]:  # 最多2个示例
        lines.append(
            f"{ex['field_id']} {ex['metric']}: "
            f'value="{ex["value"]}" unit="{ex["unit"]}"'
            f" // {ex['reasoning']}"
        )
    lines.append("")
    return "\n".join(lines)
