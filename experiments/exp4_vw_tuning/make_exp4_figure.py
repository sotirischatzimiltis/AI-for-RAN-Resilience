"""Compact single-column Exp 4 figure: benign users served vs provisioning delay, for the STEP and
RAMP onsets across utility weights V in {1,10,20} (W=1 slice). One panel carries both takeaways:
the delay is the severity knob, the step is catastrophic and untunable (its lines collapse together
near the floor whatever V is), and only the ramp stays tunable (benign service fans out and rises
with V). Reads vw_tuning.json; writes exp4_vw_sweep.pdf/png next to it."""
import json
from pathlib import Path
import matplotlib.pyplot as plt

_DIR = Path(__file__).parent
d = json.load(open(_DIR / "vw_tuning.json"))
res, delays = d["results"], d["delays"]

def series(scn, V):                       # benign served as a PERCENTAGE (0..100), mean only
    return [100 * res[f"delay={dl}"][scn][f"V={V},W=1.0"]["benign_mean"] for dl in delays]

plt.rc("font", family="serif", size=8)
plt.rc("axes", titlesize=8, labelsize=8.5, linewidth=0.8)
plt.rc("legend", fontsize=6.5); plt.rc("xtick", labelsize=8); plt.rc("ytick", labelsize=8)

fig, ax = plt.subplots(figsize=(3.4, 2.7))

V_VALUES = [("1.0", "1"), ("10.0", "10"), ("20.0", "20")]
SHADES = [0.42, 0.68, 0.95]                 # higher V -> darker line
# (scenario key, legend name, colormap, marker) — step is red, ramp is blue
SCN = [
    ("single_storm", "Step", plt.get_cmap("Reds"),  "o"),
    ("single_ramp",  "Ramp", plt.get_cmap("Blues"), "s"),
]
handles = {}
for scn, name, cmap, mk in SCN:
    for (V, Vlbl), sh in zip(V_VALUES, SHADES):
        (line,) = ax.plot(delays, series(scn, V), color=cmap(sh), ls="-", marker=mk,
                          ms=4.0, mew=0.8, lw=1.3, label=f"{name}, $V{{=}}{Vlbl}$", zorder=3)
        handles[(name, Vlbl)] = line

ax.set_xlabel("Provisioning delay (s)", labelpad=44)   # sits BELOW the legend strip
ax.set_ylabel(r"Benign users served $S_b$ (%)")
ax.set_ylim(0, 105); ax.set_xlim(-0.4, 10.4)
ax.set_xticks(delays)
ax.grid(True, ls="--", lw=0.4, color="0.85", zorder=0); ax.set_axisbelow(True)

# legend as a horizontal strip below the axes but above the x-axis title: columns = V, rows = onset
order = [handles[(name, Vlbl)] for Vlbl in ("1", "10", "20") for name in ("Step", "Ramp")]
ax.legend(order, [h.get_label() for h in order], loc="upper center",
          bbox_to_anchor=(0.5, -0.14), ncol=3, frameon=True, handlelength=1.6,
          columnspacing=1.1, labelspacing=0.3, handletextpad=0.5)
fig.subplots_adjust(left=0.14, right=0.97, top=0.97, bottom=0.30)
for ext in ("pdf", "png"):
    fig.savefig(_DIR / f"exp4_vw_sweep.{ext}", bbox_inches="tight", dpi=300)
print("saved exp4_vw_sweep.pdf/png ->", _DIR)
