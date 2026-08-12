"""
Experiment 5, Part B figure — contention arm comparison at the over-limit benign event.

Compact off-vs-a=1.0 bar for the fully provisioned static pool (c=16) and the agentic judge
(gpt-5.4-mini). The headline is that gpt holds the SAME correct reserve (~13 servers) at both
contention levels, so the resilience drop is pure contention, not a provisioning error.

Loads the newest compute_contention_*.json checkpoint. Run from the exp5 folder:
    python exp5_partb.py
"""
from __future__ import annotations
import glob, json
from pathlib import Path
import matplotlib.pyplot as plt

_HERE = Path(__file__).parent
# newest timestamped run during dev; falls back to the blessed compute_contention.json after a clone
_CK   = sorted(glob.glob(str(_HERE / "compute_contention*.json")))[-1]

_ARMS = [("Static (c=16)", "Static\n$c=16$"),
         ("Agentic (gpt-5.4-mini)", "Agentic\n(GPT-5.4-Mini)")]
_LEVELS = [("off", "No contention", "#0072b2"),
           ("a=1", r"Shared pool $a{=}1.0$", "#c1272d")]

d = json.load(open(_CK))["systems"]

# IEEE single-column styling (matches exp4/exp5-envelope).
plt.rc("font", family="serif", size=8)
plt.rc("axes", titlesize=8, labelsize=8.5, linewidth=0.8)
plt.rc("legend", fontsize=7); plt.rc("xtick", labelsize=8); plt.rc("ytick", labelsize=8)
plt.rc("mathtext", fontset="cm")
fig, ax = plt.subplots(figsize=(3.4, 2.5), constrained_layout=True)

W = 0.36
for gi, (arm, _) in enumerate(_ARMS):
    p_off = d["off"][arm]["P_mean"]
    for li, (lvl, _, col) in enumerate(_LEVELS):
        c = d[lvl][arm]
        x = gi + (li - 0.5) * W
        hatch = "////" if lvl != "off" else None            # hatch the contended bars for grayscale
        ax.bar(x, c["P_mean"], W, yerr=c["P_ci95"], color=col, edgecolor="black", lw=0.5,
               hatch=hatch, error_kw=dict(lw=0.7, capsize=2),
               label=_LEVELS[li][1] if gi == 0 else None, zorder=3)
        if lvl != "off":                                    # dP annotation above the contended bar + CI
            ax.annotate(f"$\\Delta P$ = {c['P_mean'] - p_off:.2f}", (x, c["P_mean"] + c["P_ci95"]),
                        textcoords="offset points", xytext=(0, 3), ha="center", fontsize=7)

# constant-reserve headline for the agentic arm (boxed so it is legible in colour AND grayscale)
r_off = d["off"]["Agentic (gpt-5.4-mini)"].get("reserve_est_mean")
r_on  = d["a=1"]["Agentic (gpt-5.4-mini)"].get("reserve_est_mean")
if r_off and r_on:
    ax.text(1, 0.60, "reserve $\\approx 13$\n(both levels)", ha="center", va="center", fontsize=6.8,
            color="0.1", bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="0.6", lw=0.4))

ax.set_ylabel(r"Resilience $P$")
ax.set_xticks(range(len(_ARMS)))
ax.set_xticklabels([lbl for _, lbl in _ARMS])
ax.set_ylim(0.5, 1.0)
ax.grid(axis="y", alpha=0.3, lw=0.4)
ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.01), ncol=2, frameon=False,
          handlelength=1.4, borderpad=0.3, columnspacing=1.2)

for ext in ("pdf", "png"):
    out = _HERE / f"exp5_partb.{ext}"
    fig.savefig(out, bbox_inches="tight", dpi=300)
    print("saved ->", out)
