$ErrorActionPreference = 'Stop'

# ==== EDIT THESE PATHS FOR YOUR MACHINE ====
# Python executable (pythonw.exe on Windows avoids a console window)
$python = 'C:\Path\To\Python311\pythonw.exe'
# Absolute path to codex_a2a_bridge.py
$bridge = 'C:\Path\To\hermes-codex-bridge\codex_a2a_bridge.py'
# Workspace directory handed to Codex (working dir for delegated tasks)
$workspace = 'C:\Path\To\your-workspace'
$port = 9998
# State directory holds the Bearer token and task records — keep it private
$stateDir = 'C:\Path\To\hermes-codex-bridge\.state'
$tokenFile = Join-Path $stateDir 'bridge.token'
# Native Codex executable (resolved automatically below if not found)
$codex = Get-ChildItem -LiteralPath 'C:\Path\To\Codex\bin' -Recurse -Filter 'codex.exe' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1 -ExpandProperty FullName
# ==== END EDIT ====

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
    '--sync-wait', '540',
    '--codex-timeout', '1800',
    '--max-concurrent', '1',
    '--token-file', '"' + $tokenFile + '"'
) -join ' '

Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory (Split-Path $bridge) -WindowStyle Hidden
