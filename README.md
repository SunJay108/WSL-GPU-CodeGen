# CodeGen: offline GPU coding assistant

A ChatGPT-style web app for programming help that runs entirely on your own machine.

- **Chat UI**: sidebar with saved chats, streaming answers, code blocks with copy buttons, light and dark themes.
- **Accounts**: sign-in page, `admin` and `user` roles, change-password menu.
- **Admin panel** (`/admin`): manage users, browse and delete chat history, review the moderation log.
- **Code-only filter**: every question and every answer is checked by Qwen3Guard and by a topic classifier. Anything unsafe or unrelated to software development is blocked.

## Architecture

```
Browser ──► codegen-ui  (Gradio + FastAPI, :7860)
               ├──► llama-server  Qwen2.5-Coder-7B GGUF on the GPU (127.0.0.1:8080 only)
               │       slot 0: chat · slot 1: topic classifier
               └──► guard         Qwen3Guard-Gen-0.6B + Qwen3Guard-Stream-0.6B (internal :8090)
SQLite (./data/chat_history.db): users, sessions, chats, moderation log
```

## Requirements

- NVIDIA GPU with a recent driver, Docker with the NVIDIA Container Toolkit (works under WSL2).
- The three models in `./models` (nothing is downloaded at runtime):

```
models/
├── qwen2.5-coder-7b-instruct-q4_k_m.gguf
├── Qwen3Guard-Gen-0.6B/        # Hugging Face folder: config.json, model.safetensors, tokenizer files
└── Qwen3Guard-Stream-0.6B/     # same, plus modeling_qwen3_guard.py and configuration_qwen3.py
```

## Setup

1. Check GPU access:
   ```
   docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu22.04 nvidia-smi
   ```
2. Copy the models into `./models` as shown above.
3. Create `.env`:
   ```
   cp .env.example .env
   ```
   Set `ADMIN_PASSWORD` for the first admin account, or leave it empty to have one generated.
4. Build and start:
   ```
   docker compose build
   docker compose up -d
   ```
   The UI waits until llama-server and the guard models report healthy (about a minute).
5. On first start, the admin account is printed in the logs:
   ```
   docker compose logs codegen | grep -A3 "administrator"
   ```
6. Open http://localhost:7860, sign in, and change the password from the user menu.

## How filtering works

| Stage | Check | Blocks |
|---|---|---|
| Question | Qwen3Guard-Gen-0.6B | Unsafe content and jailbreak attempts (and *Controversial*, see `BLOCK_CONTROVERSIAL`) |
| Question | Qwen3Guard-Stream-0.6B (query head) | Unsafe content |
| Question | Coder model as a classifier (grammar-constrained) | Anything not about software development, and non-English questions (`REQUIRE_ENGLISH`) |
| Answer, while streaming | Qwen3Guard-Stream-0.6B, token by token | Unsafe output; text is shown only after it has been checked |
| Answer, when finished | Qwen3Guard-Gen-0.6B + topic classifier | Unsafe or off-topic answers, which are withheld |

If a check cannot run (for example, the guard service is down), the message is blocked, not let through. Blocked exchanges are not saved to chat history; they are recorded in the admin panel's **Moderation log**.

## Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `MODEL_FILE` | `qwen2.5-coder-7b-instruct-q4_k_m.gguf` | GGUF file in `./models` |
| `GPU_LAYERS` | `20` | Layers offloaded to the GPU (the 7B model has 28; use `99` if VRAM allows) |
| `CTX_SIZE` | `8192` | Total llama.cpp context, split across `LLAMA_PARALLEL` slots |
| `LLAMA_PARALLEL` | `2` | Slot 0 serves chat, slot 1 the topic classifier |
| `CPU_THREADS` | `8` | llama.cpp CPU threads |
| `GUARD_DEVICE` | `cpu` | `cpu` or `cuda` for the Qwen3Guard models |
| `GUARD_THREADS` | `4` | CPU threads for the guard models (more does not help: they are memory-bound) |
| `GUARD_STREAM_INT8` | `false` | On CPU, run Qwen3Guard-Stream with int8 weights: answers stream faster, but in testing one prompt-level verdict changed (Qwen3Guard-Gen, which always runs in full precision, still blocked it) |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / generated | First admin, created only when no users exist |
| `REQUIRE_ENGLISH` | `true` | Reject questions not written in English |
| `BLOCK_CONTROVERSIAL` | `true` | Also block content Qwen3Guard rates *Controversial* |
| `COOKIE_SECURE` | `false` | Set to `true` when serving over HTTPS |

### Building the guard image offline

The guard image needs PyTorch (about 180 MB) plus about 30 smaller packages. When `guard/wheels/` holds all of them, the build installs from that folder and needs no network. To fill it on a machine with internet access (Python 3.11, Linux x86_64 wheels):

```
pip download --only-binary=:all: --python-version 3.11 --platform manylinux_2_28_x86_64 \
    -d guard/wheels -r guard/requirements.txt torch==2.8.0 \
    --extra-index-url https://download.pytorch.org/whl/cpu
```

If only some wheels are present, the build uses them and downloads the rest.

### Running the guard models on the GPU

On a 6 GB card the 7B model leaves too little VRAM, so the guard models run on the CPU by default. With more VRAM, build the guard with a CUDA PyTorch wheel and set `GUARD_DEVICE=cuda`. Either put the CUDA wheel in `guard/wheels/` in place of the CPU one, or pass its URL (used only when `guard/wheels/` holds no wheel):

```
docker compose build --build-arg TORCH_WHEEL_URL=https://download.pytorch.org/whl/cu126/torch-2.8.0%2Bcu126-cp311-cp311-manylinux_2_28_x86_64.whl guard
```

## Upgrading from the single-user version

On first start the app migrates `data/chat_history.db` in place: it saves a backup as `chat_history.db.v0.bak`, adds the account tables, and assigns all existing chats to the first admin.

## Security notes

- llama-server is published on `127.0.0.1` only, so nobody on the network can bypass the filter by calling the model directly. The guard service has no published port.
- The UI listens on all interfaces (`:7860`) and requires sign-in. Put it behind HTTPS (and set `COOKIE_SECURE=true`) if it is reachable beyond a trusted network.
- Five failed sign-ins for the same user from the same address lock that combination for 15 minutes.
