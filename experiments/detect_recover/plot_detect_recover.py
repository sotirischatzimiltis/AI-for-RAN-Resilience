"""Resilience-timeline figure: seed-averaged utility u(t) for BOTH models on one axis, over BOTH
disruption windows (botnet storm + benign event), each annotated with its start t0, end td, and
per-model recovery timestamp tr. Window bounds come from the (deterministic) scenario; tr is derived
from the saved seed-averaged u(t), so this needs no LLM re-run. Reads detect_recover_<gpt|gemini>.json;
writes detect_recover.pdf/png."""
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
from sim.config import botnet_event_traffic          # noqa: E402
from shared.events import EXP1_EVENT                  # noqa: E402

_HERE = Path(__file__).resolve().parent
_MODELS = [("gpt", "GPT-5.4-Mini", "#c1272d"), ("gemini", "Gemini-3.1-Flash-Lite", "#1f77b4")]
data = {tag: (lbl, col, json.load(open(_HERE / f"detect_recover_{tag}.json")))
        for tag, lbl, col in _MODELS if (_HERE / f"detect_recover_{tag}.json").exists()}
if not data:
    raise SystemExit("no detect_recover_*.json found — run scripts/exp_detect_recover.py --save first")

# deterministic disruption windows for the scenario: [(botnet), (event)]
WINDOWS = botnet_event_traffic(EXP1_EVENT.surge_peak()).storm_windows()
LABELS = ["botnet", "event"]


def rest_target(um, first_onset, frac=0.95, margin=5.0):
    """Recovery target = frac * the RESTING utility (mean u over the initial calm period, before
    the first disruption). Using rest -- not the pre-window level -- avoids the pre-provisioning
    ramp inflating the baseline so a post-event return to calm still counts as recovered."""
    base = [r["u"] for r in um if r["t"] < first_onset - margin]
    return frac * (sum(base) / len(base)) if base else None


def recovery_time(um, w_td, target, hold=30.0):
    """First t >= w_td at which mean u returns above `target` and holds for `hold` s."""
    if target is None:
        return None
    pts = [(r["t"], r["u"]) for r in um]
    end_t = pts[-1][0]
    for t, u in pts:
        if t < w_td or u < target:
            continue
        if all(uu >= target for tt, uu in pts if t <= tt <= min(t + hold, end_t)):
            return t
    return None


plt.rc("font", family="serif", size=8)
plt.rc("axes", titlesize=8, labelsize=8.5, linewidth=0.8)
plt.rc("legend", fontsize=7); plt.rc("xtick", labelsize=8); plt.rc("ytick", labelsize=8)
plt.rc("mathtext", fontset="cm")

fig, ax = plt.subplots(figsize=(3.6, 2.7), constrained_layout=True)

# both disruption windows: shaded span + t0/td markers
for k, (w_t0, w_td) in enumerate(WINDOWS):
    ax.axvspan(w_t0, w_td, color="0.9", alpha=0.7, lw=0, zorder=0)
    for x, sym in ((w_t0, f"$t_0^{{({k+1})}}$"), (w_td, f"$t_d^{{({k+1})}}$")):
        ax.axvline(x, ls="--", color="0.4", lw=0.8, zorder=2)
        ax.text(x - 7, 102, sym, ha="center", va="bottom", fontsize=6.6, color="0.25")
    ax.text((w_t0 + w_td) / 2, 3, LABELS[k], ha="center", va="bottom", fontsize=6.5, color="0.35")

# one u(t) line per model; a per-model tr line (95% crossing + the 30s hold) styled like t0/td
HOLD = 30.0
for tag, (lbl, col, d) in data.items():
    um = d["util_mean"]
    ax.plot([r["t"] for r in um], [r["u"] for r in um], color=col, lw=1.5, label=lbl, zorder=3)
    target = rest_target(um, WINDOWS[0][0])
    dx = -6 if tag == "gpt" else 6                    # nudge labels apart where tr coincides
    for (w_t0, w_td) in WINDOWS:
        cross = recovery_time(um, w_td, target)       # first t with u >= 95% baseline (held)
        if cross is not None:
            tr = cross + HOLD                         # confirmed recovery: 95% held for 30s
            ax.axvline(tr, ls=":", color=col, lw=1.2, zorder=2)
            ax.text(tr + dx, 102, "$t_r$", ha="center", va="bottom", fontsize=6.6, color=col)

# time-to-detect: arrow from botnet onset to each model's first storm_active verdict
# (botnet window only -- the benign event is anticipated, never "detected"). Stacked in the
# empty lower part of the storm band so they read clearly.
for tag, y in (("gemini", 13), ("gpt", 25)):
    if tag not in data:
        continue
    _, col, d = data[tag]
    t_det, t0b = d.get("t_detect_mean"), d["botnet_onset"]
    if t_det is None:
        continue
    ax.annotate("", (t0b, y), (t_det, y), arrowprops=dict(arrowstyle="|-|,widthA=0.4,widthB=0.4",
                color=col, lw=1.1))
    ax.text(t_det + 2, y, f"detect {d['detect_lag_mean']:.0f}s", ha="left", va="center",
            fontsize=6.2, color=col)

ax.set_xlabel("Time (s)")
ax.set_ylabel(r"Utility $u$ (%)")
ax.set_ylim(0, 110)
ax.set_xlim(min(r["t"] for r in um), max(r["t"] for r in um))
ax.grid(True, ls="--", lw=0.4, color="0.9", zorder=0)

handles = [Line2D([], [], color=col, lw=1.5, label=lbl) for _, lbl, col in _MODELS
           if lbl in [v[0] for v in data.values()]]
ax.legend(handles=handles, loc="upper center", ncol=2, frameon=True, framealpha=0.95,
          edgecolor="0.6", handlelength=1.4, columnspacing=1.2, fontsize=6.8,
          bbox_to_anchor=(0.5, -0.16))

for ext in ("pdf", "png"):
    fig.savefig(_HERE / f"detect_recover.{ext}", bbox_inches="tight", dpi=300)
print("saved detect_recover.pdf/png ->", _HERE)
