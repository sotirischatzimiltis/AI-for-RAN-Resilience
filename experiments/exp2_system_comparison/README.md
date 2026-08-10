# Experiment 2 — System Comparison

Does the full **agentic** framework deliver better storm resilience, at acceptable cost, than the
non-AI controllers it would replace — and what does the **LLM** add over hardcoding its own logic
as deterministic rules?

## The comparison ladder
Each rung adds one capability, so the deltas are attributable.

| System | Capacity | Filter | Forecast | Calendar | LLM |
|---|---|---|---|---|---|
| Static c=1 / 8 / 16 | fixed | — | — | — | — |
| Lyapunov | reactive | — | — | — | — |
| Deterministic (rules) | reactive | ✓ | ✓ | ✗ | — |
| Agentic (gemini) | reactive | ✓ | ✓ | ✓ | ✓ |
| Agentic (gpt-5.4-mini, reasoning on) | reactive | ✓ | ✓ | ✓ | ✓ |

- **Static** arms trace the cost–resilience frontier (c=1 cheap but starves; c=16 high P but wasteful,
  no security).
- **Lyapunov** is reactive capacity only — no filter, no anticipation.
- **Deterministic (rules)** is the agentic decision tree hardcoded (`RuleBasedController`): the same
  reactive Lyapunov capacity + malicious filter + **forecast** anticipation, but **calendar-free**. A
  calendar is human-in-the-loop knowledge the rule has not earned (the agentic arm could populate its
  own via a future web-scraping sub-agent), so the rule cannot recognize the benign event surge and
  filters it as if it were an attack. The **Rule → Agentic gap isolates the LLM's contribution.**
- **Agentic** is the full framework (scheduled-reserve judge + filter + forecast + calendar), run
  with both judges carried from Experiment 1: `gemini-3.1-flash-lite` (deployable) and `gpt-5.4-mini`
  reasoning-on (ceiling). Learning off (Exp C), operator intents off (Exp E).

## Scenario
The **same `botnet_event` scenario as Experiment 1**: one malicious botnet ramp (the forecast catches
it) followed by one benign real-event surge sized to **EXP1_EVENT**'s true attendance (only the
calendar anticipates it, so only the agentic arms can pre-provision the reserve). The deterministic
arms use the same `botnet_event_traffic(...)` builder as the agentic arm, so all arms see identical
arrivals at a given seed.

**Provisioning is SERIAL by default**, matching Experiment 1 — pre-provisioning *timing* only matters
under serial provisioning, so this is where the calendar's advantage is real. `--parallel` runs the
parallel ablation.

## Metrics (mean ± 95% CI over seeds)
The same resilience decomposition as Experiment 1, computed identically for every arm: episode `P`
with its botnet/event window split `P_bot` / `P_surge`, benign served and **benign false-positive
rate** (benign users wrongly filtered), botnet filtered / blocked, mean online capacity
(`avg_servers`), efficiency, the event-surge utility split (`rho_ev` / `uA_ev` / `uB_ev`), and — for
the agentic arms — LLM latency and the judge's crowd estimate / reserve.

## Reproduce
```bash
python -m scripts.exp_2_system_comparison --seeds 5 --save --resume --log
```
Each run writes a timestamped `system_comparison_<timestamp>.json` (checkpointed after every system,
so `--resume` continues a crashed sweep); the blessed result cited in the manuscript is hand-copied to
`system_comparison.json`. Timestamped run outputs and logs are git-ignored; only this README and
`system_comparison.json` are tracked.

## Results (5 seeds, serial provisioning, mean ± 95% CI)
Blessed run: `system_comparison_20260807_204609.json`.

| System | `P` | `P_bot` | `P_surge` | Benign served | Benign FP | Filtered | Avg. servers | Efficiency |
|---|---|---|---|---|---|---|---|---|
| Static (c=1) | 0.639 ± 0.003 | 0.764 | 0.513 | 0.232 | 0.000 | 0.000 | 1.00 | 10.22 ± 0.05 |
| Static (c=8) | 0.697 ± 0.005 | 0.854 | 0.540 | 0.396 | 0.000 | 0.000 | 8.00 | 1.39 ± 0.01 |
| Static (c=16) | 0.992 ± 0.000 | 0.993 | 0.990 | 1.000 | 0.000 | 0.000 | 16.00 | 0.99 ± 0.00 |
| Lyapunov | 0.684 ± 0.002 | 0.818 | 0.549 | 0.312 | 0.000 | 0.000 | 2.57 | 4.25 ± 0.01 |
| Deterministic (rules) | 0.710 ± 0.005 | 0.838 | 0.583 | 0.341 | 0.143 | 0.941 | 2.77 | 4.10 ± 0.04 |
| **Agentic (gemini-3.1-flash-lite)** | 0.864 ± 0.003 | 0.836 | 0.893 | 0.997 | 0.003 | 0.842 | 3.48 | 3.97 ± 0.02 |
| **Agentic (gpt-5.4-mini, reasoning on)** | 0.903 ± 0.009 | 0.829 | 0.977 | 0.998 | 0.002 | 0.506 | 4.19 | 3.45 ± 0.13 |

### Findings
- **The botnet window barely separates the arms** (`P_bot` 0.76–0.99): the attack saturates the cell
  under every policy, so raw resilience there is capacity-bound, not skill-bound.
- **The event window is where the ladder separates.** The surge is a step and provisioning is serial,
  so any controller that reacts only once load is visible cannot bring capacity online in time. The
  static `c∈{1,8}` and Lyapunov arms serve ≤40% of the surge (`P_surge` ≤ 0.55). Only static `c=16`
  absorbs it — at 16 held servers, no filter, and the worst efficiency (0.99).
- **The rule shows what the LLM adds.** With the filter + forecast but no calendar, the rule contains
  the botnet (filters 94%) but misreads the benign surge as an attack, dropping 14% of legitimate
  users (`benign_fp` 0.143) and reaching only `P_surge` 0.58.
- **Both agentic arms anticipate the event**, pre-provision the reserve, and withhold the filter for
  its duration: benign service ≈ 0.997–0.998 and `benign_fp` ≈ 0.002–0.003, giving `P_surge` 0.89
  (gemini) / 0.98 (gpt-5.4-mini). They approach the resilience of the fully-provisioned static `c=16`
  while holding only ~3.5–4.2 servers, i.e. ~3–4× its efficiency. The **rule → agentic gap isolates
  the value of anticipation** (same actuators, differ only in knowing the scheduled load in advance).
