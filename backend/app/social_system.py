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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 核心函数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


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

    AI 在 state_update 中创建新 NPC 时应遵循此结构。
    """
    return {
        "姓名": name,
        "好感度": max(MIN_AFFINITY, min(MAX_AFFINITY, affinity)),
        "关系阶段": relation,
        "性格": personality,
        "身份": "",
        "已突破阈值": [],
        "好感度变动记录": [],
        "特殊标记": [],  # 如 "结拜", "道侣", "宿敌" 等
    }


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
        if delta != 0:
            result = apply_affinity_change(npc, delta, reason)

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
