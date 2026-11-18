# Shared helpers for the PowerShell scripts. Dot-source this file:
#   . (Join-Path $PSScriptRoot "lib\common.ps1")
#
# Works in Windows PowerShell 5.1 and PowerShell 7. Rules followed here:
# - never pass arguments containing double quotes to native programs
#   (5.1 does not escape them for docker.exe);
# - run native programs through Invoke-Native so stderr output does not turn
#   into a terminating error under $ErrorActionPreference = "Stop";
# - read and write .env as UTF-8 without BOM (docker compose misreads a BOM).

$script:ProjectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$script:EnvFile = Join-Path $script:ProjectRoot ".env"
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Get-ProjectRoot { return $script:ProjectRoot }

function Read-EnvLines {
    if (-not (Test-Path -LiteralPath $script:EnvFile)) { return @() }
    $text = [System.IO.File]::ReadAllText($script:EnvFile)
    if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) { $text = $text.Substring(1) }
    return @($text -split "\r?\n")
}

function Get-EnvValue {
    # Returns $null when the key is absent, otherwise the (unquoted) value.
    param([Parameter(Mandatory)] [string] $Name)
    foreach ($line in Read-EnvLines) {
        if ($line -match "^\s*$([regex]::Escape($Name))\s*=(.*)$") {
            $value = $Matches[1].Trim()
            if ($value.Length -ge 2 -and ($value[0] -eq '"' -or $value[0] -eq "'")) {
                # Quoted: the value ends at the matching quote; anything after it is a comment.
                $close = $value.IndexOf($value[0], 1)
                if ($close -gt 0) { return $value.Substring(1, $close - 1) }
            }
            return ($value -replace '\s+#.*$', '')
        }
    }
    return $null
}

function Set-EnvValue {
    param(
        [Parameter(Mandatory)] [string] $Name,
        [Parameter(Mandatory)] [AllowEmptyString()] [string] $Value
    )
    $lines = @(Read-EnvLines)
    if ($lines.Count -gt 0 -and $lines[-1] -eq "") { $lines = @($lines[0..($lines.Count - 2)]) }
    if ($Value.Contains("'")) { throw "The value for $Name cannot contain a ' character." }
    $pattern = "^\s*$([regex]::Escape($Name))\s*="
    $entry = if ($Value -match '[\s#"$]') { "$Name='$Value'" } else { "$Name=$Value" }
    $found = $false
    $updated = foreach ($line in $lines) {
        if ($line -match $pattern) { $found = $true; $entry } else { $line }
    }
    if (-not $found) { $updated = @($updated) + $entry }
    [System.IO.File]::WriteAllText($script:EnvFile, (($updated -join "`r`n") + "`r`n"), $script:Utf8NoBom)
}

function Add-MissingEnvKeys {
    # Appends keys that exist in .env.example but not in .env (with the example's value).
    $example = Join-Path $script:ProjectRoot ".env.example"
    $added = @()
    foreach ($line in [System.IO.File]::ReadAllLines($example)) {
        if ($line -match '^\s*([A-Z0-9_]+)\s*=(.*)$') {
            $name = $Matches[1]
            if ($null -eq (Get-EnvValue $name)) {
                Set-EnvValue $name $Matches[2].Trim()
                $added += $name
            }
        }
    }
    return $added
}

function New-RandomSecret {
    param([int] $Bytes = 32)
    $buffer = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($buffer).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Invoke-Native {
    # Runs a native command and returns @{ ExitCode; Output }. Never throws because
    # the program wrote to stderr; callers decide what a non-zero exit code means.
    param(
        [Parameter(Mandatory)] [string] $FilePath,
        [string[]] $ArgumentList = @()
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $FilePath @ArgumentList 2>&1 | ForEach-Object { "$_" }
        return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = @($output) }
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

function Invoke-NativeLive {
    # Like Invoke-Native, but shows the output while it runs (builds, pulls) and
    # returns only the exit code. Safe under Windows PowerShell 5.1 with
    # $ErrorActionPreference = "Stop", even when the script's output is redirected.
    param(
        [Parameter(Mandatory)] [string] $FilePath,
        [string[]] $ArgumentList = @()
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $FilePath @ArgumentList 2>&1 | ForEach-Object { Write-Host "$_" }
        return $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

function Get-HttpStatus {
    # GET without following redirects; returns Status, Location, CacheStatus,
    # Mitigated (Cloudflare challenge), Csp and the start of the body, or $null
    # when the address cannot be reached. Works in Windows PowerShell 5.1 and 7.
    param([Parameter(Mandatory)] [string] $Url)
    Add-Type -AssemblyName System.Net.Http
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.AllowAutoRedirect = $false
    $client = New-Object System.Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(15)
    # Cloudflare's Browser Integrity Check challenges requests without a User-Agent.
    [void]$client.DefaultRequestHeaders.UserAgent.TryParseAdd("Mozilla/5.0 (Windows NT 10.0; Win64; x64) mcp-hosting-test-local/1")
    try {
        $response = $client.GetAsync($Url).GetAwaiter().GetResult()
        $header = {
            param($Name)
            $values = $null
            if ($response.Headers.TryGetValues($Name, [ref]$values)) { return @($values)[0] }
            return ""
        }
        $location = if ($response.Headers.Location) { $response.Headers.Location.ToString() } else { "" }
        $body = ""
        try { $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult() } catch { $body = "" }
        if ($body.Length -gt 400) { $body = $body.Substring(0, 400) }
        return [pscustomobject]@{
            Status = [int]$response.StatusCode; Location = $location
            CacheStatus = (& $header "cf-cache-status"); Mitigated = (& $header "cf-mitigated")
            Csp = (& $header "content-security-policy"); Body = $body
        }
    }
    catch {
        return $null
    }
    finally {
        $client.Dispose()
    }
}

function Assert-DockerRunning {
    $result = Invoke-Native docker @("info", "--format", "{{.ServerVersion}}")
    if ($result.ExitCode -ne 0) { throw "Docker is not running (start Docker Desktop first)." }
}

function Test-DockerVolume {
    param([Parameter(Mandatory)] [string] $Name)
    $result = Invoke-Native docker @("volume", "ls", "--quiet")
    if ($result.ExitCode -ne 0) { throw "Could not list Docker volumes: $($result.Output -join ' ')" }
    return (@($result.Output) -contains $Name)
}

function Wait-DockerRunning {
    # Docker Desktop can still be starting (a missed backup runs at logon or wake).
    param([int] $Minutes = 10)
    $deadline = (Get-Date).AddMinutes($Minutes)
    while ($true) {
        $result = Invoke-Native docker @("info", "--format", "{{.ServerVersion}}")
        if ($result.ExitCode -eq 0) { return }
        if ((Get-Date) -ge $deadline) { throw "Docker did not start within $Minutes minutes." }
        Write-Host "Waiting for Docker Desktop..."
        Start-Sleep -Seconds 30
    }
}

function Test-DockerImage {
    param([Parameter(Mandatory)] [string] $Name)
    $result = Invoke-Native docker @("image", "ls", "--quiet", $Name)
    return ($result.ExitCode -eq 0 -and @($result.Output | Where-Object { $_ }).Count -gt 0)
}

function Get-PowerShellHost {
    # Prefer PowerShell 7, fall back to Windows PowerShell; never the ISE.
    $pwsh = Join-Path $env:ProgramFiles "PowerShell\7\pwsh.exe"
    if (Test-Path -LiteralPath $pwsh) { return $pwsh }
    return (Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe")
}

function Test-NetworkPath {
    param([Parameter(Mandatory)] [string] $Path)
    if ($Path.StartsWith("\\")) { return $true }
    $root = [System.IO.Path]::GetPathRoot($Path)
    if (-not $root) { return $false }
    try { return ([System.IO.DriveInfo]::new($root).DriveType -eq [System.IO.DriveType]::Network) }
    catch { return $false }
}

function Select-Folder {
    param([string] $Description = "Choose a folder")
    # Shell.Application works in both Windows PowerShell 5.1 and PowerShell 7.
    Write-Host "A folder dialog has opened (check the taskbar if you do not see it)."
    $shell = New-Object -ComObject Shell.Application
    $folder = $shell.BrowseForFolder(0, $Description, 0x51, 0)
    if ($null -eq $folder) { return $null }
    return $folder.Self.Path
}

function Get-IsoWeekKey {
    param([Parameter(Mandatory)] [datetime] $Date)
    # The ISO week of a date is the week of its Thursday; also gives the right ISO year.
    $offset = (([int]$Date.DayOfWeek + 6) % 7)
    $thursday = $Date.Date.AddDays(3 - $offset)
    $week = [System.Globalization.CultureInfo]::InvariantCulture.Calendar.GetWeekOfYear(
        $thursday, [System.Globalization.CalendarWeekRule]::FirstFourDayWeek, [DayOfWeek]::Monday)
    return "{0}-W{1:D2}" -f $thursday.Year, $week
}

function Remove-ManagedAppContainers {
    # Dynamic apps have no Compose labels. Remove their containers before replacing
    # data, so old images, environments and namespaces cannot survive a restore.
    # Volumes are retained. The deployer MUST be stopped before calling this.
    $listed = Invoke-Native docker @("ps", "-aq", "--filter", "label=mcp-hosting-workspace.app")
    if ($listed.ExitCode -ne 0) { throw "Cannot list application containers before restore." }
    foreach ($container in @($listed.Output | Where-Object { $_ })) {
        $removed = Invoke-Native docker @("container", "rm", "--force", $container)
        if ($removed.ExitCode -ne 0) { throw "Cannot stop/remove application container $container; restore aborted." }
    }
    $remaining = Invoke-Native docker @("ps", "-aq", "--filter", "label=mcp-hosting-workspace.app")
    if ($remaining.ExitCode -ne 0 -or @($remaining.Output | Where-Object { $_ }).Count -gt 0) {
        throw "Application containers remain; restore aborted before replacing data."
    }
}

function Select-BackupsToKeep {
    # Input: backup folder names "mcp-hosting-yyyyMMdd-HHmmss". Keeps the newest backup of
    # each of the last $Daily days that have backups, plus the newest of each of the
    # $Weekly most recent ISO weeks older than those days. Returns the names to keep.
    param(
        [string[]] $Names,
        [int] $Daily = 7,
        [int] $Weekly = 4
    )
    $sorted = @($Names | Where-Object { $_ -match '^mcp-hosting-\d{8}-\d{6}$' } | Sort-Object -Descending)
    $dateOffset = "mcp-hosting-".Length
    $keep = New-Object 'System.Collections.Generic.List[string]'
    $days = New-Object 'System.Collections.Generic.List[string]'
    foreach ($name in $sorted) {
        $day = $name.Substring($dateOffset, 8)
        if (-not $days.Contains($day)) {
            if ($days.Count -ge $Daily) { break }
            $days.Add($day)
            $keep.Add($name)
        }
    }
    $oldestKeptDay = if ($days.Count -gt 0) { $days[$days.Count - 1] } else { "99999999" }
    $weeks = New-Object 'System.Collections.Generic.List[string]'
    foreach ($name in $sorted) {
        $day = $name.Substring($dateOffset, 8)
        if ($day -ge $oldestKeptDay) { continue }
        $week = Get-IsoWeekKey ([datetime]::ParseExact($day, "yyyyMMdd", $null))
        if (-not $weeks.Contains($week)) {
            if ($weeks.Count -ge $Weekly) { break }
            $weeks.Add($week)
            $keep.Add($name)
        }
    }
    return @($keep)
}
