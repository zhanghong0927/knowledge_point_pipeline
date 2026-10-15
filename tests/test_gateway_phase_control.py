"""Real loopback tests for gateway phase admission and accounting."""

import asyncio
import unittest

from aiohttp import ClientSession, web

from adapters.gateway_control import transition_phase
from adapters.inference_gateway import create_app


async def serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


class PhaseControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runners = []
        self.session = ClientSession()
        self.loop = asyncio.get_running_loop()
        self.old_slow_callback_duration = self.loop.slow_callback_duration
        self.loop.slow_callback_duration = 1.0

    async def asyncTearDown(self):
        await self.session.close()
        for runner in reversed(self.runners):
            await runner.cleanup()
        self.loop.slow_callback_duration = self.old_slow_callback_duration

    async def gateway(self, handler, **options):
        upstream = web.Application()
        upstream.router.add_route("*", "/{tail:.*}", handler)
        upstream_runner, upstream_url = await serve(upstream)
        self.runners.append(upstream_runner)
        gateway_runner, gateway_url = await serve(create_app(upstream_url, **options))
        self.runners.append(gateway_runner)
        return gateway_url

    async def stats(self, url):
        async with self.session.get(url + "/_scheduler/stats") as response:
            self.assertEqual(response.status, 200)
            return await response.json()

    async def control(self, url, payload):
        async with self.session.post(url + "/_scheduler/control", json=payload) as response:
            return response.status, (await response.json() if response.status == 200
                                     else await response.text())

    async def test_control_waits_for_complete_chunked_body_before_mutation(self):
        async def handler(_request):
            return web.json_response({"ok": True})
        url = await self.gateway(handler, limit=4)
        for chunks in ((b'{"limit":1}', b'garbage'),
                       (b'{"limit":1}', b' ' * 4096)):
            async def delayed_body():
                yield chunks[0]
                await asyncio.sleep(.05)
                yield chunks[1]
            async with self.session.post(url + "/_scheduler/control", data=delayed_body(),
                    headers={"Content-Type": "application/json"}) as response:
                self.assertEqual(response.status, 400)
            self.assertEqual((await self.stats(url))["limit"], 4)
        async def valid_split_body():
            yield b'{"lim'
            await asyncio.sleep(.05)
            yield b'it":1}'
        async with self.session.post(url + "/_scheduler/control", data=valid_split_body(),
                headers={"Content-Type": "application/json"}) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual((await self.stats(url))["limit"], 1)

    async def wait_until(self, predicate, timeout=2):
        end = asyncio.get_running_loop().time() + timeout
        while not await predicate():
            if asyncio.get_running_loop().time() >= end:
                self.fail("condition did not become true")
            await asyncio.sleep(0.01)

    async def request(self, url, suffix=""):
        async with self.session.get(url + "/v1/models" + suffix) as response:
            return response.status, await response.text()

    async def test_shrinking_limit_waits_for_inflight_without_losing_capacity(self):
        entered = 0
        upstream_active = peak = 0
        release_initial = asyncio.Event()
        release_next = asyncio.Event()

        async def upstream(request):
            nonlocal entered, upstream_active, peak
            entered += 1
            upstream_active += 1
            peak = max(peak, upstream_active)
            try:
                await (release_initial if entered <= 3 else release_next).wait()
                return web.Response(text="ok")
            finally:
                upstream_active -= 1

        url = await self.gateway(upstream, limit=3, max_pending=2)
        first = [asyncio.create_task(self.request(url)) for _ in range(3)]

        async def three_entered():
            return entered == 3

        await self.wait_until(three_entered)
        status, controlled = await self.control(url, {"limit": 1, "phase": "retry"})
        self.assertEqual(status, 200)
        self.assertEqual((controlled["limit"], controlled["hard_limit"], controlled["phase"]),
                         (1, 3, "retry"))
        fourth = asyncio.create_task(self.request(url))
        fifth = asyncio.create_task(self.request(url))
        try:
            await asyncio.sleep(0.05)
            self.assertEqual(entered, 3)
            self.assertEqual((await self.stats(url))["total"]["queued"], 2)
            release_initial.set()

            async def fourth_entered():
                return entered == 4

            await self.wait_until(fourth_entered)
            await asyncio.sleep(0.05)
            self.assertEqual(entered, 4, "effective cap must hold the fifth request")
            self.assertEqual(peak, 3, "shrink must not admit while prior requests are active")
        finally:
            release_initial.set()
            release_next.set()
        self.assertEqual([result[0] for result in await asyncio.gather(*first, fourth, fifth)], [200] * 5)
        self.assertEqual(entered, 5)
        self.assertEqual((await self.stats(url))["total"]["active"], 0)
        self.assertEqual((await self.request(url))[0], 200)

    async def test_pause_holds_pending_requests_and_resume_drains_them(self):
        entered = 0

        async def upstream(request):
            nonlocal entered
            entered += 1
            return web.Response(text="ok")

        url = await self.gateway(upstream, limit=1, max_pending=1)
        self.assertEqual((await self.control(url, {"paused": True}))[0], 200)
        pending = asyncio.create_task(self.request(url))

        async def queued():
            return (await self.stats(url))["total"]["queued"] == 1

        await self.wait_until(queued)
        self.assertEqual(entered, 0)
        async with self.session.get(url + "/_scheduler/health") as health:
            self.assertEqual(health.status, 200)
        self.assertEqual((await self.control(url, {"limit": 1, "phase": "resume", "paused": False}))[0], 200)
        self.assertEqual((await pending)[0], 200)
        self.assertEqual(entered, 1)

    async def test_token_waiter_does_not_block_drain_or_escape_pause(self):
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        entered = 0

        async def upstream(request):
            nonlocal entered
            entered += 1
            if entered == 1:
                first_started.set()
                await release_first.wait()
            return web.Response(text="ok")

        url = await self.gateway(upstream, limit=2, tokenize_limit=1, max_pending=1)
        first = asyncio.create_task(self.session.post(url + "/tokenize"))
        await first_started.wait()
        second = asyncio.create_task(self.session.post(url + "/tokenize"))

        async def queued():
            return (await self.stats(url))["total"]["queued"] == 1

        await self.wait_until(queued)
        transition = asyncio.create_task(asyncio.to_thread(
            transition_phase, url, 1, "token-next", timeout=2, poll_interval=0.01))

        async def paused():
            return (await self.stats(url))["paused"] is True

        await self.wait_until(paused)
        release_first.set()
        response = await first
        await response.release()
        state = await transition
        self.assertEqual((state["limit"], state["phase"], state["paused"]), (1, "token-next", False))
        response = await second
        self.assertEqual(response.status, 200)
        await response.release()
        self.assertEqual(entered, 2)

    async def test_transition_timeout_keeps_gateway_paused_then_recovery(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def upstream(request):
            started.set()
            await release.wait()
            return web.Response(text="ok")

        url = await self.gateway(upstream, limit=2, max_pending=2)
        active = asyncio.create_task(self.request(url))
        await started.wait()
        with self.assertRaises(TimeoutError):
            await asyncio.to_thread(transition_phase, url, 1, "next", timeout=0.05, poll_interval=0.01)
        paused = await self.stats(url)
        self.assertTrue(paused["paused"])
        self.assertEqual(paused["total"]["active"], 1)
        pending = asyncio.create_task(self.request(url))

        async def queued():
            return (await self.stats(url))["total"]["queued"] == 1

        await self.wait_until(queued)
        release.set()
        self.assertEqual((await active)[0], 200)
        final = await asyncio.to_thread(transition_phase, url, 1, "next", timeout=2, poll_interval=0.01)
        self.assertEqual((final["limit"], final["phase"], final["paused"]), (1, "next", False))
        self.assertEqual((await pending)[0], 200)

    async def test_control_validation_is_atomic_and_rejects_bad_types(self):
        async def upstream(request):
            return web.Response(text="ok")

        url = await self.gateway(upstream, limit=2)
        bad = ({"limit": 3}, {"limit": 0}, {"limit": True}, {"limit": 1.5},
               {"paused": 1}, {"phase": ""}, {"phase": 123}, {"unknown": 1},
               {"limit": 1, "phase": ""})
        for payload in bad:
            with self.subTest(payload=payload):
                self.assertEqual((await self.control(url, payload))[0], 400)
                state = await self.stats(url)
                self.assertEqual((state["limit"], state["paused"], state["phase"]), (2, False, None))
        async with self.session.post(url + "/_scheduler/control", data="{bad",
                                     headers={"Content-Type": "application/json"}) as response:
            self.assertEqual(response.status, 400)
        async def oversized_chunked():
            yield b" " * 5000
            yield b'{"limit":1}'

        async with self.session.post(url + "/_scheduler/control", data=oversized_chunked(),
                                     headers={"Content-Type": "application/json"}) as response:
            self.assertEqual(response.status, 400)
        async with self.session.post(url + "/_scheduler/control", data='{"limit":1}',
                                     headers={"Content-Type": "text/plain"}) as response:
            self.assertEqual(response.status, 400)
        self.assertEqual((await self.stats(url))["limit"], 2)

    async def test_http_504_completes_transport_but_not_successful_status(self):
        async def upstream(request):
            return web.Response(status=504, text="overloaded")

        url = await self.gateway(upstream)
        self.assertEqual((await self.request(url))[0], 504)
        total = (await self.stats(url))["total"]
        self.assertEqual(total["completed"], 1)
        self.assertEqual(total["errors"], 0)
        self.assertEqual(total["http_statuses"].get("504"), 1)
        self.assertEqual(total["successful_statuses"], 0)

    async def test_gateway_timeout_has_own_status_bucket(self):
        async def upstream(request):
            await asyncio.sleep(1)
            return web.Response(text="late")

        url = await self.gateway(upstream, limit=1, request_timeout=0.05)
        self.assertEqual((await self.request(url))[0], 504)
        total = (await self.stats(url))["total"]
        self.assertEqual(total["completed"], 0)
        self.assertEqual(total["errors"], 1)
        self.assertEqual(total["http_statuses"], {})
        self.assertEqual(total["gateway_statuses"].get("504"), 1)
        self.assertGreaterEqual(total["queue_wait_seconds"], 0)

    async def test_cancelled_pending_request_does_not_leak_capacity(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def upstream(request):
            entered.set()
            await release.wait()
            return web.Response(text="ok")

        url = await self.gateway(upstream, limit=1, max_pending=1)
        active = asyncio.create_task(self.request(url))
        await entered.wait()
        queued = asyncio.create_task(self.request(url))

        async def is_queued():
            return (await self.stats(url))["total"]["queued"] == 1

        await self.wait_until(is_queued)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued

        async def drained():
            return (await self.stats(url))["total"]["queued"] == 0

        await self.wait_until(drained)
        release.set()
        self.assertEqual((await active)[0], 200)
        self.assertEqual((await self.request(url))[0], 200)


if __name__ == "__main__":
    unittest.main()
