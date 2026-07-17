"""
Render the model bake-off (Experiment 1) results as a TABLE.

Reads results/model_comparison.json and prints, for the discriminating scenario
(multi_storm_flat by default), a table ranked by botnet-blocked rate:

    Model | Reasoning | Blocked (mean±std) | Benign | P | $/episode | Latency | Err

Emits two formats:
  • a readable Markdown table (stdout)
  • a LaTeX booktabs table, saved to results/model_comparison_table.tex

Run after a sweep (and after blocked_std is present — new sweeps store it; older
runs can be backfilled from the logs):
    python -m scripts.table_model_comparison
    python -m scripts.table_model_comparison --scenario single_storm
"""

import argparse
import json
import sys
from pathlib import Path

_DEFAULT_JSON = Path(__file__).parent.parent / "results" / "model_comparison.json"


def _rows(models: dict, scenario: str):
    """(label, reasoning, dict) per model that ran this scenario, ranked by blocked."""
    out = []
    for _run_id, v in models.items():
        s = v["scenarios"].get(scenario)
        if s:
            out.append((v["slug"].split("/")[-1], v["reasoning"], s))
    out.sort(key=lambda r: r[2].get("blocked_mean", 0.0), reverse=True)
    return out


def _fmt_pm(mean, std, nd=3):
    return f"{mean:.{nd}f} ± {std:.{nd}f}" if std is not None else f"{mean:.{nd}f}"


def markdown_table(rows, scenario: str, seeds) -> str:
    head = (f"### Model bake-off — {scenario} ({len(seeds)} seeds, bare judge)\n\n"
            "| Model | Rsn | Botnet-blocked ↑ | Benign | P | $/ep | Lat (s) | Err |\n"
            "|---|---|---|---|---|---|---|---|\n")
    lines = []
    for name, rsn, s in rows:
        lines.append(
            f"| {name} | {rsn} | {_fmt_pm(s['blocked_mean'], s.get('blocked_std'))} "
            f"| {s['benign_mean']:.3f} | {_fmt_pm(s['P_mean'], s.get('P_std'))} "
            f"| {s['usd_per_episode']:.4f} | {s['mean_latency_s']:.1f} | {s['errors_total']} |")
    return head + "\n".join(lines) + "\n"


def latex_table(rows, scenario: str, seeds) -> str:
    """Publication-quality booktabs table. Winner (top blocked-rate) in bold; the
    findings the numbers alone don't show live in the caption. Requires \\usepackage{booktabs}."""
    scn = scenario.replace("_", r"\_")
    n_err = sum(1 for _, _, s in rows if s.get("errors_total", 0) > 0)
    err_note = ("" if n_err == 0 else
                r" \texttt{gpt-4o-mini} produced a small number of malformed outputs over "
                r"the sweep; all other models produced none.")
    hdr = [
        r"% Requires \usepackage{booktabs} and amsmath (\mathbf, \pm, \approx).",
        r"\begin{table}[t]",
        r"\centering",
        rf"\caption{{LLM storm-judge comparison on the \texttt{{{scn}}} scenario "
        rf"({len(seeds)} seeds, bare judge). Resilience $P$ is capacity-bound and near-identical "
        r"across models ($\approx\!0.835$), and benign-served is $1.000$ for all (the malicious-UE "
        r"filter is botnet-targeted); models are therefore ranked by botnet-blocked rate "
        r"($\pm$\,std). For \texttt{gpt-5.4-mini}, reasoning \emph{off} strictly dominates "
        r"\emph{on} (non-overlapping per-seed ranges), i.e.\ reasoning does not help this task. "
        rf"The selected judge is shown in bold.{err_note}}}",
        r"\label{tab:model_bakeoff}",
        r"\small",
        r"\setlength{\tabcolsep}{5pt}",
        r"\begin{tabular}{llcccc}",
        r"\toprule",
        r"Model & Rsn. & Blocked\,$\uparrow$ & $P$ & \$/ep & Lat.\,(s) \\",
        r"\midrule",
    ]
    body = []
    for i, (name, rsn, s) in enumerate(rows):
        nm  = name.replace("_", r"\_")
        rs  = "--" if rsn == "n/a" else rsn
        blk = f"{s['blocked_mean']:.3f}\\,\\pm\\,{s.get('blocked_std', 0.0):.3f}"
        cost, lat, P = s["usd_per_episode"], s["mean_latency_s"], s["P_mean"]
        if i == 0:   # winner row — bold model + blocked
            body.append(rf"\textbf{{{nm}}} & {rs} & $\mathbf{{{blk}}}$ & "
                        rf"{P:.3f} & {cost:.3f} & {lat:.1f} \\")
        else:
            body.append(rf"{nm} & {rs} & ${blk}$ & {P:.3f} & {cost:.3f} & {lat:.1f} \\")
    ftr = [r"\bottomrule", r"\end{tabular}", r"\end{table}"]
    return "\n".join(hdr + body + ftr) + "\n"


def main(args) -> None:
    path = Path(args.json)
    if not path.exists():
        sys.exit(f"No results at {path} — run a sweep with --save first.")
    data = json.loads(path.read_text())
    scenario = args.scenario
    if scenario not in data["scenarios"]:
        sys.exit(f"Scenario '{scenario}' not in results (have {data['scenarios']}).")

    rows = _rows(data["models"], scenario)
    if rows and rows[0][2].get("blocked_std") is None:
        print("[warn] blocked_std missing — run a fresh sweep or backfill from logs "
              "for error bars.\n", file=sys.stderr)

    print(markdown_table(rows, scenario, data["seeds"]))

    tex = latex_table(rows, scenario, data["seeds"])
    out = path.parent / "model_comparison_table.tex"
    out.write_text(tex)
    print(f"LaTeX table saved -> {out}\n")
    print(tex)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Render the model bake-off results as a table")
    p.add_argument("--json", default=str(_DEFAULT_JSON))
    p.add_argument("--scenario", default="multi_storm_flat",
                   help="scenario to tabulate (default: multi_storm_flat, the discriminating one)")
    main(p.parse_args())
