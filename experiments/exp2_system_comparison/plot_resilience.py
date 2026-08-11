"""Exp 2 figure, SINGLE-COLUMN IEEE — two resilience bars per system (P_bot, P_surge), showing
the botnet-window to event-window crossover. Episode P and efficiency go in a companion table.
Reads the newest system_comparison_*.json. Kept LOCAL (git-ignored). pydantic-ai-env python."""
import json
import os
from glob import glob
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
_BLESSED = os.path.join(OUTPUT_DIR, "system_comparison.json")            # the manuscript's data
cands = sorted(glob(os.path.join(OUTPUT_DIR, "system_comparison_[0-9]*.json")))
SRC = _BLESSED if os.path.exists(_BLESSED) else (cands[-1] if cands else _BLESSED)
d = json.load(open(SRC))
scn = d["scenarios"][0]

# (x-axis label, JSON key) — full controller names, rotated below so they fit the single column
SYSTEMS = [
    ("Static $c{=}1$",       "Static (c=1)"),
    ("Static $c{=}8$",       "Static (c=8)"),
    ("Static $c{=}16$",      "Static (c=16)"),
    ("Lyapunov",             "Lyapunov"),
    ("Rule based",           "Deterministic (rules)"),
    ("Agentic Gemini",       "Agentic (gemini)"),
    ("Agentic GPT-5.4-mini", "Agentic (gpt-5.4-mini)"),
]
WINDOWS = [
    ("$P_{\\mathrm{bot}}$",   "P_bot",   "C0", "///"),
    ("$P_{\\mathrm{surge}}$", "P_surge", "C1", "xxx"),
]

def mean(key, metric):
    return d["systems"][key][scn][f"{metric}_mean"]

plt.rc("font",   family="serif", size=7)
plt.rc("axes",   titlesize=7,    labelsize=7.5)
plt.rc("legend", fontsize=6.5)
plt.rc("xtick",  labelsize=6.5)
plt.rc("ytick",  labelsize=6.5)

bar_width = 0.38
x = np.arange(len(SYSTEMS))
fig, ax = plt.subplots(figsize=(3.5, 2.5), constrained_layout=True)

for j, (_, mkey, colr, hatch) in enumerate(WINDOWS):
    for i, (_, skey) in enumerate(SYSTEMS):
        xpos = x[i] + (j - 0.5) * bar_width
        val = mean(skey, mkey)
        ax.bar(xpos, val, width=bar_width, hatch=hatch, color=colr,
               edgecolor="black", linewidth=0.3, zorder=3)
        ax.text(xpos, val + 0.012, f"{val:.2f}", ha="center", va="bottom",
                fontsize=5.4, rotation=45, zorder=4, clip_on=False)

ax.set_xticks(x)
ax.set_xticklabels([lbl for lbl, _ in SYSTEMS], rotation=30, ha="right",
                   rotation_mode="anchor")
ax.set_ylabel("Resilience")
ax.set_ylim(0.4, 1.14)   # headroom so the ~0.99 value labels clear the top border
ax.set_yticks(np.arange(0.4, 1.01, 0.2))
ax.grid(axis="y", linestyle="--", linewidth=0.4, zorder=0)
ax.set_axisbelow(True)

handles = [mpatches.Patch(hatch=w[3], facecolor=w[2], edgecolor="black", label=w[0])
           for w in WINDOWS]
fig.legend(handles=handles, loc="lower center", ncol=2, frameon=False,
           handlelength=1.5, columnspacing=1.4, bbox_to_anchor=(0.5, -0.06))

for name in ["exp2_system_comparison.pdf", "exp2_system_comparison.png"]:
    fig.savefig(os.path.join(OUTPUT_DIR, name), bbox_inches="tight", dpi=300)
print("Saved to:", OUTPUT_DIR)
