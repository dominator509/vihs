#!/usr/bin/env python3
"""EP-007 M4 — capacity + latency load harness (SPEC-008 P2/P3).

Derives `sessions_per_gpu` by ramping CONCURRENT scripted sessions on ONE
pod and reading per-stage first-chunk p95 from pod /health (SPEC-007 O1).

Contract (ExecPlan §7):
- Real mode (`VIHS_REAL_STAGES=1`, GPU host): ramps sessions until a
  first-chunk budget breach; prints `sessions_per_gpu = N (binding
  constraint: <stage|vram>)` and the evidence table.
- CI mode (mock stages): validates the harness only. The ramp must work,
  metrics must populate, and the budget-breach stop logic must be proven.
  Simulated latencies come from `VIHS_MOCK_*_MS` (pod env); the breach
  scenario (`VIHS_CAPACITY_SIM_BREACH=1`) forces one stage above its
  budget so the stop-at-first-breach path is exercised without a GPU.

Budget table: ARCHITECTURE §6 first-chunk targets (ms):
  llm_ttft        400 (target 150–400)
  tts_ttfa        300 (target 100–300)
  lipsync_ff      400 (target 100–400)
  e2e_first_frame 1500 (total well-pipelined 0.7–1.3 s; 1.5 s target)
  e2e_total       1500 (same target)
Overridable via `VIHS_CAPACITY_BUDGET_<STAGE>_MS`.

VRAM bound (ARCHITECTURE §13): sessions_per_gpu <= available/per-session.
CI mode defaults: 24 GB available, 3 GB/session (mock numbers, no GPU).
Real mode reads `VIHS_VRAM_AVAILABLE_GB`/`VIHS_VRAM_PER_SESSION_GB` (set
from nvidia-smi by the EP-010 staging run).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "e2e"))

from run_e2e import (  # noqa: E402
    POD_ADDR,
    api,
    bootstrap_tokens,
    ensure_services,
    stop_processes,
)

# ARCHITECTURE §6 first-chunk budgets (ms), env-overridable.
DEFAULT_BUDGETS_MS = {
    "llm_ttft": 400,
    "tts_ttfa": 300,
    "lipsync_ff": 400,
    "e2e_first_frame": 1500,
    "e2e_total": 1500,
}

STAGES = list(DEFAULT_BUDGETS_MS)


def budget_ms(stage: str) -> int:
    return int(
        os.environ.get(
            f"VIHS_CAPACITY_BUDGET_{stage.upper()}_MS", DEFAULT_BUDGETS_MS[stage]
        )
    )


def read_pod_health() -> dict:
    """GET the pod's local /health (aggregated per-stage metrics)."""
    with urllib.request.urlopen(f"http://{POD_ADDR}/health", timeout=5) as resp:
        return json.loads(resp.read())


async def wait_pod_ready(timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            health = read_pod_health()
            if health.get("stages") and health.get("fill", 0) == 0:
                return
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.25)
    raise RuntimeError("pod /health never became ready")


async def wait_pod_fill(expected: int, timeout: float = 20.0) -> None:
    """Wait until the pod reports the expected fill (revoke propagation)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if read_pod_health().get("fill", -1) == expected:
                return
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.25)
    raise RuntimeError(f"pod fill did not reach {expected}")


async def wait_metrics_settled(expected: int, timeout: float = 15.0) -> None:
    """Wait until the pod's aggregated metrics show `expected` e2e_total
    samples — the LAST stage of run_response, so its presence proves the
    whole pipeline (llm_ttft → tts_ttfa → lipsync_ff → e2e_first_frame →
    e2e_total) recorded for this stage's turns. Without this, reading
    /health right after the caption races the injected latency (SIM_BREACH
    sleeps 600ms in lipsync before the first frame)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            m = read_pod_health().get("metrics", {})
            if m.get("e2e_total", {}).get("count", 0) >= expected:
                return
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.25)
    raise RuntimeError(f"pod metrics never settled (e2e_total count < {expected})")


async def run_one_turn(peer) -> None:
    """One scripted turn on a connected client peer; waits for the caption."""
    await peer.say("Hello there.")
    await peer.wait_caption(timeout=15.0)


async def connect_peers(conn_ids: list[str]) -> list:
    """Fresh ClientPeer per connection (M1 lesson: one WS per peer; a closed
    peer cannot be reused) and connect all of them concurrently."""
    from run_e2e import ClientPeer  # noqa: PLC0415

    peers = [ClientPeer(cid) for cid in conn_ids]
    await asyncio.gather(*(p.connect(timeout=15.0) for p in peers))
    return peers


async def close_peers(peers: list) -> None:
    for p in peers:
        with contextlib.suppress(Exception):
            await p.close()


def stage_p95(health: dict, stage: str) -> float | None:
    m = health.get("metrics", {}).get(stage)
    if not m or m.get("count", 0) == 0:
        return None
    return float(m["p95"])


def derive_report(stage_rows: list[dict]) -> dict:
    """Return {sessions_per_gpu, constraint, rows} from the ramp evidence."""
    # VRAM bound (ARCHITECTURE §13).
    vram_avail = float(os.environ.get("VIHS_VRAM_AVAILABLE_GB", "24"))
    vram_per = float(os.environ.get("VIHS_VRAM_PER_SESSION_GB", "3"))
    vram_bound = int(vram_avail // vram_per) if vram_per > 0 else 10**9

    if not stage_rows:
        return {
            "sessions_per_gpu": 0,
            "constraint": "no-data",
            "rows": [],
            "vram_bound": vram_bound,
        }

    # The largest ramp stage whose every stage p95 stayed within budget.
    last_ok = 0
    for row in stage_rows:
        if row["ok"]:
            last_ok = row["n"]
        else:
            return {
                "sessions_per_gpu": row["n"] - 1,
                "constraint": row["breach_stage"],
                "rows": stage_rows,
                "vram_bound": vram_bound,
            }
    # No latency breach up to the tested cap → latency bound is at least the
    # cap; VRAM may bind first.
    latency_bound = last_ok
    n = min(latency_bound, vram_bound)
    constraint = (
        "vram"
        if vram_bound <= latency_bound
        else "cap (no latency breach in tested range)"
    )
    return {
        "sessions_per_gpu": n,
        "constraint": constraint,
        "rows": stage_rows,
        "vram_bound": vram_bound,
    }


def fmt_evidence(report: dict) -> str:
    head = f"{'stage':<18}" + "".join(
        f"p95@{r['n']:>2}".rjust(11) for r in report["rows"]
    )
    lines = [head, "-" * len(head)]
    for s in STAGES:
        row = f"{s:<18}"
        for r in report["rows"]:
            v = r["p95"].get(s)
            row += f"{'-' if v is None else f'{v:>7.0f}ms':>11}"
        lines.append(row)
    return "\n".join(lines)


def start_pod(sim_breach: bool, cap: int) -> subprocess.Popen:
    """Start the ONE pod under test; optionally force a stage over budget.

    `_start_pod_agent` builds pod_env from os.environ, so latency + cap
    overrides must be set there (restored by the caller's finally).
    """
    if sim_breach:
        os.environ["VIHS_MOCK_LIPSYNC_FF_MS"] = str(budget_ms("lipsync_ff") + 200)
    # The pod must be able to hold the whole ramp (default cap is 2).
    os.environ["POD_MAX_SESSIONS"] = str(cap)
    from run_e2e import _start_pod_agent  # noqa: PLC0415

    pod_proc, _log = _start_pod_agent("load-pod", mock_answers=["Understood."])
    if pod_proc is None:
        raise RuntimeError("pod failed to start")
    return pod_proc


async def main() -> int:
    sim_breach = os.environ.get("VIHS_CAPACITY_SIM_BREACH", "0") == "1"
    cap = int(os.environ.get("VIHS_CAPACITY_CAP", "3"))
    ramp = list(range(1, cap + 1))
    mode = "real" if os.environ.get("VIHS_REAL_STAGES", "0") == "1" else "mock"

    owned: list[subprocess.Popen] = []
    pod_proc: subprocess.Popen | None = None
    session_ids: list[str] = []
    peers: list = []

    try:
        owned = ensure_services()
        bootstrap_tokens()
        print(f"== loadtest: capacity derivation (mode: {mode}, cap: {cap}) ==")
        if sim_breach:
            print("  SIM_BREACH: forcing lipsync_ff above budget to prove stop logic")

        pod_proc = start_pod(sim_breach, cap)
        await wait_pod_ready()
        print("  pod ready (registered, fill=0)")

        stage_rows: list[dict] = []
        for n in ramp:
            # Create n concurrent sessions (all land on the one pod).
            created = []
            for _ in range(n):
                status, body = api(
                    "/v1/sessions", method="POST", body={"persona_id": "rl"}
                )
                if status != 201:
                    raise RuntimeError(f"create session {status} {body}")
                created.append(body["session_id"])
            session_ids.extend(created)

            # Connect every session concurrently; each needs a fresh peer.
            from run_e2e import _connect_session  # noqa: PLC0415

            conn_ids = [_connect_session(sid) for sid in created]
            peers = await connect_peers(conn_ids)
            # One concurrent turn per session = the load.
            await asyncio.gather(*(run_one_turn(p) for p in peers))
            # Wait for the full pipeline to record (e2e_total is the LAST
            # stage) — reading too early races injected latencies.
            await wait_metrics_settled(expected=n)

            # Read the pod's aggregated per-stage p95.
            health = read_pod_health()
            p95 = {s: stage_p95(health, s) for s in STAGES}
            breach = None
            for s in STAGES:
                v = p95[s]
                if v is not None and v > budget_ms(s):
                    breach = s
                    break
            ok = breach is None
            stage_rows.append({"n": n, "p95": p95, "ok": ok, "breach_stage": breach})
            parts = " ".join(
                f"{s}={p95[s]:.0f}ms" if p95[s] is not None else f"{s}=-"
                for s in STAGES
            )
            print(f"  stage {n}: {parts}" + ("" if ok else f"  BREACH ({breach})"))

            # Close this stage's peers and wait for the pod to drain before
            # the next stage (revoke propagation frees the fill slots).
            await close_peers(peers)
            peers = []
            await wait_pod_fill(0)
            if not ok:
                break

        report = derive_report(stage_rows)
        print()
        print(fmt_evidence(report))
        print()
        print(
            f"sessions_per_gpu = {report['sessions_per_gpu']} "
            f"(binding constraint: {report['constraint']})"
        )
        if sim_breach:
            if report["constraint"] != "lipsync_ff":
                raise RuntimeError(
                    f"SIM_BREACH expected lipsync_ff constraint, got {report['constraint']}"
                )
            print("  SIM_BREACH OK: harness stopped at first budget breach")
        print("LOADTEST OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"loadtest FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        await close_peers(peers)
        for sid in session_ids:
            with contextlib.suppress(Exception):
                api(f"/v1/sessions/{sid}", method="DELETE")
        if pod_proc is not None and pod_proc.poll() is None:
            with contextlib.suppress(Exception):
                pod_proc.send_signal(15)  # SIGTERM
        # Restore env overrides set by start_pod.
        for k in ("VIHS_MOCK_LIPSYNC_FF_MS", "POD_MAX_SESSIONS"):
            os.environ.pop(k, None)
        stop_processes(owned)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
