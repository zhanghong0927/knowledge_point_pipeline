"""Bounded loopback relay for model-facing HTTP requests."""

import argparse
import asyncio
import ipaddress
import json
import re
import signal
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web
from yarl import URL

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared_request_pool import ProducerBudget, process_start


ROUTES = frozenset({"v1/chat/completions", "v1/models", "tokenize", "metrics"})
JOB_PATH = re.compile(r"^/_jobs/([A-Za-z0-9_-]{1,128})/(.*)$", re.ASCII)
HOP_HEADERS = frozenset({"connection", "keep-alive", "proxy-authenticate",
                         "proxy-authorization", "te", "trailer", "transfer-encoding",
                         "upgrade"})


def _headers(headers):
    blocked = set(HOP_HEADERS)
    for value in headers.getall("Connection", []):
        blocked.update(part.strip().lower() for part in value.split(","))
    return [(key, value) for key, value in headers.items()
            if key.lower() not in blocked and key.lower() != "host"]


def _counter():
    return {"received": 0, "completed": 0, "errors": 0, "rejected": 0,
            "active": 0, "queued": 0, "peak_active": 0, "peak_queued": 0,
            "queue_wait_seconds": 0.0, "max_queue_wait_seconds": 0.0,
            "http_statuses": {}, "successful_statuses": 0,
            "gateway_statuses": {}}


class Gateway:
    def __init__(self, upstream, limit, max_pending, tokenize_limit, request_timeout,
                 max_body_bytes, stats_path):
        parsed = urlsplit(upstream)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ValueError("upstream must be an http(s) origin without credentials or path")
        if min(limit, tokenize_limit, request_timeout, max_body_bytes) <= 0 or max_pending < 0:
            raise ValueError("limits and timeout must be positive; max_pending may be zero")
        self.upstream = upstream.rstrip("/")
        self.limit = limit
        self.hard_limit = limit
        self.paused = False
        self.phase = None
        self.max_pending = max_pending
        self.tokenize_limit = tokenize_limit
        self.request_timeout = request_timeout
        self.max_body_bytes = max_body_bytes
        self.stats_path = Path(stats_path) if stats_path else None
        self.admission = asyncio.Condition()
        self.token_sem = asyncio.Semaphore(tokenize_limit)
        self.total = _counter()
        self.routes = {}
        self.jobs = {}
        self.session = None
        self.writer = None
        self.producer_budget = ProducerBudget()
        self.producer_processes = {}

    def buckets(self, route, job):
        result = [self.total, self.routes.setdefault(route, _counter())]
        if job is not None:
            entry = self.jobs.setdefault(job, {"total": _counter(), "routes": {}})
            result.extend((entry["total"], entry["routes"].setdefault(route, _counter())))
        return result

    def bump(self, buckets, key, delta=1):
        for bucket in buckets:
            bucket[key] += delta
            if key in ("active", "queued"):
                peak = "peak_" + key
                bucket[peak] = max(bucket[peak], bucket[key])

    def record_status(self, buckets, status, *, gateway=False):
        for bucket in buckets:
            statuses = bucket["gateway_statuses" if gateway else "http_statuses"]
            statuses[str(status)] = statuses.get(str(status), 0) + 1
            if not gateway and 200 <= status < 300:
                bucket["successful_statuses"] += 1

    def record_queue_wait(self, buckets, waited):
        for bucket in buckets:
            bucket["queue_wait_seconds"] += waited
            bucket["max_queue_wait_seconds"] = max(bucket["max_queue_wait_seconds"], waited)

    def snapshot(self):
        return {"upstream": self.upstream, "limit": self.limit,
                "hard_limit": self.hard_limit, "paused": self.paused, "phase": self.phase,
                "max_pending": self.max_pending, "tokenize_limit": self.tokenize_limit,
                "total": self.total, "routes": self.routes, "jobs": self.jobs,
                "producer_pool": {"active": self.producer_budget.active,
                                  "owners": dict(self.producer_budget.owners),
                                  "waiting": len(self.producer_budget.waiters)}}

    async def producers(self, request):
        """One bounded producer budget shared by all native child processes."""
        try:
            local = ipaddress.ip_address(request.remote or '').is_loopback
        except ValueError:
            local = False
        if not local:
            raise web.HTTPForbidden(text='loopback access required')
        if request.content_length is None or request.content_length > 4096:
            raise web.HTTPBadRequest(text='bounded producer control body required')
        try:
            payload = await request.json()
            owner, op, count = payload['owner'], payload['op'], payload['count']
            pid, start = payload['pid'], payload['start_ticks']
            if (not isinstance(owner, str) or not re.fullmatch(r'[a-f0-9]{32}', owner)
                    or op not in ('claim', 'release', 'close')
                    or type(pid) is not int or pid < 1 or type(start) is not int
                    or process_start(pid) != start):
                raise ValueError('Producer identity or operation invalid')
            # Do not reclaim a slow or stopped process's slots just because a
            # timer expired. A dead process or a different PID incarnation is
            # the only implicit release; a frozen writer retains ownership.
            for key, identity in list(self.producer_processes.items()):
                if process_start(identity[0]) != identity[1]:
                    self.producer_budget.close(key)
                    del self.producer_processes[key]
            identity = (pid, start)
            if owner in self.producer_processes and self.producer_processes[owner] != identity:
                raise ValueError('Producer owner identity changed')
            self.producer_processes[owner] = identity
            granted = 0
            if op == 'claim':
                if type(count) is not int or count < 1:
                    raise ValueError('Invalid producer claim')
                granted = self.producer_budget.claim(owner, count, limit=self.limit, paused=self.paused)
            elif op == 'release':
                self.producer_budget.release(owner, count)
            else:
                self.producer_budget.close(owner)
                self.producer_processes.pop(owner, None)
        except (ValueError, KeyError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response({'granted': granted, 'active': self.producer_budget.active,
                                  'limit': self.limit, 'paused': self.paused})

    def write_stats(self):
        if not self.stats_path:
            return
        self.stats_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.stats_path.with_name(self.stats_path.name + ".tmp")
        temporary.write_text(json.dumps(self.snapshot(), sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.stats_path)

    async def periodic_stats(self):
        while True:
            await asyncio.sleep(1)
            self.write_stats()

    async def startup(self, app):
        connector = TCPConnector(limit=self.limit, limit_per_host=self.limit)
        self.session = ClientSession(connector=connector, auto_decompress=False, trust_env=False,
                                     timeout=ClientTimeout(total=self.request_timeout))
        self.write_stats()
        if self.stats_path:
            self.writer = asyncio.create_task(self.periodic_stats())

    async def cleanup(self, app):
        if self.writer:
            self.writer.cancel()
            try:
                await self.writer
            except asyncio.CancelledError:
                pass
        self.write_stats()
        await self.session.close()

    async def local(self, request):
        if request.path == "/_scheduler/health":
            return web.json_response({"status": "ok"})
        return web.json_response(self.snapshot())

    async def control(self, request):
        try:
            remote = ipaddress.ip_address(request.remote or "")
        except ValueError:
            raise web.HTTPForbidden(text="loopback access required")
        if not remote.is_loopback:
            raise web.HTTPForbidden(text="loopback access required")
        if request.content_type != "application/json":
            raise web.HTTPBadRequest(text="control requires application/json")
        if request.content_length is not None and request.content_length > 4096:
            raise web.HTTPBadRequest(text="control body too large")
        try:
            body = bytearray()
            while len(body) <= 4096:
                chunk = await request.content.read(4097 - len(body))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > 4096:
                    raise web.HTTPBadRequest(text="control body too large")
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="invalid JSON")
        if (not isinstance(payload, dict) or not payload or
                set(payload) - {"limit", "paused", "phase"}):
            raise web.HTTPBadRequest(text="invalid control fields")
        if "limit" in payload and (type(payload["limit"]) is not int or
                                   not 1 <= payload["limit"] <= self.hard_limit):
            raise web.HTTPBadRequest(text="limit exceeds startup cap or is invalid")
        if "paused" in payload and type(payload["paused"]) is not bool:
            raise web.HTTPBadRequest(text="paused must be boolean")
        if "phase" in payload and (not isinstance(payload["phase"], str) or
                                   not 1 <= len(payload["phase"]) <= 128 or
                                   not payload["phase"].strip()):
            raise web.HTTPBadRequest(text="phase must be a nonempty string")
        async with self.admission:
            self.limit = payload.get("limit", self.limit)
            self.paused = payload.get("paused", self.paused)
            self.phase = payload.get("phase", self.phase)
            self.admission.notify_all()
        self.write_stats()
        return web.json_response(self.snapshot())

    async def disconnected(self, request, task):
        while not task.done():
            await asyncio.sleep(0.1)
            transport = request.transport
            if transport is None or transport.is_closing():
                task.cancel()
                return

    async def relay(self, request):
        path = request.path
        job = None
        match = JOB_PATH.fullmatch(path)
        if match:
            job, route = match.groups()
        else:
            route = path.lstrip("/")
        if route not in ROUTES:
            raise web.HTTPNotFound()
        buckets = self.buckets(route, job)
        self.bump(buckets, "received")
        if self.total["active"] + self.total["queued"] >= self.hard_limit + self.max_pending:
            self.bump(buckets, "rejected")
            return web.json_response({"error": "gateway_queue_full", "retryable": True},
                                     status=429, headers={"Retry-After": "1"})
        length = request.content_length
        if length is not None and length > self.max_body_bytes:
            self.bump(buckets, "errors")
            raise web.HTTPRequestEntityTooLarge(max_size=self.max_body_bytes, actual_size=length)

        self.bump(buckets, "queued")
        queued_at = time.monotonic()
        token_acquired = active = False
        response = None
        watcher = asyncio.create_task(self.disconnected(request, asyncio.current_task()))
        try:
            if route == "tokenize":
                await self.token_sem.acquire()
                token_acquired = True
            async with self.admission:
                await self.admission.wait_for(
                    lambda: not self.paused and self.total["active"] < self.limit)
                self.bump(buckets, "queued", -1)
                self.record_queue_wait(buckets, time.monotonic() - queued_at)
                self.bump(buckets, "active")
                active = True
            size = 0

            async def body():
                nonlocal size
                async for chunk in request.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > self.max_body_bytes:
                        raise web.HTTPRequestEntityTooLarge(max_size=self.max_body_bytes,
                                                            actual_size=size)
                    yield chunk

            target = self.upstream + "/" + route
            raw_query = request.raw_path.partition("?")[2]
            if raw_query:
                target += "?" + raw_query
            async with self.session.request(request.method, URL(target, encoded=True),
                                            data=body() if request.can_read_body else None,
                                            headers=_headers(request.headers),
                                            allow_redirects=False) as upstream:
                response = web.StreamResponse(status=upstream.status, reason=upstream.reason,
                                              headers=_headers(upstream.headers))
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(65536):
                    await response.write(chunk)
                await response.write_eof()
                self.bump(buckets, "completed")
                self.record_status(buckets, upstream.status)
                return response
        except web.HTTPException:
            self.bump(buckets, "errors")
            raise
        except (ClientError, asyncio.TimeoutError) as exc:
            self.bump(buckets, "errors")
            if size > self.max_body_bytes:
                raise web.HTTPRequestEntityTooLarge(max_size=self.max_body_bytes,
                                                    actual_size=size) from exc
            if response is not None and response.prepared:
                response.force_close()
                transport = request.transport
                if transport is not None:
                    transport.close()
                raise asyncio.CancelledError() from exc
            status = 504 if isinstance(exc, asyncio.TimeoutError) else 502
            self.record_status(buckets, status, gateway=True)
            return web.json_response({"error": "upstream_timeout" if status == 504 else "upstream_error",
                                      "retryable": True}, status=status)
        except (asyncio.CancelledError, ConnectionError):
            self.bump(buckets, "errors")
            raise
        finally:
            if watcher:
                watcher.cancel()
            if active:
                async with self.admission:
                    self.bump(buckets, "active", -1)
                    self.admission.notify(1)
            else:
                self.bump(buckets, "queued", -1)
            if token_acquired:
                self.token_sem.release()


def create_app(upstream, limit=1024, max_pending=2048, tokenize_limit=64,
               request_timeout=900, max_body_bytes=16777216, stats_path=None):
    gateway = Gateway(upstream, limit, max_pending, tokenize_limit,
                      request_timeout, max_body_bytes, stats_path)
    app = web.Application(client_max_size=max_body_bytes)
    app.router.add_get("/_scheduler/health", gateway.local)
    app.router.add_get("/_scheduler/stats", gateway.local)
    app.router.add_post("/_scheduler/control", gateway.control)
    app.router.add_post("/_scheduler/producers", gateway.producers)
    app.router.add_route("*", "/{tail:.*}", gateway.relay)
    app.on_startup.append(gateway.startup)
    app.on_cleanup.append(gateway.cleanup)
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--limit", type=int, default=1024)
    parser.add_argument("--max-pending", type=int, default=2048)
    parser.add_argument("--tokenize-limit", type=int, default=64)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.host != "127.0.0.1" or not 0 <= args.port <= 65535:
        parser.error("gateway only binds 127.0.0.1 and port 0..65535")

    async def run():
        args.out.mkdir(parents=True, exist_ok=True)
        ready_path = args.out / "READY.json"
        app = create_app(args.upstream, limit=args.limit, max_pending=args.max_pending,
                         tokenize_limit=args.tokenize_limit, stats_path=args.out / "STATS.json")
        runner = web.AppRunner(app)
        await runner.setup()
        stopping = asyncio.Event()
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGTERM, stopping.set)
            signal_installed = True
        except NotImplementedError:
            signal_installed = False
        try:
            site = web.TCPSite(runner, args.host, args.port)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            ready = {"api_url": f"http://127.0.0.1:{port}", "port": port,
                     "upstream": args.upstream, "limit": args.limit}
            ready_path.write_text(json.dumps(ready) + "\n", encoding="utf-8")
            await stopping.wait()
        finally:
            try:
                await runner.cleanup()
            finally:
                ready_path.unlink(missing_ok=True)
                if signal_installed:
                    loop.remove_signal_handler(signal.SIGTERM)

    asyncio.run(run())


if __name__ == "__main__":
    main()
