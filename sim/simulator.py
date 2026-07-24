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

@dataclass # this is what controllers consume/observe each time step
class TelemetrySample:
    t: float                   # simulation time (s)
    lam_current: float         # instantaneous Poisson arrival rate (benign+botnet), UEs/s
    queue_len: int             # attempts waiting for a server
    busy: int                  # attempts in service
    in_system: int             # queue + busy
    c_online: int              # servers ONLINE and serving (actual capacity)
    c_target: int = 0          # servers COMMANDED by the controller (may exceed c_online during warm-up)
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
    # `failed` lumps every denial; the class-split fields below reconcile to it:
    #   failed == benign_failed + benign_dropped + malicious_failed + malicious_dropped
    benign_arrivals:   int = 0   # SIM-6: benign UEs spawned (per-UE)
    benign_completed:  int = 0
    benign_failed:     int = 0   # benign UE that exhausted its retries (genuine starvation denial)
    benign_dropped:    int = 0   # SIM-2: benign UE dropped by the filter (false positive)
    malicious_arrivals:int = 0   # botnet UEs spawned (per-UE, for per-storm blocked rate)
    malicious_dropped: int = 0   # botnet UE rejected at admission by the filter (desired)
    malicious_failed:  int = 0   # botnet UE that exhausted retries without being filtered


class _Attempt:
    """One attach attempt (a UE may make several across retries)."""
    # __slots__: no per-instance __dict__ — saves memory/speed at ~tens of thousands of attempts.
    __slots__ = ("ue_id", "malicious", "served_event", "abandoned", "in_service")

    def __init__(self, ue_id: int, malicious: bool, env: simpy.Environment):
        self.ue_id = ue_id            # UE this try belongs to (stable across its retries)
        self.malicious = malicious    # botnet UE? routes the outcome to the right stat bucket
        self.served_event = env.event() # create SimPy event with no trigger condition. Something has to call .succeed(), either served or timer expires.
        self.abandoned = False        # True once the timer expires — guards against double-counting (see _serve)
        self.in_service = False       # True once a server took it — tells the abandon path not to pull from queue

class StormSim:
   # -------- Initialization and configuration of the simulation ------------------------------
    def __init__(self, cfg: SimConfig):
        self.cfg = cfg # get the configuration object from config.py SimConfig 
        if cfg.realtime: # if i want the simulation to run in real time or as fast as possible 
            self.env = simpy.rt.RealtimeEnvironment(factor=1.0 / cfg.rt_factor, strict=False)
        else:
            self.env = simpy.Environment() # non real time simulation environment
        # Separate RNG per role: arrival/class streams are exogenous (identical storm
        # across all controller arms for paired comparison); service/admit are
        # endogenous and may diverge based on each controller's decisions.
        seed = cfg.seed if cfg.seed is not None else 0
        self.rng_arrival = random.Random(seed)        # inter-arrival gaps (exogenous)
        self.rng_class   = random.Random(seed + 1)    # benign vs malicious label (exogenous)
        self.rng_service = random.Random(seed + 2)    # service times + benign retry backoff
        self.rng_admit   = random.Random(seed + 3)    # admission-filter draws

        c0 = max(1, min(int(cfg.c0), cfg.c_max)) # clamp the initial count into [1, c_max]
        self.c_target = c0         # COMMANDED server count (set by controller)
        self.c_online = c0         # servers actually ONLINE (initial ones already up)
        self.busy = 0 # number of servers currently busy serving attempts
        self.waiting: List[_Attempt] = [] # queue of attempts waiting for a free server
        self.stats = Stats() # cumulative statistics of the simulation
        self.telemetry: List[TelemetrySample] = [] # telemetry samples collected at each tick (for controller feedback and post-run analysis)
        self.malicious_drop_prob = 0.0 # rate-limit actuator: fraction of *malicious* attempts dropped at admission
        
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
        # Wake signals for the two loops that park when idle. A SimPy event is single-use,
        # so each is fired with .succeed() then immediately replaced with a fresh one (see
        # _signal / set_servers) — that fire-and-replace is what makes it reusable.
        self._wake = self.env.event()            # dispatcher waits on this; poked when queue/capacity changes
        self._provision_wake = self.env.event()  # provisioning mgr waits on this; poked when c_target changes
        self._ue_counter = 0                     # monotonic id handed to each new UE

        # IMPORTANT. register the four concurrent processes that drive the simulation
        # they run cooperatively on the SimPy event loop once env.run() starts.
        self.env.process(self._arrival_process())        # spawns UEs per the traffic schedule
        self.env.process(self._dispatcher())             # assigns waiting attempts to free servers
        self.env.process(self._provisioning_manager())   # brings servers online/offline toward c
        self.env.process(self._telemetry_process())      # samples state into telemetry each tick

    # -- runtime actuators (used by controllers / agents) --------------------
    def set_servers(self, c: int):
        c = max(1, min(int(c), self.cfg.c_max)) # clamp the commanded count into [1, c_max]
        if c != self.c_target: 
            self.c_target = c
            # wake the provisioning manager to reconcile online capacity
            if not self._provision_wake.triggered: # check if the event has already been triggered
                self._provision_wake.succeed() # trigger the event to wake up the provisioning manager
            self._provision_wake = self.env.event() # create a new event for the next wake-up

    def set_malicious_drop_prob(self, p: float):
        self.malicious_drop_prob = max(0.0, min(1.0, p)) # set the drop probability, clamped to [0, 1]

    @property
    def mu_single(self) -> float: # this is so i can call sim.mu_single to get the per-server service rate without needing to access the private variable directly
        return self._mu_single

    # Poke the dispatcher: fire-and-replace its wake event so it re-checks the queue.
    # Called whenever the dispatcher may have new work or freed capacity: after a new
    # attempt is enqueued (_ue_attach), after a server finishes (_serve), and after a
    # server comes online (_provisioning_manager).
    def _signal(self):
        if not self._wake.triggered:  # a SimPy event fires only once; don't re-fire (would raise)
            self._wake.succeed()      # wake the dispatcher (parked at `yield self._wake`)
        self._wake = self.env.event() # re-arm: fresh event so it can be woken again next time

    # -- 1 of the 4 processes we have 
    # arrival process: Poisson benign + botnet, time-varying --------------
    def _arrival_process(self):
        env = self.env # get the simulation environment
        cfg = self.cfg # get the simulation configuration
        while True: 
            t = env.now # get the current simulation time
            if self.live_rate_override is not None: # if the live rate override is set, use it instead of the traffic schedule
                benign, botnet = self.live_rate_override
            else:
                benign, botnet = cfg.traffic.rates_at(t) # get the current benign and botnet lambda rates from the traffic schedule
            total = benign + botnet # total arrival rate (benign + botnet)
            if total <= 0: # check if no arrivals are scheduled (total rate is zero)
                yield env.timeout(cfg.telemetry_dt_s) # wait for the telemetry interval before checking again
                continue # skip the rest of the loop and go back to the start 
            # sleep a Poisson inter-arrival gap ~ Exp(total), i.e. wait until the next arrival
            yield env.timeout(self.rng_arrival.expovariate(total))
            # label this arrival: malicious with prob = botnet's share of the total rate
            # (splitting one merged Poisson stream into benign/botnet by thinning)
            malicious = self.rng_class.random() < (botnet / total if total > 0 else 0.0) # boolean indicating if benign or malicious next spawned UE
            self._spawn_ue(malicious)  # launch this UE's attach process (bumps arrival counter)

    # -- spawn a new UE and start its attach lifecycle --------------------------------
    def _spawn_ue(self, malicious: bool):
        self._ue_counter += 1 # increment the UE counter
        if malicious:
            self.stats.malicious_arrivals += 1 # increment the malicious arrivals counter
        else:
            self.stats.benign_arrivals += 1 # increment the benign arrivals counter
        # launch ue attach process: it will run concurrently with the dispatcher and other UEs    
        self.env.process(self._ue_attach(self._ue_counter, malicious, t_arrival=self.env.now))

    # -- a single UE's attach lifecycle, with T300 timer and retries ---------
    def _ue_attach(self, ue_id: int, malicious: bool, t_arrival: float):
        env = self.env # get the simulation environment
        cfg = self.cfg # get the simulation configuration
        for attempt_idx in range(cfg.rrc.max_attempts): # loop over the maximum number of allowed attach attempts for this UE
            # --- admission control (rate-limit actuator): drop bots, plus a few benign by mistake ---
            # `random()` is a uniform draw in [0,1); `random() < p` is therefore True with
            # probability p. Example: malicious_drop_prob=0.8 drops ~80% of bot attempts; with
            # benign_fp_alpha=0.05 the benign drop rate is 0.05*0.8=0.04, so ~4% of real users.
            # A dropped attempt ends the UE here (return): no queue, no retry.
            if malicious:
                if self.rng_admit.random() < self.malicious_drop_prob:   # drop this bot w.p. malicious_drop_prob
                    self.stats.failed += 1
                    self.stats.malicious_dropped += 1   # botnet blocked at admission (desired)
                    return
            else:
                benign_drop = cfg.benign_fp_alpha * self.malicious_drop_prob   # false-positive rate (0 when filter off)
                if benign_drop > 0 and self.rng_admit.random() < benign_drop:  # unlucky real user dropped w.p. benign_drop
                    self.stats.failed += 1
                    self.stats.benign_dropped += 1      # false positive (collateral)
                    return

            att = _Attempt(ue_id, malicious, env) # create a new attach attempt object for this UE
            self.stats.arrivals += 1 # increment the total arrivals counter (including retries)
            self.waiting.append(att) # add this attempt to the waiting queue for a server
            self._signal() # wake the dispatcher to check for available servers and assign this attempt if possible

            # SIM-1: the botnet is impatient — it abandons and re-attaches on its own
            # short period (botnet_attach_period_ms) rather than waiting the full T300
            # a handset uses. This makes the attacker strictly more aggressive and is
            # what drives the retry amplification.
            timeout_s = (cfg.botnet_attach_period_ms if malicious else cfg.rrc.t300_ms) / 1000.0
            timer = env.timeout(timeout_s)
            res = yield att.served_event | timer # wait for either the attach to be served or the timer to expire

            if att.served_event in res: # if the attach was served before the timer expired
                # success
                self.stats.completed += 1
                if not malicious:
                    self.stats.benign_completed += 1
                # record this success: end-to-end latency (ms), when it completed (s),
                # and whether it was a real user — kept index-aligned across the 3 lists.
                self.stats.completion_delays.append((env.now - t_arrival) * 1000.0)
                self.stats.completion_times.append(env.now) # record the simulation time when the attach completed
                self.stats.completion_benign.append(not malicious) # record whether this completed attach was benign (True) or malicious (False)
                return
            else:  # attach timer expired: abandon this attempt
                att.abandoned = True
                if not att.in_service and att in self.waiting: # if the attempt is not in service and is still in the waiting queue, remove it from the waiting queue
                    self.waiting.remove(att) # remove the abandoned attempt from the waiting queue
                if attempt_idx == cfg.rrc.max_attempts - 1: # if this was the last allowed attempt, stop here: no retry to count and
                    break
                self.stats.retries += 1   # a genuine re-attempt follows
                if not malicious and cfg.rrc.backoff_ms > 0:  # wait a random backoff before retrying (only for benign UEs)
                    yield env.timeout(self.rng_service.uniform(0.0, cfg.rrc.backoff_ms) / 1000.0) # wait a random backoff time before retrying (uniformly distributed between 0 and backoff_ms)
        # Reached only when the for-loop runs out of attempts (all max_attempts timed out).
        # `failed` is counted ONCE per UE here — a timed-out non-final attempt counts as a
        # `retry`, not a fail. (Admission-dropped UEs are counted as failed separately above,
        # in the drop paths, and never reach this line.)
        self.stats.failed += 1
        if malicious:
            self.stats.malicious_failed += 1
        else:
            self.stats.benign_failed += 1   # a genuine denial of a legitimate user

    # 2nd of the 4 processes -- dispatcher: assigns waiting attempts to free servers ----------------
    def _dispatcher(self):
        env = self.env # get the simulation environment
        while True: 
            progressed = False # flag to track if any attempts were assigned to servers in this iteration
            while self.busy < self.c_online and self.waiting: # while there are free servers and waiting attempts
                att = self.waiting.pop(0) # get the next waiting attempt from the queue
                if att.abandoned: # if the attempt has already been abandoned (timer expired), skip it and continue to the next one
                    continue
                att.in_service = True # mark the attempt as being in service (a server has taken it)
                self.busy += 1 # increment the count of busy servers
                env.process(self._serve(att)) # start the service process for this attempt (it will run concurrently)
                progressed = True # mark that we made progress in this iteration (an attempt was assigned to a server)
            if not progressed: # if no attempts were assigned to servers in this iteration, park until signaled (wait for new arrivals or server availability)
                yield self._wake # wait for the dispatcher to be signaled (either a new attempt arrives or a server becomes free)
            else:
                # let service processes start, then re-check
                yield env.timeout(0) # we essential give way to the serve process to start 

    def _provisioning_manager(self):
        """Reconcile c_online toward c_target. Scale-UP is gradual (one server per
        server_provision_delay_s: image pull/boot/attach of a vDU/vCU); scale-DOWN is
        immediate — no preemption, a busy server finishes and the dispatcher stops feeding it."""
        env = self.env # get the simulation environment
        delay = self.cfg.server_provision_delay_s # delay for scaling up servers (time to provision a new server)
        while True:
            if self.c_online > self.c_target: # if the current online server count is greater than the target server count
                self.c_online = self.c_target # scale down that happends immediately
                self._signal() # wake the dispatcher to check for available servers and assign waiting attempts if possible
            if self.c_online < self.c_target: # check if i need to scale up 
                if delay > 0: # if there is provision delay added 
                    yield env.timeout(delay) # wait for the provision delay before adding a new server
                if self.c_online < self.c_target:  # target may have dropped during warm-up
                    self.c_online += 1 # increment the online server count (scale up)
                    self._signal() # wake the dispatcher to check for available servers and assign waiting attempts if possible
                continue
            yield self._provision_wake # sleep until signaled (wait for a change in the target server count)

    def _serve(self, att: _Attempt):
        env = self.env
        yield env.timeout(self._service_time()) # wait for the service time to complete (simulate the time taken to serve the attempt)
        self.busy -= 1 # decrement the count of busy servers (the server is now free)
        if not att.abandoned: # if the attempt has not been abandoned (timer expired), mark it as served and trigger its served_event to notify the UE attach process that it has been served
            att.served_event.succeed() # trigger the served_event to notify the UE attach process that it has been served
        self._signal() # wake the dispatcher to check for available servers and assign waiting attempts if possible

    def _service_time(self) -> float: # compute the service time for an attempt, considering contention and propagation delays
        """Mean-exponential service time. With shared-compute contention on, the PROCESSING
        component inflates by the processor-sharing factor 1/(1 - rho_c), rho_c = busy/kappa;
        the propagation component is fixed link physics."""
        kappa = self.cfg.compute_kappa # get the compute capacity (kappa) from the configuration
        if kappa is None or kappa <= 0: # if compute capacity is not set or is non-positive, use the base service time without contention
            mean = self._svc_time_s # use the base service time without contention
        else: 
            rho_c = min(self.busy / kappa, self.cfg.compute_rho_cap) # compute the processor-sharing factor (rho_c) based on the number of busy servers and the compute capacity, capped at compute_rho_cap
            proc_eff = self._proc_s / (1.0 - rho_c) # compute the effective processing time considering contention
            mean = proc_eff + self._prop_s # total mean service time is the sum of the effective processing time and the fixed propagation time
        return self.rng_service.expovariate(1.0 / mean) # sample the actual service time from an exponential distribution with the computed mean service time

    # -- telemetry sampling  get the current values for the sample---------------------------------------
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
                c_online=self.c_online,
                c_target=self.c_target,
                completed=self.stats.completed,
                failed=self.stats.failed,
                retries=self.stats.retries,
                arrivals=self.stats.arrivals,
                malicious_arrivals=self.stats.malicious_arrivals,
                malicious_dropped=self.stats.malicious_dropped,
            ))
            yield env.timeout(cfg.telemetry_dt_s) # wait for the telemetry interval before taking the next sample 

    # -- run -----------------------------------------------------------------
    def run(self, until: Optional[float] = None, controller=None):
        """
        Run the simulation. If a controller is given, it is invoked every
        control_dt_s with the current telemetry and may call set_servers() etc.
        """
        horizon = until if until is not None else self.cfg.traffic.horizon() # this shows the end time of the simulation
        if controller is None: # if no controller is provided, run the simulation without a control loop
            self.env.run(until=horizon)
        else: # if a controller is provided, run the simulation with a control loop that invokes the controller at each control interval
            self.env.process(self._control_loop(controller)) # start control loop process with specific controller 
            self.env.run(until=horizon) # START THE SIMULATION RUN UNTIL THE HORIZON TIME
        self._check_invariants() # check the invariants of the simulation to ensure that the accounting is consistent and that the telemetry data is valid
        return self.telemetry

    def _check_invariants(self): # end of run self-audit: check that the accounting is consistent and that the telemetry data is valid
        s = self.stats # get the statistics object 
        assert s.failed == s.benign_failed + s.benign_dropped + s.malicious_failed + s.malicious_dropped, (
            f"failed accounting mismatch: {s.failed} != benign_failed({s.benign_failed}) + "
            f"benign_dropped({s.benign_dropped}) + malicious_failed({s.malicious_failed}) + "
            f"malicious_dropped({s.malicious_dropped})")
        assert len(s.completion_delays) == len(s.completion_times) == len(s.completion_benign) == s.completed, (
            "completion lists out of sync with completed count")

    def _control_loop(self, controller):
        env = self.env
        while True:
            if self.telemetry: # if there is telemetry data available, invoke the controller with the latest telemetry sample
                controller.step(self, self.telemetry[-1]) # invoke the controller's step function with the current simulation instance and the latest telemetry sample
            yield env.timeout(self.cfg.control_dt_s) # wait for the control interval before invoking the controller again
