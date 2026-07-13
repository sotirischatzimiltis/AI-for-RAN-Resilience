"""
Run the simulation + Near-RT control loop together — no LLM, no API key.

Starts a StormSim episode (via runtime.host) and the pure-code 1 Hz control loop.
A small scripted "storm signal" stands in for the Non-RT judge so BOTH control
levers are exercised: it sets storm_active from the arrival rate, which gates the
malicious-UE drop filter. Capacity (servers) reacts on its own every tick.

Three concurrent async tasks make up a run:
  1. watch_until_done   — stops everything when the episode finishes
  2. storm_signal       — the scripted stand-in judge (writes policy)
  3. run_control_loop   — the fast 1 Hz control loop (reads policy, actuates)

Usage:
    python -m scripts.run_near_rt [--rt-factor 1.0] [--poll-interval 1.0] [--t-post 60]
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from runtime import host as sim_host
from agents.near_rt_control_loop import run_control_loop
from agents.policy import SharedPolicy, EpisodeStats


async def storm_signal(policy: SharedPolicy, stop_event: asyncio.Event, interval: float) -> None:
    """
    Scripted stand-in for the Non-RT judge (no LLM). Every `interval` seconds it
    declares a storm when the arrival rate is elevated and writes the verdict into
    shared policy — exactly what the real Non-RT agent would write.
    """
    while not stop_event.is_set():
        sim = sim_host.sim
        if sim is not None and sim.telemetry:
            lam   = sim.telemetry[-1].lam_current
            storm = lam > 100.0                      # baseline ~20, storm ~200
            policy.update(
                storm_active=storm,
                malicious_drop_prob=0.8 if storm else 0.0,
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def main(args: argparse.Namespace) -> None:
    policy     = SharedPolicy()
    stats      = EpisodeStats()
    stop_event = asyncio.Event()

    # 1. start the simulation episode (background thread inside runtime.host)
    msg = sim_host.start(
        scenario="single_storm", seed=args.seed, c_max=args.c_max,
        rt_factor=args.rt_factor, t_post=args.t_post,
    )
    print(f"[run] {msg}\n")

    # stop the loop + signal once the episode finishes
    async def watch_until_done():
        while not sim_host.is_done:
            await asyncio.sleep(0.5)
        stop_event.set()

    # safety net: force-stop if the episode overruns its expected wall clock
    horizon_sim      = 50.0 + 60.0 + args.t_post
    wall_clock_limit = horizon_sim / args.rt_factor + 60.0
    async def hard_timeout():
        await asyncio.sleep(wall_clock_limit)
        if not stop_event.is_set():
            print(f"\n[run] Hard timeout ({wall_clock_limit:.0f}s) — forcing stop.")
            stop_event.set()

    # 2 + 3. run the storm signal and the control loop until the episode ends
    t0 = time.monotonic()
    timeout_task = asyncio.create_task(hard_timeout())
    try:
        await asyncio.gather(
            watch_until_done(),
            storm_signal(policy, stop_event, args.signal_interval),
            run_control_loop(policy, stop_event, args.poll_interval, stats),
        )
    finally:
        timeout_task.cancel()
    elapsed = time.monotonic() - t0

    sim = sim_host.sim
    print(f"\n{'=' * 56}")
    print("NEAR-RT RUN REPORT")
    print(f"{'=' * 56}")
    print(f"  Wall-clock time : {elapsed:.1f}s  (sim horizon / rt_factor)")
    print(f"  Control steps   : {stats.near_rt_steps}")
    print(f"  Completed UEs   : {sim.stats.completed}")
    print(f"  Failed UEs      : {sim.stats.failed}")
    print(f"  Retries         : {sim.stats.retries}")
    print(f"{'=' * 56}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Run sim + Near-RT control loop (no LLM)")
    p.add_argument("--rt-factor",      type=float, default=1.0, dest="rt_factor",
                   help="wall-clock seconds per simulated second (1.0 = real time)")
    p.add_argument("--poll-interval",  type=float, default=1.0, dest="poll_interval",
                   help="control-loop tick interval in seconds")
    p.add_argument("--signal-interval", type=float, default=5.0, dest="signal_interval",
                   help="scripted storm-signal cadence in seconds")
    p.add_argument("--t-post",         type=float, default=60.0, dest="t_post",
                   help="post-storm duration in simulated seconds")
    p.add_argument("--seed",           type=int,   default=3,   dest="seed")
    p.add_argument("--c-max",          type=int,   default=16,  dest="c_max")
    args = p.parse_args()
    asyncio.run(main(args))
