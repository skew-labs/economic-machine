"""A single persistent dispatcher; source readers never wait on a model batch."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
from pathlib import Path
import signal
import threading
import time

from ..muse_policy_worker import atomic
from .model import DEADLINE, Model, Policy
from .queue import Queue, now_ms


class Runner:
    def __init__(self, pool_root, solana_base, arbitrum_base, robinhood_base, key_file, policy_path, policy_sha256):
        self.pool_root, self.solana_base = Path(pool_root), Path(solana_base)
        self.arbitrum_base, self.robinhood_base = Path(arbitrum_base), Path(robinhood_base)
        self.queue = Queue(self.pool_root)
        self.policy = Policy(policy_path, policy_sha256, self.solana_base)
        self.model = Model(pool_root, solana_base, key_file, self.policy)
        self.stop = threading.Event()
        self.last_feeder = None
        self.feeder_errors = {}
        self.feeder_results = {}
        self.cached_totals = None
        self.totals_at_ms = 0

    def feed(self):
        from ..pump_research import research_pool_adapter as solana
        from ..arbitrum_research import research_pool_adapter as evm
        queue = Queue(self.pool_root)
        try:
            for name, call in (
                ('solana', lambda: solana.enqueue_jobs(queue, self.solana_base, max_pending=80)),
                ('evm_consume', lambda: evm.consume_results(queue, self.arbitrum_base, self.robinhood_base, max_jobs=8)),
                ('evm', lambda: evm.enqueue_jobs(queue, self.arbitrum_base, self.robinhood_base, max_pending=120)),
            ):
                if self.stop.is_set():
                    break
                self.policy.check()
                try:
                    result = call()
                    encoded = json.dumps(result, sort_keys=True, allow_nan=False)
                    self.feeder_results[name] = result if len(encoded.encode()) <= 16384 else {'state': 'ADAPTER_REPORT_BOUND'}
                    self.feeder_errors.pop(name, None)
                except Exception as exc:
                    # Exceptions from evidence or transport may contain private
                    # endpoints. Retain only class and adapter identity.
                    code = type(exc).__name__
                    if self.feeder_errors.get(name) != code:
                        queue.event('adapter_error', dict(adapter=name, exception_class=code))
                    self.feeder_errors[name] = code
            self.last_feeder = now_ms()
        finally:
            queue.close()

    def publish(self, state, futures):
        current = self.model.current()
        running_states = ('RESEARCHING', 'WAITING_NEW_EVIDENCE', 'PROVIDER_BACKOFF', 'DRAINING')
        if self.cached_totals is None or now_ms() - self.totals_at_ms >= 30000 or state not in running_states:
            self.cached_totals = self.queue.totals()
            self.totals_at_ms = now_ms()
        totals = dict(self.cached_totals)
        totals['last_sent_ms'] = self.queue.db.execute('SELECT coalesce(max(sent_ms),0) FROM calls').fetchone()[0]
        totals['last_finished_ms'] = self.queue.db.execute('SELECT coalesce(max(finished_ms),0) FROM calls').fetchone()[0]
        gate = dict(self.queue.db.execute('SELECT * FROM provider_gate WHERE id=1').fetchone())
        counts = self.queue.counts()
        if state == 'WAITING_NEW_EVIDENCE' and counts['by_state'].get('valid', 0):
            state = 'WAITING_NATIVE_CHECKS'
        status = dict(schema='economic-machine.muse-pool-status.v1', updated_ms=now_ms(),
                      state=state, model='meta/muse-spark-1.3-contributor',
                      configured=50, configured_workers=50, active=current['actual_inflight'],
                      queued_or_running_futures=len(futures), **current,
                      queue=counts, model_calls=totals,
                      wait_reason=('approved_source_checks_pending' if state == 'WAITING_NATIVE_CHECKS'
                                   else 'no_new_distinct_evidence' if state == 'WAITING_NEW_EVIDENCE' else None),
                      model_cost_summary_at_ms=self.totals_at_ms,
                      last_sent_ms=totals['last_sent_ms'], last_finished_ms=totals['last_finished_ms'],
                      provider_not_before_ms=gate['not_before_ms'], provider_last_error=gate['last_error'],
                      last_feeder_ms=self.last_feeder, adapter_errors=dict(self.feeder_errors),
                      adapter_results=dict(self.feeder_results),
                      deadline_ms=DEADLINE,
                      provider_interval_definition='client provider attempt send-to-response; not provider internal execution',
                      adaptive_target_lanes={'solana': 30, '42161': 15, '4663': 5},
                      idle_capacity_reassigned=True, public_deployment=False, trading=False)
        status['workers'] = [dict(lane=i, state='PROVIDER_REQUEST' if str(i) in current['lanes'] else 'IDLE',
                                  **current['lanes'].get(str(i), {})) for i in range(50)]
        atomic(self.queue.out / 'status.json', status)
        return status

    def run(self, once=False):
        lock = open(self.queue.out / 'worker.lock', 'a+')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.model.recover()
        self.queue.event('runner_started', dict(configured=50, policy_sha256=self.policy.digest))
        old_handlers = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                old_handlers[sig] = signal.signal(sig, lambda *_: self.stop.set())
        futures, available = {}, set(range(50))
        feed_future = None
        last_publish = 0
        # Every ten preferred admissions contain six SOL, three ARB and one RH.
        # If a chain has no queue, oldest ready work may use the idle lane.
        cycle = ('solana', '42161', 'solana', '4663', 'solana', '42161', 'solana', 'solana', '42161', 'solana')
        turn = 0
        admitted = 0
        final_state = 'STOPPED'
        try:
            with ThreadPoolExecutor(max_workers=50, thread_name_prefix='muse-evidence') as pool, \
                 ThreadPoolExecutor(max_workers=1, thread_name_prefix='muse-native-evidence') as feeder:
                while True:
                    for future, lane in list(futures.items()):
                        if not future.done():
                            continue
                        try:
                            result = future.result()
                        except Exception:
                            result = 'worker_error'
                        if result in ('budget_blocked', 'admission_error', 'reserved_unknown_recovery', 'deadline',
                                      'cost_breach', 'worker_error', 'provider_fatal'):
                            self.stop.set()
                            final_state = result.upper()
                        del futures[future]
                        available.add(lane)
                    try:
                        self.policy.check()
                    except ValueError as exc:
                        self.stop.set()
                        final_state = 'DEADLINE' if str(exc) == 'POOL_DEADLINE' else 'POLICY_CHANGED'
                    if feed_future is not None and feed_future.done():
                        try:
                            feed_future.result()
                        except Exception:
                            self.stop.set()
                            final_state = 'ADAPTER_WORKER_ERROR'
                        feed_future = None
                    if (not self.stop.is_set() and feed_future is None
                            and (self.last_feeder is None or now_ms() - self.last_feeder >= 5000)):
                        feed_future = feeder.submit(self.feed)
                    gate = self.queue.db.execute('SELECT not_before_ms FROM provider_gate WHERE id=1').fetchone()[0]
                    backoff = now_ms() < gate
                    if not self.stop.is_set() and not backoff:
                        while available and (not once or admitted < 50):
                            job = self.queue.claim(cycle[turn % len(cycle)])
                            turn += 1
                            if job is None:
                                break
                            lane = min(available)
                            available.remove(lane)
                            futures[pool.submit(self.model.call, job, lane)] = lane
                            admitted += 1
                    if once and not futures and feed_future is None and self.last_feeder is not None:
                        final_state = 'BOUNDED_ROUND_COMPLETE'
                        break
                    if self.stop.is_set() and not futures:
                        break
                    state = 'DRAINING' if self.stop.is_set() else 'PROVIDER_BACKOFF' if backoff else 'RESEARCHING' if futures else 'WAITING_NEW_EVIDENCE'
                    if now_ms() - last_publish >= 2000:
                        self.publish(state, futures)
                        last_publish = now_ms()
                    self.stop.wait(.25) if not self.stop.is_set() else time.sleep(.25)
            # EVM checks are read-only native work; the SOL graph's sole writer
            # consumes its results separately. No final model admission occurs.
            from ..arbitrum_research import research_pool_adapter as evm
            try:
                evm.consume_results(self.queue, self.arbitrum_base, self.robinhood_base, max_jobs=100)
            except Exception as exc:
                self.queue.event('final_native_consumer_error', {'exception_class': type(exc).__name__})
                final_state = 'FINAL_NATIVE_CONSUMER_ERROR'
            self.queue.event('runner_finished', dict(state=final_state, admissions=admitted))
            return self.publish(final_state, futures)
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
            lock.close()
            self.queue.close()
