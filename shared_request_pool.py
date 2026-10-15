"""Bound request producers globally, without quotas by book or subject.

The existing gateway still admits HTTP calls. These short-lived worker leases
keep native blocking clients from creating one full thread pool per book.
"""
from collections import deque
from concurrent.futures import Future, FIRST_COMPLETED, wait
import ipaddress
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener
import uuid


class ProducerBudget:
    def __init__(self):
        self.owners = {}
        self.waiters = deque()
        self.lock = threading.RLock()

    @property
    def active(self):
        with self.lock:
            return sum(self.owners.values())

    def claim(self, owner, count, *, limit, paused=False):
        if not owner or type(count) is not int or count < 1 or limit < 1:
            raise ValueError('Invalid producer reservation')
        with self.lock:
            if owner not in self.waiters:
                self.waiters.append(owner)
            free = max(0, limit - self.active)
            if paused or not free or self.waiters[0] != owner:
                return 0
            granted = min(count, free)
            self.owners[owner] = self.owners.get(owner, 0) + granted
            self.waiters.popleft()
            return granted

    def release(self, owner, count):
        with self.lock:
            if type(count) is not int or not 0 < count <= self.owners.get(owner, 0):
                raise ValueError('Producer released unowned slots')
            self.owners[owner] -= count
            if not self.owners[owner]:
                del self.owners[owner]

    def close(self, owner):
        with self.lock:
            self.owners.pop(owner, None)
            self.waiters = deque(x for x in self.waiters if x != owner)


def process_start(pid):
    try:
        raw = (Path('/proc') / str(pid) / 'stat').read_text()
        return int(raw[raw.rfind(')') + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


class LeaseClient:
    def __init__(self, url):
        p = urlsplit(url)
        try:
            local = ipaddress.ip_address(p.hostname or '').is_loopback
        except ValueError:
            local = False
        if not local or p.scheme != 'http' or p.path not in ('', '/') or p.query or p.fragment or p.username or p.password:
            raise ValueError('Shared producer budget requires a loopback HTTP origin')
        self.url = url.rstrip('/') + '/_scheduler/producers'
        self.owner = uuid.uuid4().hex
        self.pid = os.getpid()
        self.start = process_start(self.pid)

    def _call(self, operation, count=0):
        payload = {'owner': self.owner, 'op': operation, 'count': count,
                   'pid': self.pid, 'start_ticks': self.start}
        req = Request(self.url, data=json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json'})
        with build_opener(ProxyHandler({})).open(req, timeout=15) as response:
            value = json.load(response)
        if not isinstance(value, dict) or type(value.get('granted')) is not int:
            raise ValueError('Invalid producer budget response')
        return value['granted']

    def claim(self, count):
        return self._call('claim', count)

    def release(self, count):
        self._call('release', count)

    def close(self):
        self._call('close')


def _start(fn, item, *, name='model-request'):
    future = Future()
    def run():
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(fn(item))
            except BaseException as exc:
                future.set_exception(exc)
    thread = threading.Thread(target=run, name=name, daemon=False)
    thread.start()
    return future, thread


def completed(fn, items, workers):
    """Yield (input, future) as completed; callers persist each result immediately.

    There are no idle per-book threads. A surviving producer can claim every
    remaining global slot. A permit is returned after the consumer handles the
    yielded result, including a checkpoint write.
    """
    if type(workers) is not int or workers < 1:
        raise ValueError('workers must be positive')
    url = os.environ.get('PIPELINE_SHARED_POOL_URL')
    client = LeaseClient(url) if url else None
    iterator = iter(items)
    end = object()
    following = next(iterator, end)
    pending = {}
    leased = 0
    try:
        while following is not end or pending:
            while following is not end and len(pending) < workers:
                request = min(32, workers - len(pending))
                granted = client.claim(request) if client else request
                if not granted:
                    break
                leased += granted
                used = 0
                for _ in range(granted):
                    if following is end:
                        break
                    item = following
                    future, thread = _start(fn, item)
                    pending[future] = (item, thread)
                    used += 1
                    following = next(iterator, end)
                if granted != used:
                    if client:
                        client.release(granted - used)
                    leased -= granted - used
            if not pending:
                time.sleep(.05)
                continue
            ready, _ = wait(pending, timeout=.05, return_when=FIRST_COMPLETED)
            for future in ready:
                item, thread = pending.pop(future)
                thread.join()
                try:
                    yield item, future
                finally:
                    if client:
                        client.release(1)
                    leased -= 1
    finally:
        # Keep ownership until all outstanding native writers have completed.
        for future, (_, thread) in pending.items():
            future.cancel()
            thread.join()
        if client:
            client.close()


class SharedExecutor:
    """Executor for range dispatchers whose work functions persist their own data."""
    def __init__(self, max_workers):
        if type(max_workers) is not int or max_workers < 1:
            raise ValueError('workers must be positive')
        self.workers = max_workers
        url = os.environ.get('PIPELINE_SHARED_POOL_URL')
        self.client = LeaseClient(url) if url else None
        self.local = threading.BoundedSemaphore(max_workers)
        self.threads = {}
        self.closed = False
        self.submitting = 0
        self.condition = threading.Condition()
        self.cleanup_errors = []
        self.reaper = threading.Thread(target=self._reap, name='model-pool-reaper', daemon=False)
        self.reaper.start()

    def _cleanup_error(self, error):
        with self.condition:
            self.cleanup_errors.append(error)
            self.condition.notify_all()

    def _reap(self):
        """Return permits only after native worker threads have actually exited."""
        while True:
            with self.condition:
                ready = [(future, thread) for future, thread in self.threads.items()
                         if not thread.is_alive()]
                for future, _ in ready:
                    del self.threads[future]
                if not ready:
                    if self.closed and not self.threads and not self.submitting:
                        break
                    self.condition.wait(.01)
                    continue
            for _, thread in ready:
                thread.join()
            try:
                if self.client:
                    self.client.release(len(ready))
            except BaseException as exc:
                # Reject further submissions; close the owner after all existing
                # writers join, even if a release response was lost.
                self._cleanup_error(exc)
            finally:
                for _ in ready:
                    self.local.release()
        if self.client:
            try:
                self.client.close()
            except BaseException as exc:
                self._cleanup_error(exc)

    def submit(self, fn, /, *args, **kwargs):
        with self.condition:
            if self.closed:
                raise RuntimeError('Executor already closed')
            self.submitting += 1
        acquired = claimed = False
        try:
            self.local.acquire()
            acquired = True
            while True:
                with self.condition:
                    if self.closed:
                        raise RuntimeError('Executor already closed')
                    if self.cleanup_errors:
                        raise self.cleanup_errors[0]
                if not self.client or self.client.claim(1):
                    claimed = bool(self.client)
                    break
                time.sleep(.05)
            future, thread = _start(lambda _: fn(*args, **kwargs), None, name='model-range')
            with self.condition:
                self.threads[future] = thread
                self.condition.notify_all()
            acquired = claimed = False  # The reaper now owns both reservations.
            return future
        except BaseException:
            try:
                if claimed:
                    self.client.release(1)
            except BaseException as exc:
                self._cleanup_error(exc)
            finally:
                if acquired:
                    self.local.release()
            raise
        finally:
            with self.condition:
                self.submitting -= 1
                self.condition.notify_all()

    def shutdown(self, wait=True, cancel_futures=False):
        if not wait:
            raise ValueError('Shared producer ownership requires waiting for active writers')
        with self.condition:
            self.closed = True
            if cancel_futures:
                for future in self.threads:
                    future.cancel()
            self.condition.notify_all()
        self.reaper.join()
        if self.cleanup_errors:
            raise self.cleanup_errors[0]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.shutdown()
