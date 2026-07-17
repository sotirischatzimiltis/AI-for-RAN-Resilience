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
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from agents.near_rt_control_loop import run_control_loop
from agents.non_rt_agent        import build_non_rt_agent, run_assessment_loop, _accumulate_usage
from agents.policy              import SharedPolicy, EpisodeStats
from event_calendar             import ScheduledEvent
from runtime                    import host as sim_host, UP
from sim.metrics                import (resilience_multi, benign_success_rate,
                                        malicious_blocked_rate, malicious_filtered_rate,
                                        avg_servers, per_storm_blocked)
from policy_store               import load_knobs, save_knobs, load_storm_memory, save_storm_memory
from storm_memory               import StormMemory


# ---------------------------------------------------------------------------
# The Orchestrator agent — understand an operator intent, then act
# ---------------------------------------------------------------------------
# A single agent at the network-management (SMO/rApp) tier. It reads a free-text
# operator intent and produces ONE OperatorDirective that can:
#   • SET POLICY itself — the network posture (V/W), an SLA capacity floor, a
#     scheduled event — overriding the site's autonomous tuning; and/or
#   • DELEGATE to the Non-RT judge — a standing instruction the storm judge reads
#     in every assessment (operational nuance the site should apply).
# Either or both, in one shot.

_PROMPTS_DIR  = Path(__file__).parent.parent / "prompts"
SYSTEM_PROMPT = (_PROMPTS_DIR / "orchestrator.md").read_text()

# priority -> (V, W) when the operator gives no explicit weights
PRIORITY_VW = {
    "qos":      (5000.0, 1.0),   # favour service: many servers
    "cost":     (500.0, 5.0),    # favour efficiency: few servers
    "balanced": (1000.0, 1.0),   # the default posture
}


class OperatorDirective(BaseModel):
    priority: Literal["qos", "cost", "balanced"] = Field(
        description="Network posture: 'qos' favours service (more servers), 'cost' favours "
                    "efficiency (fewer), 'balanced' is neutral. Use 'balanced' if the intent "
                    "is only a delegation to the site judge")
    lyapunov_V: float | None = Field(default=None, ge=0.0, le=100000.0,
        description="Explicit utility-weight override (higher -> more servers); null -> from priority")
    lyapunov_W: float | None = Field(default=None, ge=0.0, le=1000.0,
        description="Explicit cost-weight override (higher -> fewer servers); null -> from priority")
    min_servers: int | None = Field(default=None, ge=1, le=64,
        description="SLA capacity FLOOR: never run fewer than this. Null = no floor")
    schedule_event_name: str | None = Field(default=None,
        description="Label of a KNOWN upcoming load event named in the intent; else null")
    schedule_event_t: float | None = Field(default=None, ge=0.0,
        description="Simulated time (s) the scheduled event begins; else null")
    schedule_event_severity: Literal["low", "medium", "high"] = Field(default="high",
        description="Expected load of the scheduled event")
    nonrt_instruction: str | None = Field(default=None,
        description="If the intent is operational nuance for the SITE JUDGE rather than a "
                    "posture change — e.g. 'tonight's surge is a legitimate flash crowd, do "
                    "not treat high load alone as an attack' — put a concise standing "
                    "instruction here for the Non-RT storm judge; else null")
    reasoning: str = Field(description="One sentence: how this serves the operator's intent")


def build_orchestrator_agent(model) -> Agent:
    return Agent(model=model, output_type=OperatorDirective, system_prompt=SYSTEM_PROMPT)


async def route_intent(
    intent:       str,
    orchestrator: Agent,
    policy:       SharedPolicy,
    stats:        EpisodeStats | None = None,
) -> str:
    """Understand an operator intent and act: set policy and/or delegate to the judge."""
    if stats:
        stats.intents_routed += 1

    print(f"[Orchestrator] Operator intent: {intent}")
    _t0 = time.monotonic()
    result = await orchestrator.run(f"Operator intent: {intent}\nCurrent {policy.context_str()}")
    if stats:
        _accumulate_usage(stats, result, time.monotonic() - _t0)
    d = result.output
    actions = []

    # --- branch A: set policy directly (network posture / SLA / schedule) ---
    posture_changed = (d.priority != "balanced" or d.lyapunov_V is not None
                       or d.lyapunov_W is not None or d.min_servers is not None)
    if posture_changed:
        base_v, base_w = PRIORITY_VW.get(d.priority, PRIORITY_VW["balanced"])
        v = d.lyapunov_V if d.lyapunov_V is not None else base_v
        w = d.lyapunov_W if d.lyapunov_W is not None else base_w
        policy.set_operator(lyapunov_V=v, lyapunov_W=w, min_servers=d.min_servers)
        floor = f", min_servers={d.min_servers}" if d.min_servers else ""
        actions.append(f"policy(priority={d.priority}, V={v:.0f}, W={w:.2f}{floor})")

    if d.schedule_event_name and d.schedule_event_t is not None:
        sim_host.calendar.append(ScheduledEvent(
            t_start=d.schedule_event_t, name=d.schedule_event_name, severity=d.schedule_event_severity))
        actions.append(f"scheduled '{d.schedule_event_name}'@t={d.schedule_event_t:.0f}s")

    # --- branch B: delegate a standing instruction to the Non-RT judge ---
    if d.nonrt_instruction:
        policy.set_operator_note(d.nonrt_instruction)
        actions.append(f"delegated to Non-RT: \"{d.nonrt_instruction}\"")

    summary = "; ".join(actions) if actions else "no-op"
    print(f"[Orchestrator] {summary}  — {d.reasoning}")
    return summary


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
    window_s:              float   = 40.0,
    t_post:                float | None = None,
    calendar: list | None = None,
    intents: list[tuple[float, str, str]] | None = None,
    persist_knobs:         bool = False,
    learn_within:          bool = False,
    learn_across:          bool = False,
    no_forecast:           bool = False,
    no_calendar:           bool = False,
    no_release_valve:      bool = False,
    compute_kappa:         float | None = None,
    provision_delay:       float = 0.0,
    non_rt_prompt:         str | None = None,
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
    non_rt = build_non_rt_agent(model, system_prompt=non_rt_prompt)  # per-site storm judge (optional custom prompt)
    # The Orchestrator (operator-intent tier) is only built/run when intents are present
    # (Phase E). With no intents — Phases A–D — it never spins up.
    orchestrator = build_orchestrator_agent(model) if intents else None

    # Seed the slow tuning knobs from the previous episode if persistence is on;
    # the operational levers (storm_active, drop) always start fresh.
    knobs = load_knobs() if persist_knobs else None
    policy = SharedPolicy(**knobs) if knobs else SharedPolicy()
    if knobs:
        print(f"[Orchestrator] Loaded persisted knobs: {knobs}")
    stats   = EpisodeStats()

    # Learned storm signature for the fast loop (auto-engage the filter).
    memory = None
    if learn_within or learn_across:
        memory = StormMemory(learn_within=learn_within, learn_across=learn_across)
        if learn_across:
            sig = load_storm_memory()
            if sig:
                for k, v in sig.items():
                    setattr(memory, k, v)
                print(f"[Orchestrator] Loaded storm signature: learned={memory.learned}, "
                      f"engage>{memory.engage_threshold}, storms_seen={memory.storms_seen}")

    # Publish the operator calendar so the get_calendar MCP tool can read it
    sim_host.calendar = calendar or []
    # Ablation gates for the anticipation MCP tools (read by the MCP server)
    sim_host.forecast_enabled = not no_forecast
    sim_host.calendar_enabled = not no_calendar
    if no_forecast or no_calendar or no_release_valve:
        off = [n for n, f in (("forecast", no_forecast), ("calendar", no_calendar),
                              ("release-valve", no_release_valve)) if f]
        print(f"[Orchestrator] Ablation: disabled {', '.join(off)}")

    # Start simulator
    msg = sim_host.start(scenario=scenario, seed=seed, c_max=c_max, rt_factor=rt_factor,
                         t_post=t_post, compute_kappa=compute_kappa, provision_delay=provision_delay)
    print(f"[Orchestrator] {msg}")

    # Event that signals both loops to stop
    stop_event = asyncio.Event()

    async def _watch_episode():
        """Sets stop_event when the simulator finishes."""
        while not sim_host.is_done:
            await asyncio.sleep(0.5)
        stop_event.set()
        print("[Orchestrator] Episode complete — signalling loops to stop.")

    # Optional operator intents, injected at scheduled wall-clock delays and
    # interpreted by the Orchestrator intent agent (target field kept for compat).
    async def _inject(delay: float, target: str, intent: str):
        await asyncio.sleep(delay)
        if not stop_event.is_set():
            await route_intent(intent, orchestrator, policy, stats)

    t_start = time.monotonic()

    tasks = [
        asyncio.create_task(_watch_episode()),
        asyncio.create_task(
            run_control_loop(policy, stop_event, poll_interval_s, stats, memory,
                             release_valve=not no_release_valve)
        ),
        asyncio.create_task(
            run_assessment_loop(non_rt, policy, stop_event, assessment_interval_s, stats, window_s=window_s)
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

    # Carry the learned storm signature into the next episode (across-episode learning).
    if memory is not None and learn_across:
        save_storm_memory(memory)
        print(f"[Orchestrator] Persisted storm signature: learned={memory.learned}, "
              f"engage>{memory.engage_threshold}, storms_seen={memory.storms_seen}")

    wall_time = time.monotonic() - t_start
    sim_stats = sim_host.sim.stats if sim_host.sim else None

    # Final A3RT resilience score, computed once at episode end (ground truth).
    # Score every storm in the scenario against its own local baseline; final_P is
    # the whole-episode aggregate (equals the single-storm score when there's one).
    final_P = 0.0
    per_storm: list = []
    per_storm_block: list = []
    if sim_host.sim and len(sim_host.sim.telemetry) >= 4:
        try:
            storms = sim_host.sim.cfg.traffic.storm_windows()
            rm = resilience_multi(sim_host.sim.telemetry, sim_host.sim.mu_single, UP, storms)
            final_P   = rm["P_episode"]
            per_storm = [round(s["P"], 4) for s in rm["per_storm"]]
            per_storm_block = per_storm_blocked(sim_host.sim.telemetry, storms)
        except Exception:
            final_P = 0.0
            per_storm_block = []

    return {
        "scenario":            scenario,
        "seed":                seed,
        "wall_time_s":         round(wall_time, 1),
        "near_rt_steps":       stats.near_rt_steps,
        "near_rt_errors":      stats.near_rt_errors,
        "non_rt_assessments":  stats.non_rt_assessments,
        "non_rt_errors":       stats.non_rt_errors,
        "intents_routed":      stats.intents_routed,
        "llm_requests":        stats.llm_requests,
        "llm_input_tokens":    stats.llm_input_tokens,
        "llm_output_tokens":   stats.llm_output_tokens,
        "llm_latency_s":       round(stats.llm_latency_s, 2),
        "mean_llm_latency_s":        round(stats.llm_latency_s / max(1, stats.non_rt_assessments), 3),
        "mean_assessment_latency_s": round(stats.assessment_latency_s / max(1, stats.non_rt_assessments), 3),
        "final_P":             round(final_P, 4),
        "per_storm_P":         per_storm,
        "per_storm_blocked":   per_storm_block,
        "benign_success_rate":     round(benign_success_rate(sim_stats), 4) if sim_stats else 1.0,
        "malicious_blocked_rate":  round(malicious_blocked_rate(sim_stats), 4) if sim_stats else 0.0,
        "malicious_filtered_rate": round(malicious_filtered_rate(sim_stats), 4) if sim_stats else 0.0,
        "avg_servers":             round(avg_servers(sim_host.sim.telemetry), 3) if sim_host.sim else 0.0,
        "final_policy": {
            "malicious_drop_prob":  policy.malicious_drop_prob,
            "queue_hold_threshold": policy.queue_hold_threshold,
            "lyapunov_V":           policy.snapshot().lyapunov_V,
            "lyapunov_W":           policy.snapshot().lyapunov_W,
            "min_servers":          policy.min_servers,
            "operator_override":    policy.operator_V is not None or policy.operator_W is not None,
        },
        "completed": sim_stats.completed if sim_stats else 0,
        "failed":    sim_stats.failed    if sim_stats else 0,
        "retries":   sim_stats.retries   if sim_stats else 0,
        "storm_memory": {
            "learned":          memory.learned,
            "engage_threshold": memory.engage_threshold,
            "storms_seen":      memory.storms_seen,
        } if memory is not None else None,
    }
