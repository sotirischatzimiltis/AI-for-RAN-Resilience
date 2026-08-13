"""
Experiment 6, Fold 1, Part 1 — the operator-intent grounding figure.

Renders the blessed grounding result (grounding_blessed.json, produced by scripts/exp_6_intents.py)
as a grouped bar chart: two overall metrics (grounding accuracy, exact-match) and the five per-lever
accuracies, for the two LLM arms and the two non-LLM floors (null, keyword). The story it carries:
both LLMs ground nearly every lever, the gap over the lexical floor concentrates on POSTURE and
especially WEIGHT (the semantic and graded levers), and doing nothing (null) collapses on exactly the
levers that must be set. IEEE single-column styling (matches exp4/exp5/exp6_posture).
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np, matplotlib.pyplot as plt

_HERE = Path(__file__).parent
_BLESSED = _HERE / "grounding_blessed.json"

# (column label, key, kind)  kind 'o' = top-level summary metric, 'l' = per_lever entry
METRICS = [("Grounding", "grounding_acc", "o"), ("Exact", "exact_rate", "o"),
           ("Posture", "posture", "l"), ("Weight", "weight", "l"), ("Floor", "min_servers", "l"),
           ("Sched.", "schedule", "l"), ("Deleg.", "nonrt", "l")]

# (display label, substring to find the arm in the summary, colour). Wong colour-blind-safe palette.
ARMS = [("GPT-5.4-Mini", "gpt", "#0072b2"), ("Gemini-3.1-FL", "gemini", "#009e73"),
        ("Keyword", "keyword", "#e69f00")]


def _find(summary: dict, sub: str) -> dict:
    for k, v in summary.items():
        if sub in k.lower():
            return v
    raise KeyError(sub)


def _value(arm: dict, key: str, kind: str) -> float:
    return arm[key] if kind == "o" else arm["per_lever"][key]


def plot():
    summary = json.loads(_BLESSED.read_text())["summary"]
    arms = [(lbl, _find(summary, sub), col) for lbl, sub, col in ARMS]

    plt.rc("font", family="serif", size=8)
    plt.rc("axes", titlesize=8, labelsize=8.5, linewidth=0.8)
    plt.rc("legend", fontsize=6.5); plt.rc("xtick", labelsize=7.5); plt.rc("ytick", labelsize=8)
    plt.rc("mathtext", fontset="cm")
    fig, ax = plt.subplots(figsize=(3.4, 2.5), constrained_layout=True)

    x = np.arange(len(METRICS)); n = len(arms); w = 0.8 / n          # bars fill ~80% of each slot
    for i, (lbl, arm, col) in enumerate(arms):
        vals = [_value(arm, key, kind) for _, key, kind in METRICS]
        ax.bar(x + (i - (n - 1) / 2) * w, vals, w, label=lbl, color=col,
               edgecolor="black", lw=0.4, zorder=3)

    ax.axvline(1.5, color="0.6", lw=0.7, ls="--", zorder=1)          # split overall | per-lever
    ax.text(0.5, 1.02, "overall", ha="center", va="bottom", fontsize=6.5, color="0.35")
    ax.text(4.5, 1.02, "per lever", ha="center", va="bottom", fontsize=6.5, color="0.35")

    ax.set_ylabel("Accuracy")
    ax.set_ylim(0.2, 1.0)                                            # zoom on the discriminating range
    ax.set_xticks(x); ax.set_xticklabels([m[0] for m in METRICS], rotation=25, ha="right")
    ax.grid(axis="y", alpha=0.3, lw=0.4, zorder=0)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.06), ncol=4, frameon=False,
              columnspacing=1.0, handlelength=1.2, handletextpad=0.4)

    for ext in ("pdf", "png"):
        fig.savefig(_HERE / f"exp6_grounding.{ext}", dpi=300, bbox_inches="tight")
    print("saved -> exp6_grounding.pdf/.png")


if __name__ == "__main__":
    plot()
