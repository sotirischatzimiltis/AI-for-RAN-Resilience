from __future__ import annotations

import asyncio

from runtime import UP, host as sim_host
from sim.controllers import lyapunov_optimal_c
from agents.policy import SharedPolicy, EpisodeStats


def apply_decision(
    sim,
    storm_active:         bool,
    proposed_servers:     int,
    malicious_drop_prob:      float,
    queue_hold_threshold: int = 10,
) -> tuple[int, float, bool]:
    """
    Clamp the proposed servers + policy-gated drop against the safety rules, apply
    them to the sim, and return (applied_servers, applied_drop, acted).
    Server rule : clamp to [1, c_max]; never scale BELOW the current count while
                  the queue is still backed up (queue_len >= queue_hold_threshold).
                  Capacity is otherwise reactive — it follows the proposal (c_star).
                  queue_hold_threshold is a policy knob the Non-RT judge may tune
                  between storms/episodes.
    Drop rule   : apply malicious_drop_prob during a storm, else 0.0 (the malicious
                  filter has no purpose off-storm).
    """
    current_c    = sim.c
    current_drop = sim.malicious_drop_prob
    queue_len    = sim.telemetry[-1].queue_len if sim.telemetry else 0

    # --- adaptation lever: servers (reactive) ---
    proposed_c = max(1, min(int(proposed_servers), sim.cfg.c_max))
    if proposed_c < current_c and queue_len >= queue_hold_threshold:
        applied_c = current_c            # hold: don't shed capacity mid-drain
    else:
        applied_c = proposed_c

    # --- absorption lever: drop probability (policy-gated) ---
    applied_drop = malicious_drop_prob if storm_active else 0.0

    acted = (applied_c != current_c) or (abs(applied_drop - current_drop) > 1e-9)
    sim.set_servers(applied_c)
    sim.set_malicious_drop_prob(applied_drop)
    return applied_c, applied_drop, acted


async def run_control_loop(
    policy:        SharedPolicy,
    stop_event:    asyncio.Event,
    poll_interval: float = 1.0,
    stats:         EpisodeStats | None = None,
) -> None:
    """
    Deterministic 1 Hz control loop
    Each tick:
      1. read latest telemetry from the sim,
      2. compute c_star (Lyapunov drift-plus-penalty) in Python,
      3. read the atomic policy snapshot (storm_active, malicious_drop_prob),
      4. apply a clamped action (capacity reactive; drop gated by storm_active),
      5. sleep to the next tick (waking early when the episode ends).
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

            applied_c, applied_drop, acted = apply_decision(
                sim,
                pol.storm_active,
                c_star,                    # capacity always reactive
                pol.malicious_drop_prob,
                pol.queue_hold_threshold,
            )

            marker = "✦" if acted else "·"
            print(
                f"[Fast] {marker} step={step:3d}  t={s.t:5.1f}s  "
                f"lam={s.lam_current:5.0f}  q={s.queue_len:4d}  "
                f"c={applied_c:2d} (c*={c_star:2d})  drop={applied_drop:.2f}  "
                f"storm={pol.storm_active}  acted={acted}"
            )

        # sleep poll_interval; wake immediately if the episode ends
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass