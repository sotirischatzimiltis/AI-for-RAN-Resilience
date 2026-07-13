"""
Main entry point for the Pydantic AI agentic resilience framework.

Architecture (decoupled two-agent design):
  - Fast control loop (pure code, 1 Hz): reads telemetry, computes c_star, reads
    the policy snapshot, clamps and actuates. NO LLM on the tick. Capacity is
    reactive every tick; only the drop filter waits on the Non-RT verdict.
  - Non-RT-Agent (LLM, ~10s cadence): judges storm-vs-noise from a telemetry
    window and writes storm_active + malicious_drop_prob into SharedPolicy.
  - Orchestrator starts the episode and routes operator intents; idle otherwise.

Usage:
    python -m scripts.run [flags]

Flags:
    --model               LLM model string (default: ollama:llama3.2)
                          Prefix with 'ollama:'     for local Ollama models.
                          Prefix with 'openrouter:' for OpenRouter models.
                          Use 'test' for a fast deterministic stub (no LLM needed).
    --scenario            single_storm | multi_storm  (default: single_storm)
    --seed                RNG seed (default: 3)
    --c-max               max servers (default: 16)
    --rt-factor           speed multiplier: simulated seconds per wall-clock second
                          (default 1.0 = real time). >1 runs FASTER than real time
                          (wall-time ~= horizon / rt_factor); <1 runs slower.
    --poll-interval       Near-RT control cycle interval in seconds (default: 1.0)
    --assessment-interval Non-RT assessment interval in seconds (default: 30.0)

Examples:
    # fast smoke test — no LLM required (~13s wall clock at rt_factor 10)
    python -m scripts.run --model test --rt-factor 10 --t-post 20

    # OpenRouter (gpt-4o-mini) — cheap and fast
    python -m scripts.run --model openrouter:openai/gpt-4o-mini

    # Ollama (local, slow)
    python -m scripts.run --model ollama:llama3.2

    # Accelerated real-time with faster Non-RT assessments
    python -m scripts.run --model openrouter:openai/gpt-4o-mini \\
        --rt-factor 10.0 --poll-interval 1.0 --assessment-interval 10.0
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


def resolve_model(model_str: str):
    """Convert a CLI model string to a pydantic-ai model object or passthrough string."""
    if model_str == "test":
        from pydantic_ai.models.test import TestModel
        return TestModel()

    if model_str.startswith("ollama:"):
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.ollama import OllamaProvider
        name = model_str.split(":", 1)[1]
        return OpenAIChatModel(name, provider=OllamaProvider(base_url="http://localhost:11434/v1"))

    if model_str.startswith("openrouter:"):
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openrouter import OpenRouterProvider
        name = model_str.split(":", 1)[1]
        return OpenAIChatModel(name, provider=OpenRouterProvider())

    # Bare string (e.g. "anthropic:claude-haiku-4-5-20251001") — pass through to pydantic-ai
    return model_str


async def main(args: argparse.Namespace) -> None:
    model = resolve_model(args.model)

    # Optional scheduled mass event on the operator's calendar (sim time)
    calendar = None
    if args.event_at is not None:
        calendar = [ScheduledEvent(t_start=args.event_at, name="scheduled mass event", severity="high")]
        print(f"[run] Calendar: mass event scheduled at sim t={args.event_at:.0f}s")

    # Start MCP server as a background asyncio task
    print(f"[run] Starting MCP server on {MCP_HOST}:{MCP_PORT} ...")
    server_task = asyncio.create_task(
        mcp.run_http_async(
            host=MCP_HOST, port=MCP_PORT,
            show_banner=False, log_level="warning",
        )
    )
    await asyncio.sleep(1.5)
    print("[run] MCP server ready.")
    print(
        f"[run] Starting episode | model={args.model}  scenario={args.scenario}  "
        f"seed={args.seed}  c_max={args.c_max}  rt_factor={args.rt_factor}\n"
        f"      Near-RT poll={args.poll_interval}s  "
        f"Non-RT assessment={args.assessment_interval}s"
    )
    print()

    t0 = time.monotonic()
    report = await run_episode(
        model=model,
        scenario=args.scenario,
        seed=args.seed,
        c_max=args.c_max,
        rt_factor=args.rt_factor,
        poll_interval_s=args.poll_interval,
        assessment_interval_s=args.assessment_interval,
        t_post=args.t_post,
        calendar=calendar,
        persist_knobs=args.persist_knobs,
    )
    elapsed = time.monotonic() - t0

    print("\n" + "=" * 62)
    print("EPISODE REPORT")
    print("=" * 62)
    print(f"  Wall-clock time        : {elapsed:.1f}s")
    print(f"  Near-RT control steps  : {report['near_rt_steps']}")
    print(f"  Near-RT errors         : {report['near_rt_errors']}")
    print(f"  Non-RT assessments     : {report['non_rt_assessments']}")
    print(f"  Non-RT errors          : {report['non_rt_errors']}")
    print(f"  Intents routed         : {report['intents_routed']}")
    print(f"  Final resilience P     : {report['final_P']:.4f}")
    if len(report.get('per_storm_P', [])) > 1:
        print(f"  Per-storm P            : {report['per_storm_P']}  (episode = mean)")
    print(f"  Benign success rate    : {report['benign_success_rate']:.3f}  (legit users served)")
    print(f"  Malicious blocked rate : {report['malicious_blocked_rate']:.3f}  (botnet denied)")
    print(f"  Completed UEs          : {report['completed']}")
    print(f"  Failed UEs             : {report['failed']}")
    print(f"  Retries                : {report['retries']}")
    print(f"  Final policy           : {report['final_policy']}")
    print("=" * 62)

    # Shut the MCP server down. Cancelling the task interrupts uvicorn's lifespan
    # coroutine mid-await, which it logs as an ERROR + CancelledError traceback —
    # harmless shutdown noise. Mute uvicorn's error logger first so the run ends
    # cleanly at the report above.
    logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pydantic AI agentic resilience run")
    parser.add_argument("--model",               default="ollama:llama3.2")
    parser.add_argument("--scenario",            default="single_storm",
                        choices=["single_storm", "multi_storm"])
    parser.add_argument("--seed",                type=int,   default=3)
    parser.add_argument("--c-max",               type=int,   default=16,   dest="c_max")
    parser.add_argument("--rt-factor",           type=float, default=1.0,  dest="rt_factor")
    parser.add_argument("--poll-interval",       type=float, default=1.0,  dest="poll_interval")
    parser.add_argument("--assessment-interval", type=float, default=10.0, dest="assessment_interval")
    parser.add_argument("--t-post",              type=float, default=None, dest="t_post",
                        help="post-storm duration in simulated seconds (default: full 900s)")
    parser.add_argument("--event-at",            type=float, default=None, dest="event_at",
                        help="sim time (s) of a scheduled mass event on the calendar (e.g. 50 = storm onset)")
    parser.add_argument("--persist-knobs",       action="store_true", dest="persist_knobs",
                        help="carry the Non-RT judge's tuned knobs (queue_hold, V, W) across episodes")
    args = parser.parse_args()
    asyncio.run(main(args))
