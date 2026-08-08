# ============================================================
# A2A SDK sidecar 启动脚本（可选组件）
# ============================================================
# 用途：启动标准 A2A 接入层（默认 :10000），面向第三方标准 A2A agent
# 前提：已安装 a2a-sdk（pip install 'hermes-codex-bridge[a2a]'）
# 默认：不启动（现有桥 :9998 照常工作，本组件是可选增强）
#
# 用法示例：
#   # 本地最小（HTTP :10000，内存态）
#   powershell -File start_a2a_sidecar.ps1
#
#   # 全特性（持久化 + gRPC + 推送）
#   powershell -File start_a2a_sidecar.ps1 -Db sidecar_tasks.db -GrpcPort 50051 -Push
#
#   # 远程部署（Bearer 鉴权 + 独立 token）
#   powershell -File start_a2a_sidecar.ps1 -RequireToken -SidecarToken <你的token>
#
# 参数说明：
#   -Port          HTTP 监听端口（默认 10000）
#   -Db            SQLite 文件路径（TaskStore 持久化；不填=内存态，重启丢失）
#   -GrpcPort      gRPC 监听端口（默认 0=不启用 gRPC；建议 50051）
#   -Push          [开关] 启用推送通知（任务状态变化主动推给客户端 webhook）
#   -RequireToken  [开关] 远程部署加固（所有 A2A 端点需 Bearer token）
#   -SidecarToken  sidecar 自身鉴权 token（RequireToken 时生效；不填=复用桥 token）
#   -BridgeUrl     内部桥地址（默认 http://127.0.0.1:9998）
# ============================================================

param(
    [int]$Port = 10000,
    [string]$Db = "",
    [int]$GrpcPort = 0,
    [switch]$Push,
    [switch]$RequireToken,
    [string]$SidecarToken = "",
    [string]$BridgeUrl = "http://127.0.0.1:9998"
)

$ErrorActionPreference = 'Stop'

# 找 python（优先 sidecar 专用 venv，其次系统）
$Python = $null
$venvCandidates = @(
    "$PSScriptRoot\.a2a-venv\Scripts\python.exe",
    "$PSScriptRoot\..\.a2a-venv\Scripts\python.exe"
)
foreach ($v in $venvCandidates) {
    if (Test-Path -LiteralPath $v) { $Python = $v; break }
}
if (-not $Python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $Python = $cmd.Source }
}
if (-not $Python) {
    Write-Error "找不到 python。需要 a2a-sdk 环境：pip install 'hermes-codex-bridge[a2a]'"
    exit 1
}

$sidecar = Join-Path $PSScriptRoot 'a2a_sidecar.py'
if (-not (Test-Path -LiteralPath $sidecar)) {
    Write-Error "找不到 a2a_sidecar.py（$sidecar）"
    exit 1
}

# 组装参数
$args = @($sidecar, "--port", $Port, "--bridge-url", $BridgeUrl)
if ($Db) { $args += "--db", $Db }
if ($GrpcPort -gt 0) { $args += "--grpc-port", $GrpcPort }
if ($Push) { $args += "--push" }
if ($RequireToken) {
    $args += "--require-token"
    if ($SidecarToken) { $args += "--sidecar-token", $SidecarToken }
}

$featureDesc = @()
if ($Db) { $featureDesc += "持久化($Db)" }
if ($GrpcPort -gt 0) { $featureDesc += "gRPC(:$GrpcPort)" }
if ($Push) { $featureDesc += "推送" }
if ($RequireToken) { $featureDesc += "鉴权" }
$feat = if ($featureDesc) { $featureDesc -join " + " } else { "最小(内存态)" }

Write-Host "[a2a-sidecar] 启动 127.0.0.1:$Port -> $BridgeUrl [$feat]"
Write-Host "[a2a-sidecar] python: $Python"
& $Python $args
