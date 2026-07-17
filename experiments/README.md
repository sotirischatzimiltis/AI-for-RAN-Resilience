# Experiments

Each experiment lives in its own directory with a `README.md` (question, exact
reproduce command, result table, findings) and its curated artifacts (JSON data,
figures, LaTeX tables). The runner scripts are shared in [`../scripts/`](../scripts);
the working outputs land in the gitignored `../results/` scratch dir and the finished
artifacts are promoted here.

| # | Experiment | Status | Directory |
|---|---|---|---|
| 1 | LLM storm-judge bake-off (model selection) | ✅ done — winner: `gemini-3.1-flash-lite` | [`exp1_model_comparison/`](exp1_model_comparison/) |
| A | Headline: Static vs Lyapunov vs Agentic | ⬜ planned | — |
| B | Ablations (forecast / calendar / release-valve / learning) | ⬜ planned | — |
| C | Learning curve (within / across episode) | ⬜ planned | — |
| D | Robustness (contention κ, provisioning delay, cadence) | ⬜ planned | — |
| E | Orchestrator / operator intents | ⬜ planned | — |

**Judge model (Phases A–E):** `openrouter:google/gemini-3.1-flash-lite` (from Exp 1).
