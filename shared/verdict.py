"""
Verdict — ONE decision record emitted by BOTH judges (the deterministic rule controller and
the LLM agent), so agreement/divergence analysis is a join on shared fields rather than a
bespoke per-arm script.

The DECISION fields (storm_active, malicious_drop_prob, lyapunov_V, reserve_servers, tighten)
are the policy levers both arms produce — these are the join keys. The EVIDENCE fields (rest,
severity, elevated, calendar_hit, forecast_rising, absorption) are what drove the call; the
rule fills them exactly, the agent may leave them at defaults (its reasoning is free text).
Keeping them on one record lets a shadow run line the two judges up cycle-by-cycle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Verdict:
    t: float                       # sim time of the assessment
    source: str                    # "rule" | "agent"
    # --- decision (the policy levers — the join keys for agreement) ---
    storm_active:        bool
    malicious_drop_prob: float
    lyapunov_V:          float
    reserve_servers:     int
    tighten:             bool
    # --- evidence (why; rule fills exactly, agent leaves at defaults) ---
    latest_lam:      float = 0.0
    resting_lam:     float = 0.0
    severity:        float = 0.0
    elevated:        bool  = False
    calendar_hit:    bool  = False
    forecast_rising: bool  = False
    absorption:      float = 1.0
    reasoning:       str   = ""

    def agrees_with(self, other: "Verdict") -> bool:
        """Same storm call AND same filter engagement — the coarse agreement test. (Drop
        magnitude / V / reserve closeness are separate, finer comparisons.)"""
        return (self.storm_active == other.storm_active
                and (self.malicious_drop_prob > 0) == (other.malicious_drop_prob > 0))
