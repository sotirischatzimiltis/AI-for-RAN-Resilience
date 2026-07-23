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

def multi_storm_traffic() -> TrafficConfig:
    # Three storms of growing intensity with a malicious component.
    return TrafficConfig(baseline_rate=20.0, phases=[
        TrafficPhase(0,    60,   20,  0,  "calm-1"),
        TrafficPhase(60,   120,  120, 40, "storm-1"),
        TrafficPhase(120,  420,  20,  0,  "recover-1"),
        TrafficPhase(420,  480,  180, 60, "storm-2"),
        TrafficPhase(480,  780,  20,  0,  "recover-2"),
        TrafficPhase(780,  840,  220, 80, "storm-3"),
        TrafficPhase(840, 1100,  20,  0,  "recover-3"),
    ])

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
