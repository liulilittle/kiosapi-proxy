import unittest
import asyncio
import aiohttp
import json
import time
import subprocess
import socket
import tempfile
import os
import sys
import threading
from aiohttp import web
from collections import Counter


class TokenBucket:
    """Per-key independent fixed-window limiter: first `rate` requests in a window pass.

    Fixed window (not sliding) so the whole test run is inside one window and the cap is hard.
    """

    def __init__(self, rate=15, window=60.0):
        self.rate = rate
        self.window = window
        self.window_start = time.time()
        self.count = 0

    def allow(self):
        now = time.time()
        if now - self.window_start >= self.window:
            self.window_start = now
            self.count = 0
        if self.count < self.rate:
            self.count += 1
            return True
        return False


buckets = {}


async def rate_limited_upstream(request):
    key = request.headers.get("Authorization", "").replace("Bearer ", "")
    if key not in buckets:
        buckets[key] = TokenBucket(rate=15, window=60.0)
    if not buckets[key].allow():
        return web.Response(status=429, text=json.dumps({"error": {"message": "too many requests", "type": "rate_limit"}}))
    return web.Response(text=json.dumps({"ok": True, "used_key": key}))


def wait_port(port, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError("proxy did not listen")


def start_proxy(keys, port, base_url):
    ini = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False, encoding="utf-8")
    ini.write(f"[server]\nhost=127.0.0.1\nport={port}\nbase_url={base_url}\n\n[keys]\n")
    for i, k in enumerate(keys, 1):
        ini.write(f"key{i}={k}\n")
    ini.close()
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kiosapi.py")
    proc = subprocess.Popen([sys.executable, script, "--config", ini.name],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    wait_port(port)
    return proc


async def fire(url, keys, total, interval, own_auth=None):
    codes = Counter()
    session = aiohttp.ClientSession()
    body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "x"}]}).encode()
    t0 = time.time()
    for i in range(total):
        if own_auth is not None:
            key = own_auth
        else:
            key = keys[i % len(keys)]
        try:
            async with session.post(url, data=body, headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key}) as r:
                await r.read()
                codes[r.status] += 1
        except Exception:
            codes["ERR"] += 1
        el = time.time() - t0
        nxt = (i + 1) * interval
        if nxt > el:
            await asyncio.sleep(nxt - el)
    await session.close()
    return codes


class TestRotationScalesThroughput(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.loop = asyncio.new_event_loop()
        cls.test_app = web.Application()
        cls.test_app.router.add_route("*", "/{tail:.*}", rate_limited_upstream)
        cls.runner = web.AppRunner(cls.test_app)

        def serve():
            asyncio.set_event_loop(cls.loop)
            cls.loop.run_until_complete(cls.runner.setup())
            cls.site = web.TCPSite(cls.runner, "127.0.0.1", 18990)
            cls.loop.run_until_complete(cls.site.start())
            cls.base_url = "http://127.0.0.1:18990/v1"
            cls.loop.run_forever()

        cls.thread = threading.Thread(target=serve, daemon=True)
        cls.thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", 18990), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise TimeoutError("rate-limited upstream did not start")

    @classmethod
    def tearDownClass(cls):
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.thread.join(timeout=5)

    def setUp(self):
        buckets.clear()

    def test_single_key_capped_at_quota(self):
        codes = asyncio.run(fire(self.base_url + "/chat/completions", None, 40, 0.05, own_auth="K1"))
        self.assertLessEqual(codes.get(200, 0), 16)

    def test_rotation_scales_across_five_keys(self):
        keys = ["K1", "K2", "K3", "K4", "K5"]
        codes = asyncio.run(fire(self.base_url + "/chat/completions", keys, 40, 0.05))
        self.assertEqual(codes.get(200, 0), 40)
        self.assertEqual(codes.get(429, 0), 0)

    def test_proxy_rotation_beats_single_key(self):
        keys = ["K1", "K2", "K3", "K4", "K5"]
        port = 18870
        proc = start_proxy(keys, port, self.base_url)
        try:
            direct = asyncio.run(fire(self.base_url + "/chat/completions", None, 30, 0.05, own_auth="K1"))
            rotated = asyncio.run(fire(f"http://127.0.0.1:{port}/v1/chat/completions", keys, 30, 0.05))
            self.assertLessEqual(direct.get(200, 0), 16)
            self.assertEqual(rotated.get(200, 0), 30)
        finally:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()