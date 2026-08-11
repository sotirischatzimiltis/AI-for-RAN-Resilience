# Experiment 4 — Reactive Tuning Under Provisioning Delay

A **pure capacity study**. No LLM, no admission filter, no anticipation. It asks a single question:
**can tuning the fast loop's weights compensate for a slow server provisioning delay?** The answer
depends entirely on the **shape of the load**, which is the whole point.

## Setup
The only moving parts are the fast loop's utility weight `V`, its server-cost weight `W`, and the
server **provisioning delay** (servers come online serially, one per delay). We drive the controller
with two benign single-surge scenarios that share the same peak and the same elevated duration and
differ **only in onset shape**:

| Scenario | Onset | Builder |
|---|---|---|
| `single_storm` | **step** — load jumps 20 → 200 UEs/s instantly | `single_storm_traffic` |
| `single_ramp` | **ramp** — load climbs 20 → 200 over 30 s (a staircase) | `single_ramp_traffic` |

There is no botnet, so the admission filter is irrelevant and stays off.

## Sweep
`V, W ∈ {1,2,5,10,20} × {1,5,20}` and provisioning delay `∈ {0,2,5,10}` s, over 5 seeds. Each cell
reports resilience `P`, benign users served, and mean online capacity (`avg_servers`) as mean ± 95%
CI. The output is the resilience–cost Pareto plus the delay-sensitivity of the weights.

## Reproduce
```bash
python -m scripts.exp_4_V_W_tuning \
  --scenario single_storm single_ramp \
  --v-grid 1 2 5 10 20 --w-grid 1 5 20 \
  --provision-delay 0 2 5 10 \
  --seeds 5 --rt-factor 50 --save --log
python experiments/exp4_vw_tuning/plot_vw_tuning.py      # 3 diagnostic figures
python experiments/exp4_vw_tuning/make_exp4_figure.py    # the compact single-column paper figure
```
No API cost. `logs/` is git-ignored; the JSON, plot scripts, and figures are tracked.

## Results (5 seeds, W=1 slice, benign users served)

| | delay 0 s | delay 2 s | delay 5 s | delay 10 s |
|---|---|---|---|---|
| **Step**, V=1 | 0.66 | 0.15 | 0.14 | 0.13 |
| **Step**, V=20 | 1.00 | 0.15 | 0.16 | 0.13 |
| **Ramp**, V=1 | 0.79 | 0.22 | 0.19 | 0.16 |
| **Ramp**, V=20 | 1.00 | 1.00 | 0.88 | 0.21 |

(Resilience `P` at a 5 s delay follows the same pattern: step ≈ 0.66 for every weight; ramp rises
from 0.69 at V=1 to 0.87 at V=20.)

### Findings
- **The provisioning delay, not the weighting, governs the outcome.** At zero delay every
  configuration serves the surge; service falls steeply as the delay grows, and by 10 s every
  configuration collapses to near the same low value regardless of `V` and `W`.
- **The onset shape decides whether tuning helps at all.** Under the **step**, serial provisioning
  cannot assemble capacity before an instantaneous surge ends, so from a 2 s delay onward the
  controller serves only ~15% of benign users and raising `V` from 1 to 20 changes nothing (tuning
  gain +0.01 in `P`).
- **A ramp is trackable.** Under the **ramp** a high utility weight keeps provisioning ahead of the
  gradual rise and serves 88% of the surge at a 5 s delay against 19% at V=1 (tuning gain +0.18).
- **The takeaway:** an instantaneous surge cannot be caught after the fact by any weight setting, so
  **anticipation (provisioning before the load) is the only defense**, and keeping the provisioning
  delay small is what prevents a surge from becoming catastrophic. This substantiates the Experiment 2
  claim that no Lyapunov weight setting recovers what anticipation provides — the scheduled event
  surge there is a step.

## Figures
- `exp4_vw_sweep.{pdf,png}` — **the paper figure**: benign users served vs delay, step and ramp at
  V=1 and V=20 (W=1), 95% CI.
- `vw_delay_lines.png` — diagnostic: `P` and benign completion vs delay, a line per `V`.
- `vw_heatmaps.png` — diagnostic: `P` over the `V×W` grid, one panel per delay.
- `vw_pareto.png` — diagnostic: `P` vs `avg_servers`, one series per delay.
