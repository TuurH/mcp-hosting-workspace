<#
.SYNOPSIS
    Restores a backup made by backup.ps1.
.DESCRIPTION
    Checks every archive first, then stops the stack, replaces the contents of
    every volume found in the backup (each archive is extracted into a
    temporary volume first, so a broken archive never leaves a half-emptied
    volume), optionally restores the workspace folders, and starts the stack
    again. Your .env must be the one that was used when the backup was made.
.PARAMETER Backup
    Path of a hertogs-YYYYMMDD-HHMMSS folder. Without it you pick from a list.
.PARAMETER IncludeWorkspaceFolders
    Also extract the saved workspace folders (apps, projects, ...) over the
    current ones. Files added since the backup are kept.
.PARAMETER Force
    Do not ask for confirmation.
#>
param(
    [string] $Backup,
    [switch] $IncludeWorkspaceFolders,
    [switch] $Force
)

$ErrorActionPreference = "Stop"
# ProviderPath: a plain C:\... or \\server\share path, never "Microsoft.PowerShell.Core\FileSystem::...".
if ($Backup) { $Backup = (Resolve-Path -LiteralPath $Backup).ProviderPath }
. (Join-Path $PSScriptRoot "lib\common.ps1")
Set-Location (Get-ProjectRoot)

$HelperImage = "hertogs/platform:local"
$WorkspaceVolume = "hertogs-ai-workspace-data"

Assert-DockerRunning
if (-not (Test-DockerImage $HelperImage)) { throw "Image $HelperImage not found. Run 'docker compose build' first." }

if (-not $Backup) {
    $backupDir = Get-EnvValue "BACKUP_DIR"
    if (-not $backupDir -or -not (Test-Path -LiteralPath $backupDir)) {
        throw "BACKUP_DIR is not set or missing. Pass -Backup <folder>."
    }
    $list = @(Get-ChildItem -LiteralPath $backupDir -Directory |
        Where-Object { $_.Name -match '^hertogs-\d{8}-\d{6}$' -and (Test-Path -LiteralPath (Join-Path $_.FullName "manifest.json")) } |
        Sort-Object Name -Descending)
    if ($list.Count -eq 0) { throw "No backups found in $backupDir" }
    for ($i = 0; $i -lt $list.Count; $i++) { Write-Host ("[{0}] {1}" -f ($i + 1), $list[$i].Name) }
    $choice = [int](Read-Host "Number of the backup to restore")
    if ($choice -lt 1 -or $choice -gt $list.Count) { throw "Invalid choice." }
    $Backup = $list[$choice - 1].FullName
}

if (Test-NetworkPath $Backup) {
    throw "Copy the backup folder to a local drive first; Docker Desktop cannot read network folders."
}
$manifestPath = Join-Path $Backup "manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath)) {
    throw "$Backup does not look like a complete backup (manifest.json missing)."
}
$manifest = [System.IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json

function Invoke-Helper {
    param([string[]] $Mounts, [string] $Script, [string[]] $ScriptArgs = @())
    $dockerArgs = @("run", "--rm", "--network", "none", "--user", "0:0", "--entrypoint", "/opt/platform/bin/$Script")
    foreach ($mount in $Mounts) { $dockerArgs += @("-v", $mount) }
    $dockerArgs += $HelperImage
    $dockerArgs += $ScriptArgs
    return (Invoke-Native docker $dockerArgs)
}

# --- Check every archive before touching anything ---------------------------
$volumeFiles = @(Get-ChildItem -LiteralPath $Backup -Filter "volume-*.tar.gz" -File)
$volumes = foreach ($file in $volumeFiles) {
    $name = $file.Name.Substring(7, $file.Name.Length - 7 - 7)
    if ($name -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]+$') { throw "Unexpected file name $($file.Name)" }
    [pscustomobject]@{ Volume = $name; File = $file.Name }
}
$workspaceArchive = "workspace-folders.tar.gz"
$hasFolders = Test-Path -LiteralPath (Join-Path $Backup $workspaceArchive)

# Everything the manifest lists must be there: a partly copied backup is refused.
foreach ($entry in @($manifest.files)) {
    if ($entry.name -notmatch '^[a-zA-Z0-9][a-zA-Z0-9._-]*$') { throw "Invalid filename in backup manifest." }
    if ($entry.PSObject.Properties["sha256"] -and $entry.sha256) {
        $entryPath = Join-Path $Backup $entry.name
        if ((Get-FileHash -LiteralPath $entryPath -Algorithm SHA256).Hash -ne $entry.sha256) {
            throw "$($entry.name) failed SHA256 verification."
        }
    }
    if ($entry.name -ne "manifest.json" -and -not (Test-Path -LiteralPath (Join-Path $Backup $entry.name))) {
        throw "The backup is incomplete: $($entry.name) is missing."
    }
}
foreach ($name in @($manifest.volumes)) {
    if ($name -and -not (Test-Path -LiteralPath (Join-Path $Backup "volume-$name.tar.gz"))) {
        throw "The backup is incomplete: volume-$name.tar.gz is missing."
    }
}
if ($IncludeWorkspaceFolders -and -not $hasFolders) {
    throw "This backup has no workspace folders ($workspaceArchive)."
}
$restoreFolders = $IncludeWorkspaceFolders -and $hasFolders
if (@($volumes).Count -eq 0 -and -not $restoreFolders) {
    if ($hasFolders) { throw "Nothing to restore: this backup only contains workspace folders. Add -IncludeWorkspaceFolders." }
    throw "Nothing to restore: the backup contains no archives."
}
$toCheck = @($volumes | ForEach-Object File)
if ($restoreFolders) { $toCheck += $workspaceArchive }

foreach ($archive in $toCheck) {
    $expected = @($manifest.files | Where-Object { $_.name -eq $archive })
    $actual = (Get-Item -LiteralPath (Join-Path $Backup $archive)).Length
    if ($expected.Count -eq 1 -and $expected[0].PSObject.Properties["sha256"] -and $expected[0].sha256) {
        $hash = (Get-FileHash -LiteralPath (Join-Path $Backup $archive) -Algorithm SHA256).Hash
        if ($hash -ne $expected[0].sha256) { throw "$archive failed SHA256 verification. The backup is damaged." }
    }
    if ($expected.Count -eq 1 -and [int64]$expected[0].bytes -ne $actual) {
        throw "$archive has the wrong size ($actual bytes, manifest says $($expected[0].bytes)). The backup is damaged."
    }
    $result = Invoke-Helper -Mounts @("${Backup}:/backup:ro") -Script "verify-archive.sh" -ScriptArgs @($archive)
    if ($result.ExitCode -ne 0) { throw "$archive is damaged: $($result.Output -join ' ')" }
}
Write-Host "All archives checked."

Write-Host "Backup: $Backup"
Write-Host "Volumes to replace: $(@($volumes | ForEach-Object Volume) -join ', ')"
if ($restoreFolders) { Write-Host "Workspace folders will be extracted over the current ones." }
if (-not $Force) {
    $answer = Read-Host "This REPLACES the current data in those volumes. Type 'restore' to continue"
    if ($answer -ne "restore") { Write-Host "Cancelled."; return }
}

Write-Host "Stopping the stack..."
$stop = Invoke-Native docker @("compose", "stop")
if ($stop.ExitCode -ne 0) { throw "docker compose stop failed: $($stop.Output -join ' ')" }
Write-Host "Stopping and removing managed application containers (data is retained)..."
Remove-ManagedAppContainers

$failed = $false
try {
    $runtimeArchive = Join-Path $Backup "runtime-images.tar"
    if (Test-Path -LiteralPath $runtimeArchive) {
        $loaded = Invoke-Native docker @("image", "load", "--input", $runtimeArchive)
        if ($loaded.ExitCode -ne 0) { throw "Restoring runtime images failed: $($loaded.Output -join ' ')" }
    }
    foreach ($item in $volumes) {
        Write-Host "Restoring $($item.Volume) ..."
        if (-not (Test-DockerVolume $item.Volume)) {
            $created = Invoke-Native docker @("volume", "create", "--label", "be.hertogs.backup=true", $item.Volume)
            if ($created.ExitCode -ne 0) { throw "Creating volume $($item.Volume) failed" }
        }
        $staging = "hertogs-restore-staging-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
        [void](Invoke-Native docker @("volume", "create", $staging))
        $result = Invoke-Helper -Mounts @("$($item.Volume):/data", "${Backup}:/backup:ro", "${staging}:/staging") `
            -Script "restore-volume.sh" -ScriptArgs @($item.File)
        if ($result.ExitCode -ne 0) {
            # Keep the staging volume: if the copy into the real volume broke halfway,
            # it still holds the complete extracted data.
            throw ("Restoring $($item.Volume) failed: $($result.Output -join ' ')`n" +
                "The extracted data is kept in the Docker volume $staging. Run this script again " +
                "after fixing the cause; delete that volume afterwards (docker volume rm $staging).")
        }
        [void](Invoke-Native docker @("volume", "rm", $staging))
    }

    if ($restoreFolders) {
        Write-Host "Restoring workspace folders ..."
        $result = Invoke-Helper -Mounts @("${WorkspaceVolume}:/data", "${Backup}:/backup:ro") -Script "restore-folders.sh"
        if ($result.ExitCode -ne 0) { throw "Restoring workspace folders failed: $($result.Output -join ' ')" }
    }
    Write-Host "Restore complete."
}
catch {
    $failed = $true
    throw
}
finally {
    if ($failed) {
        Write-Warning "The stack was left stopped so nothing runs on half-restored data. Start it with: docker compose up -d"
    }
    else {
        Write-Host "Starting the stack..."
        $up = Invoke-Native docker @("compose", "up", "-d")
        if ($up.ExitCode -ne 0) { Write-Warning "docker compose up failed: $($up.Output -join ' ')" }
    }
}
