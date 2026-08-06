from __future__ import annotations
import asyncio
import time                                      # monotonic clock, to compensate the tick period
from runtime import UP, host as sim_host        # shared utility params + the process-wide sim owner
from sim.controllers import lyapunov_optimal_c  # the pure drift-plus-penalty c* search
from shared.policy import SharedPolicy, RunStats  # judge<->loop blackboard + per-episode counters


def ramp_time(reserve: int, cfg) -> float:
    """Seconds needed to bring `reserve` servers online from a cold cell, given the sim's
    provisioning model. Parallel: all pending spin up together, so ~one delay regardless of
    count. Serial: one server per delay, so (reserve-1) delays. Used to start pre-provisioning
    just early enough that the reserve is online BY the event — never earlier."""
    delay = cfg.server_provision_delay_s
    if getattr(cfg, "parallel_provision", False):
        return delay
    return max(0, reserve - 1) * delay

def apply_decision( # apply the Non-RT judge's policy to the sim, clamping against guardrails
    sim,
    storm_active:         bool,
    proposed_servers:     int,
    malicious_drop_prob:      float,
    queue_hold_threshold: int = 10,
    current_lam:          float = 0.0,
    memory=None,
    min_servers:          int = 1,
) -> tuple[int, float, bool]:
    """
    Clamp the proposal + policy-gated drop against the safety rules, actuate the sim,
    return (applied_servers, applied_drop, acted).
    Server: follow c_star, clamped to [floor, c_max], but never scale DOWN while the
            queue is backed up (queue_len >= queue_hold_threshold).
    Drop  : the judge owns storm detection (engage AND release) — apply its drop while
            storm_active, else 0.0. (A learned auto-engage may engage early; StormMemory.)
    """
    current_c    = sim.c_target                   # servers currently commanded
    current_drop = sim.malicious_drop_prob        # drop prob currently applied
    queue_len    = sim.telemetry[-1].queue_len if sim.telemetry else 0   # latest backlog (0 before first sample)

    # --- adaptation lever: servers (reactive) ---
    # Floor at the operator SLA minimum (min_servers), clamp to [1, c_max].
    floor = max(1, min(int(min_servers), sim.cfg.c_max))            # SLA floor, itself kept within [1, c_max]
    proposed_c = max(floor, min(int(proposed_servers), sim.cfg.c_max))  # clamp the proposal into [floor, c_max]
    if proposed_c < current_c and queue_len >= queue_hold_threshold:    # asked to scale DOWN while still backed up?
        applied_c = current_c            # hold: don't shed capacity mid-drain
    else:
        applied_c = proposed_c           # otherwise follow the proposal (scale up freely, scale down only when drained)

    # --- absorption lever: drop probability ---
    # The filter follows the judge's storm_active verdict (engage AND release).
    # Learned auto-engage: once the fast loop knows the storm signature it may engage
    # the filter itself, without waiting on the slow LLM verdict.
    auto_engage = memory is not None and memory.should_engage(current_lam)  # learned arm: load matches storm signature?
    if auto_engage:
        applied_drop = memory.storm_drop_level    # learned drop level (no LLM in the loop)
    elif storm_active:
        applied_drop = malicious_drop_prob        # judge says storm -> apply its drop level
    else:
        applied_drop = 0.0                        # no storm -> filter off (drop nothing)

    # did anything actually change this tick? (used only for the log marker / stats)
    acted = (applied_c != current_c) or (abs(applied_drop - current_drop) > 1e-9)
    sim.set_servers(applied_c)                    # actuator 1: commanded capacity
    sim.set_malicious_drop_prob(applied_drop)     # actuator 2: admission-filter strength
    return applied_c, applied_drop, acted

async def run_control_loop(
    policy:        SharedPolicy, # the judge's current policy (read-only)
    stop_event:    asyncio.Event, # set by the episode-done callback to exit the loop
    poll_interval: float = 1.0, # the loop's tick period (s)
    stats:         RunStats | None = None, # per-episode counters (None = no counting)
    memory=None, # optional StormMemory for learned auto-engage (None = no learning, no auto-engage)
) -> None:
    """
    Deterministic 1 Hz control loop
    Each tick:
      1. read latest telemetry from the sim,
      2. compute c_star (Lyapunov drift-plus-penalty) in Python,
      3. read the atomic policy snapshot (storm_active, malicious_drop_prob),
      4. (optional) update the learned storm signature from telemetry,
      5. apply a clamped action (capacity reactive; drop gated by storm_active
         or the learned auto-engage),
      6. sleep to the next tick (waking early when the episode ends).

    `memory` (StormMemory | None): when present, the loop learns the storm
    signature and may auto-engage the filter ahead of the LLM verdict.
    """
    step = 0
    while not stop_event.is_set():           # run until the episode-done event is set
        tick_start = time.monotonic()        # mark tick start so we sleep only the REMAINDER (fixed-rate)
        step += 1
        if stats:
            stats.near_rt_steps += 1         # count ticks for the episode summary

        sim = sim_host.sim                   # the live episode (owned by SimHost, shared in-process)
        if sim is not None and sim.telemetry:  # skip until the sim exists and has produced a sample
            s      = sim.telemetry[-1]       # latest telemetry snapshot
            pol    = policy.snapshot()       # atomic read of the judge's current policy
            # c_star uses the Lyapunov weights the Non-RT judge currently holds
            c_star = lyapunov_optimal_c(
                s, sim.mu_single, sim.cfg.c_max, UP,
                V=pol.lyapunov_V, W=pol.lyapunov_W,
            )

            # update the learned storm signature (within-episode learning)
            if memory is not None and memory.learn_within:
                memory.observe(s.lam_current, pol.storm_active)

            # Pre-provisioning reserve applied ON SCHEDULE: only from (event_time - ramp_time)
            # onward, so the servers are online BY the event without going up too early (a botnet
            # in the pre-event window is still filtered — capacity isn't raised for it). event_time
            # <= 0 means "no schedule" (rule/operator path) -> apply the reserve immediately.
            ramp = ramp_time(pol.reserve_servers, sim.cfg)
            reserve_due = pol.event_time <= 0.0 or s.t >= (pol.event_time - ramp - poll_interval)
            # Stand the reserve down on our own once the surge has PASSED: past the event time and
            # load back near the calm baseline. Telemetry-detected, so the judge never has to time
            # the drop (and adjusting V never clears the plan). Only for a scheduled event (>0).
            baseline   = getattr(sim.cfg.traffic, "baseline_rate", 20.0)
            surge_over = pol.event_time > 0.0 and s.t > pol.event_time and s.lam_current <= baseline * 3
            eff_reserve = pol.reserve_servers if (reserve_due and not surge_over) else 1
            eff_min     = max(pol.min_servers, eff_reserve)   # operator SLA floor + scheduled reserve

            applied_c, applied_drop, acted = apply_decision(   # clamp + actuate the two levers
                sim,
                pol.storm_active,
                c_star,                    # capacity always reactive
                pol.malicious_drop_prob,
                pol.queue_hold_threshold,
                current_lam=s.lam_current,
                memory=memory,
                min_servers=eff_min,
            )

            # --- per-tick console trace (observability only) ---
            marker   = "✦" if acted else "·"   # ✦ = state changed this tick, · = no-op
            auto     = memory is not None and memory.should_engage(s.lam_current) and not pol.storm_active  # filter engaged by learning, not the judge
            tag      = "  [auto-engage: learned]" if auto else ""
            print(
                f"[Fast] {marker} step={step:3d}  t={s.t:5.1f}s  "
                f"lam={s.lam_current:5.0f}  q={s.queue_len:4d}  "
                # c = commanded (jumps at once); online = actually booted (ramps behind it at the
                # provisioning delay). They diverge during warm-up — that gap IS the delay working.
                f"c={applied_c:2d}(c*={c_star:2d}) online={s.c_online:2d}  drop={applied_drop:.2f}  "
                f"storm={pol.storm_active}  acted={acted}{tag}"
            )
        # sleep to the next tick, but wake early if the episode ends
        remaining = max(0.0, poll_interval - (time.monotonic() - tick_start)) 
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            pass                             # timeout = the tick's remaining time elapsed; loop again