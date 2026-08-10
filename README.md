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
agents/                         the three actors (nothing else lives here)
├── orchestrator.py             starts the episode, launches loops, routes operator intents
├── non_rt_agent.py             LLM storm judge — telemetry-window trends → PolicyUpdate
└── near_rt_control_loop.py     PURE-CODE 1 Hz loop — c_star + policy → clamp → actuate

shared/                         state + tool backends shared across tiers (not actors)
├── policy.py                   SharedPolicy (judge↔loop handoff) + EpisodeStats
├── forecast.py                 λ-regression behind the get_forecast MCP tool
├── event_calendar.py           scheduled-event data behind the get_calendar MCP tool
├── storm_memory.py             learned storm-signature (within/across-episode learning)
└── policy_store.py             persists tuned knobs + signature between episodes

mcp_server/
└── server.py                   hosts the running episode (SimHost) + the 3 MCP read tools

sim/                            the simulator (the "world"; no AI) — see sim/README.md
├── config.py                   SimConfig, Open RAN architecture, traffic schedules
├── simulator.py                StormSim: SimPy engine, real-time capable
├── controllers.py              shared lyapunov_optimal_c() + Fixed/Lyapunov baselines
├── metrics.py                  utility u(t) and the A3RT resilience score P
└── README.md                   per-file / per-component guide to sim/

prompts/
├── non_rt_agent_system_prompt.md   full-system storm-judge prompt (default; scheduled-reserve)
├── non_rt_agent_system_prompt_v1.md / _v2.md   archived judge-prompt versions
└── orchestrator.md             operator-intent prompt

scripts/
├── run.py                      full-system episode CLI (Orchestrator → run_episode)
├── run_near_rt.py              bare fast-loop + judge runner (no Orchestrator)
├── exp_1_model_comparison.py         Experiment 1 — LLM model comparison (choose the judge)
├── exp_2_system_comparison.py        Experiment 2 — baselines vs full agentic system
├── exp_3_reserve_sizing.py           Experiment 3 — event-portfolio reserve sizing (attendance estimation)
├── exp_4_V_W_tuning.py               Experiment 4 — V/W × provisioning-delay sweep
├── exp_5_ablation.py                 Experiment 5 — mechanism ablation (forecast / calendar / learning)
└── gui.py                      live GUI viewer of a running episode

experiments/                    curated results per experiment (data, figures, LaTeX)
├── README.md                   the campaign index (Exp 1 + Phases A–E)
└── exp1_model_comparison/      Exp 1 artifacts (json, png, .tex, README)

runtime.py                      SimHost — owns the running episode; every tier reads it
STRUCTURE.md                    detailed directory map (source of truth for layout)
FEATURES.md                     catalog of everything the system models
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
