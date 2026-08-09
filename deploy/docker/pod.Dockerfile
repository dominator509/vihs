# syntax=docker/dockerfile:1
# VIHS pod image — data-plane agent (EP-009 M1).
# CUDA base for real-stage GPU inference; mock stages run in the same image.
# Weights are NEVER baked in — mounted read-mostly at VIHS_MODEL_DIR (network
# volume). Layers ordered for cache: base → system deps → venv deps → code.

FROM nvidia/cuda:12.9.2-base-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VIHS_MODEL_DIR=/workspace/models

# System deps: Python 3.12 (venv+pip), GStreamer 1.24 for the mux stage
# (ENVIRONMENT.md: GStreamer 1.22+ pod image only), PyGObject bindings,
# curl for health probes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        python3-pip \
        python3-gi \
        gstreamer1.0-tools \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        libgstreamer1.0-0 \
        gir1.2-gstreamer-1.0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# venv + runtime deps (pinned to requirements.lock runtime subset — dev tools
# pytest/mypy/ruff stay out of the image; Decision Log EP-009 M1).
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
RUN pip install --upgrade pip \
    && pip install \
        aiortc==1.15.0 \
        av==17.1.0 \
        websockets==17.0.1 \
        numpy==2.5.1 \
        httpx==0.28.1 \
        faster-whisper==1.2.1 \
        piper-tts==1.6.0

# Pod package from source (setuptools>=68 per pyproject).
COPY pod/ /workspace/pod/
RUN pip install /workspace/pod

# Model volume mount point (weights never in the image).
RUN mkdir -p /workspace/models

EXPOSE 8093
CMD ["python", "-m", "vihs_pod.agent"]
