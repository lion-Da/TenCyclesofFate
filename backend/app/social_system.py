"""
好感度系统 (Social / Affinity System)
======================================

参考鬼谷八荒的好感度系统设计:

核心理念:
- 每个NPC有独立好感度 [-100, +100]
- 好感度分 6 个阶段，每个阶段有瓶颈阈值 (20/40/60/80/100)
- 突破瓶颈需要特定"好感度突破事件"，否则好感度停在阈值上限
- 高好感触发正面事件（赠礼、助战、结拜、道侣）
- 低好感触发负面事件（挑衅、陷害、暗杀、夺宝）
- 好感度变动以事件为主、送礼为辅，且送礼边际递减

好感度阶段:
  [-100, -60) → 死敌 (Nemesis)
  [-60, -20)  → 仇人 (Hostile)
  [-20, +20)  → 陌生 (Stranger)
  [+20, +40)  → 相识 (Acquaintance)
  [+40, +60)  → 知交 (Friend)
  [+60, +80)  → 挚友 (Close Friend)
  [+80, +100) → 生死之交 (Sworn)
  +100        → 道侣/结义 (Soulbound) — 需特殊事件触发

瓶颈机制:
  当好感度自然增长到 20/40/60/80 时会"卡住"，
  除非 AI 触发了对应的突破事件(breakthrough_event)，否则不会超过瓶颈值。
  突破事件例如:
    20 → 共同经历一场战斗
    40 → 一同探索秘境/互救
    60 → 共渡生死大劫
    80 → 同心破敌 / 天道见证
"""

import logging
import random
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Robust type helpers (AI output can be anything)
# ─────────────────────────────────────────────

def _ensure_str_list(value: Any) -> list[str]:
    """
    Normalise *any* AI-produced value into a flat ``list[str]``.

    Handles every quirky shape the LLM might return for 特殊标记,
    已突破阈值, 好感度变动记录 etc.:
      - None / missing            → []
      - "结拜"                    → ["结拜"]
      - ["结拜", "道侣"]          → as-is
      - ["结拜", ["道侣"]]        → ["结拜", "道侣"]  (flatten)
      - 123 / True                → ["123"] / ["True"]
      - {"a": 1}                  → ['{"a": 1}']
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (int, float, bool)):
        return [str(value)]
    if isinstance(value, dict):
        # Dicts are not iterable in a useful way; stringify
        import json as _json
        try:
            return [_json.dumps(value, ensure_ascii=False)]
        except Exception:
            return [str(value)]
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for item in value:
            result.extend(_ensure_str_list(item))  # recursive flatten
        return result
    # Fallback: stringify anything else
    return [str(value)]


def _ensure_int_list(value: Any) -> list[int]:
    """
    Normalise *any* AI-produced value into a flat ``list[int]``.

    Used for 已突破阈值 which should be [20, 40, 60, 80] subset.
    Handles: None, single int, str("20"), list of mixed, nested, etc.
    Non-numeric items are silently dropped.
    """
    if value is None:
        return []
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, float):
        return [int(value)]
    if isinstance(value, str):
        try:
            return [int(value)]
        except (ValueError, TypeError):
            return []
    if isinstance(value, (list, tuple)):
        result: list[int] = []
        for item in value:
            result.extend(_ensure_int_list(item))
        return result
    return []


def _safe_int(value: Any, default: int = 0) -> int:
    """Coerce any AI-produced value to int. Returns *default* on failure."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except (ValueError, TypeError):
            # Try extracting leading digits: "15点" -> 15
            import re
            m = re.match(r"^[-+]?\d+", value.strip())
            return int(m.group()) if m else default
    return default


# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────
MIN_AFFINITY = -100
MAX_AFFINITY = 100

# 瓶颈阈值列表 — 好感度在 *未突破* 时不能超过这些值
BOTTLENECK_THRESHOLDS = [20, 40, 60, 80]

# 好感度阶段定义
AFFINITY_STAGES = [
    {"min": -100, "max": -60, "name": "死敌",   "key": "nemesis"},
    {"min": -60,  "max": -20, "name": "仇人",   "key": "hostile"},
    {"min": -20,  "max":  20, "name": "陌生",   "key": "stranger"},
    {"min":  20,  "max":  40, "name": "相识",   "key": "acquaintance"},
    {"min":  40,  "max":  60, "name": "知交",   "key": "friend"},
    {"min":  60,  "max":  80, "name": "挚友",   "key": "close_friend"},
    {"min":  80,  "max": 100, "name": "生死之交", "key": "sworn"},
    {"min": 100, "max": 100,  "name": "道侣/结义", "key": "soulbound"},
]

# ─────────────────────────────────────────────
# 随机事件池 — AI 根据好感阶段触发
# ─────────────────────────────────────────────
# 正面事件: 高好感随机触发
POSITIVE_EVENTS = {
    "acquaintance": [  # 20+
        {"type": "gift_minor",    "desc": "赠送一枚低阶灵丹",       "affinity_delta": 3,  "weight": 40},
        {"type": "info_share",    "desc": "透露附近秘境线索",       "affinity_delta": 2,  "weight": 30},
        {"type": "trade_favor",   "desc": "以优惠价格交易珍稀材料",  "affinity_delta": 2,  "weight": 30},
    ],
    "friend": [  # 40+
        {"type": "gift_medium",   "desc": "赠送一部中阶功法",       "affinity_delta": 4,  "weight": 30},
        {"type": "invite_explore","desc": "邀请共探秘境",           "affinity_delta": 5,  "weight": 35},
        {"type": "combat_assist", "desc": "危急时刻出手相助",       "affinity_delta": 5,  "weight": 35},
    ],
    "close_friend": [  # 60+
        {"type": "gift_rare",     "desc": "赠送珍稀天材地宝",       "affinity_delta": 5,  "weight": 25},
        {"type": "secret_share",  "desc": "分享独门秘术",           "affinity_delta": 6,  "weight": 30},
        {"type": "risk_together", "desc": "共同闯入禁地寻宝",       "affinity_delta": 7,  "weight": 25},
        {"type": "life_save",     "desc": "拼死相救",               "affinity_delta": 8,  "weight": 20},
    ],
    "sworn": [  # 80+
        {"type": "gift_legendary","desc": "赠送传说级法宝",          "affinity_delta": 5, "weight": 20},
        {"type": "oath_proposal", "desc": "提议结拜/结为道侣",       "affinity_delta": 10, "weight": 30},
        {"type": "ultimate_aid",  "desc": "不惜代价全力相助",        "affinity_delta": 8, "weight": 25},
        {"type": "legacy_share",  "desc": "共享修炼传承",            "affinity_delta": 7, "weight": 25},
    ],
}

# 负面事件: 低好感随机触发
NEGATIVE_EVENTS = {
    "stranger": [  # -20 ~ 0 范围
        {"type": "cold_shoulder", "desc": "冷言相讥，出言不逊",     "affinity_delta": -2, "weight": 50},
        {"type": "refuse_help",   "desc": "见死不救，袖手旁观",     "affinity_delta": -3, "weight": 50},
    ],
    "hostile": [  # -60 ~ -20
        {"type": "provoke",       "desc": "公然挑衅，当众羞辱",     "affinity_delta": -5,  "weight": 30},
        {"type": "sabotage",      "desc": "暗中破坏，设下陷阱",     "affinity_delta": -5,  "weight": 30},
        {"type": "steal",         "desc": "趁夜窃取珍贵道具",       "affinity_delta": -4,  "weight": 20},
        {"type": "spread_rumors", "desc": "散布谣言，败坏名声",     "affinity_delta": -3,  "weight": 20},
    ],
    "nemesis": [  # -100 ~ -60
        {"type": "ambush",        "desc": "埋伏暗杀",               "affinity_delta": -8,  "weight": 25},
        {"type": "frame",         "desc": "栽赃陷害，引来追杀",     "affinity_delta": -10, "weight": 25},
        {"type": "betray_sect",   "desc": "向敌对势力出卖情报",     "affinity_delta": -8,  "weight": 25},
        {"type": "poison",        "desc": "在饮食中下毒",           "affinity_delta": -7,  "weight": 25},
    ],
}

# ─────────────────────────────────────────────
# 好感度突破事件类型（AI 需要在叙事中触发）
# ─────────────────────────────────────────────
BREAKTHROUGH_EVENTS = {
    20: {
        "name": "共历风雨",
        "description": "与该NPC共同经历一场危机（战斗、逃难、灾变），在互相扶持中建立初步信任。",
        "examples": ["合力击退山贼", "一同逃过妖兽追杀", "在暴风雨中互相搀扶"],
    },
    40: {
        "name": "肝胆相照",
        "description": "与该NPC在生死攸关时互救，或一同深入险地成功归来。",
        "examples": ["为其挡下致命一击", "在秘境中背其脱险", "共同炼制救命丹药"],
    },
    60: {
        "name": "同舟共济",
        "description": "与该NPC共渡大劫，在几近绝望之境中不离不弃。",
        "examples": ["在天劫中合力渡过", "被围困绝境中生死与共", "为其承受因果反噬"],
    },
    80: {
        "name": "道心相印",
        "description": "与该NPC的道心产生共鸣，经天道见证，缘分得到升华。",
        "examples": ["在论道中心意相通", "天降异象印证此缘", "双方互明心迹，道心坚定"],
    },
}


# ─────────────────────────────────────────────
# 叙事情感强度分析 — 基于关键词检测好感度事件的重大程度
# ─────────────────────────────────────────────

# 情感强度等级：每级定义关键词列表、最低delta保底、可突破的最高瓶颈
NARRATIVE_INTENSITY_TIERS = [
    {
        # Tier 4: 天道级 — 道侣/结义/天劫共渡/神魂融合
        "tier": 4,
        "keywords": [
            "道侣", "结为道侣", "天道见证", "天作之合", "神魂融合", "双修", "心意相通",
            "结义", "结拜", "金兰", "义结金兰",
            "共渡天劫", "天劫", "雷劫", "心劫",
            "合道", "道心共鸣", "同生共死",
        ],
        "min_delta": 25,   # 此类事件至少 +25
        "max_breakthrough": 80,  # 可直接突破到 80
    },
    {
        # Tier 3: 生死级 — 舍命相救/绝境共存
        "tier": 3,
        "keywords": [
            "生死与共", "生死相依", "以命相护", "以命相搏", "舍命", "替死", "挡下致命",
            "不离不弃", "绝境", "九死一生", "死里逃生", "拼死相救", "以身挡",
            "互托后事", "同归于尽", "生死之交", "血盟", "斩杀仇敌",
            "承受反噬", "因果反噬", "魂飞魄散", "灵魂献祭",
        ],
        "min_delta": 18,
        "max_breakthrough": 60,
    },
    {
        # Tier 2: 深交级 — 互救/共探险境/患难与共
        "tier": 2,
        "keywords": [
            "互救", "救命之恩", "出手相助", "搭救", "救下", "恩情",
            "患难", "共同击退", "并肩作战", "合力", "联手",
            "秘境", "共探", "共闯", "共同冒险",
            "传授", "赠送功法", "倾囊相授", "传承", "赐予",
            "信任", "托付", "坦诚相告", "互明心迹",
        ],
        "min_delta": 10,
        "max_breakthrough": 40,
    },
    {
        # Tier 1: 初识级 — 小恩小惠/日常互动（不触发瓶颈突破）
        "tier": 1,
        "keywords": [
            "寒暄", "交谈", "指点", "切磋", "论道", "赠送", "赠礼",
            "一同", "同行", "相伴", "照顾", "关心",
        ],
        "min_delta": 3,
        "max_breakthrough": 0,  # tier 1 不自动突破任何瓶颈
    },
]


def _analyze_narrative_intensity(reason: str) -> dict:
    """
    分析好感度变化的"原因"文本，返回情感强度信息。

    Returns:
        {
            "tier": int (0-4, 0表示无明显关键词),
            "min_delta": int (该强度下的最低好感度变化),
            "max_breakthrough": int (该强度可突破的最高瓶颈),
            "matched_keywords": list[str]
        }
    """
    if not reason:
        return {"tier": 0, "min_delta": 0, "max_breakthrough": 0, "matched_keywords": []}

    best_tier = {"tier": 0, "min_delta": 0, "max_breakthrough": 0, "matched_keywords": []}

    for tier_def in NARRATIVE_INTENSITY_TIERS:
        matched = [kw for kw in tier_def["keywords"] if kw in reason]
        if matched and tier_def["tier"] > best_tier["tier"]:
            best_tier = {
                "tier": tier_def["tier"],
                "min_delta": tier_def["min_delta"],
                "max_breakthrough": tier_def["max_breakthrough"],
                "matched_keywords": matched,
            }

    return best_tier


def get_affinity_stage(score: int | Any) -> dict:
    """根据好感度分数返回当前阶段信息。"""
    score = _safe_int(score, 0)
    score = max(MIN_AFFINITY, min(MAX_AFFINITY, score))
    for stage in AFFINITY_STAGES:
        if stage["min"] <= score < stage["max"] or (score == MAX_AFFINITY and stage["max"] == MAX_AFFINITY):
            return {"name": stage["name"], "key": stage["key"]}
    return {"name": "陌生", "key": "stranger"}


def get_current_bottleneck(score: int, breakthroughs: list | None = None) -> int | None:
    """
    获取当前好感度面临的瓶颈值。
    如果该瓶颈已被突破(在 breakthroughs 列表中)则返回 None。
    """
    score = _safe_int(score, 0)
    breakthroughs = _ensure_int_list(breakthroughs)
    for threshold in BOTTLENECK_THRESHOLDS:
        if score >= threshold and threshold not in breakthroughs:
            return threshold
    return None


def clamp_affinity(
    new_score: int,
    breakthroughs: list | None = None,
) -> int:
    """
    将好感度值 clamp 到合法范围，并考虑瓶颈约束。
    正方向: 如果遇到未突破的瓶颈，卡在瓶颈值
    负方向: 无瓶颈，直接到 -100
    """
    breakthroughs = _ensure_int_list(breakthroughs)
    new_score = max(MIN_AFFINITY, min(MAX_AFFINITY, new_score))

    # 正向瓶颈检查
    if new_score > 0:
        for threshold in BOTTLENECK_THRESHOLDS:
            if new_score >= threshold and threshold not in breakthroughs:
                new_score = threshold
                break

    return new_score


def apply_affinity_change(
    npc: dict,
    delta: int,
    reason: str = "",
) -> dict:
    """
    对一个 NPC 施加好感度变化，自动处理瓶颈限制。

    Args:
        npc: NPC 字典 (来自 current_life.人物关系.<name>)
        delta: 好感度变化量 (正/负)
        reason: 变化原因描述

    Returns:
        包含更新结果的字典:
        {
            "old_score": int,
            "new_score": int,
            "stage": str,
            "bottleneck_hit": int | None,  # 如果卡在瓶颈，返回瓶颈值
            "stage_changed": bool,
        }
    """
    old_score = _safe_int(npc.get("好感度", 0), 0)
    breakthroughs = _ensure_int_list(npc.get("已突破阈值", []))
    delta = _safe_int(delta, 0)
    raw_new = old_score + delta
    new_score = clamp_affinity(raw_new, breakthroughs)

    old_stage = get_affinity_stage(old_score)
    new_stage = get_affinity_stage(new_score)

    # 检查是否命中了瓶颈
    bottleneck_hit = None
    if delta > 0 and raw_new > new_score:
        bottleneck_hit = get_current_bottleneck(new_score, breakthroughs)

    npc["好感度"] = new_score

    # 记录变动日志
    log_entry = {
        "delta": delta,
        "reason": reason,
        "old": old_score,
        "new": new_score,
    }
    history = npc.get("好感度变动记录")
    if not isinstance(history, list):
        history = []
    history.append(log_entry)
    npc["好感度变动记录"] = history[-10:]  # 只保留最近10条

    result = {
        "old_score": old_score,
        "new_score": new_score,
        "stage": new_stage["name"],
        "stage_key": new_stage["key"],
        "bottleneck_hit": bottleneck_hit,
        "stage_changed": old_stage["key"] != new_stage["key"],
    }

    logger.info(
        f"好感度变动: NPC={npc.get('姓名', '?')}, "
        f"{old_score} → {new_score} (Δ{delta:+d}), "
        f"阶段={new_stage['name']}, 瓶颈={bottleneck_hit}, "
        f"原因={reason}"
    )

    return result


def process_breakthrough(npc: dict, threshold: int) -> bool:
    """
    处理好感度瓶颈突破。当 AI 触发了突破事件时调用。

    Args:
        npc: NPC 字典
        threshold: 要突破的阈值 (20/40/60/80)

    Returns:
        是否成功突破
    """
    if threshold not in BOTTLENECK_THRESHOLDS:
        logger.warning(f"无效的突破阈值: {threshold}")
        return False

    threshold = _safe_int(threshold, 0)
    breakthroughs = _ensure_int_list(npc.get("已突破阈值", []))
    if threshold in breakthroughs:
        logger.info(f"阈值 {threshold} 已经被突破过了")
        return False

    npc["已突破阈值"] = breakthroughs + [threshold]

    logger.info(
        f"好感度突破: NPC={npc.get('姓名', '?')}, "
        f"阈值={threshold}, 已突破={npc['已突破阈值']}"
    )
    return True


def roll_npc_reaction(
    npc: dict,
    event_type: str,
    base_chance: int = 50,
    roll_sides: int = 100,
) -> dict:
    """
    NPC 对玩家行为的反应骰子判定。
    用于: 主角做了伤害伙伴的事情时，判定伙伴反应。

    Args:
        npc: NPC 字典
        event_type: 事件类型 ("伤害", "背叛", "冒犯" 等)
        base_chance: 基础反应概率
        roll_sides: 骰面数

    Returns:
        {
            "roll": int,
            "target": int,
            "forgive": bool,     # 是否原谅
            "affinity_delta": int,  # 好感度实际变化
            "reaction": str,     # 反应描述
        }
    """
    score = npc.get("好感度", 0)
    personality = npc.get("性格", "")

    # 好感越高 → 越容易原谅
    # 好感越低 → 越难原谅且后果更严重
    affinity_modifier = score // 5  # -20 ~ +20

    # 性格修正
    personality_mod = 0
    if any(k in personality for k in ["豁达", "温和", "宽厚", "仁慈"]):
        personality_mod = 10
    elif any(k in personality for k in ["暴躁", "记仇", "冷酷", "偏执"]):
        personality_mod = -10

    # 事件严重度修正
    severity_mod = 0
    if event_type in ["背叛", "暗害"]:
        severity_mod = -20
    elif event_type in ["冒犯", "无视"]:
        severity_mod = -5
    elif event_type in ["误伤", "意外"]:
        severity_mod = 5

    final_target = max(5, min(95, base_chance + affinity_modifier + personality_mod + severity_mod))
    roll_result = random.randint(1, roll_sides)

    forgive = roll_result <= final_target

    # 计算好感变化
    if forgive:
        # 原谅了，但仍有轻微负面影响
        affinity_delta = random.randint(-3, -1)
        reaction = "虽心有芥蒂，但选择了包容"
    else:
        # 没有原谅，好感大幅下降
        base_drop = {
            "背叛": random.randint(-25, -15),
            "暗害": random.randint(-30, -20),
            "冒犯": random.randint(-10, -5),
            "无视": random.randint(-8, -3),
            "误伤": random.randint(-8, -3),
            "伤害": random.randint(-15, -8),
        }.get(event_type, random.randint(-10, -5))

        affinity_delta = base_drop
        if score <= -20:
            reaction = "怒不可遏，心中杀意已起"
        elif score <= 20:
            reaction = "面色骤变，拂袖而去"
        else:
            reaction = "眼中闪过受伤的神色，默然转身离去"

    return {
        "roll": roll_result,
        "target": final_target,
        "forgive": forgive,
        "affinity_delta": affinity_delta,
        "reaction": reaction,
        "breakdown": {
            "base": base_chance,
            "affinity_mod": affinity_modifier,
            "personality_mod": personality_mod,
            "severity_mod": severity_mod,
        },
    }


def pick_random_social_event(npc: dict) -> dict | None:
    """
    根据 NPC 当前好感阶段，随机选取一个社交事件。
    返回事件字典，或 None (无事件)。

    调用时机: 每回合结束后有一定概率触发 (由 game_logic 控制)。
    """
    score = npc.get("好感度", 0)
    stage = get_affinity_stage(score)
    stage_key = stage["key"]

    # 负面事件池
    if score < 0 and stage_key in NEGATIVE_EVENTS:
        pool = NEGATIVE_EVENTS[stage_key]
    # 正面事件池
    elif score >= 20 and stage_key in POSITIVE_EVENTS:
        pool = POSITIVE_EVENTS[stage_key]
    else:
        return None

    if not pool:
        return None

    # 加权随机选择
    weights = [e["weight"] for e in pool]
    chosen = random.choices(pool, weights=weights, k=1)[0]

    return {
        "npc_name": npc.get("姓名", "未知"),
        "event_type": chosen["type"],
        "description": chosen["desc"],
        "affinity_delta": chosen["affinity_delta"],
        "stage": stage["name"],
    }


def should_trigger_social_event(round_count: int) -> bool:
    """
    判断本回合是否应该触发随机社交事件。
    大约每3-5回合触发一次，概率约25%。
    """
    if round_count < 3:
        return False  # 前几回合不触发社交事件
    return random.random() < 0.25


def create_npc_template(
    name: str,
    relation: str = "陌生",
    personality: str = "",
    affinity: int = 0,
) -> dict:
    """
    创建一个标准 NPC 数据模板。

    NPC 数据分为两层：
    - 社交层（好感度、关系阶段等）：始终存在
    - 战斗/修炼层（境界、功法、战力等）：可选，由 AI 在叙事中逐步补充

    好感度达到一定阶段后，玩家可查看 NPC 的详细战斗属性：
    - 相识(20+): 可见 身份、性格
    - 知交(40+): 可见 境界、功法概要
    - 挚友(60+): 可见 全部属性、物品
    """
    return {
        # ── 基础信息 ──
        "姓名": name,
        "性格": personality,
        "身份": "",
        # ── 社交系统 ──
        "好感度": max(MIN_AFFINITY, min(MAX_AFFINITY, affinity)),
        "关系阶段": relation,
        "已突破阈值": [],
        "好感度变动记录": [],
        "特殊标记": [],  # 如 "结拜", "道侣", "宿敌" 等
        # ── 战斗/修炼属性（AI 可选填充，默认为空/None 表示尚未探知） ──
        "境界": None,       # 如 "练气三层", "筑基初期"
        "功法": None,       # 如 [{"名称": "xxx", "品阶": "黄", "等阶": "下品"}]
        "战力": None,       # 数值
        "物品": None,       # 如 ["飞剑", "护身玉佩"]
        "生命值": None,     # 数值
        "最大生命值": None,  # 数值
    }


# NPC 属性可见性阈值 — 好感度达到此值才能查看对应类别的详细信息
NPC_VISIBILITY_THRESHOLDS = {
    "基础": 0,     # 姓名、关系阶段、好感度条 — 始终可见
    "身份": 20,    # 身份、性格 — 相识后可见
    "境界": 40,    # 境界、功法概要 — 知交后可见
    "详细": 60,    # 全部属性、物品、战力 — 挚友后可见
}


def get_npc_visible_data(npc: dict) -> dict:
    """
    根据好感度阶段返回玩家可见的 NPC 数据子集。
    
    Args:
        npc: 完整的 NPC 数据字典
    
    Returns:
        过滤后的 NPC 数据（只包含玩家有权查看的字段）
    """
    score = _safe_int(npc.get("好感度", 0), 0)
    
    # 始终可见
    visible = {
        "姓名": npc.get("姓名", "???"),
        "好感度": score,
        "关系阶段": npc.get("关系阶段", "陌生"),
        "特殊标记": npc.get("特殊标记", []),
    }
    
    # 相识(20+): 基础个人信息
    if score >= NPC_VISIBILITY_THRESHOLDS["身份"]:
        visible["身份"] = npc.get("身份", "")
        visible["性格"] = npc.get("性格", "")
    
    # 知交(40+): 修炼概况
    if score >= NPC_VISIBILITY_THRESHOLDS["境界"]:
        visible["境界"] = npc.get("境界")
        gongfa = npc.get("功法")
        if gongfa and isinstance(gongfa, list):
            # 只显示功法名称和品阶，不显示详细描述
            visible["功法"] = [
                {"名称": g.get("名称", "?"), "品阶": g.get("品阶", "?")}
                for g in gongfa if isinstance(g, dict)
            ]
        else:
            visible["功法"] = gongfa
    
    # 挚友(60+): 全部战斗属性
    if score >= NPC_VISIBILITY_THRESHOLDS["详细"]:
        visible["战力"] = npc.get("战力")
        visible["物品"] = npc.get("物品")
        visible["生命值"] = npc.get("生命值")
        visible["最大生命值"] = npc.get("最大生命值")
    
    return visible


def process_social_state_update(current_life: dict, social_update: dict) -> list[str]:
    """
    处理来自 AI 的社交状态更新。

    social_update 格式 (AI 在 state_update 中输出):
    {
        "人物关系": {
            "NPC名字": {
                "好感度变化": +5,
                "原因": "共同击退山贼",
                "突破阈值": 20,        // 可选，触发突破事件
                "新NPC": { ... },     // 可选，首次出现的NPC
            }
        }
    }

    Returns:
        描述文本列表 (用于叙事)
    """
    if not current_life or not social_update:
        return []

    npcs = current_life.get("人物关系", {})
    if not isinstance(npcs, dict):
        npcs = {}
        current_life["人物关系"] = npcs

    messages = []

    for npc_name, update in social_update.items():
        if not isinstance(update, dict):
            continue

        # 处理新 NPC 加入（显式标记 或 隐式首次出现）
        if npc_name not in npcs:
            new_npc_data = update.get("新NPC", {})
            if not isinstance(new_npc_data, dict):
                new_npc_data = {}
            # 即使没有"新NPC"字段，只要NPC名字不在人物关系中，
            # 就自动创建条目（AI可能只输出好感度变化而忘记加"新NPC"标记）
            npc = create_npc_template(
                name=str(npc_name),
                personality=str(new_npc_data.get("性格", update.get("性格", "")) or ""),
                affinity=_safe_int(
                    new_npc_data.get("初始好感度", update.get("初始好感度", 0)), 0
                ),
            )
            npc["身份"] = str(
                new_npc_data.get("身份", update.get("身份", "")) or ""
            )
            npc["关系阶段"] = get_affinity_stage(npc["好感度"])["name"]
            npcs[npc_name] = npc
            messages.append(f"结识新人: {npc_name} ({npc.get('身份', '') or '未知身份'})")
            logger.info(f"新NPC加入: {npc_name} (auto-created={'新NPC' not in update})")

        npc = npcs.get(npc_name)
        if not npc:
            continue

        # 处理突破事件
        breakthrough_raw = update.get("突破阈值")
        breakthrough = _safe_int(breakthrough_raw, 0) if breakthrough_raw is not None else 0
        if breakthrough:
            if process_breakthrough(npc, breakthrough):
                bt_info = BREAKTHROUGH_EVENTS.get(breakthrough, {})
                bt_name = bt_info.get("name", f"阈值{breakthrough}突破")
                messages.append(
                    f"【缘分突破 · {bt_name}】与{npc_name}的羁绊突破了{breakthrough}点瓶颈！"
                )

        # 处理好感度变化
        delta = _safe_int(update.get("好感度变化", 0), 0)
        reason = str(update.get("原因", "") or "")

        # --- 叙事强度修正 ---
        # 分析"原因"中的情感关键词，当AI给的delta与叙事强度不匹配时自动修正
        if delta > 0 and reason:
            intensity = _analyze_narrative_intensity(reason)
            if intensity["tier"] > 0:
                # 如果AI给的delta低于该强度的最低值，自动提升
                if delta < intensity["min_delta"]:
                    old_delta = delta
                    delta = intensity["min_delta"]
                    logger.info(
                        f"叙事强度修正: NPC={npc_name}, "
                        f"tier={intensity['tier']}, "
                        f"delta {old_delta} → {delta}, "
                        f"keywords={intensity['matched_keywords']}, "
                        f"reason='{reason}'"
                    )

                # 该强度可以突破的瓶颈——自动执行突破
                max_bt = intensity["max_breakthrough"]
                breakthroughs = _ensure_int_list(npc.get("已突破阈值", []))
                current_score = _safe_int(npc.get("好感度", 0), 0)
                for threshold in BOTTLENECK_THRESHOLDS:
                    if threshold > max_bt:
                        break
                    if threshold not in breakthroughs and current_score + delta >= threshold:
                        if process_breakthrough(npc, threshold):
                            bt_info = BREAKTHROUGH_EVENTS.get(threshold, {})
                            bt_name = bt_info.get("name", f"阈值{threshold}突破")
                            messages.append(
                                f"【缘分突破 · {bt_name}】"
                                f"与{npc_name}的羁绊突破了{threshold}点瓶颈！"
                            )
                            logger.info(
                                f"叙事强度自动突破: NPC={npc_name}, "
                                f"threshold={threshold}, tier={intensity['tier']}, "
                                f"keywords={intensity['matched_keywords']}"
                            )

        if delta != 0:
            result = apply_affinity_change(npc, delta, reason)

            # --- 回退自动突破（无关键词但delta足够大） ---
            # 当叙事强度系统没有匹配到关键词，但AI给了很大的delta
            # （说明AI意图突破），且delta >= 15时才触发
            if result["bottleneck_hit"] and delta >= 15:
                hit_bn = result["bottleneck_hit"]
                old_score = result["old_score"]
                intended_score = old_score + delta
                if intended_score > hit_bn:
                    logger.info(
                        f"回退自动突破: NPC={npc_name}, "
                        f"intended={intended_score} > bottleneck={hit_bn}, "
                        f"delta={delta}, reason='{reason}'"
                    )
                    if process_breakthrough(npc, hit_bn):
                        bt_info = BREAKTHROUGH_EVENTS.get(hit_bn, {})
                        bt_name = bt_info.get("name", f"阈值{hit_bn}突破")
                        messages.append(
                            f"【缘分突破 · {bt_name}】与{npc_name}的羁绊突破了{hit_bn}点瓶颈！"
                        )
                        # 突破后补回被截断的好感度
                        result = apply_affinity_change(npc, 0, "")
                        remaining = intended_score - hit_bn
                        if remaining > 0:
                            result = apply_affinity_change(npc, remaining, reason + "(突破后)")

            # 更新关系阶段
            npc["关系阶段"] = result["stage"]

            if result["stage_changed"]:
                messages.append(
                    f"与{npc_name}的关系变为【{result['stage']}】"
                    f"(好感度: {result['old_score']} → {result['new_score']})"
                )
            if result["bottleneck_hit"]:
                messages.append(
                    f"与{npc_name}的好感度已达瓶颈({result['bottleneck_hit']})，"
                    f"需要更深的羁绊事件方能突破。"
                )

        # 处理特殊标记 — AI may return str, list, nested list, etc.
        special_mark_raw = update.get("特殊标记")
        if special_mark_raw:
            new_marks = _ensure_str_list(special_mark_raw)
            existing = _ensure_str_list(npc.get("特殊标记", []))
            for m in new_marks:
                if m and m not in existing:
                    existing.append(m)
                    messages.append(f"与{npc_name}建立了特殊关系:【{m}】")
            npc["特殊标记"] = existing

        # 处理NPC战斗/修炼属性更新
        # AI 可以在社交更新中附带 NPC 的战斗属性变化
        _NPC_COMBAT_FIELDS = ("境界", "功法", "战力", "物品", "生命值", "最大生命值")
        for field in _NPC_COMBAT_FIELDS:
            if field in update:
                old_val = npc.get(field)
                new_val = update[field]
                npc[field] = new_val
                if old_val != new_val and new_val is not None:
                    logger.info(f"NPC属性更新: {npc_name}.{field} = {new_val}")

    current_life["人物关系"] = npcs
    return messages


def get_social_summary(current_life: dict) -> list[dict]:
    """
    获取当前所有 NPC 关系的摘要信息，用于前端展示。
    """
    npcs = current_life.get("人物关系", {})
    if not isinstance(npcs, dict):
        return []

    summary = []
    for name, npc in npcs.items():
        if not isinstance(npc, dict):
            continue
        score = _safe_int(npc.get("好感度", 0), 0)
        stage = get_affinity_stage(score)
        summary.append({
            "name": str(name),
            "score": score,
            "stage": stage["name"],
            "stage_key": stage["key"],
            "personality": str(npc.get("性格", "") or ""),
            "identity": str(npc.get("身份", "") or ""),
            "special_marks": _ensure_str_list(npc.get("特殊标记", [])),
            "bottleneck": get_current_bottleneck(score, npc.get("已突破阈值", [])),
        })

    # 按好感度降序排列
    summary.sort(key=lambda x: x["score"], reverse=True)
    return summary


def inject_social_context_for_ai(current_life: dict) -> str:
    """
    生成供 AI 参考的社交状态摘要文本，注入到 prompt 中。
    """
    npcs = current_life.get("人物关系", {})
    if not npcs:
        return ""

    lines = ["【当前人物关系】"]
    for name, npc in npcs.items():
        if not isinstance(npc, dict):
            continue
        score = _safe_int(npc.get("好感度", 0), 0)
        stage = get_affinity_stage(score)
        bottleneck = get_current_bottleneck(score, npc.get("已突破阈值", []))
        marks = _ensure_str_list(npc.get("特殊标记", []))
        mark_str = f" [{', '.join(marks)}]" if marks else ""

        line = f"  - {name}: 好感度={score} ({stage['name']}){mark_str}"
        if bottleneck:
            line += f" [瓶颈:{bottleneck}]"
        lines.append(line)

    return "\n".join(lines)
