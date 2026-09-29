"""
Plot for Experiment 7 (cascading / simultaneous stressors): episode resilience P versus
shared-compute contention severity a, one line per controller, mean with 95% CI over 5 seeds.

Data are the 5-seed means/CIs from scripts/exp_7_cascading.py (botnet+event scenario with the
shared pool at a in {0, 0.5, 1.0}). Run from this directory or the repo root:
    python experiments/exp7_cascading/plot_exp7_cascading.py
Writes exp7_cascading.pdf (+ .png) alongside this script. Copy the PDF into the paper's
Figures_TNSE_sub/ to update Fig.~\\ref{fig:exp7}.
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

A = [0.0, 0.5, 1.0]                         # contention severity swept
# (label, P means, P 95% CI, colour, marker, linewidth, z-order) — 5 seeds, exp_7_cascading.py
SERIES = [
    ("Static ($c{=}16$)",         [0.992, 0.990, 0.858], [0.000, 0.002, 0.004], "#1f3b73", "s", 1.6, 2),
    ("Lyapunov",                  [0.684, 0.684, 0.683], [0.002, 0.002, 0.002], "#8a8f98", "v", 1.4, 1),
    ("Rule-based",                [0.710, 0.710, 0.710], [0.005, 0.004, 0.005], "#9c7a3c", "D", 1.4, 1),
    ("Agentic (Gemini)",          [0.874, 0.880, 0.823], [0.012, 0.029, 0.089], "#1b9e77", "o", 2.4, 3),
    ("Agentic (GPT-5.4-Mini)",    [0.905, 0.903, 0.810], [0.007, 0.009, 0.060], "#c1272d", "^", 2.4, 3),
]


def main() -> None:
    # match the Exp-5 figure style (exp5_partb.py) for a consistent look in the paper
    plt.rc("font", family="serif", size=8)
    plt.rc("axes", titlesize=8, labelsize=8.5, linewidth=0.8)
    plt.rc("legend", fontsize=7)
    plt.rc("xtick", labelsize=8); plt.rc("ytick", labelsize=8)

    # wide landscape canvas so the axes stays broad and the below-axes legend gets its own strip
    # underneath rather than squeezing the plot (Exp 5 is 3.4x2.5 with the legend above)
    fig, ax = plt.subplots(figsize=(4.0, 2.9), constrained_layout=True)
    for label, m, ci, c, mk, lw, z in SERIES:
        ax.errorbar(A, m, yerr=ci, label=label, color=c, marker=mk, markersize=4.5,
                    linewidth=lw, capsize=2.5, zorder=z,
                    markeredgecolor="white", markeredgewidth=0.5)
    ax.set_xlabel("Contention severity $a$")
    ax.set_ylabel(r"Resilience $P$")
    ax.set_xticks(A)
    ax.set_ylim(0.62, 1.01)
    ax.grid(True, linewidth=0.4, alpha=0.5)
    # legend BELOW the axes (3 columns) so it never covers the flat Lyapunov/Rule lines
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.20), ncol=3, frameon=False,
              handlelength=1.6, columnspacing=1.1)

    here = Path(__file__).resolve().parent
    fig.savefig(here / "exp7_cascading.pdf", bbox_inches="tight", dpi=300)
    fig.savefig(here / "exp7_cascading.png", bbox_inches="tight", dpi=300)
    print("wrote", here / "exp7_cascading.pdf")


if __name__ == "__main__":
    main()
