"""
Non-RT-Agent — the storm judge in the decoupled (two-agent) design.

Runs asynchronously on its own cadence (a few seconds), ABOVE the deterministic
1 Hz fast control loop, which it never blocks. Each assessment it is fed a
summary of the recent telemetry WINDOW (trends, not one instant), judges whether
a signaling storm is active, and writes storm_active + drop_prob_floor into the
shared policy. The fast loop reads that verdict to gate the malicious-UE filter;
capacity is handled reactively by the fast loop without waiting on this agent.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset, FastMCPClient

from mcp_server.server import MCP_HOST, MCP_PORT, host as sim_host
from agents.policy import SharedPolicy, EpisodeStats

MCP_URL = f"http://{MCP_HOST}:{MCP_PORT}/mcp"

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
SYSTEM_PROMPT = (_PROMPTS_DIR / "non_rt.md").read_text()


class PolicyUpdate(BaseModel):
    storm_active:          bool  = Field(description="True if a signaling storm is active RIGHT NOW")
    drop_prob_floor:       float = Field(ge=0.0, le=1.0,
                                         description="Malicious-drop probability to apply while the storm is active (0.0 when not)")
    escalation_threshold:  float = Field(ge=0.0, le=1.0,
                                         description="Slow tuning knob; only meaningful when tighten=True")
    tighten:               bool  = Field(description="True only if escalation_threshold should be applied")
    resilience_P_observed: float = Field(description="Cumulative resilience P read from get_episode_stats")
    reasoning:             str   = Field(description="1-2 sentences citing the leading signals (lam, retry-rate)")


def build_non_rt_agent(model) -> Agent:
    toolset = MCPToolset(FastMCPClient(MCP_URL))
    return Agent(
        model=model,
        output_type=PolicyUpdate,
        toolsets=[toolset],
        system_prompt=SYSTEM_PROMPT,
    )


def summarize_window(telemetry, window_s: float = 40.0, n_bins: int = 8) -> str:
    """
    Summarise the last ~window_s of telemetry as TRENDS so the model can tell a
    storm onset from its tail. Reports arrival-rate and queue direction, the
    retry-rate first-half vs second-half (leading signal), the in-window peak
    arrival rate and its recency, and a coarse binned arrival-rate trajectory.
    """
    if not telemetry:
        return "No telemetry yet — episode may not have started."
    t_now = telemetry[-1].t
    win   = [s for s in telemetry if s.t >= t_now - window_s]
    if len(win) < 2:
        return f"Only {len(win)} sample(s) so far; system just started."

    t0, t1     = win[0].t, win[-1].t
    lam0, lam1 = win[0].lam_current, win[-1].lam_current
    q0, q1     = win[0].queue_len, win[-1].queue_len

    # retry-rate: first half vs second half of the window (retries are cumulative)
    mid = len(win) // 2
    def rate(a: int, b: int) -> float:
        dt = max(win[b].t - win[a].t, 1e-6)
        return (win[b].retries - win[a].retries) / dt
    rr_first  = rate(0, mid)
    rr_second = rate(mid, len(win) - 1)

    # in-window peak arrival rate and how long ago it occurred
    peak       = max(win, key=lambda s: s.lam_current)
    since_peak = t_now - peak.t

    def direction(a: float, b: float, eps: float) -> str:
        if b > a + eps: return "rising"
        if b < a - eps: return "falling"
        return "flat"
    lam_dir = direction(lam0, lam1, 5.0)
    q_dir   = direction(q0, q1, 5.0)
    rr_dir  = direction(rr_first, rr_second, 1.0)

    # coarse binned arrival-rate trajectory (means per bin)
    span = max(t1 - t0, 1e-6)
    bins = []
    for i in range(n_bins):
        lo, hi = t0 + span * i / n_bins, t0 + span * (i + 1) / n_bins
        vals   = [s.lam_current for s in win if lo <= s.t < hi]
        bins.append(str(round(sum(vals) / len(vals))) if vals else "-")
    traj = " ".join(bins)

    return (
        f"Window: last {t1 - t0:.0f}s ({len(win)} samples), now t={t_now:.0f}s\n"
        f"  arrival-rate lam: {lam0:.0f} -> {lam1:.0f} UEs/s ({lam_dir}); "
        f"peak {peak.lam_current:.0f} at t={peak.t:.0f}s ({since_peak:.0f}s ago)  [LEADING]\n"
        f"  lam trajectory (bin means): {traj}\n"
        f"  queue_len: {q0} -> {q1} ({q_dir})  [LAGGING]\n"
        f"  retry-rate: {rr_first:.0f}/s -> {rr_second:.0f}/s ({rr_dir})  [LEADING]"
    )


async def _do_assessment(
    agent:      Agent,
    policy:     SharedPolicy,
    assessment: int,
    stats:      EpisodeStats | None,
) -> None:
    t0     = time.monotonic()
    window = summarize_window(sim_host.sim.telemetry) if sim_host.sim else "No sim running."
    prompt = (
        f"Assessment #{assessment}. {policy.context_str()}\n"
        f"Recent telemetry window:\n{window}\n"
        "Call get_episode_stats for the cumulative resilience P, then judge "
        "storm-vs-noise from the window trends and return a PolicyUpdate."
    )
    try:
        result  = await agent.run(prompt)
        pu      = result.output
        elapsed = time.monotonic() - t0
        policy.update(
            storm_active=pu.storm_active,
            drop_prob_floor=pu.drop_prob_floor,
            escalation_threshold=pu.escalation_threshold,
            resilience_P=pu.resilience_P_observed,
            tighten=pu.tighten,
        )
        print(
            f"[Non-RT]  assessment={assessment}  storm={pu.storm_active}  "
            f"drop_floor={pu.drop_prob_floor:.2f}  P={pu.resilience_P_observed:.3f}  "
            f"({elapsed:.1f}s)  {pu.reasoning}"
        )
    except Exception as e:
        if stats:
            stats.non_rt_errors += 1
        print(f"[Non-RT] error at assessment {assessment}: {e}")


async def run_assessment_loop(
    agent:      Agent,
    policy:     SharedPolicy,
    stop_event: asyncio.Event,
    interval:   float = 10.0,
    stats:      EpisodeStats | None = None,
) -> None:
    """
    Autonomous Non-RT assessment loop. Sleeps `interval` between assessments,
    wakes early and exits cleanly when stop_event fires, and always runs one
    final assessment after the episode ends.
    """
    assessment = 0
    while True:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break  # episode done → drop to final assessment
        except asyncio.TimeoutError:
            pass

        if stop_event.is_set():
            break

        assessment += 1
        if stats:
            stats.non_rt_assessments += 1
        await _do_assessment(agent, policy, assessment, stats)

    # Final assessment at episode end
    assessment += 1
    if stats:
        stats.non_rt_assessments += 1
    print(f"[Non-RT]  Episode complete — running final assessment #{assessment}.")
    await _do_assessment(agent, policy, assessment, stats)
