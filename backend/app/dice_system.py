"""
博德之门3风格骰子判定系统
=========================

核心理念:
- 告别纯随机 D100，改为 "基础成功率 + 属性/道具/状态 修正 → 最终成功率(上限95%)"
- 保留大成功机制(骰面5%以内)，去掉大失败
- 投骰结果 <= 最终成功率 → 成功，否则 → 失败
- 骰面前 5% → 大成功（无论最终成功率如何，只要骰出大成功就算成功）
"""

import logging
import random
from typing import Any

logger = logging.getLogger(__name__)

# --- 常量 ---
MAX_SUCCESS_RATE = 95          # 最终成功率硬上限(%)
CRITICAL_SUCCESS_THRESHOLD = 5  # 大成功阈值: 骰面前5%
DEFAULT_SIDES = 100             # 默认骰面数


# --- 属性映射 ---
# 将 roll_request 中的 type 关键词映射到 current_life.属性 中的字段名
ROLL_TYPE_TO_ATTRIBUTE_MAP: dict[str, list[str]] = {
    "根骨": ["根骨", "筋骨"],
    "筋骨": ["筋骨", "根骨"],
    "悟性": ["悟性", "慧根"],
    "慧根": ["慧根", "悟性"],
    "气运": ["气运", "福缘", "机缘"],
    "福缘": ["福缘", "气运", "机缘"],
    "机缘": ["机缘", "气运", "福缘"],
    "心境": ["心境", "悟性"],
    "胆魄": ["胆魄", "筋骨"],
    "感知": ["感知", "悟性", "慧根"],
    "潜行": ["感知", "胆魄"],
    "炼丹": ["悟性", "慧根"],
    "战斗": ["筋骨", "根骨", "胆魄"],
    "攻击": ["筋骨", "根骨"],
    "闪避": ["筋骨", "感知"],
    "察言观色": ["感知", "心境"],
    "交涉": ["心境", "感知"],
    "说服": ["心境", "感知"],
    "NPC反应": ["心境", "感知"],
    "好感": ["心境", "感知"],
    "魅力": ["心境", "福缘"],
    "威慑": ["胆魄", "筋骨"],
    "谈判": ["心境", "悟性"],
}


def _find_relevant_attribute(roll_type: str, attributes: dict[str, Any]) -> int | None:
    """
    根据 roll_type 关键词，在角色属性字典中找到最相关的属性值。
    返回属性值(int)或 None。
    """
    if not attributes:
        return None

    # 1) 直接精确匹配 roll_type 中的关键词
    for keyword, attr_names in ROLL_TYPE_TO_ATTRIBUTE_MAP.items():
        if keyword in roll_type:
            for attr_name in attr_names:
                val = attributes.get(attr_name)
                if val is not None:
                    try:
                        return int(val)
                    except (ValueError, TypeError):
                        continue
            break  # 找到了关键词匹配但没找到属性，继续用模糊匹配

    # 2) 模糊匹配: roll_type 字符串包含属性名
    for attr_name, val in attributes.items():
        if attr_name in roll_type:
            try:
                return int(val)
            except (ValueError, TypeError):
                continue

    return None


def _calculate_attribute_bonus(attribute_value: int | None) -> int:
    """
    将属性值(通常0-100)换算成成功率加成(%)。
    
    属性值  →  加成
    0-20    →  -5 ~ 0
    21-40   →  0 ~ +5
    41-60   →  +5 ~ +10
    61-80   →  +10 ~ +15
    81-100  →  +15 ~ +20
    """
    if attribute_value is None:
        return 0

    val = max(0, min(100, attribute_value))

    if val <= 20:
        return int(-5 + val * 0.25)        # -5 ~ 0
    elif val <= 40:
        return int((val - 20) * 0.25)      # 0 ~ +5
    elif val <= 60:
        return int(5 + (val - 40) * 0.25)  # +5 ~ +10
    elif val <= 80:
        return int(10 + (val - 60) * 0.25) # +10 ~ +15
    else:
        return int(15 + (val - 80) * 0.25) # +15 ~ +20


def _calculate_item_bonus(items: list[dict] | None, roll_type: str) -> int:
    """
    根据角色携带的道具计算加成。
    
    道具如果有 "效果" 或 "加成" 字段且与当前判定类型相关，给予加成。
    通用宝物给予小幅加成。
    """
    if not items:
        return 0

    bonus = 0
    for item in items:
        if not isinstance(item, dict):
            continue

        item_name = str(item.get("名称", ""))
        item_effect = str(item.get("效果", ""))
        item_bonus = item.get("加成", 0)

        # 如果道具自带数值加成
        if item_bonus:
            try:
                bonus += int(item_bonus)
                continue
            except (ValueError, TypeError):
                pass

        # 根据道具名称/效果与判定类型的关联度给予加成
        keywords_in_roll = roll_type.lower()
        item_text = (item_name + item_effect).lower()

        # 特定匹配关键词
        relevance_keywords = {
            "炼丹": ["丹", "炉", "药"],
            "战斗": ["剑", "刀", "甲", "盾", "武", "兵"],
            "攻击": ["剑", "刀", "武", "兵", "弓"],
            "悟性": ["书", "经", "卷", "典", "简"],
            "潜行": ["隐", "暗", "影"],
            "感知": ["灵", "识", "眼"],
        }

        for keyword, related_items in relevance_keywords.items():
            if keyword in keywords_in_roll:
                for ri in related_items:
                    if ri in item_text:
                        bonus += 3
                        break

    return min(bonus, 15)  # 道具加成上限 15%


def _calculate_status_bonus(status_effects: list[str] | None) -> int:
    """
    根据角色状态效果计算加成/减益。
    """
    if not status_effects:
        return 0

    bonus = 0
    for effect in status_effects:
        if not isinstance(effect, str):
            continue

        effect_lower = effect.lower()

        # 负面状态
        if any(k in effect_lower for k in ["重伤", "濒死", "中毒", "虚弱"]):
            bonus -= 10
        elif any(k in effect_lower for k in ["轻伤", "疲惫", "眩晕"]):
            bonus -= 5
        elif any(k in effect_lower for k in ["诅咒", "封印", "压制"]):
            bonus -= 8

        # 正面状态
        elif any(k in effect_lower for k in ["全盛", "巅峰", "突破"]):
            bonus += 10
        elif any(k in effect_lower for k in ["祝福", "护佑", "加持", "灵光"]):
            bonus += 8
        elif any(k in effect_lower for k in ["专注", "冷静", "坚定"]):
            bonus += 5

    return max(-20, min(bonus, 20))  # 状态修正范围 -20% ~ +20%


def calculate_final_success_rate(
    base_target: int,
    sides: int,
    roll_type: str,
    current_life: dict | None,
) -> tuple[int, dict]:
    """
    计算最终成功率。
    
    Args:
        base_target: AI 给出的基础目标值(基础成功率 = base_target/sides*100%)
        sides: 骰面数
        roll_type: 判定类型(如"悟性判定")
        current_life: 当前角色状态字典
    
    Returns:
        (final_target, breakdown)
        final_target: 最终目标值(1~sides范围)
        breakdown: 各修正项的明细字典
    """
    if sides <= 0:
        sides = DEFAULT_SIDES

    # 基础成功率(%)
    base_rate = (base_target / sides) * 100

    # 从 current_life 中提取属性、道具、状态
    attributes = {}
    items = []
    status_effects = []
    if current_life and isinstance(current_life, dict):
        attributes = current_life.get("属性", {}) or {}
        items = current_life.get("物品", []) or []
        status_effects = current_life.get("状态效果", []) or []

    # 计算各项加成
    attr_value = _find_relevant_attribute(roll_type, attributes)
    attr_bonus = _calculate_attribute_bonus(attr_value)
    item_bonus = _calculate_item_bonus(items, roll_type)
    status_bonus = _calculate_status_bonus(status_effects)

    # 合计
    total_bonus = attr_bonus + item_bonus + status_bonus
    final_rate = base_rate + total_bonus

    # 硬上限 95%，下限 5%（给玩家最低希望）
    final_rate = max(5, min(MAX_SUCCESS_RATE, final_rate))

    # 转换回目标值
    final_target = max(1, min(sides, int(final_rate / 100 * sides)))

    breakdown = {
        "base_rate": round(base_rate, 1),
        "attribute_name": _find_attribute_name(roll_type, attributes),
        "attribute_value": attr_value,
        "attribute_bonus": attr_bonus,
        "item_bonus": item_bonus,
        "status_bonus": status_bonus,
        "total_bonus": total_bonus,
        "final_rate": round(final_rate, 1),
        "final_target": final_target,
    }

    logger.info(
        f"骰子判定计算: type={roll_type}, base={base_rate:.1f}%, "
        f"attr={attr_bonus:+d}%, item={item_bonus:+d}%, status={status_bonus:+d}%, "
        f"final={final_rate:.1f}% (target={final_target}/{sides})"
    )

    return final_target, breakdown


def _find_attribute_name(roll_type: str, attributes: dict) -> str | None:
    """找到用于加成计算的属性名称（用于展示）"""
    if not attributes:
        return None

    for keyword, attr_names in ROLL_TYPE_TO_ATTRIBUTE_MAP.items():
        if keyword in roll_type:
            for attr_name in attr_names:
                if attr_name in attributes:
                    return attr_name
            break

    for attr_name in attributes:
        if attr_name in roll_type:
            return attr_name

    return None


def roll_dice(
    base_target: int,
    sides: int,
    roll_type: str,
    current_life: dict | None,
) -> dict:
    """
    执行一次带属性修正的骰子判定。
    
    Returns:
        {
            "roll_result": int,       # 骰子点数
            "final_target": int,      # 经修正后的最终目标值
            "sides": int,             # 骰面数
            "outcome": str,           # "大成功" | "成功" | "失败"（无大失败）
            "breakdown": dict,        # 修正明细
        }
    """
    if sides <= 0:
        sides = DEFAULT_SIDES

    final_target, breakdown = calculate_final_success_rate(
        base_target, sides, roll_type, current_life
    )

    roll_result = random.randint(1, sides)

    # 判定结果: 大成功 > 成功 > 失败（无大失败）
    critical_threshold = max(1, int(sides * CRITICAL_SUCCESS_THRESHOLD / 100))

    if roll_result <= critical_threshold:
        outcome = "大成功"
    elif roll_result <= final_target:
        outcome = "成功"
    else:
        outcome = "失败"

    logger.info(
        f"骰子判定结果: roll={roll_result}, target={final_target}/{sides}, outcome={outcome}"
    )

    return {
        "roll_result": roll_result,
        "final_target": final_target,
        "original_target": base_target,
        "sides": sides,
        "outcome": outcome,
        "breakdown": breakdown,
    }
