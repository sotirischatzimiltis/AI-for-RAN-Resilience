
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset, FastMCPClient

from mcp_server.server import MCP_HOST, MCP_PORT, UP, host as sim_host
from sim.metrics import utility as compute_utility
from agents.policy import SharedPolicy, EpisodeStats

# we set the url of the MCP server here so the agent can call its tools via HTTP
MCP_URL = f"http://{MCP_HOST}:{MCP_PORT}/mcp"

# directory containing the system prompt for the Near-RT-Agent
_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
SYSTEM_PROMPT = (_PROMPTS_DIR / "near_rt.md").read_text()

# This is the structured output returned by the Near-RT-Agent each cycle.
class NearRTDecision(BaseModel):
    servers_applied: int   = Field(description="Current server count (whether changed or held)")
    drop_prob:       float = Field(ge=0.0, le=1.0, description="Current drop probability (whether changed or held)")
    storm_active:    bool  = Field(description="True if a signaling storm is currently active")
    action_taken:    bool  = Field(description="True if the agent called set_servers or set_drop_prob this cycle")
    reasoning:       str   = Field(description="Brief reasoning — what was observed and why the agent did (or did not) act")

# Here we build the Near-RT-Agent with its model, output type, and tools.
def build_near_rt_agent(model) -> Agent:
    toolset = MCPToolset(FastMCPClient(MCP_URL)) # provide the agent with a toolset for calling via the MCP
    # here we define the agent with its model, what to output, the toolset, and the system prompt for guidance
    return Agent(
        model=model,
        output_type=NearRTDecision,
        toolsets=[toolset],
        system_prompt=SYSTEM_PROMPT,
    )

# Here we craft the "user" input to the agent 
def _build_telemetry_snapshot(prev_retries: int) -> tuple[str, int]:
    """
    Read the latest telemetry directly from the simulator (no MCP round-trip).
    Returns (formatted snapshot string, current retries for delta tracking).
    """

    if sim_host.sim is None or not sim_host.sim.telemetry:
        return "No telemetry available yet — episode may not have started.", prev_retries

    s           = sim_host.sim.telemetry[-1]
    u           = compute_utility(s, sim_host.sim.mu_single, UP)
    new_retries = s.retries - prev_retries
    server_util = round(s.busy / s.c, 2) if s.c > 0 else 0.0
    drop_prob   = sim_host.sim.malicious_drop_prob

    snapshot = (
        f"t={s.t:.1f}s | "
        f"lam_current={s.lam_current:.1f} UEs/s | "
        f"queue_len={s.queue_len} | "
        f"busy={s.busy} | "
        f"c={s.c} | "
        f"c_max={sim_host.sim.cfg.c_max} | "
        f"utility={u:.3f} | "
        f"new_retries={new_retries} | "
        f"server_utilization={server_util:.2f} | "
        f"drop_prob={drop_prob:.2f}"
    )
    return snapshot, s.retries

# this is the control loop that runs the Near-RT-Agent
# Function signature / inputs and expected output
async def run_control_loop(
    agent:         Agent,
    policy:        SharedPolicy,
    stop_event:    asyncio.Event,
    poll_interval: float = 1.0,
    stats:         EpisodeStats | None = None,
) -> None:
    
    step         = 0 # iteration counter for logging
    prev_retries = 0 # track the number of retries from the previous cycle to compute new_retries

    while not stop_event.is_set(): # loop until the stop_event is triggered
        step += 1
        if stats:
            stats.near_rt_steps += 1
        # create current step snapshot of telemetry and update prev_retries for next cycle
        snapshot, prev_retries = _build_telemetry_snapshot(prev_retries)

        t0 = time.monotonic() # record the start time of this cycle to measure elapsed time for logging
        # craft user prompt for the agent
        prompt = (
            f"Cycle {step}. {policy.context_str()}\n"
            f"Telemetry: {snapshot}\n"
            "Observe and decide whether to act."
        )
        # run the agent with the prompt and handle any exceptions
        try:
            result  = await agent.run(prompt) # wait for response from the agent
            d       = result.output # extract the structured output from the agent's response
            elapsed = time.monotonic() - t0 # measure how long the agent took to respond

            # log the decision and reasoning for this cycle
            marker = "✦" if d.action_taken else "·"
            print(
                f"[Near-RT] {marker} step={step:3d}  "
                f"c={d.servers_applied:2d}  drop={d.drop_prob:.2f}  "
                f"storm={d.storm_active}  acted={d.action_taken}  "
                f"({elapsed:.1f}s)  {d.reasoning}"
            )
        # catch any exceptions raised during the agent's execution and log them
        # increase the near_rt_errors counter in stats if provided
        except Exception as e:
            if stats:
                stats.near_rt_errors += 1
            print(f"[Near-RT] error at step {step}: {e}")

        # Sleep unitl one of those happens
        # 1. stop_event is set (episode ends)
        # 2. poll_interval elapses (next cycle)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass