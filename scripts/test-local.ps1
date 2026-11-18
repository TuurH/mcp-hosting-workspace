$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "lib\common.ps1")
Set-Location (Get-ProjectRoot)

$failures = 0
$skipped = 0
function Report([string] $Label, [bool] $Ok, [string] $Detail = "") {
    if ($Ok) { Write-Host "ok    $Label $Detail" }
    else { Write-Host "FAIL  $Label $Detail" -ForegroundColor Red; $script:failures++ }
}

function Skip-Check([string] $Detail) {
    Write-Host "skip  $Detail" -ForegroundColor Yellow
    $script:skipped++
}

Assert-DockerRunning

Write-Host "Waiting for the containers to become healthy (up to 3 minutes)..."
$deadline = (Get-Date).AddMinutes(3)
foreach ($container in "mcp-hosting-workspace", "mcp-hosting-files", "mcp-hosting-deployer", "mcp-hosting-admin") {
    do {
        $health = (Invoke-Native docker @("inspect", "--format", "{{.State.Health.Status}}", $container)).Output -join ""
        if ($health -eq "healthy") { break }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)
    Report "$container is healthy" ($health -eq "healthy") "($health)"
}

$health = Invoke-Native docker @("compose", "exec", "-T", "workspace", "curl", "-fsS", "http://127.0.0.1:8000/healthz")
$body = $health.Output -join ""
Report "workspace /healthz answers" ($health.ExitCode -eq 0) $body
if ($body -match '"locked"') {
    Write-Warning "The MCP endpoint is LOCKED: fill in the CF_ACCESS_* values in .env, then: docker compose up -d --force-recreate workspace"
    $failures++
}
if ($body -match '"insecure"') {
    Write-Warning "The workspace still runs in local test mode WITHOUT login (compose.local.yaml). Return to normal: docker compose up -d --force-recreate workspace cloudflared"
    $failures++
}

$files = Invoke-Native docker @("compose", "exec", "-T", "files", "python", "-c",
    "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8081/healthz', timeout=3).read().decode())")
Report "files service answers" ($files.ExitCode -eq 0) ($files.Output -join "")

foreach ($container in "mcp-hosting-pomerium", "mcp-hosting-static", "mcp-hosting-workspace-tunnel") {
    $state = (Invoke-Native docker @("inspect", "--format", "{{.State.Status}}", $container)).Output -join ""
    Report "$container is running" ($state -eq "running") "($state)"
}

$deployer = Invoke-Native docker @("compose", "exec", "-T", "workspace", "curl", "-fsS", "http://deployer:8090/healthz")
Report "workspace reaches the deployer" ($deployer.ExitCode -eq 0) ($deployer.Output -join "")

$platform = Invoke-Native docker @("exec", "mcp-hosting-deployer", "python", "-m", "mcp_hosting_platform.deployer.cli", "status")
$status = $null
if ($platform.ExitCode -eq 0) { try { $status = ($platform.Output -join "`n") | ConvertFrom-Json } catch { $status = $null } }
Report "deployer can use Docker (gaming mode)" ($null -ne $status -and $status.docker -eq "ok") `
    "($(if ($status) { $status.docker } else { 'no status' }); check DOCKER_SOCKET_GID in .env)"
Report "routing configuration is applied" ($null -ne $status -and $status.routing.in_sync -eq $true)
Report "anonymous routing checks are current" ($null -ne $status -and $status.routing.anonymous_check_current -eq $true)
if ($status -and @($status.route_problems).Count -gt 0) {
    Report "routing checks passed" $false (@($status.route_problems) -join "; ")
}

$admin = Invoke-Native docker @("compose", "exec", "-T", "admin", "python", "-c",
    "import urllib.request, urllib.error`ntry: urllib.request.urlopen('http://127.0.0.1:8080/', timeout=3)`nexcept urllib.error.HTTPError as e: print(e.code)")
$adminCode = ($admin.Output -join "").Trim()
if ($adminCode -eq "503") {
    Report "admin configuration complete" $false "LOCKED: set ADMIN_CF_ACCESS_AUD, PLATFORM_OWNER_EMAIL and ADMIN_CSRF_KEY in .env, then: docker compose up -d admin"
}
else {
    Report "admin page requires a login" ($adminCode -eq "403" -or $adminCode -eq "421") "(HTTP $adminCode)"
}

$ports = (Invoke-Native docker @("ps", "--filter", "label=com.docker.compose.project=mcp-hosting-workspace", "--format", "{{.Names}} {{.Ports}}")).Output |
    Where-Object { $_ -match "->" }
Report "no infrastructure host ports published" (-not $ports) ($ports -join "; ")
$appPorts = (Invoke-Native docker @("ps", "--filter", "label=mcp-hosting-workspace.app", "--format", "{{.Names}} {{.Ports}}")).Output |
    Where-Object { $_ -match "->" }
if ($appPorts) {
    Write-Host "Managed app listeners (compare each with its Network and GPU grants):"
    $appPorts | ForEach-Object { Write-Host "  $_" }
}

Write-Host "GPU check:"
$gpu = Invoke-Native docker @("compose", "exec", "-T", "workspace", "python", "/opt/app/scripts/gpu_test.py")
$gpu.Output | ForEach-Object { Write-Host "  $_" }
Report "GPU test" ($gpu.ExitCode -eq 0)

Write-Host "Container identity and workspace:"
$identity = Invoke-Native docker @("compose", "exec", "-T", "workspace", "bash", "-lc",
    "id && pwd && python --version && nvidia-smi --query-gpu=name,memory.total --format=csv,noheader")
$identity.Output | ForEach-Object { Write-Host "  $_" }

# --- From the internet ----------------------------------------------------------
# Nothing private may answer without a login. Skipped (with a note) while DNS or
# the tunnel are not set up yet.
$domain = Get-EnvValue "PLATFORM_DOMAIN"
if (-not $domain) { $domain = "example.com" }
Write-Host "From the internet:"
$canary = Get-HttpStatus "https://canary.$domain/"
if ($null -eq $canary) { Skip-Check "canary.$domain is not reachable yet (DNS or tunnel route missing?)" }
else {
    Report "canary.$domain asks for a login" ($canary.Status -in 301, 302, 303, 307 -and $canary.Location -match "^https://auth\.") `
        "(HTTP $($canary.Status) $($canary.Location))"
}
if ($null -ne $canary) {
    # Twice, with a file type Cloudflare caches by default: never a cache HIT.
    $null = Get-HttpStatus "https://canary.$domain/check.css"
    $again = Get-HttpStatus "https://canary.$domain/check.css"
    Report "private pages bypass Cloudflare caching" ($null -ne $again -and $again.CacheStatus -in "DYNAMIC", "BYPASS") `
        "(cf-cache-status $($again.CacheStatus); add the Cache Rule from docs\SETUP.md)"
    $unknown = Get-HttpStatus "https://no-such-app-$([guid]::NewGuid().ToString('N').Substring(0, 8)).$domain/"
    Report "unknown app names answer 404" ($null -ne $unknown -and $unknown.Status -eq 404) "(HTTP $($unknown.Status))"
}
$mcpHost = Get-EnvValue "PUBLIC_MCP_HOST"
if (-not $mcpHost) { $mcpHost = "mcp.$domain" }
foreach ($endpoint in "https://admin.$domain/", "https://$mcpHost/mcp") {
    $answer = Get-HttpStatus $endpoint
    if ($null -eq $answer) { Skip-Check "$endpoint is not reachable"; continue }
    if ($answer.Mitigated -eq "challenge") { Skip-Check "$endpoint answered with a Cloudflare challenge"; continue }
    # Our own servers' answers (the MCP server's JWT error, the admin page's CSP)
    # mean the request got past Cloudflare without an Access login.
    $fromOrigin = ($answer.Body -match "Cloudflare Access JWT") -or ($answer.Csp -match "form-action 'self'")
    $viaAccess = ($answer.Status -in 301, 302, 303, 307 -and $answer.Location -match "\.cloudflareaccess\.com") -or
        ($answer.Status -in 401, 403 -and -not $fromOrigin)
    Report "$endpoint is behind Cloudflare Access" ($viaAccess -and -not $fromOrigin) `
        "(HTTP $($answer.Status) $($answer.Location); create the Access application, docs\SETUP.md)"
}

if ($failures -gt 0) { Write-Host "$failures check(s) failed." -ForegroundColor Red; exit 1 }
if ($skipped -gt 0) { Write-Host "Local checks passed, but $skipped internet check(s) were skipped. Go-live validation is INCOMPLETE." -ForegroundColor Yellow; exit 2 }
Write-Host "All automated checks passed. Complete the signed-in and feature checks in docs\GO-LIVE.md."
