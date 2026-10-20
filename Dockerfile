# --- frontend build -------------------------------------------------------
FROM node:22-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build

# --- app --------------------------------------------------------------------
FROM python:3.12-slim
ARG WITH_DOCLING=1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy \
    HF_HOME=/models/hf
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.4.30 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock* README.md ./
COPY backend ./backend
RUN if [ "$WITH_DOCLING" = "1" ]; then uv sync --no-dev --extra docling; else uv sync --no-dev; fi
# Download Docling's layout/OCR weights at build time so nothing is fetched at runtime.
RUN if [ "$WITH_DOCLING" = "1" ]; then uv run docling-tools models download || true; fi
COPY config.yaml ./
COPY eval ./eval
COPY --from=ui /ui/dist ./frontend/dist
# no outbound calls at runtime
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
EXPOSE 8080
CMD ["uv", "run", "--no-sync", "form-filler", "serve"]
