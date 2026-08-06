# Project Structure

> **Living document — keep it current.** Update this file whenever a script,
> module, prompt, or folder is added, renamed, or repurposed. Last updated: 2026-08-03.

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
| `config.py` | scenarios & traffic (`single_storm`, `botnet_event` (exp_1), `mixed_storm` ×4, `multi_storm_flat`/`_ramp`, `event_surge`), arch constants, stressor knobs |
| `controllers.py` | deterministic controllers (Fixed / Lyapunov) — the **baselines** |
| `metrics.py` | resilience P, benign-served & botnet-blocked rates (episode + **per-storm-window**: `per_storm_benign_served`/`per_storm_blocked`), utility, efficiency, attach-latency, `resilience_multi`. **Utility (`utility_parts`):** uA is UTILISATION-based — `uA=1/(1+exp(kA·(rho−mfracA)))`, `rho=lam/(c·mu)`; `kA`(=κ)=10 is the c-independent steepness, `mfracA`=0.90 the utilisation knee (uA=0.5 at rho=0.9). ONE utility shared by the LyapunovController and the score. **Baseline-at-rest (`resilience_multi`/`recovery_report`):** per-storm `u_des` is averaged over calm samples at REST capacity (utilisation ρ≥0.5), skipping the pre-provisioning ramp that lands in [t0-lookback,t0] — else the reserve inflates the baseline and the recovery bar becomes unreachable (cell returns to true-rest u<0.95·inflated → never "recovers"). **Diagnostic decomposition (does NOT change P):** `utility_parts` splits u into uA (capacity-margin)+uB (queue-health); `utility_decomposition` reports per-window mean uA/uB/rho; `recovery_report` exposes the detector internals (u_des, target, recovered, post_peak_u) so a censored `tr` can be checked against the logs |

### `agents/` — the agentic control layer (the "brain")
| File | Role |
|---|---|
| `orchestrator.py` | network tier: understands operator intents; `run_episode()` (full-system runner) |
| `non_rt_agent.py` | the **LLM storm judge** (the model under comparison); token/cost accounting |
| `rule_based_controller.py` | the judge prompt's decision tree as **deterministic rules** (no LLM); the Exp 2 baseline that isolates what the LLM adds |
| `near_rt_control_loop.py` | the fast deterministic loop (Lyapunov capacity + applies the judge-set filter) |

### `shared/` — state + utilities shared across tiers (not actors)
| File | Role |
|---|---|
| `policy.py` | `SharedPolicy` (judge↔fast-loop handoff) + `RunStats` (counters, LLM usage). Carries the **scheduled reserve**: `reserve_servers` + `event_time` (fast loop applies it from `event_time − ramp_time`, so servers are online by the event without provisioning too early; `event_time=0` = apply now, rule/operator path) |
| `forecast.py` | the λ-regression behind the `get_forecast` MCP tool |
| `event_calendar.py` | scheduled-event data behind the `get_calendar` MCP tool. `summarize_calendar(..., committed)` annotates events the judge has already provisioned a reserve for ("do NOT re-estimate") so it acts on each event ONCE, while the event stays visible so its surge is still classified benign (`SimHost.mark_event_committed`; reset each episode) |
| `events.py` | **Exp 4** event portfolio: `VenueEvent` + the 12 real events (reserve-sizing ground truth) |
| `verdict.py` | shared `Verdict` record both judges (rule + LLM) emit — the agreement/shadow join |
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
| `non_rt_agent_system_prompt.md` | full judge prompt v1 (default; used by the full system, phases A–E) |
| `non_rt_agent_system_prompt_v2.md` | judge prompt v2 (A/B candidate: explicit calendar-timing in-progress/upcoming/none + persistence rules). Run via `exp_1_llm_judges --prompt <file> --tag v2` |
| `non_rt_agent_system_prompt_v3.md` | judge prompt v3: **scheduled one-shot reserve** — judge writes `expected_attendance` + `event_time` once; the fast loop provisions the reserve at `event_time − ramp_time` (see `near_rt_control_loop.ramp_time`). Calendar now gives absolute times |
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
| `exp_1_llm_judges.py` | **Exp 1 — THE BASE**: non-rt-agent LLM comparison to **choose the judge model**. Self-contained (owns `run_agentic`, `_agg`, roster). Sweeps the 5 models on ONE tricky scenario (`botnet_event`: a botnet ramp for get_forecast + a real England-v-Brazil event surge for get_calendar reasoning); scores P/benign/cost AND the judge's crowd estimate vs ground truth; downselects the winner + checkpoints per model (`--resume`). Also dumps each episode's per-assessment reasoning trace to `experiments/exp1_llm_judges/reasoning/*.jsonl` (`_dump_traces`: what the judge saw + its reasoning + decision + held plan, one record per cycle) |
| `exp1_model_comparison_non_rt.py` | retired bare-judge bake-off (kept for reference; no longer imported by the base) |
| `exp_2_system_comparison.py` | **Exp 2: system comparison** — Static(c=1/8/16) + Lyapunov + rule vs full agentic (Exp 1 winner). Likely to be folded away; exp_1 is the base and does not depend on it |
| `exp_3_V_W_tuning.py` | **Exp 3: V/W × provisioning-delay** sweep (no LLM); resilience–cost trade-off |
| `exp_4_reserve_sizing.py` | **Exp 4: reserve sizing** — flat rule vs formula rule vs LLM on the event portfolio (Non-RT justification) |
| `ablation.py` | mechanism knockouts (forecast/calendar/release/learning) |
| `learning_curve.py`, `learning_demo.py` | learning experiments |
| `plot_vw_tuning.py` | Exp 3 figures (delay-lines / heatmaps / Pareto) |

## Runtime notes
- **Interpreter:** use `/Users/admin/miniforge3/envs/pydantic-ai-env/bin/python` (pydantic-ai 1.70). The repo `.venv` has an OLD pydantic-ai that breaks MCP imports.
- **Live runs** source `~/.zshrc` for the OpenRouter key and pass `--model openrouter:<slug>`.

## Experiment plan (phases)
- **Exp 1** — LLM judges in the full agentic loop (headline result) → downselect the winning model for Exp 2–4 (`exp_1_llm_judges.py`)
- **Exp 2** — system comparison (baselines + rule vs the Exp 1 winner)
- **Exp 3** — V/W × provisioning-delay sweep · **Exp 4** — reserve sizing (calendar judgement)
- **A** headline (Static/Lyapunov/Agentic) · **B** ablations · **C** learning curve · **D** robustness (κ, provisioning, cadence) · **E** orchestrator/intents
