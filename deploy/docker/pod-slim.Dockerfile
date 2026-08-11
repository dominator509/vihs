# syntax=docker/dockerfile:1
# VIHS pod image — SLIM launcher (EP-009 M4 cold-node fix).
#
# WHY: cold-node pulls of the 379MB CUDA-fat image crawl (>45 min) on
# RunPod's network from both GHCR (>600s/50MB) and ttl.sh (~6s/MB).
# Docker Hub pulls are fast (26s/50MB) but we have no Docker Hub push
# creds. Instead: base this on python:3.12-slim FROM Docker Hub (small,
# so the *delta* we push to ttl.sh is small too — the whole image is
# ~100MB compressed ≈ 10 min pull at ttl.sh rates), and install the
# heavy pip deps AT CONTAINER START from the operator wheel mirror
# (http://66.94.123.250:8099/wheels-full/ — proven fast path: staging
# pods pulled 581MB from the same box).
#
# GStreamer stays baked in (mux stage needs it; apt on a fresh RunPod
# node is slow/unknown). libcudart is copied in (CUDA base provided it
# before; LD_LIBRARY_PATH already includes /usr/local/cuda/lib64).
# libcublas stays staged on the volume (/workspace/models/cublas).

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VIHS_MODEL_DIR=/workspace/models

# System deps: GStreamer 1.24 for the mux stage (ENVIRONMENT.md:
# GStreamer 1.22+ pod image only), PyGObject bindings, curl for probes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-gi \
        gstreamer1.0-tools \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        libgstreamer1.0-0 \
        gir1.2-gstreamer-1.0 \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# libcudart (was in nvidia/cuda base; python:3.12-slim has none).
# ctranslate2 dlopens libcudart.so.12 + libcublas.so.12 (volume-staged).
COPY deploy/docker/cudart/ /usr/local/cuda/lib64/

# Pod package from source (setuptools>=68 per pyproject). No pip deps
# here — installed at container start from the operator wheel mirror.
COPY pod/ /workspace/pod/
RUN pip install --no-deps /workspace/pod

# EP-010 M2 (bake): the runtime wheel closure (56 wheels, ~180MB) is baked
# into the image at build time so a cold boot NEVER pip-installs from the
# operator box. This removes the exact stall window that killed I-02 (node
# egress to our mirror stalled mid-install). The 1.36GB llama-cpp-python
# wheel and the 4.9GB GGUF + voice model are deliberately NOT baked (image
# would balloon to ~1.8GB and ttl.sh pulls would take hours on cold nodes);
# slim-boot.sh installs llama only when VIHS_LLAMA_GGUF is set, and the
# GGUF/voice persist on the network volume.
# The wheelhouse is fetched from the operator mirror at BUILD time (builds
# happen on the operator box; the mirror serves the full closure). It is
# removed after install so the wheel files do not ship in the image.
ARG VIHS_WHEEL_BASE=http://66.94.123.250:8099/wheels-full
RUN mkdir -p /opt/wheelhouse \
    && for f in $(curl -s "$VIHS_WHEEL_BASE/" | grep -oE 'href="[^"]+\.whl"' | sed 's|.*/||;s/"//'); do \
         case "$f" in llama_cpp_python*) continue;; esac; \
         curl -s -o "/opt/wheelhouse/$f" "$VIHS_WHEEL_BASE/$f" || exit 1; \
       done \
    && pip install --no-index --trusted-host localhost --find-links /opt/wheelhouse \
        aiortc==1.15.0 \
        av==17.1.0 \
        websockets==17.0.1 \
        numpy==2.5.1 \
        httpx==0.28.1 \
        faster-whisper==1.2.1 \
        piper-tts==1.6.0 \
    && touch /opt/vihs-wheels-installed \
    && rm -rf /opt/wheelhouse

# Bootstrap: fetch + install runtime wheels, then run the agent.
COPY deploy/docker/slim-boot.sh /usr/local/bin/slim-boot.sh
RUN chmod +x /usr/local/bin/slim-boot.sh

# Model volume mount point (weights never in the image).
RUN mkdir -p /workspace/models

EXPOSE 8093
CMD ["slim-boot.sh"]
