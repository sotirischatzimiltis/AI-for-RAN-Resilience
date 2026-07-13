"""
Live-agent seed sweep — quantify the agentic LLM system's lift over the
deterministic control baseline.

Runs the full agentic episode (Orchestrator + Non-RT LLM judge + fast loop) across
several seeds, in ONE process with the MCP server up throughout, and reports P and
success rate per seed with mean ± std. For each seed it also runs the deterministic
Lyapunov controller (no LLM) and prints the per-seed lift = agent_P - lyapunov_P.

The agent's P is noisy (the malicious filter engages LLM-late, timing jitters run
to run); this sweep is how we see the mean lift above that noise. Costs API calls.

Usage (sources the shell env for the API key):
    python -m scripts.agent_sweep --model openrouter:openai/gpt-4o-mini --seeds 5
"""

import argparse
import asyncio
import logging
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.server import mcp, MCP_HOST, MCP_PORT
from agents.orchestrator import run_episode
from event_calendar import ScheduledEvent
from scripts.run import resolve_model
from scripts.seed_sweep import run_one as det_run_one, CONTROLLERS

# deterministic baseline the agent is measured against
_LYAP_FACTORY, _LYAP_C0 = CONTROLLERS["Lyapunov V=1000"]

# per-scenario: sim time of the first storm onset (for the calendar event) and
# whether t_post applies (single-storm only)
_SCENARIO_ONSET = {"single_storm": 50.0, "multi_storm": 60.0}


async def main(args: argparse.Namespace) -> None:
    model = resolve_model(args.model)

    print(f"[sweep] Starting MCP server on {MCP_HOST}:{MCP_PORT} ...")
    server_task = asyncio.create_task(
        mcp.run_http_async(host=MCP_HOST, port=MCP_PORT, show_banner=False, log_level="warning")
    )
    await asyncio.sleep(1.5)

    onset  = _SCENARIO_ONSET[args.scenario]
    t_post = 20.0 if args.scenario == "single_storm" else None  # multi-storm horizon is fixed
    seeds = list(range(1, args.seeds + 1))
    rows = []
    t0 = time.monotonic()
    for seed in seeds:
        calendar = [ScheduledEvent(t_start=onset, name="scheduled mass event", severity="high")]
        report = await run_episode(
            model=model, scenario=args.scenario, seed=seed, c_max=16,
            rt_factor=args.rt_factor, poll_interval_s=1.0,
            assessment_interval_s=args.assessment_interval, t_post=t_post,
            calendar=calendar,
        )
        # deterministic Lyapunov baseline for the same seed
        lyap_p, _, _ = det_run_one(_LYAP_FACTORY, _LYAP_C0, args.scenario, seed)
        rows.append((seed, report["final_P"], report["success_rate"],
                     report["failed"], report["non_rt_errors"], lyap_p))
        per = report.get("per_storm_P", [])
        per_str = f"  per_storm={per}" if len(per) > 1 else ""
        print(f"[sweep] seed={seed}  agent_P={report['final_P']:.3f}  "
              f"succ={report['success_rate']:.3f}  fail={report['failed']}  "
              f"non_rt_errors={report['non_rt_errors']}  "
              f"lyap_P={lyap_p:.3f}  lift={report['final_P'] - lyap_p:+.3f}{per_str}")

    elapsed = time.monotonic() - t0

    agent_ps = [r[1] for r in rows]
    succs    = [r[2] for r in rows]
    lifts    = [r[1] - r[5] for r in rows]
    print("\n" + "=" * 64)
    print(f"AGENT SWEEP  ({args.scenario}, {len(seeds)} seeds, model={args.model})")
    print("=" * 64)
    print(f"  agent P      : {statistics.mean(agent_ps):.3f} ± "
          f"{statistics.pstdev(agent_ps) if len(agent_ps) > 1 else 0.0:.3f}  "
          f"(min {min(agent_ps):.3f}, max {max(agent_ps):.3f})")
    print(f"  success rate : {statistics.mean(succs):.3f}")
    print(f"  lift vs Lyap : {statistics.mean(lifts):+.3f} ± "
          f"{statistics.pstdev(lifts) if len(lifts) > 1 else 0.0:.3f}")
    print(f"  total errors : {sum(r[4] for r in rows)} Non-RT across all seeds")
    print(f"  wall time    : {elapsed:.0f}s")
    print("=" * 64)

    logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Live-agent seed sweep vs deterministic baseline")
    p.add_argument("--model", default="openrouter:openai/gpt-4o-mini")
    p.add_argument("--scenario", default="single_storm",
                   choices=["single_storm", "multi_storm"])
    p.add_argument("--seeds", type=int, default=5)
    p.add_argument("--rt-factor", type=float, default=4.0, dest="rt_factor")
    p.add_argument("--assessment-interval", type=float, default=6.0, dest="assessment_interval")
    args = p.parse_args()
    asyncio.run(main(args))
