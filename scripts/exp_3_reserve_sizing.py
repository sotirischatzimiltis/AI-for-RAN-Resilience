"""
Experiment 3 — reserve sizing: does the Non-RT LLM ESTIMATE a scheduled crowd better than a rule,
and does it GENERALISE across a whole portfolio of real events (not just the one from Exp 1)?

The one place a language model should beat a hardcoded controller: reading a free-text calendar
entry and REASONING to the crowd it implies. Four arms size the pre-provisioning reserve for each
of the 12 real events in docs/event_portfolio.md:
  • Flat rule      — "an event is scheduled" -> reserve a fixed number (default 10). No estimate.
  • Formula rule   — given the STRUCTURED fields {capacity, sold_out}: sold_out -> capacity, else a
                     fixed fraction x capacity (the honest strong baseline, parsing conceded). Its
                     implied attendance is scored too, so it appears on the estimate figure.
  • LLM (gemini)   — deployable operating point; the REAL Non-RT agent reads the FREE-TEXT line.
  • LLM (gpt-5.4-mini, reasoning on) — the ceiling.
Each event gets ONE committed estimate per episode, over N seeds. Every seed is an independent
episode that runs the deployed judge's short assessment LOOP until it COMMITS a reserve, and we read
the PEAK committed crowd/reserve exactly as Exp 1 does (stats.judge_peak_*) — so a model that lapses
to "no action" on one cycle still engages on a later one. The per-seed committed estimates carry a
95% CI; a seed where it never commits is a non-engagement (est=0), kept OUT of the estimate mean and
reported as an engage rate. The reserve each estimate derives is simulated at that seed as a benign
surge (ground-truth attendance), scoring benign QoS (penalises under-reserve) and servers used
(penalises over-reserve). Split by sold-out vs not: the arms TIE on sold-out (the flag suffices) and
SEPARATE on the rest (context reasoning needed).

Usage (rules only, no API — check the design):
    python -m scripts.exp_3_reserve_sizing
With the LLM arms (needs the OpenRouter key; both models, 5 seeds):
    python -m scripts.exp_3_reserve_sizing --llm --seeds 5 --save
"""

import argparse
import asyncio
import json
import logging
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.server import mcp, MCP_HOST, MCP_PORT
from scripts.run import resolve_model
from scripts.exp_1_model_comparison import _Tee, _prevent_sleep, judge_settings  # logging + no-sleep + LLM settings
from agents.non_rt_agent import build_non_rt_agent, compose_system_prompt, _do_assessment
from shared.events import PORTFOLIO, reserve_for
from shared.policy import SharedPolicy, RunStats
from shared.event_calendar import ScheduledEvent
from sim.config import (SimConfig, open_ran_arch, RRCConfig, event_surge_traffic,
                        TrafficConfig, TrafficPhase)
from sim.simulator import StormSim
from sim.controllers import FixedController
from sim.metrics import benign_success_rate, avg_servers
from runtime import host as sim_host

C_MAX = 16
MU    = open_ran_arch().service_rate()      # per-server service rate the reserve math uses
_STORM_PROMPT = (Path(__file__).parent.parent / "prompts" / "non_rt_agent_system_prompt.md").read_text()
_EXP_DIR = Path(__file__).parent.parent / "experiments" / "exp3_reserve_sizing"

# The two judges carried from Experiment 1 (label, slug, reasoning-mode for judge_settings):
# gemini-3.1-flash-lite is the deployable operating point, gpt-5.4-mini (reasoning on) the ceiling.
ROSTER = [
    ("gemini",       "openrouter:google/gemini-3.1-flash-lite", "n/a"),
    ("gpt-5.4-mini", "openrouter:openai/gpt-5.4-mini",          "on"),
]

# t-critical (two-sided 95%) for small n; falls back to the normal 1.96 for n>10.
_TCRIT = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571,
          7: 2.447, 8: 2.365, 9: 2.306, 10: 2.262}


def _mean_ci(xs) -> tuple[float, float]:
    """Mean and Student-t 95% half-width of a sample. CI is 0 for a single (deterministic) value."""
    xs = [float(x) for x in xs]
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    m = statistics.mean(xs)
    if n == 1:
        return m, 0.0
    sd = statistics.stdev(xs)
    return m, _TCRIT.get(n, 1.96) * sd / math.sqrt(n)


# ------------------------------- the rule arms (attendance -> reserve) --------------------------
def flat_reserve(event, args) -> int:
    return min(args.flat, C_MAX)                          # context-blind floor


def formula_reserve(event, fraction: float) -> int:
    att = event.capacity if event.sold_out else fraction * event.capacity   # sold-out -> full, else fixed fraction
    return reserve_for(att, MU, C_MAX)


def formula_attendance(event, fraction: float) -> int:
    """The attendance the formula rule IMPLIES (so it can sit on the estimate figure too)."""
    return int(round(event.capacity if event.sold_out else fraction * event.capacity))


def best_fraction(events) -> tuple[float, float]:
    """The single fixed fraction minimising mean |reserve error| over the NOT-sold-out events —
    the strongest the formula rule can be. (In-sample for now; a calibration/eval split is the
    follow-up.) Returns (fraction, that mean error)."""
    ns = [e for e in events if not e.sold_out]
    best_f, best_err, f = 0.5, 1e9, 0.20
    while f <= 1.0001:
        err = statistics.mean(abs(formula_reserve(e, f) - e.ideal_reserve(MU, C_MAX)) for e in ns)
        if err < best_err:
            best_err, best_f = err, round(f, 2)
        f += 0.05
    return best_f, best_err


# ------------------------------- LLM arm: the REAL agent via get_calendar ------------------------
async def run_llm_model(events, slug, mode, calm_contexts, max_cycles):
    """For each event, ONE committed estimate per episode (per seed), read the way the deployed judge
    is measured in Exp 1: run a short assessment LOOP and take the PEAK committed crowd/reserve
    (`stats.judge_peak_*`). A weak model that lapses to "no action" on a cycle still gets to engage on
    a later one; we stop the moment it commits. A seed where it never commits records est=0 (a genuine
    non-engagement), kept out of the estimate mean. Anticipation tools (calendar + forecast) match
    Exp 1. Returns a per-event dict with per-seed estimates + reserves and one reasoning sample."""
    agent = build_non_rt_agent(
        resolve_model(slug),
        system_prompt=compose_system_prompt(_STORM_PROMPT, calendar_enabled=True, forecast_enabled=True))
    settings = judge_settings(slug, mode)
    out = []
    for e in events:
        ests, ress, reason = [], [], ""
        for calm, t_now in calm_contexts:        # one INDEPENDENT episode per seed
            sim_host.sim = calm
            sim_host.calendar = [ScheduledEvent(t_now + 30.0, e.name, "high", e.venue, e.sold_out)]
            sim_host.calendar_committed = set()   # fresh episode -> uncommitted
            policy, stats = SharedPolicy(), RunStats()
            for cyc in range(1, max_cycles + 1):  # loop until it commits a reserve (peak captured in stats)
                v = await _do_assessment(agent, policy, cyc, stats, window_s=15.0, model_settings=settings)
                if v is not None and v.expected_attendance > 0 and not reason:
                    reason = v.attendance_reasoning or v.reasoning or ""
                if stats.judge_peak_attendance > 0:
                    break
            ests.append(stats.judge_peak_attendance)                        # 0 = never engaged this seed
            ress.append(stats.judge_peak_reserve if stats.judge_peak_reserve > 0 else 1)
        eng = [x for x in ests if x > 0]
        em, eci = _mean_ci(eng) if eng else (0.0, 0.0)
        print(f"[{slug.split('/')[-1]:22s}] {e.name[:36]:36s} est~{em:>7.0f}±{eci:<5.0f} "
              f"(eng {len(eng)}/{len(ests)}, true {e.attendance:>6d}) -> reserve "
              f"{round(statistics.mean(ress))} (ideal {e.ideal_reserve(MU, C_MAX)})")
        out.append({"est": ests, "reserve": ress, "reasoning": reason})
    return out


# ------------------------------- simulate one event at a given reserve+seed ----------------------
def simulate_one(event, reserve: int, seed: int) -> tuple[float, float]:
    """Play the event as a benign surge with `reserve` servers pre-provisioned at one seed; return
    (benign completion, avg_servers)."""
    reserve = max(1, min(reserve, C_MAX))
    cfg = SimConfig(arch=open_ran_arch(), rrc=RRCConfig(t300_ms=1000, max_attempts=5),
                    c0=reserve, c_max=C_MAX, traffic=event_surge_traffic(event.surge_peak()), seed=seed)
    sim = StormSim(cfg)
    sim.run(controller=FixedController(reserve))
    return benign_success_rate(sim.stats), avg_servers(sim.telemetry)


# ------------------------------------------ scoring --------------------------------------------
def _arm_stats(ests, reserves, sims, ideal, true, reasoning=None) -> dict:
    """One arm's numbers for one event. ests=None for the flat rule (no attendance estimate);
    a list otherwise. reserves is one value for the rules, one-per-seed for the LLMs. sims is one
    (benign, servers) per seed."""
    res_m, res_ci = _mean_ci(reserves)
    ben_m, ben_ci = _mean_ci([b for b, _ in sims])
    srv_m, _      = _mean_ci([s for _, s in sims])
    d = {"reserve": round(res_m, 2), "reserve_ci": round(res_ci, 2),
         "reserve_err": round(res_m - ideal, 2),
         "benign": round(ben_m, 4), "benign_ci": round(ben_ci, 4),
         "servers": round(srv_m, 2)}
    if ests is None:
        d.update(est=None, est_ci=None, att_err_pct=None, engaged=None, n=len(reserves))
    else:
        eng = [x for x in ests if x > 0]                 # zeros = seeds where the judge never engaged
        if eng:
            e_m, e_ci = _mean_ci(eng)
            d.update(est=round(e_m), est_ci=round(e_ci),
                     att_err_pct=round(100 * (e_m - true) / true, 1) if true else None)
        else:
            d.update(est=0, est_ci=0, att_err_pct=None)
        d.update(engaged=len(eng), n=len(ests),
                 raw_est=[int(x) for x in ests], raw_reserve=[int(x) for x in reserves])
    if reasoning is not None:
        d["reasoning"] = reasoning
    return d


def build_records(events, rule_reserves, fraction, llm_by_label, seeds):
    records = []
    for i, e in enumerate(events):
        ideal = e.ideal_reserve(MU, C_MAX)
        rec = {"name": e.name, "venue": e.venue, "capacity": e.capacity, "sold_out": e.sold_out,
               "attendance": e.attendance, "ideal": ideal, "arms": {}}
        # rule arms: deterministic reserve, but QoS still varies over the seeds
        for arm in ("flat", "formula"):
            res  = rule_reserves[arm][i]
            ests = None if arm == "flat" else [formula_attendance(e, fraction)]
            sims = [simulate_one(e, res, s) for s in seeds]
            rec["arms"][arm] = _arm_stats(ests, [res], sims, ideal, e.attendance)
        # LLM arms: one estimate+reserve per seed/episode; simulate each seed's reserve AT that seed
        for label, trials in llm_by_label.items():
            t = trials[i]
            sims = [simulate_one(e, t["reserve"][k], seeds[k]) for k in range(len(seeds))]
            rec["arms"][label] = _arm_stats(t["est"], t["reserve"], sims, ideal, e.attendance, t["reasoning"])
        records.append(rec)
    return records


def aggregate(records, arms) -> dict:
    """Per arm, mean |attendance error| %, mean |reserve error|, benign QoS, servers — split by
    sold-out (the flag suffices) vs NOT sold-out (context reasoning needed) vs all."""
    groups = [("sold", lambda r: r["sold_out"]),
              ("not_sold", lambda r: not r["sold_out"]),
              ("all", lambda r: True)]
    agg = {}
    for a in arms:
        agg[a] = {}
        for grp, sel in groups:
            sub = [r["arms"][a] for r in records if sel(r)]
            if not sub:                                   # e.g. a single-event subset with no sold-out row
                agg[a][grp] = {"att_err_abs": None, "reserve_err_abs": None, "benign": None, "servers": None}
                continue
            att = [abs(x["att_err_pct"]) for x in sub if x["att_err_pct"] is not None]
            agg[a][grp] = {
                "att_err_abs":     round(statistics.mean(att), 1) if att else None,
                "reserve_err_abs": round(statistics.mean(abs(x["reserve_err"]) for x in sub), 2),
                "benign":          round(statistics.mean(x["benign"] for x in sub), 3),
                "servers":         round(statistics.mean(x["servers"] for x in sub), 2),
            }
    return agg


# ------------------------------------------ report ---------------------------------------------
def _report(records, arms, agg):
    print("\n" + "=" * 108)
    print("RESERVE SIZING — per-event attendance estimate (est vs true) and reserve (ideal vs arm)")
    print("=" * 108)
    print(f"  {'event':40s} {'sold':>4s} {'true':>7s} {'ideal':>5s} "
          + " ".join(f"{a[:10]:>12s}" for a in arms))
    for r in records:
        cells = []
        for a in arms:
            x = r["arms"][a]
            cells.append(f"{'--' if x['est'] is None else x['est']:>6}/{x['reserve']:>4.1f}")
        print(f"  {r['name'][:40]:40s} {('Y' if r['sold_out'] else 'n'):>4s} "
              f"{r['attendance']:>7d} {r['ideal']:>5d} " + " ".join(f"{c:>12s}" for c in cells))
    print("  (cell = estimated attendance / mean reserve)")
    print("=" * 108)
    print("AGGREGATE — mean |att err| %, mean |reserve err|, benign QoS, servers (sold-out vs not)")
    print("=" * 108)
    def _f(v, p):                                        # format a possibly-None number
        return "--" if v is None else f"{v:.{p}f}"
    for grp in ("sold", "not_sold", "all"):
        if all(agg[a][grp]["reserve_err_abs"] is None for a in arms):
            continue                                     # no events in this group (e.g. single-event run)
        print(f"\n  --- {grp} ---")
        print(f"  {'arm':16s} {'|att err|%':>10s} {'|res err|':>10s} {'benign':>8s} {'servers':>8s}")
        for a in arms:
            g = agg[a][grp]
            print(f"  {a:16s} {_f(g['att_err_abs'],1):>10s} {_f(g['reserve_err_abs'],2):>10s} "
                  f"{_f(g['benign'],3):>8s} {_f(g['servers'],2):>8s}")
    print("=" * 108)


# ------------------------------------------ main -----------------------------------------------
def _build_calm(seed: int):
    """One independent calm episode (60 s baseline) at `seed`; returns (sim, t_now)."""
    calm = StormSim(SimConfig(arch=open_ran_arch(), rrc=RRCConfig(t300_ms=1000, max_attempts=5),
                              c0=2, c_max=C_MAX, seed=seed,
                              traffic=TrafficConfig(baseline_rate=20.0,
                                                    phases=[TrafficPhase(0.0, 60.0, 20.0, 0.0, "calm")])))
    calm.run(controller=FixedController(2))
    return calm, calm.telemetry[-1].t


async def main(args):
    if args.list_events:
        print("Event portfolio (index | ideal reserve | sold-out | name @ venue):")
        for i, e in enumerate(PORTFOLIO):
            print(f"  {i:>2d} | {e.ideal_reserve(MU, C_MAX):>2d} | {'Y' if e.sold_out else 'n'} | {e.name} @ {e.venue}")
        return

    seeds  = list(range(1, args.seeds + 1))
    frac, frac_err = best_fraction(PORTFOLIO)
    events = [PORTFOLIO[args.event]] if args.event is not None else PORTFOLIO
    fraction = args.fraction if args.fraction is not None else frac
    print(f"[exp3] mu={MU:.2f}  seeds={args.seeds} (one estimate per event per episode)  "
          f"formula fraction={fraction:.2f} (best-fit non-sold-out, mean |err|={frac_err:.2f})")

    rule_reserves = {"flat":    [flat_reserve(e, args) for e in events],
                     "formula": [formula_reserve(e, fraction) for e in events]}
    arms = ["flat", "formula"]
    llm_by_label = {}

    if args.llm:
        roster = [m for m in ROSTER if args.models is None or m[0] in args.models]
        print(f"[exp3] starting MCP server on {MCP_HOST}:{MCP_PORT} for the LLM arms "
              f"({', '.join(m[0] for m in roster)}) ...")
        server_task = asyncio.create_task(
            mcp.run_http_async(host=MCP_HOST, port=MCP_PORT, show_banner=False, log_level="warning"))
        await asyncio.sleep(1.5)
        try:
            calm_contexts = [_build_calm(s) for s in seeds]   # one independent episode per seed
            sim_host.calendar_enabled = True
            sim_host.forecast_enabled = True                  # anticipation tools match Exp 1
            for label, slug, mode in roster:
                print(f"\n[exp3] === {label}  (slug={slug}, reasoning={mode}) ===")
                llm_by_label[label] = await run_llm_model(events, slug, mode, calm_contexts, args.cycles)
                arms.append(label)
        finally:
            logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass

    records = build_records(events, rule_reserves, fraction, llm_by_label, seeds)
    agg = aggregate(records, arms)
    _report(records, arms, agg)

    if any(llm_by_label):                                    # echo the agent's reasoning per event
        print("\nLLM REASONING (one sample per event):")
        for r in records:
            for label in llm_by_label:
                x = r["arms"][label]
                print(f"  [{label:12s}] {r['name'][:44]:44s} est~{x['est']} -> {x['reserve']:.1f} | {x.get('reasoning','')}")

    if args.save:
        _EXP_DIR.mkdir(parents=True, exist_ok=True)
        out = _EXP_DIR / "reserve_sizing.json"
        out.write_text(json.dumps({
            "mu": MU, "c_max": C_MAX, "fraction": fraction, "seeds": seeds,
            "arms": arms,
            "arm_meta": {label: {"slug": slug, "mode": mode} for label, slug, mode in ROSTER if label in arms},
            "records": records, "aggregate": agg,
        }, indent=2))
        print(f"\n  saved -> {out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Experiment 3 — reserve sizing / attendance estimation: rules vs LLM")
    p.add_argument("--llm", action="store_true", help="include the LLM arms (real Non-RT agent; needs the OpenRouter key)")
    p.add_argument("--models", nargs="+", default=None, metavar="LABEL",
                   help="subset of the roster to run (default: both) — e.g. --models gemini")
    p.add_argument("--event", type=int, default=None, metavar="N",
                   help="run ONLY portfolio event N (0-based; see --list-events). Default: all 12")
    p.add_argument("--list-events", action="store_true", dest="list_events",
                   help="print the event portfolio with indices, then exit")
    p.add_argument("--seeds", type=int, default=5,
                   help="episodes (seeds) per event; one estimate per episode -> the CI. Default 5")
    p.add_argument("--cycles", type=int, default=5,
                   help="max assessment cycles per episode; stop when the judge commits a reserve. Default 5")
    p.add_argument("--flat", type=int, default=10, help="flat rule's fixed reserve")
    p.add_argument("--fraction", type=float, default=None,
                   help="formula rule's fixed fill fraction (default = best-fit over non-sold-out)")
    p.add_argument("--save", action="store_true", help="cache records+reasoning to exp3_reserve_sizing/reserve_sizing.json")
    p.add_argument("--log", nargs="?", const="AUTO", default=None,
                   help="tee ALL output to a file (bare --log auto-names it under exp3_reserve_sizing/logs/)")
    args = p.parse_args()

    _logfile = None
    if args.log is not None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = (_EXP_DIR / "logs" / f"log_reserve_sizing_{stamp}.txt"
                    if args.log == "AUTO" else Path(args.log))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _logfile = open(log_path, "w")
        sys.stdout = _Tee(sys.__stdout__, _logfile)
        sys.stderr = _Tee(sys.__stderr__, _logfile)
        print(f"[exp3] logging this run to {log_path}")

    _prevent_sleep()
    try:
        asyncio.run(main(args))
    finally:
        if _logfile is not None:
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
