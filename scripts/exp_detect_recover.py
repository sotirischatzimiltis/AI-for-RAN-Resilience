"""Resilience timeline — ONE agentic episode, logging time-to-detect and time-to-recover.

Runs the SAME botnet+event scenario (LLM_COMPARE) and the SAME agent/model as Exp 1
(GPT-5.4-Mini, reasoning on), over the deterministic fast loop, and records:
  - the per-sample utility u(t) trace,
  - the agent's storm_active verdict over time (a logger coroutine samples it as the sim clock
    advances), so we can read the DETECTION time = first storm_active=True after the botnet onset,
  - the recovery internals from recovery_report (RECOVERY time tr = when u returns above 95% of the
    pre-storm baseline and holds).

Writes experiments/detect_recover/detect_recover.json. Plot it with the companion
plot_detect_recover.py (kept separate so re-plotting never needs another LLM run).

This is a NEW, self-contained script. It imports the existing building blocks and does NOT modify
any of them. Run from repo root with the OpenRouter key sourced:
    python -m scripts.exp_detect_recover --seed 1 --save
"""
import argparse
import asyncio
import json
import logging
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_server.server import mcp, MCP_HOST, MCP_PORT
from scripts.run import resolve_model
from agents.non_rt_agent import build_non_rt_agent, compose_system_prompt, run_assessment_loop
from agents.near_rt_control_loop import run_control_loop
from shared.policy import SharedPolicy, RunStats
from sim.metrics import resilience_multi, recovery_report, utility
from runtime import UP, host as sim_host, LLM_COMPARE

_ROOT = Path(__file__).resolve().parents[1]
_OUT_DIR = _ROOT / "experiments" / "detect_recover"
_SYS_PROMPT = (_ROOT / "prompts" / "non_rt_agent_system_prompt.md").read_text()

# tag -> (slug, per-call LLM settings), matching how Exp 1 evaluated each: GPT-5.4-Mini with
# reasoning ON (the winner), Gemini-3.1-Flash-Lite plain at pinned temperature (efficient tier).
MODELS = {
    "gpt":    ("openrouter:openai/gpt-5.4-mini",
               {"timeout": 60.0, "max_tokens": 8000, "extra_body": {"reasoning": {"effort": "high"}}}),
    "gemini": ("openrouter:google/gemini-3.1-flash-lite",
               {"timeout": 60.0, "temperature": 0.0}),
}


_TCRIT = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571, 7: 2.447, 8: 2.365, 9: 2.306, 10: 2.262}


def _mean_ci(xs: list[float]) -> tuple[float, float]:
    """(mean, 95% CI half-width) via Student-t for small n; 0 CI for n<2."""
    xs = [float(x) for x in xs if x is not None]
    n = len(xs)
    if n == 0:
        return float("nan"), float("nan")
    m = statistics.mean(xs)
    if n < 2:
        return m, 0.0
    sd = statistics.stdev(xs)
    return m, _TCRIT.get(n, 1.96) * sd / math.sqrt(n)


def _aggregate(tag: str, slug: str, episodes: list[dict]) -> dict:
    """Pool per-seed detect lag + botnet recovery time (mean +/- 95% CI) and average u(t)."""
    onset = episodes[0]["botnet_onset"]
    end = episodes[0]["botnet_end"]
    detect_lags = [e["detect_lag"] for e in episodes]
    recover_times = [(e["recovery"][0]["tr"] - end) if e["recovery"][0]["recovered"] else None
                     for e in episodes]
    targets = [100 * e["recovery"][0]["target"] for e in episodes]

    # average u(t) across seeds on the shared telemetry grid (same scenario -> same 1s ticks)
    L = min(len(e["util_trace"]) for e in episodes)
    grid = [e["util_trace"][:L] for e in episodes]
    util_mean = [{"t": grid[0][i]["t"],
                  "u": round(100 * statistics.mean(g[i]["u"] for g in grid), 3)}
                 for i in range(L)]

    dl_m, dl_ci = _mean_ci(detect_lags)
    rt_m, rt_ci = _mean_ci(recover_times)
    return {
        "tag": tag, "model": slug.split(":", 1)[1], "scenario": LLM_COMPARE,
        "seeds": [e["seed"] for e in episodes],
        "botnet_onset": onset, "botnet_end": end,
        "detect_lags": detect_lags, "detect_lag_mean": round(dl_m, 1), "detect_lag_ci": round(dl_ci, 1),
        "recover_times": recover_times, "recover_mean": round(rt_m, 1), "recover_ci": round(rt_ci, 1),
        "detection_rate": sum(x is not None for x in detect_lags) / len(episodes),
        "recovery_rate": sum(x is not None for x in recover_times) / len(episodes),
        "target_mean": round(statistics.mean(targets), 2),
        "t_detect_mean": round(onset + dl_m, 1) if not math.isnan(dl_m) else None,
        "t_recover_mean": round(end + rt_m, 1) if not math.isnan(rt_m) else None,
        "util_mean": util_mean,
    }


async def _run(slug: str, settings: dict, seed: int, rt_factor: float,
               assessment_interval: float, window_s: float) -> dict:
    non_rt = build_non_rt_agent(
        resolve_model(slug),
        system_prompt=compose_system_prompt(_SYS_PROMPT, calendar_enabled=True, forecast_enabled=True),
    )
    policy, stats = SharedPolicy(), RunStats()
    sim_host.forecast_enabled = sim_host.calendar_enabled = True
    sim_host.start(scenario=LLM_COMPARE, seed=seed, c_max=16, rt_factor=rt_factor,
                   provision_parallel=False)                  # serial, as in Exp 1/2

    stop_event = asyncio.Event()
    verdict_log: list[tuple[float, bool]] = []                # (sim_t, storm_active) at each new tick

    async def _watch():
        while not sim_host.is_done:
            await asyncio.sleep(0.5)
        stop_event.set()

    async def _logger():
        last_t = -1.0
        while not stop_event.is_set():
            sim = sim_host.sim
            if sim and sim.telemetry:
                t = sim.telemetry[-1].t
                if t != last_t:
                    verdict_log.append((round(t, 1), bool(policy.storm_active)))
                    last_t = t
            await asyncio.sleep(0.2)

    await asyncio.gather(
        _watch(), _logger(),
        run_control_loop(policy, stop_event, 1.0, stats, memory=None),
        run_assessment_loop(non_rt, policy, stop_event, assessment_interval, stats,
                            window_s=window_s, model_settings=settings),
    )

    sim = sim_host.sim
    storms = sim.cfg.traffic.storm_windows()
    recov = recovery_report(sim.telemetry, sim.mu_single, UP, storms)
    resilience_multi(sim.telemetry, sim.mu_single, UP, storms)  # keep parity with Exp 1 scoring path

    # merge storm_active onto every telemetry sample by last-known verdict (step function)
    def _verdict_at(t):
        v = False
        for vt, va in verdict_log:
            if vt <= t:
                v = va
            else:
                break
        return v

    util_trace = [{"t": round(s.t, 1), "u": round(utility(s, sim.mu_single, UP), 4),
                   "lam": round(s.lam_current, 1), "storm_active": _verdict_at(s.t)}
                  for s in sim.telemetry]

    # detection = first storm_active=True at or after the botnet-window onset
    bot_t0, bot_td = storms[0]
    t_detect = next((vt for vt, va in verdict_log if va and vt >= bot_t0), None)

    return {
        "scenario": LLM_COMPARE, "seed": seed, "model": slug.split(":", 1)[1],
        "storms": [[round(a, 1), round(b, 1)] for a, b in storms],
        "botnet_onset": round(bot_t0, 1), "botnet_end": round(bot_td, 1),
        "t_detect": t_detect,
        "detect_lag": (round(t_detect - bot_t0, 1) if t_detect is not None else None),
        "recovery": recov,
        "verdict_log": verdict_log,
        "util_trace": util_trace,
    }


async def _main(args):
    tags = list(MODELS) if args.model == "both" else [args.model]
    seeds = list(range(1, args.seeds + 1))
    print(f"[detect-recover] MCP server on {MCP_HOST}:{MCP_PORT} ...  models: {tags}  seeds: {seeds}")
    server_task = asyncio.create_task(
        mcp.run_http_async(host=MCP_HOST, port=MCP_PORT, show_banner=False, log_level="warning"))
    await asyncio.sleep(1.5)
    aggs: dict[str, dict] = {}
    try:
        for tag in tags:
            slug, settings = MODELS[tag]
            episodes = []
            for seed in seeds:
                print(f"\n=== {tag} seed {seed}: {slug} ===")
                e = await _run(slug, settings, seed, args.rt_factor,
                               args.assessment_interval, args.window_s)
                print(f"  seed {seed}: detect lag {e['detect_lag']}s  "
                      f"recovered={e['recovery'][0]['recovered']} tr={e['recovery'][0]['tr']}")
                episodes.append(e)
            aggs[tag] = _aggregate(tag, slug, episodes)
    finally:
        logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass

    if args.save:
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
    for tag, d in aggs.items():
        print(f"\n  [{tag}] over {len(d['seeds'])} seeds: "
              f"detect {d['detect_lag_mean']}+/-{d['detect_lag_ci']}s (rate {d['detection_rate']:.0%}), "
              f"recover {d['recover_mean']}+/-{d['recover_ci']}s (rate {d['recovery_rate']:.0%})")
        if args.save:
            out = _OUT_DIR / f"detect_recover_{tag}.json"
            out.write_text(json.dumps(d, indent=2))
            print(f"  [{tag}] saved -> {out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Resilience timeline: time-to-detect + time-to-recover (1 agentic episode)")
    p.add_argument("--model", choices=["gpt", "gemini", "both"], default="both")
    p.add_argument("--seeds", type=int, default=3, help="run seeds 1..N per model (each ~7 min real time)")
    p.add_argument("--rt-factor", type=float, default=1.0, dest="rt_factor",
                   help="1.0 = real time (keeps LLM latency-to-detection realistic)")
    p.add_argument("--assessment-interval", type=float, default=5.0, dest="assessment_interval")
    p.add_argument("--window", type=float, default=15.0, dest="window_s")
    p.add_argument("--save", action="store_true")
    asyncio.run(_main(p.parse_args()))
