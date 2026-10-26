<#
.SYNOPSIS
    Publish a backup made by backup_wsl.ps1 as GitHub release assets.

.DESCRIPTION
    GitHub takes release files up to 2 GB, so the archive is split into parts
    (<archive>.part01, .part02, ...); restore.cmd joins them again. The parts and
    the restore files are staged in dist\<backup folder>.release\.

    Needs the GitHub CLI (gh), signed in. If the upload stops, run the same
    command again: files already uploaded are skipped.

    The repository is public: anyone can download what you attach.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\release_backup.ps1 -Tag v2.0.0
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\release_backup.ps1 -Tag v2.0.0 -PrepareOnly
#>
param(
    [Parameter(Mandatory = $true)][string]$Tag,
    # Default: the newest dist\<distro>-backup-<date> folder.
    [string]$Bundle = '',
    [string]$Title = '',
    # Markdown release notes. Default: short notes on how to restore.
    [string]$NotesFile = '',
    [int]$PartMB = 1024,
    # Split and stage the files, but do not create the release or upload.
    [switch]$PrepareOnly
)

$Root = Split-Path $PSScriptRoot -Parent

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "   WARNING: $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }
function Size($bytes) {
    if ($bytes -ge 1GB) { '{0:N1} GB' -f ($bytes / 1GB) }
    elseif ($bytes -ge 1MB) { '{0:N1} MB' -f ($bytes / 1MB) }
    else { '{0:N0} KB' -f [math]::Ceiling($bytes / 1KB) }
}

if (-not $Bundle) {
    $Bundle = Get-ChildItem (Join-Path $Root 'dist') -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '-backup-\d{8}$' } | Sort-Object Name | Select-Object -Last 1 -ExpandProperty FullName
    if (-not $Bundle) { Die 'No dist\<distro>-backup-<date> folder found. Run scripts\backup_wsl.ps1 first.' }
}
$Bundle = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Bundle)
$infoFile = Join-Path $Bundle 'backup-info.txt'
if (-not (Test-Path $infoFile)) { Die "$infoFile not found." }
$info = [ordered]@{}
foreach ($line in Get-Content $infoFile) {
    $i = $line.IndexOf('=')
    if ($i -gt 0) { $info[$line.Substring(0, $i)] = $line.Substring($i + 1) }
}
$archive = Get-Item (Join-Path $Bundle $info['archive']) -ErrorAction SilentlyContinue
if (-not $archive) { Die "Archive $($info['archive']) not found in $Bundle." }
$Stage = "$Bundle.release"
New-Item -ItemType Directory -Force -Path $Stage | Out-Null

Step "Splitting $($archive.Name) ($(Size $archive.Length)) into $PartMB MB parts"
$partBytes = [int64]$PartMB * 1MB
$count = [int][math]::Ceiling($archive.Length / $partBytes)
$parts = @(Get-ChildItem $Stage -File | Where-Object { $_.Name -match '\.part\d+$' } | Sort-Object Name)
$staged = ($parts | Measure-Object Length -Sum).Sum
if ($parts.Count -eq $count -and $staged -eq $archive.Length -and $parts[0].Name -eq "$($archive.Name).part01") {
    Write-Host "   already split ($count parts)"
} else {
    $parts | Remove-Item
    $buffer = New-Object byte[] (4MB)
    $in = [IO.File]::OpenRead($archive.FullName)
    try {
        for ($n = 1; $n -le $count; $n++) {
            $out = [IO.File]::Create((Join-Path $Stage ('{0}.part{1:D2}' -f $archive.Name, $n)))
            try {
                $left = $partBytes
                while ($left -gt 0) {
                    $read = $in.Read($buffer, 0, [int][math]::Min($buffer.Length, $left))
                    if ($read -le 0) { break }
                    $out.Write($buffer, 0, $read)
                    $left -= $read
                }
            } finally { $out.Dispose() }
        }
    } finally { $in.Dispose() }
    Write-Host "   $count parts"
}

Step 'Staging the restore files'
# Restore script and guide from the repo (newest); the rest from the backup.
Copy-Item (Join-Path $Root 'scripts\restore_wsl.ps1'), (Join-Path $Root 'WSL-BACKUP-GUIDE.md') $Stage
foreach ($f in 'restore.cmd', 'wslconfig.txt', "$($archive.Name).sha256") {
    if (Test-Path (Join-Path $Bundle $f)) { Copy-Item (Join-Path $Bundle $f) $Stage }
}
$info['archive_bytes'] = $archive.Length
$info['parts'] = $count
Set-Content -Encoding ASCII -Path (Join-Path $Stage 'backup-info.txt') -Value @($info.Keys | ForEach-Object { "$_=$($info[$_])" })

$small = @('restore.cmd', 'restore_wsl.ps1', 'backup-info.txt', 'WSL-BACKUP-GUIDE.md', 'wslconfig.txt', "$($archive.Name).sha256")
$assets = @($small | ForEach-Object { Get-Item (Join-Path $Stage $_) -ErrorAction SilentlyContinue }) +
          @(Get-ChildItem $Stage -File | Where-Object { $_.Name -match '\.part\d+$' } | Sort-Object Name)
$assets | ForEach-Object { '   {0,-44} {1,10}' -f $_.Name, (Size $_.Length) }

if ($PrepareOnly) {
    Write-Host "`nStaged in $Stage. Run again without -PrepareOnly to upload."
    exit 0
}

Step "Creating release $Tag"
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { Die 'The GitHub CLI (gh) is not installed.' }
Push-Location $Root
try {
    & gh release view $Tag --json name | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host '   exists; adding files to it'
    } else {
        if (-not $Title) { $Title = "$Tag - $($info['distro']) WSL backup" }
        if (-not $NotesFile) {
            $NotesFile = Join-Path $Stage 'release-notes.md'
            Set-Content -Encoding ASCII -Path $NotesFile -Value (@'
Whole WSL {0} system backup (made {1}): Ubuntu, Docker, the NVIDIA Container Toolkit, the CodeGen images and models.

**Restore on a PC with no internet:** download **all** files below into one folder, then double-click `restore.cmd`. It joins the {2} parts, verifies the checksum and imports the distribution. See `WSL-BACKUP-GUIDE.md` for what the PC needs first (current WSL package, NVIDIA Windows driver).
'@ -f $info['distro'], $info['created'], $count)
        }
        & gh release create $Tag --title $Title --notes-file $NotesFile
        if ($LASTEXITCODE -ne 0) { Die 'gh release create failed.' }
    }

    Step "Uploading $($assets.Count) files"
    $have = @{}
    foreach ($a in (& gh release view $Tag --json assets | ConvertFrom-Json).assets) { $have[$a.name] = [int64]$a.size }
    foreach ($f in $assets) {
        if ($have[$f.Name] -eq $f.Length) { Write-Host "   $($f.Name): already uploaded"; continue }
        for ($try = 1; $try -le 5; $try++) {
            $started = Get-Date
            & gh release upload $Tag $f.FullName --clobber
            if ($LASTEXITCODE -eq 0) { break }
            Warn "upload of $($f.Name) failed (try $try of 5)"
            Start-Sleep -Seconds 15
        }
        if ($LASTEXITCODE -ne 0) { Die "Could not upload $($f.Name). Run the same command again to continue." }
        $seconds = [math]::Max(1, ((Get-Date) - $started).TotalSeconds)
        Write-Host ('   {0}: {1} at {2:N1} MB/s' -f $f.Name, (Size $f.Length), ($f.Length / 1MB / $seconds))
    }
    Step 'Done'
    & gh release view $Tag --json url --jq .url
} finally {
    Pop-Location
}
