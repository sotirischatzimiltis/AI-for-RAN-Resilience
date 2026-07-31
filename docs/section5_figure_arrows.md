# Section V Figure — Arrow / Edge List

Complete edge list for the agentic resilience framework figure (Fig. 5), grounded in the
implemented code. Each edge: **source → target · label · style**.

Components: Operator · Orchestrator (SMO/Non-RT RIC coordinator) · Judge (Non-RT rApp
instance) · MCP tools (get_episode_stats / get_forecast / get_calendar) · Calendar ·
Shared Policy (blackboard) · Fast loop (Near-RT xApp instance) · Memory & Persistence
(StormMemory + policy_store) · Attach-server pool (SimPy digital twin / RAN) · UEs.

---

## Down — control / actuation (solid grey)
1. **Operator → Orchestrator** · "operator intent (free text)"
2. **Orchestrator → Shared Policy** · "set posture (V,W) + SLA floor (min_servers)" · over A1/O1
3. **Judge → Shared Policy** · "PolicyUpdate: storm_active, p_drop (+ knobs when tighten)"
4. **Shared Policy → Fast loop** · "atomic snapshot, each tick"
5. **Fast loop → Attach-server pool** · "set c*(t)" · E2 · (Adaptation)
6. **Fast loop → Attach-server pool** · "set p_drop" · E2 · (Absorption — enforced at admission on λ_mal)

## Coordination — orchestrator ↔ judge (dashed)
7. **Orchestrator → Judge** · "A2A: standing instruction (via shared policy)" · dashed
8. **Orchestrator → Calendar** · "schedule event (write)"

> Note on #7: mechanically this is *Orchestrator writes `operator_note` → Shared Policy →
> Judge reads it.* Draw EITHER the dashed Orchestrator→Judge "A2A (via shared policy)" OR
> the two hops through Shared Policy — not both, or you double-count it.

## Up — observation / telemetry (blue)
9. **UEs → Attach-server pool** · "attach requests λ_ben + λ_mal" · radio access
10. **Attach-server pool → Fast loop** · "telemetry: latest sample (λ, L_q, busy, c)"
11. **Attach-server pool → Judge** · "telemetry window (trends)"
12. **Attach-server pool → MCP tools** · "reads telemetry/stats" (feeds get_episode_stats, get_forecast)

> #10–12 can share one blue rail off the pool that branches to fast loop, judge, and tools.

## Tools & policy reads (read arrows)
13. **Judge ↔ MCP tools** · "tool calls / responses: stats, forecast, calendar" · bidirectional
14. **Calendar → MCP get_calendar → Judge** · (part of #13's responses; the calendar the orchestrator wrote in #8 is read here)
15. **Shared Policy → Judge** · "reads operator note + context each assessment"

## Memory / Evolution
16. **Fast loop → Memory&Persistence** · "observe(λ, storm) — learn signature" · within-episode
17. **Memory&Persistence → Fast loop** · "auto-engage: should_engage(λ)" · within-episode · (Evolution feeding early Absorption)
18. **Shared Policy ↔ Memory&Persistence** · "tuned knobs saved / reloaded" · dashed · cross-episode
19. **Memory&Persistence self-loop** · "persist + reload across episodes" · dashed · (Evolution)

---

## Quick sanity summary of directions
- **Judge**: writes Shared Policy (#3), reads Shared Policy (#15), calls tools (#13), reads telemetry window (#11).
- **Fast loop**: reads Shared Policy (#4), actuates pool (#5, #6), reads telemetry (#10), read/writes memory (#16, #17).
- **Orchestrator**: reads intent (#1), writes Shared Policy (#2) + calendar (#8) + note-to-judge (#7).
- **Shared Policy**: written by orchestrator (#2) and judge (#3); read by fast loop (#4) and judge (#15); persisted (#18).
- **Pool**: actuated by fast loop (#5, #6); emits telemetry to fast loop/judge/tools (#10–12); receives λ from UEs (#9).

## Honesty flags when drawing
- The **filter (p_drop) enforcement point is at the pool's admission**, not in the fast-loop box —
  arrow #6 sets the rate; the drop happens inside the pool on incoming λ_mal.
- **Persistence writes (#18)** happen at **episode boundaries** by the harness, not live by the
  coordinator agent — keep them dashed / cross-episode, distinct from the live control arrows.
- **A2A is not a wire protocol** — it's two pydantic-ai agents exchanging a message via the shared
  policy object; frame as "implicit A2A via pydantic-ai", never a standardized transport.

## Resilience lifecycle → where each stage lives
- **Anticipation** — judge get_forecast + get_calendar → raise V to pre-provision ahead of τ_prov
- **Absorption** — storm detection → malicious-drop filter p_drop at admission (#6)
- **Adaptation** — fast-loop Lyapunov capacity c*(t) under κ contention (#5)
- **Recovery** — judge clears storm_active → filter disengages + queue drains to baseline
- **Evolution** — StormMemory auto-engage (#16, #17) + policy_store across episodes (#18, #19)
