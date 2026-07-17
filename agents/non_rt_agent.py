"""
Non-RT-Agent — the storm judge in the decoupled (two-agent) design.

Runs asynchronously on its own cadence (a few seconds), ABOVE the deterministic
1 Hz fast control loop, which it never blocks. Each assessment it:
  • is fed a summary of the recent telemetry WINDOW (trends, not one instant);
  • READS two MCP tools — get_episode_stats (resilience) and get_calendar
    (known scheduled load events);
  • WRITES a PolicyUpdate into shared policy: the operational levers
    (storm_active, malicious_drop_prob) every cycle, plus the slow tuning knobs
    (queue_hold_threshold, lyapunov_V, lyapunov_W) when it sets tighten=True —
    e.g. raising lyapunov_V to pre-provision ahead of a scheduled event.

The fast loop reads that policy to gate the malicious-UE filter and shape its
Lyapunov server-count optimisation; capacity itself stays reactive and never
waits on this agent.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStreamableHTTP
from pydantic_ai.usage import UsageLimits

from mcp_server.server import MCP_HOST, MCP_PORT
from runtime import host as sim_host
from agents.policy import SharedPolicy, EpisodeStats

MCP_URL = f"http://{MCP_HOST}:{MCP_PORT}/mcp"

# Bound each assessment so a chatty model can't loop tool calls until it hits the
# framework's default request_limit of 50 (seen stalling an assessment ~60s).
# Sized with margin above normal use — 3 read tools + the structured-output
# submission + a little slack (gpt-4o-mini was observed using 5-6 tool calls) —
# while still well under a true runaway.
ASSESSMENT_LIMITS = UsageLimits(request_limit=10, tool_calls_limit=8)

# Per-HTTP-request timeout, provider-native via pydantic-ai model settings — flows to
# the OpenAI/httpx client OpenRouter uses, so a hung connection fails here instead of
# waiting out the client's ~600s default (which was blocking whole episodes: observed
# 3 stalls of ~600s in one sweep). Well above normal use (<=~26s even for reasoning
# models); a timed-out request is counted as an error and the assessment is skipped.
# NOTE: if per-request timeouts still stack across tool-call retries and an episode
# stalls, wrap the agent.run() below in an asyncio.wait_for(...) total backstop.
REQUEST_TIMEOUT_S = 60.0

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
SYSTEM_PROMPT = (_PROMPTS_DIR / "non_rt.md").read_text()


class PolicyUpdate(BaseModel):
    storm_active:         bool  = Field(description="True if a signaling storm is active RIGHT NOW")
    malicious_drop_prob:  float = Field(ge=0.0, le=1.0,
                                        description="Malicious-drop probability while a storm is active — YOUR calibrated "
                                                    "choice in (0,1], scaled to overload severity and whether filtering is "
                                                    "already containing it (see FILTER STRENGTH). 0.0 when no storm.")
    queue_hold_threshold: int   = Field(ge=1, le=1000, default=10,
                                        description="Queue length below which the fast loop may scale servers "
                                                    "down; higher holds capacity longer during drain. Applied "
                                                    "only when tighten=True")
    lyapunov_V:           float = Field(ge=0.0, le=100000.0, default=1000.0,
                                        description="Lyapunov utility/performance weight (raw scale, default 1000). "
                                                    "Higher -> provision MORE servers (favour QoS); raise ahead of a "
                                                    "forecast storm or scheduled mass event. Applied only when tighten=True")
    lyapunov_W:           float = Field(ge=0.0, le=1000.0, default=1.0,
                                        description="Lyapunov server-cost weight (raw scale, default 1). Higher -> "
                                                    "provision FEWER servers (favour cost). Applied only when tighten=True")
    tighten:              bool  = Field(description="True only if the slow tuning knobs (queue_hold_threshold, "
                                                    "lyapunov_V, lyapunov_W) should be applied")
    reasoning:            str   = Field(description="1-2 sentences citing the leading signals (lam, retry-rate)")


def build_non_rt_agent(model, system_prompt: str | None = None) -> Agent:
    toolset = MCPServerStreamableHTTP(MCP_URL)
    return Agent(
        model=model,
        output_type=PolicyUpdate,
        toolsets=[toolset],
        system_prompt=system_prompt or SYSTEM_PROMPT,
    )


def _resting_lam(telemetry, bin_size: float = 10.0, min_frac: float = 0.05) -> float:
    """Estimate the cell's resting arrival rate as the LOWEST arrival-rate LEVEL the
    cell actually sits at for a non-trivial share of the episode — the low mode of the
    lam distribution. Unlike a fixed percentile this keys on the VALUE of the low
    cluster, not on how often it occurs, so it stays correct even in a storm-dominant
    episode (as long as the cell rests at all). Arrival rates are binned; brief
    transition samples are ignored by requiring a bin to hold at least `min_frac` of the
    busiest bin's count, then the lowest surviving bin is the resting level."""
    from collections import Counter
    lams = [s.lam_current for s in telemetry]
    if not lams:
        return 0.0
    binned = Counter(round(l / bin_size) * bin_size for l in lams)
    floor  = max(1.0, min_frac * max(binned.values()))
    return float(min(b for b, c in binned.items() if c >= floor))


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

    # Resting baseline from the FULL history so far (not just the window), so the judge
    # has a rest reference even when the current window is entirely inside a storm.
    baseline = _resting_lam(telemetry)

    return (
        f"Window: last {t1 - t0:.0f}s ({len(win)} samples), now t={t_now:.0f}s\n"
        f"  >>> LATEST lam = {lam1:.0f} UEs/s  (this is NOW; your storm verdict keys on this)\n"
        f"  resting lam (calm baseline over the whole episode so far): ~{baseline:.0f} UEs/s  "
        f"[the reference for 'rest' — valid even if this window has no calm]\n"
        f"  arrival-rate lam over window: {lam0:.0f} -> {lam1:.0f} UEs/s ({lam_dir}); "
        f"peak was {peak.lam_current:.0f} at t={peak.t:.0f}s ({since_peak:.0f}s ago, already past)  [LEADING]\n"
        f"  lam trajectory (bin means): {traj}\n"
        f"  queue_len: {q0} -> {q1} ({q_dir})  [LAGGING]\n"
        f"  retry-rate: {rr_first:.0f}/s -> {rr_second:.0f}/s ({rr_dir})  [LEADING]"
    )


def _accumulate_usage(stats: EpisodeStats, result, elapsed: float) -> None:
    """Fold one agent.run()'s token usage + wall latency into the episode stats.
    Defensive across pydantic-ai usage-field names (input/output vs request/response)."""
    stats.llm_latency_s += elapsed
    try:
        u = result.usage()
    except Exception:
        return
    inp = getattr(u, "input_tokens", None)
    if inp is None:
        inp = getattr(u, "request_tokens", 0) or 0
    out = getattr(u, "output_tokens", None)
    if out is None:
        out = getattr(u, "response_tokens", 0) or 0
    stats.llm_requests      += getattr(u, "requests", 1) or 1
    stats.llm_input_tokens  += int(inp)
    stats.llm_output_tokens += int(out)


async def _do_assessment(
    agent:      Agent,
    policy:     SharedPolicy,
    assessment: int,
    stats:      EpisodeStats | None,
    window_s:   float = 40.0,
) -> None:
    t0     = time.monotonic()
    window = summarize_window(sim_host.sim.telemetry, window_s=window_s) if sim_host.sim else "No sim running."

    note = policy.get_operator_note()
    note_line = (f"\nOPERATOR INSTRUCTION (from the network operator, honour it): {note}\n"
                 if note else "")

    # Only instruct the model to call the tools that are actually enabled. When the
    # anticipation tools are ablated (bare-judge bake-off, or --no-forecast/--no-calendar)
    # they return "disabled", so naming them here would just waste tool calls/tokens.
    tools = ["get_episode_stats for the cumulative resilience (P, absorption, adaptation)"]
    if getattr(sim_host, "calendar_enabled", True):
        tools.append("get_calendar for scheduled load events")
    if getattr(sim_host, "forecast_enabled", True):
        tools.append("get_forecast for the short-term prediction of where load is heading")
    tool_line = "Call " + ", ".join(tools) + "."

    prompt = (
        f"Assessment #{assessment}. {policy.context_str()}\n"
        f"Recent telemetry window:\n{window}\n"
        f"{note_line}"
        f"{tool_line} Then judge storm-vs-noise from the window "
        "trends and return a PolicyUpdate."
    )
    try:
        t_llm   = time.monotonic()
        result  = await agent.run(
            prompt, usage_limits=ASSESSMENT_LIMITS,
            model_settings={"timeout": REQUEST_TIMEOUT_S},  # provider-native per-request cap
        )
        elapsed = time.monotonic() - t_llm     # pure LLM call time (incl. tool round-trips)
        pu      = result.output
        if stats:
            _accumulate_usage(stats, result, elapsed)
        policy.update(
            storm_active=pu.storm_active,
            malicious_drop_prob=pu.malicious_drop_prob,
            queue_hold_threshold=pu.queue_hold_threshold,
            lyapunov_V=pu.lyapunov_V,
            lyapunov_W=pu.lyapunov_W,
            tighten=pu.tighten,
        )
        tuned = f"  tighten(V={pu.lyapunov_V:.0f})" if pu.tighten else ""
        print(
            f"[Non-RT]  assessment={assessment}  storm={pu.storm_active}  "
            f"drop={pu.malicious_drop_prob:.2f}{tuned}  "
            f"({elapsed:.1f}s)  {pu.reasoning}"
        )
    except Exception as e:
        # includes request timeouts and, for agent/MCP runs, exceptions wrapped in a
        # TaskGroup/ExceptionGroup — unwrap those (recursively) to surface the real cause
        # instead of the opaque "unhandled errors in a TaskGroup" message.
        if stats:
            stats.non_rt_errors += 1
        def _unwrap(x, depth=0):
            subs = getattr(x, "exceptions", None)
            if subs and depth < 5:
                return " | ".join(_unwrap(s, depth + 1) for s in subs)
            return f"{type(x).__name__}: {x}"
        print(f"[Non-RT] error at assessment {assessment}: {_unwrap(e)}")
    finally:
        # full assessment wall time: telemetry summary + prompt build + LLM + policy write
        if stats:
            stats.assessment_latency_s += time.monotonic() - t0


async def run_assessment_loop(
    agent:      Agent,
    policy:     SharedPolicy,
    stop_event: asyncio.Event,
    interval:   float = 10.0,
    stats:      EpisodeStats | None = None,
    window_s:   float = 40.0,
) -> None:
    """
    Autonomous Non-RT assessment loop. Sleeps `interval` between assessments,
    wakes early and exits cleanly when stop_event fires, and always runs one
    final assessment after the episode ends. Scheduled events reach the agent via
    the get_calendar MCP tool (the calendar lives on runtime.host).

    window_s: how much recent telemetry (seconds) the judge sees each assessment.
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
        await _do_assessment(agent, policy, assessment, stats, window_s=window_s)

    # Final assessment at episode end
    assessment += 1
    if stats:
        stats.non_rt_assessments += 1
    print(f"[Non-RT]  Episode complete — running final assessment #{assessment}.")
    await _do_assessment(agent, policy, assessment, stats, window_s=window_s)
