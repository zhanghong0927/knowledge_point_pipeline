import asyncio
import gzip
import io
import json
import contextlib
import os
import signal
import subprocess
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession, TCPConnector, web
from yarl import URL

from adapters.inference_gateway import create_app
from adapters.inference_gateway import main


async def serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop = asyncio.get_running_loop()
        self.original_slow_callback_duration = self.loop.slow_callback_duration
        self.loop.slow_callback_duration = 1.0
        self.runners = []
        self.session = ClientSession(auto_decompress=False)

    async def asyncTearDown(self):
        await self.session.close()
        for runner in reversed(self.runners):
            await runner.cleanup()
        self.loop.slow_callback_duration = self.original_slow_callback_duration

    async def gateway(self, handler, **options):
        upstream = web.Application()
        upstream.router.add_route("*", "/{tail:.*}", handler)
        upstream_runner, upstream_url = await serve(upstream)
        self.runners.append(upstream_runner)
        gateway_runner, gateway_url = await serve(create_app(upstream_url, **options))
        self.runners.append(gateway_runner)
        return gateway_url

    async def test_job_path_passthrough_and_stats(self):
        seen = []

        async def upstream(request):
            seen.append((request.path_qs, request.headers.get("Authorization"), await request.read()))
            return web.Response(status=201, body=b'{"ok":true}', content_type="application/json",
                                headers={"X-Upstream": "yes"})

        url = await self.gateway(upstream)
        async with self.session.post(url + "/_jobs/math_1/v1/chat/completions?x=1",
                                     data=b'{"a":1}', headers={"Authorization": "Bearer secret"}) as response:
            self.assertEqual(response.status, 201)
            self.assertEqual(await response.read(), b'{"ok":true}')
            self.assertEqual(response.headers["X-Upstream"], "yes")
        self.assertEqual(seen, [("/v1/chat/completions?x=1", "Bearer secret", b'{"a":1}')])
        async with self.session.get(url + "/_scheduler/stats") as response:
            stats = await response.json()
        self.assertEqual(stats["jobs"]["math_1"]["routes"]["v1/chat/completions"]["completed"], 1)
        async with self.session.get(url + "/_scheduler/health") as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(len(seen), 1)

    async def test_global_cap_across_clients_and_tokenize_subcap(self):
        active = peak = tokenize_active = tokenize_peak = 0
        release = asyncio.Event()

        async def upstream(request):
            nonlocal active, peak, tokenize_active, tokenize_peak
            active += 1
            peak = max(peak, active)
            if request.path == "/tokenize":
                tokenize_active += 1
                tokenize_peak = max(tokenize_peak, tokenize_active)
            try:
                await release.wait()
                return web.Response(text="ok")
            finally:
                active -= 1
                if request.path == "/tokenize":
                    tokenize_active -= 1

        url = await self.gateway(upstream, limit=3, max_pending=12, tokenize_limit=1)
        async def call(path):
            async with self.session.post(url + path) as response:
                return response.status, await response.text()

        tasks = [asyncio.create_task(call(f"/_jobs/j{i % 2}/" + ("tokenize" if i < 4 else "v1/chat/completions")))
                 for i in range(9)]
        try:
            deadline = asyncio.get_running_loop().time() + 2
            while peak < 2 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            self.assertLessEqual(peak, 3)
            self.assertLessEqual(tokenize_peak, 1)
            observed_peak = peak
        finally:
            release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertGreaterEqual(observed_peak, 2, results)
        self.assertEqual([status for status, _ in results], [200] * 9)

    async def test_upstream_connections_can_exceed_aiohttp_default_100(self):
        active = peak = 0
        release = asyncio.Event()

        async def upstream(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await release.wait()
                return web.Response(text="ok")
            finally:
                active -= 1

        url = await self.gateway(upstream, limit=128, max_pending=0)

        async def call(session):
            async with session.get(url + "/v1/models") as response:
                self.assertEqual(response.status, 200)

        async with ClientSession(connector=TCPConnector(limit=0)) as session:
            tasks = [asyncio.create_task(call(session)) for _ in range(112)]
            try:
                deadline = asyncio.get_running_loop().time() + 10
                while peak <= 100 and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.05)
                observed_peak = peak
            finally:
                release.set()
            await asyncio.gather(*tasks)
        self.assertGreater(observed_peak, 100)
        self.assertLessEqual(peak, 128)

    async def test_bounded_queue_and_error_release(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def upstream(request):
            entered.set()
            if request.query.get("fail"):
                raise web.HTTPBadGateway()
            await release.wait()
            return web.Response(text="ok")

        url = await self.gateway(upstream, limit=1, max_pending=1)
        first = asyncio.create_task(self.session.get(url + "/v1/models"))
        await entered.wait()
        second = asyncio.create_task(self.session.get(url + "/v1/models"))
        await asyncio.sleep(0.05)
        async with self.session.get(url + "/v1/models") as rejected:
            self.assertEqual(rejected.status, 429)
            self.assertIn("Retry-After", rejected.headers)
        release.set()
        for task in (first, second):
            response = await task
            self.assertEqual(response.status, 200)
            await response.release()
        async with self.session.get(url + "/v1/models?fail=1") as failed:
            self.assertEqual(failed.status, 502)
        async with self.session.get(url + "/v1/models") as recovered:
            self.assertEqual(recovered.status, 200)

    async def test_streaming_response_and_restricted_routes(self):
        async def upstream(request):
            response = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"data: first\n\n")
            await asyncio.sleep(0.02)
            await response.write(b"data: second\n\n")
            await response.write_eof()
            return response

        url = await self.gateway(upstream)
        async with self.session.post(url + "/v1/chat/completions") as response:
            chunks = [chunk async for chunk in response.content.iter_any()]
            self.assertEqual(b"".join(chunks), b"data: first\n\ndata: second\n\n")
            self.assertEqual(response.headers["Content-Type"], "text/event-stream")
        for path in ("/other", "/_jobs/bad.id/tokenize", "/_jobs/" + "x" * 129 + "/tokenize"):
            async with self.session.get(url + path) as response:
                self.assertEqual(response.status, 404)

    async def test_stats_file_does_not_contain_sensitive_values(self):
        async def upstream(request):
            await request.read()
            return web.Response(text="ok")

        with tempfile.TemporaryDirectory() as directory:
            stats_file = Path(directory) / "STATS.json"
            url = await self.gateway(upstream, stats_path=stats_file)
            async with self.session.post(url + "/tokenize", data=b"private-prompt",
                                         headers={"Authorization": "Bearer private-key"}) as response:
                self.assertEqual(response.status, 200)
            await asyncio.sleep(0.05)
            contents = stats_file.read_text(encoding="utf-8")
            self.assertNotIn("private-prompt", contents)
            self.assertNotIn("private-key", contents)
            self.assertEqual(json.loads(contents)["total"]["completed"], 0)
            deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < deadline:
                contents = stats_file.read_text(encoding="utf-8")
                if json.loads(contents)["total"]["completed"] == 1:
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(json.loads(contents)["routes"]["tokenize"]["completed"], 1)
            await self.runners.pop().cleanup()

    async def test_upstream_timeout_releases_capacity(self):
        async def upstream(request):
            if request.query.get("slow"):
                await asyncio.sleep(1)
            return web.Response(text="ok")

        url = await self.gateway(upstream, limit=1, request_timeout=0.05)
        async with self.session.get(url + "/v1/models?slow=1") as response:
            self.assertEqual(response.status, 504)
        async with self.session.get(url + "/v1/models") as response:
            self.assertEqual(response.status, 200)

    async def test_stream_timeout_after_headers_interrupts_response(self):
        async def upstream(request):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"data: first\n\n")
            await asyncio.sleep(1)
            try:
                await response.write(b"data: late\n\n")
            except ConnectionError:
                pass
            return response

        url = await self.gateway(upstream, limit=1, request_timeout=0.15)
        with patch("adapters.inference_gateway.web.json_response", wraps=web.json_response) as json_response:
            async with self.session.get(url + "/v1/chat/completions") as response:
                self.assertEqual(response.status, 200)
                with self.assertRaises(Exception) as interrupted:
                    await asyncio.wait_for(response.read(), 1)
                self.assertNotIsInstance(interrupted.exception, asyncio.TimeoutError)
            self.assertEqual(json_response.call_count, 0)
        async with self.session.get(url + "/_scheduler/stats") as response:
            stats = await response.json()
        self.assertEqual(stats["total"]["errors"], 1)
        self.assertEqual(stats["total"]["completed"], 0)

    async def test_gzip_body_and_headers_remain_consistent(self):
        payload = gzip.compress(b'{"items":[1,2]}')

        async def upstream(request):
            return web.Response(body=payload, headers={"Content-Encoding": "gzip",
                                                      "Content-Type": "application/json"})

        url = await self.gateway(upstream)
        async with self.session.get(url + "/v1/models") as response:
            self.assertEqual(response.headers["Content-Encoding"], "gzip")
            self.assertEqual(await response.read(), payload)

    async def test_encoded_query_is_forwarded_unchanged(self):
        seen = []

        async def upstream(request):
            seen.append(request.raw_path)
            return web.Response(text="ok")

        url = await self.gateway(upstream)
        async with self.session.get(URL(url + "/_jobs/a/v1/models?x=%2F%25&y=a+b",
                                        encoded=True)) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(seen, ["/v1/models?x=%2F%25&y=a+b"])

    async def test_empty_get_does_not_become_chunked_upload(self):
        seen = []

        async def upstream(request):
            seen.append((request.headers.get("Transfer-Encoding"), request.headers.get("Content-Length")))
            return web.Response(text="ok")

        url = await self.gateway(upstream)
        async with self.session.get(url + "/v1/models") as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(seen, [(None, None)])

    async def test_redirect_is_not_followed(self):
        calls = 0

        async def upstream(request):
            nonlocal calls
            calls += 1
            raise web.HTTPFound("http://example.invalid/steal")

        url = await self.gateway(upstream)
        async with self.session.post(url + "/tokenize", data=b"secret", allow_redirects=False) as response:
            self.assertEqual(response.status, 302)
            self.assertEqual(response.headers["Location"], "http://example.invalid/steal")
        self.assertEqual(calls, 1)

    async def test_queued_disconnect_releases_queue_slot(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def upstream(request):
            entered.set()
            await release.wait()
            return web.Response(text="ok")

        url = await self.gateway(upstream, limit=1, max_pending=1)
        first = asyncio.create_task(self.session.get(url + "/v1/models"))
        await entered.wait()
        second = asyncio.create_task(self.session.get(url + "/v1/models"))
        await asyncio.sleep(0.05)
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        try:
            for _ in range(20):
                async with self.session.get(url + "/_scheduler/stats") as response:
                    stats = await response.json()
                if stats["total"]["queued"] == 0:
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(stats["total"]["queued"], 0)
        finally:
            release.set()
            response = await first
            await response.release()

    async def test_cli_rejects_external_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(["--upstream", "http://127.0.0.1:1234", "--host", "0.0.0.0",
                      "--out", directory])

    async def test_active_disconnect_releases_capacity(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def upstream(request):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"data: one\n\n")
            started.set()
            await release.wait()
            return response

        url = await self.gateway(upstream, limit=1, max_pending=0)
        response = await self.session.get(url + "/v1/chat/completions")
        await started.wait()
        self.assertEqual(await response.content.read(11), b"data: one\n\n")
        response.close()
        try:
            for _ in range(30):
                async with self.session.get(url + "/_scheduler/stats") as stats_response:
                    stats = await stats_response.json()
                if stats["total"]["active"] == 0:
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(stats["total"]["active"], 0)
        finally:
            release.set()

    async def test_chunked_body_limit_releases_capacity(self):
        async def upstream(request):
            await request.read()
            return web.Response(text="ok")

        url = await self.gateway(upstream, limit=1, max_body_bytes=8)

        async def body():
            yield b"12345"
            yield b"67890"

        async with self.session.post(url + "/tokenize", data=body()) as response:
            self.assertEqual(response.status, 413)
        async with self.session.get(url + "/v1/models") as response:
            self.assertEqual(response.status, 200)

    async def test_cli_ready_and_stats_without_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.Popen(
                [sys.executable, "-m", "adapters.inference_gateway", "--upstream",
                 "http://127.0.0.1:9", "--port", "0", "--out", directory],
                env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                ready_path = Path(directory) / "READY.json"
                deadline = time.monotonic() + 5
                while not ready_path.exists() and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                self.assertTrue(ready_path.exists(), process.poll())
                ready = json.loads(ready_path.read_text(encoding="utf-8"))
                self.assertEqual(ready["api_url"], f"http://127.0.0.1:{ready['port']}")
                self.assertEqual(ready["limit"], 1024)
                self.assertEqual(ready["upstream"], "http://127.0.0.1:9")
                async with self.session.get(ready["api_url"] + "/_scheduler/stats") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual((await response.json())["total"]["received"], 0)
                self.assertTrue((Path(directory) / "STATS.json").exists())
            finally:
                process.terminate()
                process.wait(timeout=5)
                process.stderr.close()

    @unittest.skipIf(os.name == "nt", "Windows terminate() bypasses Python SIGTERM handlers")
    async def test_sigterm_flushes_stats_and_removes_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.Popen(
                [sys.executable, "-m", "adapters.inference_gateway", "--upstream",
                 "http://127.0.0.1:9", "--out", directory],
                env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            ready_path = Path(directory) / "READY.json"
            try:
                deadline = time.monotonic() + 5
                while not ready_path.exists() and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                self.assertTrue(ready_path.exists())
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertFalse(ready_path.exists())
                self.assertTrue((Path(directory) / "STATS.json").exists())
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                process.stderr.close()
