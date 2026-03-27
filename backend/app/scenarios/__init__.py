"""
剧本系统 (Scenario System)
============================
管理可选的游戏剧本模式。

剧本类型:
- "freestyle": 默认休闲修仙模式（原始玩法）
- "doupo":  斗破苍穹世界
- "douluo": 斗罗大陆世界
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SCENARIO_DIR = Path(__file__).parent

# 剧本注册表
SCENARIOS = {
    "freestyle": {
        "id": "freestyle",
        "name": "浮生修仙",
        "description": "经典休闲修仙模式，随机生成角色与世界",
        "icon": "🌙",
        "visible": True,
    },
    "doupo": {
        "id": "doupo",
        "name": "斗破苍穹",
        "description": "化身斗气大陆的一员，从斗之气开始修炼，争夺异火、对抗魂殿",
        "icon": "🔥",
        "visible": True,
    },
    "douluo": {
        "id": "douluo",
        "name": "斗罗大陆",
        "description": "踏入武魂的世界，觉醒武魂、猎杀魂兽、争夺神位",
        "icon": "💎",
        "visible": False,  # 暂不开放，数据已就绪
    },
}

# 缓存已加载的剧本数据
_scenario_cache: dict[str, dict] = {}


def list_scenarios() -> list[dict]:
    """返回所有对玩家可见的剧本基本信息列表。"""
    return [s for s in SCENARIOS.values() if s.get("visible", True)]


def get_scenario_data(scenario_id: str) -> dict | None:
    """
    获取指定剧本的完整数据（含世界观、人物、境界体系等）。
    首次调用时从JSON文件加载并缓存。
    """
    if scenario_id == "freestyle" or scenario_id not in SCENARIOS:
        return None

    if scenario_id in _scenario_cache:
        return _scenario_cache[scenario_id]

    data_file = _SCENARIO_DIR / f"{scenario_id}.json"
    if not data_file.exists():
        logger.warning(f"剧本数据文件不存在: {data_file}")
        return None

    try:
        with open(data_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 同时加载时间线文件（如果存在）
        timeline_file = _SCENARIO_DIR / f"{scenario_id}_timeline.json"
        if timeline_file.exists():
            with open(timeline_file, "r", encoding="utf-8") as f:
                data["_timeline"] = json.load(f)
            logger.info(f"剧本时间线加载成功: {scenario_id}")
        _scenario_cache[scenario_id] = data
        logger.info(f"剧本数据加载成功: {scenario_id} ({len(json.dumps(data, ensure_ascii=False))} chars)")
        return data
    except Exception as e:
        logger.error(f"剧本数据加载失败: {scenario_id}: {e}")
        return None


def build_scenario_system_prompt(scenario_id: str) -> str | None:
    """
    构建剧本专用的 system prompt 片段。
    包含世界观补充 + 简化时间线（供 AI 参考剧情走向、触发机缘副本）。
    """
    data = get_scenario_data(scenario_id)
    if not data:
        return None

    supplement = data.get("system_prompt_supplement", "")

    # 注入时间线摘要（基于 story_timeline 字段）
    timeline = data.get("story_timeline", [])
    if timeline:
        tl_lines = ["\n\n## 【剧情时间线参考·GM专用】\n"]
        tl_lines.append(
            "以下是原著剧情的关键时间线。你应根据玩家当前境界和所在地，"
            "参考对应弧段来推进剧情、安排NPC出场、触发机缘事件。\n"
            "**你不必完全按原著走，但应以此为骨架自由演绎。**\n"
            "**当玩家境界或位置与某弧段匹配时，应自然引入该弧段的事件和NPC。**\n"
        )

        for arc in timeline:
            tl_lines.append(
                f"\n**弧段{arc['arc_id']}: {arc['arc_name']}**"
                f"（境界: {arc['protagonist_realm']}，地点: {arc['location']}）"
            )
            # 事件摘要
            for ev in arc.get("events", []):
                tl_lines.append(
                    f"  · {ev.get('name', ev.get('event', '?'))}"
                    f"（{ev.get('what', '')}）"
                    f" [谁:{ev.get('who','')}]"
                    f" [因:{ev.get('why','')}]"
                    f" [果:{ev.get('result','')}]"
                )
            # 机缘提示
            opps = arc.get("opportunity_triggers", arc.get("opportunities", []))
            if opps:
                tl_lines.append(f"  🌟 可触发机缘: {', '.join(opps)}")
            # 关键NPC
            npcs = arc.get("key_npcs", [])
            if npcs:
                npc_str = "; ".join(
                    f"{n['name']}({n['realm']})" for n in npcs
                )
                tl_lines.append(f"  [NPC] {npc_str}")

        supplement += "\n".join(tl_lines)

    return supplement


def get_character_preset(scenario_id: str, player_name: str) -> dict | None:
    """
    获取指定剧本中某个预设角色的完整数据。
    返回 None 如果不是预设角色。
    """
    data = get_scenario_data(scenario_id)
    if not data or not player_name:
        return None
    return data.get("character_presets", {}).get(player_name)


def build_scenario_start_prompt(scenario_id: str, player_name: str = "",
                                 companion_mode: str = "同行（生成初始伙伴）") -> str | None:
    """
    构建剧本专用的开局 prompt。
    如果玩家选择了预设角色，将角色预设数据（含人物关系）直接注入 prompt。
    人物关系由后端程序化注入 session，不再依赖 AI 复制。
    """
    data = get_scenario_data(scenario_id)
    if not data:
        return None

    tmpl = data.get("start_prompt_template", "")
    if not tmpl:
        return None

    prompt = tmpl.replace("{player_name}", player_name or "").replace(
        "{companion_mode}", companion_mode
    )

    # 如果玩家选的角色有预设数据，注入到 prompt 中
    presets = data.get("character_presets", {})
    preset = presets.get(player_name) if player_name else None

    if preset:
        import json as _json
        # 构建预设注入段
        relations = preset.get("人物关系", {})
        npc_names = list(relations.keys())

        # 开局场景描述
        opening = preset.get("opening_scenario", "")
        opening_section = ""
        if opening:
            opening_section = (
                f"\n### 开局场景（opening_scenario）\n\n"
                f"{opening}\n"
            )

        # 预设物品
        preset_items = preset.get("物品", [])
        items_section = ""
        if preset_items:
            items_desc = ", ".join(
                i.get("名称", str(i)) if isinstance(i, dict) else str(i)
                for i in preset_items
            )
            items_section = (
                f"\n### 预设物品（系统自动注入）\n\n"
                f"以下物品已由系统**自动注入**到角色物品栏中: {items_desc}\n"
                f"你的叙事中应自然提及这些关键物品的存在。\n"
            )

        preset_section = (
            f"\n\n## 【系统预设·必须严格使用】\n\n"
            f"玩家魂穿的角色「{player_name}」有以下预设信息，你**必须完全采用**，不可修改或省略：\n\n"
            f"- 出身: {preset.get('出身', '')}\n"
            f"- 初始境界: {preset.get('初始境界', '')}\n"
            f"- 初始天赋: {preset.get('初始天赋', '')}\n"
            f"- 位置: {preset.get('位置', '')}\n"
            f"- 功法: {_json.dumps(preset.get('功法', []), ensure_ascii=False)}\n"
            f"{opening_section}"
            f"{items_section}\n"
            f"### 人物关系（系统自动注入，无需在 state_update 中输出）\n\n"
            f"以下NPC的人物关系数据（含好感度、境界等）已由系统**自动注入**到 session 中，"
            f"你**不需要**在 `state_update` 中输出 `current_life.人物关系`。\n\n"
            f"已注入的NPC: {', '.join(npc_names)}\n\n"
            f"你的叙事中应自然地提到**当前场景中在场**的NPC，"
            f"但不要提及尚未在剧情中登场的NPC。\n"
        )
        prompt += preset_section
        logger.info(
            f"剧本预设注入: {player_name}, {len(relations)} NPCs (关系由后端注入)"
        )
    else:
        # 非预设角色，提示 AI 自由生成
        prompt += (
            f"\n\n## 【提示】\n\n"
            f"「{player_name or '（未指定）'}」不是预设角色，请在{data.get('world_name', '该世界')}中"
            f"为其自由生成身份、出身、人物关系等全部信息。\n"
        )

    return prompt
