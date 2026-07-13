"""
Assemble the paper's results — a comparison table and matplotlib figures.

Runs the DETERMINISTIC baselines live (cheap, no LLM) and reads the cached live-
agent and learning-demo results (produced by `agent_sweep --save` and
`learning_demo --save`). Prints a Markdown table and writes figures to
results/figures/:

  fig1_resilience_P.png      P by controller (agent vs baselines), both scenarios
  fig2_learning_curve.png    botnet blocked vs learning stage (the learning curve)
  fig3_per_storm_blocked.png per-storm blocked, baseline vs learned (within-episode)

Usage:
    python -m scripts.agent_sweep   --scenario single_storm --save   # once, ~min (API)
    python -m scripts.agent_sweep   --scenario multi_storm  --save
    python -m scripts.learning_demo --save
    python -m scripts.make_results  [--seeds 5]
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from scripts.seed_sweep import run_one, CONTROLLERS, SCENARIOS

RESULTS = Path(__file__).parent.parent / "results"
FIGS    = RESULTS / "figures"

# subject-specific palette (deliberate, not default matplotlib cycle)
C_BASE   = "#9aa7b4"   # muted slate for baselines
C_LYAP   = "#4c78a8"   # blue for the classical control baseline
C_AGENT  = "#e4572e"   # warm accent for the agentic system
C_LEARN  = "#2a9d8f"   # teal for the learning story


def deterministic_baselines(seeds):
    """{scenario: {controller: {P, benign}}} across seeds (blocked=0, no filter)."""
    out = {}
    for scenario in SCENARIOS:
        out[scenario] = {}
        for label, (factory, c0) in CONTROLLERS.items():
            ps, bs = [], []
            for s in seeds:
                p, _f, benign = run_one(factory, c0, scenario, s)
                ps.append(p); bs.append(benign)
            out[scenario][label] = {"P": statistics.mean(ps), "benign": statistics.mean(bs)}
    return out


def load(name):
    p = RESULTS / name
    return json.loads(p.read_text()) if p.exists() else None


def print_table(base, agents):
    print("\n## Resilience & security — agent vs baselines\n")
    print("| scenario | system | P | benign served | botnet blocked |")
    print("|---|---|---|---|---|")
    for scenario in SCENARIOS:
        for label, m in base[scenario].items():
            print(f"| {scenario} | {label} | {m['P']:.3f} | {m['benign']:.3f} | — |")
        a = agents.get(scenario)
        if a:
            print(f"| {scenario} | **Agentic (LLM)** | **{a['agent_P_mean']:.3f}** "
                  f"| **{a['benign_mean']:.3f}** | **{a['blocked_mean']:.3f}** "
                  f"| (lift {a['lift_mean']:+.3f}) |".replace("| (lift", " (lift"))


def fig_resilience(base, agents):
    scenarios = list(SCENARIOS)
    labels = list(CONTROLLERS) + ["Agentic (LLM)"]
    fig, axes = plt.subplots(1, len(scenarios), figsize=(11, 4.2), sharey=True)
    for ax, scenario in zip(axes, scenarios):
        vals, colors = [], []
        for lab in CONTROLLERS:
            vals.append(base[scenario][lab]["P"])
            colors.append(C_LYAP if "Lyapunov" in lab else C_BASE)
        a = agents.get(scenario)
        vals.append(a["agent_P_mean"] if a else 0.0)
        colors.append(C_AGENT)
        ax.bar(range(len(labels)), vals, color=colors)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
        ax.set_title(scenario.replace("_", " "), fontsize=11)
        ax.set_ylim(0, 1.0)
        ax.axhline(0.834, ls="--", lw=0.8, color=C_LYAP, alpha=0.5)
        for i, v in enumerate(vals):
            ax.text(i, v + 0.01, f"{v:.2f}", ha="center", fontsize=7.5)
    axes[0].set_ylabel("A3RT resilience P")
    fig.suptitle("Resilience P: agentic system vs control baselines", fontsize=13)
    fig.tight_layout()
    out = FIGS / "fig1_resilience_P.png"
    fig.savefig(out, dpi=140); plt.close(fig)
    return out


def fig_learning(learn):
    if not learn:
        return None
    labels = [r["label"].replace("learn episode", "ep").replace("baseline (no learn)", "baseline")
              for r in learn["runs"]]
    blocked = [r["blocked"] for r in learn["runs"]]
    benign  = [r["benign"] for r in learn["runs"]]
    fig, ax = plt.subplots(figsize=(7, 4.2))
    x = range(len(labels))
    ax.plot(x, blocked, "-o", color=C_LEARN, lw=2.2, label="botnet blocked")
    ax.plot(x, benign, "-s", color=C_AGENT, lw=1.6, label="benign served")
    for i, v in enumerate(blocked):
        ax.text(i, v + 0.03, f"{v:.2f}", ha="center", fontsize=8, color=C_LEARN)
    ax.set_xticks(list(x)); ax.set_xticklabels(labels)
    ax.set_ylim(0, 1.05); ax.set_ylabel("rate")
    ax.set_title("Learning: botnet blocked rises with experience,\nbenign service stays intact", fontsize=12)
    ax.axvspan(-0.4, 0.4, color="grey", alpha=0.07)
    ax.legend(loc="center right"); ax.grid(axis="y", alpha=0.2)
    fig.tight_layout()
    out = FIGS / "fig2_learning_curve.png"
    fig.savefig(out, dpi=140); plt.close(fig)
    return out


def fig_per_storm(learn):
    if not learn:
        return None
    runs = learn["runs"]
    base = next((r for r in runs if "baseline" in r["label"]), None)
    warm = runs[-1]
    if not base or not base.get("per_storm_blocked"):
        return None
    n = len(base["per_storm_blocked"])
    x = range(n)
    fig, ax = plt.subplots(figsize=(7, 4.2))
    w = 0.38
    ax.bar([i - w/2 for i in x], base["per_storm_blocked"], w, color=C_BASE, label="baseline (no learn)")
    ax.bar([i + w/2 for i in x], warm["per_storm_blocked"], w, color=C_LEARN, label=warm["label"])
    ax.set_xticks(list(x)); ax.set_xticklabels([f"storm {i+1}" for i in x])
    ax.set_ylim(0, 1.05); ax.set_ylabel("fraction of botnet blocked")
    ax.set_title("Per-storm blocking: the fast loop learns after storm 1", fontsize=12)
    ax.legend(); ax.grid(axis="y", alpha=0.2)
    fig.tight_layout()
    out = FIGS / "fig3_per_storm_blocked.png"
    fig.savefig(out, dpi=140); plt.close(fig)
    return out


def main():
    ap = argparse.ArgumentParser(description="Assemble results table + figures")
    ap.add_argument("--seeds", type=int, default=5)
    args = ap.parse_args()
    FIGS.mkdir(parents=True, exist_ok=True)

    seeds = list(range(1, args.seeds + 1))
    print(f"Running deterministic baselines ({args.seeds} seeds, both scenarios)...")
    base = deterministic_baselines(seeds)
    agents = {sc: load(f"agent_{sc}.json") for sc in SCENARIOS}
    agents = {k: v for k, v in agents.items() if v}
    learn = load("learning_demo.json")

    print_table(base, agents)

    made = [fig_resilience(base, agents), fig_learning(learn), fig_per_storm(learn)]
    print("\nFigures:")
    for m in made:
        print(f"  {m}" if m else "  (skipped — missing cached data)")
    if not agents:
        print("\n[note] No agent_*.json cache — run `agent_sweep --save` for the agent bars.")
    if not learn:
        print("[note] No learning_demo.json — run `learning_demo --save` for the learning figures.")


if __name__ == "__main__":
    main()
