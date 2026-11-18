<#
.SYNOPSIS
    Backs up the platform data to a folder you choose. The 100+ GB workspace
    volume is NOT included, only the workspace folders listed in
    BACKUP_WORKSPACE_FOLDERS (default: apps, projects).

.DESCRIPTION
    Included:
      - every Docker volume labelled be.hertogs.backup=true (platform state;
        later also app data, static sites and approved app images)
      - the workspace folders from BACKUP_WORKSPACE_FOLDERS, without
        node_modules, .venv, __pycache__ and .cache
    Before copying, running containers labelled be.hertogs.backup.pre get that
    command executed (consistent database snapshots).

    Keeps the newest backup of each of the last BACKUP_KEEP_DAILY days and of
    each of the BACKUP_KEEP_WEEKLY weeks before that. The result of every run
    is written to last-backup.json in the backup folder and in
    %LOCALAPPDATA%\HertogsBackup (which also holds the logs of scheduled runs,
    so a failure is recorded even when the backup folder cannot be reached).

    Keep your .env in a password manager: encrypted data in the backup cannot
    be restored without it.

.PARAMETER ChooseFolder
    Pick (or change) the backup folder. The choice is saved in .env as BACKUP_DIR.
.PARAMETER Install
    Register a daily scheduled task that runs this script (while you are logged in).
.PARAMETER At
    Time of day for the scheduled task, default 03:30.
.PARAMETER Scheduled
    Used by the scheduled task: never show dialogs, wait for Docker Desktop, and
    write a log file.
#>
param(
    [switch] $ChooseFolder,
    [switch] $Install,
    [string] $At = "03:30",
    [switch] $Scheduled
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "lib\common.ps1")
Set-Location (Get-ProjectRoot)

# Status and logs always go to a local folder too, so a run that fails before
# the backup folder is reachable (NAS offline, drive unplugged) is still recorded.
$localDir = Join-Path $env:LOCALAPPDATA "HertogsBackup"
New-Item -ItemType Directory -Force -Path (Join-Path $localDir "logs") | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$transcript = $false
if ($Scheduled) {
    Start-Transcript -Path (Join-Path $localDir "logs\backup-$stamp.log") | Out-Null
    $transcript = $true
    Get-ChildItem -LiteralPath (Join-Path $localDir "logs") -Filter "backup-*.log" |
        Sort-Object Name -Descending | Select-Object -Skip 60 |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
}
$status = [ordered]@{ time = (Get-Date).ToString("o"); status = "error"; folder = $null; error = $null }
$statusFile = $null

try {
    $HelperImage = "hertogs/platform:local"
    $WorkspaceVolume = "hertogs-ai-workspace-data"

    # --- Backup folder -----------------------------------------------------------
    $backupDir = Get-EnvValue "BACKUP_DIR"
    if ($ChooseFolder -or -not $backupDir) {
        if ($Scheduled) { throw "BACKUP_DIR is not set in .env. Run .\scripts\backup.ps1 -ChooseFolder once." }
        $picked = Select-Folder "Choose where Hertogs backups are stored"
        if (-not $picked) { throw "No folder chosen." }
        if ($picked -match "['""]") { throw "Choose a folder whose path has no quote characters." }
        Set-EnvValue "BACKUP_DIR" $picked
        $backupDir = $picked
        Write-Host "Backup folder saved in .env: $backupDir"
    }
    $backupDir = [System.IO.Path]::GetFullPath($backupDir)
    if (-not (Test-Path -LiteralPath $backupDir)) {
        New-Item -ItemType Directory -Path $backupDir | Out-Null
    }
    $statusFile = Join-Path $backupDir "last-backup.json"

    # --- Scheduled task ----------------------------------------------------------
    if ($Install) {
        $scriptPath = Join-Path $PSScriptRoot "backup.ps1"
        $shellPath = Get-PowerShellHost
        $action = New-ScheduledTaskAction -Execute $shellPath `
            -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`" -Scheduled" `
            -WorkingDirectory (Get-ProjectRoot)
        $trigger = New-ScheduledTaskTrigger -Daily -At $At
        $taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
            -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 15)
        Register-ScheduledTask -TaskName "Hertogs backup" -Action $action -Trigger $trigger `
            -Settings $taskSettings -Description "Backs up Hertogs platform data to $backupDir" -Force | Out-Null
        Write-Host "Scheduled 'Hertogs backup' daily at $At using $shellPath."
        Write-Host "It runs while you are logged in (Docker Desktop must be running). Logs: $localDir\logs"
        $status.status = "installed"
        return
    }

    # --- Backup ------------------------------------------------------------------
    # Docker Desktop cannot mount network shares, so those go through a local folder first.
    $isNetwork = Test-NetworkPath $backupDir
    $workRoot = if ($isNetwork) { Join-Path $env:LOCALAPPDATA "HertogsBackupStaging" } else { $backupDir }
    $work = Join-Path $workRoot "hertogs-$stamp.partial"
    $final = Join-Path $backupDir "hertogs-$stamp"
    $warnings = New-Object 'System.Collections.Generic.List[string]'

    function Invoke-Helper {
        # Runs one of the platform image's helper scripts with plain arguments.
        param([string[]] $Mounts, [string] $Script, [string[]] $ScriptArgs = @())
        $dockerArgs = @("run", "--rm", "--network", "none", "--user", "0:0", "--entrypoint", "/opt/platform/bin/$Script")
        foreach ($mount in $Mounts) { $dockerArgs += @("-v", $mount) }
        $dockerArgs += $HelperImage
        $dockerArgs += $ScriptArgs
        return (Invoke-Native docker $dockerArgs)
    }

    function Assert-Archive {
        param([string] $Path)
        $item = Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue
        if ($null -eq $item -or $item.Length -le 0) { throw "Expected archive $Path was not written." }
    }

    try {
        if ($Scheduled) { Wait-DockerRunning -Minutes 10 } else { Assert-DockerRunning }
        if (-not (Test-DockerImage $HelperImage)) { throw "Image $HelperImage not found. Run 'docker compose build' first." }
        New-Item -ItemType Directory -Force -Path $work | Out-Null

        # Consistent snapshots first (e.g. SQLite databases).
        $running = Invoke-Native docker @("ps", "--filter", "label=be.hertogs.backup.pre", "--format", "{{.Names}}")
        foreach ($container in @($running.Output | Where-Object { $_ })) {
            $state = (Invoke-Native docker @("inspect", "--format", "{{.State.Status}}", $container)).Output -join ""
            if ($state -ne "running") {
                $warnings.Add("$container is $state, so its snapshot was skipped; its last snapshot is copied instead")
                continue
            }
            $labels = (Invoke-Native docker @("inspect", "--format", "{{json .Config.Labels}}", $container)).Output -join ""
            $command = (ConvertFrom-Json $labels)."be.hertogs.backup.pre"
            if (-not $command) { continue }
            Write-Host "Snapshot in $container ..."
            $result = Invoke-Native docker (@("exec", $container) + ($command -split '\s+'))
            # A failed snapshot must not cost the whole backup: the volumes are
            # still copied, with the previous snapshot inside.
            if ($result.ExitCode -ne 0) { $warnings.Add("snapshot in $container failed: $($result.Output -join ' ')") }
        }

        $volumes = @((Invoke-Native docker @("volume", "ls", "--quiet", "--filter", "label=be.hertogs.backup=true")).Output |
            Where-Object { $_ })
        foreach ($volume in $volumes) {
            Write-Host "Volume $volume ..."
            $archive = "volume-$volume.tar.gz"
            $result = Invoke-Helper -Mounts @("${volume}:/data:ro", "${work}:/backup") -Script "backup-volume.sh" -ScriptArgs @($archive)
            if ($result.ExitCode -eq 1) { $warnings.Add("$volume changed while it was copied") }
            elseif ($result.ExitCode -ne 0) { throw "Backing up volume $volume failed: $($result.Output -join ' ')" }
            Assert-Archive (Join-Path $work $archive)
        }

        $rawFolders = Get-EnvValue "BACKUP_WORKSPACE_FOLDERS"
        if ($null -eq $rawFolders) { $rawFolders = "apps,projects" }
        $folders = @($rawFolders -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        foreach ($folder in $folders) {
            if ($folder -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
                throw "BACKUP_WORKSPACE_FOLDERS may only contain top-level folder names; got '$folder'"
            }
        }
        if ($folders.Count -gt 0 -and -not (Test-DockerVolume $WorkspaceVolume)) {
            $warnings.Add("the workspace volume $WorkspaceVolume does not exist, so no workspace folders were copied")
        }
        elseif ($folders.Count -gt 0) {
            Write-Host "Workspace folders: $($folders -join ', ') ..."
            $result = Invoke-Helper -Mounts @("${WorkspaceVolume}:/data:ro", "${work}:/backup") -Script "backup-folders.sh" -ScriptArgs $folders
            switch ($result.ExitCode) {
                0 { Assert-Archive (Join-Path $work "workspace-folders.tar.gz") }
                1 { $warnings.Add("some workspace files changed while they were copied"); Assert-Archive (Join-Path $work "workspace-folders.tar.gz") }
                3 { $warnings.Add("none of the workspace folders exist yet: $($folders -join ', ')") }
                default { throw "Backing up workspace folders failed: $($result.Output -join ' ')" }
            }
        }

        # Keep immutable runtime images alongside source and data volumes.
        if (@($running.Output) -contains "hertogs-deployer") {
            $runtimeResult = Invoke-Native docker @("exec", "hertogs-deployer", "python", "-m", "hertogs_platform.deployer.cli", "runtime-images")
            if ($runtimeResult.ExitCode -ne 0) { throw "Could not enumerate runtime images for this backup." }
            $runtimeImages = @($runtimeResult.Output | Where-Object { $_ -match '^sha256:[a-f0-9]{64}$' })
            if ($runtimeImages.Count -gt 0) {
                $saved = Invoke-Native docker (@("image", "save", "--output", (Join-Path $work "runtime-images.tar")) + $runtimeImages)
                if ($saved.ExitCode -ne 0) { throw "Saving runtime images failed: $($saved.Output -join ' ')" }
            }
        }
        $files = @(Get-ChildItem -LiteralPath $work -File)
        if ($files.Count -eq 0) {
            # Nothing to keep: do not let an empty run push real backups out of retention.
            $status.status = "empty"
            $status.warnings = @($warnings)
            Remove-Item -LiteralPath $work -Recurse -Force
            Write-Warning "Nothing was backed up: no labelled volumes and no workspace folders."
            foreach ($warning in $warnings) { Write-Warning $warning }
            return
        }
        $manifest = [ordered]@{
            created = (Get-Date).ToString("o")
            volumes = $volumes
            workspace_folders = $folders
            warnings = @($warnings)
            files = @($files | ForEach-Object { [ordered]@{ name = $_.Name; bytes = $_.Length; sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() } })
        }
        [System.IO.File]::WriteAllText((Join-Path $work "manifest.json"), ($manifest | ConvertTo-Json -Depth 4), $script:Utf8NoBom)

        if ($isNetwork) {
            # Copy under a .partial name, manifest last, check sizes, then rename:
            # an interrupted copy never looks like a complete backup.
            $remote = "$final.partial"
            New-Item -ItemType Directory -Force -Path $remote | Out-Null
            foreach ($file in $files) { Copy-Item -LiteralPath $file.FullName -Destination $remote }
            foreach ($file in $files) {
                $copied = Get-Item -LiteralPath (Join-Path $remote $file.Name)
                if ($copied.Length -ne $file.Length -or (Get-FileHash -LiteralPath $copied.FullName -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash) { throw "Copy of $($file.Name) to $remote is incomplete." }
            }
            Copy-Item -LiteralPath (Join-Path $work "manifest.json") -Destination $remote
            Rename-Item -LiteralPath $remote -NewName (Split-Path $final -Leaf)
            Remove-Item -LiteralPath $work -Recurse -Force
        }
        else {
            Rename-Item -LiteralPath $work -NewName (Split-Path $final -Leaf)
        }

        $total = ($files | Measure-Object -Property Length -Sum).Sum
        Write-Host ("Backup written to {0} ({1:N1} MB)" -f $final, ($total / 1MB))
        foreach ($warning in $warnings) { Write-Warning $warning }
        $status.status = if ($warnings.Count -gt 0) { "ok-with-warnings" } else { "ok" }
        $status.folder = $final
        $status.bytes = $total
        $status.warnings = @($warnings)

        # Retention (only complete backups count).
        $rawDaily = Get-EnvValue "BACKUP_KEEP_DAILY"
        $keepDaily = if ($rawDaily -match '^\d+$' -and [int]$rawDaily -gt 0) { [int]$rawDaily } else { 7 }
        $rawWeekly = Get-EnvValue "BACKUP_KEEP_WEEKLY"
        $keepWeekly = if ($rawWeekly -match '^\d+$') { [int]$rawWeekly } else { 4 }
        $complete = @(Get-ChildItem -LiteralPath $backupDir -Directory |
            Where-Object { $_.Name -match '^hertogs-\d{8}-\d{6}$' -and (Test-Path -LiteralPath (Join-Path $_.FullName "manifest.json")) })
        $keep = Select-BackupsToKeep -Names @($complete | ForEach-Object Name) -Daily $keepDaily -Weekly $keepWeekly
        foreach ($backup in $complete) {
            if ($keep -notcontains $backup.Name) {
                Write-Host "Removing old backup $($backup.Name)"
                Remove-Item -LiteralPath $backup.FullName -Recurse -Force
            }
        }
        # Leftovers from interrupted runs.
        foreach ($root in @($backupDir, $workRoot) | Select-Object -Unique) {
            if (-not (Test-Path -LiteralPath $root)) { continue }
            Get-ChildItem -LiteralPath $root -Directory -Filter "hertogs-*.partial" |
                Where-Object { $_.FullName -ne $work -and $_.LastWriteTime -lt (Get-Date).AddDays(-1) } |
                ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
        }
    }
    catch {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
        if ($isNetwork) { Remove-Item -LiteralPath "$final.partial" -Recurse -Force -ErrorAction SilentlyContinue }
        throw
    }
}
catch {
    $status.error = "$_"
    if ($status.status -eq "ok" -or $status.status -eq "ok-with-warnings") { $status.status = "ok-with-errors" }
    throw
}
finally {
    if ($status.status -ne "installed") {
        $json = $status | ConvertTo-Json
        foreach ($target in @((Join-Path $localDir "last-backup.json"), $statusFile)) {
            if (-not $target) { continue }
            try { [System.IO.File]::WriteAllText($target, $json, $script:Utf8NoBom) }
            catch { Write-Warning "Could not write $target : $_" }
        }
    }
    if ($transcript) { Stop-Transcript | Out-Null }
}
