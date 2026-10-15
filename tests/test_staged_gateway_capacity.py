"""Observe the declared phase caps at a held, literal-loopback upstream."""
import asyncio
import json
import sys
import unittest

from aiohttp import ClientSession, TCPConnector, web
from adapters.gateway_control import transition_phase
from adapters.inference_gateway import create_app
from stage_scheduling import ResourceProfile


@unittest.skipIf(sys.platform == 'win32', 'held-socket capacity test runs in Linux QA')
class StagedGatewayCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_and_recovery_caps_are_observed_upstream(self):
        profile = ResourceProfile()
        runners = []
        held = asyncio.Event()
        active = peak = received = 0
        observed = []
        loop = asyncio.get_running_loop()
        old_duration = loop.slow_callback_duration
        loop.slow_callback_duration = 1.0

        async def serve(app):
            runner = web.AppRunner(app)
            await runner.setup()
            runners.append(runner)
            site = web.TCPSite(runner, '127.0.0.1', 0, backlog=4096)
            await site.start()
            return f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'

        async def upstream(request):
            nonlocal active, peak, received
            await request.read()
            active += 1
            received += 1
            peak = max(peak, active)
            try:
                await held.wait()
                return web.json_response({'choices': [{'message': {'content': '{}'}}]})
            finally:
                active -= 1

        tasks = []
        try:
            app = web.Application()
            app.router.add_post('/v1/chat/completions', upstream)
            fake_url = await serve(app)
            gateway_url = await serve(create_app(fake_url, limit=profile.global_limit,
                                                  max_pending=1024))
            async with ClientSession(connector=TCPConnector(limit=0)) as client:
                async def submit(index):
                    job = 'book_' + str(index % 8)
                    async with client.post(gateway_url + '/_jobs/' + job + '/v1/chat/completions',
                                           json={'model': 'offline', 'messages': []}) as response:
                        self.assertEqual(response.status, 200)
                        await response.read()
                rounds = [(kind, number, limit) for kind in ('front', 'production')
                          for number, limit in enumerate(profile.round_limits_for(kind), 1)]
                for kind, number, limit in rounds:
                    held.clear()
                    peak = received = 0
                    snapshot = await asyncio.to_thread(transition_phase, gateway_url, limit,
                                                        'capacity_' + kind + '_' + str(number))
                    self.assertEqual(snapshot['limit'], limit)
                    self.assertEqual(active, 0)
                    tasks = [asyncio.create_task(submit(i)) for i in range(limit + 2)]
                    deadline = loop.time() + 30
                    while peak < limit and loop.time() < deadline:
                        await asyncio.sleep(.01)
                    self.assertEqual(peak, limit)
                    async with client.get(gateway_url + '/_scheduler/stats') as response:
                        stats = await response.json()
                    self.assertEqual(stats['total']['active'], limit)
                    self.assertGreaterEqual(stats['total']['queued'], 1)
                    held.set()
                    await asyncio.gather(*tasks)
                    self.assertEqual(received, limit + 2)
                    self.assertLessEqual(peak, limit)
                    self.assertEqual(active, 0)
                    observed.append({'kind': kind, 'round': number, 'configured_limit': limit,
                                     'observed_upstream_peak': peak, 'requests': received})
            self.assertEqual([item['observed_upstream_peak'] for item in observed], [64, 16, 4, 128, 32, 8])
            print('OBSERVED_PHASE_LIMITS=' + json.dumps(observed), flush=True)
        finally:
            held.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for runner in reversed(runners):
                await runner.cleanup()
            loop.slow_callback_duration = old_duration


if __name__ == '__main__':
    unittest.main()
