# Experiment 1 — LLM storm-judge bake-off

**Question.** Which LLM should drive the Non-RT storm judge? Compare candidate
efficient-tier models under identical conditions and pick one for the rest of the
campaign (Phases A–E). Exp 1 is a **model-selection** experiment only — the
deterministic control loop is held fixed, so its performance is not under study here.

**Setup.** Bare judge (telemetry-only detection, no anticipation tools / learning /
operator intents), real-time pacing (`rt_factor=1`), 5 seeds, two
scenarios: `single_storm` (benign surge only, no botnet) and `multi_storm_flat` (three
identical botnet storms — the discriminating scenario). Frozen prompt:
`prompts/exp1_model_comparison_non_rt_system_prompt.md`. Temperature pinned to 0.2 for
all runs except reasoning-on (providers force their default). Reasoning-capable models
run on+off where the OpenRouter reasoning toggle is honoured (only `gpt-5.4-mini`).

## Reproduce
```bash
source ~/.zshrc                      # OPENROUTER_API_KEY
python -m scripts.exp1_model_comparison_non_rt --probe                 # reachability first
python -m scripts.exp1_model_comparison_non_rt --seeds 5 --save --log  # full sweep
```

## Result (5 seeds, bare judge)

| Model | Rsn | P¹ | Tok/asmt (in/out)² | Cost³ | Lat (s) |
|---|---|---|---|---|---|
| **gemini-3.1-flash-lite** | — | 0.709 ± 0.007 | 3333 / 91 | **0.97** | **2.1** |
| gpt-5.4-mini | off | 0.706 ± 0.007 | 3294 / 132 | 3.07 | 3.1 |
| gpt-4o-mini | — | 0.706 ± 0.008 | 3277 / 74 | 0.54 | 2.6 |
| qwen3.7-plus | off | 0.705 ± 0.006 | 4104 / 136 | 1.49 | 3.3 |
| claude-haiku-4.5 | — | 0.704 ± 0.007 | 5142 / 188 | 6.08 | 3.7 |
| gpt-5.4-mini | on | 0.701 ± 0.006 | 3294 / 508 | 4.75 | 7.9 |

¹ Resilience $P$ on `multi_storm_flat`. ² Mean input/output tokens per assessment
(scenario-independent — the telemetry window is fixed; explains the cost column, e.g.
gpt-5.4-mini-on's 508 output tokens = the reasoning tax). ³ Milli-USD per assessment
(length-invariant; episodes differ in duration, per-assessment cost does not).
**± = 95% CI (Student-t, n = 5 seeds);** `errors_total = 0` for every model.

Two behavioural rates are **omitted** from the selection table because they are ≈constant
across models and so carry no discriminating signal (both live in `model_comparison.json`
and are discussed under Findings): **botnet-blocked** (≈100% for all, congestion-driven) and
**benign false-positive / over-filtering** (~15–18% for all, on `single_storm`).

## Findings
- **Resilience P is a statistical tie.** All six 95% CIs overlap (~0.70–0.71 on the botnet
  scenario). $P$ does not separate models, so the choice is made on cost and latency.
- **Botnet-blocked ≈100% is a congestion artifact, not a defense result.** A no-LLM
  decomposition on `multi_storm_flat` shows that with the filter **off** the botnet is still
  99.9% blocked (10,722 of 10,728 fail) — the botnet is modeled as impatient/aggressive, so
  under the ρ≈1 overload it self-defeats. The judge's filter changes the *mechanism*
  (drop-at-admission vs fail-after-retries) but barely the *rate*. It is 100% for every model,
  so it is excluded from the selection table; isolating the filter's own contribution is left
  to the security evaluation on a headroom scenario.
- **Benign completion is capacity-bound and out of scope.** On the storm scenarios only
  ~15–27% of legitimate users complete, identical across models — this is congestion
  starvation under the fixed reactive controller (which provisions to balance load, ρ≈1,
  no headroom), not a judge property. The capacity/headroom trade-off is studied
  separately by sweeping the Lyapunov weights V/W; it plays no part in model selection.
- **Every bare judge over-filters a benign surge** (~15–18% on `single_storm`, no botnet):
  arrivals-only detection cannot tell a benign surge from an attack. This is the
  false-positive floor that motivates the anticipation signals (calendar / post-outage
  reconnection) added in later phases.
- **Reasoning does not help.** `gpt-5.4-mini` on vs off: identical P and blocked, only a
  small drop in over-filtering, bought at ~2.5× latency (7.9 s vs 3.1 s) — enough that it
  falls behind the 5 s assessment cadence. Not worth it for this task.
- **Cost spans ~11×** per assessment (0.54 → 6.08 m\$). `gemini-3.1-flash-lite` gives the
  top P point-estimate, lowest latency, zero errors at 0.97 m\$/asmt; `gpt-4o-mini` is the
  budget alternative (0.54 m\$/asmt) at a hair more latency. The two are a statistical tie on P.
- **Provider constraint:** qwen and claude cannot run reasoning *and* our structured
  tool-output together (thinking is incompatible with forced `tool_choice`), so they run
  in default mode.

**Decision:** `openrouter:google/gemini-3.1-flash-lite` is the judge model for Phases A–E.

## Files
- `model_comparison.json` — full results: per model × scenario, each of P/benign/blocked/fp
  as mean + sample std + 95% CI + raw per-seed array; tokens; per-assessment + per-episode
  USD; latency.
- `logs/` — full run logs (gitignored).

The runner and prompt are **not duplicated here** — they live in
`scripts/exp1_model_comparison_non_rt.py` and
`prompts/exp1_model_comparison_non_rt_system_prompt.md` (the reproduce commands invoke them).
