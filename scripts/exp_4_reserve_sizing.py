"""
Experiment 4 — reserve sizing: does the Non-RT LLM size a scheduled surge better than a rule?

The one place a language model should beat a hardcoded controller: reading a free-text calendar
entry and REASONING to the crowd it implies. Three arms size the pre-provisioning reserve for
each of the 12 real events in docs/event_portfolio.md:
  • Flat rule      — "an event is scheduled" -> reserve a fixed number (default 10). No context.
  • Formula rule   — given the STRUCTURED fields {capacity, sold_out}: sold_out -> capacity, else
                     a fixed fraction x capacity. The honest strong baseline (parsing conceded);
                     it can't infer the turnout fraction from context.
  • LLM            — the REAL Non-RT agent calls get_calendar, reads the event's FREE-TEXT
                     description, and sets reserve_servers. No parallel agent — the same judge and
                     tool the deployed system uses.
Every arm's decision becomes a reserve; each event is then simulated as a benign surge sized by
its GROUND-TRUTH attendance, with that reserve pre-provisioned (FixedController), scoring benign
QoS (penalises under-reserve) and servers used (penalises over-reserve). Split by sold-out vs not:
the arms should tie on sold-out (flag suffices) and separate on the rest.

Usage (rules only, no API — check the design):
    python -m scripts.exp_4_reserve_sizing
With the LLM arm (needs the OpenRouter key):
    python -m scripts.exp_4_reserve_sizing --llm --save
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
from scripts.exp_1_model_comparison import _Tee, _prevent_sleep   # reuse logging + no-sleep
from agents.non_rt_agent import build_non_rt_agent, compose_system_prompt, _do_assessment
from shared.events import PORTFOLIO, reserve_for
from shared.policy import SharedPolicy
from shared.event_calendar import ScheduledEvent
from sim.config import (SimConfig, open_ran_arch, RRCConfig, event_surge_traffic,
                        TrafficConfig, TrafficPhase)
from sim.simulator import StormSim
from sim.controllers import FixedController
from sim.metrics import benign_success_rate, avg_servers
from runtime import host as sim_host

C_MAX = 16
MU    = open_ran_arch().service_rate()      # per-server service rate the reserve math uses
AGENT_MODEL  = "openrouter:google/gemini-3.1-flash-lite"   # Exp 1 winner
_STORM_PROMPT = (Path(__file__).parent.parent / "prompts" / "non_rt_agent_system_prompt.md").read_text()
_EXP_DIR = Path(__file__).parent.parent / "experiments" / "exp4_reserve_sizing"


# ------------------------------- the rule arms (attendance -> reserve) --------------------------
def flat_reserve(event, args) -> int:
    return min(args.flat, C_MAX)                          # context-blind floor

def formula_reserve(event, fraction: float) -> int:
    att = event.capacity if event.sold_out else fraction * event.capacity   # sold-out -> full, else fixed fraction
    return reserve_for(att, MU, C_MAX)


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
async def llm_reserves(events, args) -> tuple[list[int], list[str]]:
    """For each event, the REAL Non-RT agent assesses a calm window with ONLY that event on the
    calendar; it calls get_calendar, reads the free-text, and sets reserve_servers. We read that
    plus the verdict's reasoning. The deployed judge + tool, not a bespoke agent — just isolated
    to the pre-event moment."""
    # one calm telemetry context, reused (the reserve decision is made BEFORE the surge)
    calm = StormSim(SimConfig(arch=open_ran_arch(), rrc=RRCConfig(t300_ms=1000, max_attempts=5),
                              c0=2, c_max=C_MAX, seed=1,
                              traffic=TrafficConfig(baseline_rate=20.0,
                                                    phases=[TrafficPhase(0.0, 60.0, 20.0, 0.0, "calm")])))
    calm.run(controller=FixedController(2))
    sim_host.sim = calm
    sim_host.calendar_enabled = sim_host.forecast_enabled = True
    t_now = calm.telemetry[-1].t
    agent = build_non_rt_agent(resolve_model(AGENT_MODEL),
                               system_prompt=compose_system_prompt(_STORM_PROMPT,
                                                                   calendar_enabled=True, forecast_enabled=True))
    reserves, reasons = [], []
    for e in events:
        sim_host.calendar = [ScheduledEvent(t_now + 30.0, e.name, "high", e.venue, e.sold_out)]  # event in 30s
        policy = SharedPolicy()
        v = await _do_assessment(agent, policy, 1, None, window_s=15.0)  # real judge + get_calendar
        reserves.append(max(1, min(policy.reserve_servers, C_MAX)))      # reserve DERIVED from the crowd
        crowd = v.expected_attendance if v is not None else 0
        why   = (v.attendance_reasoning or v.reasoning) if v is not None else "(no verdict)"
        reasons.append(f"[crowd~{crowd}] {why}")
        print(f"[llm] {e.name[:52]:52s} crowd~{crowd:>6d} -> reserve {reserves[-1]:2d}")
    return reserves, reasons


# ------------------------------- simulate one event at a given reserve --------------------------
def simulate_event(event, reserve: int, seeds) -> tuple[float, float]:
    """Play the event as a benign surge with `reserve` servers pre-provisioned; return
    (mean benign completion, mean avg_servers) over seeds."""
    reserve = max(1, min(reserve, C_MAX))
    bens, srvs = [], []
    for s in seeds:
        cfg = SimConfig(arch=open_ran_arch(), rrc=RRCConfig(t300_ms=1000, max_attempts=5),
                        c0=reserve, c_max=C_MAX, traffic=event_surge_traffic(event.surge_peak()), seed=s)
        sim = StormSim(cfg)
        sim.run(controller=FixedController(reserve))
        bens.append(benign_success_rate(sim.stats))
        srvs.append(avg_servers(sim.telemetry))
    return statistics.mean(bens), statistics.mean(srvs)


# ------------------------------------------ report ---------------------------------------------
def _agg(rows, key):
    return round(statistics.mean(r[key] for r in rows), 3) if rows else 0.0

def _report(records, arms):
    print("\n" + "=" * 100)
    print("RESERVE SIZING — per-event reserve (ideal vs each arm)")
    print("=" * 100)
    print(f"  {'event':44s} {'sold':>4s} {'ideal':>5s} " + " ".join(f"{a:>8s}" for a in arms))
    for r in records:
        print(f"  {r['label']:44s} {('Y' if r['sold_out'] else 'n'):>4s} {r['ideal']:>5d} "
              + " ".join(f"{r[a+'_res']:>8d}" for a in arms))
    print("=" * 100)
    print("OUTCOMES — mean |reserve error| / benign QoS / avg servers, split by sold-out")
    print("=" * 100)
    for grp, sel in [("sold-out (control)", lambda r: r["sold_out"]),
                     ("NOT sold-out", lambda r: not r["sold_out"]),
                     ("all", lambda r: True)]:
        sub = [r for r in records if sel(r)]
        print(f"\n  --- {grp}  (n={len(sub)}) ---")
        print(f"  {'arm':16s} {'|err|':>7s} {'benign':>8s} {'servers':>8s}")
        for a in arms:
            err = _agg([{'e': abs(r[a+'_res'] - r['ideal'])} for r in sub], 'e')
            ben = _agg([{'b': r[a+'_ben']} for r in sub], 'b')
            srv = _agg([{'s': r[a+'_srv']} for r in sub], 's')
            print(f"  {a:16s} {err:>7.2f} {ben:>8.3f} {srv:>8.2f}")
    print("=" * 100)


async def _score(events, reserves, arms, seeds, llm_reasons=None):
    records = []
    for i, e in enumerate(events):
        rec = {"label": e.name[:44], "name": e.name, "venue": e.venue, "sold_out": e.sold_out,
               "attendance": e.attendance, "ideal": e.ideal_reserve(MU, C_MAX)}
        for a in arms:
            res = reserves[a][i]
            ben, srv = simulate_event(e, res, seeds)
            rec[a + "_res"], rec[a + "_ben"], rec[a + "_srv"] = res, round(ben, 4), round(srv, 3)
        if llm_reasons is not None:
            rec["llm_reasoning"] = llm_reasons[i]     # WHY the agent chose its reserve (for observation)
        records.append(rec)
    return records


async def main(args):
    if args.list_events:                                 # just show the portfolio + indices, then exit
        print("Event portfolio (index | ideal reserve | sold-out | name @ venue):")
        for i, e in enumerate(PORTFOLIO):
            print(f"  {i:>2d} | {e.ideal_reserve(MU, C_MAX):>2d} | {'Y' if e.sold_out else 'n'} | {e.name} @ {e.venue}")
        return

    seeds  = list(range(1, args.seeds + 1))
    frac, frac_err = best_fraction(PORTFOLIO)            # fraction fit on the FULL portfolio, always
    events = [PORTFOLIO[args.event]] if args.event is not None else PORTFOLIO   # --event N -> just one
    fraction = args.fraction if args.fraction is not None else frac
    print(f"[exp4] mu={MU:.2f}  formula fraction={fraction:.2f} "
          f"(best-fit non-sold-out fraction={frac:.2f}, its mean |err|={frac_err:.2f})")

    arms = ["flat", "formula"]
    reserves = {"flat":    [flat_reserve(e, args) for e in events],
                "formula": [formula_reserve(e, fraction) for e in events]}
    llm_reasons = None

    if args.llm:                                          # LLM arm needs the MCP server up (get_calendar)
        print(f"[exp4] starting MCP server on {MCP_HOST}:{MCP_PORT} for the LLM arm ...")
        server_task = asyncio.create_task(
            mcp.run_http_async(host=MCP_HOST, port=MCP_PORT, show_banner=False, log_level="warning"))
        await asyncio.sleep(1.5)
        try:
            reserves["llm"], llm_reasons = await llm_reserves(events, args)
            arms.append("llm")
        finally:
            logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass

    records = await _score(events, reserves, arms, seeds, llm_reasons)
    _report(records, arms)
    if llm_reasons is not None:                           # echo the agent's reasoning per event
        print("\nLLM REASONING (why each reserve):")
        for r in records:
            print(f"  [{r['ideal']:>2d} ideal | {r['llm_res']:>2d} chosen] {r['name'][:46]:46s} | {r['llm_reasoning']}")
    if args.save:
        _EXP_DIR.mkdir(parents=True, exist_ok=True)
        out = _EXP_DIR / "reserve_sizing.json"
        out.write_text(json.dumps({"mu": MU, "fraction": fraction, "seeds": seeds,
                                   "arms": arms, "records": records}, indent=2))
        print(f"\n  saved -> {out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Experiment 4 — reserve sizing: rules vs LLM")
    p.add_argument("--llm", action="store_true", help="include the LLM arm (real Non-RT agent; needs the OpenRouter key)")
    p.add_argument("--event", type=int, default=None, metavar="N",
                   help="run ONLY portfolio event N (0-based; see --list-events). Default: all 12")
    p.add_argument("--list-events", action="store_true", dest="list_events",
                   help="print the event portfolio with indices, then exit")
    p.add_argument("--seeds", type=int, default=5, help="seeds per event for the outcome sim")
    p.add_argument("--flat", type=int, default=10, help="flat rule's fixed reserve")
    p.add_argument("--fraction", type=float, default=None,
                   help="formula rule's fixed fill fraction (default = best-fit over non-sold-out)")
    p.add_argument("--save", action="store_true", help="cache records+reasoning to exp4_reserve_sizing/reserve_sizing.json")
    p.add_argument("--log", nargs="?", const="AUTO", default=None,
                   help="tee ALL output to a file (bare --log auto-names it under exp4_reserve_sizing/logs/)")
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
        print(f"[exp4] logging this run to {log_path}")

    _prevent_sleep()
    try:
        asyncio.run(main(args))
    finally:
        if _logfile is not None:
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
            _logfile.close()
