"""
Non-RT-Agent — the storm judge in the decoupled (two-agent) design.

Runs async on its own slow cadence, ABOVE the 1 Hz fast loop (never blocks it).
Each assessment: reads a telemetry-WINDOW summary (trends) plus up to three MCP
tools (get_episode_stats always; get_calendar/get_forecast when not ablated), and
writes a PolicyUpdate into shared policy — storm_active + malicious_drop_prob every
cycle, plus the slow tuning knobs (V, W, queue_hold) when tighten=True. The fast
loop reads that policy to gate the malicious-UE filter; capacity stays reactive.
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
from shared.policy import SharedPolicy, RunStats

MCP_URL = f"http://{MCP_HOST}:{MCP_PORT}/mcp"

# Assign a usage limit to each assessment, so a runaway model can't eat the whole episode's budget. The
# model may call the tools multiple times, but the total usage must stay under this cap.
ASSESSMENT_LIMITS = UsageLimits(request_limit=10, tool_calls_limit=8)

# The model's per-request timeout (provider-native) — the LLM call may take longer than this if it calls tools, but the LLM itself must return within this cap.
REQUEST_TIMEOUT_S = 60.0

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts" 
SYSTEM_PROMPT = (_PROMPTS_DIR / "non_rt.md").read_text()

# STRUCTURED OUTPUT PolicyUpdate is the structured output the model must return each assessment.
# The fast loop reads this to gate the malicious-UE filter and shape its Lyapunov server-count optimisation.
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
    lyapunov_V:           float = Field(ge=0.0, le=20.0, default=1.0,
                                        description="Lyapunov utility/QoS weight (nominal 1 = load-tracking). Higher "
                                                    "favours QoS -> provision MORE servers; raise it ahead of a forecast "
                                                    "storm or scheduled mass event to pre-provision (the effect grows "
                                                    "with V but saturates around ~10). Applied only when tighten=True")
    lyapunov_W:           float = Field(ge=0.0, le=20.0, default=1.0,
                                        description="Lyapunov server-cost weight (nominal 1). Higher favours cost -> "
                                                    "provision FEWER servers. Applied only when tighten=True")
    tighten:              bool  = Field(description="True only if the slow tuning knobs (queue_hold_threshold, "
                                                    "lyapunov_V, lyapunov_W) should be applied")
    reasoning:            str   = Field(description="1-2 sentences citing the leading signals (lam, retry-rate)")

# Which read tools are live this episode — a SYSTEM-prompt fact (a capability), not
# per-cycle evidence. get_episode_stats is always on; the anticipation tools are
# omitted when ablated (bare-judge bake-off, or --no-forecast/--no-calendar), where
# they return "disabled" and naming them would just waste tool calls/tokens.
def _tool_availability_text(calendar_enabled: bool = True, forecast_enabled: bool = True) -> str:
    tools = [
        "get_episode_stats — cumulative resilience (P, absorption, adaptation). "
        "Absorption tells you whether your filter strength has been working."
    ]
    if calendar_enabled:
        tools.append(
            "get_calendar — KNOWN scheduled load events near now "
            "(e.g. a stadium egress, a planned mass registration)."
        )
    if forecast_enabled:
        tools.append(
            "get_forecast — a short-term prediction of where load is heading (arrival rate, "
            "retry/fail rate, queue: trend, slope, confidence); it catches unscheduled ramps."
        )
    return ("TOOLS AVAILABLE THIS EPISODE (call the ones you need before deciding):\n  - "
            + "\n  - ".join(tools))

# function to compose the system prompt based on available tools
def compose_system_prompt(
    base: str | None = None,
    *,
    calendar_enabled: bool = True,
    forecast_enabled: bool = True,
    variables: dict[str, str] | None = None,
) -> str:
    """Assemble the judge's system prompt ONCE at experiment start (it stays constant
    for the whole run) by filling named {{slots}} in a template.

    The template (a .md file, defaults to non_rt.md) marks WHERE each dynamic piece
    goes with a placeholder like {{tools}}:
      • {{tools}}   -> the enabled-tool list (per calendar_enabled / forecast_enabled)
      • {{<name>}}  -> filled from `variables` (e.g. {{scenario}}, {{posture}})
    Placing tools in a slot lets each experiment control exactly where it appears,
    instead of appending at the end. If a template has NO {{tools}} slot, the tool
    list is appended at the end (back-compat)."""
    template = (base or SYSTEM_PROMPT).strip()
    slots = {"tools": _tool_availability_text(calendar_enabled, forecast_enabled)}
    if variables:
        slots.update(variables)                       # experiment-specific {{name}} -> value
    for name, value in slots.items():
        placeholder = "{{" + name + "}}"
        if placeholder in template:
            template = template.replace(placeholder, value)
        elif name == "tools":
            template = f"{template}\n\n{value}"        # no {{tools}} slot -> append
    return template

# BUILD THE NON-RT AGENT
def build_non_rt_agent(model, system_prompt: str | None = None) -> Agent:
    toolset = MCPServerStreamableHTTP(MCP_URL)     # Create HTTP client pointing to the MCP Server HTTP client for the MCP read tools
    return Agent(
        model=model,                               # define the LLM to be used
        output_type=PolicyUpdate,                  # force structured output — the model MUST return a valid PolicyUpdate
        toolsets=[toolset],                        # provide the HTTP connection to the MCP server
        # System prompt is composed ONCE by the caller (compose_system_prompt) and passed
        # in whole; falls back to the bare base if a caller doesn't compose it.
        system_prompt=system_prompt or SYSTEM_PROMPT,
    )

def _resting_lam(telemetry, bin_size: float = 10.0, min_frac: float = 0.05) -> float:
    """Estimate the cell's calm baseline lam — the reference the judge measures a
    storm against. Returns the lowest arrival-rate bin the cell actually dwells in
    (sparse transition bins ignored), so it stays valid even when the current window
    is entirely inside a storm."""
    from collections import Counter
    lams = [s.lam_current for s in telemetry]
    if not lams:
        return 0.0
    binned = Counter(round(l / bin_size) * bin_size for l in lams)   # snap each lam to a bin, count occupancy
    floor  = max(1.0, min_frac * max(binned.values()))              # ignore sparse bins (< min_frac of the busiest)
    return float(min(b for b, c in binned.items() if c >= floor))    # lowest bin the cell actually dwells in = resting level

# PART OF THE USER PROMPT  
def summarize_window(telemetry, window_s: float = 40.0, n_bins: int = 8) -> str:
    """
    Summarise the last ~window_s of telemetry as TRENDS so the model can tell a
    storm onset from its tail. Reports arrival-rate and queue direction, the
    retry-rate first-half vs second-half (leading signal), the in-window peak
    arrival rate and its recency, and a coarse binned arrival-rate trajectory.
    """
    if not telemetry:
        return "No telemetry yet — episode may not have started."
    t_now = telemetry[-1].t # current sim time (last sample)
    win   = [s for s in telemetry if s.t >= t_now - window_s]   # keep only the last window_s of samples
    if len(win) < 2:
        return f"Only {len(win)} sample(s) so far; system just started."

    t0, t1     = win[0].t, win[-1].t                 # window start/end times
    lam0, lam1 = win[0].lam_current, win[-1].lam_current   # arrival rate: window start vs now
    q0, q1     = win[0].queue_len, win[-1].queue_len       # queue length: window start vs now

    # retry-rate: first half vs second half of the window (retries are CUMULATIVE, so
    # differentiate over each half; a rising retry-rate is the earliest storm signal)
    mid = len(win) // 2
    def rate(a: int, b: int) -> float:               # retries/sec between sample a and b
        dt = max(win[b].t - win[a].t, 1e-6)          # guard against zero dt
        return (win[b].retries - win[a].retries) / dt
    rr_first  = rate(0, mid)                          # retry-rate over the window's first half
    rr_second = rate(mid, len(win) - 1)              # ...and its second half

    # in-window peak arrival rate and how long ago it occurred (recency = is the surge fresh or fading?)
    peak       = max(win, key=lambda s: s.lam_current)
    since_peak = t_now - peak.t

    def direction(a: float, b: float, eps: float) -> str:   # classify a->b as rising/falling/flat (eps = deadband)
        if b > a + eps: return "rising"
        if b < a - eps: return "falling"
        return "flat"
    lam_dir = direction(lam0, lam1, 5.0)             # deadbands sized to the traffic scale (UEs/s)
    q_dir   = direction(q0, q1, 5.0)
    rr_dir  = direction(rr_first, rr_second, 1.0)

    # coarse binned arrival-rate trajectory: split the window into n_bins equal slices and
    # report each slice's mean lam, so the model sees the SHAPE (ramp/plateau/decay), not noise
    span = max(t1 - t0, 1e-6) # get the winow span (guard against zero span if the window is tiny)
    bins = [] # initialize bins 
    for i in range(n_bins):
        lo, hi = t0 + span * i / n_bins, t0 + span * (i + 1) / n_bins   # get the start and end of the bin 
        vals   = [s.lam_current for s in win if lo <= s.t < hi] # get the arrival rates in the bin
        bins.append(str(round(sum(vals) / len(vals))) if vals else "-")  # mean lam, or "-" if empty
    traj = " ".join(bins)

    # Resting baseline from the FULL history so far (not just the window), so the judge
    # has a rest reference even when the current window is entirely inside a storm.
    baseline = _resting_lam(telemetry)

    # Data-only summary: values + compact tags. The reasoning rules (LATEST lam drives
    # the verdict, resting = rest reference, LEADING/LAGGING semantics) live in the system prompt.
    return (
        f"Window: last {t1 - t0:.0f}s ({len(win)} samples), now t={t_now:.0f}s\n"
        f"  LATEST lam = {lam1:.0f} UEs/s\n"
        f"  resting lam (calm baseline, whole episode so far): ~{baseline:.0f} UEs/s\n"
        f"  arrival-rate lam over window: {lam0:.0f} -> {lam1:.0f} UEs/s ({lam_dir}); "
        f"peak {peak.lam_current:.0f} at t={peak.t:.0f}s ({since_peak:.0f}s ago)  [LEADING]\n"
        f"  lam trajectory (bin means): {traj}\n"
        f"  queue_len: {q0} -> {q1} ({q_dir})  [LAGGING]\n"
        f"  retry-rate: {rr_first:.0f}/s -> {rr_second:.0f}/s ({rr_dir})  [LEADING]"
    )

# Function that essentially counts how many tokens the model consumed, and how long it took, so we can estimate the cost.
def _accumulate_usage(stats: RunStats, result, elapsed: float) -> None:
    """Fold one agent.run()'s token usage + wall latency into the episode stats.
    Defensive across pydantic-ai usage-field names (input/output vs request/response).
    """
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

# ONE FULL JUDGE CYCLE, runs the LLM , publishes the verdict and records the stats 
async def _do_assessment(
    agent:      Agent, # provide the agent, from the build_non_rt_agent function
    policy:     SharedPolicy, # provide the SharedPolicy object to publish
    assessment: int, # what number this assessment is
    stats:      RunStats | None,  # LLM meter to fold this call into (tokens/latency/errors); None = skip bookkeeping
    window_s:   float = 40.0,         # how many seconds of telemetry the summary covers
) -> None:
    t0     = time.monotonic()                        # start of the WHOLE assessment (for asmt-latency stat)
    # turn the raw telemetry into the trend summary the model reasons over (never raw samples)
    window = summarize_window(sim_host.sim.telemetry, window_s=window_s) if sim_host.sim else "No sim running."

    note = policy.get_operator_note()                # standing operator instruction, if the Orchestrator set one
    note_line = (f"\nOPERATOR INSTRUCTION (from the network operator, honour it): {note}\n"
                 if note else "")

    # USER PROMPT constrcution  
    user_prompt = (
        f"Assessment #{assessment}. {policy.context_str()}\n"
        f"Recent telemetry window:\n{window}\n"
        f"{note_line}"
        "Judge storm-vs-noise from the window trends and return a PolicyUpdate."
    )
    try:
        t_llm   = time.monotonic() # 
        result  = await agent.run(                 # call the LLM 
            user_prompt, usage_limits=ASSESSMENT_LIMITS,
            model_settings={"timeout": REQUEST_TIMEOUT_S},  # provider-native per-request cap
        )
        elapsed = time.monotonic() - t_llm     # pure LLM call time (incl. tool round-trips)
        verdict = result.output                # the validated PolicyUpdate — the judge's decision
        if stats:
            _accumulate_usage(stats, result, elapsed)
        policy.update(                         # publish the verdict to the blackboard the fast loop reads
            storm_active=verdict.storm_active,
            malicious_drop_prob=verdict.malicious_drop_prob,
            queue_hold_threshold=verdict.queue_hold_threshold,
            lyapunov_V=verdict.lyapunov_V,
            lyapunov_W=verdict.lyapunov_W,
            tighten=verdict.tighten,
        )
        tuned = f"  tighten(V={verdict.lyapunov_V:.0f})" if verdict.tighten else ""
        print(
            f"[Non-RT]  assessment={assessment}  storm={verdict.storm_active}  "
            f"drop={verdict.malicious_drop_prob:.2f}{tuned}  "
            f"({elapsed:.1f}s)  {verdict.reasoning}"
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

# LOOP of the Non-RT assessment 
async def run_assessment_loop(
    agent:      Agent, # pass the agent created
    policy:     SharedPolicy, # pass the shared policy object to write in
    stop_event: asyncio.Event, # the notifier when the episode ends 
    interval:   float = 10.0, # seconds between assessments 
    stats:      RunStats | None = None, # if i will save the run stats for logging 
    window_s:   float = 40.0, # how much telemtry each assessment sees. 
) -> None:
    """
    Autonomous Non-RT assessment loop. Sleeps `interval` between assessments,
    wakes early and exits cleanly when stop_event fires, and always runs one
    final assessment after the episode ends. 
    """
    assessment = 0
    while True:
        try:
            # sleep `interval` between assessments, but wake the instant the episode ends
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break  # stop_event fired → episode done → drop to the final assessment below
        except asyncio.TimeoutError:
            pass   # a normal interval elapsed → run the next assessment

        if stop_event.is_set():          # double-check (race: could have fired during setup)
            break

        assessment += 1
        if stats:
            stats.non_rt_assessments += 1
        await _do_assessment(agent, policy, assessment, stats, window_s=window_s)

    # Always run ONE final assessment after the episode ends (captures the recovery state)
    assessment += 1
    if stats:
        stats.non_rt_assessments += 1
    print(f"[Non-RT]  Episode complete — running final assessment #{assessment}.")
    await _do_assessment(agent, policy, assessment, stats, window_s=window_s)
