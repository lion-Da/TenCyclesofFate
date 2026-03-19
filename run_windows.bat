@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1
title 浮生十梦 - Ten Cycles of Fate

REM ══════════════════════════════════════════════
REM   浮生十梦 Windows 启动脚本 (双击即运行)
REM ══════════════════════════════════════════════

cd /d "%~dp0"
echo.
echo  [浮生十梦] 正在初始化...
echo.

REM ─── 1. 检测 Python ───
where python >nul 2>&1
if !ERRORLEVEL! neq 0 (
    echo [错误] 未找到 Python，请安装 Python 3.10+ 并勾选 "Add to PATH"
    echo         https://www.python.org/downloads/
    pause
    exit /b 1
)

REM 用 Python 自身检查版本号
python -c "import sys; exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if !ERRORLEVEL! neq 0 (
    echo [警告] Python 版本可能不兼容，建议 3.10+
)
for /f "delims=" %%V in ('python --version 2^>^&1') do echo [*] 检测到 %%V

REM ─── 2. 虚拟环境 ───
if not exist ".venv\Scripts\activate.bat" (
    echo [*] 创建虚拟环境...
    python -m venv .venv
    if !ERRORLEVEL! neq 0 (
        echo [错误] 虚拟环境创建失败
        pause
        exit /b 1
    )
    echo [ok] 虚拟环境已创建
) else (
    echo [ok] 虚拟环境已存在
)

REM 激活
call .venv\Scripts\activate.bat

REM ─── 3. 安装依赖 ───
if exist "backend\requirements.txt" (
    echo [*] 检查并安装依赖...
    pip install -r backend\requirements.txt -q
    if !ERRORLEVEL! neq 0 (
        echo [错误] 依赖安装失败
        pause
        exit /b 1
    )
    echo [ok] 依赖就绪
)

REM ─── 4. .env 配置 ───
if not exist "backend\.env" (
    if exist "backend\.env.example" (
        echo [*] 首次运行：正在从 .env.example 创建 .env ...
        copy "backend\.env.example" "backend\.env" >nul
        echo.
        echo ════════════════════════════════════════════════════
        echo  请编辑 backend\.env 填入你的 API Key 等配置后重新运行
        echo  可以用记事本打开: notepad backend\.env
        echo ════════════════════════════════════════════════════
        echo.
        pause
        exit /b 0
    ) else (
        echo [错误] 未找到 backend\.env 和 backend\.env.example
        pause
        exit /b 1
    )
)

REM ─── 5. 用 Python 安全加载 .env 中的 HOST 和 PORT ───
set "_HELPER=%TEMP%\_tcof_env.py"
echo from dotenv import dotenv_values> "!_HELPER!"
echo v = dotenv_values("backend/.env")>> "!_HELPER!"
echo print(v.get("HOST", "127.0.0.1"))>> "!_HELPER!"
echo print(v.get("PORT", "8000"))>> "!_HELPER!"
set "_IDX=0"
for /f "delims=" %%L in ('python "!_HELPER!" 2^>nul') do (
    if !_IDX! equ 0 ( set "HOST=%%L" ) else ( set "PORT=%%L" )
    set /a _IDX+=1
)
del "!_HELPER!" >nul 2>&1

if not defined HOST set "HOST=127.0.0.1"
if not defined PORT set "PORT=8000"

REM ─── 6. 启动 ───
echo.
echo ╔══════════════════════════════════════════════════════════╗
echo ║           浮 生 十 梦  /  Ten Cycles of Fate            ║
echo ╠══════════════════════════════════════════════════════════╣
echo ║  地址:  http://!HOST!:!PORT!                            ║
echo ║  停止:  Ctrl + C                                        ║
echo ╚══════════════════════════════════════════════════════════╝
echo.

python -m uvicorn backend.app.main:app --host !HOST! --port !PORT! --reload

echo.
echo [!] 服务器已停止
pause
endlocal
