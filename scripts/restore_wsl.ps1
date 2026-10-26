<#
.SYNOPSIS
    Restore a WSL distribution backed up by backup_wsl.ps1 on a PC with no
    internet. Run it from the backup folder (or double-click restore.cmd).

.EXAMPLE
    .\restore.cmd
.EXAMPLE
    .\restore.cmd -InstallDir D:\WSL\Ubuntu-22.04 -SetDefault -Start
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\restore_wsl.ps1 -Name Ubuntu-22.04-codegen
#>
param(
    # Name for the new distribution. Default: the name it had when backed up.
    [string]$Name = '',
    # Where its virtual disk (ext4.vhdx) goes. Default: <system drive>\WSL\<Name>
    [string]$InstallDir = '',
    # Default: the single .tar / .tar.gz / .tar.xz next to this script.
    [string]$Archive = '',
    # Make it the distribution that plain 'wsl' opens.
    [switch]$SetDefault,
    # Start CodeGen (docker compose up -d) once restored.
    [switch]$Start,
    [switch]$SkipHashCheck
)

$env:WSL_UTF8 = '1'   # wsl.exe prints UTF-8 instead of UTF-16
$Here = $PSScriptRoot
$GpuTestImage = 'ghcr.io/ggml-org/llama.cpp:server-cuda'

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "   WARNING: $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }
function Size($bytes) { '{0:N1} GB' -f ($bytes / 1GB) }

# Run a command inside the restored distribution as root.
function InDistro {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Cmd)
    & wsl.exe -d $Name -u root -- @Cmd
}

function Get-WslDistros {
    Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' -ErrorAction SilentlyContinue |
        ForEach-Object { (Get-ItemProperty $_.PSPath).DistributionName }
}

# backup-info.txt: key=value lines written by backup_wsl.ps1
$info = @{}
$infoFile = Join-Path $Here 'backup-info.txt'
if (Test-Path $infoFile) {
    foreach ($line in Get-Content $infoFile) {
        $i = $line.IndexOf('=')
        if ($i -gt 0) { $info[$line.Substring(0, $i)] = $line.Substring($i + 1) }
    }
}
if (-not $Name) { $Name = $info['distro'] }
if (-not $Name) { $Name = 'Ubuntu-22.04' }
if (-not $InstallDir) { $InstallDir = Join-Path "$env:SystemDrive\WSL" $Name }
$InstallDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($InstallDir)

Step 'Checking WSL'
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    Die 'WSL is not installed. See "Install WSL offline" in WSL-BACKUP-GUIDE.md.'
}
# 'wsl --version' only works on the current WSL package, which systemd (and so
# Docker starting by itself) needs; the old built-in WSL is not enough.
$wslVersion = & wsl.exe --version
if ($LASTEXITCODE -ne 0) {
    Die 'WSL is missing or too old. See "Install WSL offline" in WSL-BACKUP-GUIDE.md.'
}
Ok "$($wslVersion | Select-Object -First 1) (backup made with: $($info['source_wsl']))"
if ((Get-WslDistros) -contains $Name) {
    Die "A distribution named '$Name' already exists. Choose another with -Name <new-name>."
}

Step 'Finding the archive'
if (-not $Archive) {
    $found = @(Get-ChildItem $Here -File | Where-Object { $_.Name -match '\.tar(\.gz|\.xz)?$' })
    if ($found.Count -ne 1) { Die "Expected one .tar/.tar.gz/.tar.xz in $Here, found $($found.Count). Use -Archive <file>." }
    $Archive = $found[0].FullName
}
if (-not (Test-Path $Archive)) { Die "$Archive not found." }
$Archive = (Resolve-Path $Archive).Path
Ok "$Archive ($(Size (Get-Item $Archive).Length))"

if (-not $SkipHashCheck) {
    Step 'Verifying the checksum (a few minutes)'
    $sumFile = "$Archive.sha256"
    if (Test-Path $sumFile) {
        $expected = ((Get-Content $sumFile | Select-Object -First 1) -split '\s+')[0].ToLower()
        $actual = (Get-FileHash -Algorithm SHA256 $Archive).Hash.ToLower()
        if ($actual -ne $expected) { Die 'Checksum mismatch: the archive is damaged. Copy it again.' }
        Ok 'checksum OK'
    } else {
        Warn "$sumFile not found; skipping the check."
    }
}

Step "Preparing $InstallDir"
if (Test-Path (Join-Path $InstallDir 'ext4.vhdx')) { Die "$InstallDir already holds a distribution disk. Use -InstallDir <empty folder>." }
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$need = [int64]$info['used_bytes'] * 1.1
$free = (Get-PSDrive -Name (Split-Path -Qualifier (Resolve-Path $InstallDir).Path).TrimEnd(':')).Free
if ($need -gt 0 -and $free -lt $need) { Die "Need about $(Size $need) free on that drive, only $(Size $free) available." }
Ok "$(Size $free) free"

$wslconfig = Join-Path $env:USERPROFILE '.wslconfig'
$wslconfigBackup = Join-Path $Here 'wslconfig.txt'
if ((Test-Path $wslconfigBackup) -and -not (Test-Path $wslconfig)) {
    Copy-Item $wslconfigBackup $wslconfig
    Ok "installed $wslconfig (keeps WSL running with no terminal open; applies after 'wsl --shutdown')"
} elseif (Test-Path $wslconfigBackup) {
    Ok "kept your existing $wslconfig (the source PC's settings are in wslconfig.txt)"
}

Step "Importing as '$Name' (several minutes)"
& wsl.exe --import $Name $InstallDir $Archive --version 2
if ($LASTEXITCODE -ne 0) { Die 'wsl --import failed (see the message above).' }
Ok 'imported'

Step 'Checking the restored system'
$user = $info['default_user']
$who = (& wsl.exe -d $Name -- whoami | Select-Object -Last 1)
if ($who) { $who = $who.Trim() }
if ($user -and $who -ne $user) {
    & wsl.exe --manage $Name --set-default-user $user | Out-Null
    $who = (& wsl.exe -d $Name -- whoami | Select-Object -Last 1).Trim()
}
Ok "default user: $who"

$dockerState = ''
for ($i = 0; $i -lt 30 -and $dockerState -ne 'active'; $i++) {
    $dockerState = (InDistro systemctl is-active docker | Select-Object -Last 1)
    if ($dockerState) { $dockerState = $dockerState.Trim() }
    if ($dockerState -ne 'active') { Start-Sleep -Seconds 2 }
}
if ($dockerState -eq 'active') { Ok 'Docker is running' } else { Warn "Docker is '$dockerState'. Try: wsl -d $Name -u root -- systemctl start docker" }

if ($info['images']) {
    $have = @(InDistro docker images --format '{{.Repository}}:{{.Tag}}')
    $missing = @($info['images'].Split(',') | Where-Object { $_ -and $have -notcontains $_ })
    if ($missing.Count -eq 0) { Ok "all $($info['images'].Split(',').Count) Docker images present" }
    else { Warn "missing Docker images: $($missing -join ', ')" }
}

InDistro /usr/lib/wsl/lib/nvidia-smi -L
if ($LASTEXITCODE -eq 0) {
    Ok 'GPU visible in WSL'
    InDistro docker image inspect $GpuTestImage | Out-Null
    if ($LASTEXITCODE -eq 0) {
        InDistro docker run --rm --gpus all --entrypoint nvidia-smi $GpuTestImage -L | Out-Null
        if ($LASTEXITCODE -eq 0) { Ok 'GPU visible in Docker' } else { Warn "'docker run --gpus all' failed; see Troubleshooting in the guide." }
    }
} else {
    Warn 'no GPU in WSL. Install the NVIDIA driver for Windows on this PC, then run: wsl --shutdown'
}

if ($SetDefault) {
    & wsl.exe --set-default $Name
    Ok "'$Name' is now the default distribution"
}

$project = $info['project_dir']
if ($Start -and $project) {
    Step 'Starting CodeGen'
    & wsl.exe -d $Name --cd $project -- docker compose up -d
}

Step 'Done'
Write-Host "   Open a shell:   wsl -d $Name"
if ($project) {
    Write-Host "   Start CodeGen:  wsl -d $Name --cd $project -- docker compose up -d"
    Write-Host '   Then open:      http://localhost:7860   (same accounts as on the source PC)'
}
