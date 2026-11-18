# Exercise fresh setup without Docker, GPU hardware or real credentials.
# Runs on Windows PowerShell 5.1 and PowerShell 7.
$ErrorActionPreference = "Stop"
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("hertogs-setup-test-" + [guid]::NewGuid().ToString("N"))
$originalLocation = Get-Location
$testState = @{
    Calls = New-Object 'System.Collections.Generic.List[string]'
    FailBuilderPull = $false
}

# Native helpers call this fake Docker command; no real daemon is contacted.
function docker {
    $command = $args -join " "
    $testState.Calls.Add($command)
    $global:LASTEXITCODE = 0
    switch -Regex ($command) {
        '^info ' { "test-engine"; return }
        '^compose --profile tools pull builder-image$' {
            if ($testState.FailBuilderPull) { $global:LASTEXITCODE = 1 }
            return
        }
        '^image ls --quiet ' { return } # Empty image cache on a fresh install.
        '^pull nvidia/cuda:' { return }
        '^run --rm --gpus all .* nvidia-smi$' { "test GPU"; return }
        '^compose build$' { return }
        '^run --rm --network none hertogs/platform:local python -m hertogs_platform.setup_secrets ' {
            foreach ($name in @("DEPLOYER_SECRETS_KEY", "POMERIUM_SHARED_SECRET", "POMERIUM_COOKIE_SECRET", "POMERIUM_SIGNING_KEY")) {
                "$name=test-only-value"
            }
            return
        }
        '^compose up ' { return }
        '^compose ps$' { return }
        default { throw "Unexpected Docker command: $command" }
    }
}

function Check([string] $Label, [bool] $Condition) {
    if (-not $Condition) { throw "FAIL  $Label" }
    Write-Host "ok    $Label"
}

try {
    $scripts = Join-Path $tmp "scripts"
    $lib = Join-Path $scripts "lib"
    New-Item -ItemType Directory -Path $lib -Force | Out-Null
    Copy-Item (Join-Path $repo "scripts/setup.ps1") (Join-Path $scripts "setup.ps1")
    Copy-Item (Join-Path $repo "scripts/lib/common.ps1") (Join-Path $lib "common.ps1")
    Copy-Item (Join-Path $repo ".env.example") (Join-Path $tmp ".env.example")
    $setup = Join-Path $scripts "setup.ps1"
    $envFile = Join-Path $tmp ".env"

    & $setup
    Check "fresh setup creates its own environment" (Test-Path -LiteralPath $envFile)
    $pullIndex = $testState.Calls.IndexOf("compose --profile tools pull builder-image")
    $buildIndex = $testState.Calls.IndexOf("compose build")
    Check "fresh setup installs the builder before building and starting" ($pullIndex -ge 0 -and $buildIndex -gt $pullIndex -and @($testState.Calls | Where-Object { $_ -like "compose up *" }).Count -eq 1)

    [System.IO.File]::Delete($envFile)
    $testState.Calls.Clear()
    & $setup -BuildOnly
    Check "BuildOnly installs the builder on a fresh install" ($testState.Calls.Contains("compose --profile tools pull builder-image"))
    Check "BuildOnly does not start services" (@($testState.Calls | Where-Object { $_ -like "compose up *" }).Count -eq 0)

    $testState.Calls.Clear()
    $customCudaImage = "nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04"
    $savedEnv = [System.IO.File]::ReadAllText($envFile)
    [System.IO.File]::WriteAllText($envFile, $savedEnv.Replace("nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04", $customCudaImage))
    & $setup -BuildOnly
    Check "GPU probe uses the configured CUDA image" ($testState.Calls.Contains("run --rm --gpus all $customCudaImage nvidia-smi"))

    [System.IO.File]::Delete($envFile)
    $testState.Calls.Clear()
    $testState.FailBuilderPull = $true
    $failure = $null
    try { & $setup -BuildOnly } catch { $failure = $_ }
    Check "failed builder download reports the missing prerequisite" ($null -ne $failure -and $failure.Exception.Message -like "*BuildKit*")
    Check "failed builder download prevents image builds and startup" (-not $testState.Calls.Contains("compose build") -and @($testState.Calls | Where-Object { $_ -like "compose up *" }).Count -eq 0)
}
finally {
    Set-Location $originalLocation
    if (Test-Path -LiteralPath $tmp) {
        $resolved = (Resolve-Path -LiteralPath $tmp).Path
        if ($resolved -ne [System.IO.Path]::GetFullPath($tmp)) { throw "Refusing to remove an unexpected test directory." }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
Write-Host "All fresh-install setup tests passed."
# The intentional failed Docker mock above leaves LASTEXITCODE=1. Both GitHub
# PowerShell shells propagate it even though every assertion passed.
$global:LASTEXITCODE = 0
