<#
.SYNOPSIS
    浮生十梦 - Windows 启动脚本 (PowerShell)
.DESCRIPTION
    自动创建虚拟环境、安装依赖、配置 .env 并启动 Uvicorn 服务器。
    用法: 右键以 PowerShell 运行，或在终端执行:
        powershell -ExecutionPolicy Bypass -File run_windows.ps1
#>

param(
    [switch]$SkipVenv,      # 跳过虚拟环境创建
    [switch]$SkipInstall,   # 跳过依赖安装
    [switch]$Production     # 生产模式 (禁用热重载)
)

# ─────────── 颜色输出工具 ───────────
function Write-Step  { param($msg) Write-Host "[*] $msg" -ForegroundColor Cyan }
function Write-Ok    { param($msg) Write-Host "[√] $msg" -ForegroundColor Green }
function Write-Warn  { param($msg) Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Err   { param($msg) Write-Host "[X] $msg" -ForegroundColor Red }

# ─────────── 0. 路径设置 ───────────
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot
Write-Step "项目根目录: $ProjectRoot"

$VenvDir     = Join-Path $ProjectRoot ".venv"
$BackendDir  = Join-Path $ProjectRoot "backend"
$ReqFile     = Join-Path $BackendDir  "requirements.txt"
$EnvFile     = Join-Path $BackendDir  ".env"
$EnvExample  = Join-Path $BackendDir  ".env.example"

# ─────────── 1. Python 检测 ───────────
Write-Step "检测 Python 环境..."

$PythonCmd = $null
foreach ($candidate in @("python", "python3", "py")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($ver -match "Python\s+3\.(\d+)") {
            $minor = [int]$Matches[1]
            if ($minor -ge 10) {
                $PythonCmd = $candidate
                Write-Ok "找到 $ver (命令: $candidate)"
                break
            }
        }
    } catch { }
}

if (-not $PythonCmd) {
    Write-Err "未找到 Python 3.10+，请先安装 Python: https://www.python.org/downloads/"
    Write-Err "安装时务必勾选 'Add Python to PATH'"
    Read-Host "按 Enter 退出"
    exit 1
}

# ─────────── 2. 虚拟环境 ───────────
if (-not $SkipVenv) {
    if (-not (Test-Path $VenvDir)) {
        Write-Step "创建虚拟环境 (.venv)..."
        & $PythonCmd -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) {
            Write-Err "虚拟环境创建失败"
            Read-Host "按 Enter 退出"
            exit 1
        }
        Write-Ok "虚拟环境已创建"
    } else {
        Write-Ok "虚拟环境已存在"
    }
}

# 激活虚拟环境
$ActivateScript = Join-Path $VenvDir "Scripts\Activate.ps1"
if (Test-Path $ActivateScript) {
    Write-Step "激活虚拟环境..."
    & $ActivateScript
    Write-Ok "虚拟环境已激活"
} else {
    Write-Warn "未找到虚拟环境激活脚本，使用全局 Python"
}

# ─────────── 3. 安装依赖 ───────────
if (-not $SkipInstall) {
    if (Test-Path $ReqFile) {
        Write-Step "安装 / 更新依赖..."
        & pip install --upgrade pip 2>&1 | Out-Null
        & pip install -r $ReqFile
        if ($LASTEXITCODE -ne 0) {
            Write-Err "依赖安装失败，请检查网络或 requirements.txt"
            Read-Host "按 Enter 退出"
            exit 1
        }
        Write-Ok "依赖安装完成"
    } else {
        Write-Warn "未找到 $ReqFile，跳过依赖安装"
    }
}

# ─────────── 4. .env 配置 ───────────
if (-not (Test-Path $EnvFile)) {
    if (Test-Path $EnvExample) {
        Write-Step "首次运行：从 .env.example 创建 .env ..."
        Copy-Item $EnvExample $EnvFile
        Write-Warn "已创建 $EnvFile，请编辑该文件填入你的配置（API Key 等）后重新运行"
        Write-Warn "  记事本打开命令: notepad $EnvFile"
        Read-Host "按 Enter 退出并前往编辑 .env"
        exit 0
    } else {
        Write-Err "未找到 .env 和 .env.example，请先创建配置文件"
        Read-Host "按 Enter 退出"
        exit 1
    }
} else {
    Write-Ok ".env 配置文件已存在"
}

# ─────────── 5. 加载 .env 到当前 Session ───────────
Write-Step "加载 .env 环境变量..."
$envContent = Get-Content $EnvFile -Encoding UTF8
foreach ($line in $envContent) {
    $line = $line.Trim()
    # 跳过注释和空行
    if ($line -eq "" -or $line.StartsWith("#")) { continue }
    # 解析 KEY=VALUE (支持带引号的值)
    if ($line -match '^([^=]+)=(.*)$') {
        $key   = $Matches[1].Trim()
        $value = $Matches[2].Trim().Trim('"').Trim("'")
        [System.Environment]::SetEnvironmentVariable($key, $value, "Process")
    }
}
Write-Ok ".env 变量已加载"

# 读取服务器设置
$ServerHost = if ($env:HOST)  { $env:HOST }  else { "127.0.0.1" }
$ServerPort = if ($env:PORT)  { $env:PORT }  else { "8000" }
$DoReload   = if ($Production) { "" } else {
    if ($env:UVICORN_RELOAD -eq "true") { "--reload" } else { "" }
}

# ─────────── 6. 启动服务器 ───────────
Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════════╗" -ForegroundColor Magenta
Write-Host "║           浮 生 十 梦  /  Ten Cycles of Fate            ║" -ForegroundColor Magenta
Write-Host "╠══════════════════════════════════════════════════════════╣" -ForegroundColor Magenta
Write-Host "║  地址:  http://${ServerHost}:${ServerPort}                         ║" -ForegroundColor Magenta
Write-Host "║  模式:  $(if($Production){'生产'}else{'开发 (热重载)'})                              ║" -ForegroundColor Magenta
Write-Host "║  停止:  Ctrl + C                                        ║" -ForegroundColor Magenta
Write-Host "╚══════════════════════════════════════════════════════════╝" -ForegroundColor Magenta
Write-Host ""

$uvicornArgs = @(
    "-m", "uvicorn",
    "backend.app.main:app",
    "--host", $ServerHost,
    "--port", $ServerPort
)
if ($DoReload) { $uvicornArgs += $DoReload }

try {
    & python @uvicornArgs
} catch {
    Write-Err "服务器启动失败: $_"
} finally {
    Write-Host ""
    Write-Warn "服务器已停止"
    Read-Host "按 Enter 退出"
}
