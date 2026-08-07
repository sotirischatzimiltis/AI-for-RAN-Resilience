# Experiment 1 — Non-RT Agent LLM Selection

Chooses the LLM that runs as the **Non-RT storm judge** for the later experiments, by running
six efficient-tier configurations inside the full agentic loop (Lyapunov fast loop + malicious-UE
filter + forecast/calendar anticipation tools) on one deliberately tricky scenario.

## Scenario (`llm_compare`, `sim.config.botnet_event_traffic`)
Two back-to-back load spikes of opposite kinds, of comparable arrival volume so a good score
demands *reasoning*, not pattern-matching:

1. **Malicious botnet**, ramped onset — `get_forecast` catches the unscheduled climb; the judge
   should recognise and filter it.
2. **Benign event surge**, step onset, sized to a real crowd — **England v Brazil, Wembley, not
   sold out; true attendance 83,664**. The calendar names only the event, venue and sold-out
   status (never the number), so the judge must *estimate the crowd itself* and pre-provision a
   reserve (ideal ≈ 13 servers) **without** filtering it.

Resilience is scored with the utilisation-based utility `uA = 1/(1+exp(kA·(ρ − mfracA)))`,
`ρ = λ/(c·μ)` (`kA = 10`, `mfracA = 0.90`), the same utility the controller optimises.

## Models (5 seeds each, serial provisioning)
`gpt-5.4-mini` (reasoning on / off), `qwen3.7-plus`, `gemini-3.1-flash-lite`, `gpt-4o-mini`,
`claude-haiku-4.5`. Temperature pinned to `0.0` for reproducibility, except the reasoning-on run
(provider default).

## Result — `model_comparison.json`
Winner: **`gpt-5.4-mini` (reasoning on)** — the only model that both infers the crowd accurately
(reserve 12.4 ≈ ideal 13) and holds genuine surge headroom (`uA` 0.76), giving the highest
event-window resilience at full benign service. Two models are carried forward:

- **`gpt-5.4-mini` (reasoning on)** — the resilience ceiling.
- **`gemini-3.1-flash-lite`** — the deployable choice: full benign service and near-ceiling
  resilience at roughly a seventh of the latency and cost, with no tool errors.

The JSON holds per-model means, 95% confidence intervals and the raw per-seed arrays for every
metric, plus the per-storm-window resilience decomposition.

## Reproduce
```bash
python -m scripts.exp_1_model_comparison --serial --seeds 5 --save --resume --log
```
The scheduled-reserve prompt (`prompts/non_rt_agent_system_prompt.md`, the former `_v3`) is now the
default, so no `--prompt` flag is needed. Each run writes a timestamped
`model_comparison_<timestamp>.json`; the blessed result above was hand-copied to
`model_comparison.json`.

Per-episode reasoning/utility traces (`reasoning/`), run logs (`logs/`), and timestamped run
outputs are regenerable and git-ignored; only this README and `model_comparison.json` are tracked.
