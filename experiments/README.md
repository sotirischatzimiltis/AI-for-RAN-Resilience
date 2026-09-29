# Experiments

Each experiment lives in its own directory with a `README.md` (question, exact
reproduce command, result table, findings) and its curated artifacts (JSON data,
figures, LaTeX tables). The runner scripts are shared in [`../scripts/`](../scripts);
the working outputs land in the gitignored `../results/` scratch dir and the finished
artifacts are promoted here.

Numbering follows the manuscript order.

| # | Experiment | Status | Directory |
|---|---|---|---|
| 1 | LLM comparison (judge model selection) | ✅ done | [`exp1_model_comparison/`](exp1_model_comparison/) |
| 2 | System comparison: Static vs Lyapunov vs rule vs Agentic | ✅ done | [`exp2_system_comparison/`](exp2_system_comparison/) |
| 3 | Event-portfolio reserve sizing (attendance estimation) | ✅ done | [`exp3_reserve_sizing/`](exp3_reserve_sizing/) |
| 4 | V/W × provisioning-delay sweep (benign step vs ramp) | ✅ done | [`exp4_vw_tuning/`](exp4_vw_tuning/) |
| 5 | Compute-contention sensitivity (shared vCU/vDU pool) | ✅ done | [`exp5_compute_contention/`](exp5_compute_contention/) |
| 6 | Operator-intent grounding + end-to-end posture demo | ✅ done | [`exp6_intents/`](exp6_intents/) |
| 7 | Cascading stressors (botnet + event + contention in one episode) | ✅ done | [`exp7_cascading/`](exp7_cascading/) |
| 8 | Multi-site fleet (SMO decomposes/routes operator intents) | ✅ done | [`exp8_multisite/`](exp8_multisite/) |
| — | Resilience timeline (time-to-detect + time-to-recover, GPT vs Gemini) | ✅ done | [`detect_recover/`](detect_recover/) |

> **Mechanism ablation** (forecast/calendar/learning) was dropped from the paper — Exp 2 isolates
> anticipation, Exp 3 the calendar, and cross-episode memory is covered by the evolution stage. The
> script is kept as a diagnostic at `scripts/ablation.py`.

**Judge models (from Exp 1):** `openrouter:openai/gpt-5.4-mini` (reasoning on) as the resilience
ceiling and `openrouter:google/gemini-3.1-flash-lite` as the deployable operating point.
