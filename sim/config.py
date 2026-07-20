# Configuration for the signaling-storm simulator.

from dataclasses import dataclass, field
from typing import List, Literal

# ----------------------------- Architecture / delay model --------------------
@dataclass
class ArchConfig:
    # Per-message control-plane delay accounting for the UE attach procedure.
    # attribute name: datatype = default value  # comment describing the field
    n_ctrl_messages: int = 3          # M : CU-handled RRC msgs (Setup Req/Setup/Setup Complete) field
    proc_total_ms: float = 30.0       # sum_i t_p,i : total internal processing (Table VII row "30") field
    oneway_delay_ms: float = 1.60     # RU->CU (Open RAN): 0.10 O-FH + 1.50 F1. Use 0.25 for monolithic. field

    def service_time_ms(self) -> float:
        return self.proc_total_ms + self.n_ctrl_messages * self.oneway_delay_ms  #Mean service time of one full attach attempt (ms)

    def service_rate(self) -> float:
        return 1000.0 / self.service_time_ms() #Per-server service rate mu (UEs/s)
    
# Functions that call the dataclass Constructors for Open RAN and Monolithic
def open_ran_arch(**kw) -> ArchConfig:
    return ArchConfig(oneway_delay_ms=1.60, **kw)

def monolithic_arch(**kw) -> ArchConfig:
    return ArchConfig(oneway_delay_ms=0.25, **kw)

# -------------- RRC timer / retry behaviour  ---------------------------------
@dataclass
class RRCConfig:
    # contains 3 fields that control the RRC retry behaviour of UEs in the simulation.
    #  The default values are chosen to match the 3GPP spec and the prior paper's assumptions.
    t300_ms: float = 1000.0           # RRC setup guard timer (T300). 3GPP allows 100..2000 ms.
    max_attempts: int = 5             # attempts before the UE gives up (failure)
    backoff_ms: float = 0.0           # extra wait before a retry (0 = immediate re-attach)

# -------- StormPhase dataclass-----------------------------------------------
@dataclass
class StormPhase:
    # Define a traffic phase: start time, end time, benign and botnet arrival rates.
    t_start: float # Start time of the phase (seconds, inclusive)
    t_end: float # End time of the phase (seconds, exclusive)
    benign_rate: float                # benign UE arrivals/s
    botnet_rate: float = 0.0          # malicious UE arrivals/s (repeated attach)
    label: str = "" # Optional label for the phase (for logging / plotting)

# --------TrafficConfig dataclass-----------------------------------------------
@dataclass
class TrafficConfig:
    """
    Defines the complete traffic profile for a simulation.
    The traffic profile consists of a sequence of `StormPhase` objects that
    describe how benign and malicious arrival rates evolve over time. The
    configuration provides helper methods for determining the simulation
    duration and retrieving the active traffic rates at a given time.
    """
    # Ordered list of phases, each with its own benign and botnet arrival rates.
    phases: List[StormPhase] = field(default_factory=list)

    # Total scenario duration, find the largest phase end tim (horizon).
    def horizon(self) -> float:
        return max((p.t_end for p in self.phases), default=0.0)

    # Return the active benign and botnet arrival rates at a given time t.
    def rates_at(self, t: float):
        for p in self.phases:
            if p.t_start <= t < p.t_end:
                return p.benign_rate, p.botnet_rate
        return 0.0, 0.0

    # Return the (t0, td) windows for each distinct storm — phases with load elevated, merge storms that are adjacent.
    def storm_windows(self) -> list[tuple[float, float]]:
        if not self.phases:
            return []
        baseline = min(p.benign_rate for p in self.phases) # find the lowest benign rate (baseline)
        windows: list[tuple[float, float]] = [] # list of (start, end) tuples for elevated phases
        for p in self.phases:
            elevated = p.benign_rate > baseline or p.botnet_rate > 0
            if not elevated:
                continue
            if windows and abs(p.t_start - windows[-1][1]) < 1e-9:
                windows[-1] = (windows[-1][0], p.t_end)   # merge adjacent
            else:
                windows.append((p.t_start, p.t_end))
        return windows
    
# ------- SimConfig Dataclass # Top-level simulation config---------------------------------------
@dataclass
class SimConfig:
    arch: ArchConfig = field(default_factory=open_ran_arch)
    rrc: RRCConfig = field(default_factory=RRCConfig)
    traffic: TrafficConfig = field(default_factory=TrafficConfig)
    c0: int = 1                       # initial number of servers
    c_max: int = 16                   # max servers the actuator may allocate
    lq_max: float = 7000.0            # queue length at which utility uB -> 0
    sample_dt_s: float = 0.5          # telemetry sampling interval (s)
    seed: int = 0
    # malicious UEs are flagged in telemetry; a rate-limit actuator may drop them
    botnet_attach_period_ms: float = 200.0   # how often a botnet UE re-attaches when admitted
    # realtime=False -> virtual time, runs as fast as possible (use for experiments).
    # rt_factor = wall-secs per sim-sec: 1.0 real, 0.1 = 10x faster, 2.0 = 2x slower (GUI).
    realtime: bool = False
    rt_factor: float = 1.0
    # --- shared-compute contention (load-dependent processing time) ---
    # vCU/vDU on a finite compute pool: PROCESSING time inflates by 1/(1 - rho_c),
    # where rho_c = (busy workers)/compute_kappa. Propagation delay is unaffected.
    #   compute_kappa = None -> contention OFF (recovers the paper's numbers)
    #   compute_kappa = K    -> pool runs ~K attach-workers at full speed
    #   compute_rho_cap      -> clamp rho_c < 1 to avoid the infinite pole
    compute_kappa: float = None
    compute_rho_cap: float = 0.98
    # --- server provisioning delay ---
    # Seconds to bring a new vDU/vCU online (image pull/boot/pool attach); they come
    # up one at a time. 0.0 (default) = instant, matching the paper. Scale-down is
    # always immediate (no preemption of in-flight attaches).
    server_provision_delay_s: float = 0.0

    def __post_init__(self):
        # compute_rho_cap must stay strictly below 1: service time inflates by
        # 1/(1 - rho_c) and rho_c is capped at this value, so cap >= 1 would divide
        # by zero (==1) or produce negative service times (>1). Fail loud on misconfig.
        if not (0.0 <= self.compute_rho_cap < 1.0):
            raise ValueError(f"compute_rho_cap must be in [0, 1) (got {self.compute_rho_cap})")

# ------ Convenience scenario builders----------------------------------------------
def single_storm_traffic(normal=20.0, storm=200.0,
                         t_pre=50.0, t_storm=60.0, t_post=900.0) -> TrafficConfig:
    """The prior paper's scenario: 20 -> 200 -> 20 UEs/s."""
    return TrafficConfig(phases=[
        StormPhase(0.0, t_pre, normal, 0.0, "pre"),
        StormPhase(t_pre, t_pre + t_storm, storm, 0.0, "storm"),
        StormPhase(t_pre + t_storm, t_pre + t_storm + t_post, normal, 0.0, "recovery"),
    ])

def multi_storm_traffic() -> TrafficConfig:
    # Three storms of growing intensity with a malicious component, used to
    return TrafficConfig(phases=[
        StormPhase(0,    60,   20, 0,   "calm-1"),
        StormPhase(60,   120,  120, 40, "storm-1"),
        StormPhase(120,  360,  20, 0,   "recover-1"),
        StormPhase(360,  420,  20, 0,   "calm-2"),
        StormPhase(420,  480,  180, 60, "storm-2"),
        StormPhase(480,  720,  20, 0,   "recover-2"),
        StormPhase(720,  780,  20, 0,   "calm-3"),
        StormPhase(780,  840,  220, 80, "storm-3"),
        StormPhase(840, 1100,  20, 0,   "recover-3"),
    ])

def multi_storm_flat_traffic(benign=180.0, botnet=60.0, normal=20.0,
                             lead=60.0, storm=60.0, gap=90.0,
                             n_storms=3) -> TrafficConfig:
    # N IDENTICAL storms (non-incremental arrivals) on a COMPRESSED timeline. 
    # lead  — initial calm before storm 1 (must be >= the scoring baseline lookback ~50s)
    # storm — each storm's duration,  gap   — idle/recovery between storms; long enough for the system to settle back
    # to baseline AND to give the next storm ~50s of clean pre-storm baseline.
    # Default 60 + 3*(60+90) = 510s (vs the old 1100s), storm-1 still at 60-120s.
    phases = [StormPhase(0.0, lead, normal, 0.0, "calm-1")]
    t = lead
    for i in range(1, n_storms + 1):
        phases.append(StormPhase(t, t + storm, benign, botnet, f"storm-{i}")); t += storm
        phases.append(StormPhase(t, t + gap, normal, 0.0, f"recover-{i}"));    t += gap
    return TrafficConfig(phases=phases)
