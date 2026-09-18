# ==============================================================================
# empire-orchestrator — multi-stage build with CPU-only torch
#
# Targets:
#   base         — system packages only (build-essential, curl, nodejs, npm)
#   deps         — Python packages incl. CPU-only torch  (~3.5–4 GB)
#   api-new      — new multi-tenant FastAPI app
#   api-old      — legacy single-tenant FastAPI app (optional, for migration)
#   telegram-bot — Telegram bot manager (subprocess-per-org)
#
# The heavy `deps` layer is built ONCE and reused by all three app targets,
# so the ~4 GB of Python packages is stored only once in the docker image
# cache, instead of being duplicated per service.
#
# Torch note: we pin torch==2.5.1+cpu because transformers 5.x (pulled in by
# sentence-transformers 6.x) requires torch >= 2.5. The `+cpu` suffix is
# mandatory — a plain `torch==2.5.1` would match PyPI's GPU wheel and drag
# back the ~3 GB CUDA/nvidia stack we're trying to avoid.
# ==============================================================================

# ── 1. System dependencies ────────────────────────────────────────────────────
FROM python:3.10-slim AS base

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/*

# ── 2. Python dependencies (incl. CPU-only torch) ─────────────────────────────
FROM base AS deps

COPY requirements.txt .

# Upgrade pip, then install CPU-only torch FIRST so subsequent installs see
# it as already satisfied. We use `--extra-index-url` (not `--index-url`) so
# that PyPI remains active as a fallback for pure-Python build deps like
# flit_core, packaging, wheel, etc. — which the PyTorch CPU mirror does NOT
# carry. The `+cpu` suffix guarantees we get the CPU wheel, not the GPU one.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        'torch==2.5.1+cpu' \
    && pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r requirements.txt

# ── 3. New multi-tenant API ───────────────────────────────────────────────────
FROM deps AS api-new

COPY . .

RUN mkdir -p /app/data/workspaces \
    && chmod +x run_mission.py

EXPOSE 8000 8001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]

# ── 4. Legacy single-tenant API (optional, for migration) ─────────────────────
FROM deps AS api-old

COPY . .

RUN mkdir -p /app/data/workspaces

EXPOSE 8000

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]

# ── 5. Telegram bot manager ───────────────────────────────────────────────────
FROM deps AS telegram-bot

COPY . .

RUN mkdir -p /app/data/workspaces

CMD ["python", "telegram_bridge_empire.py"]
