"""Exp 3 figures — attendance estimation across the event portfolio.

Reads experiments/exp3_reserve_sizing/reserve_sizing.json (written by
exp_3_reserve_sizing.py --save) and renders two IEEE single-column figures:

  • FOREST  (exp3_reserve_sizing_forest.pdf/png): one row per event, grouped
    sold-out / not-sold-out, showing the true attendance and each arm's estimate
    (with 95% CI whiskers on the LLM arms). Shows the estimate GENERALISES across
    the whole portfolio, and where the context-blind formula diverges.
  • SCATTER (exp3_reserve_sizing_scatter.pdf/png): estimated vs true attendance,
    log-log with a y=x reference. Points on the diagonal = accurate.

Only arms that produce an attendance estimate are plotted (formula + the LLMs);
the flat rule has no estimate and appears only in the reserve/QoS table.

Usage (run from this folder):  python plot_reserve_sizing.py [--seeds N]
"""
import argparse
import json
import math
import statistics
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as mticker
import numpy as np

_DIR  = Path(__file__).parent          # this script lives in the exp3 folder, next to its data + outputs
_JSON = _DIR / "reserve_sizing.json"
SUFFIX = ""     # appended to output filenames (e.g. "_5seed"); set in __main__

_TCRIT = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571,
          7: 2.447, 8: 2.365, 9: 2.306, 10: 2.262}


def _mean_ci(xs):
    xs = [float(x) for x in xs]
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    m = statistics.mean(xs)
    if n == 1:
        return m, 0.0
    sd = statistics.stdev(xs)
    return m, _TCRIT.get(n, 1.96) * sd / math.sqrt(n)


def _est(a, n):
    """(est, est_ci) for an arm dict. If n is set, recompute from the FIRST n seeds of raw_est
    (engaged/non-zero only); else use the stored full-run aggregate."""
    if a is None:
        return None, 0.0
    if n is None or not a.get("raw_est"):
        return a.get("est"), (a.get("est_ci") or 0.0)
    eng = [x for x in a["raw_est"][:n] if x > 0]
    if not eng:
        return 0, 0.0
    m, ci = _mean_ci(eng)
    return round(m), round(ci)

# arm -> (legend label, colour, marker, filled?)   — distinct SHAPES so it survives grayscale print
_STYLE = {
    "gpt-5.4-mini": ("GPT-5.4-mini", "C0", "o", True),
    "gemini":       ("Gemini",       "C1", "x", True),
    "formula":      ("Formula",      "C2", "o", False),
}
_TRUE = ("True", "black", "|", True)     # ground-truth attendance marker

plt.rc("font",   family="serif", size=8)
plt.rc("axes",   titlesize=8, labelsize=9, linewidth=0.8)
plt.rc("legend", fontsize=7.5)
plt.rc("xtick",  labelsize=8)
plt.rc("ytick",  labelsize=8)
plt.rc("lines",  linewidth=1.0)


def _load():
    if not _JSON.exists():
        sys.exit(f"No results at {_JSON} — run scripts.exp_3_reserve_sizing --llm --save first.")
    d = json.load(open(_JSON))
    arms = [a for a in ("gpt-5.4-mini", "gemini", "formula") if a in d["arms"]]
    return d, arms


def _k(x):                      # attendance -> thousands, floored to stay valid on a log axis
    return max(float(x), 100.0) / 1000.0


def _short(name, n=34):         # trim long event names for the row labels
    return name if len(name) <= n else name[: n - 1] + "…"


# ------------------------------------- forest -------------------------------------------------
def forest(d, arms, n=None):
    records = d["records"]
    sold    = sorted([r for r in records if r["sold_out"]],     key=lambda r: r["attendance"], reverse=True)
    notsold = sorted([r for r in records if not r["sold_out"]], key=lambda r: r["attendance"], reverse=True)

    # stack rows top->bottom with a blank gap + header between the two groups
    rows, ylabels, headers = [], [], []       # rows: list of (record or None); None = spacer
    for title, grp in (("Sold out", sold), ("Not sold out", notsold)):
        headers.append((len(rows), title))
        for r in grp:
            rows.append(r)
            ylabels.append(_short(r["name"]))
        rows.append(None)                     # spacer between groups
        ylabels.append("")
    if rows and rows[-1] is None:
        rows.pop(); ylabels.pop()

    n = len(rows)
    y = np.arange(n)[::-1]                     # first row at the top
    fig, ax = plt.subplots(figsize=(3.5, 0.30 * n + 0.9), constrained_layout=True)

    for i, r in enumerate(rows):
        if r is None:
            continue
        yi = y[i]
        ax.plot(_k(r["attendance"]), yi, marker="|", color="black",
                ms=9, mew=1.4, zorder=5)      # true attendance tick
        for arm in arms:
            est, est_ci = _est(r["arms"].get(arm), n)
            if not est:                      # None (no estimate) or 0 (never engaged)
                continue
            lbl, col, mk, filled = _STYLE[arm]
            ax.errorbar(_k(est), yi, xerr=est_ci / 1000.0, fmt=mk, color=col, ms=3.4,
                        mfc=(col if filled else "none"), mew=0.8, elinewidth=0.7,
                        capsize=1.5, zorder=4)

    ax.set_yticks(y)
    ax.set_yticklabels(ylabels)
    ax.set_xscale("log")
    ax.set_xlabel("Attendance (thousands)")
    ax.set_xlim(0.4, 200)
    ax.grid(axis="x", linestyle="--", linewidth=0.4, zorder=0)
    ax.set_axisbelow(True)
    for row_i, title in headers:              # group headers as bold text at the block top
        yi = y[row_i]
        ax.text(0.45, yi + 0.55, title, fontsize=6.5, style="italic", va="bottom")

    handles = [plt.Line2D([], [], color=_TRUE[1], marker="|", ls="", ms=8, mew=1.4, label=_TRUE[0])]
    for arm in arms:
        lbl, col, mk, filled = _STYLE[arm]
        handles.append(plt.Line2D([], [], color=col, marker=mk, ls="", ms=4,
                                  mfc=(col if filled else "none"), label=lbl))
    ax.legend(handles=handles, loc="lower right", frameon=True, framealpha=0.9,
              handlelength=1.2, borderpad=0.4)
    _save(fig, "forest")


# ------------------------------------- scatter ------------------------------------------------
def scatter(d, arms, n=None):
    records = d["records"]
    fig, ax = plt.subplots(figsize=(3.5, 3.2), constrained_layout=True)

    lim = (0.4, 100)
    # Non-uniform axis: the low decade [0.4,10]k gets one third of the span, the CROWDED
    # [10,100]k decade gets two thirds, so the big-attendance events spread out and stay readable.
    X0, X1, X2 = 0.4, 10.0, 100.0
    def _fwd(x):
        x = np.clip(np.asarray(x, float), 1e-6, None)
        lo = np.log10(x / X0) / np.log10(X1 / X0) / 3.0
        hi = 1.0 / 3 + np.log10(x / X1) / np.log10(X2 / X1) * (2.0 / 3)
        return np.where(x <= X1, lo, hi)
    def _inv(p):
        p = np.asarray(p, float)
        lo = X0 * (X1 / X0) ** (3.0 * p)
        hi = X1 * (X2 / X1) ** ((p - 1.0 / 3) * 1.5)
        return np.where(p <= 1.0 / 3, lo, hi)

    gx = np.logspace(np.log10(lim[0]), np.log10(lim[1]), 120)
    ax.fill_between(gx, 0.8 * gx, 1.2 * gx, color="0.75", alpha=0.30, lw=0, zorder=1,
                    label=r"$\pm20\%$ band")
    ax.plot(lim, lim, ls="--", color="0.4", lw=1.0, zorder=2)                   # y = x reference (unlabelled)

    for arm in [a for a in arms if a != "formula"]:     # LLM estimates vs the true diagonal only
        lbl, col, mk, filled = _STYLE[arm]
        pts = []
        for r in records:
            est, _ = _est(r["arms"].get(arm), n)
            if est:
                pts.append((_k(r["attendance"]), _k(est)))
        if not pts:
            continue
        xs, ys = zip(*pts)
        ax.plot(xs, ys, mk, color=col, ms=5, mfc=(col if filled else "none"),
                mew=2.0, ls="", zorder=3, label=lbl)   # smaller but bolder strokes

    ax.set_xscale("function", functions=(_fwd, _inv))
    ax.set_yscale("function", functions=(_fwd, _inv))
    ax.set_xlim(*lim); ax.set_ylim(*lim)
    ax.set_xlabel("True attendance (thousands)")
    ax.set_ylabel("Estimated attendance (thousands)")
    for axis in (ax.xaxis, ax.yaxis):                    # plain labels; extra tick at 30 in the wide decade
        axis.set_major_locator(mticker.FixedLocator([1, 10, 30, 100]))
        axis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:g}"))
        axis.set_minor_locator(mticker.NullLocator())
    ax.grid(True, which="major", linestyle="--", linewidth=0.4, color="0.85", zorder=0)
    ax.set_axisbelow(True)
    ax.legend(loc="lower right", frameon=True, framealpha=0.95, handlelength=1.0,
              borderpad=0.5, labelspacing=0.4)
    _save(fig, "scatter")


def _save(fig, tag):
    for ext in ("pdf", "png"):
        fig.savefig(_DIR / f"exp3_reserve_sizing_{tag}{SUFFIX}.{ext}", bbox_inches="tight", dpi=300)
    plt.close(fig)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Exp 3 reserve-sizing figures (forest + scatter)")
    p.add_argument("--seeds", type=int, default=None, metavar="N",
                   help="use only the first N seeds (recompute est from raw); writes _Nseed-suffixed "
                        "files so the full-run figures are NOT overwritten")
    args = p.parse_args()
    n = args.seeds
    SUFFIX = f"_{n}seed" if n else ""
    d, arms = _load()
    if not arms:
        sys.exit("No estimating arms in the JSON (run with --llm to get the LLM estimates).")
    forest(d, arms, n)
    scatter(d, arms, n)
    print(f"Saved forest + scatter to {_DIR}" + (f"  (first {n} seeds -> {SUFFIX})" if n else ""))
