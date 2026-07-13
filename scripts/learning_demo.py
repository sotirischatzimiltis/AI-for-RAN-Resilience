"""
Learning demo — does experience-based auto-engagement block more of the botnet?

One multi-storm scenario, same seed, real LLM. Compares:
  • baseline      : no learning (filter waits on the LLM verdict) — the control.
  • learn ep1..N  : within + across learning ON, run as a sequence from a clean
                    slate. Episode 1 learns during its first storm (storms 2-3 then
                    auto-engage); episodes 2+ start PRIMED from the persisted
                    signature, so even storm 1 is met fast.

Reports botnet-blocked rate, benign-served rate and P per run. The story:
blocked rate should jump from baseline -> episode 1 (within-episode) -> episode 2
(across-episode priming), while benign-served stays ~1.0.

Usage:
    python -m scripts.learning_demo --model openrouter:openai/gpt-4o-mini --episodes 3
"""

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.server import mcp, MCP_HOST, MCP_PORT
from agents.orchestrator import run_episode
from event_calendar import ScheduledEvent
from scripts.run import resolve_model
from policy_store import DEFAULT_PATH, STORM_MEMORY_PATH

SEED = 3
ONSET = 60.0  # multi_storm first storm


def _clean():
    Path(DEFAULT_PATH).unlink(missing_ok=True)
    Path(STORM_MEMORY_PATH).unlink(missing_ok=True)


async def _run(model, rt_factor, interval, learn):
    calendar = [ScheduledEvent(t_start=ONSET, name="scheduled mass event", severity="high")]
    return await run_episode(
        model=model, scenario="multi_storm", seed=SEED, c_max=16,
        rt_factor=rt_factor, poll_interval_s=1.0, assessment_interval_s=interval,
        calendar=calendar, learn_within=learn, learn_across=learn,
    )


async def main(args: argparse.Namespace) -> None:
    model = resolve_model(args.model)

    print(f"[demo] Starting MCP server on {MCP_HOST}:{MCP_PORT} ...")
    server_task = asyncio.create_task(
        mcp.run_http_async(host=MCP_HOST, port=MCP_PORT, show_banner=False, log_level="warning")
    )
    await asyncio.sleep(1.5)

    rows = []
    t0 = time.monotonic()

    # baseline: no learning
    _clean()
    r = await _run(model, args.rt_factor, args.assessment_interval, learn=False)
    rows.append(("baseline (no learn)", r))
    print(f"[demo] baseline           blocked={r['malicious_blocked_rate']:.3f}  "
          f"benign={r['benign_success_rate']:.3f}  P={r['final_P']:.3f}")

    # learning sequence from a clean slate
    _clean()
    for ep in range(1, args.episodes + 1):
        r = await _run(model, args.rt_factor, args.assessment_interval, learn=True)
        rows.append((f"learn episode {ep}", r))
        sm = r.get("storm_memory") or {}
        print(f"[demo] learn ep{ep}          blocked={r['malicious_blocked_rate']:.3f}  "
              f"benign={r['benign_success_rate']:.3f}  P={r['final_P']:.3f}  "
              f"(learned={sm.get('learned')}, storms_seen={sm.get('storms_seen')})")

    elapsed = time.monotonic() - t0
    print("\n" + "=" * 68)
    print(f"LEARNING DEMO  (multi_storm, seed={SEED}, model={args.model})")
    print("=" * 68)
    print(f"{'run':22s}  {'blocked':>8s}  {'benign':>7s}  {'P':>6s}")
    for label, r in rows:
        print(f"{label:22s}  {r['malicious_blocked_rate']:8.3f}  "
              f"{r['benign_success_rate']:7.3f}  {r['final_P']:6.3f}")
    print(f"\n  botnet blocked: baseline {rows[0][1]['malicious_blocked_rate']:.3f}  ->  "
          f"final {rows[-1][1]['malicious_blocked_rate']:.3f}")
    print(f"  wall time: {elapsed:.0f}s")
    print("=" * 68)

    logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Experience-based auto-engagement learning demo")
    p.add_argument("--model", default="openrouter:openai/gpt-4o-mini")
    p.add_argument("--episodes", type=int, default=3)
    p.add_argument("--rt-factor", type=float, default=6.0, dest="rt_factor")
    p.add_argument("--assessment-interval", type=float, default=8.0, dest="assessment_interval")
    args = p.parse_args()
    asyncio.run(main(args))
