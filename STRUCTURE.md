# Project Structure

> **Living document — keep it current.** Update this file whenever a script,
> module, prompt, or folder is added, renamed, or repurposed. Last updated: 2026-07-20.

An agentic controller for signaling-storm resilience in Open RAN: a **3-tier control
stack** (Orchestrator → LLM storm judge → deterministic fast loop) sitting on top of a
**SimPy network simulator**, plus scripts to run experiments.

## Architecture at a glance

```
Operator (natural-language intent)
      │
   agents/orchestrator.py     (network tier — SMO/rApp)      prompts/orchestrator.md
      │  sets policy / delegates a standing instruction
      ▼
   agents/non_rt_agent.py     (LLM storm judge, ~seconds)    prompts/non_rt_agent_system_prompt.md
      │  reads MCP tools, writes SharedPolicy
      ▼
   agents/near_rt_control_loop.py   (fast code loop, ~1 Hz — never blocks on the LLM)
      │  sets server count + malicious-drop filter
      ▼
   sim/simulator.py           (Open RAN control-plane digital twin — SimPy)
```

## Folders

### `sim/` — the simulator (the "world"; no AI)
See [`sim/README.md`](sim/README.md) for a full component-by-component breakdown.
| File | Role |
|---|---|
| `README.md` | per-file / per-component guide to the whole `sim/` package |
| `simulator.py` | SimPy discrete-event engine: UE attach, T300 retries, storms, botnet, servers |
| `config.py` | scenarios & traffic (`single_storm`, `multi_storm`, `multi_storm_flat`), arch constants, stressor knobs |
| `controllers.py` | deterministic controllers (Fixed / Lyapunov) — the **baselines** |
| `metrics.py` | resilience P, benign-served & botnet-blocked rates, utility, efficiency, attach-latency, `resilience_multi` |

### `agents/` — the agentic control layer (the "brain")
| File | Role |
|---|---|
| `orchestrator.py` | network tier: understands operator intents; `run_episode()` (full-system runner) |
| `non_rt_agent.py` | the **LLM storm judge** (the model under comparison); token/cost accounting |
| `near_rt_control_loop.py` | the fast deterministic loop (Lyapunov capacity + applies the judge-set filter) |

### `shared/` — state + utilities shared across tiers (not actors)
| File | Role |
|---|---|
| `policy.py` | `SharedPolicy` (judge↔fast-loop handoff) + `EpisodeStats` (counters, LLM usage) |
| `forecast.py` | the λ-regression behind the `get_forecast` MCP tool |
| `event_calendar.py` | scheduled-event data behind the `get_calendar` MCP tool |
| `storm_memory.py` | learned storm-signature (within/across-episode learning) |
| `policy_store.py` | persists tuned knobs + learned signature between episodes (JSON at repo root) |

### The storm judge (`non_rt_agent.py`) — two run modes

The judge's **decision surface** (`PolicyUpdate`) is: `storm_active`, `malicious_drop_prob`
(now an LLM-**calibrated** value in (0,1], not a fixed 0.8), the slow capacity knobs
`lyapunov_V / lyapunov_W / queue_hold_threshold` (applied only when `tighten=true`), and
`reasoning`. It runs in two configurations:

| Setting | **Full system** (`run.py` → `run_episode`) | **Bare judge** (Exp 1) |
|---|---|---|
| Prompt | `prompts/non_rt.md` | `prompts/exp1_model_comparison_non_rt_system_prompt.md` |
| Tools offered | `stats` + `forecast` + `calendar` | `get_episode_stats` only |
| Capacity knobs (V/W/queue_hold) | judge tunes them (`tighten=true`) to pre-provision | **inert** — prompt forces `tighten=false`; capacity is fixed Lyapunov (V=1, W=1) |
| Release valve (code-side filter drop) | on | off |
| Learned auto-engage | optional (`--learn-*`) | off |
| Operator intents | yes | none |
| Reasoning on/off | model default | explicitly toggled (bake-off ablation) |
| Isolates | full agentic performance | **raw model judgment** (storm + drop only) |

In Exp 1 the capacity knobs still exist in the shared `PolicyUpdate` schema but are
neutralized, so every model faces an identical fixed-capacity baseline and the comparison
measures only `storm_active` + `malicious_drop_prob`.

### `prompts/` — system prompts the LLMs read
| File | Role |
|---|---|
| `non_rt_agent_system_prompt.md` | full judge prompt (used by the full system, phases A–E) |
| `orchestrator.md` | operator-intent prompt |
| `exp1_model_comparison_non_rt_system_prompt.md` | trimmed **bare-judge** prompt for Experiment 1 (telemetry-only) |

### `mcp_server/` — tools the judge can call
| File | Role |
|---|---|
| `server.py` | MCP server exposing `get_episode_stats`, `get_forecast`, `get_calendar` |

### `results/` — saved experiment output (JSON + figures)

## Top-level shared modules
| File | Role |
|---|---|
| `runtime.py` | `SimHost` — owns the running episode; the single object every tier reads |
| `FEATURES.md` | catalog of everything the system models |
| `README.md`, `requirements.txt` | docs + dependencies |

## `scripts/` — runners & experiments

**Core runners (the framework):**
| Script | Role |
|---|---|
| `run.py` | **main runner — full system, all capabilities** (phases A–E go through this) |
| `run_near_rt.py` | no-LLM Lyapunov baseline runner |
| `gui.py` | demo dashboard |

**Experiment scripts:**
| Script | Experiment |
|---|---|
| `exp1_model_comparison_non_rt.py` | **Exp 1: LLM model selection — self-contained** (own bare-judge episode; does not use `run_episode`) |
| `exp_2_system_comparison.py` | **Exp 2: system comparison** — Static(c=1/8/16) + Lyapunov vs full agentic (gemini); self-contained |
| `exp_3_V_W_tuning.py` | **Exp 3: V/W × provisioning-delay** sweep (no LLM); resilience–cost trade-off |
| `ablation.py` | mechanism knockouts (forecast/calendar/release/learning) |
| `learning_curve.py`, `learning_demo.py` | learning experiments |
| `plot_vw_tuning.py` | Exp 3 figures (delay-lines / heatmaps / Pareto) |

## Runtime notes
- **Interpreter:** use `/Users/admin/miniforge3/envs/pydantic-ai-env/bin/python` (pydantic-ai 1.70). The repo `.venv` has an OLD pydantic-ai that breaks MCP imports.
- **Live runs** source `~/.zshrc` for the OpenRouter key and pass `--model openrouter:<slug>`.

## Experiment plan (phases)
- **Exp 1** — model bake-off → pick the judge LLM (`exp1_model_comparison_non_rt.py`)
- **A** headline (Static/Lyapunov/Agentic) · **B** ablations · **C** learning curve · **D** robustness (κ, provisioning, cadence) · **E** orchestrator/intents
