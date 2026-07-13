"""
Orchestrator intent agent — translates a network operator's free-text intent into
a structured IntentDirective the Orchestrator applies to shared policy.

This is the network-management (SMO/rApp) tier: the operator expresses a high-level
goal ("protect this site", "cut cost tonight", "prepare for the match at 21:00") and
the agent maps it to concrete levers — the Lyapunov posture (V/W), an SLA capacity
floor (min_servers), and optionally a scheduled load event. These operator overrides
outrank the Non-RT judge's autonomous tuning until changed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
SYSTEM_PROMPT = (_PROMPTS_DIR / "orchestrator.md").read_text()


class IntentDirective(BaseModel):
    priority: Literal["qos", "cost", "balanced"] = Field(
        description="Overall posture the operator wants: 'qos' favours service (more "
                    "servers), 'cost' favours efficiency (fewer servers), 'balanced' is neutral")
    lyapunov_V: float | None = Field(
        default=None, ge=0.0, le=100000.0,
        description="Explicit Lyapunov utility weight override (higher -> more servers). "
                    "Leave null to derive from priority")
    lyapunov_W: float | None = Field(
        default=None, ge=0.0, le=1000.0,
        description="Explicit Lyapunov server-cost weight override (higher -> fewer servers). "
                    "Leave null to derive from priority")
    min_servers: int | None = Field(
        default=None, ge=1, le=64,
        description="SLA capacity floor: never run fewer than this many servers. Null = no floor")
    schedule_event_name: str | None = Field(
        default=None, description="If the intent names a KNOWN upcoming load event, its label; else null")
    schedule_event_t: float | None = Field(
        default=None, ge=0.0, description="Simulated-time (seconds) the scheduled event begins; else null")
    schedule_event_severity: Literal["low", "medium", "high"] = Field(
        default="high", description="Expected load of the scheduled event")
    reasoning: str = Field(description="One sentence: how this directive serves the operator's intent")


def build_intent_agent(model) -> Agent:
    return Agent(model=model, output_type=IntentDirective, system_prompt=SYSTEM_PROMPT)


# priority -> (V, W) when the operator did not give explicit weights
PRIORITY_VW = {
    "qos":      (5000.0, 1.0),   # favour service: many servers
    "cost":     (500.0, 5.0),    # favour efficiency: few servers
    "balanced": (1000.0, 1.0),   # the default posture
}
