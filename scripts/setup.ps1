<#
.SYNOPSIS
    First-time setup and upgrades: creates .env, adds new settings, generates
    secrets, installs the Dockerfile build worker, builds and starts the stack.
.PARAMETER UpdateWsl
    Also run "wsl --update" and "wsl --shutdown" first. This restarts Docker Desktop.
.PARAMETER Pull
    Also download newer base images (CUDA, Python) before building. Slower: it can
    rebuild the whole workspace image.
.PARAMETER BuildOnly
    Install the build worker and build images; do not start anything (used while the old version still runs).
#>
param([switch] $UpdateWsl, [switch] $Pull, [switch] $BuildOnly)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "lib\common.ps1")
Set-Location (Get-ProjectRoot)

if ($UpdateWsl) {
    Write-Host "Updating WSL (this restarts Docker Desktop)..."
    wsl --update
    wsl --shutdown
    Write-Host "Start Docker Desktop again, wait until it is running, then press Enter."
    Read-Host | Out-Null
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example. Fill in the Cloudflare values before going live."
}
else {
    $added = Add-MissingEnvKeys
    if ($added.Count -gt 0) { Write-Host "Added new settings to .env: $($added -join ', ')" }
}

foreach ($name in "FILES_MINT_TOKEN", "DEPLOYER_API_TOKEN", "ADMIN_CSRF_KEY") {
    if (-not (Get-EnvValue $name)) {
        Set-EnvValue $name (New-RandomSecret 32)
        Write-Host "Generated $name in .env."
    }
}

# The platform owner defaults to the address that may use the MCP connector.
if (-not (Get-EnvValue "PLATFORM_OWNER_EMAIL")) {
    $first = ((Get-EnvValue "CF_ACCESS_ALLOWED_EMAILS") -split ",")[0]
    if ($first) { $first = $first.Trim() }
    if ($first -and $first -match "@" -and $first -notmatch "@example\.com$") {
        Set-EnvValue "PLATFORM_OWNER_EMAIL" $first
        Write-Host "PLATFORM_OWNER_EMAIL set to $first (the first address of CF_ACCESS_ALLOWED_EMAILS)."
    }
}

$problems = @()
if ((Get-EnvValue "CF_ACCESS_ENABLED") -ne "true") { $problems += "CF_ACCESS_ENABLED is not true" }
foreach ($name in "CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD", "CF_ACCESS_ALLOWED_EMAILS") {
    $value = Get-EnvValue $name
    if (-not $value -or $value -match "CHANGE-ME|@example\.com") { $problems += "$name is not set" }
}
if ($problems.Count -gt 0) {
    Write-Warning ("The MCP endpoint will stay locked (HTTP 503) until .env is complete: " + ($problems -join "; "))
}
if (-not (Get-EnvValue "CLOUDFLARE_TUNNEL_TOKEN")) {
    Write-Warning "CLOUDFLARE_TUNNEL_TOKEN is empty: cloudflared will keep restarting and mcp/files stay offline until it is set."
}
$memory = Get-EnvValue "WORKSPACE_MEMORY_LIMIT"
if ($memory -match '^(\d+)g$' -and [int]$Matches[1] -gt 12) {
    Write-Warning "WORKSPACE_MEMORY_LIMIT is $memory. With a 32 GB PC use 12g and keep it below memory= in %UserProfile%\.wslconfig (see docs\SETUP.md)."
}

Assert-DockerRunning

# Dockerfile apps need this pinned worker even though its service is in the
# optional tools profile. Provision it for normal setup and BuildOnly alike.
Write-Host "Installing the BuildKit worker for Dockerfile apps..."
if ((Invoke-NativeLive docker @("compose", "--profile", "tools", "pull", "builder-image")) -ne 0) {
    throw "BuildKit worker download failed. Check Docker/network access and rerun setup."
}
$cudaImage = Get-EnvValue "CUDA_BASE_IMAGE"
if (-not $cudaImage) { $cudaImage = "nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04" }
if (-not (Test-DockerImage $cudaImage)) {
    Write-Host "Downloading the CUDA image for the GPU check (several GB, only the first time)..."
    if ((Invoke-NativeLive docker @("pull", $cudaImage)) -ne 0) { throw "docker pull $cudaImage failed." }
}
Write-Host "Checking the GPU..."
$gpu = Invoke-Native docker @("run", "--rm", "--gpus", "all", $cudaImage, "nvidia-smi")
$gpu.Output | ForEach-Object { Write-Host $_ }
if ($gpu.ExitCode -ne 0) { throw "GPU passthrough check failed." }

Write-Host "Building images (the first build takes a while)..."
$buildArgs = @("compose", "build")
if ($Pull) { $buildArgs += "--pull" }
if ((Invoke-NativeLive docker $buildArgs) -ne 0) { throw "docker compose build failed." }

# Keys Windows PowerShell cannot make itself (an EC key in PEM form, a Fernet
# key) come from a short-lived platform container.
$missing = @("DEPLOYER_SECRETS_KEY", "POMERIUM_SHARED_SECRET", "POMERIUM_COOKIE_SECRET", "POMERIUM_SIGNING_KEY") |
    Where-Object { -not (Get-EnvValue $_) }
if ($missing) {
    $generated = Invoke-Native docker (@("run", "--rm", "--network", "none", "hertogs/platform:local",
        "python", "-m", "hertogs_platform.setup_secrets") + $missing)
    if ($generated.ExitCode -ne 0) { throw "Generating secrets failed: $($generated.Output -join ' ')" }
    foreach ($line in $generated.Output) {
        if ($line -match '^([A-Z_]+)=(\S+)$' -and $missing -contains $Matches[1]) {
            Set-EnvValue $Matches[1] $Matches[2]
            Write-Host "Generated $($Matches[1]) in .env."
        }
    }
}

$hasOwner = [bool](Get-EnvValue "PLATFORM_OWNER_EMAIL")
if (-not $hasOwner) {
    Write-Warning ("PLATFORM_OWNER_EMAIL is empty, so only the workspace, the files service and the tunnel " +
        "start; the app platform waits. Set it (your Google address) in .env, or fill in " +
        "CF_ACCESS_ALLOWED_EMAILS, and run this script again.")
}

if ($BuildOnly) {
    Write-Host "Images built. Nothing was started or stopped."
    return
}

if (-not (Get-EnvValue "ADMIN_CF_ACCESS_AUD")) {
    Write-Warning "ADMIN_CF_ACCESS_AUD is empty: the admin site stays locked (503) until you create its Access application (docs\SETUP.md, section 5)."
}
if (-not (Get-EnvValue "GOOGLE_CLIENT_ID") -or -not (Get-EnvValue "GOOGLE_CLIENT_SECRET")) {
    Write-Warning "GOOGLE_CLIENT_ID/SECRET are empty: signing in to hosted apps fails until they are set (docs\SETUP.md, section 5)."
}

Write-Host "Starting the stack and waiting until it is healthy..."
$upArgs = @("compose", "up", "-d", "--wait", "--wait-timeout", "300")
if (-not $hasOwner) { $upArgs += @("workspace", "files", "cloudflared") }
if ((Invoke-NativeLive docker $upArgs) -ne 0) {
    Write-Warning "Not every container became healthy within 5 minutes. Check: docker compose ps; docker compose logs"
}

Invoke-NativeLive docker @("compose", "ps") | Out-Null
Write-Host ""
Write-Host "Run .\scripts\test-local.ps1 to check health and the GPU."
