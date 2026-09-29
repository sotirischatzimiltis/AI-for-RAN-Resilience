"""
Fleet orchestrator — the SMO tier across MANY sites (Exp 8).

The single-site Orchestrator (agents/orchestrator.py) grounds one free-text intent into ONE
OperatorDirective for one site. Across a fleet the operator may target one site, several, or
everyone, and two intents (or an intent and a standing posture) may CLASH. This layer decomposes a
free-text intent into a FleetDirective: a per-site list of directives, each carrying the SAME five
levers, dispatched only to the sites the intent targets — the A2A decomposition described in the
framework section. It reuses the base orchestrator's lever-grounding rules unchanged (prompts/
orchestrator.md) and adds only routing + conflict resolution on top.

Conflict resolution precedence (stated to the model, graded in Exp 8):
  1. scope specificity — a site-specific request overrides a fleet-wide one on the sites it names.
  2. lever priority    — at equal scope, SLA/service > security/filtering > cost.
  3. recency           — at equal scope and priority, the later intent wins.
  4. escalate          — two HARD constraints that cannot both hold (e.g. floor>=12 vs cap<=4 on one
                         site) are irreconcilable: emit NO directive for that lever and set `conflict`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from agents.orchestrator import OperatorDirective, SYSTEM_PROMPT, PRIORITY_VW
from agents.non_rt_agent import _accumulate_usage
from shared.policy import SharedPolicy, RunStats
from shared.event_calendar import ScheduledEvent


# ---------------------------------------------------------------------------
# A site as the orchestrator sees + acts on it
# ---------------------------------------------------------------------------
@dataclass
class SiteContext:
    """One fleet member: an id + human description the orchestrator ROUTES on, and the live policy +
    host it ACTS on. The agent prompt sees only id/description; route_fleet_intent uses policy/host."""
    id: str
    description: str
    policy: SharedPolicy | None = None
    host: object | None = None       # a SimHost (runtime.SimHost); the site's calendar lives here


# ---------------------------------------------------------------------------
# Output schema: one intent -> many per-site directives (+ an optional conflict note)
# ---------------------------------------------------------------------------
class SiteDirective(OperatorDirective):
    """A single-site OperatorDirective tagged with the site it applies to. Inherits all five levers
    (posture/weights, floor, scheduled event, delegation) and their field descriptions unchanged."""
    site_id: str = Field(description="The id of the site this directive applies to, taken EXACTLY "
                         "from the site registry. Emit a directive ONLY for a site the intent targets.")


class FleetDirective(BaseModel):
    # reasoning FIRST (generation order = plan-before-commit): name the target sites and how each
    # clause maps to a lever on them, and call out any clash and how the precedence rules resolve it.
    reasoning: str = Field(description="Reason FIRST: which sites does the intent target, which lever "
                    "does each clause set on them, and if two requests clash, which precedence rule "
                    "resolves it (scope > priority > recency > escalate)")
    site_directives: list[SiteDirective] = Field(default_factory=list,
        description="One directive per TARGETED site. A fleet-wide intent emits one for every site; a "
                    "site-specific intent emits only for the named site(s). Do NOT emit no-op directives "
                    "for sites the intent does not touch")
    conflict: str | None = Field(default=None,
        description="Set ONLY when two HARD constraints on the same site cannot both be satisfied and "
                    "no precedence rule resolves them. Describe the clash for the human; leave the "
                    "irreconcilable lever unset. Null when everything reconciles")


# ---------------------------------------------------------------------------
# Prompt: the base lever rules + a fleet header (registry, routing, precedence)
# ---------------------------------------------------------------------------
_FLEET_HEADER = """You are the Orchestrator of a MULTI-SITE AI RAN network at the SMO tier. An
operator gives free-text intents that may target one site, several sites, or the whole fleet, and two
requests may CLASH. You return a FleetDirective: a list of per-site directives (each a site_id plus
the standard levers), and an optional `conflict` note.

# The fleet
{registry}

# Routing — send each request only where it belongs
Map every clause to the site(s) it targets, using the registry descriptions.
- A fleet-wide request ("across the fleet", "everywhere", no site named for a global posture) emits a
  directive for EVERY site.
- A site-specific request emits a directive ONLY for the named/implied site(s). Leave every other
  site out entirely — do NOT emit a no-op directive for a site the intent does not touch. A directive
  sent to the wrong site, or to a site the intent never mentioned, is an error.

# Conflict resolution — apply in this order
1. Scope: a site-specific request OVERRIDES a fleet-wide one on the sites it names. So "cut cost
   everywhere but spare no capacity at the stadium" -> cost posture on every other site, QoS on the
   stadium.
2. Priority: at equal scope, protecting service/SLA beats security/filtering beats cost.
3. Recency: at equal scope and priority, the later request wins.
4. Escalate: if two HARD constraints on the same site cannot both hold (e.g. a floor of >=12 servers
   and a cap of <=4), they are irreconcilable — leave that lever unset and describe the clash in
   `conflict`. Do not silently pick one.

# Per-site lever rules
Apply the rules below INDEPENDENTLY to each site directive you emit. They describe a single
OperatorDirective; ignore their statement that you return only one, and set `site_id` on each.

---
{base}
"""


def _registry_block(sites: list[SiteContext]) -> str:
    return "\n".join(f"- {s.id}: {s.description}" for s in sites)


def compose_fleet_prompt(sites: list[SiteContext]) -> str:
    """The fleet system prompt = the base orchestrator lever rules (reused verbatim) wrapped with the
    site registry, routing rules and conflict precedence."""
    return _FLEET_HEADER.format(registry=_registry_block(sites), base=SYSTEM_PROMPT)


def build_fleet_orchestrator_agent(model, sites: list[SiteContext],
                                   system_prompt: str | None = None) -> Agent:
    """Build the multi-site orchestrator. The site registry is baked into the system prompt, so the
    agent knows the fleet it is routing over."""
    return Agent(model=model, output_type=FleetDirective,
                 system_prompt=system_prompt or compose_fleet_prompt(sites))


# ---------------------------------------------------------------------------
# Apply a grounded FleetDirective to the live fleet
# ---------------------------------------------------------------------------
def _apply_site_directive(d: SiteDirective, ctx: SiteContext) -> list[str]:
    """Apply one per-site directive to its site's policy + host calendar — the SAME five-lever logic
    as the single-site route_intent, scoped to this site."""
    actions: list[str] = []
    posture_changed = (d.priority != "balanced" or d.lyapunov_V is not None
                       or d.lyapunov_W is not None or d.min_servers is not None)
    if posture_changed and ctx.policy is not None:
        base_v, base_w = PRIORITY_VW.get(d.priority, PRIORITY_VW["balanced"])
        v = d.lyapunov_V if d.lyapunov_V is not None else base_v
        w = d.lyapunov_W if d.lyapunov_W is not None else base_w
        ctx.policy.set_operator(lyapunov_V=v, lyapunov_W=w, min_servers=d.min_servers)
        floor = f", min_servers={d.min_servers}" if d.min_servers else ""
        actions.append(f"policy(priority={d.priority}, V={v:.0f}, W={w:.2f}{floor})")

    if d.schedule_event_name and d.schedule_event_t is not None and ctx.host is not None:
        ctx.host.calendar.append(ScheduledEvent(
            t_start=d.schedule_event_t, name=d.schedule_event_name,
            venue=d.schedule_event_venue or "", sold_out=d.schedule_event_sold_out))
        actions.append(f"scheduled '{d.schedule_event_name}'@t={d.schedule_event_t:.0f}s")

    if d.nonrt_instruction and ctx.policy is not None:
        ctx.policy.set_operator_note(d.nonrt_instruction)
        actions.append(f"delegated: \"{d.nonrt_instruction}\"")

    return actions


async def route_fleet_intent(
    intent:       str,
    orchestrator: Agent,
    fleet:        dict[str, SiteContext],
    stats:        RunStats | None = None,
    settings:     dict | None = None,
) -> tuple[str, FleetDirective, list[str]]:
    """Ground a free-text intent into a FleetDirective and apply each per-site directive to its site.

    Returns (human summary, the grounded FleetDirective, list of misrouted site_ids). A directive
    whose site_id is not in the fleet is a MISROUTE: it is reported and NOT applied (never leaks to a
    real site). Applying is skipped for a site left in `conflict` — the escalation is surfaced instead."""
    if stats:
        stats.intents_routed += 1
    print(f"[Fleet] Operator intent: {intent}")

    # per-site current posture, so the model can reason about clashes with standing policy
    ctx_lines = "\n".join(f"- {sid}: {c.policy.context_str()}" for sid, c in fleet.items()
                          if c.policy is not None)
    prompt = f"Operator intent: {intent}\nCurrent fleet posture:\n{ctx_lines}"

    _t0 = time.monotonic()
    result = await orchestrator.run(prompt, model_settings=settings)
    if stats:
        _accumulate_usage(stats, result, time.monotonic() - _t0)
    fd: FleetDirective = result.output

    summary_parts: list[str] = []
    misrouted: list[str] = []
    for sd in fd.site_directives:
        if sd.site_id not in fleet:
            misrouted.append(sd.site_id)
            print(f"[Fleet]   MISROUTE -> unknown site '{sd.site_id}' (not applied)")
            continue
        acts = _apply_site_directive(sd, fleet[sd.site_id])
        if acts:
            summary_parts.append(f"{sd.site_id}: " + "; ".join(acts))
            print(f"[Fleet]   {sd.site_id}: {'; '.join(acts)}")
    if fd.conflict:
        print(f"[Fleet]   CONFLICT (escalated, not applied): {fd.conflict}")
        summary_parts.append(f"conflict: {fd.conflict}")

    return ("  |  ".join(summary_parts) if summary_parts else "no-op"), fd, misrouted
