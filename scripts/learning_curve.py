"""
Learning curve — does the agent improve as it RETAINS its tuned posture?

Runs N successive episodes on the SAME seed with knob persistence on, so the only
thing that changes episode-to-episode is the slow tuning posture the Non-RT judge
carries over (queue_hold_threshold, lyapunov_V/W). Records P and the knobs each
episode. If persistence helps, P should climb over the first episodes and then
plateau as the posture converges.

Same seed is deliberate: it removes seed noise so any P change is attributable to
the carried-over knobs, not luck. Costs API calls (one episode each).

Usage:
    python -m scripts.learning_curve --model openrouter:openai/gpt-4o-mini --episodes 5
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
from policy_store import DEFAULT_PATH

_ONSET = {"single_storm": 50.0, "multi_storm": 60.0}


async def main(args: argparse.Namespace) -> None:
    # start each run from a clean slate so episode 1 is the true "cold" baseline
    Path(DEFAULT_PATH).unlink(missing_ok=True)

    model  = resolve_model(args.model)
    onset  = _ONSET[args.scenario]
    t_post = 20.0 if args.scenario == "single_storm" else None

    print(f"[curve] Starting MCP server on {MCP_HOST}:{MCP_PORT} ...")
    server_task = asyncio.create_task(
        mcp.run_http_async(host=MCP_HOST, port=MCP_PORT, show_banner=False, log_level="warning")
    )
    await asyncio.sleep(1.5)

    rows = []
    t0 = time.monotonic()
    for ep in range(1, args.episodes + 1):
        calendar = [ScheduledEvent(t_start=onset, name="scheduled mass event", severity="high")]
        report = await run_episode(
            model=model, scenario=args.scenario, seed=args.seed, c_max=16,
            rt_factor=args.rt_factor, poll_interval_s=1.0,
            assessment_interval_s=args.assessment_interval, t_post=t_post,
            calendar=calendar, persist_knobs=True,
        )
        fp = report["final_policy"]
        rows.append((ep, report["final_P"], report["benign_success_rate"],
                     fp["queue_hold_threshold"], fp["lyapunov_V"], fp["lyapunov_W"]))
        print(f"[curve] episode={ep}  P={report['final_P']:.3f}  "
              f"benign={report['benign_success_rate']:.3f}  "
              f"end_knobs(queue_hold={fp['queue_hold_threshold']}, "
              f"V={fp['lyapunov_V']:.0f}, W={fp['lyapunov_W']:.2f})")

    elapsed = time.monotonic() - t0
    print("\n" + "=" * 64)
    print(f"LEARNING CURVE  ({args.scenario}, seed={args.seed}, {args.episodes} episodes)")
    print("=" * 64)
    print(f"{'episode':>7s}  {'P':>6s}  {'benign':>7s}  {'queue_hold':>10s}  {'V':>6s}  {'W':>5s}")
    for ep, p, b, qh, v, w in rows:
        print(f"{ep:7d}  {p:6.3f}  {b:7.3f}  {qh:10d}  {v:6.0f}  {w:5.2f}")
    p_first, p_last = rows[0][1], rows[-1][1]
    print(f"\n  P: episode 1 = {p_first:.3f}  ->  episode {len(rows)} = {p_last:.3f}  "
          f"(delta {p_last - p_first:+.3f})")
    print(f"  wall time: {elapsed:.0f}s")
    print("=" * 64)

    logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Knob-persistence learning curve over successive episodes")
    p.add_argument("--model", default="openrouter:openai/gpt-4o-mini")
    p.add_argument("--scenario", default="single_storm", choices=["single_storm", "multi_storm"])
    p.add_argument("--episodes", type=int, default=5)
    p.add_argument("--seed", type=int, default=3)
    p.add_argument("--rt-factor", type=float, default=5.0, dest="rt_factor")
    p.add_argument("--assessment-interval", type=float, default=6.0, dest="assessment_interval")
    args = p.parse_args()
    asyncio.run(main(args))
