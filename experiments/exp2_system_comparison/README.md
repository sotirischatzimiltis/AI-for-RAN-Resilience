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

_Result table + findings to be filled once the headline run is blessed._
