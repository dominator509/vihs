#!/usr/bin/env python3
"""EP-009 M5 rollback drill: deploy the CURRENT release tag, smoke; roll
staging back ONE tag, smoke again and time the rollback; then restore the
current release. Validates ROLLBACK.md / DEPLOYMENT.md steps as written.

Flow (each leg reuses staging-deploy.py, which ALWAYS terminates its pod):
  1. deploy <current>  -> smoke OK (release assertion: <current>)
  2. deploy <previous> -> smoke OK (release assertion: <previous>)  <-- ROLLBACK
  3. deploy <current>  -> smoke OK (release assertion: <current>)    <-- RESTORE

The release assertion (--expect-release) proves WHICH image is live: the pod
registers versions.pod from VIHS_RELEASE, visible in GET /admin/pods.

Env:
  VIHS_RUNPOD_IMAGE_CURRENT  (default ttl.sh/vihs-pod-slimlauncher:v0.2.0)
  VIHS_RUNPOD_IMAGE_PREVIOUS (default ttl.sh/vihs-pod-slimlauncher:v0.1.0)
  VIHS_ROLLBACK_KEEP_RESTORED (default 1; set 0 to leave staging on the
                               rolled-back tag — drill ends at previous)
Rollback timing: measured from the moment the previous-tag deploy starts to
its SMOKE OK (includes pod create + ready + smoke). Validation gate: <= 600 s
per EP-009 M5.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "runpod" / "staging-deploy.py"

CURRENT = os.environ.get("VIHS_RUNPOD_IMAGE_CURRENT", "ttl.sh/vihs-pod-slimlauncher:v0.2.0")
PREVIOUS = os.environ.get("VIHS_RUNPOD_IMAGE_PREVIOUS", "ttl.sh/vihs-pod-slimlauncher:v0.1.0")
KEEP_RESTORED = os.environ.get("VIHS_ROLLBACK_KEEP_RESTORED", "1") == "1"

ROLLBACK_BUDGET_S = 600


def run_leg(tag: str, expected_release: str, label: str) -> tuple[int, float]:
    t0 = time.monotonic()
    # VIHS_RELEASE is what the pod registers as versions.pod. The v0.1.0
    # image (M4 state) HARDCODES "0.1.0" and ignores the env; v0.2.0 reads
    # the env. expected_release must equal what the image actually reports.
    env = {**os.environ, "VIHS_RUNPOD_IMAGE": tag, "VIHS_RELEASE": expected_release}
    print(f"\n=== {label}: deploying {tag} (expect release {expected_release}) ===")
    proc = subprocess.run(
        [sys.executable, str(DEPLOY)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=2700,
    )
    elapsed = time.monotonic() - t0
    print(proc.stdout[-4000:])
    if proc.stderr:
        print("stderr:", proc.stderr[-2000:])
    return proc.returncode, elapsed


def main() -> int:
    print(f"EP-009 M5 rollback drill: current={CURRENT} previous={PREVIOUS}")

    rc, t_cur = run_leg(CURRENT, "v0.2.0", "LEG 1 current")
    if rc != 0:
        print(f"drill: CURRENT deploy failed rc={rc} — aborting", file=sys.stderr)
        return 1

    t0 = time.monotonic()
    # v0.1.0 (M4 image) HARDCODES "0.1.0" in the agent — assert exactly that.
    rc, t_prev = run_leg(PREVIOUS, "0.1.0", "LEG 2 ROLLBACK")
    rollback_s = time.monotonic() - t0
    if rc != 0:
        print(f"drill: ROLLBACK deploy failed rc={rc}", file=sys.stderr)
        return 2
    print(f"rollback: previous-tag deploy OK in {rollback_s:.1f}s")

    if not KEEP_RESTORED:
        print("drill: VIHS_ROLLBACK_KEEP_RESTORED=0 — staging left on previous tag")
        print(f"ROLLBACK_OK elapsed_s={rollback_s:.1f}")
        return 0 if rollback_s <= ROLLBACK_BUDGET_S else 3

    rc, t_rest = run_leg(CURRENT, "v0.2.0", "LEG 3 RESTORE")
    if rc != 0:
        print(f"drill: RESTORE deploy failed rc={rc}", file=sys.stderr)
        return 4

    print(f"\nleg1_current_s={t_cur:.1f} rollback_s={rollback_s:.1f} restore_s={t_rest:.1f}")
    print(f"ROLLBACK_OK elapsed_s={rollback_s:.1f} budget_s={ROLLBACK_BUDGET_S}")
    if rollback_s > ROLLBACK_BUDGET_S:
        print("ROLLBACK_BUDGET_EXCEEDED", file=sys.stderr)
        return 3
    print("ROLLBACK DRILL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
