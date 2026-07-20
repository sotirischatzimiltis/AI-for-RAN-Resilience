"""
Discrete-event signaling-storm simulator for Open RAN UE initial attachment.

Models individual UEs performing the control-plane attach procedure against a
pool of c CU-processing servers. Each attach attempt holds a server for the
service time derived from the F1/O-FH delay accounting (see config.py).

Realism: each UE runs an explicit RRC setup timer (T300). If the attach does
not complete before the timer expires, the UE abandons and retries, injecting
additional control-plane load. Under overload this retry loop self-amplifies,
which is the defining behaviour of a signaling storm (and what the analytical
M/M/c model in the prior paper cannot capture).

The server count c is mutable at runtime via set_servers(), so external
controllers (fixed, Lyapunov, or agentic) can act on the system.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import List, Optional

import simpy
import simpy.rt

from .config import SimConfig

@dataclass
class TelemetrySample:
    t: float                   # simulation time (s)
    lam_current: float         # instantaneous Poisson arrival rate (benign+botnet), UEs/s
    queue_len: int             # attempts waiting for a server
    busy: int                  # attempts in service
    in_system: int             # queue + busy
    c: int                     # servers ONLINE and serving (actual capacity)
    c_target: int = 0          # servers COMMANDED by the controller (may exceed c during warm-up)
    completed: int = 0         # cumulative successful attaches
    failed: int = 0            # cumulative UEs that exhausted retries
    retries: int = 0           # cumulative retry events
    arrivals: int = 0          # cumulative attach attempts submitted (incl. retries)
    malicious_arrivals: int = 0  # cumulative botnet UEs spawned
    malicious_dropped: int = 0   # cumulative botnet UEs dropped at admission

@dataclass
class Stats:
    completed: int = 0          # cumulative successful attaches
    failed: int = 0             # cumulative UEs that exhausted retries
    retries: int = 0            # cumulative retry events
    arrivals: int = 0           # cumulative attach attempts submitted (incl. retries)
    completion_delays: List[float] = field(default_factory=list)  # successful attach latency (ms)
    completion_times:  List[float] = field(default_factory=list)  # sim-time (s) each success completed
    completion_benign: List[bool]  = field(default_factory=list)  # True if that completed UE was benign
    # ^ these three are index-aligned (one entry per successful attach), so latency can be
    #   sliced by storm window (completion_times) and by user class (completion_benign).

    # split by UE class so we can tell "legit users served" from "botnet blocked".
    # failed lumps both a benign UE that timed out AND a botnet UE the filter dropped;
    # only the benign figures answer "did real users get service?".
    benign_completed:  int = 0
    benign_failed:     int = 0   # benign UE that exhausted its retries (genuine denial)
    malicious_arrivals:int = 0   # botnet UEs spawned (per-UE, for per-storm blocked rate)
    malicious_dropped: int = 0   # botnet UE rejected at admission by the filter (desired)
    malicious_failed:  int = 0   # botnet UE that exhausted retries without being filtered


class _Attempt:
    """One attach attempt (a UE may make several across retries)."""
    # __slots__ fixes the allowed attributes and drops the per-instance __dict__:
    # with tens of thousands of attempts per episode this saves memory and speeds
    # attribute access. Only these five fields may ever be set on an _Attempt.
    __slots__ = ("ue_id", "malicious", "served_evt", "abandoned", "in_service")

    def __init__(self, ue_id: int, malicious: bool, env: simpy.Environment):
        self.ue_id = ue_id            # which UE this try belongs to (stable across its retries)
        self.malicious = malicious    # botnet UE? carried so the outcome lands in the right stat bucket
        self.served_evt = env.event() # SimPy event the server fires (.succeed()) to signal "attached";
                                      #   the UE process waits on it, raced against the T300 timer
        self.abandoned = False        # set True when T300 expires -> this try is given up (see the race
                                      #   guards in _serve and the abandon path so it isn't counted twice)
        self.in_service = False       # set True once the dispatcher hands it to a server; tells the
                                      #   abandon path whether it's still safe to pull from the queue

class StormSim:
   # -------- Initialization and configuration of the simulation ------------------------------
    def __init__(self, cfg: SimConfig):
        self.cfg = cfg
        if cfg.realtime:
            # strict=False: if a control step (e.g. an agent's LLM call) overruns
            # its real-time slot, don't crash -- let the action land late. That
            # lateness is exactly the agentic overhead acting on the queue.
            # SimPy factor = real-seconds per simulated-second (inverse of rt_factor)
            self.env = simpy.rt.RealtimeEnvironment(factor=1.0 / cfg.rt_factor, strict=False)
        else:
            self.env = simpy.Environment()
        self.rng = random.Random(cfg.seed)

        # clamp the initial count into [1, c_max] so a misconfigured c0 can't start
        # the sim above the ceiling (set_servers enforces the same bound at runtime).
        c0 = max(1, min(int(cfg.c0), cfg.c_max))
        self.c = c0                # COMMANDED server count (set by controller)
        self.c_online = c0         # servers actually ONLINE (initial ones already up)
        self.busy = 0
        self.waiting: List[_Attempt] = []
        self.stats = Stats()
        self.telemetry: List[TelemetrySample] = []

        # rate-limit actuator: fraction of *malicious* attempts dropped at admission
        self.malicious_drop_prob = 0.0

        # live arrival-rate override for interactive/GUI use: if not None, it is
        # used as (benign_rate, botnet_rate) instead of the traffic schedule.
        self.live_rate_override = None

        self._mu_single = cfg.arch.service_rate()      # per-server rate (UEs/s), UNLOADED
        self._svc_time_s = cfg.arch.service_time_ms() / 1000.0
        # split for load-dependent (processor-sharing) inflation: only the
        # compute/processing component inflates under contention; the F1/O-FH
        # propagation component is fixed link physics and does not.
        self._proc_s = cfg.arch.proc_total_ms / 1000.0
        self._prop_s = (cfg.arch.n_ctrl_messages * cfg.arch.oneway_delay_ms) / 1000.0
        # re-armable "poke me" signals for the two parked background loops (one-shot
        # SimPy events, re-created after each fire — see _signal / set_servers).
        self._wake = self.env.event()            # wakes the dispatcher: queue/capacity changed
        self._provision_wake = self.env.event()  # wakes the provisioning mgr: commanded c changed
        self._ue_counter = 0                     # monotonic id handed to each new UE

        # register the four concurrent processes that drive the simulation; they run
        # cooperatively on the SimPy event loop once env.run() starts.
        self.env.process(self._arrival_process())        # spawns UEs per the traffic schedule
        self.env.process(self._dispatcher())             # assigns waiting attempts to free servers
        self.env.process(self._provisioning_manager())   # brings servers online/offline toward c
        self.env.process(self._telemetry_process())      # samples state into telemetry each tick

    # -- runtime actuators (used by controllers / agents) --------------------
    def set_servers(self, c: int):
        c = max(1, min(int(c), self.cfg.c_max))
        if c != self.c:
            self.c = c
            # wake the provisioning manager to reconcile online capacity
            if not self._provision_wake.triggered:
                self._provision_wake.succeed()
            self._provision_wake = self.env.event()

    def set_malicious_drop_prob(self, p: float):
        self.malicious_drop_prob = max(0.0, min(1.0, p))

    @property
    def mu_single(self) -> float:
        return self._mu_single

    # -- internal wake mechanism for the dynamic-capacity dispatcher ---------
    def _signal(self):
        if not self._wake.triggered:
            self._wake.succeed()
        self._wake = self.env.event()

    # -- arrival process: Poisson benign + botnet, time-varying --------------
    def _arrival_process(self):
        env = self.env
        cfg = self.cfg
        while True:
            t = env.now
            if self.live_rate_override is not None:
                benign, botnet = self.live_rate_override
            else:
                benign, botnet = cfg.traffic.rates_at(t)
            total = benign + botnet
            if total <= 0:
                yield env.timeout(cfg.sample_dt_s)
                continue
            # next arrival ~ Exp(total)
            yield env.timeout(self.rng.expovariate(total))
            malicious = self.rng.random() < (botnet / total if total > 0 else 0.0)
            self._spawn_ue(malicious)

    def _spawn_ue(self, malicious: bool):
        self._ue_counter += 1
        if malicious:
            self.stats.malicious_arrivals += 1
        self.env.process(self._ue_attach(self._ue_counter, malicious, t_arrival=self.env.now))

    # -- a single UE's attach lifecycle, with T300 timer and retries ---------
    def _ue_attach(self, ue_id: int, malicious: bool, t_arrival: float):
        env = self.env
        cfg = self.cfg
        for attempt_idx in range(cfg.rrc.max_attempts):
            # admission control (rate-limit actuator) acts on malicious UEs
            if malicious and self.rng.random() < self.malicious_drop_prob:
                self.stats.failed += 1
                self.stats.malicious_dropped += 1   # botnet blocked at admission (desired)
                return

            att = _Attempt(ue_id, malicious, env)
            self.stats.arrivals += 1
            self.waiting.append(att)
            self._signal()

            timer = env.timeout(cfg.rrc.t300_ms / 1000.0)
            res = yield att.served_evt | timer

            if att.served_evt in res:
                # success
                self.stats.completed += 1
                if not malicious:
                    self.stats.benign_completed += 1
                # record this success: end-to-end latency (ms), when it completed (s),
                # and whether it was a real user — kept index-aligned across the 3 lists.
                self.stats.completion_delays.append((env.now - t_arrival) * 1000.0)
                self.stats.completion_times.append(env.now)
                self.stats.completion_benign.append(not malicious)
                return
            else:
                # T300 expired: abandon this attempt
                att.abandoned = True
                if not att.in_service and att in self.waiting:
                    self.waiting.remove(att)
                # if this was the last allowed attempt, stop here: no retry to count and
                # no re-attach wait to serve — fall through to the exhausted-failure path.
                if attempt_idx == cfg.rrc.max_attempts - 1:
                    break
                self.stats.retries += 1   # a genuine re-attempt follows
                if cfg.rrc.backoff_ms > 0:
                    yield env.timeout(cfg.rrc.backoff_ms / 1000.0)
                # botnet UEs re-attach on their own period (on top of any backoff)
                if malicious and cfg.botnet_attach_period_ms > 0:
                    yield env.timeout(cfg.botnet_attach_period_ms / 1000.0)
        # exhausted attempts
        self.stats.failed += 1
        if malicious:
            self.stats.malicious_failed += 1
        else:
            self.stats.benign_failed += 1   # a genuine denial of a legitimate user

    # -- dispatcher: assigns waiting attempts to free servers ----------------
    def _dispatcher(self):
        env = self.env
        while True:
            progressed = False
            while self.busy < self.c_online and self.waiting:
                att = self.waiting.pop(0)
                if att.abandoned:
                    continue
                att.in_service = True
                self.busy += 1
                env.process(self._serve(att))
                progressed = True
            if not progressed:
                yield self._wake
            else:
                # let service processes start, then re-check
                yield env.timeout(0)

    def _provisioning_manager(self):
        """
        Reconciles online server count (c_online) toward the commanded target
        (c). Scaling UP is gradual: each new server takes server_provision_delay_s
        to come online (image pull / boot / pool attach of a vDU/vCU instance),
        brought up one at a time. Scaling DOWN is immediate (no preemption: any
        busy server finishes its current attach, the dispatcher just stops
        starting new work once busy >= c_online). delay=0 -> instant (default).
        """
        env = self.env
        delay = self.cfg.server_provision_delay_s
        while True:
            if self.c_online > self.c:
                # scale down: take offline immediately
                self.c_online = self.c
                self._signal()
            if self.c_online < self.c:
                # scale up: warm up one server, then loop to bring up more
                if delay > 0:
                    yield env.timeout(delay)
                if self.c_online < self.c:      # target may have dropped during warm-up
                    self.c_online += 1
                    self._signal()
                continue
            # in sync with target: park until set_servers changes it
            yield self._provision_wake

    def _serve(self, att: _Attempt):
        env = self.env
        yield env.timeout(self._service_time())
        self.busy -= 1
        if not att.abandoned:
            att.served_evt.succeed()
        self._signal()

    def _service_time(self) -> float:
        """
        Mean-exponential service time for one attach. If shared-compute
        contention is enabled, the PROCESSING component is inflated by the
        processor-sharing factor 1/(1 - rho_c), rho_c = busy/kappa (the mean
        sojourn time of an M/M/1-PS server of demand `proc` at utilization
        rho_c). The propagation component is fixed.
        """
        kappa = self.cfg.compute_kappa
        if kappa is None or kappa <= 0:
            mean = self._svc_time_s
        else:
            rho_c = min(self.busy / kappa, self.cfg.compute_rho_cap)
            proc_eff = self._proc_s / (1.0 - rho_c)
            mean = proc_eff + self._prop_s
        return self.rng.expovariate(1.0 / mean)

    # -- telemetry sampling --------------------------------------------------
    def _telemetry_process(self):
        env = self.env
        cfg = self.cfg
        while True:
            if self.live_rate_override is not None:
                benign, botnet = self.live_rate_override
            else:
                benign, botnet = cfg.traffic.rates_at(env.now)
            self.telemetry.append(TelemetrySample(
                t=env.now,
                lam_current=benign + botnet,
                queue_len=len(self.waiting),
                busy=self.busy,
                in_system=len(self.waiting) + self.busy,
                c=self.c_online,
                c_target=self.c,
                completed=self.stats.completed,
                failed=self.stats.failed,
                retries=self.stats.retries,
                arrivals=self.stats.arrivals,
                malicious_arrivals=self.stats.malicious_arrivals,
                malicious_dropped=self.stats.malicious_dropped,
            ))
            yield env.timeout(cfg.sample_dt_s)

    # -- run -----------------------------------------------------------------
    def run(self, until: Optional[float] = None, controller=None):
        """
        Run the simulation. If a controller is given, it is invoked every
        sample_dt_s with the current telemetry and may call set_servers() etc.
        """
        horizon = until if until is not None else self.cfg.traffic.horizon()
        if controller is None:
            self.env.run(until=horizon)
        else:
            self.env.process(self._control_loop(controller))
            self.env.run(until=horizon)
        return self.telemetry

    def _control_loop(self, controller):
        env = self.env
        while True:
            if self.telemetry:
                controller.step(self, self.telemetry[-1])
            yield env.timeout(self.cfg.sample_dt_s)
