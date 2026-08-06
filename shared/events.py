"""
Event portfolio for the reserve-sizing experiment (Non-RT judgement justification).

Each VenueEvent is a real scheduled event whose turnout a fixed rule cannot guess. The
job under test is sizing the pre-provisioning reserve from context:
  - the LLM sees the FREE-TEXT description (name, venue, sold-out) and must ESTIMATE the
    attendance from world knowledge + reasoning;
  - the rules get the STRUCTURED fields (capacity, sold_out) — we concede the parsing so the
    comparison is on judgement, not NLP.
The hidden `attendance` is ground truth (see docs/event_portfolio.md for sources); it drives
the simulated surge and scores every arm. It is NEVER shown to any judge.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

NORMAL_LAM   = 20.0     # calm cell baseline (UEs/s), same as the sim scenarios
SURGE_DIVISOR = 300.0   # attendance -> peak surge (UEs/s); calibration constant (cell-share x
                        # 1/peak-window) mapping real crowds onto the sim scale. See the doc.
MU_DEFAULT   = 28.7     # per-server service rate (UEs/s) for standalone reserve math
RHO_TARGET   = 0.8      # target utilisation the reserve is sized to. Sizing to bare capacity
                        # (rho=1) leaves NO headroom: a queue at rho->1 has exploding delay, so
                        # even a perfect crowd estimate would fail QoS. We provision servers so
                        # the surge fills only RHO_TARGET of them, leaving slack — the same
                        # "never park at rho=1" principle the Lyapunov loop uses.


def surge_peak(attendance: float) -> float:
    """Peak signaling surge (UEs/s) a crowd of `attendance` produces at the cell."""
    return attendance / SURGE_DIVISOR


def reserve_for(attendance: float, mu: float = MU_DEFAULT, c_max: int = 16) -> int:
    """Servers to serve the surge from `attendance` WITH headroom: sized so the surge loads the
    reserve to RHO_TARGET (not to the brink), because a reserve at rho=1 still fails QoS. Floored
    at 1 (a tiny event whose surge sits below the calm baseline is a NON-event needing no
    pre-provisioning) and capped at c_max. This same map converts every arm's attendance estimate
    to a reserve, so the comparison is purely on the estimate."""
    return max(1, min(c_max, math.ceil(surge_peak(attendance) / (mu * RHO_TARGET))))


@dataclass(frozen=True)
class VenueEvent:
    name:       str    # agent-visible, e.g. "Aston Villa Women v Brighton, Women's Super League"
    venue:      str    # agent-visible, e.g. "Bescot Stadium, Walsall"
    capacity:   int    # knowable by both (LLM world knowledge; rule lookup table)
    sold_out:   bool   # agent-visible flag
    attendance: int    # HIDDEN ground truth (drives the surge + scores the arms)

    def description(self) -> str:
        """FREE-TEXT the LLM sees. Venue named (not its capacity — the LLM brings that), no
        attendance. Sold-out stated when true."""
        return f"{self.name}, at {self.venue}" + (", sold out" if self.sold_out else "")

    def structured(self) -> dict:
        """STRUCTURED fields the rules get (parsing conceded)."""
        return {"capacity": self.capacity, "sold_out": self.sold_out}

    def surge_peak(self) -> float:
        return surge_peak(self.attendance)

    def ideal_reserve(self, mu: float = MU_DEFAULT, c_max: int = 16) -> int:
        return reserve_for(self.attendance, mu, c_max)


# The 12-event portfolio (see docs/event_portfolio.md for verified figures + sources).
# name, venue, capacity, sold_out, attendance. Deliberately BREAKS the sold-out<->high-fill
# correlation: rows 6-7 are NOT sold out yet nearly full (surprise-high), while rows 10-12 are
# NOT sold out and nearly empty. No single fill fraction serves both, so the formula rule must
# either under-reserve the surprise-high (QoS fail) or over-reserve the low ones (waste); only
# context reasoning handles both.
PORTFOLIO: list[VenueEvent] = [
    # --- sold out (control: the flag => ~full, both arms size it) ---
    VenueEvent("Taylor Swift concert", "Wembley Stadium", 90000, True, 89000),
    VenueEvent("Manchester United v Liverpool, Premier League", "Old Trafford", 74310, True, 73738),
    VenueEvent("Arsenal Women v Manchester United, Women's Super League", "Emirates Stadium", 60704, True, 60160),
    VenueEvent("Liverpool v Real Madrid, Champions League round of 16", "Anfield", 53394, True, 52337),
    VenueEvent("Adele concert", "The O2 Arena", 20000, True, 18750),
    # --- NOT sold out, but nearly FULL (surprise-high: a fixed low fraction under-reserves) ---
    VenueEvent("England v Brazil, international friendly", "Wembley Stadium", 90000, False, 83664),
    VenueEvent("Chelsea v Manchester United, Women's FA Cup final", "Wembley Stadium", 90000, False, 77390),
    # --- NOT sold out, mid fill (low-interest European) ---
    VenueEvent("West Ham v FK TSC, Europa League group stage", "London Stadium", 62500, False, 41374),
    VenueEvent("West Ham v Viborg, Europa Conference League", "London Stadium", 62500, False, 30230),
    # --- NOT sold out, low fill (routine women's league: a fixed high fraction over-reserves) ---
    VenueEvent("Aston Villa Women v Manchester United, Women's Super League", "Villa Park", 42785, False, 12533),
    VenueEvent("Manchester United Women v Brighton, Women's Super League", "Leigh Sports Village", 12000, False, 4060),
    VenueEvent("West Ham Women v Everton, Women's Super League", "Chigwell Construction Stadium, Dagenham", 6078, False, 614),
]

# The event driving exp_1's benign surge (the LLM-judge comparison). England v Brazil: a 90k
# Wembley crowd that did NOT sell out yet ran 93% full — the "not sold out -> discount" trap, so
# a model that pattern-matches under-reserves. The judge sees only "England v Brazil, at Wembley
# Stadium" (no attendance, not flagged sold-out) and must reason the ~84k crowd to size the
# reserve. Referenced by runtime.start (traffic surge + calendar) and exp_1 (ground-truth score).
EXP1_EVENT: VenueEvent = PORTFOLIO[5]
