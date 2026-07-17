# Project Structure

> **Living document — keep it current.** Update this file whenever a script,
> module, prompt, or folder is added, renamed, or repurposed. Last updated: 2026-07-17.

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
   agents/non_rt_agent.py     (LLM storm judge, ~seconds)    prompts/non_rt.md
      │  reads MCP tools, writes SharedPolicy
      ▼
   agents/near_rt_control_loop.py   (fast code loop, ~1 Hz — never blocks on the LLM)
      │  sets server count + malicious-drop filter
      ▼
   sim/simulator.py           (Open RAN control-plane digital twin — SimPy)
```

## Folders

### `sim/` — the simulator (the "world"; no AI)
| File | Role |
|---|---|
| `simulator.py` | SimPy discrete-event engine: UE attach, T300 retries, storms, botnet, servers |
| `config.py` | scenarios & traffic (`single_storm`, `multi_storm`, `multi_storm_flat`), arch constants, stressor knobs |
| `controllers.py` | deterministic controllers (Lyapunov / Fixed / Forecast) — the **baselines** |
| `metrics.py` | resilience P, benign-served & botnet-blocked rates, utility, `resilience_multi` |

### `agents/` — the agentic control layer (the "brain")
| File | Role |
|---|---|
| `orchestrator.py` | network tier: understands operator intents; `run_episode()` (full-system runner) |
| `non_rt_agent.py` | the **LLM storm judge** (the model under comparison); token/cost accounting |
| `near_rt_control_loop.py` | the fast deterministic loop (Lyapunov capacity, filter, release valve) |
| `policy.py` | `SharedPolicy` (judge↔fast-loop handoff) + `EpisodeStats` (counters, LLM usage) |

### The storm judge (`non_rt_agent.py`) — two run modes

The judge's **decision surface** (`PolicyUpdate`) is: `storm_active`, `malicious_drop_prob`
(now an LLM-**calibrated** value in (0,1], not a fixed 0.8), the slow capacity knobs
`lyapunov_V / lyapunov_W / queue_hold_threshold` (applied only when `tighten=true`), and
`reasoning`. It runs in two configurations:

| Setting | **Full system** (`run.py` → `run_episode`) | **Bare judge** (Exp 1) |
|---|---|---|
| Prompt | `prompts/non_rt.md` | `prompts/prompts_mc_non_rt.md` |
| Tools offered | `stats` + `forecast` + `calendar` | `get_episode_stats` only |
| Capacity knobs (V/W/queue_hold) | judge tunes them (`tighten=true`) to pre-provision | **inert** — prompt forces `tighten=false`; capacity is fixed Lyapunov (V=1000, W=1) |
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
| `non_rt.md` | full judge prompt (used by the full system, phases A–E) |
| `orchestrator.md` | operator-intent prompt |
| `prompts_mc_non_rt.md` | trimmed **bare-judge** prompt for Experiment 1 (telemetry-only) |

### `mcp_server/` — tools the judge can call
| File | Role |
|---|---|
| `server.py` | MCP server exposing `get_episode_stats`, `get_forecast`, `get_calendar` |

### `results/` — saved experiment output (JSON + figures)

## Top-level shared modules
| File | Role |
|---|---|
| `runtime.py` | `SimHost` — owns the running episode; the single object every tier reads |
| `forecast.py` | the λ-regression behind `get_forecast` |
| `event_calendar.py` | scheduled-event data behind `get_calendar` |
| `storm_memory.py` | learned storm-signature (within/across-episode learning) |
| `policy_store.py` | persists tuned knobs + learned signature between episodes |
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
| `experiment_model_comparison.py` | **Exp 1: LLM bake-off — self-contained** (own bare-judge episode; does not use `run_episode`) |
| `plot_model_comparison.py` | plot Exp 1 results (`model_comparison.json` → 4-panel `model_comparison.png`) |
| `table_model_comparison.py` | tabulate Exp 1 results (`model_comparison.json` → markdown + `model_comparison_table.tex`) |
| `ablation.py` | Exp B: mechanism knockouts (forecast/calendar/release/learning) |
| `seed_sweep.py` | deterministic baselines across seeds |
| `agent_sweep.py` | live-agent lift vs baseline |
| `learning_curve.py`, `learning_demo.py` | learning experiments |
| `compare_baselines.py`, `make_results.py`, `plot_compare.py` | results tables + figures |

## Runtime notes
- **Interpreter:** use `/Users/admin/miniforge3/envs/pydantic-ai-env/bin/python` (pydantic-ai 1.70). The repo `.venv` has an OLD pydantic-ai that breaks MCP imports.
- **Live runs** source `~/.zshrc` for the OpenRouter key and pass `--model openrouter:<slug>`.

## Experiment plan (phases)
- **Exp 1** — model bake-off → pick the judge LLM (`experiment_model_comparison.py`)
- **A** headline (Static/Lyapunov/Agentic) · **B** ablations · **C** learning curve · **D** robustness (κ, provisioning, cadence) · **E** orchestrator/intents
