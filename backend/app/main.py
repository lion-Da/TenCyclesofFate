import logging
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated
from pathlib import Path

from fastapi import (
    FastAPI, APIRouter, Depends, HTTPException, status,
    WebSocket, WebSocketDisconnect, Request
)
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

from . import auth, game_logic, state_manager, security, legacy_system, email_auth
from .websocket_manager import manager as websocket_manager
from .live_system import live_manager
from .request_queue import queue as request_queue
from .config import settings

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- App Lifecycle ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("Application startup...")
    await state_manager.init_storage()
    state_manager.start_auto_save_task()
    request_queue.start()
    yield
    logging.info("Application shutdown...")
    request_queue.stop()
    await state_manager.shutdown_storage()

# --- FastAPI App Instance ---
app = FastAPI(lifespan=lifespan, title="浮生十梦")

# Add SessionMiddleware for OAuth flow state management
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)

# --- Routers ---
# Router for /api prefixed routes
api_router = APIRouter(prefix="/api")
# Router for root-level routes like /callback
root_router = APIRouter()


# --- Authentication Routes ---
@api_router.get('/login/linuxdo')
async def login_linuxdo(request: Request):
    """
    Redirects the user to Linux.do for authentication.
    临时关闭: Linux.do 登录渠道维护中
    """
    raise HTTPException(status_code=503, detail="Linux.do 登录渠道暂时关闭维护中")

@root_router.get('/callback')
async def auth_linuxdo_callback(request: Request):
    """
    Handles the callback from Linux.do after authentication.
    This route is now at the root to match the expected OAuth callback URL.
    Fetches user info, creates a JWT, and sets it in a cookie.
    """
    try:
        token = await auth.oauth.linuxdo.authorize_access_token(request)
    except Exception as e:
        logger.error(f"Error during OAuth callback: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not authorize access token",
        )

    resp = await auth.oauth.linuxdo.get('api/user', token=token)
    resp.raise_for_status()
    user_info = resp.json()

    # Create JWT with user info from linux.do
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    jwt_payload = {
        "sub": user_info.get("username"),
        "id": user_info.get("id"),
        "name": user_info.get("name"),
        "trust_level": user_info.get("trust_level"),
    }
    access_token = auth.create_access_token(
        data=jwt_payload, expires_delta=access_token_expires
    )

    # Set token in cookie and redirect to frontend
    response = RedirectResponse(url="/")
    response.set_cookie(
        "token",
        value=access_token,
        httponly=True,
        max_age=int(access_token_expires.total_seconds()),
        samesite="lax",
    )
    return response

@api_router.post("/logout")
async def logout():
    """
    Logs the user out by clearing the authentication cookie.
    """
    response = RedirectResponse(url="/")
    response.delete_cookie("token")
    return response


# --- Email Authentication Routes ---

class EmailSendCodeRequest(BaseModel):
    email: str
    purpose: str = "register"  # "register" | "login" | "reset"

class EmailRegisterRequest(BaseModel):
    email: str
    password: str
    code: str

class EmailLoginRequest(BaseModel):
    email: str
    password: str

@api_router.post("/auth/send-code")
async def send_verification_code(request: EmailSendCodeRequest):
    """发送邮箱验证码"""
    return await email_auth.send_verification_code(request.email, request.purpose)

@api_router.post("/auth/register")
async def register_with_email(request: EmailRegisterRequest, raw_request: Request):
    """使用邮箱注册（支持邮箱验证码或邀请码）"""
    # 获取客户端真实IP（支持反向代理）
    client_ip = (
        raw_request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or raw_request.headers.get("X-Real-IP", "")
        or (raw_request.client.host if raw_request.client else "")
    )
    return await email_auth.register_user(
        request.email, request.password, request.code, client_ip=client_ip
    )

@api_router.post("/auth/login")
async def login_with_email(request: EmailLoginRequest):
    """使用邮箱登录"""
    result = await email_auth.login_user(request.email, request.password)
    if result["success"] and result.get("token"):
        from fastapi.responses import JSONResponse
        response = JSONResponse(content={"success": True, "message": result["message"]})
        from datetime import timedelta
        max_age = int(timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES).total_seconds())
        response.set_cookie(
            "token",
            value=result["token"],
            httponly=True,
            max_age=max_age,
            samesite="lax",
        )
        return response
    return result

# --- Game Routes ---
@api_router.get("/live/players")
async def get_live_players():
    """Returns a list of the most recently active players for the live view."""
    return state_manager.get_most_recent_sessions(limit=10)

@api_router.get("/scenarios")
async def get_scenarios():
    """返回所有可用剧本列表。"""
    from .scenarios import list_scenarios
    return list_scenarios()

@api_router.get("/scenarios/{scenario_id}/characters")
async def get_scenario_characters(scenario_id: str):
    """返回指定剧本的可选角色列表及是否允许自定义角色。"""
    from .scenarios import get_scenario_data
    data = get_scenario_data(scenario_id)
    if not data:
        return {"characters": [], "allow_custom": True}
    return {
        "characters": data.get("playable_characters", []),
        "allow_custom": data.get("allow_custom_character", True),
    }

@api_router.post("/game/init")
async def init_game(
    current_user: Annotated[dict, Depends(auth.get_current_active_user)],
):
    """
    Initializes or retrieves the daily game session for the player.
    This does NOT start a trial, it just ensures the session for the day exists.
    """
    game_state = await game_logic.get_or_create_daily_session(current_user)
    return game_state


# --- Legacy System Routes ---

class BlessingPurchaseRequest(BaseModel):
    blessing_id: str

@api_router.get("/legacy")
async def get_legacy(
    current_user: Annotated[dict, Depends(auth.get_current_active_user)],
):
    """获取玩家的继承系统数据（功德点、可用先天奖励等）"""
    player_id = current_user["username"]
    return await legacy_system.get_legacy_data(player_id)


@api_router.post("/legacy/purchase")
async def purchase_blessing(
    request: BlessingPurchaseRequest,
    current_user: Annotated[dict, Depends(auth.get_current_active_user)],
):
    """购买一个先天奖励"""
    player_id = current_user["username"]
    return await legacy_system.purchase_blessing(player_id, request.blessing_id)


@api_router.post("/legacy/clear")
async def clear_blessings(
    current_user: Annotated[dict, Depends(auth.get_current_active_user)],
):
    """清除当前激活的先天奖励（新一天开始时由前端调用或自动执行）"""
    player_id = current_user["username"]
    await legacy_system.clear_active_blessings(player_id)
    return {"success": True}

# --- WebSocket Endpoint ---
@api_router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Handles WebSocket connections for real-time game state updates."""
    token = websocket.cookies.get("token")
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Missing token")
        return
    try:
        payload = auth.decode_access_token(token)
        username: str | None = payload.get("sub")
        if username is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid token payload")
            return
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Token validation failed")
        return

    await websocket_manager.connect(websocket, username)

    try:
        user_info = await auth.get_current_user(token)
        session = await state_manager.get_session(user_info["username"])
        if session:
            await websocket_manager.send_json_to_player(
                user_info["username"], {"type": "full_state", "data": session}
            )

        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            if action:
                await game_logic.process_player_action(user_info, action)

    except WebSocketDisconnect:
        websocket_manager.disconnect(username)
        request_queue.cancel(username)
    except Exception as e:
        logger.error(f"WebSocket error for {username}: {e}", exc_info=True)
        websocket_manager.disconnect(username)
        request_queue.cancel(username)

@api_router.websocket("/live/ws")
async def live_websocket_endpoint(websocket: WebSocket):
    """Handles WebSocket connections for the live viewing system."""
    token = websocket.cookies.get("token")
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Missing token")
        return
    try:
        user_info = await auth.get_current_user(token)
        viewer_id = user_info["username"]
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Token validation failed")
        return

    await websocket_manager.connect(websocket, viewer_id)

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            if action == "watch":
                encrypted_id = data.get("player_id")
                if encrypted_id:
                    target_id = security.decrypt_player_id(encrypted_id)
                    if not target_id:
                        logger.warning(f"Received invalid encrypted ID from {viewer_id}")
                        continue
                    
                    live_manager.add_viewer(viewer_id, target_id)
                    # Send the current state of the watched player immediately
                    target_state = await state_manager.get_session(target_id)
                    if target_state:
                        await websocket_manager.send_json_to_player(
                            viewer_id, {"type": "live_update", "data": target_state}
                        )

    except WebSocketDisconnect:
        websocket_manager.disconnect(viewer_id)
        live_manager.remove_viewer(viewer_id)


# --- Include API Router and Mount Static Files ---
app.include_router(api_router)
app.include_router(root_router) # Include the root router before mounting static files
static_files_dir = Path(__file__).parent.parent.parent / "frontend"

# --- 404 Exception Handler ---
@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    """Redirect all 404 errors to the root page."""
    return RedirectResponse(url="/")

app.mount("/", StaticFiles(directory=static_files_dir, html=True), name="static")

# --- Uvicorn Runner ---
if __name__ == "__main__":
    import uvicorn
    # The first argument should be "main:app" and we should specify the app_dir
    # This makes running the script directly more robust.
    # For command line, the equivalent is:
    # uvicorn backend.app.main:app --host <host> --port <port> --reload
    uvicorn.run(
        "main:app",
        app_dir="backend/app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.UVICORN_RELOAD
    )