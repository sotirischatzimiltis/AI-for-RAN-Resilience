"""
Experiment 6, Fold 1 — the end-to-end intent demos (two of them).

Each demo drives the FULL live loop (orchestrator -> route_intent -> MCP judge -> fast loop -> sim)
with one free-text operator intent, run twice (baseline with no intent, then with the intent), and
saves the multi-agent INTERACTION for the paper (operator -> orchestrator directive -> judge
assessment -> outcome).

  reserve : single_storm. A sold-out-stadium intent NAMES a scaled event, so the orchestrator fills
            schedule_event_{name,venue,sold_out,t} plus a delegation, the judge sizes the crowd (Exp 3)
            and pre-provisions a reserve, and admits the surge instead of filtering it.
  posture : single_ramp. A QoS strength intent sets priority=qos (full V), so the controller
            over-provisions a benign ramp. No scheduling or delegation, just the posture lever.

Writes one JSON per demo (structured trace + a boxed text transcript + the baseline-vs-intent
metrics). LLM + MCP run (source ~/.zshrc first):
    python -m scripts.exp_6_demo --seeds 3 --save --log
"""
import argparse
import asyncio
import json
import logging
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.server import mcp, MCP_HOST, MCP_PORT
from scripts.run import resolve_model
from scripts.exp_1_model_comparison import _Tee, _prevent_sleep
from agents.orchestrator import run_episode
from runtime import host as sim_host
from sim.metrics import benign_false_positive_rate

_EXP_DIR   = Path(__file__).parent.parent / "experiments" / "exp6_intents"
_LOGS_DIR  = _EXP_DIR / "logs"
_MODEL_SLUG = "openrouter:openai/gpt-5.4-mini"

# Two demos, each an operator intent that exercises a DIFFERENT directive lever end-to-end.
# The reserve demo (schedule+delegate on single_storm) is kept here for reference but DROPPED from the
# paper: with a deliberately vague crowd the judge under-sizes the reserve, and a STEP surge caps the
# gain (Exp 4), so benign/P stay ~flat while only FP and servers move. The POSTURE demo is the
# end-to-end example we use.
_RESERVE_DEMO = dict(key="reserve", scenario="single_storm", t_post=60.0, inject_at=5.0,
    blurb="intent -> crowd estimate -> reserve (schedule + delegate)",
    intent=("A sold-out stadium beside this cell empties at t=50 seconds and the crowd will "
            "reconnect at once. This is a legitimate flash crowd, so tell the site judge not to "
            "treat the surge as an attack."))
DEMOS = [
    dict(key="posture", scenario="single_ramp", t_post=60.0, inject_at=5.0,
         blurb="intent -> network posture V/W (over-provision)",
         intent=("Tonight is a major product launch. Protect service quality and spare no capacity.")),
]
_METRICS = [("benign", "benign"), ("benign_fp", "benign FP"), ("P", "P"), ("servers", "servers")]


async def _one(model, demo, seed: int, with_intent: bool) -> dict:
    intents = [(demo["inject_at"], "site", demo["intent"])] if with_intent else None
    r = await run_episode(model=model, scenario=demo["scenario"], seed=seed, t_post=demo["t_post"],
                          assessment_interval_s=5.0, window_s=15.0, intents=intents, no_calendar=False)
    fp = benign_false_positive_rate(sim_host.sim.stats) if sim_host.sim else 0.0
    return {"P": r["final_P"], "benign": r["benign_success_rate"], "servers": r["avg_servers"],
            "benign_fp": round(fp, 4), "intents_routed": r["intents_routed"]}


def _agg(rows: list[dict]) -> dict:
    out = {}
    for k in ("P", "benign", "servers", "benign_fp"):
        vals = [r[k] for r in rows] or [0.0]
        out[k] = round(statistics.mean(vals), 4)
        out[f"{k}_ci"] = round(1.96 * statistics.stdev(vals) / len(vals) ** 0.5, 4) if len(vals) > 1 else 0.0
    return out


async def _run_demo(model, demo, seeds: int) -> dict:
    results = {}
    for label, with_intent in (("baseline", False), ("intent", True)):
        print(f"\n@@@ {demo['key']} | {label} @@@")               # parse anchor for the transcript
        rows = []
        for s in range(1, seeds + 1):
            try:
                r = await _one(model, demo, s, with_intent)
            except Exception as e:
                print(f"[demo] {demo['key']}/{label} seed={s} ERROR {type(e).__name__}: {e}")
                continue
            rows.append(r)
            print(f"[demo] {demo['key']}/{label} seed={s}  P={r['P']:.3f} benign={r['benign']:.3f} "
                  f"fp={r['benign_fp']:.3f} servers={r['servers']:.1f} routed={r['intents_routed']}")
        results[label] = {"agg": _agg(rows), "rows": rows}

    b, i = results["baseline"]["agg"], results["intent"]["agg"]
    print(f"\n=== DEMO {demo['key']}  ({demo['scenario']}, {demo['blurb']}) ===")
    print(f"  {'metric':10s} {'baseline':>9s} {'+intent':>9s} {'delta':>9s}")
    for k, lbl in _METRICS:
        print(f"  {lbl:10s} {b[k]:>9.3f} {i[k]:>9.3f} {i[k]-b[k]:>+9.3f}")
    return results


# ---------------------------------------------------------------------------
# Interaction capture: parse the run's own log (no shared-code changes) into a trace.
# ---------------------------------------------------------------------------
def _strip(line: str) -> str:
    return line.split("] ", 1)[1].strip() if "] " in line else line.strip()


def _transcript(log_text: str, demo: dict, results: dict) -> dict:
    """Pull the orchestrator directive and a few judge assessments from the +intent seed-1 run."""
    key = demo["key"]
    block = ""
    if f"@@@ {key} | intent @@@" in log_text:
        block = log_text.split(f"@@@ {key} | intent @@@", 1)[1].split("@@@", 1)[0]
    seed1 = ("episode started" + block.split("episode started", 2)[1]) if "episode started" in block else block
    lines = seed1.splitlines()

    directive = next((_strip(ln) for ln in lines if "[Orchestrator]" in ln
                      and any(t in ln for t in ("scheduled", "policy(", "delegated", "no-op"))), "")
    judge = [_strip(ln) for ln in lines if "[Non-RT]" in ln]
    # a compact, representative slice: first, middle, last assessment
    if len(judge) >= 3:
        judge = [judge[0], judge[len(judge) // 2], judge[-1]]

    b, i = results["baseline"]["agg"], results["intent"]["agg"]
    outcome = {k: {"baseline": b[k], "intent": i[k], "delta": round(i[k] - b[k], 4)} for k, _ in _METRICS}
    return {"demo": key, "scenario": demo["scenario"], "lever": demo["blurb"],
            "intent": demo["intent"], "directive": directive, "judge": judge, "outcome": outcome}


def _boxed(t: dict) -> str:
    W = 92
    L = ["=" * W, f"END-TO-END INTERACTION   demo={t['demo']}  ({t['scenario']})   {t['lever']}", "=" * W,
         "OPERATOR INTENT", f"  {t['intent']}", "",
         "ORCHESTRATOR  (directive + reasoning)", f"  {t['directive'] or '<none captured>'}", "",
         "NON-RT JUDGE  (assessments: first / mid / last)"]
    L += [f"  - {j}" for j in t["judge"]] or ["  <none captured>"]
    L += ["", "OUTCOME   baseline -> +intent"]
    for k, lbl in _METRICS:
        o = t["outcome"][k]
        L.append(f"  {lbl:10s} {o['baseline']:.3f} -> {o['intent']:.3f}   ({o['delta']:+.3f})")
    L.append("=" * W)
    return "\n".join(L)


async def _main(args, log_path: Path | None):
    print(f"[demo] Starting MCP server on {MCP_HOST}:{MCP_PORT} ...")
    server_task = asyncio.create_task(
        mcp.run_http_async(host=MCP_HOST, port=MCP_PORT, show_banner=False, log_level="warning"))
    await asyncio.sleep(1.5)
    model = resolve_model(_MODEL_SLUG)
    all_results = {}
    try:
        for demo in DEMOS:
            all_results[demo["key"]] = await _run_demo(model, demo, args.seeds)
    finally:
        logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass

    # Build + save one file per demo (structured trace + boxed transcript + metrics).
    if log_path and log_path.exists():
        sys.stdout.flush()
        log_text = log_path.read_text()
        for demo in DEMOS:
            t = _transcript(log_text, demo, all_results[demo["key"]])
            box = _boxed(t)
            print("\n" + box)
            if args.save:
                out = _EXP_DIR / f"exp6_demo_{demo['key']}.json"
                out.write_text(json.dumps({**t, "trace_text": box,
                                           "results": all_results[demo["key"]]}, indent=2))
                print(f"  saved -> {out}")
    elif args.save:
        print("[demo] WARNING no log captured, so no transcript was built (run with --log).")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Exp 6 Fold 1 — end-to-end intent demos (reserve + posture)")
    p.add_argument("--seeds", type=int, default=3)
    p.add_argument("--save", action="store_true")
    p.add_argument("--log", nargs="?", const="AUTO", default=None)
    args = p.parse_args()

    # A log is needed to build the transcript, so force one on when --save.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.log is None and args.save:
        args.log = "AUTO"
    log_path = None
    _logfile = None
    if args.log is not None:
        log_path = (_LOGS_DIR / f"log_demo_{stamp}.txt" if args.log == "AUTO" else Path(args.log))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _logfile = open(log_path, "w")
        sys.stdout = _Tee(sys.__stdout__, _logfile); sys.stderr = _Tee(sys.__stderr__, _logfile)
        print(f"[demo] logging to {log_path}")

    _prevent_sleep()
    try:
        asyncio.run(_main(args, log_path))
    finally:
        if _logfile is not None:
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
            _logfile.close()
