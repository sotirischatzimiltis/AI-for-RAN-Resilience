# AI-for-RAN Resilience

Agentic simulation framework for studying signaling-storm resilience in Open RAN networks. Built to support the IEEE TNSE paper *"AI-for-RAN Resilience"*.

---

## Overview

A signaling storm occurs when a burst of UE attach requests overwhelms the control-plane processing capacity of a CU/DU. Retries amplify the load in a self-reinforcing loop — behaviour that analytical M/M/c models cannot capture. This repository provides:

- A discrete-event simulator (`sim/`) that reproduces the storm dynamics, calibrated to the Open RAN delay model from [arXiv:2505.00605](https://arxiv.org/abs/2505.00605).
- An LLM-based Near-RT-Agent (`agents/`) that watches live telemetry and autonomously decides when and how to intervene.

---

## Repository Structure

```
sim/
├── config.py        — SimConfig, architecture parameters, traffic schedules
├── simulator.py     — StormSim: SimPy discrete-event engine with real-time mode
├── metrics.py       — Utility function u(t) and A3RT resilience score P
└── controllers.py   — Baseline controllers (Fixed, Lyapunov, ForecastLyapunov)

agents/
└── near_rt_agent.py — Near-RT-Agent: autonomous 1-second LLM control loop

prompts/
└── near_rt.md       — System prompt for the Near-RT-Agent
```

---

## Simulation (`sim/`)

### `config.py`
Defines `SimConfig` and all tunable parameters:
- **Architecture**: Open RAN delay model (O-FH + F1 latency, service rate μ)
- **RRC timers**: T300 expiry, max retries, backoff
- **Traffic schedules**: `single_storm_traffic()` — 20 → 200 → 20 UEs/s; `multi_storm_traffic()` — three escalating storms
- **Control knobs**: `c_max`, `server_provision_delay_s`, `compute_kappa`

### `simulator.py`
`StormSim` — the core SimPy engine:
- Poisson arrivals (benign + botnet), UE attach lifecycle with T300 timer and retries
- Two runtime actuators callable by controllers or agents:
  - `set_servers(c)` — adaptation lever: change commanded server count
  - `set_malicious_drop_prob(p)` — absorption lever: drop fraction of botnet UEs at admission
- `TelemetrySample` recorded every `sample_dt_s` seconds: `t, lam_current, queue_len, busy, c, retries, ...`
- Supports both accelerated (`rt_factor > 1`) and true real-time (`rt_factor = 1`) modes

### `metrics.py`
- `utility(s, mu, params)` — composite score combining arrival-rate headroom and queue pressure (0–1)
- `resilience_score(telemetry, ...)` — A3RT metric P = 0.4·absorption + 0.4·adaptation + 0.2·trec

### `controllers.py`
Classical baselines used as comparison points in the paper:
- `FixedController` — static server count
- `LyapunovController` — drift-plus-penalty optimal c(t) computed each tick
- `ForecastLyapunov` — Lyapunov + 1-step arrival-rate forecast

---

## Near-RT Agent (`agents/`)

`near_rt_agent.py` implements an LLM agent running at the Near-RT RIC timescale (≈1-second cycles).

**Key design decisions:**
- Telemetry is pre-injected into every prompt directly from Python memory — no MCP HTTP round-trip for observation, keeping the agent as close to real-time as possible.
- The agent has three action-oriented tools: `compute_lyapunov()`, `set_servers(c)`, `set_drop_prob(p)`.
- The agent decides autonomously when to act (HOLD / MONITOR / ACT / RECOVER) — it is not scripted to call all tools every cycle.
- The system prompt lives in `prompts/near_rt.md` and is loaded at import time.

**Control loop:**
```
while episode running:
    snapshot ← read sim telemetry directly (Python, no HTTP)
    decision ← LLM(system_prompt + policy_context + snapshot)
    if action_taken:
        call set_servers() and/or set_drop_prob() via MCP
    sleep poll_interval (interruptible by episode-end event)
```

---

## Dependencies

- [SimPy](https://simpy.readthedocs.io/) — discrete-event simulation
- [pydantic-ai](https://ai.pydantic.dev/) — LLM agent framework with structured outputs
- [FastMCP](https://github.com/jlowin/fastmcp) — MCP server exposing simulator tools

The agent can run with any OpenAI-compatible model (tested with `openai/gpt-4o-mini` via OpenRouter).
