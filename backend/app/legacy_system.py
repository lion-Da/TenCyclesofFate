"""
继承系统 (Legacy System)
========================

核心理念:
- 玩家每次通关"破碎虚空"带出的灵石，除了生成兑换码外，
  还会按比例转化为"功德点"(Legacy Points)存入永久账户。
- 功德点跨局持久化，不随每日重置而消失。
- 新一局开始时，玩家可以消耗功德点兑换"先天奖励"，
  为新角色提供初始优势。

数据存储: 使用独立的 JSON 文件按玩家存储，与 session 数据隔离。
"""

import json
import logging
import time
from pathlib import Path
from typing import Any
import aiofiles

logger = logging.getLogger(__name__)

# --- 存储路径 ---
LEGACY_DIR = Path("game_data") / "legacy"

# --- 功德点转化率 ---
# 灵石 → 功德点的转化公式: legacy_points = spirit_stones * CONVERSION_RATE
CONVERSION_RATE = 0.1  # 10% 转化率

# --- 先天奖励定义 ---
# 每个奖励的结构:
#   id: 唯一标识
#   name: 显示名称
#   description: 描述
#   cost: 消耗的功德点
#   effect: 效果类型和数值（供 game_logic 在开局时应用）
#   category: 分类
INNATE_BLESSINGS: list[dict] = [
    # --- 属性强化类 ---
    {
        "id": "attr_boost_small",
        "name": "灵根微淬",
        "description": "天道留痕，前世修为未尽散去。新生角色随机一项属性 +10。",
        "cost": 50,
        "effect": {"type": "random_attribute_boost", "value": 10},
        "category": "属性强化",
    },
    {
        "id": "attr_boost_medium",
        "name": "仙骨初凝",
        "description": "前世苦修凝为仙骨。新生角色随机两项属性各 +15。",
        "cost": 150,
        "effect": {"type": "random_attribute_boost_multi", "count": 2, "value": 15},
        "category": "属性强化",
    },
    {
        "id": "attr_boost_large",
        "name": "天命之躯",
        "description": "携带前世全部修为转世。新生角色所有属性 +8。",
        "cost": 300,
        "effect": {"type": "all_attribute_boost", "value": 8},
        "category": "属性强化",
    },

    # --- 生存保障类 ---
    {
        "id": "extra_hp",
        "name": "金刚不坏",
        "description": "前世护体真元残留，最大生命值 +30。",
        "cost": 80,
        "effect": {"type": "hp_boost", "value": 30},
        "category": "生存保障",
    },
    {
        "id": "death_save",
        "name": "逆死还生",
        "description": "携带一枚前世遗留的护命符，在濒死时自动触发，恢复30%生命值（仅限一次）。",
        "cost": 200,
        "effect": {"type": "death_save", "charges": 1},
        "category": "生存保障",
    },

    # --- 资源类 ---
    {
        "id": "starting_stones",
        "name": "前世余财",
        "description": "前世部分灵石穿越轮回随你而来。初始灵石 +100。",
        "cost": 100,
        "effect": {"type": "starting_spirit_stones", "value": 100},
        "category": "资源",
    },
    {
        "id": "starting_item",
        "name": "轮回遗物",
        "description": "获得一件前世遗留的随机珍贵道具。",
        "cost": 120,
        "effect": {"type": "starting_item", "rarity": "rare"},
        "category": "资源",
    },

    # --- 判定强化类 ---
    {
        "id": "luck_boost",
        "name": "紫气东来",
        "description": "天道眷顾，本局所有判定成功率 +5%。",
        "cost": 250,
        "effect": {"type": "global_roll_bonus", "value": 5},
        "category": "判定强化",
    },
    {
        "id": "reroll_token",
        "name": "改命珠",
        "description": "获得一次重投骰子的机会（本局限用一次）。",
        "cost": 180,
        "effect": {"type": "reroll", "charges": 1},
        "category": "判定强化",
    },

    # --- 特殊类 ---
    {
        "id": "extra_opportunity",
        "name": "天道加恩",
        "description": "今日额外获得一次试炼机缘（11次）。",
        "cost": 500,
        "effect": {"type": "extra_opportunity", "value": 1},
        "category": "特殊",
    },
]


def _get_legacy_path(player_id: str) -> Path:
    """获取玩家的继承数据文件路径"""
    safe_id = player_id.replace("/", "_").replace("\\", "_")
    return LEGACY_DIR / f"{safe_id}.json"


async def _read_legacy(player_id: str) -> dict:
    """读取玩家的继承数据"""
    path = _get_legacy_path(player_id)
    try:
        if not path.exists():
            return _default_legacy(player_id)
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            content = await f.read()
            data = json.loads(content)
            # 兼容旧数据
            if "player_id" not in data:
                data["player_id"] = player_id
            return data
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"读取继承数据失败 {player_id}: {e}")
        return _default_legacy(player_id)


async def _write_legacy(player_id: str, data: dict):
    """写入玩家的继承数据"""
    path = _get_legacy_path(player_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(data, ensure_ascii=False, indent=2))
    except IOError as e:
        logger.error(f"写入继承数据失败 {player_id}: {e}")


def _default_legacy(player_id: str) -> dict:
    """创建默认的继承数据"""
    return {
        "player_id": player_id,
        "legacy_points": 0,
        "total_earned": 0,
        "total_spent": 0,
        "runs_completed": 0,
        "best_spirit_stones": 0,
        "history": [],  # 历史记录: [{date, spirit_stones, points_earned, ...}]
        "active_blessings": [],  # 当前局生效的先天奖励ID列表
    }


# --- 公开 API ---

async def get_legacy_data(player_id: str) -> dict:
    """
    获取玩家的完整继承数据（包括可用奖励列表）。
    供前端展示用。
    """
    data = await _read_legacy(player_id)
    return {
        "legacy_points": data.get("legacy_points", 0),
        "total_earned": data.get("total_earned", 0),
        "total_spent": data.get("total_spent", 0),
        "runs_completed": data.get("runs_completed", 0),
        "best_spirit_stones": data.get("best_spirit_stones", 0),
        "active_blessings": data.get("active_blessings", []),
        "available_blessings": INNATE_BLESSINGS,
        "history": data.get("history", [])[-20:],  # 只返回最近20条
    }


async def add_legacy_points(
    player_id: str,
    spirit_stones: int,
    session: dict | None = None,
    difficulty_multiplier: float = 1.0,
) -> dict:
    """
    通关时根据角色综合状态评估功德点并存入。

    评估维度 (总分 0-150):
      - 境界/修为等级 (0-70分) — 指数关系，元婴(lv4)起步才有分
        公式: score = min(70, floor(3 * 1.65^level))
        元婴≈22, 化神≈37, 炼虚≈61, 合体/大乘/渡劫/仙=70(封顶)
      - 灵石数量 (0-30分) — 对数映射
      - 道具丰富度 (0-25分)
      - 属性总和 (0-25分)

    最终点数 = 综合评分 × difficulty_multiplier

    Args:
        player_id: 玩家ID
        spirit_stones: 本局带出的灵石数
        session: 当前游戏 session（用于读取 current_life）
        difficulty_multiplier: 难度系数 (0=无收益, 0.5=半倍, 1=正常, 1.5=1.5倍)

    Returns:
        {"points_earned": int, "total_points": int, "breakdown": dict}
    """
    data = await _read_legacy(player_id)

    # ── 综合评估 (总分 0-150) ──
    # 境界是核心维度，采用指数关系，低于元婴不得分
    # 其他维度作为辅助加成
    import math as _math

    breakdown = {}
    total_score = 0

    current_life = (session or {}).get("current_life") or {}

    # 1) 境界/修为 (0-70) — 指数关系，元婴起步
    #    境界等级: 练气=1, 筑基=2, 金丹=3, 元婴=4, 化神=5, 炼虚=6, 合体=7, 大乘=8, 渡劫=9, 飞升/仙=10
    #    元婴(4)及以上才有功德点，公式: score = floor(3 * 1.65^level)
    #    元婴=3*1.65^4 ≈ 22, 化神≈37, 炼虚≈61, 合体≈70(cap), 大乘=70, 渡劫=70, 仙=70
    realm_score = 0
    realm = current_life.get("境界", current_life.get("修为", current_life.get("修为境界", "")))
    realm_level = 0
    if isinstance(realm, str) and realm:
        realm_tiers = {
            1: ["练气", "凡人", "入门"],
            2: ["筑基", "开光"],
            3: ["金丹", "结丹"],
            4: ["元婴"],
            5: ["化神"],
            6: ["炼虚"],
            7: ["合体"],
            8: ["大乘"],
            9: ["渡劫"],
            10: ["飞升", "仙", "圣", "天仙", "真仙", "金仙"],
        }
        for level, keywords in realm_tiers.items():
            if any(kw in realm for kw in keywords):
                realm_level = level
        if realm_level == 0 and realm:
            realm_level = 1  # 有境界描述但未匹配到，视为最低阶
    # 元婴(level=4)及以上才产生功德点
    if realm_level >= 4:
        realm_score = min(70, int(3 * (1.65 ** realm_level)))
    breakdown["realm"] = realm_score
    breakdown["realm_level"] = realm_level
    total_score += realm_score

    # 2) 灵石 (0-30) — 对数映射
    stone_score = 0
    if spirit_stones > 0:
        # 1石≈2分, 100石≈12分, 1000石≈17分, 10000石≈22分, 100000石≈27分
        stone_score = min(30, int(2 + 5 * _math.log10(max(1, spirit_stones))))
    breakdown["spirit_stones"] = stone_score
    total_score += stone_score

    # 3) 道具丰富度 (0-25)
    item_score = 0
    items = current_life.get("物品", [])
    if isinstance(items, list):
        item_count = len(items)
        # 每个道具约2.5分，上限25
        item_score = min(25, int(item_count * 2.5))
        # 高品质道具额外加分
        for item in items:
            if isinstance(item, dict):
                quality = str(item.get("品质", item.get("稀有度", "")))
                if any(kw in quality for kw in ["传说", "仙", "神", "SSR"]):
                    item_score = min(25, item_score + 4)
                elif any(kw in quality for kw in ["史诗", "极品", "SR"]):
                    item_score = min(25, item_score + 2)
    breakdown["items"] = item_score
    total_score += item_score

    # 4) 属性总和 (0-25)
    attr_score = 0
    attributes = current_life.get("属性", {})
    if isinstance(attributes, dict):
        attr_sum = 0
        for v in attributes.values():
            try:
                attr_sum += int(v)
            except (ValueError, TypeError):
                pass
        # 典型总和 300-500 对应 10-16 分, 上限 25
        attr_score = min(25, max(0, int(attr_sum / 30)))
    breakdown["attributes"] = attr_score
    total_score += attr_score

    # 钳制总分到 0-150
    total_score = max(0, min(150, total_score))
    breakdown["total_score"] = total_score

    # 应用难度系数
    points_earned = max(0, int(total_score * difficulty_multiplier))
    breakdown["difficulty_multiplier"] = difficulty_multiplier
    breakdown["final_points"] = points_earned

    if points_earned == 0:
        logger.info(
            f"玩家 {player_id} 功德点为 0 (难度系数={difficulty_multiplier})，跳过写入"
        )
        return {
            "points_earned": 0,
            "total_points": data.get("legacy_points", 0),
            "breakdown": breakdown,
        }

    data["legacy_points"] = data.get("legacy_points", 0) + points_earned
    data["total_earned"] = data.get("total_earned", 0) + points_earned
    data["runs_completed"] = data.get("runs_completed", 0) + 1
    data["best_spirit_stones"] = max(
        data.get("best_spirit_stones", 0), spirit_stones
    )
    data["history"].append({
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "spirit_stones": spirit_stones,
        "points_earned": points_earned,
        "total_after": data["legacy_points"],
        "breakdown": breakdown,
    })

    # 只保留最近100条历史
    data["history"] = data["history"][-100:]

    await _write_legacy(player_id, data)

    logger.info(
        f"玩家 {player_id} 获得 {points_earned} 功德点 "
        f"(评分={total_score}, 难度系数={difficulty_multiplier}, "
        f"灵石={spirit_stones})，总计 {data['legacy_points']}"
    )

    return {
        "points_earned": points_earned,
        "total_points": data["legacy_points"],
        "breakdown": breakdown,
    }


async def purchase_blessing(player_id: str, blessing_id: str) -> dict:
    """
    购买一个先天奖励。
    
    Returns:
        {"success": bool, "message": str, "remaining_points": int}
    """
    # 查找奖励定义
    blessing = None
    for b in INNATE_BLESSINGS:
        if b["id"] == blessing_id:
            blessing = b
            break

    if not blessing:
        return {"success": False, "message": "此奖励不存在。", "remaining_points": 0}

    data = await _read_legacy(player_id)
    current_points = data.get("legacy_points", 0)

    if current_points < blessing["cost"]:
        return {
            "success": False,
            "message": f"功德点不足。需要 {blessing['cost']}，当前 {current_points}。",
            "remaining_points": current_points,
        }

    # 检查是否已购买（某些奖励可能只能买一次/局）
    active = data.get("active_blessings", [])
    if blessing_id in active:
        return {
            "success": False,
            "message": "此奖励已在本局中激活，不可重复购买。",
            "remaining_points": current_points,
        }

    # 扣除功德点
    data["legacy_points"] = current_points - blessing["cost"]
    data["total_spent"] = data.get("total_spent", 0) + blessing["cost"]
    data["active_blessings"] = active + [blessing_id]

    await _write_legacy(player_id, data)

    logger.info(
        f"玩家 {player_id} 购买先天奖励: {blessing['name']} "
        f"(花费 {blessing['cost']}，剩余 {data['legacy_points']})"
    )

    return {
        "success": True,
        "message": f"成功激活「{blessing['name']}」！",
        "remaining_points": data["legacy_points"],
    }


async def clear_active_blessings(player_id: str):
    """
    清除当前局的激活奖励（新一天/新周期时调用）。
    注意：功德点不退回，只是清除激活状态。
    """
    data = await _read_legacy(player_id)
    data["active_blessings"] = []
    await _write_legacy(player_id, data)


async def get_active_blessings(player_id: str) -> list[dict]:
    """
    获取当前激活的先天奖励的完整定义列表。
    供 game_logic 在开局时应用效果。
    """
    data = await _read_legacy(player_id)
    active_ids = set(data.get("active_blessings", []))

    result = []
    for b in INNATE_BLESSINGS:
        if b["id"] in active_ids:
            result.append(b)
    return result


async def apply_blessings_to_session(player_id: str, session: dict) -> dict:
    """
    将激活的先天奖励应用到新创建的 session 上。
    在试炼开始时由 game_logic 调用。
    
    Args:
        player_id: 玩家ID
        session: 当前 session 字典(含 current_life)
    
    Returns:
        修改后的 session
    """
    blessings = await get_active_blessings(player_id)
    if not blessings:
        return session

    current_life = session.get("current_life")
    if not current_life:
        return session

    import random as _random

    applied_effects = []

    for blessing in blessings:
        effect = blessing.get("effect", {})
        effect_type = effect.get("type")

        if effect_type == "random_attribute_boost":
            attributes = current_life.get("属性", {})
            if attributes:
                attr_name = _random.choice(list(attributes.keys()))
                try:
                    attributes[attr_name] = int(attributes[attr_name]) + effect["value"]
                    applied_effects.append(f"{blessing['name']}({attr_name}+{effect['value']})")
                except (ValueError, TypeError):
                    pass

        elif effect_type == "random_attribute_boost_multi":
            attributes = current_life.get("属性", {})
            if attributes and len(attributes) >= effect.get("count", 1):
                chosen = _random.sample(list(attributes.keys()), min(effect["count"], len(attributes)))
                for attr_name in chosen:
                    try:
                        attributes[attr_name] = int(attributes[attr_name]) + effect["value"]
                        applied_effects.append(f"{blessing['name']}({attr_name}+{effect['value']})")
                    except (ValueError, TypeError):
                        pass

        elif effect_type == "all_attribute_boost":
            attributes = current_life.get("属性", {})
            for attr_name in attributes:
                try:
                    attributes[attr_name] = int(attributes[attr_name]) + effect["value"]
                except (ValueError, TypeError):
                    pass
            if attributes:
                applied_effects.append(f"{blessing['name']}(全属性+{effect['value']})")

        elif effect_type == "hp_boost":
            try:
                current_life["最大生命值"] = int(current_life.get("最大生命值", 100)) + effect["value"]
                current_life["生命值"] = current_life["最大生命值"]
                applied_effects.append(f"{blessing['name']}(HP+{effect['value']})")
            except (ValueError, TypeError):
                pass

        elif effect_type == "death_save":
            status_effects = current_life.get("状态效果", [])
            status_effects.append(f"护命符(剩余{effect.get('charges', 1)}次)")
            current_life["状态效果"] = status_effects
            applied_effects.append(f"{blessing['name']}")

        elif effect_type == "starting_spirit_stones":
            try:
                current_life["灵石"] = int(current_life.get("灵石", 1)) + effect["value"]
                applied_effects.append(f"{blessing['name']}(灵石+{effect['value']})")
            except (ValueError, TypeError):
                pass

        elif effect_type == "starting_item":
            items = current_life.get("物品", [])
            items.append({
                "名称": "轮回遗物",
                "数量": 1,
                "效果": "前世遗留之物，蕴含神秘力量",
                "品质": effect.get("rarity", "rare"),
            })
            current_life["物品"] = items
            applied_effects.append(f"{blessing['name']}")

        elif effect_type == "global_roll_bonus":
            # 存储到 session 级别供 dice_system 读取
            session["legacy_roll_bonus"] = session.get("legacy_roll_bonus", 0) + effect["value"]
            applied_effects.append(f"{blessing['name']}(判定+{effect['value']}%)")

        elif effect_type == "reroll":
            session["reroll_charges"] = session.get("reroll_charges", 0) + effect.get("charges", 1)
            applied_effects.append(f"{blessing['name']}")

        elif effect_type == "extra_opportunity":
            session["opportunities_remaining"] = (
                session.get("opportunities_remaining", 10) + effect.get("value", 1)
            )
            applied_effects.append(f"{blessing['name']}(+{effect['value']}次机缘)")

    # 记录应用了哪些效果（可选，用于叙事）
    if applied_effects:
        session["applied_blessings_desc"] = applied_effects
        logger.info(f"玩家 {player_id} 应用先天奖励: {', '.join(applied_effects)}")

    return session
