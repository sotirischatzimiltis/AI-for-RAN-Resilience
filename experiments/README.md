# Experiments

Each experiment lives in its own directory with a `README.md` (question, exact
reproduce command, result table, findings) and its curated artifacts (JSON data,
figures, LaTeX tables). The runner scripts are shared in [`../scripts/`](../scripts);
the working outputs land in the gitignored `../results/` scratch dir and the finished
artifacts are promoted here.

Numbering follows the manuscript order (Section VI).

| # | Experiment | Status | Directory |
|---|---|---|---|
| 1 | LLM comparison (judge model selection) | ✅ done — ceiling `gpt-5.4-mini` (reasoning on), deployable `gemini-3.1-flash-lite` | [`exp1_model_comparison/`](exp1_model_comparison/) |
| 2 | System comparison: Static vs Lyapunov vs rule vs full Agentic | ✅ done | [`exp2_system_comparison/`](exp2_system_comparison/) |
| 3 | Event-portfolio reserve sizing (attendance estimation vs flat/formula rules) | ✅ done (10-seed) | [`exp3_reserve_sizing/`](exp3_reserve_sizing/) |
| 4 | V/W × provisioning-delay sweep on benign step vs ramp (resilience–cost) | 🟡 code redesigned (step/ramp); sweep pending | [`exp4_vw_tuning/`](exp4_vw_tuning/) |
| 5 | Compute-contention sensitivity (independent servers vs shared vCU/vDU pool) | 🟡 built; deterministic preview validated, full (LLM) sweep pending | `scripts/exp_5_compute_contention.py` |
| 6 | Operator intents + multi-site coordination | ⬜ planned | — |
| 7 | Memory / evolution (cross-episode learning) | ⬜ planned | `scripts/learning_curve.py` |

> **Mechanism ablation** (forecast/calendar/learning) was dropped from the paper on 2026-08-11 —
> Exp 2 already isolates anticipation, Exp 3 the calendar, and Exp 7 the memory. The script is kept
> as a diagnostic at `scripts/ablation.py` (de-numbered from `exp_5_ablation.py`).

**Judge models (Phases A–E, from Exp 1):** `openrouter:openai/gpt-5.4-mini` (reasoning on) as the
resilience ceiling and `openrouter:google/gemini-3.1-flash-lite` as the deployable operating point.
