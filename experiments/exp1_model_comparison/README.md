# Experiment 1 — LLM storm-judge bake-off

**Question.** Which LLM should drive the Non-RT storm judge? Compare candidate
efficient-tier models under identical conditions and pick one for the rest of the
campaign (Phases A–E).

**Setup.** Bare judge (telemetry-only detection, no anticipation tools / learning /
release valve / operator intents), real-time pacing (`rt_factor=1`), 5 seeds, two
scenarios: `single_storm` (utility only, no botnet) and `multi_storm_flat` (three
identical storms with a botnet — the discriminating scenario). Frozen prompt:
`prompts/prompts_mc_non_rt.md`. Reasoning-capable models run on+off where the
OpenRouter reasoning toggle is honoured (only `gpt-5.4-mini`).

## Reproduce
```bash
source ~/.zshrc                      # OPENROUTER_API_KEY
# Step 1 — the reasoning ablation (gpt-5.4 on/off)
python -m scripts.experiment_model_comparison --models gpt-5.4 --seeds 5 --save --log
# Step 2 — the other four, merged into the same JSON
python -m scripts.experiment_model_comparison --models gemini qwen gpt-4o-mini claude --seeds 5 --save --log
# table + plot
python -m scripts.table_model_comparison
python -m scripts.plot_model_comparison
```

## Result (multi_storm_flat, 5 seeds, ranked by botnet-blocked)

| Model | Rsn | Blocked ↑ | Benign | P | $/ep | Lat (s) | Err |
|---|---|---|---|---|---|---|---|
| **gemini-3.1-flash-lite** | n/a | **0.799 ± 0.037** | 1.000 | 0.835 | 0.087 | 2.0 | 0 |
| gpt-5.4-mini | off | 0.778 ± 0.048 | 1.000 | 0.835 | 0.222 | 3.1 | 0 |
| gpt-4o-mini | n/a | 0.745 ± 0.056 | 1.000 | 0.836 | 0.044 | 2.4 | 2 |
| claude-haiku-4.5 | n/a | 0.688 ± 0.043 | 1.000 | 0.836 | 0.416 | 3.5 | 0 |
| qwen3.7-plus | off | 0.687 ± 0.064 | 1.000 | 0.836 | 0.098 | 4.0 | 0 |
| gpt-5.4-mini | on | 0.658 ± 0.025 | 1.000 | 0.836 | 0.230 | 7.5 | 0 |

## Findings
- **Resilience P is capacity-bound** (~0.835 for every model, both scenarios) — it does
  not separate models. Benign-served is 1.000 for all (the filter is botnet-targeted).
  The comparison is therefore on **botnet-blocked rate**.
- **Winner: `gemini-3.1-flash-lite`** — top blocked-rate point estimate, cheap, fastest,
  zero errors. The top three (gemini / gpt-5.4-off / gpt-4o-mini) are a statistical tie on
  blocked; gemini wins on the cost/latency/robustness tiebreakers.
- **Reasoning hurts.** gpt-5.4 `off` (0.778) beats `on` (0.658) with **non-overlapping**
  per-seed ranges — reasoning lowers blocked and is ~2.4× slower. This control task is
  light enough that reasoning effort provides no benefit.
- **Provider constraint noted:** qwen and claude cannot run reasoning *and* our structured
  tool-output together (thinking is incompatible with forced `tool_choice`), so they run
  in default mode.

**Decision:** `openrouter:google/gemini-3.1-flash-lite` is the judge model for Phases A–E.

## Files
- `model_comparison.json` — full results (means + std for P/benign/blocked, tokens, $, latency)
- `model_comparison.png` — 4-panel figure (P / security / cost / quality-vs-cost)
- `model_comparison_table.tex` — LaTeX (booktabs) table for the paper
