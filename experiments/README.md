# Experiments

Each experiment lives in its own directory with a `README.md` (question, exact
reproduce command, result table, findings) and its curated artifacts (JSON data,
figures, LaTeX tables). The runner scripts are shared in [`../scripts/`](../scripts);
the working outputs land in the gitignored `../results/` scratch dir and the finished
artifacts are promoted here.

| # | Experiment | Status | Directory |
|---|---|---|---|
| 1 | LLM storm-judge bake-off (model selection) | ✅ done — winner: `gemini-3.1-flash-lite` | [`exp1_model_comparison/`](exp1_model_comparison/) |
| 2 | System comparison: Static vs Lyapunov vs full Agentic | 🟡 baselines validated — agentic run pending | `scripts/exp_2_system_comparison.py` |
| 3 | V/W × provisioning-delay sweep (resilience–cost) | ✅ done | [`exp3_vw_tuning/`](exp3_vw_tuning/) |
| B | Ablations (forecast / calendar / learning) | ⬜ planned | `scripts/ablation.py` |
| C | Learning curve (within / across episode) | ⬜ planned | `scripts/learning_demo.py` |
| D | Robustness (contention κ, provisioning delay, cadence) | ⬜ planned | — |
| E | Orchestrator / operator intents | ⬜ planned | — |

**Judge model (Phases A–E):** `openrouter:google/gemini-3.1-flash-lite` (from Exp 1).
