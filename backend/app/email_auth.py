"""
邮箱注册/登录系统
==================

功能：
- 邮箱+密码注册，需邮箱验证码确认
- 邮箱+密码登录
- 验证码通过SMTP发送到用户邮箱
- 用户数据存储在 JSON 文件中（与 legacy_system 保持一致的存储模式）

用户数据存储路径: game_data/users/<email_hash>.json
验证码存储: 内存中（带过期时间）
"""

import hashlib
import json
import logging
import random
import smtplib
import string
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Any

import aiofiles

from .config import settings
from . import auth  # reuse password hashing and JWT creation

logger = logging.getLogger(__name__)

# --- Storage ---
USERS_DIR = Path("game_data") / "users"

# --- Verification codes: email -> {code, expires_at, purpose} ---
_verification_codes: dict[str, dict] = {}

# Code validity duration (seconds)
CODE_EXPIRY_SECONDS = 300  # 5 minutes
CODE_COOLDOWN_SECONDS = 60  # minimum gap between sends


def _email_hash(email: str) -> str:
    """Deterministic filename-safe hash of email."""
    return hashlib.sha256(email.lower().strip().encode()).hexdigest()[:24]


def _user_path(email: str) -> Path:
    return USERS_DIR / f"{_email_hash(email)}.json"


async def _read_user(email: str) -> dict | None:
    path = _user_path(email)
    if not path.exists():
        return None
    try:
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            return json.loads(await f.read())
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"读取用户数据失败 {email}: {e}")
        return None


async def _write_user(email: str, data: dict):
    path = _user_path(email)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(data, ensure_ascii=False, indent=2))
    except IOError as e:
        logger.error(f"写入用户数据失败 {email}: {e}")
        raise


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Verification code
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _generate_code(length: int = 6) -> str:
    return "".join(random.choices(string.digits, k=length))


def _send_email(to_email: str, subject: str, body_html: str) -> bool:
    """Send email via SMTP. Returns True on success."""
    if not settings.SMTP_HOST or not settings.SMTP_USER:
        logger.warning("SMTP未配置，无法发送邮件。验证码将仅记录在日志中。")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_USER}>"
        msg["To"] = to_email
        msg.attach(MIMEText(body_html, "html", "utf-8"))

        if settings.SMTP_USE_SSL:
            server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10)
        else:
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10)
            server.starttls()

        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.sendmail(settings.SMTP_USER, [to_email], msg.as_string())
        server.quit()
        logger.info(f"验证码邮件已发送至 {to_email}")
        return True
    except Exception as e:
        logger.error(f"发送邮件失败: {e}", exc_info=True)
        return False


async def send_verification_code(email: str, purpose: str = "register") -> dict:
    """
    Generate and send a verification code to the email.

    Args:
        email: target email
        purpose: "register" or "login" or "reset"

    Returns:
        {"success": bool, "message": str}
    """
    email = email.lower().strip()
    if not email or "@" not in email:
        return {"success": False, "message": "邮箱格式不正确"}

    # Check cooldown
    existing = _verification_codes.get(email)
    if existing:
        elapsed = time.time() - (existing.get("created_at", 0))
        if elapsed < CODE_COOLDOWN_SECONDS:
            remaining = int(CODE_COOLDOWN_SECONDS - elapsed)
            return {"success": False, "message": f"请等待 {remaining} 秒后再试"}

    # For register: check user doesn't already exist
    if purpose == "register":
        user = await _read_user(email)
        if user and user.get("verified"):
            return {"success": False, "message": "该邮箱已注册，请直接登录"}

    # For login/reset: check user exists
    if purpose in ("login", "reset"):
        user = await _read_user(email)
        if not user or not user.get("verified"):
            return {"success": False, "message": "该邮箱未注册"}

    code = _generate_code()
    _verification_codes[email] = {
        "code": code,
        "purpose": purpose,
        "expires_at": time.time() + CODE_EXPIRY_SECONDS,
        "created_at": time.time(),
    }

    subject = f"【{settings.SMTP_FROM_NAME}】验证码"
    purpose_text = {"register": "注册", "login": "登录验证", "reset": "重置密码"}.get(purpose, "验证")
    body_html = f"""
    <div style="font-family: 'KaiTi','STKaiti',serif; max-width:500px; margin:0 auto; padding:30px; 
         background:#f5f7f6; border:2px solid #8a704c; border-radius:8px;">
        <h2 style="color:#2c1e18; text-align:center;">浮生十梦 · {purpose_text}</h2>
        <p style="color:#3a2e28; font-size:16px;">汝的验证码为：</p>
        <div style="text-align:center; margin:20px 0;">
            <span style="font-size:32px; font-weight:bold; color:#a8453c; letter-spacing:8px;
                 background:#fff; padding:10px 30px; border-radius:6px; border:1px solid #8a704c;">
                {code}
            </span>
        </div>
        <p style="color:#666; font-size:14px;">此验证码 {CODE_EXPIRY_SECONDS // 60} 分钟内有效，请勿泄露于他人。</p>
        <p style="color:#999; font-size:12px; margin-top:20px; border-top:1px solid #ddd; padding-top:10px;">
            若非汝本人操作，请忽略此信。—— 天道试炼官
        </p>
    </div>
    """

    # Run blocking SMTP in a thread to avoid blocking the event loop
    import asyncio
    loop = asyncio.get_event_loop()
    try:
        sent = await loop.run_in_executor(None, _send_email, email, subject, body_html)
    except Exception as e:
        logger.error(f"邮件发送异常: {e}")
        sent = False
    if not sent:
        # SMTP not configured — log the code for dev/testing
        logger.warning(f"[DEV] 验证码 for {email}: {code} (SMTP未配置或发送失败)")

    return {"success": True, "message": "验证码已发送，请查收邮箱"}


def verify_code(email: str, code: str, purpose: str = "register") -> bool:
    """Verify a submitted code. Consumes it on success."""
    email = email.lower().strip()
    stored = _verification_codes.get(email)
    if not stored:
        return False
    if stored["purpose"] != purpose:
        return False
    if time.time() > stored["expires_at"]:
        del _verification_codes[email]
        return False
    if stored["code"] != code.strip():
        return False

    # Consume code
    del _verification_codes[email]
    return True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Registration & Login
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def register_user(email: str, password: str, code: str, client_ip: str = "") -> dict:
    """
    Register a new user with email verification code OR invite code.

    The `code` field accepts:
    1. Email verification code (sent via send_verification_code)
    2. Invite code (6-digit, generated by admin script)

    If an invite code is provided, email verification is skipped.

    Returns:
        {"success": bool, "message": str}
    """
    from . import invite_code as invite_code_module

    email = email.lower().strip()

    if not email or "@" not in email:
        return {"success": False, "message": "邮箱格式不正确"}
    if not password or len(password) < 6:
        return {"success": False, "message": "密码长度至少为6位"}

    # Check if already registered
    existing = await _read_user(email)
    if existing and existing.get("verified"):
        return {"success": False, "message": "该邮箱已注册"}

    # Determine code type: invite code or email verification code
    code = code.strip()
    used_invite_code = False

    if invite_code_module.is_invite_code_format(code):
        # Try as invite code first
        invite_result = await invite_code_module.validate_and_consume(
            code, email, client_ip
        )
        if invite_result["valid"]:
            used_invite_code = True
            logger.info(f"用户 {email} 通过邀请码 {code} 注册")
        else:
            # Not a valid invite code — fall through to try as verification code
            if not verify_code(email, code, "register"):
                return {"success": False, "message": invite_result["message"]}
    else:
        # Not invite code format — must be email verification code
        if not verify_code(email, code, "register"):
            return {"success": False, "message": "验证码错误或已过期"}

    # Create user
    # Generate a stable user ID (numeric) for compatibility with existing system
    user_id = abs(hash(email)) % (10 ** 9)
    # Use email prefix as username
    username = email.split("@")[0]
    # Ensure uniqueness by appending hash suffix
    username = f"{username}_{_email_hash(email)[:6]}"

    user_data = {
        "email": email,
        "username": username,
        "password_hash": auth.get_password_hash(password),
        "user_id": user_id,
        "verified": True,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "login_method": "email",
        "registered_via": f"invite_code:{code}" if used_invite_code else "email_verification",
    }

    await _write_user(email, user_data)
    logger.info(f"新用户注册: {email} -> {username}")

    return {"success": True, "message": "注册成功，请登录"}


async def login_user(email: str, password: str) -> dict:
    """
    Login with email and password.

    Returns:
        {"success": bool, "message": str, "token": str | None}
    """
    email = email.lower().strip()

    user = await _read_user(email)
    if not user or not user.get("verified"):
        return {"success": False, "message": "邮箱未注册", "token": None}

    if not auth.verify_password(password, user["password_hash"]):
        return {"success": False, "message": "密码错误", "token": None}

    # Create JWT — same format as OAuth login for compatibility
    from datetime import timedelta
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    jwt_payload = {
        "sub": user["username"],
        "id": user["user_id"],
        "name": user["username"],
        "trust_level": 1,  # default trust level for email users
        "login_method": "email",
    }
    token = auth.create_access_token(data=jwt_payload, expires_delta=access_token_expires)

    logger.info(f"用户登录: {email}")
    return {"success": True, "message": "登录成功", "token": token}


def is_email_auth_enabled() -> bool:
    """Check if email auth is available (SMTP may be optional for dev)."""
    # Email auth is always available; SMTP is optional (codes logged in dev mode)
    return True
