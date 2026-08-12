# Experiment 5 — Compute Contention (Section VI-E)

Shows that shared vCU/vDU compute contention is a resilience factor no controller can escape,
because it silently lowers the effective service rate `mu_eff` of a pool the controller does not
observe. Two parts.

## Part A — resilience envelope (deterministic, no LLM)
`exp5_envelope.py` sweeps a benign step storm over intensity against a fixed, instantly provisioned
pool (`c = c_max = 16`), with contention off (dedicated) and on at severities `a in {0.5, 1.0}`.
Contention shifts a hard stability cliff left, from `c_max*mu ~ 459` UEs/s to `c_max*mu_eff` (~321 at
a=0.5, ~247 at a=1.0). Cached to `exp5_envelope.json`; figure `exp5_envelope.pdf` (Fig. `fig:exp5_envelope`).

```
python exp5_envelope.py            # cached -> figure
python exp5_envelope.py --refresh  # recompute the sweep (~2 min, 315 sims, no LLM)
```

## Part B — controllers under contention (with LLM)
`scripts/exp_5_compute_contention.py` runs the Exp-2 arm set on one benign scheduled-event surge
(`event_heavy`, the Exp-1 crowd ~279 UEs/s the judge pre-provisions for), off vs a=1.0. A botnet
would be filtered and never fill the pool, so a benign event is what exposes contention.

```
source ~/.zshrc
python -m scripts.exp_5_compute_contention --seeds 5 --severity 1.0 --save --log
python experiments/exp5_compute_contention/exp5_partb.py   # figure from the blessed json
```

`compute_contention.json` is the blessed 5-seed off+a=1.0 result. The paper uses **GPT-5.4-Mini only**
(gemini under-estimates the crowd and commits its reserve late — a timing failure, not contention).

### Result (blessed)
| arm | off | a=1.0 | dP |
|-----|-----|-------|-----|
| Static c=16 | P=0.99, benign=1.00 | P=0.73, benign=0.31 | -0.26 |
| Agentic (GPT-5.4-Mini) | P=0.98, benign=1.00 | P=0.76, benign=0.54 | -0.22 |

GPT estimates the crowd accurately (~84k vs true 83,664) and commits the same ideal reserve (~13
servers) at **both** levels, yet resilience collapses at a=1.0 — isolating contention as the sole
cause. The maxed static pool (100% occupancy) is hit hardest; the demand-matched agentic reserve
(~81% occupancy) is slightly less exposed but still cannot escape.
