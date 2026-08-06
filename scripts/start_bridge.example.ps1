$ErrorActionPreference = 'Stop'

$python = 'C:\Path\To\pythonw.exe'
$bridge = 'C:\Path\To\a2a-bridge\codex_a2a_bridge.py'
$workspace = 'C:\Path\To\Workspace'
$port = 9998
$stateDir = 'C:\Path\To\Workspace\tools\.codex-a2a'
$tokenFile = Join-Path $stateDir 'bridge.token'
$codex = Get-ChildItem -LiteralPath 'C:\Path\To\Codex\bin' -Recurse -Filter 'codex.exe' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1 -ExpandProperty FullName

if (-not (Test-Path -LiteralPath $python)) { throw "Python not found: $python" }
if (-not (Test-Path -LiteralPath $bridge)) { throw "Bridge not found: $bridge" }
if (-not $codex -or -not (Test-Path -LiteralPath $codex)) { throw 'Native Codex executable not found' }
if (-not (Test-Path -LiteralPath $workspace)) { throw "Workspace not found: $workspace" }

if (-not (Test-Path -LiteralPath $stateDir)) {
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
}

# Generate or read the shared Bearer token, then hand it to the bridge via the token file.
$token = $null
if (Test-Path -LiteralPath $tokenFile) {
    $token = (Get-Content -LiteralPath $tokenFile -Raw -ErrorAction SilentlyContinue).Trim()
}
if (-not $token) {
    $token = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
    Set-Content -LiteralPath $tokenFile -Value $token -Encoding ASCII -NoNewline
}

# Inbound report token (reverse link: MCP server -> bridge /inbound/events)
$inboundTokenFile = Join-Path $stateDir 'inbound.token'
$inboundToken = $null
if (Test-Path -LiteralPath $inboundTokenFile) {
    $inboundToken = (Get-Content -LiteralPath $inboundTokenFile -Raw -ErrorAction SilentlyContinue).Trim()
}
if (-not $inboundToken) {
    $inboundToken = [guid]::NewGuid().ToString('N')
    Set-Content -LiteralPath $inboundTokenFile -Value $inboundToken -Encoding ASCII -NoNewline
}

# Port in use: confirm the listener is actually this bridge before treating it as "already running".
$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -TimeoutSec 3
        if ($health.service -ne 'codex-a2a-bridge') {
            throw "service mismatch: $($health.service)"
        }
        exit 0
    } catch {
        throw "Port $port is already in use by a process that is not the Codex A2A bridge (health check failed: $($_.Exception.Message)). Refusing to start."
    }
}

# Best-effort: restrict the state directory (token + task files) to the current user.
try {
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls $stateDir /inheritance:r /grant:r "${user}:(OI)(CI)F" 2>$null | Out-Null
} catch { }

$arguments = @(
    '"' + $bridge + '"',
    '--host', '127.0.0.1',
    '--port', "$port",
    '--workspace', '"' + $workspace + '"',
    '--codex', '"' + $codex + '"',
    '--state-dir', '"' + $stateDir + '"',
    '--sync-wait', '540',
    '--codex-timeout', '1800',
    '--max-concurrent', '1',
    '--token-file', '"' + $tokenFile + '"',
    '--inbound-token', '"' + $inboundToken + '"'
) -join ' '

Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory 'C:\Path\To\Workspace' -WindowStyle Hidden

# Also write an env file so the MCP server (hermes_mcp_server.py) can pick up
# the inbound report token without hardcoding it.
$envFile = Join-Path $stateDir 'inbound.env'
"INBOUND_REPORT_TOKEN=$inboundToken" | Set-Content -LiteralPath $envFile -Encoding ASCII
"INBOUND_REPORT_URL=http://127.0.0.1:$port" | Add-Content -LiteralPath $envFile -Encoding ASCII
