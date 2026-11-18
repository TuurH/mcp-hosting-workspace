<#
.SYNOPSIS
    Gaming mode: stops GPU work in the workspace and lowers its CPU and memory
    limits so games get the PC. Off restores the normal limits.
.DESCRIPTION
    .\scripts\gaming.ps1 on          turn gaming mode on now
    .\scripts\gaming.ps1 off         turn it off again
    .\scripts\gaming.ps1             show the state and the last log lines
    .\scripts\gaming.ps1 -Watch      keep running: turn gaming mode ON when one of
                                     the game programs listed on the admin page
                                     (Settings, Gaming mode) starts
    .\scripts\gaming.ps1 -Install    start the watcher automatically at logon

    The watcher only ever turns gaming mode on, and only when a game starts, so
    turning it off while still playing is respected. The limits are edited on
    the admin page.
#>
param(
    [ValidateSet("on", "off", "status")] [string] $Action = "status",
    [switch] $Watch,
    [switch] $Install,
    [int] $IntervalSeconds = 20
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "lib\common.ps1")
Set-Location (Get-ProjectRoot)

function Invoke-DeployerCli {
    param([string[]] $CliArgs)
    return Invoke-Native docker (@("exec", "hertogs-deployer", "python", "-m", "hertogs_platform.deployer.cli") + $CliArgs)
}

function Get-GamingState {
    $result = Invoke-DeployerCli @("gaming", "status")
    if ($result.ExitCode -ne 0) { return $null }
    return (($result.Output -join "`n") | ConvertFrom-Json)
}

if ($Install) {
    $scriptPath = Join-Path $PSScriptRoot "gaming.ps1"
    $shellPath = Get-PowerShellHost
    $action = New-ScheduledTaskAction -Execute $shellPath `
        -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`" -Watch" `
        -WorkingDirectory (Get-ProjectRoot)
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    # No time limit: the watcher runs for the whole session.
    $taskSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName "Hertogs gaming watcher" -Action $action -Trigger $trigger `
        -Settings $taskSettings -Description "Turns Hertogs gaming mode on when a listed game starts" -Force | Out-Null
    Write-Host "The gaming watcher starts at every logon (using $shellPath)."
    Write-Host "List your games on the admin page: Settings, Gaming mode (for example: eldenring.exe)."
    Start-ScheduledTask -TaskName "Hertogs gaming watcher"
    return
}

if ($Watch) {
    $games = @()
    $refreshed = [datetime]::MinValue
    $wasRunning = $false
    Write-Host "Watching for games every $IntervalSeconds seconds (Ctrl+C to stop)."
    while ($true) {
        try {
            if (((Get-Date) - $refreshed).TotalMinutes -ge 5) {
                $list = Invoke-DeployerCli @("games")
                if ($list.ExitCode -eq 0) {
                    $games = @($list.Output | ForEach-Object { [System.IO.Path]::GetFileNameWithoutExtension($_.Trim()) } |
                        Where-Object { $_ })
                    $refreshed = Get-Date
                }
            }
            $running = $false
            if ($games.Count -gt 0) {
                $running = @(Get-Process -Name $games -ErrorAction SilentlyContinue).Count -gt 0
            }
            $handled = $true
            if ($running -and -not $wasRunning) {
                $state = Get-GamingState
                if ($null -eq $state) { $handled = $false }  # Docker or the deployer not ready: retry
                elseif ($state.gaming -eq "off") {
                    Write-Host "$(Get-Date -Format 'HH:mm:ss') a game started: turning gaming mode on"
                    $on = Invoke-DeployerCli @("gaming", "on", "--source", "auto")
                    if ($on.ExitCode -ne 0) {
                        Write-Warning ($on.Output -join " ")
                        $handled = $false  # try again next round
                    }
                }
            }
            # Only a game that starts triggers; turning it off while playing is respected.
            if ($handled) { $wasRunning = $running }
        }
        catch {
            Write-Warning "$_"
        }
        Start-Sleep -Seconds $IntervalSeconds
    }
}

Assert-DockerRunning
switch ($Action) {
    "on" { $result = Invoke-DeployerCli @("gaming", "on") }
    "off" { $result = Invoke-DeployerCli @("gaming", "off") }
    default { $result = Invoke-DeployerCli @("gaming", "status") }
}
$result.Output | ForEach-Object { Write-Host $_ }
if ($result.ExitCode -ne 0) { throw "The deployer did not accept the command (is the stack running?)." }
