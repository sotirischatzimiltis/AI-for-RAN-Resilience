# AI-for-RAN Resilience

Agentic simulation framework for studying signaling-storm resilience in Open RAN networks.

---

## Overview

A signaling storm occurs when a burst of UE attach requests overwhelms the control-plane processing capacity of a CU/DU. Retries amplify the load in a self-reinforcing loop — behaviour that analytical M/M/c models cannot capture. This repository contains the framework internals:

- A discrete-event simulator (`sim/`) that reproduces the storm dynamics, calibrated to the Open RAN delay model from [arXiv:2505.00605](https://arxiv.org/abs/2505.00605).
- A **decoupled two-agent control system** (`agents/`) that keeps the network resilient during a storm.

---

## Architecture — decoupled two-agent design

Two LLM-adjacent components sit above a deterministic fast loop:

```
 Orchestrator (code coordinator)   starts the episode, launches the loops,
                                    routes operator intents; idle otherwise.

 Non-RT-Agent (LLM, ~10 s cadence)  the STORM JUDGE. Reads a telemetry *window*
                                    (trends, not one instant), decides storm-vs-
                                    noise, and writes storm_active + malicious_drop_prob
                                    into shared policy. Never blocks the fast loop.

 Fast control loop (pure code, 1 Hz)  reads telemetry, computes the Lyapunov-optimal
                                      server count, reads the policy snapshot, clamps,
                                      and actuates. NO LLM on the tick.
```

The key idea: **capacity adapts reactively every second** (the fast loop always
follows `c_star`), while **only the malicious-UE filter waits on the LLM's storm
verdict**. Filtering is the one lever that benefits from judgment; capacity never
waits for it.

---

## Repository layout

```
agents/
├── orchestrator.py         starts the episode, launches loops, routes intents
├── non_rt_agent.py         LLM storm judge — telemetry-window trends → policy
├── near_rt_control_loop.py PURE-CODE 1 Hz loop — c_star + policy → clamp → actuate
└── policy.py               SharedPolicy: atomic storm_active / malicious_drop_prob handoff

mcp_server/
└── server.py               hosts the running episode (SimHost) + get_episode_stats

sim/
├── config.py               SimConfig, Open RAN architecture, traffic schedules
├── simulator.py            StormSim: SimPy engine, real-time capable
├── metrics.py              utility u(t) and the A3RT resilience score P
└── controllers.py          shared lyapunov_optimal_c() + classical baselines

prompts/
└── non_rt.md               system prompt for the Non-RT storm judge
```

---

## The two control levers

The simulator exposes two runtime actuators, mapped to the two resilience mechanisms:

- **Adaptation — `set_servers(c)`**: the commanded server count. Driven every tick by the fast loop from the Lyapunov-optimal `c_star`. A guardrail refuses to shed servers while the queue is still draining.
- **Absorption — `set_malicious_drop_prob(p)`**: fraction of botnet UEs dropped at admission. Gated by the Non-RT judge's `storm_active` verdict (`malicious_drop_prob` during a storm, `0.0` otherwise).

Resilience is scored with the A3RT metric **P = 0.4·absorption + 0.4·adaptation + 0.2·trec**.

---

## Fast-loop control flow

```
every 1 s (no LLM):
    s       = latest telemetry sample
    c_star  = lyapunov_optimal_c(s, ...)          # Python, in-process
    pol     = policy.snapshot()                   # atomic: storm_active, drop_floor
    action  = (servers = c_star,                  # capacity always reactive
               drop    = pol.malicious_drop_prob if pol.storm_active else 0.0)
    apply_decision(sim, action, pol.malicious_drop_prob)   # clamp + actuate
```

---

## Dependencies

- [SimPy](https://simpy.readthedocs.io/) — discrete-event simulation
- [pydantic-ai](https://ai.pydantic.dev/) — LLM agent framework (Non-RT judge)
- [FastMCP](https://github.com/jlowin/fastmcp) — MCP server exposing `get_episode_stats`

The Non-RT judge runs with any OpenAI-compatible model (tested with `openai/gpt-4o-mini` via OpenRouter).
