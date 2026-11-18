# Unit tests for scripts/lib/common.ps1. Plain PowerShell (no Pester) so it runs
# unchanged in Windows PowerShell 5.1 and PowerShell 7. Exits 1 on failure.
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "..\..\scripts\lib\common.ps1")

$failures = 0
function Check([string] $Label, [bool] $Condition) {
    if ($Condition) { Write-Host "ok    $Label" }
    else { Write-Host "FAIL  $Label"; $script:failures++ }
}

# Point the helpers at a temporary .env (never the real one).
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("mcp-hosting-test-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
$script:EnvFile = Join-Path $tmp ".env"

# --- Get/Set-EnvValue --------------------------------------------------------
[System.IO.File]::WriteAllText($script:EnvFile, ([char]0xFEFF + "A=1`r`nEMPTY=`r`nQUOTED='x y'`r`nCOMMENT=v # note`r`nQC='a # b' # note`r`n"))
Check "BOM is ignored and plain values read" ((Get-EnvValue "A") -eq "1")
Check "empty value is empty string" ((Get-EnvValue "EMPTY") -eq "")
Check "absent key is null" ($null -eq (Get-EnvValue "MISSING"))
Check "quoted value is unquoted" ((Get-EnvValue "QUOTED") -eq "x y")
Check "inline comment is stripped" ((Get-EnvValue "COMMENT") -eq "v")
Check "quoted value followed by a comment" ((Get-EnvValue "QC") -eq "a # b")

Set-EnvValue "A" "2"
Set-EnvValue "NEW" "D:\My Backups"
Check "update existing key" ((Get-EnvValue "A") -eq "2")
Check "value with spaces round-trips" ((Get-EnvValue "NEW") -eq "D:\My Backups")
$bytes = [System.IO.File]::ReadAllBytes($script:EnvFile)
Check "written without BOM" (-not ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB))
Check "other keys kept" ((Get-EnvValue "QUOTED") -eq "x y")
$refused = $false
try { Set-EnvValue "BACKUP_DIR" "D:\Jan's Backups" } catch { $refused = $true }
Check "apostrophes are refused, not silently dropped" ($refused -and $null -eq (Get-EnvValue "BACKUP_DIR"))

# --- Add-MissingEnvKeys ----------------------------------------------------------
[System.IO.File]::WriteAllText($script:EnvFile, "CLOUDFLARE_TUNNEL_TOKEN=abc`r`nBACKUP_WORKSPACE_FOLDERS=`r`n")
$added = Add-MissingEnvKeys
Check "missing keys are added" ($added -contains "BACKUP_KEEP_DAILY")
Check "existing keys are not overwritten" ((Get-EnvValue "CLOUDFLARE_TUNNEL_TOKEN") -eq "abc")
Check "existing empty value stays empty" ((Get-EnvValue "BACKUP_WORKSPACE_FOLDERS") -eq "")

# --- Retention -----------------------------------------------------------------
$names = @(
    "mcp-hosting-20261005-033000", "mcp-hosting-20261005-120000",   # two on the same day
    "mcp-hosting-20261004-033000", "mcp-hosting-20261003-033000", "mcp-hosting-20261002-033000",
    "mcp-hosting-20261001-033000", "mcp-hosting-20260930-033000", "mcp-hosting-20260929-033000",
    "mcp-hosting-20260928-033000", "mcp-hosting-20260921-033000", "mcp-hosting-20260914-033000",
    "mcp-hosting-20260907-033000", "mcp-hosting-20260831-033000", "mcp-hosting-20260824-033000",
    "not-a-backup"
)
$keep = Select-BackupsToKeep -Names $names -Daily 7 -Weekly 4
Check "newest of each of 7 days kept" (($keep | Where-Object { $_ -ge "mcp-hosting-20260929" }).Count -eq 7)
Check "only newest per day" (($keep -contains "mcp-hosting-20261005-120000") -and -not ($keep -contains "mcp-hosting-20261005-033000"))
Check "4 weekly backups older than the dailies" (($keep | Where-Object { $_ -lt "mcp-hosting-20260929" }).Count -eq 4)
Check "oldest weekly dropped" (-not ($keep -contains "mcp-hosting-20260824-033000"))
Check "non-backup names ignored" (-not ($keep -contains "not-a-backup"))
Check "zero weekly keeps only dailies" ((Select-BackupsToKeep -Names $names -Daily 2 -Weekly 0).Count -eq 2)

# --- ISO weeks across a year boundary ---------------------------------------------
Check "2026-12-31 is 2026-W53" ((Get-IsoWeekKey ([datetime]"2026-12-31")) -eq "2026-W53")
Check "2027-01-01 is 2026-W53" ((Get-IsoWeekKey ([datetime]"2027-01-01")) -eq "2026-W53")
Check "2025-12-29 is 2026-W01" ((Get-IsoWeekKey ([datetime]"2025-12-29")) -eq "2026-W01")

# --- Dynamic app containers must not survive a restore -------------------------
$script:removeCalls = New-Object 'System.Collections.Generic.List[string]'
$script:listCalls = 0
function Invoke-Native {
    param([string] $File, [string[]] $Arguments)
    $script:removeCalls.Add(($Arguments -join " "))
    if ($Arguments[0] -eq "ps") {
        $script:listCalls++
        $output = if ($script:listCalls -eq 1) { @("worker-id", "gateway-id") } else { @() }
        return [pscustomobject]@{ ExitCode = 0; Output = $output }
    }
    return [pscustomobject]@{ ExitCode = 0; Output = @() }
}
Remove-ManagedAppContainers
Check "restore finds dynamic containers by platform label" ($script:removeCalls[0] -eq "ps -aq --filter label=mcp-hosting-workspace.app")
Check "restore removes workers and helpers before replacing data" (($script:removeCalls -contains "container rm --force worker-id") -and ($script:removeCalls -contains "container rm --force gateway-id"))
Check "restore verifies no app containers remain" ($script:listCalls -eq 2)
function Invoke-Native { param([string] $File, [string[]] $Arguments); return [pscustomobject]@{ExitCode=1;Output=@()} }
$refused = $false
try { Remove-ManagedAppContainers } catch { $refused = $true }
Check "restore aborts when managed containers cannot be listed" $refused

Remove-Item -LiteralPath $tmp -Recurse -Force
if ($failures -gt 0) { Write-Host "$failures failure(s)"; exit 1 }
Write-Host "All PowerShell helper tests passed."
