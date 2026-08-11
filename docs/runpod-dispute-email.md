# RunPod Billing Dispute — Draft Email

To: billing@runpod.io / support@runpod.io (via https://www.runpod.io/contact)
From: doministic@gmail.com
Subject: Billing dispute — 20 unusable pods billed while never becoming
operational (Aug 11, 2026, US-IL-1 RTX 4090) — refund requested

---

Hello RunPod Support,

I am requesting a refund for pod time that was billed while the instances
were never usable. On 2026-08-11 I attempted to deploy a workload on an
RTX 4090 in US-IL-1 (SECURE cloud). Over the course of the day, **20 pods
were created and billed, and not one of them ever became operational** —
no container logs, no application registration, no listening port, no
agent startup. Total unusable billed time: **~5 hours 51 minutes** (351
minutes). I am attaching logs and evidence below.

## Summary of the problem

- Every pod reached `desiredStatus=RUNNING` and started billing
  immediately (per-second billing from creation, per RunPod's own
  billing documentation).
- None of the 20 pods ever ran my workload: the container either
  crash-looped (uptime non-monotonic), stalled indefinitely during image
  pull / dependency installation, or produced zero console output while
  the port map never became reachable.
- The same image and configuration **booted successfully in ~110 seconds
  on a healthy node earlier the same day** (pod 8r3rty40np6d19, for
  contrast). So the failure is not in our image or configuration — it is
  node-side / platform-side.
- RunPod's own optimization documentation states: "If cold start exceeds
  7 minutes, the worker is marked unhealthy." All 20 pods exceeded this
  threshold or never became reachable at all — several hung 3-6x longer
  before we terminated them.

## Incident table (all times local, 2026-08-11)

| # | Pod ID | Created | Killed | Billed | Symptom |
|---|--------|---------|--------|--------|---------|
| 1 | ryqiz3rqtqs9kd | 18:06 | 18:52 | ~46 min | Image pulled, then wheel install from our operator mirror stalled 39 min at a 2.4MB file; container crash-looped (uptime 223s→210s→400s→8s); agent never started |
| 2 | ab9mgl4xxp33r2 | 19:04 | 19:40 | ~36 min | Zero console logs, crash-loops (uptime 461s→291s→131s), port 8093 never reachable (502) |
| 3 | e83mgcakbwb877 | 19:41 | 20:08 | ~27 min | Zero logs, crash-restart at 673s, never registered |
| 4 | 4lby9klwrazkav | 20:07 | 22:1x | ~131 min | Ports assigned but no agent registration, zero report lines — killed after 2h+ unwatched (loop downtime) |
| 5 | gqwksjzmljp3ds | 20:38 | 20:48 | ~10 min | No registration, no logs after creation — killed per 7-min rule |
| 6 | 2er50ruc3lvgwb | 20:48 | 20:55 | ~7 min | Never Ready within 420s — no registration |
| 7 | 14bngamekc9ypj | 20:56 | 21:03 | ~7 min | Never Ready within 420s |
| 8 | 0yex0vd8eqbj8e | 21:05 | 21:12 | ~7 min | Never Ready within 420s |
| 9 | 0oauaxclyf0bss | 21:14 | 21:20 | ~7 min | Never Ready within 420s |
| 10 | d8fxsgvyn40tur | 21:24 | 21:31 | ~7 min | Never Ready within 420s |
| 11 | 55yhg271r7mooi | 21:32 | 21:39 | ~7 min | Never Ready within 420s |
| 12 | utaxwo3ydm3ban | 21:41 | 21:48 | ~7 min | Never Ready within 420s |
| 13 | r94blg8ng6ceat | 21:51 | 22:02 | ~7 min | Never Ready within 420s |
| 14 | op92y3ud0xtgfl | 22:04 | 22:11 | ~7 min | Never Ready within 420s |
| 15 | 4rnnus91ojg7rg | 22:13 | 22:20 | ~7 min | Never Ready within 420s |
| 16 | t7v1oa87uxjaj5 | 22:22 | 22:28 | ~7 min | Never Ready within 420s |
| 17 | bwrs77wzd69sf1 | 22:30 | 22:37 | ~7 min | Never Ready within 420s |
| 18 | 1sxfyrnnr9dyw6 | 22:39 | 22:45 | ~7 min | Never Ready within 420s |
| 19 | fglyqvyhsxtudx | 22:47 | 22:54 | ~7 min | Never Ready within 420s |
| 20 | hpcfs3fzpqvses | 22:56 | 22:59 | ~3 min | Never Ready; terminated when we paused |

**Total: 20 pods, ~351 minutes (~5h51m) of billed-but-unusable time.**

## Evidence

1. **Full incident log with per-incident evidence links** (committed to our
   repo, can share on request): `docs/runpod-issues-log.md` — includes a
   dispute summary table, research appendix, and raw console captures.
2. **Raw RunPod console dump for pod ryqiz3rqtqs9kd** (61KB, SSE stream):
   shows the image layer pull completing at 16:22Z, then a **39-minute gap**
   before pip wheel fetches at 17:01Z stalling at `pylibsrtp`
   (2.4MB) from our operator mirror — node egress to a normally-fast host
   stalled mid-install. Healthy pods fetch the same file in <1s.
3. **Uptime monitor for pod e83mgcakbwb877**: 30-second poll deltas showing
   uptime crash-restarting (…673s → 19s…).
4. **Empty logs payload for pod ab9mgl4xxp33r2**: RunPod console returned
   nothing while the pod reported RUNNING.
5. **Orchestrator readiness polls**: repeated `/admin/pods` queries showing
   no pod ever reaching `state=ready`.
6. **Our report server log** (`/tmp/pod-reports.log`): zero lines from any
   of the failed pods — the application never started.

## Policy references (RunPod's own documentation)

- **Billing** (https://docs.runpod.io/accounts-billing/billing): "All
  compute and storage charges are billed per second" — charged from pod
  creation, including image pull / initialization time, with **no grace
  period** for pods that never become usable.
- **Optimization** (https://docs.runpod.io/serverless/development/optimization):
  "If cold start exceeds 7 minutes, the worker is marked unhealthy."
  Our pods hung 3-6x that long or never became reachable.

## Community reports (same symptom, for context)

- https://www.answeroverflow.com/m/1319757071448543346 — "Throttled
  download speed from container registry while still being billed"
  (~45 min image pull, billed while waiting)
- https://www.answeroverflow.com/m/1400148062248239104 — "Registry
  fetching extremely slow" (60+ min pulls, billed throughout)
- https://www.reddit.com/r/RunPod/comments/1s2be4u/ — "Is anyone having
  to stop and start pods over and over to get them running correctly?"
- https://github.com/runpod-workers/worker-vllm/issues/111 — "Very slow
  cold starts even with flashboot" (RunPod staff acknowledged)
- https://pierce.dev/notes/speeding-up-runpod — "varying runtime
  performance box-to-box"

## Refund requested

I am requesting a **full refund of the unusable instance time listed
above (~5h51m of RTX 4090 US-IL-1 SECURE cloud time)**. These instances
were billed per-second from creation while never running any workload.
The failure pattern (20 consecutive bad nodes, same image that boots in
~110s on a healthy node) indicates a platform-side / node-quality issue,
not a customer-side one.

Please credit the account (doministic@gmail.com) for the pods listed
above. I'm happy to provide the raw evidence files or additional
console dumps if useful.

Thank you for your time.

— Dominic
