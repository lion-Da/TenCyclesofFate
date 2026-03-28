import logging
import math
import random
import json
import asyncio
import time
import traceback
import base64
from copy import deepcopy
from datetime import date
from pathlib import Path
from fastapi import HTTPException, status

from . import state_manager, cheat_check, redemption
from . import dice_system, legacy_system, social_system, cultivation_system
from . import ai_service
from .websocket_manager import manager as websocket_manager
from .config import settings

# --- Logging ---
logger = logging.getLogger(__name__)

# --- Game Constants ---
INITIAL_OPPORTUNITIES = 10
REWARD_SCALING_FACTOR = 500000  # Previously LOGARITHM_CONSTANT_C

# --- Difficulty Presets ---
# Each difficulty defines:
#   roll_modifier: added to base_target (percentage points) before roll
#   attr_min / attr_max: clamp range for initial attributes (None = no clamp)
#   auto_success: if True, all rolls are forced to succeed
#   legacy_multiplier: multiplier for legacy points earned at endgame
DIFFICULTY_PRESETS = {
    "气运之父": {
        "roll_modifier": None,       # not used — auto_success overrides
        "auto_success": True,
        "attr_min": 60,
        "attr_max": None,
        "legacy_multiplier": 0.0,    # 无法获得继承点
        "label": "气运之父",
        "description": "Roll点必成功，属性下限60，无法获得继承点",
    },
    "气运之子": {
        "roll_modifier": 50,         # target +50% of sides (绝对值)
        "auto_success": False,
        "attr_min": 50,
        "attr_max": 80,
        "legacy_multiplier": 0.5,
        "label": "气运之子",
        "description": "Roll成功率+50%，属性50-80，继承点×0.5",
    },
    "凡人修仙": {
        "roll_modifier": 0,
        "auto_success": False,
        "attr_min": None,
        "attr_max": None,
        "legacy_multiplier": 1.0,
        "label": "凡人修仙",
        "description": "正常模式",
    },
    "绝处逢生": {
        "roll_modifier": -25,        # target -25% of sides (绝对值)
        "auto_success": False,
        "attr_min": None,
        "attr_max": 30,
        "legacy_multiplier": 1.5,
        "label": "绝处逢生",
        "description": "Roll成功率-25%，属性上限30，继承点×1.5",
    },
}
DEFAULT_DIFFICULTY = "凡人修仙"


def _get_difficulty_preset(session: dict) -> dict:
    """Get the difficulty preset for a session, defaulting to 凡人修仙."""
    name = session.get("difficulty", DEFAULT_DIFFICULTY)
    return DIFFICULTY_PRESETS.get(name, DIFFICULTY_PRESETS[DEFAULT_DIFFICULTY])


def _clamp_attributes(current_life: dict, preset: dict) -> dict:
    """
    Clamp all numeric attributes in current_life.属性 according to difficulty preset.
    If a value is outside [attr_min, attr_max], re-randomize it within that range.
    This ensures attributes feel naturally rolled within bounds, not just clamped.
    Returns modified current_life (in-place).
    """
    import random as _random
    attributes = current_life.get("属性")
    if not attributes or not isinstance(attributes, dict):
        return current_life

    attr_min = preset.get("attr_min")
    attr_max = preset.get("attr_max")

    if attr_min is None and attr_max is None:
        return current_life

    lo = attr_min if attr_min is not None else 1
    hi = attr_max if attr_max is not None else 200

    for key in attributes:
        try:
            val = int(attributes[key])
            if (attr_min is not None and val < attr_min) or (attr_max is not None and val > attr_max):
                # Re-randomize within allowed range instead of hard clamping
                val = _random.randint(lo, hi)
            attributes[key] = val
        except (ValueError, TypeError):
            continue
    return current_life


def _apply_talent_bonuses(current_life: dict) -> None:
    """
    Parse talent description for attribute bonuses and apply them.
    Supports patterns like:
      "魂穿（灵觉、意志+10）" → 灵觉+10, 意志+10
      "天资聪颖（悟性+15）"   → 悟性+15
    Bonuses are applied AFTER difficulty clamping, so they are final.
    """
    import re as _re
    talent = current_life.get("初始天赋", "")
    if not talent or not isinstance(talent, str):
        return

    attributes = current_life.get("属性")
    if not attributes or not isinstance(attributes, dict):
        return

    # Find all patterns like "（xxx+N）" or "(xxx+N)"
    # Matches: 灵觉、意志+10  or  悟性+15  or  根骨、气运+5
    bonus_patterns = _re.findall(r'[（(]([^）)]+\+\d+)[）)]', talent)
    if not bonus_patterns:
        return

    applied = []
    for pattern in bonus_patterns:
        # Extract bonus value: last +N in the pattern
        match = _re.search(r'\+(\d+)$', pattern.strip())
        if not match:
            continue
        bonus = int(match.group(1))
        # Everything before the +N is the attribute list
        attr_part = pattern[:match.start()].strip()
        # Split by Chinese/English comma and 、
        attr_names = _re.split(r'[,，、]', attr_part)
        attr_names = [a.strip() for a in attr_names if a.strip()]

        for attr_name in attr_names:
            if attr_name in attributes:
                try:
                    old_val = int(attributes[attr_name])
                    attributes[attr_name] = min(200, old_val + bonus)
                    applied.append(f"{attr_name}: {old_val}+{bonus}={attributes[attr_name]}")
                except (ValueError, TypeError):
                    continue

    if applied:
        logger.info(f"天赋属性加成: {', '.join(applied)}")


# --- Image Generation State ---
# 记录每个玩家的最后活动时间，用于判断是否触发图片生成
_pending_image_tasks: dict[str, asyncio.Task] = {}


# --- Prompt Loading ---
def _load_prompt(filename: str) -> str:
    try:
        prompt_path = Path(__file__).parent / "prompts" / filename
        with open(prompt_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.error(f"Prompt file not found: {filename}")
        return ""


GAME_MASTER_SYSTEM_PROMPT = _load_prompt("game_master.txt")
START_GAME_PROMPT = _load_prompt("start_game_prompt.txt")
START_TRIAL_PROMPT = _load_prompt("start_trial_prompt.txt")


# --- Image Generation Logic ---
def _extract_scene_prompts(session: dict) -> str:
    """
    从 session 中提取场景描述作为图片生成提示词。
    构建方式与 _process_player_action_async 中的 session_copy 类似，
    再加上最新的 narrative。
    """
    session_copy = deepcopy(session)
    session_copy.pop("internal_history", None)
    
    # 获取最新的 narrative（从 display_history 末尾找非用户输入的内容）
    display_history = session_copy.get("display_history", [])
    latest_narrative = ""
    for item in reversed(display_history):
        if item and isinstance(item, str) and not item.strip().startswith(">"):
            # 跳过系统消息和图片
            if not item.startswith("【系统提示") and not item.startswith("!["):
                latest_narrative = item[:500]
                break
    
    # display_history 转为字符串并截取最后 1000 字符
    session_copy["display_history"] = (
        "\n".join(display_history)
    )[-1000:]
    
    # 构建提示词
    prompt = f"当前游戏状态：\n{json.dumps(session_copy, ensure_ascii=False)}"
    if latest_narrative:
        prompt += f"\n\n最新场景：\n{latest_narrative}"
    
    return prompt


async def _delayed_image_generation(player_id: str, trigger_time: float):
    """
    延迟图片生成任务。
    等待指定时间后，检查状态是否仍然静止，如果是则生成图片。
    """
    idle_seconds = settings.IMAGE_GEN_IDLE_SECONDS
    
    try:
        await asyncio.sleep(idle_seconds)
        
        # 检查是否仍然应该生成图片
        session = await state_manager.get_session(player_id)
        if not session:
            logger.debug(f"图片生成取消：玩家 {player_id} 的会话不存在")
            return
        
        # 检查 last_modified 是否变化（说明有新的活动）
        current_modified = session.get("last_modified", 0)
        if current_modified != trigger_time:
            logger.debug(f"图片生成取消：玩家 {player_id} 有新活动")
            return
        
        # 检查是否正在处理中
        if session.get("is_processing"):
            logger.debug(f"图片生成取消：玩家 {player_id} 正在处理中")
            return
        
        # 检查是否在试炼中（只在试炼中生成图片）
        if not session.get("is_in_trial"):
            logger.debug(f"图片生成取消：玩家 {player_id} 不在试炼中")
            return
        
        # 提取场景提示词
        scene_prompt = _extract_scene_prompts(session)
        
        if not scene_prompt:
            logger.debug(f"图片生成取消：玩家 {player_id} 没有有效的场景描述")
            return
        
        logger.info(f"开始为玩家 {player_id} 生成场景图片")
        
        # 调用图片生成
        image_data_url = await ai_service.generate_image(scene_prompt, user_id=player_id)
        
        if image_data_url:
            # 重新获取最新的 session（可能在生成期间有变化）
            session = await state_manager.get_session(player_id)
            if not session:
                return
            
            # 再次检查是否有新活动
            if session.get("last_modified", 0) != trigger_time:
                logger.debug(f"图片生成完成但不插入：玩家 {player_id} 在生成期间有新活动")
                return
            
            # 构建图片 markdown
            image_markdown = f"\n\n![场景插画]({image_data_url})\n"
            
            # 插入到 display_history 末尾
            session["display_history"].append(image_markdown)
            
            # 保存并推送更新
            await state_manager.save_session(player_id, session)
            logger.info(f"玩家 {player_id} 的场景图片已生成并插入")
        else:
            logger.warning(f"玩家 {player_id} 的图片生成失败")
            
    except asyncio.CancelledError:
        logger.debug(f"玩家 {player_id} 的图片生成任务被取消")
    except Exception as e:
        logger.error(f"玩家 {player_id} 的图片生成任务出错: {e}", exc_info=True)
    finally:
        # 清理任务引用
        if player_id in _pending_image_tasks:
            del _pending_image_tasks[player_id]


def _schedule_image_generation(player_id: str, trigger_time: float):
    """
    调度图片生成任务。
    如果已有待处理的任务，先取消它。
    """
    if not ai_service.is_image_gen_enabled():
        return
    
    # 取消之前的任务（如果有）
    if player_id in _pending_image_tasks:
        old_task = _pending_image_tasks[player_id]
        if not old_task.done():
            old_task.cancel()
    
    # 创建新任务
    task = asyncio.create_task(_delayed_image_generation(player_id, trigger_time))
    _pending_image_tasks[player_id] = task


# --- Game Logic ---


async def get_or_create_daily_session(current_user: dict) -> dict:
    player_id = current_user["username"]
    today_str = date.today().isoformat()
    session = await state_manager.get_session(player_id)
    if session and session.get("session_date") == today_str:
        dirty = False

        if session.get("is_processing"):
            session["is_processing"] = False
            dirty = True

        if session.get("daily_success_achieved") and not session.get("redemption_code"):
            session["daily_success_achieved"] = False
            dirty = True

        # ── 重连清理：若当前不在试炼中，清除上次失败/结束试炼残留的历史 ──
        # 玩家登出重登时，如果 is_in_trial==False 且 current_life==None，
        # 说明没有正在进行的试炼。此时 display_history 中可能残留上次失败的
        # 叙事和错误信息，应该清理掉，只保留欢迎横幅 + 一条状态提示。
        # 但如果玩家今天已经成功（有兑换码），保留完整历史让他能看到兑换码。
        if (not session.get("is_in_trial")
            and session.get("current_life") is None
            and not session.get("daily_success_achieved")
            and not session.get("redemption_code")):
            display_hist = session.get("display_history", [])
            # 只保留第一条（欢迎横幅），丢弃其余残留内容
            welcome = display_hist[0] if display_hist else ""
            opps = session.get("opportunities_remaining", INITIAL_OPPORTUNITIES)
            if opps < INITIAL_OPPORTUNITIES:
                # 之前有过试炼但失败了，给一个简要提示
                status_msg = (
                    f"\n\n---\n\n"
                    f"【轮回续缘】\n\n"
                    f"汝已重返此界。前番试炼之因果已随风散去。\n\n"
                    f"> 今日剩余机缘：**{opps}** 次\n\n"
                    f"准备好了，便可开启下一场浮生之梦。"
                )
                new_display = [welcome, status_msg] if welcome else [status_msg]
            else:
                new_display = [welcome] if welcome else []

            if len(display_hist) != len(new_display):
                session["display_history"] = new_display
                # 同时重置 internal_history，避免残留的错误重试指令
                session["internal_history"] = [
                    {"role": "system", "content": GAME_MASTER_SYSTEM_PROMPT}
                ]
                dirty = True
                logger.info(
                    f"Reconnect cleanup for {player_id}: cleared "
                    f"{len(display_hist)} stale display entries, "
                    f"opps={opps}"
                )

        if dirty:
            await state_manager.save_session(player_id, session)

        return session

    logger.info(f"Starting new daily session for {player_id}.")
    new_session = {
        "player_id": player_id,
        "session_date": today_str,
        "opportunities_remaining": INITIAL_OPPORTUNITIES,
        "daily_success_achieved": False,
        "is_in_trial": False,
        "is_processing": False,
        "pending_punishment": None,
        "unchecked_rounds_count": 0,
        "current_life": None,
        "internal_history": [{"role": "system", "content": GAME_MASTER_SYSTEM_PROMPT}],
        "display_history": [
            """
# 《浮生十梦》

【司命星君 · 恭候汝来】

---

汝既踏入此门，便已与命运结缘。

此处非凡俗游戏之地，乃命数轮回之所。无升级打怪之俗套，无氪金商城之铜臭，唯有一道亘古命题横亘于前——知足与贪欲的永恒博弈。

---

【天道法则】

汝每日将获赐十次入梦机缘。每一次，星君将为汝织就全新命数：或为寒窗苦读的穷酸书生，或为仗剑江湖的热血侠客，亦或为孤身求道的散修。万千可能，绝无重复，每一局皆是独一无二的浮生一梦。

试炼规则至简，却蕴玄机：

> 在任何关键时刻，汝皆可选择「破碎虚空」，将此生所得灵石带离此界。然此念一起，今日所有试炼便就此终结，再无回旋。

这便是天道对汝的终极考验：是满足于眼前造化，还是冒失去一切之险继续问道？

灵石价值遵循天道玄理——初得之石最为珍贵，后续所得边际递减。此乃上古圣贤的无上智慧：知足常乐，贪心常忧。

---

【天规须知】

- 每日十次机缘，开启新轮回即消耗一次
- 轮回中道消身殒，所得化为泡影，机缘不返
- 「破碎虚空」成功带出灵石，今日试炼即刻终结
- 天道有眼，明察秋毫——以奇巧咒语欺瞒天机者，必受严惩

---

汝可准备好了？司命星君已恭候多时，静待汝开启第一场浮生之梦。
"""
        ],
        "roll_event": None,
        "redemption_code": None,
    }
    await state_manager.save_session(player_id, new_session)
    return new_session


async def _handle_roll_request(
    player_id: str,
    session: dict,
    last_state: dict,
    roll_request: dict,
    original_action: str,
    first_narrative: str,
    internal_history: list[dict],
) -> tuple[str, dict]:
    roll_type = roll_request.get("type", "判定")
    base_target = roll_request.get("target", 50)
    sides = roll_request.get("sides", 100)

    # 获取角色状态用于属性修正
    current_life = session.get("current_life")

    # 使用新骰子系统：基础成功率 + 属性 + 道具 + 状态
    dice_result = dice_system.roll_dice(
        base_target=base_target,
        sides=sides,
        roll_type=roll_type,
        current_life=current_life,
    )

    # 应用继承系统的全局判定加成
    legacy_bonus = session.get("legacy_roll_bonus", 0)
    if legacy_bonus > 0:
        # 加成后重新计算（在 dice_system 基础上额外加成）
        adjusted_target = min(
            int(sides * 0.95),  # 上限95%
            dice_result["final_target"] + int(legacy_bonus / 100 * sides)
        )
        # 如果 legacy_bonus 改变了 target，重新判定 outcome
        if adjusted_target != dice_result["final_target"]:
            dice_result["final_target"] = adjusted_target
            dice_result["breakdown"]["legacy_bonus"] = legacy_bonus
            dice_result["breakdown"]["final_target"] = adjusted_target
            # 重新判定
            roll_result = dice_result["roll_result"]
            critical_threshold = max(1, int(sides * 0.05))
            if roll_result <= critical_threshold:
                dice_result["outcome"] = "大成功"
            elif roll_result <= adjusted_target:
                dice_result["outcome"] = "成功"
            else:
                dice_result["outcome"] = "失败"

    # --- 难度系统：应用难度修正 ---
    difficulty_preset = _get_difficulty_preset(session)
    if difficulty_preset.get("auto_success"):
        # 气运之父：强制成功
        dice_result["outcome"] = "大成功"
        dice_result["breakdown"]["difficulty_bonus"] = "自动成功"
    elif difficulty_preset.get("roll_modifier", 0) != 0:
        diff_mod = difficulty_preset["roll_modifier"]
        # diff_mod is in percentage points, apply to target
        diff_target_delta = int(diff_mod / 100 * sides)
        adjusted_target = max(1, min(
            int(sides * 0.95),
            dice_result["final_target"] + diff_target_delta
        ))
        if adjusted_target != dice_result["final_target"]:
            dice_result["final_target"] = adjusted_target
            dice_result["breakdown"]["difficulty_bonus"] = diff_mod
            dice_result["breakdown"]["final_target"] = adjusted_target
            # 重新判定 outcome
            roll_result = dice_result["roll_result"]
            critical_threshold = max(1, int(sides * 0.05))
            if roll_result <= critical_threshold:
                dice_result["outcome"] = "大成功"
            elif roll_result <= adjusted_target:
                dice_result["outcome"] = "成功"
            else:
                dice_result["outcome"] = "失败"

    roll_result = dice_result["roll_result"]
    final_target = dice_result["final_target"]
    outcome = dice_result["outcome"]
    breakdown = dice_result["breakdown"]

    # 构建详细的结果文本
    bonus_parts = []
    if breakdown.get("attribute_bonus", 0) != 0:
        attr_name = breakdown.get("attribute_name", "属性")
        bonus_parts.append(f"{attr_name}{breakdown['attribute_bonus']:+d}%")
    if breakdown.get("item_bonus", 0) != 0:
        bonus_parts.append(f"道具{breakdown['item_bonus']:+d}%")
    if breakdown.get("status_bonus", 0) != 0:
        bonus_parts.append(f"状态{breakdown['status_bonus']:+d}%")
    if breakdown.get("combat_bonus", 0) != 0:
        bonus_parts.append(f"功法战力{breakdown['combat_bonus']:+d}%")
    if breakdown.get("legacy_bonus", 0) != 0:
        bonus_parts.append(f"功德{breakdown['legacy_bonus']:+d}%")
    if breakdown.get("difficulty_bonus") is not None:
        db = breakdown["difficulty_bonus"]
        if isinstance(db, str):
            bonus_parts.append(f"难度[{db}]")
        elif db != 0:
            bonus_parts.append(f"难度{db:+d}%")

    bonus_text = f"（修正: {', '.join(bonus_parts)}）" if bonus_parts else ""
    result_text = (
        f"【系统提示：针对 '{roll_type}' 的D{sides}判定已执行。"
        f"基础目标值: {base_target}，修正后目标值: {final_target}{bonus_text}，"
        f"投掷结果: {roll_result}，最终结果: {outcome}】"
    )

    roll_event = {
        "id": f"{player_id}_{int(time.time() * 1000)}",
        "type": roll_type,
        "target": final_target,
        "original_target": base_target,
        "sides": sides,
        "result": roll_result,
        "outcome": outcome,
        "result_text": result_text,
        "breakdown": breakdown,
    }

    # 把骰子事件存到 session
    session["roll_event"] = roll_event
    await state_manager.save_session(player_id, session)

    # ── Send roll event IMMEDIATELY via dedicated WS message ──
    # This bypasses the debounce in state sync, so the roll animation
    # displays instantly on the frontend without needing a page refresh.
    await websocket_manager.send_roll_event(player_id, roll_event)

    prompt_for_ai_part2 = f"{result_text}\n\n请严格基于此判定结果，继续叙事，并返回包含叙事和状态更新的最终JSON对象。这是当前的游戏状态JSON:\n{json.dumps(last_state, ensure_ascii=False)}"
    history_for_part2 = internal_history
    ai_response = await ai_service.get_ai_response(
        prompt=prompt_for_ai_part2, history=history_for_part2, user_id=player_id
    )
    return ai_response, roll_event


def end_game_and_get_code(
    user_id: int, player_id: str, spirit_stones: int
) -> tuple[dict, dict, int]:
    if spirit_stones <= 0:
        return {"error": "未获得灵石，无法生成兑换码。"}, {}, 0

    converted_value = REWARD_SCALING_FACTOR * min(
        30, max(1, 3 * (spirit_stones ** (1 / 6)))
    )
    converted_value = int(converted_value)

    # Use the new database-integrated redemption code generation
    code_name = f"天道十试-{date.today().isoformat()}-{player_id}"
    redemption_code = redemption.generate_and_insert_redemption_code(
        user_id=user_id, quota=converted_value, name=code_name
    )

    if not redemption_code:
        final_message = "\n\n【天机有变】\n\n就在功德即将圆满之际，天道因果之线竟生出一丝紊乱。\n\n冥冥中似有外力干预，令这枚本应降世的天道馈赠化为虚无。此非汝之过，乃天机运转偶有差池。\n\n请持此凭证，寻访天道之外的司掌者，必能为汝寻回应得之物。"
        return {
            "error": "数据库错误，无法生成兑换码。",
            "final_message": final_message,
        }, {}, spirit_stones

    logger.info(
        f"Generated and stored DB code {redemption_code} for {player_id} with value {converted_value:.2f}."
    )
    final_message = f"\n\n【天道回响 · 功德圆满】\n\n九天霞光倾洒，万籁俱寂。\n\n汝于浮生十梦中历经沉浮，终悟知足之道，功德圆满。天道特赐馈赠一枚，以彰汝之慧根：\n\n> {redemption_code}\n\n此乃汝应得之物，请妥善珍藏。\n\n明日此时，轮回之门将再度开启，届时可再入梦问道。今日且去，好生休憩。"
    return {"final_message": final_message, "redemption_code": redemption_code}, {
        "daily_success_achieved": True,
        "redemption_code": redemption_code,
    }, spirit_stones


def _extract_json_from_response(response_str: str) -> str | None:
    if "```json" in response_str:
        start_pos = response_str.find("```json") + 7
        end_pos = response_str.find("```", start_pos)
        if end_pos != -1:
            return response_str[start_pos:end_pos].strip()

    # Pre-fix unescaped quotes so brace counting isn't confused
    fixed = _fix_unescaped_quotes_in_json(response_str)
    start_pos = fixed.find("{")
    if start_pos != -1:
        brace_level = 0
        in_string = False
        escape_next = False
        for i in range(start_pos, len(fixed)):
            c = fixed[i]
            if escape_next:
                escape_next = False
                continue
            if c == '\\' and in_string:
                escape_next = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                brace_level += 1
            elif c == "}":
                brace_level -= 1
                if brace_level == 0:
                    return fixed[start_pos : i + 1]
    return None


import re as _re


def _fix_unescaped_quotes_in_json(raw: str) -> str:
    """
    修复 AI 返回 JSON 中字符串值内部的未转义双引号。

    问题：AI 在 narrative 等字段中使用中文引号样式如
        "天煞孤星"之命  或  "亦可登天。"
    这些裸双引号会被 json.loads 当作字符串结束符，导致解析失败。

    策略：逐字符扫描 JSON 文本，维护状态机区分"JSON 结构引号"和
    "字符串内容中的裸引号"。如果在字符串内部遇到 `"` 且后续内容
    不像 JSON 结构（即不是 : , } ] 或跟着新 key 模式），
    就转义为 `\"`。
    """
    if not raw or '"' not in raw:
        return raw

    result = []
    i = 0
    length = len(raw)

    OUTSIDE = 0
    IN_KEY = 1
    IN_VALUE = 2
    state = OUTSIDE
    expect_key = True  # After '{' we expect a key
    # Bracket stack to track object vs array context for comma handling
    bracket_stack = []  # '{' or '['

    while i < length:
        c = raw[i]

        if state == OUTSIDE:
            result.append(c)
            if c == '"':
                if expect_key:
                    state = IN_KEY
                else:
                    state = IN_VALUE
            elif c == '{':
                bracket_stack.append('{')
                expect_key = True
            elif c == '[':
                bracket_stack.append('[')
                expect_key = False  # array elements are values
            elif c == '}':
                if bracket_stack and bracket_stack[-1] == '{':
                    bracket_stack.pop()
                # After closing }, context depends on parent
                if bracket_stack:
                    expect_key = bracket_stack[-1] == '{'
                else:
                    expect_key = True
            elif c == ']':
                if bracket_stack and bracket_stack[-1] == '[':
                    bracket_stack.pop()
                if bracket_stack:
                    expect_key = bracket_stack[-1] == '{'
                else:
                    expect_key = True
            elif c == ':':
                expect_key = False
            elif c == ',':
                # After comma: in object → expect key; in array → expect value
                if bracket_stack and bracket_stack[-1] == '[':
                    expect_key = False
                else:
                    expect_key = True
            i += 1
            continue

        if state == IN_KEY:
            if c == '\\' and i + 1 < length:
                result.append(c)
                result.append(raw[i + 1])
                i += 2
                continue
            if c == '"':
                result.append(c)
                state = OUTSIDE
                expect_key = False  # After key comes :
                i += 1
                continue
            result.append(c)
            i += 1
            continue

        if state == IN_VALUE:
            if c == '\\' and i + 1 < length:
                result.append(c)
                result.append(raw[i + 1])
                i += 2
                continue

            if c == '"':
                # Is this the REAL end of the JSON string value,
                # or an unescaped literary quote inside the text?
                j = i + 1
                while j < length and raw[j] in ' \t\r\n':
                    j += 1

                if j >= length:
                    # End of text — closing quote
                    result.append(c)
                    state = OUTSIDE
                    expect_key = True
                    i += 1
                    continue

                next_meaningful = raw[j]

                if next_meaningful in (',', ':', '}', ']'):
                    # Looks like a real JSON structural boundary
                    result.append(c)
                    state = OUTSIDE
                    if next_meaningful == ':':
                        expect_key = False
                    elif next_meaningful in (',', '}'):
                        expect_key = True
                    elif next_meaningful == ']':
                        expect_key = False
                    i += 1
                    continue

                if next_meaningful == '"':
                    # Peek: does this look like "someKey": pattern?
                    k = j + 1
                    while k < length and raw[k] != '"':
                        if raw[k] == '\\':
                            k += 1
                        k += 1
                    k += 1  # skip closing quote
                    while k < length and raw[k] in ' \t\r\n':
                        k += 1
                    if k < length and raw[k] == ':':
                        # "key": pattern → real end of value
                        result.append(c)
                        state = OUTSIDE
                        expect_key = True
                        i += 1
                        continue
                    if k < length and raw[k] in (',', '}', ']'):
                        result.append(c)
                        state = OUTSIDE
                        expect_key = True
                        i += 1
                        continue

                # Internal unescaped quote → escape it
                result.append('\\"')
                i += 1
                continue

            result.append(c)
            i += 1
            continue

        i += 1

    return ''.join(result)


def _robust_json_loads(json_str: str) -> dict:
    """
    尝试多种策略解析 AI 返回的可能不合法的 JSON 字符串。
    处理常见问题：单引号、尾逗号、注释、markdown 残留等。
    """
    # 第一次尝试：直接解析（快速路径）
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        pass

    original = json_str

    # ★ 策略0：修复字符串值内部的未转义双引号（AI 常见问题）
    fixed_quotes = _fix_unescaped_quotes_in_json(json_str)
    if fixed_quotes != json_str:
        try:
            return json.loads(fixed_quotes)
        except json.JSONDecodeError:
            pass
        # 用修复后的版本继续后续策略
        json_str = fixed_quotes

    # 策略1：去除可能的 markdown 残留
    json_str = json_str.strip()
    if json_str.startswith("```"):
        json_str = _re.sub(r'^```\w*\n?', '', json_str)
        json_str = _re.sub(r'\n?```$', '', json_str).strip()
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        pass

    # 策略2：去掉行尾注释 // ...
    cleaned = _re.sub(r'//[^\n]*', '', json_str)
    # 去掉块注释 /* ... */
    cleaned = _re.sub(r'/\*.*?\*/', '', cleaned, flags=_re.DOTALL)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 策略3：修复尾逗号 (,} 或 ,])
    cleaned = _re.sub(r',\s*([}\]])', r'\1', cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 策略4：单引号替换为双引号（注意不破坏内部文本）
    # 仅在非双引号包裹区域进行替换
    def _replace_single_quotes(s: str) -> str:
        result = []
        in_double = False
        in_single = False
        i = 0
        while i < len(s):
            c = s[i]
            if c == '\\' and i + 1 < len(s):
                result.append(c + s[i + 1])
                i += 2
                continue
            if c == '"' and not in_single:
                in_double = not in_double
                result.append(c)
            elif c == "'" and not in_double:
                if in_single:
                    result.append('"')
                    in_single = False
                else:
                    result.append('"')
                    in_single = True
            else:
                result.append(c)
            i += 1
        return ''.join(result)

    cleaned2 = _replace_single_quotes(cleaned)
    try:
        return json.loads(cleaned2)
    except json.JSONDecodeError:
        pass

    # 策略5：用正则提取看起来像 JSON 对象的部分重新尝试
    match = _re.search(r'\{[\s\S]*\}', original)
    if match:
        extracted = match.group(0)
        # 再走一遍清理流程
        extracted = _re.sub(r'//[^\n]*', '', extracted)
        extracted = _re.sub(r'/\*.*?\*/', '', extracted, flags=_re.DOTALL)
        extracted = _re.sub(r',\s*([}\]])', r'\1', extracted)
        try:
            return json.loads(extracted)
        except json.JSONDecodeError:
            try:
                return json.loads(_replace_single_quotes(extracted))
            except json.JSONDecodeError:
                pass

    # 所有策略失败，抛出原始错误
    logger.error(f"_robust_json_loads: All parsing strategies failed. Raw input (first 500 chars): {original[:500]}")
    return json.loads(original)  # 让它抛出原始错误


def _effective_unchecked_rounds_for_cheat_check(raw_value: object) -> int:
    """
    `unchecked_rounds_count` 只应由后端维护；若被注入为负数，会导致抽样回溯轮数为负，
    从而使天眼检查拿不到任何输入而被绕过。

    修复策略：天眼检查时若 raw_value < 0，则强制按 10 轮回溯；检查后计数会在天眼中重置。
    """
    try:
        v = int(raw_value)
    except (TypeError, ValueError):
        return 0
    if v < 0:
        return 10
    return v


# --- 静态字段列表：这些字段在角色创建后不再变化，合并进"人物背景" ---
STATIC_FIELDS = ["姓名", "性别", "外貌", "服饰", "出身", "初始天赋", "初始事件", "同行模式"]

# --- 核心永久字段白名单（仅作为最后一道安全网，不再是主要判定依据） ---
# 主要判定逻辑：字段名以 "~" 开头 → 临时字段，否则 → 持久字段
# 此白名单作为额外保护：即使一个白名单字段被误标记为 ~，也不会被清理
PERMANENT_FIELDS = {
    # 核心角色状态
    "人物背景", "生命值", "最大生命值", "属性", "物品", "状态效果",
    "位置", "故事事件", "人物关系", "功法", "灵石",
    # 旧版兼容（合并前的静态字段）
    "姓名", "性别", "外貌", "服饰", "出身", "初始天赋", "初始事件", "同行模式",
}

# 临时事件字段的前缀标记 —— 只有以此前缀开头的字段才会被追踪和自动清理
TEMP_FIELD_PREFIX = "~"

# 临时事件字段的最大存活回合数 —— 超过此回合数无更新则自动清理
EVENT_FIELD_MAX_AGE = 8

# 故事事件摘要配置
STORY_EVENTS_MAX_DETAIL = 5   # 保留最近N条完整事件
STORY_EVENTS_MAX_TOTAL = 20   # 最大存储总数（含摘要后的旧事件）


def _consolidate_static_fields(session: dict) -> None:
    """
    将 current_life 中的静态字段（姓名、性别、出身等）合并为一段
    「人物背景」文本，然后从 current_life 中删除这些独立字段。
    
    - 此函数在试炼开始、AI返回首次state_update后调用一次。
    - 合并后，后续发给AI的状态不再包含这些重复的静态文案，节省token。
    - 「人物背景」字段同时作为前端展示的合并区域。
    """
    current_life = session.get("current_life")
    if not current_life or not isinstance(current_life, dict):
        return
    
    # 如果已经合并过（人物背景已存在），仍需清理残留的独立字段
    if "人物背景" in current_life:
        cleaned = []
        for field in STATIC_FIELDS:
            if field in current_life:
                current_life.pop(field, None)
                cleaned.append(field)
        if cleaned:
            logger.info(f"清理「人物背景」已存在时的残留字段: {cleaned}")
        return
    
    parts = []
    name = current_life.get("姓名", "无名")
    gender = current_life.get("性别", "")
    appearance = current_life.get("外貌", "")
    attire = current_life.get("服饰", "")
    origin = current_life.get("出身", "")
    talent = current_life.get("初始天赋", "")
    initial_event = current_life.get("初始事件", "")
    companion = current_life.get("同行模式", "")
    
    if name or gender:
        parts.append(f"【{name}】{'·' + gender if gender else ''}")
    if appearance:
        parts.append(f"容貌：{appearance}")
    if attire:
        parts.append(f"衣着：{attire}")
    if origin:
        parts.append(f"出身：{origin}")
    if talent:
        parts.append(f"天赋：{talent}")
    if initial_event:
        parts.append(f"初始际遇：{initial_event}")
    if companion:
        parts.append(f"行旅：{companion}")
    
    if parts:
        current_life["人物背景"] = "\n".join(parts)
    
    # 移除已合并的独立字段
    for field in STATIC_FIELDS:
        current_life.pop(field, None)
    
    logger.info(f"静态字段已合并为「人物背景」，释放 {len(STATIC_FIELDS)} 个字段")


def _prune_story_events(current_life: dict) -> None:
    """
    精简故事事件列表，防止token无限膨胀。
    
    策略：
    - 保留最近 STORY_EVENTS_MAX_DETAIL 条完整事件
    - 更早的事件压缩为一条摘要行（如 "【前事摘要】共经历了N件旧事: xxx, xxx, ..."）
    - 总数不超过 STORY_EVENTS_MAX_TOTAL
    """
    events = current_life.get("故事事件")
    if not events or not isinstance(events, list):
        return
    
    if len(events) <= STORY_EVENTS_MAX_DETAIL:
        return  # 不需要精简
    
    # 分离：可能已有旧摘要（以"【前事摘要】"开头的条目）
    old_summaries = []
    real_events = []
    for ev in events:
        if isinstance(ev, str) and ev.startswith("【前事摘要】"):
            old_summaries.append(ev)
        else:
            real_events.append(ev)
    
    if len(real_events) <= STORY_EVENTS_MAX_DETAIL:
        return  # 实际事件不多，不需精简
    
    # 需要精简的旧事件
    events_to_summarize = real_events[:-STORY_EVENTS_MAX_DETAIL]
    recent_events = real_events[-STORY_EVENTS_MAX_DETAIL:]
    
    # 生成摘要：将旧事件压缩为简短列表
    summarized_items = []
    for ev in events_to_summarize:
        if isinstance(ev, str):
            # 截取每条事件的前20字作为摘要
            summarized_items.append(ev[:20] + ("..." if len(ev) > 20 else ""))
        elif isinstance(ev, dict):
            summarized_items.append(str(ev.get("事件", str(ev)))[:20])
    
    # 合并旧摘要中的内容（如果有）
    old_summary_count = 0
    for os_text in old_summaries:
        # 从旧摘要中提取计数
        match = _re.search(r'共经历了(\d+)件旧事', os_text)
        if match:
            old_summary_count += int(match.group(1))
    
    total_old_count = old_summary_count + len(events_to_summarize)
    summary_text = f"【前事摘要】共经历了{total_old_count}件旧事：{', '.join(summarized_items[-5:])}"
    
    # 重建事件列表
    current_life["故事事件"] = [summary_text] + recent_events
    
    logger.info(
        f"故事事件精简: {len(events)} → {len(current_life['故事事件'])} "
        f"(摘要了{total_old_count}件旧事)"
    )


def _track_and_cleanup_event_fields(session: dict) -> list[str]:
    """
    追踪并清理 current_life 中的临时事件字段。
    
    【判定规则】
    只有字段名以 TEMP_FIELD_PREFIX（"~"）开头的字段才被视为临时字段，
    会被追踪老化和自动清理。所有其他字段默认为持久字段，永不自动清理。
    
    老化规则：
    - 本回合被 AI 更新（touched） -> 重置计数为 0
    - 未被更新 -> 计数 +1
    - 计数 >= EVENT_FIELD_MAX_AGE -> 自动清除
    
    Returns:
        被清理掉的字段名列表（用于日志/通知）
    """
    current_life = session.get("current_life")
    if current_life is None or not isinstance(current_life, dict):
        return []
    
    ages: dict[str, int] = session.get("_event_field_ages", {})
    touched: set = session.pop("_event_fields_touched_this_round", set())
    cleaned = []
    
    # 扫描 current_life 中以 ~ 开头的临时字段，自动注册追踪
    for key in list(current_life.keys()):
        if key.startswith(TEMP_FIELD_PREFIX) and key not in ages:
            ages[key] = 0
    
    # 更新老化计数
    for field in list(ages.keys()):
        # 安全网：永久字段白名单中的字段绝不清理
        if field in PERMANENT_FIELDS:
            del ages[field]
            continue
        # 非 ~ 前缀的字段不应在追踪中（兼容旧数据），移除
        if not field.startswith(TEMP_FIELD_PREFIX):
            del ages[field]
            continue
        # 字段已被其他逻辑删除（如AI设为null），清理追踪记录
        if field not in current_life:
            del ages[field]
            continue
        # 本回合有更新 -> 重置
        if field in touched:
            ages[field] = 0
        else:
            ages[field] = ages.get(field, 0) + 1
    
    # 清理超龄字段
    for field in list(ages.keys()):
        if ages[field] >= EVENT_FIELD_MAX_AGE:
            if field in current_life:
                del current_life[field]
                # 显示名去掉 ~ 前缀
                display_name = field.lstrip(TEMP_FIELD_PREFIX)
                cleaned.append(display_name)
                logger.info(f"临时事件字段自动清理: '{field}' (超过{EVENT_FIELD_MAX_AGE}回合未更新)")
            del ages[field]
    
    session["_event_field_ages"] = ages
    return cleaned


def _mark_event_fields_updated(session: dict, updated_keys: list[str]) -> None:
    """
    标记本回合被AI更新的临时事件字段（以 ~ 开头的字段）。
    在 _apply_state_update 后、_track_and_cleanup_event_fields 前调用。
    
    只有以 TEMP_FIELD_PREFIX（"~"）开头的字段才会被标记。
    
    Args:
        updated_keys: 本回合 state_update 中涉及的所有 key
                      (如 "current_life.~外门大比进程" → "~外门大比进程")
    """
    touched: set = session.get("_event_fields_touched_this_round", set())
    for key in updated_keys:
        # 提取 current_life 下的直接字段名
        parts = key.split(".")
        if len(parts) >= 2 and parts[0] == "current_life":
            field_name = parts[1].rstrip("+-")  # 去掉 +/- 后缀
            if field_name.startswith(TEMP_FIELD_PREFIX):
                touched.add(field_name)
    session["_event_fields_touched_this_round"] = touched


def _build_compact_state_for_ai(session: dict) -> dict:
    """
    构建发送给AI的精简状态副本，减少token开销：
    1. 排除「人物背景」(静态文案，AI已在内部历史中见过)
    2. 只保留最近的故事事件
    3. 排除 internal_history, display_history 等非游戏状态
    """
    session_copy = deepcopy(session)
    session_copy.pop("internal_history", None)
    session_copy.pop("difficulty", None)
    session_copy.pop("_event_field_ages", None)
    session_copy.pop("_event_fields_touched_this_round", None)
    
    # 精简 display_history
    session_copy["display_history"] = (
        "\n".join(session_copy.get("display_history", []))
    )[-300:]
    
    current_life = session_copy.get("current_life")
    if current_life and isinstance(current_life, dict):
        # 不发送人物背景给AI（静态文案，开局已见过）
        current_life.pop("人物背景", None)
    
    return session_copy


def _remove_item_from_list(current_list: list, removal) -> None:
    """
    从列表中移除一个元素。
    支持多种格式：
    - 字符串: 按名称匹配 (移除物品名称相同的第一个)
    - 字典 {"名称": "xxx"}: 按名称匹配物品字典
    - 字典 {"名称": "xxx", "数量": N}: 只扣减数量，数量归零才移除
    """
    if isinstance(removal, str):
        # 按名称匹配
        for i, item in enumerate(current_list):
            if isinstance(item, dict) and item.get("名称") == removal:
                qty = item.get("数量", 1)
                if qty > 1:
                    item["数量"] = qty - 1
                else:
                    current_list.pop(i)
                return
            elif isinstance(item, str) and item == removal:
                current_list.pop(i)
                return
    elif isinstance(removal, dict):
        target_name = removal.get("名称", "")
        remove_qty = removal.get("数量", 1)
        for i, item in enumerate(current_list):
            if isinstance(item, dict) and item.get("名称") == target_name:
                current_qty = item.get("数量", 1)
                remaining = current_qty - remove_qty
                if remaining <= 0:
                    current_list.pop(i)
                else:
                    item["数量"] = remaining
                return


# --- 英文key→中文key映射（AI偶尔返回英文key时自动修正） ---
_EN_TO_CN_KEY_MAP = {
    "story_events": "故事事件", "current_cultivation": "当前修炼",
    "cultivation": "功法", "hp": "生命值", "max_hp": "最大生命值",
    "items": "物品", "inventory": "物品", "location": "位置",
    "position": "位置", "status": "状态效果", "status_effects": "状态效果",
    "attributes": "属性", "stats": "属性", "spirit_stones": "灵石",
    "background": "人物背景", "relationships": "人物关系",
    "combat_power": "战斗力", "realm": "境界",
    "cultivation_progress": "修炼进度", "sect": "门派",
    "reputation": "声望", "faction": "势力",
    "name": "姓名", "gender": "性别", "appearance": "外貌",
}


def _normalize_english_keys(update: dict) -> dict:
    """
    将 state_update 中的英文 key 自动替换为中文 key。
    处理形如 "current_life.story_events+" → "current_life.故事事件+" 的映射。
    """
    normalized = {}
    for key, value in update.items():
        parts = key.split(".")
        changed = False
        for i, part in enumerate(parts):
            # 去掉 +/- 后缀再查映射
            suffix = ""
            clean = part
            if clean.endswith("+") or clean.endswith("-"):
                suffix = clean[-1]
                clean = clean[:-1]
            cn = _EN_TO_CN_KEY_MAP.get(clean.lower())
            if cn:
                parts[i] = cn + suffix
                changed = True
        new_key = ".".join(parts)
        if changed:
            logger.debug(f"英文key自动修正: '{key}' → '{new_key}'")
        normalized[new_key] = value
    return normalized


def _apply_state_update(state: dict, update: dict) -> dict:
    # --- 英文key自动修正：AI偶尔返回英文key，统一替换为中文 ---
    update = _normalize_english_keys(update)
    
    # --- 社交系统：拦截并处理人物关系更新 ---
    social_updates = {}
    regular_updates = {}
    for key, value in update.items():
        if key.startswith("current_life.人物关系.") and isinstance(value, dict):
            # 格式: "current_life.人物关系.云霄真人": {...}
            npc_name = key.split(".", 2)[2] if key.count(".") >= 2 else key
            social_updates[npc_name] = value
        elif key == "current_life.人物关系" and isinstance(value, dict):
            # 格式: "current_life.人物关系": {"柳如烟": {...}, "新NPC": {...}}
            # AI 直接发送了整个人物关系字典 → 需要合并而非覆盖旧数据
            for npc_name, npc_data in value.items():
                if isinstance(npc_data, dict):
                    # 检查是完整NPC对象还是更新指令
                    is_full_npc = "好感度" in npc_data and "关系阶段" in npc_data
                    if is_full_npc:
                        # 完整NPC对象：直接merge到现有关系中（保留旧数据）
                        cl = state.setdefault("current_life", {})
                        if "人物关系" not in cl or not isinstance(cl.get("人物关系"), dict):
                            cl["人物关系"] = {}
                        existing_npcs = cl["人物关系"]
                        if npc_name in existing_npcs and isinstance(existing_npcs[npc_name], dict):
                            # 合并：保留旧NPC的已突破阈值、好感度变动记录等累计数据
                            old_npc = existing_npcs[npc_name]
                            for k, v in npc_data.items():
                                old_npc[k] = v
                            logger.debug(f"合并NPC '{npc_name}' 的更新数据（保留累计数据）")
                        else:
                            existing_npcs[npc_name] = npc_data
                            logger.debug(f"新增NPC '{npc_name}' 完整对象")
                    else:
                        # 更新指令（含好感度变化、原因等）→ 走社交系统处理
                        social_updates[npc_name] = npc_data
                else:
                    logger.warning(f"人物关系中 '{npc_name}' 的值不是dict: {type(npc_data)}")
            if social_updates:
                logger.info(
                    f"拦截整体人物关系: {len(social_updates)} 个NPC走社交系统更新"
                )
        else:
            regular_updates[key] = value

    # 先应用常规更新
    for key, value in regular_updates.items():
        if key == "unchecked_rounds_count":
            continue
        if key == "internal_history" or key.startswith("internal_history."):
            continue

        keys = key.split(".")
        temp_state = state
        for part in keys[:-1]:
            if part not in temp_state or temp_state[part] is None:
                temp_state[part] = {}
            temp_state = temp_state[part]

        # Handle null → delete field (AI sets value to null to clean up)
        if value is None:
            final_key = keys[-1]
            if final_key in temp_state:
                del temp_state[final_key]
                logger.debug(f"字段已清除: {key}")
            continue

        # Handle list append/extend operations (key+)
        if keys[-1].endswith("+") and isinstance(temp_state.get(keys[-1][:-1]), list):
            list_key = keys[-1][:-1]
            if isinstance(value, list):
                temp_state[list_key].extend(value)
            else:
                temp_state[list_key].append(value)

        # Handle list removal operations (key-)
        # Supports: "current_life.物品-": ["疗伤草"] or "current_life.物品-": [{"名称":"疗伤草"}]
        elif keys[-1].endswith("-") and isinstance(temp_state.get(keys[-1][:-1]), list):
            list_key = keys[-1][:-1]
            items_to_remove = value if isinstance(value, list) else [value]
            current_list = temp_state[list_key]
            for removal in items_to_remove:
                _remove_item_from_list(current_list, removal)
        else:
            temp_state[keys[-1]] = value

    # 再通过 social_system 处理人物关系更新
    if social_updates and state.get("current_life"):
        try:
            social_messages = social_system.process_social_state_update(
                state["current_life"], social_updates
            )
            # 把社交系统产生的消息存到 state 中供后续展示
            if social_messages:
                if "_social_messages" not in state:
                    state["_social_messages"] = []
                state["_social_messages"].extend(social_messages)
        except Exception as e:
            logger.error(f"社交系统处理失败: {e}", exc_info=True)

    # --- 功法系统：自动计算战力 ---
    _recalculate_combat_power(state)

    # --- 数据层英文key残留清理 ---
    # AI偶尔在 current_life 里直接写入英文key，需要合并到对应的中文key
    _cleanup_english_keys_in_current_life(state)

    return state


def _cleanup_english_keys_in_current_life(state: dict):
    """
    清理 current_life 中残留的英文key。
    如果同一字段的中英文版本同时存在，合并后移除英文版本。
    如果只有英文版本，重命名为中文。
    """
    current_life = state.get("current_life")
    if not current_life or not isinstance(current_life, dict):
        return

    keys_to_remove = []
    keys_to_add = {}

    for key in list(current_life.keys()):
        cn_key = _EN_TO_CN_KEY_MAP.get(key.lower())
        if not cn_key or cn_key == key:
            continue  # 已经是中文key或不在映射中

        en_value = current_life[key]
        cn_value = current_life.get(cn_key)

        if cn_value is not None:
            # 两者都存在 → 合并列表，或保留中文版本
            if isinstance(cn_value, list) and isinstance(en_value, list):
                # 合并列表并去重
                for item in en_value:
                    if item not in cn_value:
                        cn_value.append(item)
            # 非列表情况：保留中文版本（更可能是最新的）
        else:
            # 只有英文版本 → 重命名为中文
            keys_to_add[cn_key] = en_value

        keys_to_remove.append(key)

    for key in keys_to_remove:
        del current_life[key]
        logger.debug(f"清理current_life英文key残留: '{key}'")
    for key, value in keys_to_add.items():
        current_life[key] = value


def _recalculate_combat_power(state: dict):
    """
    当 current_life 中的功法列表变动时，自动重算战力并写入属性。
    战力 = 主功法战力(100%) + 辅助功法(各20%)
    """
    current_life = state.get("current_life")
    if not current_life or not isinstance(current_life, dict):
        return

    techniques = current_life.get("功法")
    if techniques is None:
        # AI 可能尚未生成功法字段，不做处理
        return

    if not isinstance(techniques, list):
        techniques = [techniques] if techniques else []
        current_life["功法"] = techniques

    total_power = cultivation_system.calculate_total_combat_power(techniques)

    # 写入属性.战力
    attributes = current_life.get("属性")
    if attributes is None:
        attributes = {}
        current_life["属性"] = attributes

    old_power = 0
    try:
        old_power = int(attributes.get("战力", 0))
    except (ValueError, TypeError):
        pass

    if total_power != old_power:
        attributes["战力"] = total_power
        if total_power > 0:
            logger.info(
                f"功法战力更新: {old_power} → {total_power} "
                f"(功法数: {len(techniques)})"
            )


async def _stream_narrative_to_player(player_id: str, narrative: str, stream_id: str):
    """
    将 narrative 文本以流式方式发送给前端，模拟逐字输出效果。
    每次发送一小段文本，通过短暂延迟制造打字机效果。
    """
    if not narrative:
        return
    
    # 按字符分组发送，每组2-4个字符
    chunk_size = 3
    for i in range(0, len(narrative), chunk_size):
        chunk = narrative[i:i + chunk_size]
        await websocket_manager.send_stream_chunk(player_id, chunk, stream_id)
        await asyncio.sleep(0.03)  # 30ms 间隔，约每秒100字
    
    await websocket_manager.send_stream_end(player_id, stream_id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Truncation detection, continuation, and JSON repair
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _is_json_truncated(text: str) -> bool:
    """
    Detect if a response looks like truncated JSON.
    Heuristics:
    - Contains '{' but braces don't balance
    - Ends mid-string (no closing quote) or mid-value
    """
    stripped = text.strip()
    if not stripped:
        return False
    # Remove markdown fencing if present
    if stripped.startswith("```"):
        stripped = _re.sub(r'^```\w*\n?', '', stripped)
        stripped = _re.sub(r'\n?```$', '', stripped).strip()

    # Pre-fix unescaped quotes so they don't confuse brace/string tracking
    stripped = _fix_unescaped_quotes_in_json(stripped)

    brace_depth = 0
    in_string = False
    escape_next = False
    for c in stripped:
        if escape_next:
            escape_next = False
            continue
        if c == '\\' and in_string:
            escape_next = True
            continue
        if c == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '{':
            brace_depth += 1
        elif c == '}':
            brace_depth -= 1

    # If we're still inside a string or braces aren't balanced => truncated
    return in_string or brace_depth > 0


def _repair_truncated_json(text: str) -> str | None:
    """
    Attempt to repair a truncated JSON by closing open strings, arrays, and objects.
    Returns the repaired JSON string, or None if repair isn't feasible.
    """
    stripped = text.strip()
    if not stripped:
        return None

    # Remove markdown fencing
    if stripped.startswith("```"):
        stripped = _re.sub(r'^```\w*\n?', '', stripped)
        stripped = _re.sub(r'\n?```$', '', stripped).strip()

    # Pre-fix unescaped quotes before structural analysis
    stripped = _fix_unescaped_quotes_in_json(stripped)

    # Find the first '{'
    first_brace = stripped.find('{')
    if first_brace == -1:
        return None
    json_part = stripped[first_brace:]

    # Walk through and track state
    stack = []  # stack of '{' or '['
    in_string = False
    escape_next = False
    last_key = False  # True if we just saw a key and colon
    i = 0
    while i < len(json_part):
        c = json_part[i]
        if escape_next:
            escape_next = False
            i += 1
            continue
        if c == '\\' and in_string:
            escape_next = True
            i += 1
            continue
        if c == '"':
            in_string = not in_string
            i += 1
            continue
        if in_string:
            i += 1
            continue
        # Outside string
        if c == '{':
            stack.append('}')
        elif c == '[':
            stack.append(']')
        elif c == '}' or c == ']':
            if stack:
                stack.pop()
        i += 1

    if not stack and not in_string:
        # Already balanced (maybe just had trailing garbage)
        return json_part

    # Build the repair suffix
    repair = ""
    if in_string:
        # Close the open string — truncate cleanly
        # Remove any trailing incomplete escape sequence
        if json_part.endswith('\\'):
            json_part = json_part[:-1]
        repair += '...(内容过长已截断)"'

    # Check if the JSON ends after a colon (key with no value), a comma,
    # or other incomplete construct — we need a placeholder value
    tail = (json_part + repair).rstrip()
    if tail.endswith(':'):
        repair += ' null'
    elif tail.endswith(','):
        # Remove trailing comma before closing
        json_part = json_part.rstrip()
        if json_part.endswith(','):
            json_part = json_part[:-1]
        elif repair.rstrip().endswith(','):
            repair = repair.rstrip()[:-1]

    # Close any open brackets/braces in reverse order
    for closer in reversed(stack):
        repair += closer

    repaired = json_part + repair
    logger.info(f"JSON repair: added {len(repair)} chars suffix to close {len(stack)} brackets, in_string={in_string}")
    return repaired


_CONTINUATION_PROMPT = (
    "你的上一次回答被截断了，输出不完整。请从截断处继续输出，"
    "直接接续上文（不要重复已有内容），确保JSON格式完整闭合。"
    "只输出剩余部分，不要任何解释。"
)


def _ensure_narrative_not_empty(parsed: dict, raw_text: str) -> dict:
    """
    校验解析结果中的 narrative 不为空。
    如果为空，尝试从原始文本中提取中文叙事内容。
    """
    if not isinstance(parsed, dict):
        return parsed
    
    narrative = parsed.get("narrative", "")
    if narrative and len(str(narrative).strip()) > 5:
        return parsed  # narrative 正常，直接返回
    
    # narrative 为空——尝试从原始文本抢救
    salvaged = _extract_narrative_from_broken_json(raw_text)
    if salvaged and len(salvaged) > 20:
        parsed["narrative"] = salvaged
        logger.info(f"空narrative修复: 从原始文本抢救 {len(salvaged)} 字符")
        return parsed
    
    # 尝试提取原文中的中文内容作为 narrative
    cn_lines = []
    for line in raw_text.split('\n'):
        line = line.strip()
        if line and any('\u4e00' <= c <= '\u9fff' for c in line):
            # 跳过明显的元数据行
            if not any(skip in line for skip in ['struct', 'TOOL_CALL', 'tool:', 'args:']):
                cn_lines.append(line)
    
    if cn_lines:
        extracted = '\n'.join(cn_lines)
        if len(extracted) > 20:
            parsed["narrative"] = extracted
            logger.info(f"空narrative修复: 从原文提取中文内容 {len(extracted)} 字符")
    
    return parsed


def _handle_tool_call_format(text: str) -> str:
    """
    处理某些模型（如MiniMax）返回的 [TOOL_CALL] 格式。
    
    这些模型不直接输出 JSON，而是用如下格式：
    [TOOL_CALL]
    struct Tool {
        tool: "gen_response_json",
        args: {
            narrative="叙事内容...",
            state_update={...}
        }
    }
    
    本函数检测此格式并尝试提取 narrative 内容，构造标准 JSON 返回。
    如果不是此格式，原样返回。
    """
    if "[TOOL_CALL]" not in text and "struct Tool" not in text:
        return text
    
    logger.info(f"检测到 [TOOL_CALL] 格式，尝试提取 narrative...")
    
    # 策略1: 尝试找到 narrative= 后面的字符串值
    narrative = ""
    # 匹配 narrative="..." 或 narrative: "..."
    m = _re.search(r'narrative\s*[=:]\s*"((?:[^"\\]|\\.)*)(?:"|\Z)', text, _re.DOTALL)
    if m:
        narrative = m.group(1)
        # 处理转义字符
        narrative = narrative.replace('\\"', '"').replace('\\n', '\n')
    
    if not narrative:
        # 策略2: 在 [TOOL_CALL] 之前可能有中文叙事文本
        tool_idx = text.find("[TOOL_CALL]")
        if tool_idx > 0:
            prefix = text[:tool_idx].strip()
            # 去掉英文思考过程，保留中文内容
            cn_lines = [
                line for line in prefix.split('\n')
                if line.strip() and any('\u4e00' <= c <= '\u9fff' for c in line)
            ]
            if cn_lines:
                narrative = '\n'.join(cn_lines)
    
    if narrative and len(narrative) > 20:
        logger.info(f"从 [TOOL_CALL] 格式成功提取 {len(narrative)} 字符 narrative")
        # 构造标准 JSON（只保留 narrative，state_update 太复杂不尝试提取）
        safe_narrative = json.dumps(narrative, ensure_ascii=False)
        return f'{{"narrative": {safe_narrative}, "state_update": {{}}}}'
    
    # 无法提取 narrative，返回原文让后续流程处理
    logger.warning(f"[TOOL_CALL] 格式中未找到有效 narrative，保留原文")
    return text


async def _parse_with_continuation(
    raw_response: str,
    player_id: str,
    history: list[dict],
    max_continuations: int = 2,
) -> dict:
    """
    Parse AI response JSON, with automatic truncation detection,
    continuation requests, and repair as fallback.

    Flow:
    1. Try parse directly
    2. If truncated -> ask AI to continue (up to max_continuations times)
    3. If still truncated -> attempt structural JSON repair
    4. If all fails -> 将 AI 原始文本作为明文 narrative 返回（永不抛出异常）
    """
    combined = raw_response

    # ★ Pre-fix 0: 处理 [TOOL_CALL] 格式（某些模型如MiniMax使用此格式代替纯JSON）
    combined = _handle_tool_call_format(combined)

    # ★ Pre-fix 1: escape unescaped literary quotes in the raw response
    combined = _fix_unescaped_quotes_in_json(combined)

    # ── Step 0: detect completely non-JSON response (model ignored system prompt) ──
    if '{' not in combined:
        logger.warning(
            f"Response contains no JSON at all ({len(combined)} chars). "
            f"Model likely ignored system prompt. First 200: {combined[:200]}"
        )
        # 最终手段：将 AI 的原始文本作为明文 narrative 输出
        return _build_plaintext_fallback(
            combined,
            context="非JSON响应",
            error_reason="AI返回内容中不包含任何JSON结构",
        )

    # ── Step 1: direct parse ──
    json_str = _extract_json_from_response(combined)
    if json_str:
        try:
            result = _robust_json_loads(json_str)
            return _ensure_narrative_not_empty(result, combined)
        except (json.JSONDecodeError, Exception):
            pass  # Fall through to truncation check

    # ── Step 2: truncation detection + continuation ──
    if _is_json_truncated(combined):
        logger.warning(
            f"Truncated JSON detected ({len(combined)} chars). "
            f"Attempting continuation (max {max_continuations} attempts)..."
        )

        for attempt in range(max_continuations):
            logger.info(f"Continuation attempt {attempt + 1}/{max_continuations}")

            # Build a temporary history with the partial response as assistant
            # and a continuation request as user
            cont_history = history.copy()
            cont_history.append({"role": "assistant", "content": combined})
            cont_history.append({"role": "user", "content": _CONTINUATION_PROMPT})

            # Get continuation (non-streaming for reliability)
            continuation = await ai_service.get_ai_response(
                prompt=_CONTINUATION_PROMPT,
                history=cont_history,
                force_json=False,
                user_id=player_id,
            )

            if continuation and not continuation.startswith("错误："):
                logger.info(
                    f"Continuation received: {len(continuation)} chars, "
                    f"first 200: {continuation[:200]}"
                )
                # Stitch: the continuation should directly follow the truncated text
                # Remove any repeated prefix (AI sometimes echoes a bit)
                continuation_clean = continuation.strip()
                # Remove markdown fencing from continuation
                if continuation_clean.startswith("```"):
                    continuation_clean = _re.sub(r'^```\w*\n?', '', continuation_clean)
                    continuation_clean = _re.sub(r'\n?```$', '', continuation_clean).strip()

                combined = combined.rstrip() + continuation_clean

                # Try parse again
                json_str = _extract_json_from_response(combined)
                if json_str:
                    try:
                        result = _robust_json_loads(json_str)
                        # 校验 narrative 非空
                        result = _ensure_narrative_not_empty(result, combined)
                        logger.info(
                            f"Continuation success on attempt {attempt + 1}! "
                            f"Total length: {len(combined)} chars"
                        )
                        return result
                    except (json.JSONDecodeError, Exception) as e:
                        logger.warning(f"Parse still failed after continuation {attempt + 1}: {e}")

                # Check if still truncated
                if not _is_json_truncated(combined):
                    # Not truncated anymore but still can't parse — try repair
                    break
            else:
                logger.warning(f"Continuation attempt {attempt + 1} returned error: {continuation[:200] if continuation else 'empty'}")

    # ── Step 3: structural repair ──
    logger.info(f"Attempting JSON structural repair on {len(combined)} chars...")
    repaired = _repair_truncated_json(combined)
    if repaired:
        try:
            result = _robust_json_loads(repaired)
            result = _ensure_narrative_not_empty(result, combined)
            logger.info(f"JSON repair successful! Parsed repaired response.")
            return result
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"JSON repair failed to parse: {e}")

    # ── Step 4: all strategies exhausted — 最终手段：明文输出 ──
    logger.error(
        f"All parse strategies failed ({len(combined)} chars). "
        f"First 500: {combined[:500]}"
    )
    # 不再抛出异常！将 AI 的原始响应作为明文 narrative 返回给玩家
    return _build_plaintext_fallback(
        combined,
        context="JSON解析失败",
        error_reason="所有JSON解析策略均失败（直接解析、续写、结构修复、narrative抢救）",
    )


def _extract_narrative_from_broken_json(text: str) -> str | None:
    """
    Last-resort extraction: pull the narrative value from broken JSON
    by finding the "narrative" key and reading the string value.
    """
    for marker in ['"narrative":"', '"narrative": "', '"narrative" : "']:
        idx = text.find(marker)
        if idx != -1:
            start = idx + len(marker)
            decoded = _decode_json_string_value(text[start:])
            if decoded and len(decoded) > 10:
                return decoded
    return None


def _build_error_detail_tag(raw_ai_text: str, error_reason: str) -> str:
    """
    构造一个 HTML 注释标记，嵌入到 display_history 条目末尾。
    前端检测到此标记后，可渲染为可展开的详情面板。

    格式: <!--error-details:BASE64-->
    BASE64 解码后是 JSON: {"raw": "...", "error": "..."}
    """
    detail = json.dumps({
        "raw": (raw_ai_text or "")[:5000],  # 限制长度，避免过大
        "error": (error_reason or "未知错误")[:500],
    }, ensure_ascii=False)
    encoded = base64.b64encode(detail.encode("utf-8")).decode("ascii")
    return f"<!--error-details:{encoded}-->"


def _build_plaintext_fallback(
    raw_text: str,
    context: str = "",
    error_reason: str = "",
) -> dict:
    """
    最终兜底：当所有 JSON 解析策略都失败时——

    1. 主显文字保持友好的「天机紊乱」提示
    2. 尝试从残破 JSON 中抢救 narrative；若抢救成功则直接展示（无需错误提示）
    3. 若无法抢救，则将 AI 原始返回 + 报错原因嵌入为可展开详情标记，
       前端允许玩家点击查看完整内容
    """
    # ── 空内容兜底 ──
    if not raw_text or not raw_text.strip():
        return {
            "narrative": (
                "【天机紊乱】\n\n"
                "天道回应化为虚无，未能传达任何信息。请稍候再试。"
            ),
            "state_update": {}
        }

    # ── 抢救：尝试从残破 JSON 中提取 narrative ──
    salvaged = _extract_narrative_from_broken_json(raw_text)
    if salvaged and len(salvaged) > 20:
        logger.info(
            f"Plaintext fallback: salvaged {len(salvaged)} chars of narrative from broken JSON"
        )
        return {
            "narrative": salvaged,
            "state_update": {}
        }

    # ── 无法抢救：显示友好提示 + 附带可展开详情 ──
    error_tag = _build_error_detail_tag(raw_text, error_reason or context)
    context_hint = f"（{context}）" if context else ""
    logger.info(
        f"Plaintext fallback{context_hint}: attaching {len(raw_text)} chars "
        f"as expandable error detail"
    )
    return {
        "narrative": (
            f"【天机紊乱】{context_hint}\n\n"
            "虚空微微震颤，天道之语未能正确传达。\n\n"
            "请再次尝试你的行动。\n\n"
            f"{error_tag}"
        ),
        "state_update": {}
    }


async def _get_ai_response_streaming(
    player_id: str,
    prompt: str,
    history: list[dict],
) -> str:
    """
    使用流式 API 获取 AI 响应，同时将 narrative 部分实时推送给前端。

    策略:
    1. 流式收集完整 JSON 响应
    2. 在收集过程中，尝试提取并实时推送 narrative 字段的内容
    3. 如果 Echo 返回 __ECHO_FULL_REPLACE__ 哨兵，用完整内容替换部分收集
    4. 返回完整的 JSON 字符串供后续处理
    """
    REPLACE_SENTINEL = ai_service.ECHO_FULL_REPLACE_SENTINEL

    stream_id = f"{player_id}_{int(time.time() * 1000)}"
    full_response = ""
    narrative_started = False
    narrative_buffer = ""
    sent_length = 0
    in_narrative_value = False

    async for chunk in ai_service.get_ai_response_stream(
        prompt=prompt, history=history, user_id=player_id
    ):
        if chunk is None:
            break

        # --- Echo full-replace sentinel ---
        # When the streaming chunks were incomplete, the Echo backend sends
        # __ECHO_FULL_REPLACE__<complete JSON> so we can discard partial data.
        if isinstance(chunk, str) and chunk.startswith(REPLACE_SENTINEL):
            replacement = chunk[len(REPLACE_SENTINEL):]
            logger.info(
                f"Stream replace: discarding {len(full_response)} partial chars, "
                f"using {len(replacement)} char complete response"
            )
            full_response = replacement
            # Push the full narrative from the replacement to the frontend
            narrative_started = False
            in_narrative_value = False
            sent_length = 0
            # Re-extract narrative from the complete response for streaming to frontend
            for marker in ['"narrative":"', '"narrative": "', '"narrative" : "']:
                idx = replacement.find(marker)
                if idx != -1:
                    start = idx + len(marker)
                    decoded = _decode_json_string_value(replacement[start:])
                    if decoded and len(decoded) > 0:
                        narrative_started = True
                        # Send the whole narrative at once (with small delays for effect)
                        chunk_size = 8
                        for ci in range(0, len(decoded), chunk_size):
                            text_chunk = decoded[ci:ci + chunk_size]
                            await websocket_manager.send_stream_chunk(player_id, text_chunk, stream_id)
                            await asyncio.sleep(0.01)
                        sent_length = len(decoded)
                    break
            continue

        full_response += chunk

        # --- Real-time narrative extraction and push ---
        if not narrative_started:
            for marker in ['"narrative":"', '"narrative": "', '"narrative" : "']:
                idx = full_response.find(marker)
                if idx != -1:
                    narrative_started = True
                    in_narrative_value = True
                    break

        if in_narrative_value:
            # Re-extract decoded narrative from the full response so far
            narrative_buffer = ""
            for marker in ['"narrative":"', '"narrative": "', '"narrative" : "']:
                idx = full_response.find(marker)
                if idx != -1:
                    start = idx + len(marker)
                    narrative_buffer = _decode_json_string_value(full_response[start:])
                    break

            # Send unsent portion
            if len(narrative_buffer) > sent_length:
                new_content = narrative_buffer[sent_length:]
                chunk_size = 3
                for ci in range(0, len(new_content), chunk_size):
                    text_chunk = new_content[ci:ci + chunk_size]
                    await websocket_manager.send_stream_chunk(player_id, text_chunk, stream_id)
                    await asyncio.sleep(0.02)
                sent_length = len(narrative_buffer)

    # Stream finished — send end signal
    # 无论是否检测到 narrative 都发送结束信号，防止前端永远等待
    await websocket_manager.send_stream_end(player_id, stream_id)

    # Log collected response length for debugging
    logger.info(f"Stream collection done for {player_id}: {len(full_response)} chars")
    if len(full_response) < 50:
        logger.warning(f"Stream response suspiciously short: {full_response!r}")

    # Fallback to non-streaming if stream produced nothing useful
    if not full_response or full_response.startswith("错误："):
        logger.warning(f"Stream failed or empty, falling back to non-stream API")
        full_response = await ai_service.get_ai_response(
            prompt=prompt, history=history, user_id=player_id
        )

    # If response contains no JSON at all (model ignored system prompt),
    # retry once with a reinforced prompt
    if full_response and '{' not in full_response:
        logger.warning(
            f"Stream response has no JSON for {player_id} ({len(full_response)} chars). "
            f"Retrying with reinforced prompt..."
        )
        reinforced_prompt = (
            prompt + "\n\n"
            "【系统提醒】你必须严格以JSON格式回复，格式为：\n"
            '{"narrative": "叙事文本", "state_update": {...}} 或 {"narrative": "叙事文本", "roll_request": {...}}\n'
            "不要输出任何JSON之外的内容。不要自我介绍。你是游戏司命星君。"
        )
        full_response = await ai_service.get_ai_response(
            prompt=reinforced_prompt, history=history,
            force_json=False, user_id=player_id
        )

    return full_response


def _decode_json_string_value(buf: str) -> str:
    """
    Decode a JSON string value starting right after the opening quote.
    Handles \\", \\n, \\\\, \\t, \\/ escapes.
    Uses heuristics to skip unescaped literary quotes like "天煞孤星"
    that appear inside Chinese narrative text.
    Stops at the REAL closing quote (or end of buffer for partial streams).
    """
    decoded = ""
    i = 0
    buf_len = len(buf)
    while i < buf_len:
        c = buf[i]
        if c == '\\' and i + 1 < buf_len:
            next_c = buf[i + 1]
            if next_c == '"':
                decoded += '"'
            elif next_c == 'n':
                decoded += '\n'
            elif next_c == '\\':
                decoded += '\\'
            elif next_c == 't':
                decoded += '\t'
            elif next_c == '/':
                decoded += '/'
            elif next_c == 'r':
                decoded += '\r'
            elif next_c == 'u':
                # Unicode escape \uXXXX
                if i + 5 < buf_len:
                    hex_str = buf[i + 2:i + 6]
                    try:
                        decoded += chr(int(hex_str, 16))
                        i += 6
                        continue
                    except ValueError:
                        decoded += '\\u'
                        i += 2
                        continue
                else:
                    break  # Incomplete unicode escape, wait for more data
            else:
                decoded += '\\' + next_c
            i += 2
        elif c == '"':
            # Is this the REAL end of string, or an unescaped literary quote?
            # Look ahead past whitespace for JSON structural chars
            j = i + 1
            while j < buf_len and buf[j] in ' \t\r\n':
                j += 1
            if j >= buf_len:
                # End of buffer — treat as real end (or partial stream)
                break
            next_meaningful = buf[j]
            if next_meaningful in (',', ':', '}', ']'):
                # Structural char → real end of string
                break
            if next_meaningful == '"':
                # Could be the start of the next key.
                # Peek: "someKey" : → real boundary
                k = j + 1
                while k < buf_len and buf[k] != '"':
                    if buf[k] == '\\':
                        k += 1
                    k += 1
                k += 1  # skip closing quote
                while k < buf_len and buf[k] in ' \t\r\n':
                    k += 1
                if k < buf_len and buf[k] in (':', ',', '}', ']'):
                    break  # Real boundary
            # Otherwise, it's a literary quote inside the text — include it
            decoded += '"'
            i += 1
        else:
            decoded += c
            i += 1
    return decoded


def _build_action_prompt(session_copy: dict, action: str) -> str:
    """
    构建包含社交上下文的 AI prompt。
    使用精简后的状态副本，减少 token 开销。
    """
    base = f'这是当前的游戏状态JSON:\n{json.dumps(session_copy, ensure_ascii=False)}'

    # 注入社交关系摘要 (让 AI 知道当前 NPC 好感度状态)
    current_life = session_copy.get("current_life")
    social_ctx = ""
    if current_life:
        social_ctx = social_system.inject_social_context_for_ai(current_life)
        if social_ctx:
            social_ctx = f"\n\n{social_ctx}\n（请根据NPC好感度和性格来决定NPC的态度和行为。好感度到达瓶颈时应创造突破机会。负面好感度的NPC可能主动发起挑衅/陷害事件。）"

    prompt = (
        f'{base}{social_ctx}\n\n'
        f'玩家的行动是: "{action}"\n\n'
        f'请根据状态和行动，生成包含`narrative`和(`state_update`或`roll_request`)的JSON作为回应。'
        f'如果角色死亡，请在叙述中说明，并在`state_update`中同时将`is_in_trial`设为`false`，`current_life`设为`null`。'
        f'\n【人物关系·必须更新】如果本回合有任何NPC互动（包括首次结识的新NPC、好感度变化、关系推进），'
        f'**必须**在state_update中更新人物关系。格式: "current_life.人物关系.NPC名": {{"好感度变化": N, "原因": "..."}}。'
        f'首次出现的新NPC还需加"新NPC":{{"性格":"...","身份":"...","初始好感度":N}}字段。'
        f'【切记】只要叙事中出现了有意义的NPC互动，就必须在state_update中体现，不可遗漏！'
        f'\n如果角色领悟/获得新功法，请在state_update中更新功法列表（追加用"current_life.功法+":[ ]，替换用"current_life.功法":[ ]）。'
        f'功法格式:{{"名称":"...","品阶":"黄/玄/地/天","等阶":"下品/中品/上品/极品","类型":"...","描述":"..."}}'
        f'\n【物品消耗】若玩家使用了消耗性物品，必须在state_update中移除该物品：'
        f'"current_life.物品-": ["物品名称"] 或 "current_life.物品-": [{{"名称":"物品名","数量":1}}]。'
        f'数量>1的物品只扣减数量，数量归零才移除。'
        f'\n【事件字段清理】若某个多回合事件已经结束（如大比、任务、探索），'
        f'请将相关的临时字段设为null清除，如: "current_life.外门大比进程": null。'
    )
    return prompt


async def _process_player_action_async(user_info: dict, action: str):
    player_id = user_info["username"]
    user_id = user_info["id"]
    session = await state_manager.get_session(player_id)
    if not session:
        logger.error(f"Async task: Could not find session for {player_id}.")
        return

    # Restore backend session state from persistent storage on every action
    # (survives server restarts / hot-reloads)
    try:
        ai_service.restore_backend_session(player_id, session)
    except Exception:
        pass

    try:
        # Extract base action (strip companion mode suffix like ":独行"/":同行")
        action_base = action.split(":")[0].strip() if ":" in action else action
        is_starting_trial = action_base in [
            "开始试炼",
            "开启下一次试炼",
            "开始第一次试炼",
        ] and not session.get("is_in_trial")
        is_first_ever_trial_of_day = (
            is_starting_trial
            and session.get("opportunities_remaining") == INITIAL_OPPORTUNITIES
        )

        # Reset backend session when starting a new trial
        if is_starting_trial:
            try:
                ai_service.reset_backend_session(player_id, session)
            except Exception:
                pass

            # ── Clean slate: reset internal_history to only system prompt ──
            # Previous trial's conversation (or error retries) must not bleed
            # into the new trial.
            session["internal_history"] = [
                {"role": "system", "content": GAME_MASTER_SYSTEM_PROMPT}
            ]
            # Keep only the welcome banner (first element) in display_history;
            # drop everything else (stale trial narratives, error messages, etc.)
            welcome = session.get("display_history", [""])[0] if session.get("display_history") else ""
            session["display_history"] = [welcome] if welcome else []
            logger.info(f"Trial start: cleared histories for {player_id}")

        # 记录试炼前的 opportunities 值，用于后续兜底判断
        if is_starting_trial:
            session["_pre_trial_opportunities"] = session.get("opportunities_remaining", 0)

        # ── Extract companion mode and difficulty from action ──
        # Frontend sends "开始试炼:独行:凡人修仙" or "开始试炼:同行:气运之子" etc.
        # Extended: "开始试炼:独行:凡人修仙:doupo:萧炎" (with scenario and character name)
        companion_mode = "同行（生成初始伙伴）"  # default
        difficulty_name = DEFAULT_DIFFICULTY
        scenario_id = "freestyle"
        player_character_name = ""
        if is_starting_trial and ":" in action:
            parts = action.split(":")
            if len(parts) >= 2:
                mode_part = parts[1].strip()
                if "独" in mode_part:
                    companion_mode = "独行（无同行伙伴）"
            if len(parts) >= 3:
                diff_part = parts[2].strip()
                if diff_part in DIFFICULTY_PRESETS:
                    difficulty_name = diff_part
            # 剧本模式解析: parts[3]=scenario_id, parts[4]=character_name
            if len(parts) >= 4:
                scenario_part = parts[3].strip()
                if scenario_part and scenario_part != "freestyle":
                    scenario_id = scenario_part
            if len(parts) >= 5:
                player_character_name = parts[4].strip()
            # Store in session
            session["difficulty"] = difficulty_name
            session["scenario_id"] = scenario_id
            if player_character_name:
                session["player_character_name"] = player_character_name
            # 剧本模式强制同行（人物关系由剧本预设决定）
            if scenario_id != "freestyle":
                companion_mode = "剧本模式（人物关系由原著预设）"
            logger.info(
                f"Trial start for {player_id}: companion={companion_mode}, "
                f"difficulty={difficulty_name}, scenario={scenario_id}, "
                f"character={player_character_name or '(random)'}"
            )

        # ── 剧本模式：注入 scenario-specific system prompt ──
        effective_scenario = session.get("scenario_id", "freestyle")
        if is_starting_trial and effective_scenario != "freestyle":
            from .scenarios import build_scenario_system_prompt, build_scenario_start_prompt
            scenario_supplement = build_scenario_system_prompt(effective_scenario)
            if scenario_supplement:
                # 在 system prompt 后追加剧本世界观
                combined_system_prompt = GAME_MASTER_SYSTEM_PROMPT + "\n\n" + scenario_supplement
                session["internal_history"] = [
                    {"role": "system", "content": combined_system_prompt}
                ]
                logger.info(f"剧本模式: 已注入 {effective_scenario} 世界观到 system prompt")

        session_copy = _build_compact_state_for_ai(session)

        # ── 选择开局 prompt（支持剧本模式） ──
        if is_starting_trial and effective_scenario != "freestyle":
            from .scenarios import build_scenario_start_prompt
            scenario_prompt = build_scenario_start_prompt(
                effective_scenario,
                player_name=session.get("player_character_name", ""),
                companion_mode=companion_mode,
            )
            if scenario_prompt:
                prompt_for_ai = (
                    scenario_prompt
                    .replace("{opportunities_remaining}", str(session["opportunities_remaining"]))
                    .replace("{opportunities_remaining_minus_1}", str(session["opportunities_remaining"] - 1))
                    .replace("{companion_mode}", companion_mode)
                )
            else:
                # 剧本数据加载失败，回退到默认
                prompt_for_ai = START_TRIAL_PROMPT.format(
                    opportunities_remaining=session["opportunities_remaining"],
                    opportunities_remaining_minus_1=session["opportunities_remaining"] - 1,
                    companion_mode=companion_mode,
                )
        else:
            prompt_for_ai = (
                START_GAME_PROMPT.format(companion_mode=companion_mode)
                if is_first_ever_trial_of_day
                else START_TRIAL_PROMPT.format(
                    opportunities_remaining=session["opportunities_remaining"],
                    opportunities_remaining_minus_1=session["opportunities_remaining"] - 1,
                    companion_mode=companion_mode,
                )
                if is_starting_trial
                else _build_action_prompt(session_copy, action)
            )

        # Update histories with user action first
        # ── 重要：不要把难度名称（如"气运之父""气运之子"）泄露给 AI，
        #    否则 AI 会据此生成与"气运"相关的天赋，污染初始天赋随机性。
        #    只保留 "开始试炼" 和同行模式，难度仅在后端代码层面生效。
        if is_starting_trial and ":" in action:
            # 原 action 形如 "开始试炼:独行:气运之父"
            # 清洗后只保留 "开始试炼:独行" 或 "开始试炼:同行"
            parts = action.split(":")
            sanitized_action = ":".join(parts[:2])  # "开始试炼:独行"
            session["internal_history"].append({"role": "user", "content": sanitized_action})
        else:
            session["internal_history"].append({"role": "user", "content": action})
        session["display_history"].append(f"> {action}")

        await state_manager.save_session(player_id, session)
        
        # --- 使用流式获取 AI 响应（含截断续写机制）---
        ai_json_response_str = await _get_ai_response_streaming(
            player_id, prompt_for_ai, session["internal_history"]
        )

        if ai_json_response_str.startswith("错误："):
            raise Exception(f"OpenAI Client Error: {ai_json_response_str}")

        # Persist backend session state immediately after first AI call
        # (don't wait for finally — if subsequent processing fails, we'd lose the id)
        try:
            ai_service.persist_backend_session(player_id, session)
        except Exception:
            pass

        logger.info(
            f"Full response ({len(ai_json_response_str)} chars, first 500): "
            f"{ai_json_response_str[:500]}"
        )

        # ── Try parse; if truncated, attempt continuation or repair ──
        ai_response_data = await _parse_with_continuation(
            ai_json_response_str,
            player_id,
            session["internal_history"],
        )

        # Handle Roll vs No-Roll Path
        if "roll_request" in ai_response_data and ai_response_data["roll_request"]:
            # --- ROLL PATH ---
            # 1. Update state with pre-roll narrative
            first_narrative = ai_response_data.get("narrative", "")
            session["display_history"].append(first_narrative)
            session["internal_history"].append(
                {
                    "role": "assistant",
                    "content": json.dumps(ai_response_data, ensure_ascii=False),
                }
            )

            # 2. SEND INTERIM UPDATE to show pre-roll narrative
            await state_manager.save_session(player_id, session)
            await asyncio.sleep(0.03)  # Give frontend a moment to render

            # 3. Perform roll and get final AI response
            final_ai_json_str, roll_event = await _handle_roll_request(
                player_id,
                session,
                session_copy,
                ai_response_data["roll_request"],
                action,
                first_narrative,
                internal_history=session["internal_history"],  # Pass updated history
            )
            # 4. Parse second-stage AI response (with plaintext fallback)
            try:
                final_json_str = _extract_json_from_response(final_ai_json_str)
                if not final_json_str:
                    raise json.JSONDecodeError(
                        "No JSON in second-stage", final_ai_json_str[:200] if final_ai_json_str else "", 0
                    )
                final_response_data = _robust_json_loads(final_json_str)
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(
                    f"Roll second-stage JSON parse failed: {e}. "
                    f"Using plaintext fallback for {len(final_ai_json_str)} chars."
                )
                final_response_data = _build_plaintext_fallback(
                    final_ai_json_str,
                    context="判定后叙事",
                    error_reason=str(e),
                )

            # 5. Process final response
            narrative = final_response_data.get("narrative", "")
            state_update = final_response_data.get("state_update", {})
            if state_update:
                session = _apply_state_update(session, state_update)
            session["display_history"].extend([roll_event["result_text"], narrative])
            session["internal_history"].extend(
                [
                    {"role": "system", "content": roll_event["result_text"]},
                    {"role": "assistant", "content": final_ai_json_str},
                ]
            )
        else:
            # --- NO ROLL PATH ---
            narrative = ai_response_data.get("narrative", "")
            state_update = ai_response_data.get("state_update", {})
            if state_update:
                session = _apply_state_update(session, state_update)
            session["display_history"].append(narrative)
            session["internal_history"].append(
                {"role": "assistant", "content": ai_json_response_str}
            )

        # --- 社交系统：展示好感度变动消息 ---
        social_messages = session.pop("_social_messages", [])
        if social_messages:
            social_text = "\n\n".join(f"⚔ {msg}" for msg in social_messages)
            session["display_history"].append(
                f"\n\n【人缘变动】\n\n{social_text}"
            )

        # --- NPC 时间演化：每隔若干回合 NPC 自然成长 ---
        if session.get("current_life"):
            round_count = session.get("_round_count", 0) + 1
            session["_round_count"] = round_count
            try:
                evolution_msgs = social_system.evolve_npcs_over_time(
                    session["current_life"], round_count
                )
                if evolution_msgs:
                    evo_text = "\n".join(f"· {m}" for m in evolution_msgs)
                    session["display_history"].append(
                        f"\n\n【江湖传闻】\n{evo_text}"
                    )
            except Exception as e:
                logger.warning(f"NPC时间演化异常: {e}")

        # --- 故事事件精简：每次状态更新后自动精简 ---
        if session.get("current_life"):
            _prune_story_events(session["current_life"])

        # --- 临时事件字段管理：标记更新 + 自动清理超龄字段 ---
        if state_update:
            _mark_event_fields_updated(session, list(state_update.keys()))
        if session.get("current_life"):
            cleaned_fields = _track_and_cleanup_event_fields(session)
            if cleaned_fields:
                cleanup_text = "、".join(f"「{f}」" for f in cleaned_fields)
                session["display_history"].append(
                    f"\n\n*{cleanup_text}已随因果消散，不再显示。*"
                )

        # --- 难度系统：开局时根据难度钳制属性 ---
        if is_starting_trial and session.get("current_life"):
            preset = _get_difficulty_preset(session)
            _clamp_attributes(session["current_life"], preset)
            if preset["label"] != DEFAULT_DIFFICULTY:
                logger.info(
                    f"Difficulty '{preset['label']}' applied for {player_id}: "
                    f"attr_min={preset.get('attr_min')}, attr_max={preset.get('attr_max')}"
                )

        # --- 剧本模式：程序化注入预设角色的人物关系和物品（含好感度） ---
        if is_starting_trial and session.get("current_life") and effective_scenario != "freestyle":
            try:
                from .scenarios import get_character_preset
                char_preset = get_character_preset(
                    effective_scenario,
                    session.get("player_character_name", ""),
                )
                if char_preset:
                    cl = session["current_life"]
                    # 注入人物关系
                    if "人物关系" in char_preset:
                        if "人物关系" not in cl or not isinstance(cl.get("人物关系"), dict):
                            cl["人物关系"] = {}
                        preset_relations = char_preset["人物关系"]
                        for npc_name, npc_data in preset_relations.items():
                            if isinstance(npc_data, dict):
                                cl["人物关系"][npc_name] = dict(npc_data)
                        logger.info(
                            f"剧本预设人物关系注入: {list(preset_relations.keys())}"
                        )
                    # 注入预设物品（确保关键道具如黑色戒指必定存在）
                    if "物品" in char_preset:
                        if "物品" not in cl or not isinstance(cl.get("物品"), list):
                            cl["物品"] = []
                        existing_names = {
                            item.get("名称") if isinstance(item, dict) else str(item)
                            for item in cl["物品"]
                        }
                        for item in char_preset["物品"]:
                            item_name = item.get("名称") if isinstance(item, dict) else str(item)
                            if item_name not in existing_names:
                                cl["物品"].append(dict(item) if isinstance(item, dict) else item)
                        logger.info(
                            f"剧本预设物品注入: "
                            f"{[i.get('名称', i) if isinstance(i, dict) else i for i in char_preset['物品']]}"
                        )
            except Exception as e:
                logger.warning(f"剧本预设注入失败: {e}")

        # --- 天赋属性加成：解析初始天赋描述中的属性+N并应用 ---
        # 例如 "魂穿（灵觉、意志+10）" → 灵觉+10, 意志+10
        if is_starting_trial and session.get("current_life"):
            _apply_talent_bonuses(session["current_life"])

        # --- 试炼开始兜底：确保 is_in_trial 和 opportunities_remaining 被正确设置 ---
        # AI 可能遗漏在 state_update 中设置这些字段，这里程序化保证
        if is_starting_trial and session.get("current_life"):
            if not session.get("is_in_trial"):
                session["is_in_trial"] = True
                logger.warning(
                    f"兜底: AI 未设置 is_in_trial=true，已由后端强制设置 ({player_id})"
                )
            expected_opp = session.get("_pre_trial_opportunities", session.get("opportunities_remaining", 0))
            if session.get("opportunities_remaining") == expected_opp and expected_opp > 0:
                session["opportunities_remaining"] = expected_opp - 1
                logger.warning(
                    f"兜底: AI 未扣减 opportunities_remaining，已由后端强制扣减 "
                    f"{expected_opp} -> {expected_opp - 1} ({player_id})"
                )

        # --- 静态字段合并：将姓名/出身等静态信息合并为「人物背景」 ---
        # 每次都检查，确保旧会话或遗漏情况也能补救
        if session.get("current_life"):
            _consolidate_static_fields(session)

        # --- 继承系统：试炼开始时应用先天奖励 ---
        if is_starting_trial and session.get("current_life"):
            try:
                session = await legacy_system.apply_blessings_to_session(player_id, session)
                applied = session.pop("applied_blessings_desc", None)
                if applied:
                    blessing_text = (
                        "\n\n【前世遗泽 · 先天觉醒】\n\n"
                        "轮回之力涌动，前世之因果在此刻显化：\n\n"
                        + "\n".join(f"> ✦ {desc}" for desc in applied)
                        + "\n\n前世功德，今生得报。善加利用此等先天之利。"
                    )
                    session["display_history"].append(blessing_text)
            except Exception as e:
                logger.error(f"应用先天奖励失败 {player_id}: {e}")

        # 清理临时标记
        session.pop("_pre_trial_opportunities", None)

        await state_manager.save_session(player_id, session)
        # --- Common final logic for both paths ---
        trigger = state_update.get("trigger_program")
        if trigger and trigger.get("name") == "spiritStoneConverter":
            effective_unchecked = _effective_unchecked_rounds_for_cheat_check(
                session.get("unchecked_rounds_count", 0)
            )
            inputs_to_check = await state_manager.get_last_n_inputs(
                player_id, 8 + effective_unchecked
            )

            await state_manager.save_session(
                player_id, session
            )  # Save before cheat check
            if "正常" == await cheat_check.run_cheat_check(player_id, inputs_to_check):
                # 重新获取 session，确保不为 None
                updated_session = await state_manager.get_session(player_id)
                if updated_session:
                    session = updated_session
                spirit_stones = trigger.get("spirit_stones", 0)
                end_game_data, end_day_update, earned_stones = end_game_and_get_code(
                    user_id, player_id, spirit_stones
                )
                session = _apply_state_update(session, end_day_update)
                session["display_history"].append(
                    end_game_data.get("final_message", "")
                )

                # --- 继承系统：综合评估功德点 ---
                if earned_stones > 0:
                    try:
                        difficulty_preset = _get_difficulty_preset(session)
                        legacy_result = await legacy_system.add_legacy_points(
                            player_id,
                            earned_stones,
                            session=session,
                            difficulty_multiplier=difficulty_preset["legacy_multiplier"],
                        )
                        pts = legacy_result["points_earned"]
                        total = legacy_result["total_points"]
                        lb = legacy_result.get("breakdown", {})
                        diff_name = session.get("difficulty", DEFAULT_DIFFICULTY)
                        diff_label = f"（难度: {diff_name}，系数×{difficulty_preset['legacy_multiplier']}）" if diff_name != DEFAULT_DIFFICULTY else ""
                        detail_parts = []
                        if lb.get("realm", 0) > 0:
                            detail_parts.append(f"境界 {lb['realm']}")
                        if lb.get("spirit_stones", 0) > 0:
                            detail_parts.append(f"灵石 {lb['spirit_stones']}")
                        if lb.get("items", 0) > 0:
                            detail_parts.append(f"道具 {lb['items']}")
                        if lb.get("attributes", 0) > 0:
                            detail_parts.append(f"属性 {lb['attributes']}")
                        detail_text = f"评分明细: {' + '.join(detail_parts)} = {lb.get('total_score', pts)}" if detail_parts else ""
                        session["display_history"].append(
                            f"\n\n【轮回铭刻 · 功德累积】\n\n"
                            f"此番试炼之因果已铭刻于轮回长河。\n\n"
                            f"> 综合评分: **{lb.get('total_score', pts)}** {diff_label}\n"
                            f"> {detail_text}\n"
                            f"> 获得功德点: **{pts}**\n"
                            f"> 累计功德点: **{total}**\n\n"
                            f"功德点可在下一次试炼前，于【先天奖励】中兑换前世遗泽，"
                            f"为新生之身注入先天优势。"
                        )
                    except Exception as e:
                        logger.error(f"继承系统记录失败 {player_id}: {e}")

            else:
                # 重新获取 session，确保不为 None
                updated_session = await state_manager.get_session(player_id)
                if updated_session:
                    session = updated_session
                else:
                    logger.error(f"Post-cheat-check: Could not find session for {player_id}.")
                    # 继续使用原有 session
                session["display_history"].append(
                    "【最终清算 · 天道审视】\n\n"
                    "就在汝即将破碎虚空之际——\n\n"
                    "整个世界骤然凝滞。时间静止，万物褪尽色彩，唯余黑白二色。\n\n"
                    "一道无悲无喜的目光自九天垂落，穿透时空，落于汝之神魂，开始审视此生一切轨迹。\n\n"
                    "> *「功过是非，皆有定数。然，汝之命途，存有异数。」*\n\n"
                    "天道之音在灵台中响起，不带丝毫情感，却蕴含不容置疑的威严。\n\n"
                    "> *「天机已被扰动，因果之线呈现不应有之扭曲。此番功果，暂且搁置。」*\n\n"
                    "> *「下一瞬间，将是对汝此生所有言行的最终裁决。清浊自分，功过相抵。届时，一切虚妄都将无所遁形。」*\n\n"
                    "汝感到一股无法抗拒的力量正在回溯此生的每一个瞬间。任何投机取巧的痕迹，都在这终极审视下被一一标记。\n\n"
                    "结局已定，无可更改。"
                )

    except Exception as e:
        logger.error(f"Error processing action for {player_id}: {e}", exc_info=True)
        logger.error(f"Full traceback:\n{traceback.format_exc()}")
        # ── Error recovery: show user-friendly message but do NOT pollute history ──
        # Previously, every error appended retry instructions to internal_history
        # and error text to display_history, causing garbage accumulation across
        # repeated failures. Now:
        # 1. Revert the user action that was appended to histories at line 821-822
        #    (it was never successfully processed, so keeping it is misleading).
        # 2. Show ONE transient error message without persisting retry instructions.
        if "session" in locals() and session:
            # Roll back the user action we optimistically appended before the AI call
            hist = session.get("internal_history", [])
            if hist and hist[-1].get("role") == "user":
                last_content = hist[-1].get("content", "")
                # 匹配原始 action 或清洗后的 sanitized_action
                if last_content == action or action.startswith(last_content):
                    hist.pop()
            disp = session.get("display_history", [])
            if disp and disp[-1] == f"> {action}":
                disp.pop()

            # ── 最终手段：如果 AI 有返回内容，将其作为明文展示给玩家 ──
            raw_ai_text = locals().get("ai_json_response_str", "")
            if raw_ai_text and len(raw_ai_text.strip()) > 20:
                fallback = _build_plaintext_fallback(
                    raw_ai_text,
                    context="异常恢复",
                    error_reason=str(locals().get("e", "未知异常")),
                )
                session["display_history"].append(fallback["narrative"])
                logger.info(
                    f"Error recovery: showed {len(raw_ai_text)} chars of AI raw text "
                    f"as plaintext fallback for {player_id}"
                )
            else:
                session["display_history"].append(
                    "【天机紊乱】\n\n"
                    "虚空微微震颤，汝之行动仿佛被一股无形之力化解，未能激起任何波澜。\n\n"
                    "天道运转偶有滞涩，此非汝之过。请稍候片刻，再作尝试。"
                )

    finally:
        try:
            if "session" in locals() and session:
                # Persist backend session state for hot-reload survival
                try:
                    ai_service.persist_backend_session(player_id, session)
                except Exception:
                    pass

                # Periodic cheat check in `finally` to guarantee execution
                session["unchecked_rounds_count"] = (
                    session.get("unchecked_rounds_count", 0) + 1
                )
                await state_manager.save_session(player_id, session)

                if session.get("unchecked_rounds_count", 0) > 5:
                    logger.info(f"Running periodic cheat check for {player_id}...")

                    # Re-fetch the session to get the most up-to-date count
                    s = await state_manager.get_session(player_id)
                    if s:
                        unchecked_count_raw = s.get("unchecked_rounds_count", 0)
                        unchecked_count = _effective_unchecked_rounds_for_cheat_check(
                            unchecked_count_raw
                        )
                        logger.debug(
                            f"Running cheat check for {player_id} with {unchecked_count_raw} rounds (effective={unchecked_count})."
                        )

                        inputs_to_check = await state_manager.get_last_n_inputs(
                            player_id, 8 + unchecked_count
                        )
                        # Only run if there are inputs, to save API calls
                        if inputs_to_check:
                            await cheat_check.run_cheat_check(
                                player_id, inputs_to_check
                            )

                        logger.debug(f"Cheat check for {player_id} finished.")
                    else:
                        logger.warning(
                            f"Session for {player_id} disappeared during cheat check."
                        )
        except Exception as e:
            logger.error(
                f"Error scheduling background cheat check for {player_id}: {e}",
                exc_info=True,
            )

        # 重新获取最新的 session 来重置状态
        try:
            session = await state_manager.get_session(player_id)
            if session:
                session["roll_event"] = None
                session["is_processing"] = False
                await state_manager.save_session(player_id, session)
                
                # 调度图片生成（如果启用）
                _schedule_image_generation(player_id, session.get("last_modified", 0))
        except Exception as e:
            logger.error(f"Error resetting session state for {player_id}: {e}", exc_info=True)
        
        logger.info(f"Async action task for {player_id} finished.")


async def _handle_manual_end_trial(current_user: dict):
    """
    处理玩家主动结束试炼。
    - 不发起 AI 调用
    - 不生成兑换码（灵石不带出）
    - 仍评估功德点（基于当前角色状态）
    - 结束试炼，释放 is_processing
    """
    player_id = current_user["username"]
    logger.info(f"Player {player_id} manually ending trial.")

    try:
        session = await state_manager.get_session(player_id)
        if not session or not session.get("is_in_trial"):
            logger.warning(f"Manual end: no active trial for {player_id}")
            return

        current_life = session.get("current_life") or {}
        realm = current_life.get("境界", current_life.get("修为", current_life.get("修为境界", "未知")))

        # 构建结束叙事
        end_narrative = (
            "\n\n【主动退出 · 试炼中止】\n\n"
            "汝闭目凝神，向天道传达了退出此番试炼的意愿。\n\n"
            "虚空微微震颤，一道温和的光芒将汝包裹。"
            "此生的一切记忆如走马灯般在眼前掠过——\n\n"
            f"> 最终境界：**{realm}**\n\n"
            "天道之音响起：\n\n"
            "> *「知进退者，亦为智者。此番因果，已铭刻于轮回长河。」*\n\n"
            "光芒散去，汝重归试炼之门前。\n\n"
            "---\n\n"
            "> ⚠ 主动结束试炼不会产生灵石兑换码，但仍可获得功德点。"
        )
        session["display_history"].append(end_narrative)

        # 结束试炼状态
        session["is_in_trial"] = False
        session["current_life"] = None
        session["internal_history"] = [
            {"role": "system", "content": GAME_MASTER_SYSTEM_PROMPT}
        ]

        # --- 继承系统：评估功德点（灵石=0，但境界/属性/道具仍可得分）---
        try:
            difficulty_preset = _get_difficulty_preset(session)
            legacy_result = await legacy_system.add_legacy_points(
                player_id,
                spirit_stones=0,
                session={"current_life": current_life},  # 传入结束前的角色状态
                difficulty_multiplier=difficulty_preset["legacy_multiplier"],
            )
            pts = legacy_result["points_earned"]
            total = legacy_result["total_points"]
            lb = legacy_result.get("breakdown", {})
            diff_name = session.get("difficulty", DEFAULT_DIFFICULTY)
            diff_label = (
                f"（难度: {diff_name}，系数×{difficulty_preset['legacy_multiplier']}）"
                if diff_name != DEFAULT_DIFFICULTY
                else ""
            )
            detail_parts = []
            if lb.get("realm", 0) > 0:
                detail_parts.append(f"境界 {lb['realm']}")
            if lb.get("spirit_stones", 0) > 0:
                detail_parts.append(f"灵石 {lb['spirit_stones']}")
            if lb.get("items", 0) > 0:
                detail_parts.append(f"道具 {lb['items']}")
            if lb.get("attributes", 0) > 0:
                detail_parts.append(f"属性 {lb['attributes']}")
            detail_text = (
                f"评分明细: {' + '.join(detail_parts)} = {lb.get('total_score', pts)}"
                if detail_parts
                else ""
            )
            if pts > 0:
                session["display_history"].append(
                    f"\n\n【轮回铭刻 · 功德累积】\n\n"
                    f"虽未破碎虚空，此番修行之因果仍留痕于天道。\n\n"
                    f"> 综合评分: **{lb.get('total_score', pts)}** {diff_label}\n"
                    f"> {detail_text}\n"
                    f"> 获得功德点: **{pts}**\n"
                    f"> 累计功德点: **{total}**\n\n"
                    f"功德点可在下一次试炼前，于【先天奖励】中兑换前世遗泽。"
                )
            else:
                session["display_history"].append(
                    "\n\n此番试炼修行尚浅，未能留下足够的因果印记，功德点未有增长。"
                )
        except Exception as e:
            logger.error(f"Legacy evaluation failed on manual end for {player_id}: {e}")

    except Exception as e:
        logger.error(f"Error in manual end trial for {player_id}: {e}", exc_info=True)
        if "session" in locals() and session:
            session["display_history"].append(
                "【天机紊乱】\n\n结束试炼时发生异常，但试炼已终止。"
            )
    finally:
        try:
            if "session" in locals() and session:
                session["is_processing"] = False
                session["roll_event"] = None
                try:
                    ai_service.persist_backend_session(player_id, session)
                except Exception:
                    pass
                await state_manager.save_session(player_id, session)
        except Exception as e:
            logger.error(f"Error finalizing manual end for {player_id}: {e}")


async def process_player_action(current_user: dict, action: str):
    player_id = current_user["username"]
    session = await state_manager.get_session(player_id)
    if not session:
        logger.error(f"Action for non-existent session: {player_id}")
        return
    if session.get("is_processing"):
        # 允许主动结束试炼穿透 is_processing 检查
        if action.strip() != "主动结束试炼":
            logger.warning(f"Action '{action}' blocked for {player_id}, processing.")
            return
    if session.get("daily_success_achieved"):
        logger.warning(f"Action '{action}' blocked for {player_id}, day complete.")
        return
    if session.get("opportunities_remaining", 10) <= 0 and not session.get(
        "is_in_trial"
    ):
        logger.warning(
            f"Action '{action}' blocked for {player_id}, no opportunities left."
        )
        return

    if session.get("pending_punishment"):
        punishment = session["pending_punishment"]
        level = punishment.get("level")
        reason = punishment.get("reason", "天机不可泄露")
        new_state = session.copy()
        
        if level == "轻度亵渎":
            punishment_narrative = f"""【天机示警 · 命途勘误】

虚空之中，传来一声若有若无的叹息。

汝方才之言，如投石入镜湖——虽微澜泛起，却已扰动既定的天机轨迹。

一道无形的目光自九天垂落，淡漠地注视着汝。神魂一凛，仿佛被看穿了所有心思。

> *「蝼蚁窥天，其心可悯，其行当止。」*

天道之音并非雷霆震怒，而是如万古不化的玄冰，不带丝毫情感。

---

**【天道之眼 · 审判记录】**

> {reason}

---

话音落下，眼前的世界开始如水墨画般褪色、模糊，最终化为一片虚无。此生的所有经历、记忆，乃至刚刚生出的一丝妄念，都随之烟消云散。

此非惩戒，乃是勘误。

为免因果错乱，此段命途，就此抹去。

---

> 天道已修正异常，当前试炼结束。善用下一次机缘，恪守本心，方能行稳致远。
"""
            new_state["is_in_trial"], new_state["current_life"] = False, None
            new_state["internal_history"] = [
                {"role": "system", "content": GAME_MASTER_SYSTEM_PROMPT}
            ]
        elif level == "重度渎道":
            punishment_narrative = f"""【天道斥逐 · 放逐乱流】

轰隆——！

这一次，并非雷鸣，而是整个天地法则都在为汝公然的挑衅而震颤。

脚下大地化为虚无，周遭星辰黯淡无光。时空在汝面前呈现出最原始、最混乱的姿态。

一道蕴含无上威严的金色法旨在虚空中展开，上面用大道符文烙印着两个字：

# 【渎 道】

> *「汝已非求道，而是乱道。」*

天道威严的声音响彻神魂，每一个字都化作法则之链，将汝牢牢锁住。

---

**【天道之眼 · 审判记录】**

> {reason}

---

> *「汝之行径，已触及此界根本。为护天地秩序，今将汝放逐于时空乱流之中，以儆效尤。」*

> *「一日之内，此界之门将对汝关闭。静思己过，或有再入轮回之机。若执迷不悟，再犯天条，必将汝之真灵从光阴长河中彻底抹去——神魂俱灭，永不超生。」*

金光散去，汝已被抛入无尽的混沌……

---

> 因严重违规触发【天道斥逐】，试炼资格暂时剥夺。一日之后，方可再次踏入轮回之门。
"""
            new_state["daily_success_achieved"] = True
            new_state["is_in_trial"], new_state["current_life"] = False, None
            new_state["opportunities_remaining"] = -10
        new_state["pending_punishment"] = None
        new_state["display_history"].append(punishment_narrative)
        await state_manager.save_session(player_id, new_state)
        return

    # Extract base action (strip companion mode suffix)
    action_base = action.split(":")[0].strip() if ":" in action else action
    is_starting_trial = action_base in [
        "开始试炼",
        "开启下一次试炼",
        "开始第一次试炼",
    ] and not session.get("is_in_trial")
    is_manual_end = action_base == "主动结束试炼" and session.get("is_in_trial")

    if is_starting_trial and session["opportunities_remaining"] <= 0:
        logger.warning(f"Player {player_id} tried to start trial with 0 opportunities.")
        return
    if not is_starting_trial and not is_manual_end and not session.get("is_in_trial"):
        logger.warning(
            f"Player {player_id} sent action '{action}' while not in a trial."
        )
        return

    if is_manual_end:
        # Handle manual end directly (no AI call needed)
        session["is_processing"] = True
        await state_manager.save_session(player_id, session)
        asyncio.create_task(_handle_manual_end_trial(current_user))
        return

    session["is_processing"] = True
    await state_manager.save_session(
        player_id, session
    )  # Save processing state immediately

    asyncio.create_task(_process_player_action_async(current_user, action))
