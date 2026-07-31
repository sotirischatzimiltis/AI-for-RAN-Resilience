# Reviewer Backlog — Take / Decide / Skip Triage

Triage of `storm_sim_fix_backlog.md` (the external review + `sim_edited/` rewrite) against the
ACTUAL system. Verified where noted.

**Legend**
- **TAKE** — low risk, behavior-preserving (or additive). No re-run of results needed.
- **DECIDE** — recalibrates the metric or redesigns the scenario/controller. Forces a full
  re-run (Exp 1 + Phase A + ...). A modeling choice you must own, not inherit.
- **SKIP** — wrong premise, not applicable, or already handled.

**Load-bearing fact:** experiments score with `runtime.UP = UtilityParams(lq_max=1500, kB=0.004)`,
NOT the dataclass defaults (7000, 0.01) the review reasoned from. Verified under UP: `uB` is live
(0.95->0.05) and P discriminates (Static c=1/8/16 -> 0.53/0.57/1.00). So the metric is NOT degenerate.

---

## metrics.py
| ID | Item | Call | Reason |
|---|---|---|---|
| MET-1 | lq_max 7000->1000 | **SKIP** | Premise wrong; UP=1500 already reachable. If anything, align the *default* 7000->1500 (hygiene, changes nothing since experiments pass UP). Recalibrating to 1000 forces a re-run for no gain. |
| MET-2 | kA 0.5->0.05 | **DECIDE** | kA IS operative; recalibrates uA -> re-run. Not clearly needed (uB carries the gradient). Only adopt if a P-vs-c sweep shows uA too binary. |
| MET-3 | _ratio nan for <2 samples (was free 1.0) | **TAKE** | Real fix. Well-formed episodes have many samples so current P unaffected; verify. |
| MET-4 | clamp tr/seg2 to next storm (t_limit) | **TAKE** | Correct multi-storm fix. Changes multi_storm numbers (correctly); no effect on single_storm. |
| MET-5 | u_des from fixed c_max reference, not local lookback | **DECIDE** | Changes the scoring baseline philosophy -> re-run. Real design question. |
| MET-6 | resilience_efficiency "inverted" | **SKIP** | Not inverted — it's the documented "read only next to P" property. Their Pareto-plot idea is a nice optional *reporting* add. |
| MET-7 | mutable default ResilienceWeights() | **TAKE** | Python gotcha; harmless today (never mutated), cheap to fix. |
| MET-8 | hold_window hardcoded -> parameter | **TAKE** | We already wanted this promoted. Keep default 30. |
| MET-9 | benign_success_rate nan on zero denom | **TAKE** | Sentinel change; verify downstream tolerates nan. |
| MET-10 | attach_latency_stats raise on length mismatch | **TAKE** | Stricter than fail-open; minor. Optional. |
| MET-11 | scale-free sigmoid (rho, qn) | **DECIDE** | Whole new utility form; declared change vs prior paper -> big re-run. Likely defer. |
| MET-12 | nan sentinels for consistency | **TAKE** | Hygiene. |
| MET-13 | counter_value_at O(n*storms) -> bisect | **TAKE** | Perf, behavior-preserving. Optional (only matters for long runs). |

## simulator.py
| ID | Item | Call | Reason |
|---|---|---|---|
| SIM-1 | bot retry REPLACES backoff (not adds) | **DECIDE** | The parked botnet-aggression decision. Changes storm dynamics -> re-run. |
| SIM-2 | benign false-positive drop | **DECIDE** | Filter-realism modeling choice -> re-run. |
| SIM-3 | one-shot bots / botnet_rate as aggregate | **DECIDE** | Modeling. Option A == current behavior (0 code) — adopt as a documented stance. |
| SIM-4 | Lewis-Shedler thinning arrivals | **DECIDE** | Fixes ~50ms onset staleness (negligible) but changes arrival sequence -> re-run. Low value. |
| SIM-5 | 4 RNG streams (common random numbers) | **DECIDE** | Genuinely useful for paired seed stats, but changes every episode -> re-run. |
| SIM-6 | benign_arrivals, in_flight_at_end counters | **TAKE** | Additive accounting, behavior-preserving. |
| SIM-7 | PS rho_c excludes tagged job | **DECIDE** | Only affects compute_kappa runs (off in A-D); changes contention -> re-run Exp D only. |
| SIM-8 | measured effective-mu estimator | **TAKE** (estimator) / **DECIDE** (using it = CTL-3) | Adding it is additive; feeding it to the controller changes behavior. |
| SIM-9 | frozen-at-dispatch service DOCUMENTED | **TAKE** | Doc only. |
| SIM-10 | warm-up discard | **DECIDE** | Changes which samples are scored -> recalibrates. |
| SIM-11 | end-of-run invariant asserts | **TAKE** | Safety, behavior-preserving. |
| SIM-12 | c_target required (no 0 default) | **TAKE** | Hygiene; telemetry always sets it (probe uses **dict). |
| SIM-13 | deque + O(1) live counter | **TAKE (verify)** | Perf; non-trivial queue rewrite — verify correctness (pop/remove semantics). |
| SIM-14 | run() guard vs duplicate control loop | **TAKE** | Safety. |
| SIM-15 | control loop builds own sample | **TAKE** | Robustness; low risk (we run controller=None anyway). |
| SIM-16 | provisioning docstring -> c_target | **TAKE** | Doc only (we already renamed). |
| SIM-17 | arrivals reconciliation doc/assert | **TAKE** | Doc/assert. |
| SIM-18 | completion lists unbounded | **SKIP** | Only matters for very long runs; defer. |

## config.py
| ID | Item | Call | Reason |
|---|---|---|---|
| CFG-1 | provision delay 0->15s + server cost | **DECIDE** | Redefines the control problem (a reason not to max servers). Real improvement, big re-run. THE key design decision. |
| CFG-2 | degeneracy (resolved via CFG-1) | **DECIDE** | Tied to CFG-1. Note: retries DO ignite when under-provisioned (verified maxQ~1300 at c=1/8). |
| CFG-3 | remove SimConfig.lq_max (dead) | **TAKE** | Verified unused by sim; only runtime passes it. Delete field + drop from runtime.start(). |
| CFG-4 | backoff 0->500ms + randomize | **DECIDE** | Changes retry dynamics -> re-run. Tied to SIM-1. |
| CFG-5 | TrafficConfig.max_rate() | **TAKE** | Additive helper (harmless even without SIM-4). |
| CFG-6 | phase contiguity validation | **TAKE** | Validation; verify existing scenarios pass (multi_storm phases are contiguous). |
| CFG-7 | c0 1->2 | **DECIDE** | Changes initial capacity + "calm" baseline -> re-run. |
| CFG-8 | compute_kappa Optional[float] | **TAKE** | Type annotation only. |
| CFG-9 | gap 90->120 | **DECIDE** | Changes scenario timeline -> re-run. |
| CFG-10 | explicit baseline_rate for storm_windows | **TAKE** | Verify windows unchanged with default = current min (20). |
| CFG-11 | open_ran_arch duplicate-kwarg TypeError | **TAKE** | Bug fix, low risk. |
| CFG-12 | split sample_dt_s -> telemetry_dt_s/control_dt_s | **DECIDE** | Structural; low value for us (fast-loop cadence already separate). |
| CFG-13 | merge multi_storm recover-1/calm-2 | **TAKE** | Cosmetic; storm_windows unaffected (both calm). |

## controllers.py
| ID | Item | Call | Reason |
|---|---|---|---|
| CTL-1 | explicit dt in drift (was hardcoded 1) | **DECIDE** | Rescales objective -> different c* -> re-run. Interacts with CTL-2. |
| CTL-2 | normalize objective so V/W bind | **DECIDE + INVESTIGATE** | If V is currently inert, the anticipation lever (judge raises V) is weak. HIGH priority to verify with a V-sweep — bears on the anticipation story. |
| CTL-3 | lambda from measured attempts, run BOTH | **DECIDE** | Changes controller; the measured-vs-schedule gap is a strong potential NEW experiment (quantifies retry amplification). |
| CTL-4 | dwell hysteresis + down margin | **DECIDE** | Only matters once CFG-1 provision delay lands. Tied to CFG-1. |
| CTL-5 | dataclasses.replace vs __dict__ splat | **TAKE** | Cleaner, behavior-preserving. |
| CTL-6 | reconcile util_p with sim config | **TAKE** | Fixes the LyapunovController()-builds-bare-UtilityParams footgun. Assert or pass UP. |
| CTL-7 | fixed_arm_config (no provisioning tax) | **DECIDE** | Only matters once CFG-1 lands (delay currently 0). |
| CTL-8 | Optional annotation | **TAKE** | Type only. |
| CTL-9 | FixedController dataclass + _last_c guard | **TAKE** | Perf/cleanliness, behavior-preserving. |
| CTL-10 | document tie-break | **TAKE** | Doc only. |
| CTL-11 | implement look-ahead lambda | **SKIP** | Ties to ForecastLyapunov, which we DELETED (oracle). Skip unless resurrecting it. |

---

## Top-3 to actually investigate (before any re-run)
1. **CTL-2 (V/W inert?)** — verify with a V-sweep whether raising V changes `c*`. If V is inert,
   your anticipation mechanism needs the normalized objective. This is the one that could genuinely
   affect the framework story.
2. **CFG-1 (provision delay + server cost)** — the biggest design decision. Without it, "max servers"
   is trivially optimal (mitigated today by the avg_servers / efficiency / filter axes). Decide whether
   the headline experiment needs a capacity cost.
3. **CTL-3 (measured vs schedule lambda)** — potential *new headline*: the gap quantifies retry
   amplification, the contribution over the M/M/c prior paper. Worth more than a fix.

## Cheap wins to bank now (TAKE, no re-run)
metrics: MET-3, MET-4, MET-7, MET-8, MET-12, MET-13 · sim: SIM-6, SIM-9, SIM-11, SIM-12, SIM-14,
SIM-15, SIM-16, SIM-17 · config: CFG-3, CFG-5, CFG-6, CFG-8, CFG-10, CFG-11, CFG-13 ·
controllers: CTL-5, CTL-6, CTL-8, CTL-9, CTL-10. Plus `_clamp_exp` overflow guard.

## Do NOT
Swap `sim_edited/` in wholesale. It applies every item at once (recalibrated metric + redesigned
scenario + split RNG), changing signatures and invalidating Exp 1 + Phase A. Cherry-pick instead.
