# Features & System Model

What the simulation and the agentic framework model. Split into the **simulation**
(an Open RAN control-plane digital twin) and the **framework** (the agentic control
layer on top). Delay/overhead values are reconstructed from Chatzimiltis et al.,
*"Surviving the Storm: The Impacts of Open RAN Disaggregation on Latency and
Resilience"* (arXiv:2505.00605).

Legend: **[default on]** modelled always · **[optional]** off by default, enabled via config.

---

## 1. Simulation — Open RAN control-plane digital twin

Discrete-event (SimPy) model of UE RRC attach through a finite, scalable server pool
under benign + malicious load.

### 1.1 UE attach lifecycle & signaling-storm dynamics
- **RRC connection setup per UE** `[default on]` — every UE is a process attempting
  attach; `sim/simulator.py::_ue_attach`.
- **T300 guard timer + retries** `[default on]` — a UE unserved within `t300_ms`
  (default 1000 ms) abandons and retries, up to `max_attempts` (default 5), then is
  counted as *failed*. Optional `backoff_ms` before a retry.
  `sim/config.py::RRCConfig`.
- **Retry-driven storm amplification** `[default on]` — abandoned attempts re-enter the
  queue, so overload feeds itself. This is the mechanism that turns a load spike into a
  *storm*.
- **Botnet aggressive re-attach** `[default on]` — an admitted malicious UE re-attaches
  every `botnet_attach_period_ms` (default 200 ms), the DDoS-style signaling flood.

### 1.2 Latency / service-time model (the disaggregation cost)
- **Per-attach service time = internal processing + M·(one-way delay)** `[default on]` —
  `service_time = proc_total_ms + n_ctrl_messages × oneway_delay_ms`.
  Open RAN RU→CU one-way = 1.60 ms (0.10 O-FH + 1.50 F1) vs monolithic 0.25 ms, giving
  per-server **μ ≈ 28.7 UEs/s (Open RAN)** vs **32.5 (monolithic)**. The disaggregation
  latency penalty is reproduced explicitly. `sim/config.py::ArchConfig`,
  `open_ran_arch()` / `monolithic_arch()`.
- **Shared-compute contention (processor sharing)** `[optional]` — when `compute_kappa`
  is set, the *processing* component of each attach inflates by `1/(1 − ρc)`, with
  `ρc = busy_workers / kappa` (clamped by `compute_rho_cap`). Concurrency slows every
  attach; propagation delay is unaffected. Off by default (recovers the paper's numbers).
  `sim/simulator.py::_service_time`.

### 1.3 Capacity / servers
- **Multi-server queue** `[default on]` — `c` servers, FIFO wait queue, a dispatcher
  assigns waiting attempts to free servers, exponential service times. `c0` initial,
  `c_max` cap. `sim/simulator.py::_dispatcher`.
- **Server provisioning delay (warm-up)** `[optional]` — with `server_provision_delay_s`,
  newly commanded servers come online **one at a time, `delay` s apart** (image pull /
  boot / pool attach of a vDU/vCU). **Scale-down is immediate** with no preemption of
  in-flight attaches. Default 0 = instant. `sim/simulator.py::_provisioning_manager`.
- **Online vs commanded capacity** `[default on]` — telemetry exposes both `c` (online)
  and `c_target` (commanded), so warm-up lag is observable.

### 1.4 Traffic & attack
- **Poisson benign + botnet arrivals** `[default on]`, time-varying via a phase timeline;
  each arrival is benign or malicious by the botnet share. `sim/config.py::TrafficConfig`.
- **Scenarios** `[default on]` — `single_storm` (20→200→20 UEs/s, **no botnet**) and
  `multi_storm` (three escalating storms, botnet 40 / 60 / 80). Arbitrary phase timelines
  supported; `storm_windows()` extracts each storm for scoring.
- **Malicious-UE admission filter (rate limiter)** `[default on]` — drops botnet UEs at
  admission with probability `malicious_drop_prob`. **Botnet-targeted only** — legitimate
  UEs are never dropped by the filter.

### 1.5 Metrics & instrumentation
- **Utility u(t) ∈ [0,1]** `[default on]` — combines arrival-rate pressure and
  queue-length terms. `sim/metrics.py::utility`.
- **A3RT resilience score P** `[default on]` — absorption + adaptation + time-to-recovery,
  each **capped to [0,1]** (so pre-provisioning above baseline can't push P past 1), scored
  **per storm** against a local pre-storm baseline and aggregated to a **whole-episode P**.
  `resilience_score`, `resilience_multi`.
- **Per-class outcome accounting** `[default on]` — benign vs malicious
  completed / failed / dropped, giving **benign-served rate** (did legit users get service?)
  and **botnet-blocked rate** (how much attack was denied?). Report P *alongside* these,
  never alone. `benign_success_rate`, `malicious_blocked_rate`, `per_storm_blocked`.
- **Telemetry sampling** `[default on]` — every `sample_dt_s` (0.5 s): λ, queue, busy,
  in-system, c-online, c-commanded, cumulative completed / failed / retries / arrivals and
  the malicious split.
- **Real-time pacing** `[optional]` — `realtime` + `rt_factor` pace the sim clock to
  wall-clock time for live agentic runs and dashboards; off (virtual time) for batch
  experiments.

---

## 2. Framework — the agentic control layer

A decoupled hierarchy: a network-level **Orchestrator**, a per-site **Non-RT LLM judge**,
and a deterministic **fast control loop**, over the simulation above.

### 2.1 Control architecture
- **Decoupled two-timescale control** — deterministic fast loop (~1 Hz) never blocks on the
  LLM; the Non-RT judge runs asynchronously (~seconds). `agents/near_rt_control_loop.py`,
  `agents/non_rt_agent.py`.
- **Lyapunov capacity control** — drift-plus-penalty optimiser picks the server count each
  tick; weights **V** (utility/QoS) and **W** (server cost) are tunable. Capacity stays
  reactive in code for safety and speed. `sim/controllers.py::lyapunov_optimal_c`.
- **LLM storm judge** — reads a telemetry-window trend summary, decides storm-vs-noise and
  the filter strength, writes a `PolicyUpdate` the fast loop enacts.

### 2.2 Reads (MCP tools) and anticipation
- **MCP tools** — `get_calendar` (known scheduled load events), `get_forecast` (short-term
  regression of λ / retry-rate / fail-rate / queue with trend + confidence),
  `get_episode_stats` (cumulative resilience + counters). `mcp_server/server.py`.
- **Pre-provisioning** — raise V ahead of demand on **either** a scheduled calendar event
  **or** a confident forecast ramp (an unscheduled surge).

### 2.3 Reactive security & learning
- **Release valve** — the fast loop drops the filter the instant load returns to baseline,
  without waiting for the LLM, so recovery traffic is never over-filtered.
- **Learned storm-signature auto-engagement** — after weathering a storm the fast loop
  learns the benign baseline and a storm-onset threshold, then engages the filter itself
  with no LLM latency. Two toggleable timescales: **within-episode** (`--learn-within`) and
  **across-episode** (`--learn-across`, persisted). `storm_memory.py`.
- **Persistence** — Lyapunov knobs (V / W / queue_hold) and the learned signature carry
  across episodes. `policy_store.py`.

### 2.4 Coordination & guardrails
- **Orchestrator** — starts episodes, launches the agent loops, routes operator intents,
  and returns the episode report. (Operator-intent → policy translation is the main open
  functionality gap.) `agents/orchestrator.py`.
- **Guardrails** — server clamp `[1, c_max]`; queue-hold threshold (don't shed capacity
  while the queue is still draining); LLM request / tool-call limits to bound cost.

---

## 3. Two evaluation axes

Resilience and security are **related but distinct** objectives and are reported together:

| axis | metric | won by |
|---|---|---|
| **Resilience** | A3RT P (utility maintained) | capacity management (Lyapunov + pre-provisioning) |
| **Security** | benign-served + botnet-blocked | the malicious-UE filter + learned auto-engagement |

Blocking more of the botnet does **not** raise P (it dips slightly) — the utility-based
score is dominated by capacity, so P alone is an incomplete picture of a security
mechanism's value.
