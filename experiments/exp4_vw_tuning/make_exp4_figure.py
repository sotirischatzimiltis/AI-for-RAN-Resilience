"""Compact single-column Exp 4 figure: benign users served vs provisioning delay, for the STEP and
RAMP onsets at low (V=1) and high (V=20) utility weight (W=1 slice). One panel carries both
takeaways: the delay is the severity knob, the step is catastrophic and untunable (its two lines
coincide near 0.15), and only the ramp stays tunable (high-V line holds up). Reads vw_tuning.json;
writes exp4_vw_sweep.pdf/png next to it."""
import json, math
from pathlib import Path
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

_DIR = Path(__file__).parent
d = json.load(open(_DIR / "vw_tuning.json"))
res, delays = d["results"], d["delays"]

def series(scn, V):                       # benign served as a PERCENTAGE (0..100)
    ys, es = [], []
    for dl in delays:
        c = res[f"delay={dl}"][scn][f"V={V},W=1.0"]
        ys.append(100 * c["benign_mean"]); es.append(100 * c["benign_ci95"])
    return ys, es

plt.rc("font", family="serif", size=8)
plt.rc("axes", titlesize=8, labelsize=8.5, linewidth=0.8)
plt.rc("legend", fontsize=7); plt.rc("xtick", labelsize=8); plt.rc("ytick", labelsize=8)

fig, ax = plt.subplots(figsize=(3.4, 2.35), constrained_layout=True)
# (label, scenario, V, colour, linestyle, marker, fill)
LINES = [
    ("Step, $V{=}1$",   "single_storm", "1.0",  "#c1272d", "--", "o", "none"),
    ("Step, $V{=}20$",  "single_storm", "20.0", "#c1272d", "-",  "o", "full"),
    ("Ramp, $V{=}1$",   "single_ramp",  "1.0",  "#0072b2", "--", "s", "none"),
    ("Ramp, $V{=}20$",  "single_ramp",  "20.0", "#0072b2", "-",  "s", "full"),
]
for lbl, scn, V, col, ls, mk, fill in LINES:
    ys, es = series(scn, V)
    ax.errorbar(delays, ys, yerr=es, color=col, ls=ls, marker=mk, ms=4.5,
                mfc=(col if fill == "full" else "white"), mew=1.1, lw=1.3,
                elinewidth=0.7, capsize=1.8, label=lbl, zorder=3)
ax.set_xlabel("Provisioning delay (s)")
ax.set_ylabel("Benign users served")
ax.set_ylim(0, 105); ax.set_xlim(-0.4, 10.4)
ax.yaxis.set_major_formatter(mticker.PercentFormatter(xmax=100, decimals=0))
ax.set_xticks(delays)
ax.grid(True, ls="--", lw=0.4, color="0.85", zorder=0); ax.set_axisbelow(True)
ax.legend(loc="upper right", frameon=True, framealpha=0.95, handlelength=1.8,
          borderpad=0.4, labelspacing=0.3, ncol=1)
for ext in ("pdf", "png"):
    fig.savefig(_DIR / f"exp4_vw_sweep.{ext}", bbox_inches="tight", dpi=300)
print("saved exp4_vw_sweep.pdf/png ->", _DIR)
