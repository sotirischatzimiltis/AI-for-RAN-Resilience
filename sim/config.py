# Configuration for the signaling-storm simulator.

from dataclasses import dataclass, field
from typing import List, Optional

# ----------------------------- Architecture / delay model --------------------
@dataclass
class ArchConfig:
    """Per-attach control-plane delay accounting for the UE attach procedure."""
    n_ctrl_messages: int = 3          # M: CU-handled RRC msgs (Setup Req/Setup/Setup Complete)
    proc_total_ms: float = 30.0       # total internal processing (Table VII row "30")
    oneway_delay_ms: float = 1.60     # RU->CU (Open RAN): 0.10 O-FH + 1.50 F1

    def service_time_ms(self) -> float:
        # mean service time of one full attach attempt (ms)
        return self.proc_total_ms + self.n_ctrl_messages * self.oneway_delay_ms

    def service_rate(self) -> float:
        # per-server service rate mu (UEs/s)
        return 1000.0 / self.service_time_ms()

def open_ran_arch(**kw) -> ArchConfig:
    kw.setdefault("oneway_delay_ms", 1.60) # set default but allow override
    return ArchConfig(**kw)

# -------------- RRC timer / retry behaviour  ---------------------------------
@dataclass
class RRCConfig:
    """UE retry behaviour; defaults match the 3GPP spec and the prior paper."""
    t300_ms: float = 1000.0           # RRC setup guard timer (T300). 3GPP allows 100..2000 ms.
    max_attempts: int = 5             # attempts before the UE gives up (failure)
    backoff_ms: float = 500.0 # randomised benign retry backoff (0..backoff_ms)

# ----------------------------- Traffic profile -------------------------------
@dataclass
class TrafficPhase:
    """One constant-rate interval of the traffic timeline."""
    t_start: float                    # start (s, inclusive)
    t_end: float                      # end (s, exclusive)
    benign_rate: float                # benign UE arrivals/s
    botnet_rate: float = 0.0          # malicious attach attempts/s (see SimConfig botnet note)
    label: str = ""                   # for logging / plotting

@dataclass
class TrafficConfig:
    """An ordered, contiguous (sharing a boundary/touch) sequence of TrafficPhases = the whole traffic profile."""
    phases: List[TrafficPhase] = field(default_factory=list)
    baseline_rate: float = 20.0 # explicit calm rate (UEs/s) 

    def __post_init__(self): # validate the phases based on times, rates, and not overlapping or gapped
        for p in self.phases:
            if p.t_end <= p.t_start:
                raise ValueError(f"phase '{p.label}' has t_end <= t_start ({p.t_start}, {p.t_end})")
            if p.benign_rate < 0 or p.botnet_rate < 0:
                raise ValueError(f"phase '{p.label}' has a negative rate")
        for a, b in zip(self.phases, self.phases[1:]):
            if abs(a.t_end - b.t_start) > 1e-9:
                raise ValueError(f"phase gap/overlap between '{a.label}' and '{b.label}': "
                                 f"{a.t_end} -> {b.t_start}")

    def horizon(self) -> float:
        # total scenario duration (largest phase end time)
        return max((p.t_end for p in self.phases), default=0.0)

    def rates_at(self, t: float):
        # active (benign, botnet) rates at time t
        for p in self.phases:
            if p.t_start <= t < p.t_end:
                return p.benign_rate, p.botnet_rate
        return 0.0, 0.0

    # CFG-5: dominating rate lambda* >= lambda(t) for all t. UNUSED stub, reserved for the
    # deferred SIM-4 thinning-based arrival process (Lewis–Shedler algorithm) (needed only for continuously-varying
    # rates; the current piecewise-constant arrivals don't require it). Do not delete.
    def max_rate(self) -> float:
        return max((p.benign_rate + p.botnet_rate for p in self.phases), default=0.0)

    def storm_windows(self) -> list[tuple[float, float]]:
        # (t0, td) per distinct storm: phases above baseline_rate (or any botnet); adjacent merge.
        windows: list[tuple[float, float]] = []
        for p in self.phases:
            elevated = p.benign_rate > self.baseline_rate or p.botnet_rate > 0
            if not elevated:
                continue
            if windows and abs(p.t_start - windows[-1][1]) < 1e-9:
                windows[-1] = (windows[-1][0], p.t_end)   # merge adjacent
            else:
                windows.append((p.t_start, p.t_end))
        return windows

def event_surge_traffic(surge_rate: float, normal: float = 20.0,
                        lead: float = 30.0, surge_dur: float = 60.0, post: float = 90.0) -> TrafficConfig:
    """One scheduled BENIGN surge (a stadium egress / concert let-out) for the reserve-sizing
    experiment: calm -> a `surge_rate` plateau -> recover, no botnet. `surge_rate` is the event's
    peak signaling load (max of the calm baseline and the crowd's contribution, so a tiny event
    is just baseline = a non-event). Each portfolio event is simulated as one of these, sized by
    its ground-truth attendance, to score whether the chosen reserve served it."""
    surge_rate = max(normal, surge_rate)
    return TrafficConfig(baseline_rate=normal, phases=[
        TrafficPhase(0.0, lead, normal, 0.0, "calm"),
        TrafficPhase(lead, lead + surge_dur, surge_rate, 0.0, "surge"),
        TrafficPhase(lead + surge_dur, lead + surge_dur + post, normal, 0.0, "recover"),
    ])


def botnet_event_traffic(event_surge: float, normal: float = 20.0, benign_during_botnet: float = 30.0,
                         botnet_peak: float = 250.0, lead: float = 60.0, ramp: float = 30.0,
                         hold: float = 30.0, gap: float = 120.0, ramp_steps: int = 6) -> TrafficConfig:
    """The exp_1 (LLM-judge comparison) scenario: TWO storms of opposite nature so the judge
    must use BOTH anticipation tools and reason, not pattern-match on volume.
      STORM-1 MALICIOUS botnet, RAMP onset — benign users carry on at `benign_during_botnet`
        while a botnet floods to `botnet_peak` over `ramp`s (a staircase) then holds. The rising,
        UNSCHEDULED climb is what get_forecast catches -> the judge should filter + add headroom.
      STORM-2 BENIGN event, STEP onset — pure real users at `event_surge` UEs/s (sized to a real
        event's attendance/300), no botnet, jumping instantly to peak. A step overruns a reactive
        loop, so the ONLY way to serve it is to PRE-provision from get_calendar — which means the
        judge must estimate the crowd behind the named event and size the reserve. No filtering.
    `botnet_peak` is set so the botnet TOTAL (benign+botnet) is comparable to the event surge, so
    the judge cannot separate malicious from benign on arrival volume alone."""
    phases = [TrafficPhase(0.0, lead, normal, 0.0, "calm-1")]
    t = lead
    step_dt = ramp / ramp_steps
    for k in range(1, ramp_steps + 1):                       # botnet staircase 0 -> peak
        frac = k / ramp_steps
        phases.append(TrafficPhase(t, t + step_dt, benign_during_botnet, botnet_peak * frac,
                                   f"botnet-ramp.{k}")); t += step_dt
    phases.append(TrafficPhase(t, t + hold, benign_during_botnet, botnet_peak, "botnet-storm")); t += hold
    phases.append(TrafficPhase(t, t + gap, normal, 0.0, "recover-1")); t += gap
    # STORM-2: benign event surge, STEP onset (instant to peak), no botnet, same elevated span.
    phases.append(TrafficPhase(t, t + ramp + hold, max(normal, event_surge), 0.0, "event-surge"))
    t += ramp + hold
    phases.append(TrafficPhase(t, t + gap, normal, 0.0, "recover-2")); t += gap
    return TrafficConfig(baseline_rate=normal, phases=phases)

# ----------------------------- Top-level sim config --------------------------
@dataclass
class SimConfig:
    arch: ArchConfig = field(default_factory=open_ran_arch)
    rrc: RRCConfig = field(default_factory=RRCConfig)
    traffic: TrafficConfig = field(default_factory=TrafficConfig)
    
    c0: int = 2                       # initial number of servers, for lower utilization starting point
    c_max: int = 16                   # max servers the actuator may allocate
    telemetry_dt_s: float = 1.0       # telemetry sampling interval (s)
    control_dt_s: float = 1.0         # controller invocation interval (s)
    seed: int = 0
    # --- botnet model ---
    botnet_attach_period_ms: float = 200.0   # bot's impatient attach timeout (vs benign T300)
    benign_fp_alpha: float = 0.05 # fraction of benign traffic dropped by the filter, relative to the fraction of malicious traffic dropped

    # for simulation realtime or not (1.0 real, 0.1 = 10x faster, 2.0 = 2x slower), used by the GUI.
    realtime: bool = False
    rt_factor: float = 1.0
    # --- shared-compute contention (load-dependent processing time) ---
    # vCU/vDU on a finite compute pool: PROCESSING time inflates by 1/(1 - rho_c), where
    # rho_c = (busy workers)/compute_kappa. Propagation delay is unaffected.
    #   compute_kappa = None -> contention OFF (recovers the paper's numbers)
    #   compute_kappa = K    -> pool runs ~K attach-workers at full speed (use 40..60)
    #   compute_rho_cap      -> clamp rho_c < 1 to avoid the infinite pole
    compute_kappa: Optional[float] = None
    compute_rho_cap: float = 0.98
    # --- server provisioning delay ---
    # Seconds to bring a new vDU/vCU online (image pull/boot/attach), one at a time;
    # scale-down is immediate. THE parameter that makes control non-trivial: at 0.0 capacity
    # appears instantly and set_servers(c_max) at t=0 is trivially optimal; at 5.0s an
    # anticipatory arm that provisions BEFORE the surge beats a reactive one. 0.0 recovers
    # the prior paper's instant-capacity model.
    server_provision_delay_s: float = 5.0
    # How multiple pending servers spin up. False (default) = SERIAL: one server comes online
    # per server_provision_delay_s, so c=1 -> c=4 takes 3*delay. True = PARALLEL: all pending
    # servers boot together, so any jump costs a single delay. Lets us separate the delay's
    # cost from the serial-spin-up cost.
    parallel_provision: bool = False

    def __post_init__(self): # sanity checks on the config values
        # rho_cap must stay strictly below 1: service time inflates by 1/(1 - rho_c) and
        # rho_c is capped here, so cap >= 1 would divide by zero or go negative.
        if not (0.0 <= self.compute_rho_cap < 1.0):
            raise ValueError(f"compute_rho_cap must be in [0, 1) (got {self.compute_rho_cap})")
        # SIM-7: kappa must exceed c_max, else rho_c hits the pole at full occupancy.
        if self.compute_kappa is not None and self.compute_kappa <= self.c_max:
            raise ValueError(f"compute_kappa ({self.compute_kappa}) must exceed c_max "
                             f"({self.c_max}); use 40..60")
        if not (0.0 <= self.benign_fp_alpha <= 1.0):
            raise ValueError(f"benign_fp_alpha must be in [0, 1] (got {self.benign_fp_alpha})")
        if self.c0 > self.c_max:
            raise ValueError(f"c0 ({self.c0}) exceeds c_max ({self.c_max})")

# ----------------------------- Scenario builders -----------------------------
def single_storm_traffic(normal=20.0, storm=200.0,
                         t_pre=50.0, t_storm=60.0, t_post=900.0) -> TrafficConfig:
    """The prior paper's scenario: 20 -> 200 -> 20 UEs/s."""
    return TrafficConfig(baseline_rate=normal, phases=[
        TrafficPhase(0.0, t_pre, normal, 0.0, "pre"),
        TrafficPhase(t_pre, t_pre + t_storm, storm, 0.0, "storm"),
        TrafficPhase(t_pre + t_storm, t_pre + t_storm + t_post, normal, 0.0, "recovery"),
    ])


def single_ramp_traffic(normal=20.0, peak=200.0, t_pre=50.0, ramp=30.0, hold=30.0,
                        t_post=900.0, ramp_steps=6) -> TrafficConfig:
    """The RAMP twin of single_storm_traffic: benign load rises from `normal` to `peak` over
    `ramp`s as a staircase of `ramp_steps` constant sub-phases, holds for `hold`s, then recovers.
    Same `peak` and same elevated span (ramp+hold = the plateau of single_storm) as the step
    scenario, so the two differ ONLY in onset shape (gradual vs instant). This isolates how the
    load's RATE OF CHANGE interacts with V/W tuning and the server provisioning delay: a step
    overruns a reactive loop while a ramp lets it track. No botnet — a pure capacity study,
    filtering is irrelevant here."""
    phases = [TrafficPhase(0.0, t_pre, normal, 0.0, "pre")]
    t = t_pre
    step_dt = ramp / ramp_steps
    for k in range(1, ramp_steps + 1):                     # staircase normal -> peak
        b = normal + (peak - normal) * (k / ramp_steps)
        phases.append(TrafficPhase(t, t + step_dt, b, 0.0, f"ramp.{k}")); t += step_dt
    phases.append(TrafficPhase(t, t + hold, peak, 0.0, "hold")); t += hold
    phases.append(TrafficPhase(t, t + t_post, normal, 0.0, "recovery"))
    return TrafficConfig(baseline_rate=normal, phases=phases)


def multi_storm_flat_traffic(benign=180.0, botnet=60.0, normal=20.0,
                             lead=60.0, storm=60.0, gap=120.0,
                             n_storms=3) -> TrafficConfig:
    # Create N identical storms of the same duration and intensity, with a calm baseline before and after.
    phases = [TrafficPhase(0.0, lead, normal, 0.0, "calm-1")]
    t = lead
    for i in range(1, n_storms + 1):
        phases.append(TrafficPhase(t, t + storm, benign, botnet, f"storm-{i}")); t += storm
        phases.append(TrafficPhase(t, t + gap, normal, 0.0, f"recover-{i}"));    t += gap
    return TrafficConfig(baseline_rate=normal, phases=phases)


def multi_storm_ramp_traffic(peak_benign=180.0, peak_botnet=60.0, normal=20.0,
                             lead=60.0, ramp=30.0, hold=30.0, gap=120.0,
                             n_storms=3, ramp_steps=6) -> TrafficConfig:
    """Like multi_storm_flat, but each storm RAMPS up over `ramp` seconds (a staircase of
    `ramp_steps` constant sub-phases) before a `hold` plateau, then recovers. The gradual
    rise gives a linear forecast something to catch — unlike the instant step of
    multi_storm_flat_traffic — so it exercises anticipatory pre-provisioning. Botnet ramps
    alongside benign. The elevated span per storm (ramp + hold) defaults to 60s, matching
    the flat scenario, so the two are directly comparable."""
    phases = [TrafficPhase(0.0, lead, normal, 0.0, "calm-1")]
    t = lead
    step_dt = ramp / ramp_steps
    for i in range(1, n_storms + 1):
        for k in range(1, ramp_steps + 1):                 # staircase normal -> peak
            frac = k / ramp_steps
            b    = normal + (peak_benign - normal) * frac
            bot  = peak_botnet * frac
            phases.append(TrafficPhase(t, t + step_dt, b, bot, f"ramp-{i}.{k}")); t += step_dt
        phases.append(TrafficPhase(t, t + hold, peak_benign, peak_botnet, f"storm-{i}")); t += hold
        phases.append(TrafficPhase(t, t + gap, normal, 0.0, f"recover-{i}"));              t += gap
    return TrafficConfig(baseline_rate=normal, phases=phases)


# Storm specs for the mixed scenario: (benign_peak, botnet_peak) in UEs/s (calm baseline 20).
# Storm-2 is always the BENIGN surge — a stadium egress / mass reconnection — so benign is
# HIGH (a genuine ~9x spike of real users) with botnet 0; it is the only event on the
# calendar. Storms 1 and 3 are MALICIOUS: benign stays in the NORMAL range (real users carry
# on roughly as usual) and a botnet FLOOD drives the surge. The agent sees only the TOTAL
# arrival rate (benign+botnet), which is high for both kinds, so it cannot separate them on
# volume alone — it needs the calendar / retry signature. Two intensity profiles:
#   FLAT — the two botnet storms are equal.
#   INCREASING — the botnet storms grow (mild then severe), so the agent faces a rising
#     sequence and must keep scaling its filter and its headroom.
_MIXED_SPECS_FLAT = [(30.0, 180.0),    # storm-1: malicious (normal benign + botnet flood)
                     (180.0, 0.0),     # storm-2: BENIGN surge (stadium egress, on the calendar)
                     (30.0, 180.0)]    # storm-3: malicious
_MIXED_SPECS_INC  = [(30.0, 120.0),    # storm-1: malicious, mild
                     (180.0, 0.0),     # storm-2: BENIGN surge (stadium egress, on the calendar)
                     (30.0, 240.0)]    # storm-3: malicious, severe
BENIGN_SURGE_IDX = 1                   # storm-2 is the benign surge (0-based)


def mixed_storm_traffic(ramped: bool = False, increasing: bool = False, normal: float = 20.0,
                        storm: float = 60.0, gap: float = 120.0, lead: float = 60.0,
                        ramp: float = 30.0, ramp_steps: int = 6) -> TrafficConfig:
    """ONE scenario, THREE storms of mixed nature on a single timeline: two malicious (botnet)
    storms bracketing one BENIGN surge (no botnet). The benign surge is the only event placed
    on the operator calendar (runtime.start registers it), so we can watch the agent call
    get_calendar and pre-provision for it WITHOUT filtering, while it filters the two botnet
    storms. Two independent axes give four versions:
      onset — step (ramped=False): each storm jumps instantly to peak, so a telemetry-only
        forecast is blind and the CALENDAR is the only anticipation signal (for the benign
        surge); ramp (ramped=True): each storm rises over `ramp`s (a staircase of `ramp_steps`
        sub-phases) then holds, giving the get_forecast tool a rising trend to catch too.
      intensity — flat (increasing=False, _MIXED_SPECS_FLAT): equal botnet storms; increasing
        (increasing=True, _MIXED_SPECS_INC): each botnet storm larger than the last.
    The elevated span per storm (`storm`s, split ramp+hold when ramped) is identical across the
    versions so their timelines align and P is comparable."""
    specs = _MIXED_SPECS_INC if increasing else _MIXED_SPECS_FLAT
    phases = [TrafficPhase(0.0, lead, normal, 0.0, "calm-1")]
    t = lead
    for i, (b_peak, bot_peak) in enumerate(specs, start=1):
        if ramped:
            step_dt = ramp / ramp_steps
            for k in range(1, ramp_steps + 1):            # staircase normal -> peak
                frac = k / ramp_steps
                b    = normal + (b_peak - normal) * frac
                bot  = bot_peak * frac
                phases.append(TrafficPhase(t, t + step_dt, b, bot, f"ramp-{i}.{k}")); t += step_dt
            hold = storm - ramp
            phases.append(TrafficPhase(t, t + hold, b_peak, bot_peak, f"storm-{i}")); t += hold
        else:
            phases.append(TrafficPhase(t, t + storm, b_peak, bot_peak, f"storm-{i}")); t += storm
        phases.append(TrafficPhase(t, t + gap, normal, 0.0, f"recover-{i}")); t += gap
    return TrafficConfig(baseline_rate=normal, phases=phases)
