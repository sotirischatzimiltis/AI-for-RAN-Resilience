"""
Near-RT control loop — PURE CODE, no LLM.

The 1 Hz control loop in the decoupled two-agent design. Each tick it reads
telemetry, computes the Lyapunov-optimal server count in Python, reads the latest
policy snapshot written by the Non-RT-Agent, and applies a clamped action.

Capacity (servers) adapts REACTIVELY every tick; only the absorption lever
(drop_prob) is gated on the Non-RT judge's storm_active verdict. No pydantic
models, no Agent, no MCP round-trip on the tick.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from mcp_server.server import UP, host as sim_host
from sim.controllers import lyapunov_optimal_c
from agents.policy import SharedPolicy, EpisodeStats

# guardrail constant: don't shed servers while the queue is still this backed up
_QUEUE_HOLD_THRESHOLD = 10


@dataclass
class _FastAction:
    """Code-assembled action for a single fast-loop tick."""
    storm_active:       bool
    proposed_servers:   int
    proposed_drop_prob: float


def apply_decision(sim, action: _FastAction, drop_prob_floor: float) -> tuple[int, float, bool]:
    """
    Clamp an action against the safety rules, apply it to the sim, and return
    (applied_servers, applied_drop_prob, acted).

    Server rule : clamp to [1, c_max]; never scale BELOW the current count while
                  the queue is still backed up (queue_len >= _QUEUE_HOLD_THRESHOLD).
                  Capacity is otherwise reactive — it follows the proposal (c_star).
    Drop rule   : during a storm, enforce at least drop_prob_floor; otherwise
                  force 0.0 (the malicious filter has no purpose off-storm).
    """
    current_c    = sim.c
    current_drop = sim.malicious_drop_prob
    queue_len    = sim.telemetry[-1].queue_len if sim.telemetry else 0

    # --- adaptation lever: servers (reactive) ---
    proposed_c = max(1, min(int(action.proposed_servers), sim.cfg.c_max))
    if proposed_c < current_c and queue_len >= _QUEUE_HOLD_THRESHOLD:
        applied_c = current_c            # hold: don't shed capacity mid-drain
    else:
        applied_c = proposed_c

    # --- absorption lever: drop probability (policy-gated) ---
    if action.storm_active:
        applied_drop = max(action.proposed_drop_prob, drop_prob_floor)
    else:
        applied_drop = 0.0
    applied_drop = max(0.0, min(1.0, applied_drop))

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
    Deterministic 1 Hz control loop — no LLM on the tick.

    Each tick:
      1. read latest telemetry from the sim,
      2. compute c_star (Lyapunov drift-plus-penalty) in Python,
      3. read the atomic policy snapshot (storm_active, drop_prob_floor),
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
            c_star = lyapunov_optimal_c(s, sim.mu_single, sim.cfg.c_max, UP)
            pol    = policy.snapshot()

            action = _FastAction(
                storm_active=pol.storm_active,
                proposed_servers=c_star,           # capacity always reactive
                proposed_drop_prob=pol.drop_prob_floor,
            )
            applied_c, applied_drop, acted = apply_decision(sim, action, pol.drop_prob_floor)

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
