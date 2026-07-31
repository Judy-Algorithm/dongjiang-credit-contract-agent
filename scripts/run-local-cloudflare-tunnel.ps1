param(
    [string]$TokenFile = "deploy/.env.local-tunnel"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$tokenPath = Join-Path $repoRoot $TokenFile

if (-not (Test-Path -LiteralPath $tokenPath -PathType Leaf)) {
    throw "Token file not found: $tokenPath. Copy deploy/.env.local-tunnel.example first."
}

$tokenLine = Get-Content -LiteralPath $tokenPath | Where-Object {
    $_ -match '^\s*CLOUDFLARE_TUNNEL_TOKEN\s*='
} | Select-Object -First 1
$token = if ($tokenLine) { ($tokenLine -replace '^\s*CLOUDFLARE_TUNNEL_TOKEN\s*=\s*', '').Trim() } else { "" }
if ([string]::IsNullOrWhiteSpace($token) -or $token -eq "replace-with-cloudflare-tunnel-token") {
    throw "CLOUDFLARE_TUNNEL_TOKEN is missing in $tokenPath."
}

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/health" -TimeoutSec 5
    if (-not $health.ok) { throw "health endpoint returned ok=false" }
} catch {
    throw "The local app is not healthy at http://127.0.0.1:8765. Start it with scripts/run_web.sh or python -m dongjiang_agent.cli serve --host 127.0.0.1 --port 8765. Details: $($_.Exception.Message)"
}

$cloudflared = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $cloudflared) {
    $cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
}
if (-not (Test-Path -LiteralPath $cloudflared)) {
    throw "cloudflared was not found. Install Cloudflare Tunnel first."
}

Write-Host "Local app is healthy. Starting Cloudflare Tunnel for credit.1832104.xyz..."
Write-Host "Keep this window open; closing it disconnects the public site."

& $cloudflared tunnel --no-autoupdate run --token $token

exit $LASTEXITCODE
