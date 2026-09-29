"""
Experiment 8 — multi-site orchestration: routing and conflicting operator intents across a fleet.

Experiments 1-7 run ONE site. The framework section claims the SMO orchestrator decomposes one
operator intent into heterogeneous PER-SITE directives over A2A — raising a reserve only on the cells
that serve a scheduled event while a fleet-wide cost posture applies everywhere. This experiment makes
that concrete: THREE sites run CONCURRENTLY, each with its OWN simulator, compute pool (no sharing
between sites), Non-RT judge (its own MCP server on its own port) and fast loop, and a single fleet
orchestrator routes free-text operator intents to the right site(s) and resolves clashes.

Fleet (each site independent, own pool):
  stadium     — high-density small cell at a stadium; botnet storm + a legitimate crowd surge (the
                Exp-1/2 botnet_event), its own contention pool. The site that must be PROTECTED.
  residential — macro cell, a gentle benign demand ramp, no scheduled events.
  business    — small cell, bursty but benign daytime load.

Injected operator intents (demonstrating routing + the conflict precedence in fleet_orchestrator):
  1. fleet-wide COST posture              -> every site leans cost.
  2. stadium-only QoS + "legitimate"      -> scope precedence: the stadium OVERRIDES the fleet cost
                                             (local beats global) and is told the surge is genuine;
                                             residential/business stay lean.
  3. a hard floor at the business cell     -> routed to ONE named site only (no collateral).

Output: the orchestrator's routing decision per intent (which sites got what, misroutes, conflicts),
and per-site resilience P / benign served / avg servers at the end, showing the fleet-wide posture and
the local override landed on the right sites.

Usage (source the shell env for the OpenRouter key first):
    python -m scripts.exp_8_multisite --seeds 1 --save --log
    python -m scripts.exp_8_multisite --duration 260 --seeds 1     # cap wall time
"""

import argparse
import asyncio
import json
import logging
import math
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.run import resolve_model
from scripts.exp_1_model_comparison import _Tee, _prevent_sleep, judge_settings
from mcp_server.server import build_mcp_server, MCP_HOST
from agents.non_rt_agent import build_non_rt_agent, compose_system_prompt, run_assessment_loop
from agents.near_rt_control_loop import run_control_loop
from agents.orchestrator import build_orchestrator_agent  # noqa: F401  (kept for parity/imports)
from agents.fleet_orchestrator import (SiteContext, build_fleet_orchestrator_agent, route_fleet_intent)
from shared.policy import SharedPolicy, RunStats
from runtime import SimHost, UP, LLM_COMPARE
from sim.metrics import (resilience_multi, benign_success_rate, benign_false_positive_rate,
                         malicious_blocked_rate, avg_servers)

_EXP_DIR  = Path(__file__).parent.parent / "experiments" / "exp8_multisite"
_LOGS_DIR = _EXP_DIR / "logs"
_C_MAX    = 16

_SYS_PROMPT = (Path(__file__).parent.parent / "prompts" / "non_rt_agent_system_prompt.md").read_text()

# The orchestrator model: the reasoning ceiling (gpt-5.4-mini, thinking on), matching Exp 6's grounding.
_ORCH_SLUG = "openrouter:openai/gpt-5.4-mini"
_ORCH_MODE = "on"

# --- the fleet: (id, scenario, contention kappa/a, benign surge peak + tail, registry description) --
# storm = benign surge PEAK (UEs/s); kept small (~2.7x the 20 UEs/s baseline) so a lean posture can
# still serve it and the judge does not misread it as a storm. t_post trims the idle tail so all three
# sites finish near the stadium's 420s. Stadium (botnet_event) ignores storm/t_post (fixed scenario).
_SITES = [
    dict(id="stadium", scenario=LLM_COMPARE, kappa=float(_C_MAX), a=1.0, storm=None, t_post=None,
         description=("high-density small cell at a stadium, serves event egress (e.g. Wembley); "
                      "exposed to both botnet storms and legitimate crowd surges")),
    dict(id="residential", scenario="single_ramp", kappa=None, a=0.5, storm=55.0, t_post=310.0,
         description="macro cell over a residential district; gentle benign demand, no scheduled events"),
    dict(id="business", scenario="single_storm", kappa=None, a=0.5, storm=55.0, t_post=310.0,
         description="small cell in the business district; bursty but benign daytime load"),
]

# --- injected operator intents: (wall-clock delay s, free-text intent) -------------------------------
_INTENTS = [
    (15.0, "It's a quiet Tuesday night — cut cost across the whole fleet."),
    (35.0, "Actually, the stadium has a sold-out concert finishing tonight. Spare no capacity there "
           "and treat the surge as legitimate demand, not an attack."),
    (55.0, "Keep at least 3 servers warm at the business district cell."),
]


class Site:
    """One live fleet member: its own SimHost, policy, judge, MCP server and metrics."""

    def __init__(self, spec: dict, model, seed: int, rt_factor: float, port: int):
        self.id          = spec["id"]
        self.port        = port
        self.scenario    = spec["scenario"]
        self.kappa       = spec["kappa"]
        self.a           = spec["a"]
        self.storm       = spec.get("storm")
        self.t_post      = spec.get("t_post")
        self.description = spec["description"]
        self.seed        = seed
        self.rt_factor   = rt_factor

        self.host   = SimHost()
        self.policy = SharedPolicy()
        self.stats  = RunStats()
        self.host.forecast_enabled = True
        self.host.calendar_enabled = True
        # a per-site MCP server bound to THIS host, and a judge that reads it over THIS site's port
        self.mcp_server = build_mcp_server(self.host, name=f"StormSim MCP [{self.id}]")
        self.mcp_url    = f"http://{MCP_HOST}:{self.port}/mcp"
        self.agent = build_non_rt_agent(
            model,
            system_prompt=compose_system_prompt(_SYS_PROMPT, calendar_enabled=True, forecast_enabled=True),
            mcp_url=self.mcp_url)

    def start_sim(self) -> None:
        msg = self.host.start(scenario=self.scenario, seed=self.seed, c_max=_C_MAX,
                              rt_factor=self.rt_factor, compute_kappa=self.kappa, compute_slowdown=self.a,
                              storm=self.storm, t_post=self.t_post)
        print(f"[{self.id}] {msg}")

    def ctx(self) -> SiteContext:
        return SiteContext(self.id, self.description, self.policy, self.host)

    def metrics(self) -> dict:
        sim = self.host.sim
        if sim is None or not sim.telemetry:
            return {"P": 0.0, "benign": 0.0, "benign_fp": 0.0, "blocked": 0.0, "servers": 0.0}
        storms = sim.cfg.traffic.storm_windows()
        try:
            P = resilience_multi(sim.telemetry, sim.mu_single, UP, storms)["P_episode"]
        except Exception:
            P = 0.0
        st = sim.stats
        return {"P": round(P, 4),
                "benign": benign_success_rate(st), "benign_fp": benign_false_positive_rate(st),
                "blocked": malicious_blocked_rate(st), "servers": round(avg_servers(sim.telemetry), 2)}


# Student-t 97.5th percentile by SAMPLE SIZE n (df=n-1); 1.96 fallback. Matches Exp 1/2/5/7.
_T95 = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571,
        7: 2.447, 8: 2.365, 9: 2.306, 10: 2.262}

# expected routing per injected intent (index-aligned to _INTENTS), used to SCORE routing correctness:
#   (label, expected target-site set, per-site predicate the winning directive must satisfy)
_EXPECTED = [
    ("fleet cost -> all sites cost", {"stadium", "residential", "business"},
     lambda d: d["priority"] == "cost"),
    ("stadium QoS override -> stadium only", {"stadium"},
     lambda d: d["priority"] == "qos"),
    ("business floor -> business only, floor=3", {"business"},
     lambda d: d["min_servers"] == 3),
]


def _routing_ok(routing_log: list[dict]) -> list[tuple[str, bool]]:
    """Score each intent's routing against _EXPECTED: the directive must hit EXACTLY the expected
    sites (no collateral, no misroute) and the winning directive must satisfy the predicate."""
    checks = []
    for i, (label, want_sites, pred) in enumerate(_EXPECTED):
        r = routing_log[i] if i < len(routing_log) else None
        if r is None:
            checks.append((label, False)); continue
        got_sites = {d["site_id"] for d in r["directives"]}
        ok = (got_sites == want_sites) and (not r["misrouted"]) and all(pred(d) for d in r["directives"])
        checks.append((label, bool(ok)))
    return checks


async def run_one_seed(seed, args, model, judge_set, orch_model, orch_set) -> dict:
    """One full concurrent 3-site episode at `seed`: per-site MCP server + judge + fast loop, the fleet
    orchestrator, and the scripted operator intents. Each seed uses its own port block so a previous
    seed's server (in TIME_WAIT) never blocks the next."""
    port_base = 8000 + (seed - 1) * 10
    sites = [Site(spec, model, seed, args.rt_factor, port=port_base + i) for i, spec in enumerate(_SITES)]
    fleet = {s.id: s.ctx() for s in sites}
    orchestrator = build_fleet_orchestrator_agent(orch_model, [s.ctx() for s in sites])
    orch_stats = RunStats()

    # --- start the per-site MCP servers, then the sims ---
    server_tasks = [asyncio.create_task(
        s.mcp_server.run_http_async(host=MCP_HOST, port=s.port, show_banner=False, log_level="warning"))
        for s in sites]
    await asyncio.sleep(1.5)     # let the servers bind before the judges connect
    for s in sites:
        s.start_sim()

    stop_event = asyncio.Event()
    routing_log: list[dict] = []

    async def _watch():
        t0 = time.monotonic()
        while not all(s.host.is_done for s in sites):
            if time.monotonic() - t0 > args.duration:
                print(f"[fleet] duration cap {args.duration}s reached — stopping")
                break
            await asyncio.sleep(0.5)
        stop_event.set()
        print(f"[fleet] seed {seed}: all episodes complete — signalling loops to stop.")

    async def _inject():
        for delay, text in _INTENTS:
            wait = (_t_start + delay) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            if stop_event.is_set():
                break
            summary, fd, misrouted = await route_fleet_intent(
                text, orchestrator, fleet, orch_stats, settings=orch_set)
            routing_log.append({
                "t_wall": round(time.monotonic() - _t_start, 1), "intent": text,
                "reasoning": fd.reasoning, "conflict": fd.conflict, "misrouted": misrouted,
                "directives": [{"site_id": sd.site_id, "priority": sd.priority,
                                "V": sd.lyapunov_V, "W": sd.lyapunov_W, "min_servers": sd.min_servers,
                                "schedule": sd.schedule_event_name, "nonrt": bool(sd.nonrt_instruction)}
                               for sd in fd.site_directives],
                "summary": summary})

    _t_start = time.monotonic()
    tasks = [asyncio.create_task(_watch()), asyncio.create_task(_inject())]
    for s in sites:
        tasks.append(asyncio.create_task(run_control_loop(s.policy, stop_event, 1.0, s.stats, host=s.host)))
        tasks.append(asyncio.create_task(run_assessment_loop(
            s.agent, s.policy, stop_event, args.assessment_interval, s.stats,
            window_s=args.window_s, model_settings=judge_set, host=s.host)))
    await asyncio.gather(*tasks)

    # tear down the MCP servers (silence uvicorn's lifespan CancelledError traceback on shutdown)
    logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
    for t in server_tasks:
        t.cancel()
    for t in server_tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass

    return {
        "seed": seed,
        "sites": {s.id: {"scenario": s.scenario, "kappa": s.kappa, "a": s.a,
                         "description": s.description, "metrics": s.metrics(),
                         "final_posture": s.policy.context_str()} for s in sites},
        "routing": routing_log,
        "routing_ok": _routing_ok(routing_log),
    }


def _aggregate(per_seed: list[dict]) -> dict:
    """Per-site mean / 95% CI (Student-t) over seeds, plus routing-correctness counts per intent."""
    site_ids = list(per_seed[0]["sites"].keys())
    sites = {}
    for sid in site_ids:
        sites[sid] = {"scenario": per_seed[0]["sites"][sid]["scenario"]}
        for k in ["P", "benign", "benign_fp", "blocked", "servers"]:
            vals = [ps["sites"][sid]["metrics"][k] for ps in per_seed]
            n = len(vals); m = statistics.mean(vals)
            sd = statistics.stdev(vals) if n > 1 else 0.0
            ci = _T95.get(n, 1.96) * sd / math.sqrt(n) if n > 1 else 0.0
            sites[sid][k] = {"mean": round(m, 4), "ci": round(ci, 4), "seeds": vals}
    # routing correctness: per intent, how many seeds routed it correctly
    labels = [lbl for lbl, _, _ in _EXPECTED]
    routing = []
    for i, lbl in enumerate(labels):
        correct = sum(1 for ps in per_seed if ps["routing_ok"][i][1])
        routing.append({"intent": lbl, "correct": correct, "n": len(per_seed)})
    return {"seeds": [ps["seed"] for ps in per_seed], "sites": sites, "routing_accuracy": routing,
            "routing_first": per_seed[0]["routing"], "per_seed": per_seed}


def _report(agg: dict) -> None:
    print("\n" + "=" * 96)
    print(f"MULTI-SITE ORCHESTRATION — routing & conflict resolution  ({len(agg['seeds'])} seeds)")
    print("=" * 96)
    print("\n  Representative routing (seed 1):")
    for r in agg["routing_first"]:
        print(f"\n  t={r['t_wall']:>5.1f}s  INTENT: {r['intent']}")
        for d in r["directives"]:
            lv = f"V={d['V']}" if d["V"] is not None else ""
            fl = f" floor={d['min_servers']}" if d["min_servers"] else ""
            sc = f" sched='{d['schedule']}'" if d["schedule"] else ""
            dl = " +delegation" if d["nonrt"] else ""
            print(f"      -> {d['site_id']:12s} priority={d['priority']:8s} {lv}{fl}{sc}{dl}")
        if r["misrouted"]:
            print(f"      !! MISROUTED to unknown sites: {r['misrouted']}")
        if r["conflict"]:
            print(f"      !! CONFLICT (escalated): {r['conflict']}")
    print("\n  Routing correctness across seeds:")
    for r in agg["routing_accuracy"]:
        print(f"      {r['correct']}/{r['n']}   {r['intent']}")
    print("\n" + "-" * 96)
    print(f"  {'site':12s} {'scenario':14s} {'P':>14s} {'benign':>14s} {'benign_fp':>12s} "
          f"{'blocked':>8s} {'servers':>12s}")
    for sid, s in agg["sites"].items():
        print(f"  {sid:12s} {s['scenario']:14s} "
              f"{s['P']['mean']:.3f}±{s['P']['ci']:.3f}  "
              f"{s['benign']['mean']:.3f}±{s['benign']['ci']:.3f}  "
              f"{s['benign_fp']['mean']:.3f}±{s['benign_fp']['ci']:.3f}  "
              f"{s['blocked']['mean']:>6.3f}  "
              f"{s['servers']['mean']:.2f}±{s['servers']['ci']:.2f}")
    print("=" * 96)


async def sweep(args) -> dict:
    judge_slug = "openrouter:google/gemini-3.1-flash-lite"           # the deployable judge on every site
    judge_set  = judge_settings(judge_slug, "n/a")
    model      = resolve_model(judge_slug)
    orch_model = resolve_model(_ORCH_SLUG)                           # reasoning model for the orchestrator
    orch_set   = judge_settings(_ORCH_SLUG, _ORCH_MODE)

    per_seed = []
    for seed in range(1, args.seeds + 1):
        print(f"\n############  SEED {seed}/{args.seeds}  ############")
        per_seed.append(await run_one_seed(seed, args, model, judge_set, orch_model, orch_set))

    agg = _aggregate(per_seed)
    _report(agg)
    if args.save:
        _EXP_DIR.mkdir(parents=True, exist_ok=True)
        args.ckpt_path.write_text(json.dumps(agg, indent=2))
        print(f"\n  saved -> {args.ckpt_path}")
    return agg


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Experiment 8 — multi-site orchestration (routing + conflict)")
    p.add_argument("--seeds", type=int, default=5, help="number of seeds to sweep (each run sequentially)")
    p.add_argument("--rt-factor", type=float, default=1.0, dest="rt_factor")
    p.add_argument("--assessment-interval", type=float, default=5.0, dest="assessment_interval")
    p.add_argument("--window", type=float, default=15.0, dest="window_s")
    p.add_argument("--duration", type=float, default=440.0,
                   help="per-seed wall-clock cap (s); 440 lets all three ~420s episodes finish")
    p.add_argument("--save", action="store_true",
                   help="write results to experiments/exp8_multisite/multisite_<timestamp>.json")
    p.add_argument("--log", nargs="?", const="AUTO", default=None,
                   help="tee output to a file (bare --log auto-names it under exp8_multisite/logs/)")
    args = p.parse_args()

    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    args.ckpt_path = _EXP_DIR / f"multisite_{run_stamp}.json"

    _logfile = None
    if args.log is not None:
        log_path = (_LOGS_DIR / f"log_multisite_{run_stamp}.txt" if args.log == "AUTO" else Path(args.log))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _logfile = open(log_path, "w")
        sys.stdout = _Tee(sys.__stdout__, _logfile)
        sys.stderr = _Tee(sys.__stderr__, _logfile)
        print(f"[fleet] logging this run to {log_path}")

    _prevent_sleep()

    try:
        asyncio.run(sweep(args))
    finally:
        if _logfile is not None:
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
            _logfile.close()
