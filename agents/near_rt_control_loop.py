from __future__ import annotations

import asyncio

from runtime import UP, host as sim_host
from sim.controllers import lyapunov_optimal_c
from agents.policy import SharedPolicy, EpisodeStats

# Release valve: how far above the benign baseline arrival rate the load must be
# for the malicious-drop filter to stay engaged. Baseline is ~20 UEs/s and a storm
# drives lam to ~200, so a 2x ceiling (~40) releases the filter as soon as load is
# clearly benign again while never suppressing a real storm.
BENIGN_LAM_FACTOR = 2.0


def apply_decision(
    sim,
    storm_active:         bool,
    proposed_servers:     int,
    malicious_drop_prob:      float,
    queue_hold_threshold: int = 10,
    current_lam:          float = 0.0,
    release_lam_ceiling:  float | None = None,
    memory=None,
    min_servers:          int = 1,
) -> tuple[int, float, bool]:
    """
    Clamp the proposed servers + policy-gated drop against the safety rules, apply
    them to the sim, and return (applied_servers, applied_drop, acted).
    Server rule : clamp to [1, c_max]; never scale BELOW the current count while
                  the queue is still backed up (queue_len >= queue_hold_threshold).
                  Capacity is otherwise reactive — it follows the proposal (c_star).
                  queue_hold_threshold is a policy knob the Non-RT judge may tune
                  between storms/episodes.
    Drop rule   : apply malicious_drop_prob during a storm, else 0.0. The Non-RT
                  judge decides when to ENGAGE the filter (storm detection needs
                  judgment), but a code-side RELEASE valve disengages it the moment
                  the arrival rate is clearly back to baseline — so legitimate
                  recovery traffic is never dropped in the lag between the storm
                  ending and the judge's next assessment.
    """
    current_c    = sim.c
    current_drop = sim.malicious_drop_prob
    queue_len    = sim.telemetry[-1].queue_len if sim.telemetry else 0

    # --- adaptation lever: servers (reactive) ---
    # Floor at the operator SLA minimum (min_servers), clamp to [1, c_max].
    floor = max(1, min(int(min_servers), sim.cfg.c_max))
    proposed_c = max(floor, min(int(proposed_servers), sim.cfg.c_max))
    if proposed_c < current_c and queue_len >= queue_hold_threshold:
        applied_c = current_c            # hold: don't shed capacity mid-drain
    else:
        applied_c = proposed_c

    # --- absorption lever: drop probability ---
    # Release valve: drop nothing if load is clearly benign, even if the (possibly
    # stale) policy still says storm_active — protects recovery traffic.
    released = (
        release_lam_ceiling is not None
        and current_lam <= release_lam_ceiling
    )
    # Learned auto-engage: once the fast loop knows the storm signature it engages
    # the filter itself, without waiting on the slow LLM verdict.
    auto_engage = memory is not None and memory.should_engage(current_lam)
    if released:
        applied_drop = 0.0
    elif auto_engage:
        applied_drop = memory.storm_drop_level
    elif storm_active:
        applied_drop = malicious_drop_prob
    else:
        applied_drop = 0.0

    acted = (applied_c != current_c) or (abs(applied_drop - current_drop) > 1e-9)
    sim.set_servers(applied_c)
    sim.set_malicious_drop_prob(applied_drop)
    return applied_c, applied_drop, acted


async def run_control_loop(
    policy:        SharedPolicy,
    stop_event:    asyncio.Event,
    poll_interval: float = 1.0,
    stats:         EpisodeStats | None = None,
    memory=None,
    release_valve: bool = True,
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
    while not stop_event.is_set():
        step += 1
        if stats:
            stats.near_rt_steps += 1

        sim = sim_host.sim
        if sim is not None and sim.telemetry:
            s      = sim.telemetry[-1]
            pol    = policy.snapshot()
            # c_star uses the Lyapunov weights the Non-RT judge currently holds
            c_star = lyapunov_optimal_c(
                s, sim.mu_single, sim.cfg.c_max, UP,
                V=pol.lyapunov_V, W=pol.lyapunov_W,
            )

            # release-valve ceiling, derived from the scenario's benign baseline.
            # Ablated (release_valve=False) → None disables the code-side release,
            # so the filter only disengages on the LLM's next storm_active=False.
            if release_valve:
                benign_baseline = min(p.benign_rate for p in sim.cfg.traffic.phases)
                release_ceiling = benign_baseline * BENIGN_LAM_FACTOR
            else:
                release_ceiling = None

            # update the learned storm signature (within-episode learning)
            if memory is not None and memory.learn_within:
                memory.observe(s.lam_current, pol.storm_active)

            applied_c, applied_drop, acted = apply_decision(
                sim,
                pol.storm_active,
                c_star,                    # capacity always reactive
                pol.malicious_drop_prob,
                pol.queue_hold_threshold,
                current_lam=s.lam_current,
                release_lam_ceiling=release_ceiling,
                memory=memory,
                min_servers=pol.min_servers,
            )

            marker   = "✦" if acted else "·"
            auto     = memory is not None and memory.should_engage(s.lam_current) and not pol.storm_active
            tag      = "  [auto-engage: learned]" if auto else ""
            print(
                f"[Fast] {marker} step={step:3d}  t={s.t:5.1f}s  "
                f"lam={s.lam_current:5.0f}  q={s.queue_len:4d}  "
                f"c={applied_c:2d} (c*={c_star:2d})  drop={applied_drop:.2f}  "
                f"storm={pol.storm_active}  acted={acted}{tag}"
            )

        # sleep poll_interval; wake immediately if the episode ends
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass