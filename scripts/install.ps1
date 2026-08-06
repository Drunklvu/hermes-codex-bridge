# ============================================================
# hermes-codex-bridge · one-shot installer (Windows)
# ============================================================
# 一键安装：自动配置 Codex MCP 注册 + Hermes A2A peer + 启动桥 + 开机自启
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File install.ps1            # 安装
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall # 卸载
#   powershell -ExecutionPolicy Bypass -File install.ps1 -DryRun    # 只探测不写入
#
# 可选参数：
#   -BridgeDir  <path>  桥脚本所在目录（默认：本脚本所在目录）
#   -Workspace  <path>  Codex 工作目录（默认：桥目录的上级）
#   -Port       <int>   桥端口（默认 9998）
#   -Python     <path>  python 解释器（默认自动探测 pythonw.exe）
#   -Codex      <path>  codex 可执行文件（默认自动探测）
#   -HermesCli  <path>  hermes CLI（默认自动探测；隔离测试可指向假的）
# ============================================================
param(
    [switch]$Uninstall,
    [switch]$DryRun,
    [string]$BridgeDir = "",
    [string]$Workspace = "",
    [int]$Port = 9998,
    [string]$Python = "",
    [string]$Codex = "",
    [string]$HermesCli = ""
)

$ErrorActionPreference = 'Stop'

# ---------- 工具函数 ----------
# 写文件用 UTF-8 无 BOM（PowerShell 5.1 的 Set-Content -Encoding UTF8 会写 BOM，
# 破坏 TOML 解析——Codex 审查 #2）
function Write-Utf8NoBom {
    param([string]$Path, [string]$Content)
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
}
# TOML 转义：单反斜杠 -> 双反斜杠（TOML 字符串里 \ 才是字面 \，Codex 审查 #1）
function ConvertTo-TomlPath {
    param([string]$P)
    return $P.Replace('\', '\\').Replace('"', '\"')
}

# ---------- 路径探测 ----------
# install.ps1 在 scripts/ 子目录，桥脚本在仓库根（上级目录）
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $BridgeDir) {
    # 如果 scripts/ 存在说明是仓库布局，桥在上级；否则就在本目录
    if (Test-Path -LiteralPath (Join-Path $scriptDir '..\codex_a2a_bridge.py')) {
        $BridgeDir = Split-Path -Parent $scriptDir
    } else {
        $BridgeDir = $scriptDir
    }
}
$bridgePy = Join-Path $BridgeDir 'codex_a2a_bridge.py'
$mcpPy    = Join-Path $BridgeDir 'hermes_mcp_server.py'
$startScript = Join-Path $BridgeDir 'scripts\start_bridge.example.ps1'

if (-not $Workspace) { $Workspace = Split-Path -Parent $BridgeDir }

# 探测 python（MCP 用 python.exe 保证 stdio；桥用 pythonw.exe 后台运行）
if (-not $Python) {
    $python  = (Get-Command python.exe  -ErrorAction SilentlyContinue).Source
    $pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
    if (-not $python) {
        $cand = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Recurse -Filter python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cand) { $python = $cand.FullName }
    }
    if (-not $pythonw) {
        $candw = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Recurse -Filter pythonw.exe -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($candw) { $pythonw = $candw.FullName }
    }
    $Python = $python   # 默认用 python.exe（stdio 可用）
    $PythonW = $pythonw # 桥后台用
} else {
    # 手动指定：同目录推断 pythonw
    $PythonW = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $PythonW)) { $PythonW = $Python }
}

# 探测 codex 可执行文件
$codexExe = $Codex
if (-not $codexExe) {
$codexCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\codex\codex.exe"),
    (Join-Path $env:USERPROFILE ".codex\bin\codex.exe"),
    (Join-Path $env:APPDATA "npm\codex.cmd")
)
foreach ($c in $codexCandidates) { if (Test-Path -LiteralPath $c) { $codexExe = $c; break } }
if (-not $codexExe) {
    $found = Get-Command codex.exe, codex.cmd -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { $codexExe = $found.Source }
}
}

# Codex 配置目录
$codexConfigDir = Join-Path $env:USERPROFILE '.codex'
$codexConfigToml = Join-Path $codexConfigDir 'config.toml'

# Hermes CLI（参数优先）
$hermesCli = $HermesCli
if (-not $hermesCli) { $hermesCli = (Get-Command hermes -ErrorAction SilentlyContinue).Source }

# ---------- 验证 ----------
Write-Host ""
Write-Host "=== hermes-codex-bridge 安装器 ===" -ForegroundColor Cyan
Write-Host "  桥脚本:   $bridgePy" 
Write-Host "  MCP 脚本: $mcpPy"
Write-Host "  工作目录: $Workspace"
Write-Host "  端口:     $Port"
if ($Python)   { Write-Host "  Python:   $Python" }   else { Write-Host "  Python:   [未找到]" -ForegroundColor Yellow }
if ($codexExe) { Write-Host "  Codex:    $codexExe" } else { Write-Host "  Codex:    [未找到]" -ForegroundColor Yellow }
if ($hermesCli){ Write-Host "  Hermes:   $hermesCli" } else { Write-Host "  Hermes:   [未找到]" -ForegroundColor Yellow }
Write-Host ""

if (-not (Test-Path -LiteralPath $bridgePy)) { throw "桥脚本不存在: $bridgePy" }
if (-not (Test-Path -LiteralPath $mcpPy))    { throw "MCP 脚本不存在: $mcpPy" }
if (-not $Python)    { throw "未找到 Python（可用 -Python 指定）" }
if (-not $codexExe)  { throw "未找到 Codex CLI（可用 -Codex 指定）" }

if ($DryRun) {
    Write-Host "=== 干跑模式：仅探测，不写入任何配置 ===" -ForegroundColor Yellow
    exit 0
}

# ---------- 卸载 ----------
if ($Uninstall) {
    Write-Host "=== 卸载 ===" -ForegroundColor Cyan
    # 0. 尝试停止运行中的桥（不要求存在）
    try {
        $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if ($listener) {
            Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
            Write-Host "  [0/4] 已停止运行中的桥" -ForegroundColor Green
        }
    } catch { Write-Host "  [0/4] 停止桥失败（可手动结束进程）" -ForegroundColor Yellow }
    # 1. 移除 Codex MCP 注册（config.toml 不存在则跳过，不报错）
    if (Test-Path -LiteralPath $codexConfigToml) {
        $toml = Get-Content -LiteralPath $codexConfigToml -Raw
        if (-not $toml) { $toml = "" }
        if ($toml -match '(?ms)^\[mcp_servers\.hermes\]') {
            $re = [regex]'(?ms)^\[mcp_servers\.hermes\].*?(?=^\[|\z)'
            $toml = $re.Replace($toml, '', 1)
            Write-Utf8NoBom -Path $codexConfigToml -Content $toml
            Write-Host "  [1/4] 已移除 Codex MCP 注册 [mcp_servers.hermes]" -ForegroundColor Green
        } else { Write-Host "  [1/4] Codex MCP 注册不存在，跳过" }
    } else { Write-Host "  [1/4] config.toml 不存在，跳过" }
    # 2. 移除 Hermes A2A peer（尽力，失败不阻断）
    if ($hermesCli) {
        try { & $hermesCli config set a2a_agents.codex "" --force 2>$null; Write-Host "  [2/4] 已移除 Hermes A2A peer codex" -ForegroundColor Green }
        catch { Write-Host "  [2/4] 移除 Hermes peer 失败（可手动删 a2a_agents.codex 段）" -ForegroundColor Yellow }
    }
    # 3. 移除自启动
    $lnk = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\codex-a2a-bridge.lnk'
    if (Test-Path -LiteralPath $lnk) { Remove-Item -LiteralPath $lnk -Force; Write-Host "  [3/4] 已移除开机自启" -ForegroundColor Green }
    # 4. 提示保留 state 目录
    Write-Host "  [4/4] state 目录（token/任务记录）已保留，如需删除手动清理" -ForegroundColor Yellow
    Write-Host "=== 卸载完成 ===" -ForegroundColor Cyan
    exit 0
}

# ---------- 1. 配置 Codex MCP 注册 ----------
Write-Host "=== [1/4] 配置 Codex MCP 注册 ===" -ForegroundColor Cyan
if (-not (Test-Path -LiteralPath $codexConfigDir)) { New-Item -ItemType Directory -Path $codexConfigDir -Force | Out-Null }
if (-not (Test-Path -LiteralPath $codexConfigToml)) { Write-Utf8NoBom -Path $codexConfigToml -Content "" }

$toml = Get-Content -LiteralPath $codexConfigToml -Raw
if (-not $toml) { $toml = "" }
$mcpPython = ConvertTo-TomlPath $Python
$mcpScript = ConvertTo-TomlPath $mcpPy
$mcpBlock = @"

[mcp_servers.hermes]
command = "$mcpPython"
args = ["$mcpScript"]
startup_timeout_sec = 15
tool_timeout_sec = 300
enabled = true
required = false
enabled_tools = ["call_hermes"]
"@

# 先备份（Codex 审查 #7：覆盖用户配置前必须可回退）
Copy-Item -LiteralPath $codexConfigToml -Destination ($codexConfigToml + ".bak") -Force

if ($toml -match '(?ms)^\[mcp_servers\.hermes\]') {
    # 已存在：替换整段（用 [regex]::Replace + MatchEvaluator 避免 $ 展开）
    $re = [regex]'(?ms)^\[mcp_servers\.hermes\].*?(?=^\[|\z)'
    $toml = $re.Replace($toml, { param($m) $mcpBlock + "`n" }, 1)
    Write-Host "  [mcp_servers.hermes] 已存在，更新配置（原配置备份为 .bak）" -ForegroundColor Yellow
} else {
    $toml = $toml.TrimEnd() + "`n" + $mcpBlock + "`n"
    Write-Host "  [mcp_servers.hermes] 新增注册" -ForegroundColor Green
}
Write-Utf8NoBom -Path $codexConfigToml -Content $toml
Write-Host "  已写入 $codexConfigToml"

# ---------- 2. 配置 Hermes A2A peer ----------
Write-Host "=== [2/4] 配置 Hermes A2A peer ===" -ForegroundColor Cyan
if ($hermesCli) {
    # 生成/复用 token
    $stateDir = Join-Path $Workspace 'tools\.codex-a2a'
    if (-not (Test-Path -LiteralPath $stateDir)) { New-Item -ItemType Directory -Path $stateDir -Force | Out-Null }
    $tokenFile = Join-Path $stateDir 'bridge.token'
    $token = $null
    if (Test-Path -LiteralPath $tokenFile) { $token = (Get-Content -LiteralPath $tokenFile -Raw).Trim() }
    if (-not $token) { $token = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N'); Set-Content -LiteralPath $tokenFile -Value $token -Encoding ASCII -NoNewline }

    # 用 hermes config set 写嵌套 key（避免手改 config.yaml）
    & $hermesCli config set a2a_agents.codex.url "http://127.0.0.1:$Port" --force 2>$null
    & $hermesCli config set a2a_agents.codex.timeout 600 --force 2>$null
    & $hermesCli config set a2a_agents.codex.capabilities '["coding"]' --force 2>$null
    & $hermesCli config set a2a_agents.codex.auth.type bearer --force 2>$null
    & $hermesCli config set a2a_agents.codex.auth.token $token --force 2>$null
    Write-Host "  已注册 a2a_agents.codex -> http://127.0.0.1:$Port（token 在 $tokenFile）" -ForegroundColor Green
} else {
    Write-Host "  未找到 hermes CLI，跳过 Hermes peer 配置（可手动配置）" -ForegroundColor Yellow
}

# ---------- 3. 从示例生成真实 start 脚本（先于自启，Codex 审查 #4） ----------
Write-Host "=== [3/4] 生成真实启动脚本 ===" -ForegroundColor Cyan
$exampleScript = Join-Path $BridgeDir 'scripts\start_bridge.example.ps1'
$realScript = Join-Path $BridgeDir 'scripts\start_bridge.ps1'
if (Test-Path -LiteralPath $exampleScript) {
    $startContent = Get-Content -LiteralPath $exampleScript -Raw
    # 桥用 pythonw（后台无窗口）；MCP 用 python.exe 在 [1/4] 已处理
    $bridgePython = $PythonW
    if (-not $bridgePython) { $bridgePython = $Python }
    $startContent = $startContent.Replace('C:\Path\To\pythonw.exe', $bridgePython)
    $startContent = $startContent.Replace('C:\Path\To\a2a-bridge\codex_a2a_bridge.py', $bridgePy)
    $startContent = $startContent.Replace('C:\Path\To\Workspace', $Workspace)
    $startContent = $startContent.Replace('C:\Path\To\Codex\bin', (Split-Path -Parent $codexExe))
    # 端口替换（Codex 审查 #5）：$port = 9998 -> 实际端口
    $startContent = $startContent -replace '(?m)^(\$port = )9998$', ('${1}' + $Port)
    Write-Utf8NoBom -Path $realScript -Content $startContent
    Write-Host "  已生成真实启动脚本: $realScript" -ForegroundColor Green
    $startScript = $realScript
} else {
    Write-Host "  未找到示例 start 脚本，跳过生成" -ForegroundColor Yellow
}

# ---------- 4. 开机自启（指向真实脚本） ----------
Write-Host "=== [4/4] 开机自启 ===" -ForegroundColor Cyan
$startupDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
$lnkPath = Join-Path $startupDir 'codex-a2a-bridge.lnk'
if ((Test-Path -LiteralPath $startScript) -and -not (Test-Path -LiteralPath $lnkPath)) {
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($lnkPath)
    $lnk.TargetPath = "powershell.exe"
    $lnk.Arguments = "-ExecutionPolicy Bypass -File `"$startScript`""
    $lnk.WorkingDirectory = $BridgeDir
    $lnk.Save()
    Write-Host "  已创建开机自启快捷方式: $lnkPath（指向 $startScript）" -ForegroundColor Green
} elseif (Test-Path -LiteralPath $lnkPath) {
    Write-Host "  自启快捷方式已存在，跳过" -ForegroundColor Yellow
} else {
    Write-Host "  无真实启动脚本，跳过自启" -ForegroundColor Yellow
}

# ---------- 5. 启动桥 ----------
Write-Host "=== [5/5] 启动桥 ===" -ForegroundColor Cyan
if (Test-Path -LiteralPath $startScript) {
    & powershell.exe -ExecutionPolicy Bypass -File $startScript
    Start-Sleep -Seconds 3
    $bridgeUp = $false
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5
        if ($health.service -eq 'codex-a2a-bridge') {
            Write-Host "  桥已启动 ✅  http://127.0.0.1:$Port/ui" -ForegroundColor Green
            $bridgeUp = $true
        }
    } catch {
        Write-Host "  桥启动失败（手动运行 start_bridge.ps1 排查）" -ForegroundColor Yellow
    }
    if (-not $bridgeUp) { exit 1 }
} else {
    Write-Host "  无 start 脚本，跳过启动（手动运行桥）" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "=== 安装完成 ===" -ForegroundColor Cyan
Write-Host "  监控页:    http://127.0.0.1:$Port/ui"
Write-Host "  下一步:    重启 Codex（让 MCP 注册生效），然后在 Codex 里调用 call_hermes"
Write-Host "  卸载:      powershell -File install.ps1 -Uninstall"
