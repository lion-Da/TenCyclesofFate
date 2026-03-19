"""
邀请码系统 (Invite Code System)
================================

功能：
- 生成6位数字邀请码，带使用次数限制
- 同一IP只能使用同一邀请码一次
- 注册时验证码字段可填邀请码，跳过邮箱验证直接注册
- 数据持久化在 JSON 文件中

存储路径: game_data/invite_codes.json
"""

import json
import logging
import random
import time
from pathlib import Path

import aiofiles

logger = logging.getLogger(__name__)

INVITE_CODES_PATH = Path("game_data") / "invite_codes.json"


async def _read_all() -> dict:
    """读取所有邀请码数据。返回 {code_str: {...}, ...}"""
    try:
        if not INVITE_CODES_PATH.exists():
            return {}
        async with aiofiles.open(INVITE_CODES_PATH, "r", encoding="utf-8") as f:
            return json.loads(await f.read())
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"读取邀请码数据失败: {e}")
        return {}


async def _write_all(data: dict):
    """写入所有邀请码数据。"""
    try:
        INVITE_CODES_PATH.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(INVITE_CODES_PATH, "w", encoding="utf-8") as f:
            await f.write(json.dumps(data, ensure_ascii=False, indent=2))
    except IOError as e:
        logger.error(f"写入邀请码数据失败: {e}")
        raise


def generate_code(max_uses: int = 10, note: str = "") -> dict:
    """
    同步生成一个新的邀请码并写入文件。
    供命令行脚本调用。

    Args:
        max_uses: 最大使用次数
        note: 备注（如"内测群第一批"）

    Returns:
        {"code": str, "max_uses": int, "note": str}
    """
    # 读取现有数据（同步版本，供脚本使用）
    try:
        if INVITE_CODES_PATH.exists():
            data = json.loads(INVITE_CODES_PATH.read_text(encoding="utf-8"))
        else:
            data = {}
    except (json.JSONDecodeError, IOError):
        data = {}

    # 生成不重复的6位数字
    for _ in range(100):
        code = "".join([str(random.randint(0, 9)) for _ in range(6)])
        if code not in data:
            break
    else:
        raise RuntimeError("无法生成唯一邀请码，请检查已有数据量")

    data[code] = {
        "code": code,
        "max_uses": max_uses,
        "used_count": 0,
        "used_ips": [],
        "used_by": [],
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": note,
    }

    INVITE_CODES_PATH.parent.mkdir(parents=True, exist_ok=True)
    INVITE_CODES_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    logger.info(f"生成邀请码: {code} (最大使用次数={max_uses}, 备注={note})")
    return {"code": code, "max_uses": max_uses, "note": note}


async def validate_and_consume(code: str, email: str, client_ip: str) -> dict:
    """
    验证并消费一个邀请码。

    Args:
        code: 6位数字邀请码
        email: 注册邮箱
        client_ip: 客户端IP

    Returns:
        {"valid": bool, "message": str}
    """
    data = await _read_all()

    entry = data.get(code)
    if not entry:
        return {"valid": False, "message": "邀请码不存在"}

    # 检查使用次数
    if entry["used_count"] >= entry["max_uses"]:
        return {"valid": False, "message": "该邀请码已达到使用上限"}

    # 检查同IP
    if client_ip and client_ip in entry["used_ips"]:
        return {"valid": False, "message": "该IP已使用过此邀请码"}

    # 检查同邮箱（防止同一邮箱重复消费）
    if email in entry["used_by"]:
        return {"valid": False, "message": "该邮箱已使用过此邀请码"}

    # 消费
    entry["used_count"] += 1
    if client_ip:
        entry["used_ips"].append(client_ip)
    entry["used_by"].append(email)

    await _write_all(data)

    remaining = entry["max_uses"] - entry["used_count"]
    logger.info(
        f"邀请码 {code} 被 {email} (IP: {client_ip}) 使用，"
        f"剩余 {remaining}/{entry['max_uses']} 次"
    )
    return {"valid": True, "message": "邀请码验证通过"}


def is_invite_code_format(code: str) -> bool:
    """判断一个字符串是否符合邀请码格式（6位纯数字）。"""
    return bool(code) and len(code) == 6 and code.isdigit()
