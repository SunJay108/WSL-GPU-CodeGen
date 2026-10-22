#!/usr/bin/env bash
# Build a self-contained offline bundle (source + Docker images + models + guide)
# as a single .tar, for installing on a machine with no internet.
#
#   scripts/export_offline.sh [OUTPUT_DIR]
#
# Run it where the images are built and the models are present. The images must
# already exist (docker compose build); this script does not build them.
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
OUT_DIR="${1:-$ROOT/dist}"
NAME="codegen-gpu-offline-$(date +%Y%m%d)"

IMAGES=(
    "ghcr.io/ggml-org/llama.cpp:server-cuda"
    "codegen-gpu-guard:latest"
    "codegen-gpu-codegen:latest"
)
MODELS=(qwen2.5-coder-7b-instruct-q4_k_m.gguf Qwen3Guard-Gen-0.6B Qwen3Guard-Stream-0.6B)

step() { printf '\n==> %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "Docker is not installed."
for img in "${IMAGES[@]}"; do
    docker image inspect "$img" >/dev/null 2>&1 || die "Missing image '$img'. Run: docker compose build"
done
for m in "${MODELS[@]}"; do
    [ -e "$ROOT/models/$m" ] || die "Missing models/$m"
done

# Stage under the project root so the models can be hard-linked (same filesystem,
# no extra disk) instead of copied.
STAGE="$(mktemp -d "$ROOT/.export.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
BUNDLE="$STAGE/$NAME"
mkdir -p "$BUNDLE/images" "$BUNDLE/models"

step "Copying source"
rsync -rlt \
    --exclude '.git' --exclude 'models' --exclude 'data' --exclude '.env' \
    --exclude '__pycache__' --exclude '*.pyc' --exclude 'guard/wheels' \
    --exclude 'dist' --exclude '.export.*' \
    --exclude '*:Zone.Identifier' --exclude '*.bak' \
    --exclude 'app_old.py' --exclude 'README-GPU.md' --exclude '.env - Copy*' \
    "$ROOT/./" "$BUNDLE/"
cp "$ROOT/scripts/load_offline.sh" "$BUNDLE/load.sh"
chmod +x "$BUNDLE/load.sh"

step "Hard-linking models (no extra disk used)"
cp -al "$ROOT/models/." "$BUNDLE/models/"

step "Saving Docker images (the slow part)"
: > "$BUNDLE/images/manifest.txt"
for img in "${IMAGES[@]}"; do
    file="$(echo "$img" | tr '/:' '__').tar.gz"
    echo "   $img -> images/$file"
    docker save "$img" | gzip -1 > "$BUNDLE/images/$file"
    echo "$img  images/$file" >> "$BUNDLE/images/manifest.txt"
done

step "Writing the tar to $OUT_DIR"
mkdir -p "$OUT_DIR"
tar -C "$STAGE" -cf "$OUT_DIR/$NAME.tar" "$NAME"
( cd "$OUT_DIR" && sha256sum "$NAME.tar" > "$NAME.tar.sha256" )

step "Done"
ls -lh "$OUT_DIR/$NAME.tar" "$OUT_DIR/$NAME.tar.sha256"
printf 'Top-level contents:\n'
tar -tf "$OUT_DIR/$NAME.tar" | awk -F/ '{print $1"/"$2}' | sort -u | head -40
