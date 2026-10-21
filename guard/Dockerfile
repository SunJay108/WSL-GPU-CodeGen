FROM python:3.11.9-slim

# Python packages are installed from guard/wheels/ when it holds every wheel
# (fully offline build). Otherwise PyTorch is downloaded from TORCH_WHEEL_URL and
# the rest from PyPI. CPU build by default; for a GPU build use the CUDA wheel, e.g.
# https://download.pytorch.org/whl/cu126/torch-2.8.0%2Bcu126-cp311-cp311-manylinux_2_28_x86_64.whl,
# and set GUARD_DEVICE=cuda in .env.
ARG TORCH_WHEEL_URL=https://download.pytorch.org/whl/cpu/torch-2.8.0%2Bcpu-cp311-cp311-manylinux_2_28_x86_64.whl

ENV PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HOME=/tmp/hf \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10

WORKDIR /guard

COPY wheels/ /tmp/wheels/
COPY requirements.txt /guard/requirements.txt

# The PyTorch download is resumable: its CDN can stall on slow links, which pip cannot recover from.
RUN if ! ls /tmp/wheels/torch-*.whl >/dev/null 2>&1; then \
        apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/* \
        && curl -fL --retry 30 --retry-all-errors --retry-delay 5 --speed-limit 1024 --speed-time 60 -C - \
            -o "/tmp/wheels/$(basename "${TORCH_WHEEL_URL}" | sed 's/%2B/+/')" "${TORCH_WHEEL_URL}"; \
    fi \
    && (pip install --no-cache-dir --no-index --find-links /tmp/wheels "torch==2.8.0" -r /guard/requirements.txt \
        || pip install --no-cache-dir --find-links /tmp/wheels "torch==2.8.0" -r /guard/requirements.txt) \
    && rm -rf /tmp/wheels

COPY server.py /guard/server.py

EXPOSE 8090

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8090"]
