# `sim/` — the network simulator (the "world")

This package is the **digital twin**: a SimPy discrete-event model of Open RAN
control-plane UE attachment under a signaling storm. It has **no AI** — it just
simulates the network and exposes two actuators (`set_servers`, `set_malicious_drop_prob`)
and a stream of telemetry. Controllers and agents live in `../agents/` and act on it
from the outside.

```
              ┌──────────────── config.py ────────────────┐
              │ SimConfig: arch delays, RRC/T300, traffic  │
              │ scenarios (single / multi-storm), knobs    │
              └───────────────────┬────────────────────────┘
                                  │ builds
                                  ▼
   set_servers() ───────►  ┌─────────────────────────┐  ──► telemetry: [TelemetrySample]
   set_malicious_drop_prob ►│      simulator.py       │  ──► stats: Stats (counters + latency)
                            │  StormSim (SimPy engine)│
   controllers.py  ────────►│  run(controller=...)    │
   (Fixed / Lyapunov)       └─────────────────────────┘
                                  │ telemetry + stats
                                  ▼
                            ┌──────────────┐
                            │  metrics.py  │  resilience P, benign/botnet rates,
                            │  (scoring)   │  utility, efficiency, latency
                            └──────────────┘
```

Import path: run modules with the repo interpreter as `python -m ...`; `sim` is a
regular package (`__init__.py` is an empty marker).

---

## `config.py` — the knobs and scenarios

Everything the simulation needs *before* it runs: delay physics, retry policy, and
the traffic timeline. All plain dataclasses (pure data + a few derived helpers).

| Component | What it is / does |
|---|---|
| `ArchConfig` | Per-attach control-plane delay accounting. Fields: `n_ctrl_messages` (M=3 RRC msgs), `proc_total_ms` (30 ms internal processing), `oneway_delay_ms` (RU→CU one-way). |
| &nbsp;&nbsp;`.service_time_ms()` | Mean service time of one attach = `proc_total_ms + M · oneway_delay_ms`. |
| &nbsp;&nbsp;`.service_rate()` | Per-server rate μ (UEs/s) = `1000 / service_time_ms`. |
| `open_ran_arch()` | Builder: `ArchConfig` with `oneway=1.60 ms` (Open RAN 7.2x split + F1). |
| `monolithic_arch()` | Builder: `oneway=0.25 ms` (monolithic Split-7 baseline). |
| `RRCConfig` | Retry policy. `t300_ms` (T300 guard timer, 1000 ms), `max_attempts` (5), `backoff_ms` (extra wait before retry, 0). |
| `StormPhase` | One constant-rate traffic interval: `t_start`, `t_end`, `benign_rate`, `botnet_rate`, `label`. |
| `TrafficConfig` | Ordered list of `StormPhase`s = the whole traffic profile. |
| &nbsp;&nbsp;`.horizon()` | Total scenario duration (largest phase end time). |
| &nbsp;&nbsp;`.rates_at(t)` | The `(benign, botnet)` arrival rates active at time `t`. |
| &nbsp;&nbsp;`.storm_windows()` | Reduces the phases to `[(t0, td), …]` — the merged windows where load is elevated. Ground-truth "when the storms were," used for scoring. |
| `SimConfig` | Top-level config bundling `arch` + `rrc` + `traffic` plus run knobs: `c0`/`c_max` (initial/max servers), `sample_dt_s`, `seed`, `realtime`/`rt_factor`, `botnet_attach_period_ms`, contention knobs (`compute_kappa`, `compute_rho_cap`), `server_provision_delay_s`. |
| &nbsp;&nbsp;`.__post_init__` | Validates `compute_rho_cap ∈ [0, 1)` (a cap ≥ 1 would divide by zero in the contention model). |
| `single_storm_traffic()` | The prior paper's scenario: 20 → 200 → 20 UEs/s, one benign-only storm. |
| `multi_storm_traffic()` | Three storms of **growing** intensity with a botnet component (evolution demo). |
| `multi_storm_flat_traffic()` | N **identical** storms on a compressed timeline — the fair variant for the LLM bake-off and learning curve (later-storm gains are attributable to learning, not an easier storm). |

---

## `simulator.py` — the discrete-event engine

The heart of the package. Models each UE performing the RRC attach against a pool of
`c` servers, with an explicit T300 timer and retries — the retry loop is what makes a
signaling storm self-amplify.

### Data records
| Component | What it is / does |
|---|---|
| `TelemetrySample` | One state snapshot at time `t`: `lam_current` (arrival rate), `queue_len`, `busy`, `in_system`, `c` (servers online), `c_target` (commanded), plus cumulative counters (`completed`, `failed`, `retries`, `arrivals`, `malicious_arrivals`, `malicious_dropped`). One is appended every `sample_dt_s`. |
| `Stats` | Whole-episode accounting. Cumulative counters split by class (`benign_completed/failed`, `malicious_dropped/failed`) so we can tell "real users served" from "botnet blocked". Also three **index-aligned** per-success lists: `completion_delays` (attach latency ms), `completion_times` (when), `completion_benign` (was it a real user) — the raw material for latency-under-storm. |
| `_Attempt` | One attach **attempt** (a UE spawns a fresh one per retry). Fields: `ue_id`, `malicious`, `served_evt` (SimPy event the server fires on success), `abandoned` (T300 expired), `in_service` (a server took it). `abandoned`+`in_service` resolve the timeout-vs-pickup race so each attempt is counted once. |

### `StormSim` — the engine object
| Method | What it does |
|---|---|
| `__init__(cfg)` | Builds the SimPy env (real-time-paced or virtual), the RNG, initial state, and **registers the four concurrent processes** below. Clamps `c0` to `[1, c_max]`. |
| `set_servers(c)` | **Actuator.** Set the commanded server count (clamped `[1, c_max]`); wakes the provisioning manager to reconcile. Called by controllers/agents. |
| `set_malicious_drop_prob(p)` | **Actuator.** Set the admission filter strength: fraction of *malicious* attempts dropped at admission (`[0, 1]`). |
| `mu_single` (property) | Per-server unloaded service rate μ. |
| `_signal()` | Re-arms the dispatcher wake event (one-shot SimPy event pattern). |
| `_arrival_process()` | **Process 1.** Poisson arrivals of benign + botnet UEs per the traffic schedule (or a live override). |
| `_spawn_ue(malicious)` | Creates one UE's attach process and bumps the arrival counter. |
| `_ue_attach(...)` | **A single UE's lifecycle:** admission filter (drops malicious per `malicious_drop_prob`), then up to `max_attempts` attempts racing service against the T300 timer; on success records latency; on exhaustion records a class-tagged failure. |
| `_dispatcher()` | **Process 2.** Assigns waiting attempts to free servers whenever `busy < c_online`. |
| `_provisioning_manager()` | **Process 3.** Reconciles `c_online` toward the commanded `c`: scale-up is gradual (`server_provision_delay_s` per server, one at a time), scale-down is immediate. |
| `_serve(att)` | Holds a server for `_service_time()`, then fires `att.served_evt` (unless abandoned). |
| `_service_time()` | Exponential service time; if `compute_kappa` is set, inflates the processing component by the processor-sharing factor `1/(1-ρ_c)` (shared-compute contention). |
| `_telemetry_process()` | **Process 4.** Appends a `TelemetrySample` every `sample_dt_s`. |
| `run(until, controller)` | Runs the sim to the horizon; if a `controller` is given, drives it via `_control_loop`. Returns the telemetry list. |
| `_control_loop(controller)` | Calls `controller.step(sim, latest_sample)` every `sample_dt_s`. |

---

## `controllers.py` — the deterministic baselines

Non-AI controllers that act on `c(t)`. Each exposes `.step(sim, sample)` and is passed
to `StormSim.run(controller=…)`. These are the **baselines** the agentic system is
compared against.

| Component | What it does |
|---|---|
| `lyapunov_optimal_c(s, mu, c_max, util_p, lam, V, W)` | Pure function: integer search for the server count minimizing the **drift-plus-penalty** objective `Lq·(λ−cμ) + ½(λ−cμ)² − V·u(c) + W·c`. Shared by the controller and the Near-RT loop. |
| `FixedController(c)` | Constant server count (`Static c=1/8/16` baselines). |
| `LyapunovController(V, W, util_p)` | Dynamic `c(t)` from the drift-plus-penalty optimum each tick. `_lambda_estimate` returns the current arrival rate (a forecast variant could look ahead). |

---

## `metrics.py` — scoring

Turns `telemetry` + `stats` into the numbers the paper reports. See the inline comments
in the file for the math; summary of components:

| Component | What it computes |
|---|---|
| `benign_success_rate(stats)` | Fraction of legitimate users eventually served — the "did real users get service?" number. |
| `malicious_blocked_rate(stats)` | Fraction of botnet denied service (drops **or** starvation-failures) — an *outcome* axis; read next to benign rate. |
| `malicious_filtered_rate(stats)` | Fraction of botnet **deliberately dropped** by the filter — isolates the *defense mechanism* (no-filter baseline scores 0). |
| `per_storm_blocked(telemetry, storms)` | Per-storm botnet drop fraction from the cumulative counters (`counter_value_at` reads a counter at a given time). |
| `avg_servers(telemetry)` | Mean online servers — a **capacity-cost** proxy. |
| `resilience_efficiency(P, avg_servers, c_max)` | Resilience per unit capacity (`P / (avg_servers/c_max)`) — reporting companion; read only next to P. |
| `attach_latency_stats(stats, storms, benign_only)` | Attach-latency mean / p50 / **p95** (ms), optionally restricted to completions **inside a storm window** (`_percentile` is the hand-rolled percentile). |
| `UtilityParams` / `utility` / `utility_series` | The instantaneous utility `u(t) ∈ [0,1]` (two logistic terms on arrival-rate headroom and queue length) and its time series. `UtilityParams.__post_init__` enforces `wA+wB=1`. |
| `ResilienceWeights` | The `w1/w2/w3` blend (absorption / adaptation / time-to-recovery). `__post_init__` enforces `w1+w2+w3=1`. |
| `_trapz(ys, xs)` | Trapezoidal integral of the utility curve over time. |
| `resilience_score(...)` | Single-storm **A3RT resilience P**: absorption + adaptation (utility area vs. a local baseline `u_des`, capped at 1) + a recovery-time term. |
| `resilience_multi(...)` | Scores **each storm** against its own local baseline and returns per-storm P plus an equal-weighted episode aggregate (`P_episode`) — surfaces the learning/evolution trend. |

---

## `__init__.py`

Empty file — marks `sim/` as an importable Python package. No logic, no re-exports;
import each module by its full path (`from sim.config import SimConfig`, etc.).

---

## Minimal usage

```python
from sim.config import SimConfig, open_ran_arch, single_storm_traffic
from sim.simulator import StormSim
from sim.controllers import LyapunovController
from sim.metrics import resilience_multi, UtilityParams

cfg = SimConfig(arch=open_ran_arch(), traffic=single_storm_traffic(), c0=1, c_max=16)
sim = StormSim(cfg)
sim.run(controller=LyapunovController(V=1000, W=1))

storms = cfg.traffic.storm_windows()
print(resilience_multi(sim.telemetry, sim.mu_single, UtilityParams(), storms))
```

For the full agentic stack (LLM judge + fast loop) that drives these same actuators,
see [`../agents/`](../agents/) and the top-level [`../STRUCTURE.md`](../STRUCTURE.md).
