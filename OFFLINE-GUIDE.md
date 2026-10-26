# CodeGen — Offline Install Guide

This guide installs CodeGen on a machine with **no internet access**. The offline
bundle is a single `.tar` that already contains everything needed: the source,
the three Docker images and the three models. Nothing is downloaded at install
time.

> "Offline" means no internet — it does **not** mean no GPU. CodeGen still needs
> an NVIDIA GPU to run the model.

> To copy the **whole WSL Ubuntu system** (Docker, GPU toolkit, CodeGen, data)
> onto an offline Windows PC that has nothing set up yet, see
> [WSL-BACKUP-GUIDE.md](WSL-BACKUP-GUIDE.md) instead.

## 1. What's in the bundle

After you extract `codegen-gpu-offline-<date>.tar` you get a folder like:

```
codegen-gpu-offline-<date>/
├── load.sh                 # one command that loads the images and starts CodeGen
├── OFFLINE-GUIDE.md        # this guide
├── docker-compose.yml
├── .env.example
├── app/  guard/  scripts/  …   # source code
├── images/
│   ├── ghcr.io_ggml-org_llama.cpp_server-cuda.tar.gz
│   ├── codegen-gpu-guard_latest.tar.gz
│   └── codegen-gpu-codegen_latest.tar.gz
└── models/
    ├── qwen2.5-coder-7b-instruct-q4_k_m.gguf
    ├── Qwen3Guard-Gen-0.6B/
    └── Qwen3Guard-Stream-0.6B/
```

The Docker images are **prebuilt**, so the target machine never compiles or
downloads anything. (The build inputs — `guard/wheels/` — are left out to save
space; you only need them if you want to rebuild an image from source.)

## 2. Target machine requirements

- **OS:** Linux, or Windows 11 with WSL2. Tested under WSL2 + Ubuntu 22.04.
- **GPU:** an NVIDIA GPU with ~6 GB of VRAM or more, and a recent driver.
- **Docker** with the **NVIDIA Container Toolkit** (so `docker run --gpus all` works).
- **Docker Compose v2** (`docker compose`, not the old `docker-compose`).
- **Disk:** about 25 GB free (≈12 GB for the bundle, plus the images once loaded).
- **RAM:** 16 GB recommended (the guard models run on the CPU by default).

Quick prerequisite check on the target machine:

```bash
docker version                 # Docker is installed and the daemon runs
docker compose version         # Compose v2
docker info | grep -i runtime  # should mention "nvidia"
```

## 3. Copy the bundle to the target machine

Move both files with a USB drive, internal share, etc.:

```
codegen-gpu-offline-<date>.tar
codegen-gpu-offline-<date>.tar.sha256
```

On the target machine, verify the transfer and extract:

```bash
sha256sum -c codegen-gpu-offline-<date>.tar.sha256
tar -xf codegen-gpu-offline-<date>.tar
cd codegen-gpu-offline-<date>
```

> On Windows, keep the bundle **inside WSL** (e.g. `~/codegen-gpu`), not under
> `/mnt/c` or `/mnt/d`. The model files are large and run much faster from the
> Linux filesystem.

## 4. Install and start (one command)

```bash
./load.sh
```

`load.sh` loads the three images into Docker, checks the models are present,
creates `.env` from `.env.example` if needed, verifies GPU access, and starts the
three services. The first start takes about a minute while the models load.

## 5. First sign-in

An administrator account is created on the first start. Print its password:

```bash
docker compose logs codegen | grep -A3 administrator
```

Then open **http://localhost:7860**, sign in, and change the password from the
sidebar. (To choose the password up front instead, set `ADMIN_PASSWORD` in `.env`
before running `load.sh`.)

## 6. Everyday use

```bash
docker compose ps                 # service status and health
docker compose logs -f codegen    # follow the UI logs
docker compose stop               # stop (keeps data)
docker compose up -d              # start again
docker compose down               # stop and remove the containers (data is kept)
```

Chats and accounts live in `./data/chat_history.db`; the models stay in `./models`.
Both survive `down` and restarts.

**Settings** are in `.env` (copied from `.env.example`). After editing it:

```bash
docker compose up -d              # re-reads .env
```

Useful ones:

- `GPU_LAYERS` — how much of the coder model sits on the GPU (`99` fits it all if
  you have the VRAM; `20` is the safe default for a 6 GB card).
- `BANNED_WORDS` and `data/banned_words.txt` — words that are blocked outright
  (see the main `README.md`). The file is re-read without a restart.
- `ALLOW_SIGNUP` — let users create their own accounts on the sign-in page.

## 7. Manual steps (instead of `load.sh`)

```bash
# load the images
for f in images/*.tar.gz; do gunzip -c "$f" | docker load; done

# create the config
cp .env.example .env

# start
docker compose up -d
```

## 8. Updating to a newer bundle

Bring a new `codegen-gpu-offline-<date>.tar`, then:

```bash
cd codegen-gpu-offline-<old-date>
docker compose down                       # stop the old version
cp -r data ../keep-data                   # keep your chats and accounts
cd ../ && tar -xf codegen-gpu-offline-<new-date>.tar
cd codegen-gpu-offline-<new-date>
cp -r ../keep-data/* data/ 2>/dev/null || true   # restore chats and accounts
cp ../codegen-gpu-offline-<old-date>/.env .      # keep your settings
./load.sh
```

The database upgrades itself on first start and keeps a backup next to it.

## 9. Troubleshooting

- **`docker run --gpus all` fails / "could not select device driver".** The NVIDIA
  Container Toolkit is missing or the daemon was not restarted after installing it.
- **The UI says a service is unreachable.** Check `docker compose ps`; `codegen`
  waits for `llama-server` and `guard` to report healthy (about a minute on first
  start). `docker compose logs llama-server` shows model loading.
- **Out of GPU memory.** Lower `GPU_LAYERS` in `.env` and `docker compose up -d`.
- **Port 7860 is in use.** Change the UI port mapping in `docker-compose.yml`.
- **Lost the admin password.** `docker compose exec codegen python users.py reset-password admin`.

## 10. Security notes

- The model server is published on `127.0.0.1` only, so no one on the network can
  reach the model directly and bypass the filter. The guard service has no
  published port.
- The UI (`:7860`) requires sign-in. If it is reachable beyond a trusted network,
  put it behind HTTPS and set `COOKIE_SECURE=true` in `.env`.
