<#
.SYNOPSIS
    Back up a whole WSL distribution (default: Ubuntu-22.04, with Docker, the
    CodeGen images and models inside) into one folder you can carry to an
    offline Windows PC and restore there with restore.cmd.

.DESCRIPTION
    Run on the Windows host (not inside WSL). The distribution is stopped while
    it is exported. Output folder:

        <OutDir>\<Distro>-backup-<yyyyMMdd>\
            <Distro>-<yyyyMMdd>.tar.gz          the distribution
            <Distro>-<yyyyMMdd>.tar.gz.sha256   checksum (sha256sum -c compatible)
            restore.cmd / restore_wsl.ps1       restore on the offline PC
            backup-info.txt                     what was backed up
            wslconfig.txt                       copy of this PC's .wslconfig
            WSL-BACKUP-GUIDE.md

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\backup_wsl.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\backup_wsl.ps1 -OutDir E:\Backup -Format tar
#>
param(
    [string]$Distro = 'Ubuntu-22.04',
    # Default: <repo>\dist (gitignored).
    [string]$OutDir = '',
    # tar.gz: good size/speed balance. tar.xz: smaller, much slower. tar: fastest, largest.
    [ValidateSet('tar', 'tar.gz', 'tar.xz')]
    [string]$Format = 'tar.gz',
    # Default: /home/<default user>/codegen-gpu
    [string]$ProjectDir = '',
    # Drop the Docker build cache (~4 GB). Keep it if you may rebuild images offline.
    [switch]$PruneBuildCache,
    # Do not ask before stopping the distribution.
    [switch]$Yes
)

$env:WSL_UTF8 = '1'   # wsl.exe prints UTF-8 instead of UTF-16
$Root = Split-Path $PSScriptRoot -Parent
if (-not $OutDir) { $OutDir = Join-Path $Root 'dist' }
$OutDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutDir)

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "   WARNING: $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }
function Size($bytes) {
    if ($bytes -ge 1GB) { '{0:N1} GB' -f ($bytes / 1GB) }
    elseif ($bytes -ge 1MB) { '{0:N1} MB' -f ($bytes / 1MB) }
    else { '{0:N0} KB' -f [math]::Ceiling($bytes / 1KB) }
}

# Run a command inside the distribution as root (no sudo password needed).
function InDistro {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Cmd)
    & wsl.exe -d $Distro -u root -- @Cmd
}

function Get-WslDistros {
    Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' -ErrorAction SilentlyContinue |
        ForEach-Object { (Get-ItemProperty $_.PSPath).DistributionName }
}

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { Die 'WSL is not installed.' }
if ((Get-WslDistros) -notcontains $Distro) {
    Die "No WSL distribution named '$Distro'. Installed: $((Get-WslDistros) -join ', ')"
}

$stamp   = Get-Date -Format 'yyyyMMdd'
$Bundle  = Join-Path $OutDir "$Distro-backup-$stamp"
$Archive = "$Distro-$stamp.$Format"
$ArchivePath = Join-Path $Bundle $Archive
if (Test-Path $ArchivePath) { Die "$ArchivePath already exists. Delete it or use -OutDir." }

if (-not $Yes) {
    Write-Host "This stops '$Distro' (and Docker / CodeGen inside it) while it is exported."
    $answer = Read-Host 'Continue? [y/N]'
    if ($answer -notmatch '^[yY]') { exit 1 }
}

Step "Reading $Distro"
$user = (& wsl.exe -d $Distro -- whoami | Select-Object -Last 1).Trim()
$used = [int64]((InDistro df -B1 --output=used / | Select-Object -Last 1).Trim())
$images = @(InDistro docker images --format '{{.Repository}}:{{.Tag}}' | Where-Object { $_ -and $_ -notmatch '<none>' })
if (-not $ProjectDir) { $ProjectDir = "/home/$user/codegen-gpu" }
InDistro test -f "$ProjectDir/docker-compose.yml"
if ($LASTEXITCODE -ne 0) { Warn "$ProjectDir/docker-compose.yml not found; -Start will not work after restore."; $ProjectDir = '' }
$wslVersion = @(& wsl.exe --version)[0]
Write-Host "   default user: $user"
Write-Host "   disk in use:  $(Size $used)"
Write-Host "   images:       $($images.Count) ($($images -join ', '))"

Step 'Cleaning caches inside the distribution'
InDistro apt-get clean
InDistro journalctl --quiet --vacuum-size=100M
if ($PruneBuildCache) { InDistro docker builder prune -af }

New-Item -ItemType Directory -Force -Path $Bundle | Out-Null
$free = (Get-PSDrive -Name (Split-Path -Qualifier $Bundle).TrimEnd(':')).Free
if ($free -lt $used) { Warn "only $(Size $free) free on the output drive; the archive may not fit." }

Step "Stopping $Distro"
InDistro systemctl stop docker.socket docker.service containerd.service
& wsl.exe --terminate $Distro | Out-Null

Step "Exporting to $ArchivePath (several minutes; the progress line may stay still)"
$started = Get-Date
if ($Format -eq 'tar') {
    & wsl.exe --export $Distro $ArchivePath
} else {
    & wsl.exe --export $Distro $ArchivePath --format $Format
}
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $ArchivePath)) { Die 'wsl --export failed.' }
$archiveSize = (Get-Item $ArchivePath).Length
Write-Host "   $(Size $archiveSize) in $([int]((Get-Date) - $started).TotalMinutes) min"

Step 'Writing checksum'
$hash = (Get-FileHash -Algorithm SHA256 $ArchivePath).Hash.ToLower()
Set-Content -Encoding ASCII -Path "$ArchivePath.sha256" -Value "$hash  $Archive"

Step 'Adding restore files'
Copy-Item (Join-Path $PSScriptRoot 'restore_wsl.ps1') $Bundle
Copy-Item (Join-Path $Root 'WSL-BACKUP-GUIDE.md') $Bundle
$wslconfig = Join-Path $env:USERPROFILE '.wslconfig'
if (Test-Path $wslconfig) { Copy-Item $wslconfig (Join-Path $Bundle 'wslconfig.txt') }
Set-Content -Encoding ASCII -Path (Join-Path $Bundle 'restore.cmd') -Value @(
    '@echo off'
    'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restore_wsl.ps1" %*'
    'pause'
)
Set-Content -Encoding ASCII -Path (Join-Path $Bundle 'backup-info.txt') -Value @(
    "distro=$Distro"
    "created=$(Get-Date -Format 'yyyy-MM-dd HH:mm')"
    "source_wsl=$wslVersion"
    "default_user=$user"
    "used_bytes=$used"
    "archive=$Archive"
    "project_dir=$ProjectDir"
    "images=$($images -join ',')"
)

Step 'Done'
Get-ChildItem $Bundle | ForEach-Object { '   {0,-40} {1,10}' -f $_.Name, (Size $_.Length) }
Write-Host @"

Copy the whole folder to the offline PC, then run restore.cmd in it.
  $Bundle
'$Distro' is stopped; it starts again the next time you open it.
"@
