"""
Ablation sweep — isolate the contribution of each anticipation / reaction
mechanism by knocking it out one at a time and measuring the drop.

Runs the FULL agentic system and then each single-mechanism ablation across the
same set of seeds, in ONE process with the MCP server up throughout. For every
configuration it reports resilience P, benign-served rate and botnet-blocked rate
as mean ± std, so the delta from 'Full' is the mechanism's marginal value.

Configurations (each is Full minus one mechanism):
    full            — everything on (the headline system)
    no-forecast     — get_forecast disabled (no data-driven pre-provisioning)
    no-calendar     — get_calendar disabled (no scheduled-event pre-provisioning)
    no-release      — code-side filter release disabled (drops only on LLM verdict)
    no-learning     — storm-signature auto-engagement off (multi-storm only matters)

Costs API calls (one full episode per config per seed). Report P alongside the
two security rates — never P alone (see FEATURES.md, the two evaluation axes).

Usage (sources the shell env for the API key):
    python -m scripts.ablation --model openrouter:openai/gpt-4o-mini \\
        --scenario multi_storm --seeds 5 --save
"""

import argparse
import asyncio
import json
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

# scenario -> sim time of the first storm onset (for the pre-provisioning calendar event)
_SCENARIO_ONSET = {"single_storm": 50.0, "multi_storm": 60.0, "multi_storm_flat": 60.0}

# Each config is a set of run_episode kwargs layered on the shared base. 'full' is
# the reference; every other row disables exactly one mechanism. learn_within is on
# in the base so 'no-learning' is a genuine single-mechanism knockout.
CONFIGS: dict[str, dict] = {
    "full":        {},
    "no-forecast": {"no_forecast": True},
    "no-calendar": {"no_calendar": True},
    "no-release":  {"no_release_valve": True},
    "no-learning": {"learn_within": False},
}


async def run_config(model, scenario, seed, onset, t_post, args, overrides) -> dict:
    """Run one episode for a given ablation config and return its report."""
    # base config: full system — learning on, calendar event present for pre-provisioning
    kwargs = dict(
        model=model, scenario=scenario, seed=seed, c_max=16,
        rt_factor=args.rt_factor, poll_interval_s=1.0,
        assessment_interval_s=args.assessment_interval, t_post=t_post,
        calendar=[ScheduledEvent(t_start=onset, name="scheduled mass event", severity="high")],
        learn_within=True,
    )
    kwargs.update(overrides)
    return await run_episode(**kwargs)


async def main(args: argparse.Namespace) -> None:
    model = resolve_model(args.model)

    print(f"[ablation] Starting MCP server on {MCP_HOST}:{MCP_PORT} ...")
    server_task = asyncio.create_task(
        mcp.run_http_async(host=MCP_HOST, port=MCP_PORT, show_banner=False, log_level="warning")
    )
    await asyncio.sleep(1.5)

    onset  = _SCENARIO_ONSET[args.scenario]
    t_post = 20.0 if args.scenario == "single_storm" else None
    seeds  = list(range(1, args.seeds + 1))
    configs = [c for c in CONFIGS if c in args.configs] if args.configs else list(CONFIGS)

    results: dict[str, dict] = {}
    t0 = time.monotonic()
    for name in configs:
        overrides = CONFIGS[name]
        ps, benigns, blockeds = [], [], []
        for seed in seeds:
            report = await run_config(model, args.scenario, seed, onset, t_post, args, overrides)
            ps.append(report["final_P"])
            benigns.append(report["benign_success_rate"])
            blockeds.append(report["malicious_blocked_rate"])
            print(f"[ablation] {name:12s} seed={seed}  P={report['final_P']:.3f}  "
                  f"benign={report['benign_success_rate']:.3f}  "
                  f"blocked={report['malicious_blocked_rate']:.3f}")
        results[name] = {
            "P_mean":       statistics.mean(ps),
            "P_std":        statistics.pstdev(ps) if len(ps) > 1 else 0.0,
            "benign_mean":  statistics.mean(benigns),
            "blocked_mean": statistics.mean(blockeds),
        }

    elapsed = time.monotonic() - t0

    full = results.get("full")
    print("\n" + "=" * 78)
    print(f"ABLATION SWEEP  ({args.scenario}, {len(seeds)} seeds, model={args.model})")
    print("=" * 78)
    print(f"  {'config':12s}  {'P':>14s}  {'benign':>8s}  {'blocked':>8s}  {'dP vs full':>10s}")
    for name in configs:
        r = results[name]
        dP = f"{r['P_mean'] - full['P_mean']:+.3f}" if full and name != "full" else "   —"
        print(f"  {name:12s}  {r['P_mean']:.3f} ± {r['P_std']:.3f}  "
              f"{r['benign_mean']:>8.3f}  {r['blocked_mean']:>8.3f}  {dP:>10s}")
    print(f"  wall time    : {elapsed:.0f}s")
    print("=" * 78)

    if args.save:
        out = Path(__file__).parent.parent / "results" / f"ablation_{args.scenario}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps({
            "scenario": args.scenario, "model": args.model, "seeds": seeds,
            "configs": results,
        }, indent=2))
        print(f"  saved -> {out}")

    logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Ablation sweep — marginal value of each mechanism")
    p.add_argument("--model", default="openrouter:openai/gpt-4o-mini")
    p.add_argument("--scenario", default="multi_storm_flat",
                   choices=["single_storm", "multi_storm", "multi_storm_flat"])
    p.add_argument("--seeds", type=int, default=5)
    p.add_argument("--rt-factor", type=float, default=4.0, dest="rt_factor")
    p.add_argument("--assessment-interval", type=float, default=6.0, dest="assessment_interval")
    p.add_argument("--configs", nargs="*", default=None,
                   help=f"subset of configs to run (default all): {list(CONFIGS)}")
    p.add_argument("--save", action="store_true",
                   help="cache results to results/ablation_<scenario>.json")
    args = p.parse_args()
    asyncio.run(main(args))
