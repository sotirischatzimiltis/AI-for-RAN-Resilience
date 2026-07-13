"""
Orchestrator — SMO / xApp coordinator level.

Responsibilities (revised):
  1. Start the simulator episode.
  2. Launch Near-RT-Agent and Non-RT-Agent autonomous loops — then step aside.
  3. Route operator intents to the appropriate agent when received.
  4. Collect and return the final episode report.

The Orchestrator does NOT poll sub-agents every tick.
It is event-driven: idle during steady-state, active only when an operator
intent arrives or the episode ends.

Token budget comparison (100s episode, 1s poll):
  Old design: ~100 Orchestrator→NearRT LLM calls (pure coordination waste)
  New design:   0 Orchestrator calls during steady-state
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from pydantic_ai import Agent

from agents.near_rt_control_loop import run_control_loop
from agents.non_rt_agent        import build_non_rt_agent, run_assessment_loop
from agents.policy              import SharedPolicy, EpisodeStats
from runtime                    import host as sim_host, UP
from sim.metrics                import resilience_score, success_rate
from policy_store               import load_knobs, save_knobs


# ---------------------------------------------------------------------------
# Intent routing
# ---------------------------------------------------------------------------
# The Near-RT path is now a pure code loop with no LLM, so operator intents go
# to the Non-RT-Agent (the only sub-agent). Richer routing / an LLM orchestrator
# can be added here later.

async def route_intent(
    intent: str,
    non_rt: Agent,
    policy: SharedPolicy,
    stats:  EpisodeStats | None = None,
) -> str:
    """Send a one-shot operator intent to the Non-RT-Agent; return its response."""
    if stats:
        stats.intents_routed += 1

    print(f"[Orchestrator] Routing intent to Non-RT: {intent[:80]}")
    result   = await non_rt.run(f"[Operator intent] {intent}\n{policy.context_str()}")
    response = str(result.output)
    print(f"[Orchestrator] Non-RT response: {response[:120]}")
    return response


# ---------------------------------------------------------------------------
# Episode runner
# ---------------------------------------------------------------------------

async def run_episode(
    model:                 object  = "ollama:llama3.2",
    scenario:              str     = "single_storm",
    seed:                  int     = 3,
    c_max:                 int     = 16,
    rt_factor:             float   = 1.0,
    poll_interval_s:       float   = 1.0,
    assessment_interval_s: float   = 10.0,
    t_post:                float | None = None,
    calendar: list | None = None,
    intents: list[tuple[float, str, str]] | None = None,
    persist_knobs:         bool = False,
) -> dict:
    """
    Run one full episode with autonomous Near-RT and Non-RT loops.

    Parameters
    ----------
    model                 : pydantic-ai model object or string
    scenario              : 'single_storm' | 'multi_storm'
    seed                  : RNG seed
    c_max                 : maximum server count
    rt_factor             : wall-clock seconds per simulated second
    poll_interval_s       : Near-RT control cycle interval (seconds)
    assessment_interval_s : Non-RT assessment interval (seconds)
    intents               : optional list of (delay_s, target, intent_text)
                            injected at the given wall-clock delays into the episode
    """
    non_rt = build_non_rt_agent(model)     # storm judge + policy writer + intent Q&A

    # Seed the slow tuning knobs from the previous episode if persistence is on;
    # the operational levers (storm_active, drop) always start fresh.
    knobs = load_knobs() if persist_knobs else None
    policy = SharedPolicy(**knobs) if knobs else SharedPolicy()
    if knobs:
        print(f"[Orchestrator] Loaded persisted knobs: {knobs}")
    stats   = EpisodeStats()

    # Publish the operator calendar so the get_calendar MCP tool can read it
    sim_host.calendar = calendar or []

    # Start simulator
    msg = sim_host.start(scenario=scenario, seed=seed, c_max=c_max, rt_factor=rt_factor, t_post=t_post)
    print(f"[Orchestrator] {msg}")

    # Event that signals both loops to stop
    stop_event = asyncio.Event()

    async def _watch_episode():
        """Sets stop_event when the simulator finishes."""
        while not sim_host.is_done:
            await asyncio.sleep(0.5)
        stop_event.set()
        print("[Orchestrator] Episode complete — signalling loops to stop.")

    # Optional scheduled operator intents (target field retained for compatibility,
    # unused now that Non-RT is the sole intent target)
    async def _inject(delay: float, target: str, intent: str):
        await asyncio.sleep(delay)
        if not stop_event.is_set():
            await route_intent(intent, non_rt, policy, stats)

    t_start = time.monotonic()

    tasks = [
        asyncio.create_task(_watch_episode()),
        asyncio.create_task(
            run_control_loop(policy, stop_event, poll_interval_s, stats)
        ),
        asyncio.create_task(
            run_assessment_loop(non_rt, policy, stop_event, assessment_interval_s, stats)
        ),
    ]
    if intents:
        for delay, target, intent in intents:
            tasks.append(asyncio.create_task(_inject(delay, target, intent)))

    await asyncio.gather(*tasks)

    # Carry the tuned posture into the next episode (slow knobs only).
    if persist_knobs:
        save_knobs(policy)
        print(f"[Orchestrator] Persisted knobs for next episode: "
              f"queue_hold={policy.queue_hold_threshold}, "
              f"V={policy.lyapunov_V:.0f}, W={policy.lyapunov_W:.2f}")

    wall_time = time.monotonic() - t_start
    sim_stats = sim_host.sim.stats if sim_host.sim else None

    # Final A3RT resilience score, computed once at episode end (ground truth)
    final_P = 0.0
    if sim_host.sim and len(sim_host.sim.telemetry) >= 4:
        try:
            final_P = resilience_score(
                sim_host.sim.telemetry, sim_host.sim.mu_single, UP,
                t0=sim_host.t0, td=sim_host.td,
            )["P"]
        except Exception:
            final_P = 0.0

    return {
        "scenario":            scenario,
        "seed":                seed,
        "wall_time_s":         round(wall_time, 1),
        "near_rt_steps":       stats.near_rt_steps,
        "near_rt_errors":      stats.near_rt_errors,
        "non_rt_assessments":  stats.non_rt_assessments,
        "non_rt_errors":       stats.non_rt_errors,
        "intents_routed":      stats.intents_routed,
        "final_P":             round(final_P, 4),
        "success_rate":        round(success_rate(
                                   sim_stats.completed if sim_stats else 0,
                                   sim_stats.failed if sim_stats else 0), 4),
        "final_policy": {
            "malicious_drop_prob":  policy.malicious_drop_prob,
            "queue_hold_threshold": policy.queue_hold_threshold,
            "lyapunov_V":           policy.lyapunov_V,
            "lyapunov_W":           policy.lyapunov_W,
        },
        "completed": sim_stats.completed if sim_stats else 0,
        "failed":    sim_stats.failed    if sim_stats else 0,
        "retries":   sim_stats.retries   if sim_stats else 0,
    }
