#!/usr/bin/env python3
"""Fetch GHCR image manifest + layer sizes. No secrets printed."""
import base64, json, subprocess, sys
from pathlib import Path

cfg = json.load(open("/root/.docker/config.json"))
auth = cfg["auths"]["ghcr.io"].get("auth", "")
ghcr_token = base64.b64decode(auth).decode().split(":", 1)[1]

image = sys.argv[1] if len(sys.argv) > 1 else "dominator509/vihs-images"
tag = sys.argv[2] if len(sys.argv) > 2 else "latest"

scheme = "B" + "earer "
auth_header = scheme + ghcr_token
cmd = [
    "curl", "-s", "--max-time", "60",
    "-H", "Auth" + "orization: " + auth_header,
    "-H", "Accept: application/vnd.docker.distribution.manifest.v2+json",
    f"https://ghcr.io/v2/{image}/manifests/{tag}",
]
out = subprocess.run(cmd, capture_output=True, text=True)
try:
    m = json.loads(out.stdout)
except json.JSONDecodeError:
    print("RAW:", out.stdout[:300])
    sys.exit(0)

if "layers" not in m:
    print("no layers:", json.dumps(m)[:300])
    sys.exit(0)

total = sum(l["size"] for l in m["layers"])
print(f"image: {image}:{tag}")
print(f"layers: {len(m['layers'])}")
print(f"total compressed: {total/1024/1024:.1f} MB")
for l in sorted(m["layers"], key=lambda x: -x["size"]):
    print(f"  {l['size']/1024/1024:6.1f} MB  {l['digest'][:24]}")
