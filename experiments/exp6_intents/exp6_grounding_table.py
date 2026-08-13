"""
Experiment 6, Fold 1, Part 1 — the operator-intent grounding TABLE.

Emits a booktabs LaTeX table from the blessed grounding result (grounding_blessed.json). Preferred
over the bar chart because most cells sit near 1.0 and the discriminating signal is in a few columns
(exact-match, posture, weight) where precise numbers matter (GPT 0.984 vs Gemini 0.971 does not read
off a bar). Two LLM arms above the rule, the two non-LLM floors below. Writes exp6_grounding_table.tex.
"""
from __future__ import annotations
import json
from pathlib import Path

_HERE = Path(__file__).parent
_BLESSED = _HERE / "grounding_blessed.json"

# (display label, substring). Order: LLMs, then floors (a \midrule separates them).
ARMS = [("GPT-5.4-Mini", "gpt"), ("Gemini-3.1-FL", "gemini"), ("Keyword", "keyword"), ("Null", "null")]
# (column header, key, kind, decimals). 'o' = top-level summary, 'l' = per_lever.
COLS = [("Ground.", "grounding_acc", "o", 3), ("Exact", "exact_rate", "o", 3),
        ("Posture", "posture", "l", 2), ("Weight", "weight", "l", 2), ("Floor", "min_servers", "l", 2),
        ("Sched.", "schedule", "l", 2), ("Deleg.", "nonrt", "l", 2)]


def _find(summary, sub):
    return next(v for k, v in summary.items() if sub in k.lower())


def _val(arm, key, kind):
    return arm[key] if kind == "o" else arm["per_lever"][key]


def build() -> str:
    summary = json.loads(_BLESSED.read_text())["summary"]
    rows = {lbl: _find(summary, sub) for lbl, sub in ARMS}
    # bold the best value in each of the two OVERALL columns (the headline ranking)
    best = {key: max(_val(rows[lbl], key, kind) for lbl, _ in ARMS)
            for hdr, key, kind, d in COLS if kind == "o"}

    def cell(lbl, key, kind, d):
        v = _val(rows[lbl], key, kind)
        s = f"{v:.{d}f}"
        return f"\\textbf{{{s}}}" if kind == "o" and abs(v - best[key]) < 1e-9 else s

    lines = [r"\begin{table}[t]", r"\centering",
             r"\caption{Operator-intent grounding on 40 held-out intents, 3 seeds and $n{=}120$ per "
             r"model. Two LLM agents above the rule, a keyword matcher and a do-nothing floor below. "
             r"Overall best in bold.}",
             r"\label{tab:intent_grounding}",
             r"\resizebox{\columnwidth}{!}{%",
             r"\begin{tabular}{l" + "c" * len(COLS) + "}", r"\toprule",
             " & " + " & ".join(h for h, *_ in COLS) + r" \\", r"\midrule"]
    for i, (lbl, _) in enumerate(ARMS):
        if i == 2:                                            # rule between LLMs and floors
            lines.append(r"\midrule")
        lines.append(f"{lbl} & " + " & ".join(cell(lbl, k, kd, d) for _, k, kd, d in COLS) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}%", r"}", r"\end{table}"]
    return "\n".join(lines)


if __name__ == "__main__":
    tex = build()
    (_HERE / "exp6_grounding_table.tex").write_text(tex + "\n")
    print(tex)
    print("\nsaved -> exp6_grounding_table.tex")
