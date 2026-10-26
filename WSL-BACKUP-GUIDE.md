# CodeGen — WSL Backup & Offline Restore Guide

This guide copies the **whole WSL Ubuntu-22.04 system** from a working PC to a PC
with **no internet**: Ubuntu, Docker, the NVIDIA Container Toolkit, the three
CodeGen images, the models, `.env`, the chat database and accounts — everything,
exactly as it is. On the offline PC you import one file and CodeGen is ready.

> Use [OFFLINE-GUIDE.md](OFFLINE-GUIDE.md) instead if the offline PC already has
> Linux/WSL with Docker and you only want to install the CodeGen app.

## 1. What the backup contains

`scripts/backup_wsl.ps1` produces one folder:

```
Ubuntu-22.04-backup-<date>/
├── Ubuntu-22.04-<date>.tar.gz          # the whole distribution (~12-15 GB)
├── Ubuntu-22.04-<date>.tar.gz.sha256   # checksum to verify the copy
├── restore.cmd                         # double-click on the offline PC
├── restore_wsl.ps1                     # what restore.cmd runs
├── backup-info.txt                     # distro name, user, disk size, image list
├── wslconfig.txt                       # the source PC's %USERPROFILE%\.wslconfig
└── WSL-BACKUP-GUIDE.md                 # this guide
```

> **Treat the backup as sensitive.** It is a full copy of the system: the CodeGen
> accounts and chats, `.env` (admin password), shell history, SSH keys and any
> saved tokens. Store and move it like you would the PC itself.

## 2. Make the backup (on the working PC)

In **Windows PowerShell** (not inside WSL), from the repo folder:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\backup_wsl.ps1
```

The script asks before it stops Ubuntu-22.04. While it runs it:

1. reads the default user, disk usage and Docker image list;
2. clears the apt and journal caches;
3. stops Docker and then the distribution;
4. runs `wsl --export` into `dist\Ubuntu-22.04-backup-<date>\`;
5. writes the checksum and copies the restore files next to the archive.

It takes about 15–30 minutes. The output drive needs about 25 GB free. Ubuntu
starts again the next time you open it.

Options:

| Option | Meaning |
|---|---|
| `-OutDir E:\Backup` | Write the folder somewhere else (e.g. straight to a USB disk). Default: `dist\` in the repo. |
| `-Format tar` | No compression: faster, but ~22 GB. `tar.xz` is the smallest and slowest. Default: `tar.gz`. |
| `-PruneBuildCache` | Drop the Docker build cache (~4 GB smaller). Leave it in if you may rebuild images offline. |
| `-Distro <name>` | Back up a different distribution. Default: `Ubuntu-22.04`. |
| `-Yes` | Don't ask before stopping the distribution. |

### Publish it as a GitHub release (optional)

GitHub release files must be under 2 GB, so `scripts/release_backup.ps1` splits
the archive into 1 GB parts (`<archive>.part01`, `.part02`, …). It stages them
with the restore files in `dist\<backup folder>.release\` and uploads them with
the GitHub CLI (`gh`, signed in):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\release_backup.ps1 -Tag v2.0.0
```

If the upload stops, run the same command again. Files already uploaded are
skipped. Add `-PrepareOnly` to split and stage the files without uploading.

> The repository is public, so **anyone can download the release**, including
> everything listed in section 1.

## 3. What the offline PC needs

- **Windows 11**, or Windows 10 21H2 or later, 64-bit, with **virtualization
  enabled** in the BIOS/UEFI.
- **WSL 2**, the current package (the one `wsl --version` works on). If it is
  missing, see [Install WSL offline](#install-wsl-offline) below.
- **NVIDIA driver for Windows**, a recent one. WSL gets the GPU through this
  driver. Don't install a Linux NVIDIA driver inside Ubuntu.
- **Disk:** about 25 GB free for the restored system, plus the space the backup
  folder takes while you copy it.
- **RAM:** 16 GB recommended.

### Install WSL offline

On a PC **with** internet, download these and bring them with the backup:

- the WSL package `wsl.<version>.x64.msi` from
  <https://github.com/microsoft/WSL/releases>. Use the same version as the
  source PC or newer; `wsl --version` shows it, and so does `backup-info.txt`.
- the NVIDIA Windows driver installer for the offline PC's GPU, from
  <https://www.nvidia.com/Download/index.aspx>.

On the offline PC, in **PowerShell as Administrator**:

```powershell
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
# restart Windows, then:
msiexec /i wsl.<version>.x64.msi
wsl --version          # must print the WSL version now
```

Install the NVIDIA driver by running its installer.

## 4. Restore (on the offline PC)

1. Copy the whole `Ubuntu-22.04-backup-<date>` folder to the offline PC. A local
   disk is faster than running from the USB stick.

   **From a GitHub release:** download **all** of the release's files (every
   `.partNN` and the small files) into one folder, then copy that folder.
   `restore.cmd` joins the parts first. It needs about 15 GB more free space
   while it does, and you can delete the parts afterwards.
2. Double-click **`restore.cmd`**, or run it from a terminal to pass options:

   ```powershell
   .\restore.cmd                                   # defaults
   .\restore.cmd -InstallDir D:\WSL\Ubuntu-22.04 -SetDefault -Start
   ```

The script:

1. checks WSL, and joins the parts if the backup came from a release;
2. verifies the checksum, so a damaged copy is caught before import;
3. checks free disk space;
4. installs `wslconfig.txt` as `%USERPROFILE%\.wslconfig`, but only if the PC has
   none;
5. runs `wsl --import`;
6. checks the default user, that Docker started, that the CodeGen images are
   present, and that the GPU is visible in WSL and in Docker.

Each check prints OK or a WARNING with what to do.

Options:

| Option | Meaning |
|---|---|
| `-InstallDir <folder>` | Where the virtual disk (`ext4.vhdx`) is stored. Default: `C:\WSL\<name>`. Pick the drive with the most space. |
| `-Name <name>` | Name for the restored distribution. Default: `Ubuntu-22.04`. Use another name if that one exists. |
| `-SetDefault` | Make it the distribution that plain `wsl` opens. |
| `-Start` | Start CodeGen right after restoring. |
| `-Archive <file>` | Use a specific archive, if the folder holds more than one. |

To join the parts by hand instead (in Ubuntu or any Linux):
`cat Ubuntu-22.04-<date>.tar.gz.part?? > Ubuntu-22.04-<date>.tar.gz`
| `-SkipHashCheck` | Skip the checksum check (not recommended). |

### Manual restore (instead of `restore.cmd`)

```powershell
wsl --import Ubuntu-22.04 C:\WSL\Ubuntu-22.04 .\Ubuntu-22.04-<date>.tar.gz --version 2
wsl -d Ubuntu-22.04
```

The default user (`adonis`) and systemd are set in the backup's `/etc/wsl.conf`,
so they apply automatically.

## 5. Start CodeGen

```powershell
wsl -d Ubuntu-22.04 --cd /home/adonis/codegen-gpu -- docker compose up -d
```

or inside Ubuntu:

```bash
cd ~/codegen-gpu
docker compose up -d
docker compose ps        # wait until all three are healthy (about a minute)
```

Open **http://localhost:7860**. Accounts, passwords and chats are the same as on
the source PC at backup time. For everyday commands and settings, see sections 6–9
of [OFFLINE-GUIDE.md](OFFLINE-GUIDE.md).

With the copied `.wslconfig`, WSL keeps running after you close the last terminal,
so CodeGen stays up. Run `wsl --shutdown` once after the first restore so that
setting takes effect.

## 6. Replacing it with a newer backup

Your chats live **inside** the restored system. Save them before you remove it:

```powershell
wsl -d Ubuntu-22.04 --cd /home/adonis/codegen-gpu -- docker compose down
wsl -d Ubuntu-22.04 -- cp -r /home/adonis/codegen-gpu/data /mnt/c/codegen-data-keep
wsl --unregister Ubuntu-22.04          # DELETES the restored system and its disk
```

Then run `restore.cmd` from the new backup folder. To put the saved data back:

```powershell
wsl -d Ubuntu-22.04 -- cp -r /mnt/c/codegen-data-keep/. /home/adonis/codegen-gpu/data/
```

To keep the old system as well, restore the new one under another name with
`-Name Ubuntu-22.04-new -InstallDir C:\WSL\Ubuntu-22.04-new`. Run only one of them
at a time: both publish port 7860.

## 7. Troubleshooting

- **"A distribution named 'Ubuntu-22.04' already exists".** Restore with
  `-Name <other-name>`, or remove the old one as in section 6.
- **"running scripts is disabled on this system".** Use `restore.cmd`, or run
  `powershell -ExecutionPolicy Bypass -File .\restore_wsl.ps1`.
- **Import fails with "virtualization" or HCS errors.** Enable virtualization
  (Intel VT-x / AMD-V / SVM) in the BIOS/UEFI and the *Virtual Machine Platform*
  feature (section 3).
- **"no GPU in WSL".** Install or update the NVIDIA **Windows** driver, run
  `wsl --shutdown`, open Ubuntu again and check with `nvidia-smi`.
- **"docker run --gpus all failed".** Run `nvidia-smi` inside Ubuntu first. If it
  works there, restart Docker: `sudo systemctl restart docker`.
- **Docker is not running.** `sudo systemctl start docker`, and check that
  `/etc/wsl.conf` still has `systemd=true` under `[boot]`.
- **"Failed to start the systemd user session" on start.** The source PC shows
  this message too. Docker and CodeGen work despite it.
- **WSL warns about `networkingMode=mirrored`.** Mirrored networking needs
  Windows 11 22H2 or later. Remove that line from `%USERPROFILE%\.wslconfig` and
  run `wsl --shutdown`. `localhost:7860` still works without it.
- **The restored disk uses more space over time.** Docker writes into the
  distribution's `ext4.vhdx`, which does not shrink by itself. Running
  `wsl --manage Ubuntu-22.04 --set-sparse true` lets Windows reclaim freed space.
