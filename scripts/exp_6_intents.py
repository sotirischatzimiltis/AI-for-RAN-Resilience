"""
Experiment 6, Fold 1 — operator-intent grounding (Part 1).

Scores the Orchestrator (agents/orchestrator.py) on translating free-text operator intents into the
correct OperatorDirective, against the ground-truth portfolio of shared/operator_intents.py. This is
the Exp-3 idea applied to the orchestration tier: the model sees only the words and must ground them
into the network posture (priority / V / W), an SLA floor (min_servers), a scheduled event, and/or a
standing instruction for the site judge (nonrt_instruction). Each lever is scored, hallucinated levers
count against, and we report overall grounding accuracy, the exact-match rate, a per-lever breakdown,
and per-category accuracy for the carried judges.

A parse/API failure is NOT silently dropped: it is retried, and if it persists it is recorded as a
zero-scored row (so a model that cannot emit a valid directive is penalised, not flattered by a
shrinking denominator). A schema-violation count is reported alongside.

Two non-LLM baselines can be added with --baseline for a floor to compare the LLM against:
  null    — always the neutral directive (what "do nothing" scores).
  keyword — a fixed, generic cue-matcher (posture words, a number before "server(s)", a t= time,
            delegation phrases); it has NO understanding of waivers or context. The cue lists are
            chosen a priori, not tuned per test item.

Usage (source the shell env for the OpenRouter key first):
    python -m scripts.exp_6_intents --seeds 3 --save --log
    python -m scripts.exp_6_intents --seeds 3 --baseline both --save --log   # LLM + both floors
    python -m scripts.exp_6_intents --seeds 1 --models gpt                    # one model, quick
"""

import argparse
import asyncio
import hashlib
import json
import re
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.run import resolve_model
from scripts.exp_1_model_comparison import _Tee, _prevent_sleep, judge_settings
from scripts.exp_2_system_comparison import AGENTS          # [(label, slug, mode), ...]
from agents.orchestrator import build_orchestrator_agent, SYSTEM_PROMPT, PRIORITY_VW
from shared.policy import SharedPolicy
from shared.operator_intents import PORTFOLIO, score_directive, _PRIORITY_VW

_EXP_DIR  = Path(__file__).parent.parent / "experiments" / "exp6_intents"
_LOGS_DIR = _EXP_DIR / "logs"
_LEVERS   = ["posture", "weight", "min_servers", "schedule", "nonrt"]   # 'weight' only on graded items
_RETRIES  = 2                                              # transient-error retries before a row is scored 0


def _select_models(which: str) -> list[tuple[str, str, str]]:
    """AGENTS filtered by the --models flag ('both' | 'gpt' | 'gemini')."""
    if which == "both":
        return AGENTS
    key = "gpt" if which == "gpt" else "gemini"
    return [a for a in AGENTS if key in a[1]]


# ---------------------------------------------------------------------------
# LLM grounding
# ---------------------------------------------------------------------------
def _row(case, d, error: bool = False, err_msg: str = "") -> dict:
    """Score directive `d` against `case` and attach bookkeeping (full directive dump + a compact
    `got` for the log line)."""
    sc = score_directive(case, d)
    sc.update(category=case.category, text=case.text, error=error, err_msg=err_msg,
              directive=(d.model_dump() if hasattr(d, "model_dump") else vars(d)),
              got=dict(priority=d.priority, V=d.lyapunov_V, W=d.lyapunov_W,
                       min_servers=d.min_servers, sched_t=d.schedule_event_t,
                       sched_name=d.schedule_event_name, nonrt=bool(d.nonrt_instruction)))
    return sc


def _zero_row(case, err: str) -> dict:
    """A persistent failure scored as all-wrong: n applicable levers, 0 correct. Kept in the sample so
    a model that cannot produce a valid directive is penalised rather than silently excused."""
    has_band = case.v_band is not None or case.w_band is not None
    # posture (if applicable) + weight (if a band) + floor + schedule + nonrt
    n = (1 if case.priority is not None else 0) + (1 if has_band else 0) + 3
    row = {"n": n, "correct": 0, "exact": False, "error": True, "err_msg": err,
           "category": case.category, "text": case.text, "directive": None,
           "got": dict(priority=None, V=None, W=None, min_servers=None,
                       sched_t=None, sched_name=None, nonrt=None)}
    if case.priority is not None:
        row["posture"] = False
    if has_band:
        row["weight"] = False
    row["min_servers"] = row["schedule"] = row["nonrt"] = False
    return row


async def _ground_one(agent, case, settings) -> dict:
    """Run the Orchestrator on one intent (with retries) and score it. A fresh policy context mirrors
    route_intent's prompt so grounding is measured under production conditions."""
    prompt = f"Operator intent: {case.text}\nCurrent {SharedPolicy().context_str()}"
    for attempt in range(_RETRIES + 1):
        try:
            d = (await agent.run(prompt, model_settings=settings)).output
            return _row(case, d)
        except Exception as e:                            # transient (API) or persistent (schema) — retry, then score 0
            if attempt < _RETRIES:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            print(f"[ig] {case.category:24s} PERSISTENT ERROR {type(e).__name__}: {e} -> scored 0")
            return _zero_row(case, f"{type(e).__name__}: {e}")


async def _run(models, seeds) -> dict:
    results: dict[str, list] = {}
    for label, slug, mode in models:
        model    = resolve_model(slug)
        settings = judge_settings(slug, mode)
        agent    = build_orchestrator_agent(model)
        print(f"\n############  {label}  ({slug}, {seeds} seeds)  ############")
        rows = []
        for case in PORTFOLIO:
            for s in range(1, seeds + 1):
                sc = await _ground_one(agent, case, settings)
                rows.append(sc)
                if s == 1:                                   # one line per intent to keep logs readable
                    marks = "".join("Y" if sc.get(k, True) else "." for k in _LEVERS)
                    print(f"[ig] {case.category:24s} {marks}  {sc['correct']}/{sc['n']}  "
                          f"prio={sc['got']['priority']} floor={sc['got']['min_servers']} "
                          f"sched={sc['got']['sched_t']} nonrt={sc['got']['nonrt']}")
        results[label] = rows
    return results


# ---------------------------------------------------------------------------
# Non-LLM baselines (a floor for the LLM number). Deterministic — run once.
# ---------------------------------------------------------------------------
# Generic cue lists, fixed a priori (NOT tuned against the portfolio items). The keyword baseline has
# no notion of a waiver, a combination, or context — it is meant to be beaten on the trap and combos.
_QOS_WORDS  = ("connect", "service", "quality", "protect", "drop", "uptime", "maximise", "maximize", "vip")
_COST_WORDS = ("cost", "lean", "cheap", "spend", "budget", "efficien", "save", "frugal")
_NONRT_WORDS = ("not an attack", "not a storm", "legitimate", "do not filter", "don't filter",
                "admit", "err on the side", "genuine", "refreshing")
_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
            "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_FLOOR_CUES = ("at least", "no fewer", "keep", "hold", "minimum", "warm", "online")


def _baseline_directive(kind: str, text: str) -> SimpleNamespace:
    """Synthesise a directive-shaped object (the attributes score_directive reads) with no LLM."""
    if kind == "null":                                    # the neutral 'do nothing' floor
        return SimpleNamespace(priority="balanced", lyapunov_V=None, lyapunov_W=None,
                               min_servers=None, schedule_event_t=None,
                               schedule_event_name=None, nonrt_instruction=None)
    low = text.lower()
    nq  = sum(w in low for w in _QOS_WORDS)
    nc  = sum(w in low for w in _COST_WORDS)
    prio = "qos" if nq > nc else "cost" if nc > nq else "balanced"   # tie -> balanced (no waiver logic)

    floor = None                                          # a number immediately before 'server(s)', gated on a floor cue
    m = re.search(r"\b(\w+)\s+servers?\b", low)
    if m and any(cue in low for cue in _FLOOR_CUES):
        tok = m.group(1)
        floor = int(tok) if tok.isdigit() else _WORDNUM.get(tok)

    ms = re.search(r"t\s*=\s*(\d+)", low)                 # an explicit t= time
    st = float(ms.group(1)) if ms else None
    sn = "scheduled event" if ms else None

    nrt = ("Treat this elevated load as legitimate demand; do not classify a high arrival rate alone "
           "as a malicious storm.") if any(w in low for w in _NONRT_WORDS) else None
    return SimpleNamespace(priority=prio, lyapunov_V=None, lyapunov_W=None, min_servers=floor,
                           schedule_event_t=st, schedule_event_name=sn, nonrt_instruction=nrt)


def _run_baseline(kind: str) -> list[dict]:
    print(f"\n############  baseline-{kind}  (deterministic, no LLM)  ############")
    rows = []
    for case in PORTFOLIO:
        d  = _baseline_directive(kind, case.text)
        sc = _row(case, d)
        rows.append(sc)
        marks = "".join("Y" if sc.get(k, True) else "." for k in _LEVERS)
        print(f"[ig] {case.category:24s} {marks}  {sc['correct']}/{sc['n']}  "
              f"prio={sc['got']['priority']} floor={sc['got']['min_servers']} "
              f"sched={sc['got']['sched_t']} nonrt={sc['got']['nonrt']}")
    return rows


# ---------------------------------------------------------------------------
# Aggregation / reporting
# ---------------------------------------------------------------------------
def _agg(rows: list[dict]) -> dict:
    """Overall grounding accuracy (levers correct / applied), exact-match rate, per-lever accuracy,
    per-category accuracy, and the schema-violation count over the rows of one arm."""
    tot_c = sum(r["correct"] for r in rows); tot_n = sum(r["n"] for r in rows)
    exact = statistics.mean([r["exact"] for r in rows]) if rows else 0.0
    lever = {}
    for k in _LEVERS:
        vals = [r[k] for r in rows if k in r]
        lever[k] = statistics.mean(vals) if vals else None
    cat = {}
    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)
    for c, rs in by_cat.items():
        cat[c] = sum(x["correct"] for x in rs) / max(1, sum(x["n"] for x in rs))
    return {"grounding_acc": tot_c / max(1, tot_n), "exact_rate": exact,
            "per_lever": lever, "per_category": cat, "n_rows": len(rows),
            "schema_violations": sum(bool(r.get("error")) for r in rows)}


def _report(results: dict) -> dict:
    summary = {}
    print("\n" + "=" * 78)
    print("OPERATOR-INTENT GROUNDING  —  grounding accuracy / exact-match / per-lever")
    print("=" * 78)
    for label, rows in results.items():
        a = _agg(rows)
        summary[label] = a
        print(f"\n  {label}")
        print(f"    grounding accuracy : {a['grounding_acc']:.3f}   exact-match : {a['exact_rate']:.3f}"
              f"   (n={a['n_rows']}, schema-violations={a['schema_violations']})")
        print("    per lever          : " +
              "  ".join(f"{k}={v:.2f}" for k, v in a["per_lever"].items() if v is not None))
        worst = sorted(a["per_category"].items(), key=lambda kv: kv[1])[:3]
        print("    weakest categories : " + "  ".join(f"{c}={v:.2f}" for c, v in worst))
    print("=" * 78)
    return summary


def _git_commit() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       cwd=Path(__file__).parent.parent,
                                       stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return None


def _provenance(models, seeds, baselines) -> dict:
    """What produced these numbers — so a results file stays attributable while we iterate the prompt."""
    return {"timestamp": datetime.now().isoformat(timespec="seconds"),
            "git_commit": _git_commit(),
            "prompt_sha256_12": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:12],
            "prompt_chars": len(SYSTEM_PROMPT),
            "priority_vw": PRIORITY_VW,
            "seeds": seeds,
            "baselines": baselines,
            "models": [{"label": l, "slug": s, "mode": m, "settings": str(judge_settings(s, m))}
                       for l, s, m in models]}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Exp 6 Fold 1 — operator-intent grounding")
    p.add_argument("--seeds", type=int, default=3, help="repeats per intent (captures model variance)")
    p.add_argument("--models", choices=["both", "gpt", "gemini"], default="both")
    p.add_argument("--baseline", choices=["none", "null", "keyword", "both"], default="none",
                   help="add non-LLM floor arm(s) alongside the LLM(s)")
    p.add_argument("--save", action="store_true", help="write results JSON to experiments/exp6_intents/")
    p.add_argument("--log", nargs="?", const="AUTO", default=None,
                   help="tee output to a file (bare --log auto-names it under exp6_intents/logs/)")
    args = p.parse_args()

    # Guard: the scorer's local PRIORITY_VW copy must match the orchestrator's, or every posture item
    # is silently mis-scored. Fail loud at start-up rather than reporting wrong numbers.
    assert _PRIORITY_VW == PRIORITY_VW, (
        f"PRIORITY_VW drift: operator_intents={_PRIORITY_VW} vs orchestrator={PRIORITY_VW}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _logfile = None
    if args.log is not None:
        log_path = (_LOGS_DIR / f"log_intents_{stamp}.txt" if args.log == "AUTO" else Path(args.log))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _logfile = open(log_path, "w")
        sys.stdout = _Tee(sys.__stdout__, _logfile); sys.stderr = _Tee(sys.__stderr__, _logfile)
        print(f"[ig] logging to {log_path}")

    _prevent_sleep()
    models    = _select_models(args.models)
    baselines = {"none": [], "both": ["null", "keyword"]}.get(args.baseline, [args.baseline])

    async def _main():
        results = await _run(models, args.seeds)
        for kind in baselines:
            results[f"baseline-{kind}"] = _run_baseline(kind)
        summary = _report(results)
        if args.save:
            _EXP_DIR.mkdir(parents=True, exist_ok=True)
            out = _EXP_DIR / f"intents_{stamp}.json"
            out.write_text(json.dumps({"provenance": _provenance(models, args.seeds, baselines),
                                       "summary": summary, "rows": results}, indent=2, default=str))
            print(f"\n  saved -> {out}")

    try:
        asyncio.run(_main())
    finally:
        if _logfile is not None:
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
            _logfile.close()
