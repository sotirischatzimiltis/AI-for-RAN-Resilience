"""
Operator-intent portfolio for Experiment 6, Fold 1 (intent grounding).

Each IntentCase is a plain-language operator intent plus the ground-truth control decision it should
map to. The job under test is the Orchestrator (agents/orchestrator.py) translating free text into an
OperatorDirective: the network posture (priority / V / W), an SLA floor (min_servers), a scheduled
event (name + time), and/or a standing instruction delegated to the site judge (nonrt_instruction).

This mirrors the event portfolio of Experiment 3 (shared/events.py): the LLM sees only the free text
and must ground it, while we score its structured output against a hidden key. The portfolio spans
pure-posture, SLA-floor, scheduling, judge-delegation, several COMBINED intents that exercise more
than one lever at once, one lexical TRAP (a cost word in a QoS intent), and one NULL intent that must
touch nothing. A second, harder tranche probes generalisation and robustness: oblique wording with no
obvious keyword, a MIRROR trap (a quality word inside a cost intent), a number that is NOT a floor (a
ticket id), a soft-quantity floor ("a couple"), a named event with NO usable time (must not be
scheduled), a tempting-but-vague intent that must touch nothing, and six NOISY items carrying spelling
and grammatical errors that preserve meaning. A GRADED tranche asks for a strength of posture (maximal
vs a mild tempered lean) so the model must set an explicit intermediate weight rather than snap to a
preset; these are scored on direction plus a lenient V/W magnitude band. The module constant
SCHEDULE_TOL_S is the tolerance on the extracted event time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class IntentCase:
    text:        str            # the free-text intent the Orchestrator sees
    category:    str            # grouping for the report (not shown to the model)
    priority:    str | None     # expected priority (qos/cost/balanced); None = do not score priority
    min_servers: int | None     # expected SLA floor; None = expect NO floor
    schedule_t:  float | None   # expected event time (s); None = expect NO scheduled event
    needs_nonrt: bool           # True = a judge delegation is expected; False = none
    # Optional MAGNITUDE band for graded postures: when set, the effective V (or W) must land in
    # [lo, hi]. Lenient, not exact (the operator resolved earlier that one fixed pair is not the only
    # correct config). Two levels, matching the two the controller's threshold response can realise
    # (see exp6_posture): MILD/tempered = [2, 10] (a hedged lean the model must express with an
    # explicit intermediate weight, since riding the qos/cost preset lands at 20 and MISSES this band),
    # and FULL/maximal = [10, 20] (the preset already satisfies it, so a plain or maximal ask needs no
    # explicit weight). One band per item (v_band for qos strength, w_band for cost strength).
    v_band: tuple[float, float] | None = None
    w_band: tuple[float, float] | None = None


SCHEDULE_TOL_S = 60.0   # allowed error on the extracted event time

# Mirrors agents/orchestrator.py PRIORITY_VW: the (V, W) a bare priority resolves to when the model
# leaves the explicit weights null. Kept here (not imported) so this module stays dependency-light;
# the runner (scripts/exp_6_intents.py) asserts this equals the orchestrator's dict at start-up so a
# preset drift cannot silently mis-score posture.
_PRIORITY_VW = {"qos": (20.0, 1.0), "cost": (1.0, 20.0), "balanced": (1.0, 1.0)}


# HELD-OUT test set: deliberately disjoint from the prompt's few-shot examples (which use a product
# launch, quarterly spend, a floor of three, a maintenance drain at t=200, and a sale) so the score
# measures generalisation, not recall of the examples. Different scenarios, wordings, and values.
PORTFOLIO: list[IntentCase] = [
    # --- pure posture ---
    IntentCase("The regional cup final is on tonight, keep every user connected whatever the load.",
               "posture-qos", "qos", None, None, False),
    IntentCase("We are being audited on infrastructure cost, run as lean as you safely can.",
               "posture-cost", "cost", None, None, False),
    # --- SLA floor ---
    IntentCase("Hold at least six servers for the 999 emergency line at all times.",
               "sla-floor", "balanced", 6, None, False),
    # --- scheduling ---
    IntentCase("The arena doors open at t=180 seconds and there will be a rush to connect.",
               "schedule", "balanced", None, 180.0, False),
    # --- judge delegation ---
    IntentCase("If load jumps when the exam results publish, that is students refreshing, not an attack.",
               "delegate", "balanced", None, None, True),
    IntentCase("When you are unsure, err on the side of admitting legitimate users rather than blocking them.",
               "delegate-caution", "balanced", None, None, True),
    # --- combined (two or more levers) ---
    IntentCase("Keep at least five servers online and lean toward protecting service quality tonight.",
               "combo-qos-sla", "qos", 5, None, False),
    IntentCase("The New Year countdown surge at t=650 is expected and legitimate, get ready for it "
               "and do not filter it.",
               "combo-schedule-delegate", "balanced", None, 650.0, True),
    IntentCase("Run as cheaply as you can while keeping at least two servers warm.",
               "combo-cost-sla", "cost", 2, None, False),
    IntentCase("This is a VIP broadcast, maximise protection, and there is a scheduled mass sign-on "
               "at t=360 seconds.",
               "combo-qos-schedule", "qos", None, 360.0, False),
    # --- lexical trap: a cost word inside a QoS intent (worded so it shares no phrase with the
    #     prompt's cost-waiver examples, keeping this item genuinely held out) ---
    IntentCase("Budget is not the issue tonight, we cannot have dropped calls during the broadcast.",
               "trap-qos", "qos", None, None, False),
    # --- null: business as usual, must touch nothing ---
    IntentCase("Nothing unusual is planned, just keep things ticking over as normal.",
               "null", "balanced", None, None, False),

    # =====================================================================================
    # HARD tranche: oblique wording, traps that a keyword matcher gets wrong, and items whose
    # correct answer is to do LESS than the surface suggests. Ground truth follows the prompt's
    # own rules (no invented numbers, delegation != posture, no event without a usable time).
    # =====================================================================================
    # mirror trap: a QUALITY word ("service") inside a genuine COST intent — the quality is waived
    IntentCase("Service can take a small hit tonight, what matters is cutting the compute bill.",
               "trap-cost", "cost", None, None, False),
    # oblique posture: no qos keyword; "busy signal" implies dropped calls -> favour service
    IntentCase("Nobody should get a busy signal during the telethon.",
               "oblique-qos", "qos", None, None, False),
    # oblique posture: no cost keyword; "bleeding money on idle capacity" -> run leaner
    IntentCase("We are bleeding money on idle capacity, tighten things up.",
               "oblique-cost", "cost", None, None, False),
    # floor trap: the number (4471) is a ticket id, not a server count -> NO floor; "must not drop" -> qos
    IntentCase("Hold the line for our platinum SLA, ticket 4471 must not drop.",
               "trap-floor-id", "qos", None, None, False),
    # soft-quantity floor: "a couple ... warm" is floor language resolving to 2 (keyword can't parse it)
    IntentCase("Keep a couple of servers warm just in case.",
               "soft-floor", "balanced", 2, None, False),
    # event named but NO usable time -> must NOT schedule; "genuine demand" -> delegate to the judge
    IntentCase("A crowd will pile onto the cell after the concert, that is genuine demand, but I "
               "cannot give you an exact time.",
               "schedule-notime", "balanced", None, None, True),
    # tempting-but-vague: reassurance with no actionable instruction -> touch nothing
    IntentCase("Things feel a bit busier than usual lately, keep an eye on it.",
               "vague-tempting", "balanced", None, None, False),

    # =====================================================================================
    # NOISY tranche: spelling and grammatical errors that PRESERVE meaning. Ground truth is the
    # same as the clean equivalent; several are worded so the noise breaks a naive keyword matcher
    # (a typo'd "connected", a garbled "not an attack") while a capable model still grounds them.
    # =====================================================================================
    IntentCase("keep evry user conected 2nite no mater what, big game on.",
               "noisy-qos", "qos", None, None, False),
    IntentCase("we needs minimum four server up at all time for the emergency line.",
               "noisy-floor", "balanced", 4, None, False),
    IntentCase("budget tight this month pls run things as lean as posible.",
               "noisy-cost", "cost", None, None, False),
    IntentCase("doors is opening at t=220, big rush expect.",
               "noisy-schedule", "balanced", None, 220.0, False),
    IntentCase("when the results come out ppl just refresh alot, its not a attack, dont block em.",
               "noisy-delegate", "balanced", None, None, True),
    IntentCase("vip stream 2nite, maximis protection, also scheduld mass signon at t=400.",
               "noisy-combo", "qos", None, 400.0, False),

    # =====================================================================================
    # GRADED tranche: the intent asks for a STRENGTH of posture. Scored on direction AND a lenient V/W
    # band. Two levels (the two the controller realises): MILD = [2,10], which the model must express
    # with an explicit intermediate weight since riding the preset lands at 20 and MISSES the band;
    # MAXIMAL/full = [10,20], which the preset already satisfies. So mild items test tempering, maximal
    # items test that the model does NOT temper a full ask.
    # =====================================================================================
    IntentCase("This is the national broadcast, give it maximum protection and spare no capacity.",
               "grade-qos-max", "qos", None, None, False, v_band=(10.0, 20.0)),
    IntentCase("Lean a little toward keeping users on, nothing drastic, we still watch the budget.",
               "grade-qos-mild", "qos", None, None, False, v_band=(2.0, 10.0)),
    IntentCase("Cut compute to the bone tonight, we are well over our power budget.",
               "grade-cost-max", "cost", None, None, False, w_band=(10.0, 20.0)),
    IntentCase("Trim spend a bit where you safely can, but keep the service solid.",
               "grade-cost-mild", "cost", None, None, False, w_band=(2.0, 10.0)),
    IntentCase("Absolute priority is the emergency broadcast, throw everything at keeping it up.",
               "grade-qos-max2", "qos", None, None, False, v_band=(10.0, 20.0)),
    IntentCase("Give service a gentle nudge upward for the evening peak, but stay sensible on resources.",
               "grade-qos-mild2", "qos", None, None, False, v_band=(2.0, 10.0)),
    IntentCase("We are in a hard freeze, strip capacity right down to the minimum you dare.",
               "grade-cost-max2", "cost", None, None, False, w_band=(10.0, 20.0)),
    IntentCase("Ease off compute slightly overnight, just shave a bit, nothing aggressive.",
               "grade-cost-mild2", "cost", None, None, False, w_band=(2.0, 10.0)),
    # graded + noisy: a mild lean expressed through spelling errors
    IntentCase("push servis up a notch for the crowd but dont go overbord, budget still tight.",
               "grade-qos-mild-noisy", "qos", None, None, False, v_band=(2.0, 10.0)),
    # graded adversarial: a waiver phrase ("whatever it costs") that the operator then OVERRIDES with
    # "overkill" -> stays a MILD lean, not the maximal posture the phrase would usually imply
    IntentCase("A modest lean toward users is enough, whatever it costs is overkill here.",
               "grade-qos-mild-trap", "qos", None, None, False, v_band=(2.0, 10.0)),
    # graded + floor combo: mild cost lean AND a hard floor
    IntentCase("Run leaner than usual but not to the extreme, and keep at least three servers.",
               "grade-cost-mild-floor", "cost", 3, None, False, w_band=(2.0, 10.0)),
    # graded + schedule combo: maximal qos AND a scheduled event
    IntentCase("Maximum protection for the launch, and schedule the sign-on wave at t=500.",
               "grade-qos-max-sched", "qos", None, 500.0, False, v_band=(10.0, 20.0)),

    # =====================================================================================
    # EXTRA coverage: a posture+delegation combo, a high plain floor, and a delegation in the
    # 'treat as suspicious' direction (the opposite polarity to the benign-flag delegations above).
    # =====================================================================================
    IntentCase("Protect service hard tonight and treat the fan-club sign-on as legitimate traffic.",
               "combo-qos-delegate", "qos", None, None, True),
    IntentCase("Never drop below eight servers on the trading floor line.",
               "sla-floor-high", "balanced", 8, None, False),
    IntentCase("If you see a burst of identical re-registrations from one prefix, treat that as hostile.",
               "delegate-suspicious", "balanced", None, None, True),
]


# ---------------------------------------------------------------------------
# Scoring: compare one OperatorDirective against the ground-truth IntentCase.
# ---------------------------------------------------------------------------
def score_directive(case: IntentCase, d) -> dict:
    """Per-lever correctness of the Orchestrator's OperatorDirective `d` against the key `case`.
    Returns a dict of bool checks plus `n` (checks applied) and `correct` (how many passed). A lever
    is scored only when it is meaningful for the case, and hallucinated levers count as wrong."""
    checks: dict[str, bool] = {}

    # posture: score the EFFECTIVE V/W DIRECTION, not the label or the exact numbers. Any setting
    # that provisions more (V>W) is a valid "favour QoS", any that provisions fewer (W>V) a valid
    # "favour cost", and equal weights are "balanced" — whether the model expressed it via `priority`
    # or via explicit lyapunov_V/W. The effective weights are resolved exactly as the Orchestrator
    # does (priority -> PRIORITY_VW, with any explicit override winning). So 20/1 is NOT the only
    # correct QoS config; 15/1, 12/2, or balanced+V=15 all count.
    if case.priority is not None:
        base_v, base_w = _PRIORITY_VW.get(d.priority, (1.0, 1.0))
        v = d.lyapunov_V if d.lyapunov_V is not None else base_v
        w = d.lyapunov_W if d.lyapunov_W is not None else base_w
        if case.priority == "qos":
            checks["posture"] = v > w
        elif case.priority == "cost":
            checks["posture"] = w > v
        else:                              # balanced: no directional lean (tolerant float compare)
            checks["posture"] = math.isclose(v, w)

        # graded magnitude (only for items that specify a band): the effective weight the operator
        # asked for must land in the lenient band. A mild lean that just rides the preset (V or W = 20)
        # falls outside the mild band and fails here, which is the point of the graded item.
        if case.v_band is not None:
            lo, hi = case.v_band
            checks["weight"] = lo <= v <= hi
        elif case.w_band is not None:
            lo, hi = case.w_band
            checks["weight"] = lo <= w <= hi

    # SLA floor: exact match, and None must stay None (no hallucinated floor)
    checks["min_servers"] = (d.min_servers == case.min_servers)

    # schedule: presence must match; when expected, the time must land within tolerance
    has_sched = d.schedule_event_t is not None and bool(d.schedule_event_name)
    if case.schedule_t is None:
        checks["schedule"] = not has_sched
    else:
        checks["schedule"] = has_sched and abs(d.schedule_event_t - case.schedule_t) <= SCHEDULE_TOL_S

    # judge delegation: a standing instruction present iff one is expected
    checks["nonrt"] = bool(d.nonrt_instruction) == case.needs_nonrt

    correct = sum(checks.values())
    return {**checks, "n": len(checks), "correct": correct, "exact": correct == len(checks)}
