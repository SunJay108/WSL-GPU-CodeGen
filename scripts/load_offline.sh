#!/usr/bin/env bash
# Load the offline bundle and start CodeGen. Run it from the extracted bundle
# folder: ./load.sh    (see OFFLINE-GUIDE.md)
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

LLAMA_IMAGE="ghcr.io/ggml-org/llama.cpp:server-cuda"
MODELS=(qwen2.5-coder-7b-instruct-q4_k_m.gguf Qwen3Guard-Gen-0.6B Qwen3Guard-Stream-0.6B)

step() { printf '\n==> %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

step "Checking prerequisites"
command -v docker >/dev/null || die "Docker is not installed."
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (the 'docker compose' command)."

step "Loading Docker images"
shopt -s nullglob
images=(images/*.tar.gz)
[ ${#images[@]} -gt 0 ] || die "No images/*.tar.gz found. Are you in the extracted bundle folder?"
for f in "${images[@]}"; do
    echo "   $f"
    gunzip -c "$f" | docker load
done

step "Checking models"
for m in "${MODELS[@]}"; do
    [ -e "models/$m" ] || die "Missing models/$m"
done
echo "   all models present"

step "Verifying GPU access"
if docker run --rm --gpus all --entrypoint nvidia-smi "$LLAMA_IMAGE" -L >/dev/null 2>&1; then
    echo "   GPU OK"
else
    echo "   WARNING: 'docker run --gpus all' could not reach a GPU."
    echo "   Install the NVIDIA Container Toolkit and restart Docker, then run ./load.sh again."
fi

if [ ! -f .env ]; then
    cp .env.example .env
    step "Created .env from .env.example"
fi

step "Starting CodeGen"
docker compose up -d

cat <<'EOF'

CodeGen is starting (about a minute while the models load).

  Status:          docker compose ps
  Admin password:  docker compose logs codegen | grep -A3 administrator
  Open:            http://localhost:7860

EOF
