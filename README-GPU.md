# Offline CodeGen GPU migration - V2

This version uses the latest app(1).py as app/app.py and includes filter.py.

1. Verify GPU access:
   docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu22.04 nvidia-smi

2. Put your GGUF model in ./models/

3. Create .env:
   cp .env.example .env

4. Set MODEL_FILE in .env to the exact GGUF filename.

5. Validate and build:
   docker compose config
   docker compose pull llama-server
   docker compose build --no-cache codegen

6. Start inference first:
   docker compose up -d llama-server
   docker compose logs -f llama-server

7. Test:
   curl http://localhost:8080/health
   curl http://localhost:8080/v1/models

8. Start UI:
   docker compose up -d codegen

9. Open:
   http://localhost:7860
