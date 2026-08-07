#!/usr/bin/env sh
# Build release artifacts. DOCKER=1 also builds the pod image.
set -eu
cd "$(dirname "$0")/.."
if [ -f Cargo.toml ]; then cargo build --workspace --release; else echo "build: SKIP rust"; fi
if [ -d pod/.venv ] && [ -f pod/pyproject.toml ]; then
  pod/.venv/bin/pip install --quiet build 2>/dev/null || true
  ( cd pod && .venv/bin/python -m build --wheel --outdir ../target/pod-dist ) \
    || echo "build: NOTE pod wheel build unavailable (ok pre-EP-001)"
fi
if [ "${DOCKER:-0}" = "1" ] && [ -f deploy/docker/pod.Dockerfile ]; then
  sha="$(git rev-parse --short HEAD 2>/dev/null || echo dev)"
  docker build -f deploy/docker/pod.Dockerfile -t "vihs-pod:${sha}" .
fi
echo "BUILD OK"
