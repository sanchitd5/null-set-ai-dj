# syntax=docker/dockerfile:1.7
# NULL::SET AI DJ: one container = FastAPI backend + the console frontend (served from app/ui/static).
# DRAFT (branch package-plan): not built on CI yet, see docs/engineering/PACKAGING_PLAN.md.
#
#   CPU (linux/amd64 + linux/arm64):  docker buildx build --platform linux/amd64,linux/arm64 -t aidj:cpu .
#   CUDA (linux/amd64 only):          docker build --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu130 -t aidj:cuda .

ARG PYTHON_VERSION=3.14
ARG DENO_VERSION=2.9.7

# yt-dlp needs an external JS runtime for YouTube; Deno is the one it enables by default.
FROM denoland/deno:bin-${DENO_VERSION} AS deno

# ---- build: a venv with every Python dep, copied whole into the runtime stage
FROM python:${PYTHON_VERSION}-slim-trixie AS build
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
# torch first, from the CPU or CUDA index, so demucs does not pull the default (CUDA on amd64) wheel.
# Versions = what the owner's Mac runs today (torch 2.14.0, demucs 4.1.0, yt-dlp-ejs 0.8.0).
RUN pip install --index-url "$TORCH_INDEX" torch==2.14.0
COPY requirements-ci.txt /tmp/requirements-ci.txt
# requirements-ci.txt = the pins the test suite runs on (Python 3.14). pytest comes along (small, unused).
RUN pip install -r /tmp/requirements-ci.txt demucs==4.1.0 yt-dlp-ejs==0.8.0

# ---- runtime
FROM python:${PYTHON_VERSION}-slim-trixie
# ffmpeg: decode / mp3 export / yt-dlp post-processing. rubberband-cli: keylock stretch.
# tini: PID 1 that reaps the Demucs / stem_worker children.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg rubberband-cli tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=build /opt/venv /opt/venv

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin aidj \
 && mkdir -p /data/cache /data/models \
 && chown -R aidj:aidj /data

WORKDIR /app
COPY --chown=root:root . /app
# The code still writes a few things under <repo>/data (songs/, output/hook_drops/).
# Point that at the volume so the image itself stays read-only for the app user.
RUN rm -rf /app/data && ln -s /data /app/data

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AIDJ_CACHE_DIR=/data/cache \
    HF_HOME=/data/models/hf \
    TORCH_HOME=/data/models/torch \
    LLM_BACKEND=ollama \
    OLLAMA_URL=http://host.docker.internal:11434 \
    AI_REVIEW_BACKEND=local

USER aidj
VOLUME ["/data"]
EXPOSE 8000
# /api/recipes is what start.sh already polls; there is no /api/health yet.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/recipes', timeout=4)"]

ENTRYPOINT ["/usr/bin/tini", "--"]
# 0.0.0.0 inside the container only: the host publishes it on 127.0.0.1 (see docker-compose.yml).
CMD ["python", "-m", "uvicorn", "app.ui.server:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info", "--no-access-log"]
