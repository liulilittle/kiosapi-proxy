import unittest
import threading
import json
import socket
import ssl
import http.server
import urllib.request
import urllib.error
import time
import os
from collections import Counter

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kiosapi.py")


class FakeUpstream(http.server.BaseHTTPRequestHandler):
    """Simulate kiosapi upstream: return 429 for keys in .limits, 200 otherwise."""

    limits = {}
    log = []

    def do_POST(self):
        auth = self.headers.get("Authorization", "")
        key = auth.replace("Bearer ", "")
        FakeUpstream.log.append({"path": self.path, "key": key})
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        stream = self._is_stream(body)
        if key in FakeUpstream.limits:
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            msg = json.dumps({"error": {"message": "Too many requests: max 15 per minute", "type": "rate_limit"}}).encode()
            self.wfile.write(msg)
            return
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: {\"choices\":[{\"delta\":{\"content\":\"ok\"}}]}\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        resp = json.dumps({"id": "test", "choices": [{"message": {"role": "assistant", "content": "hi"}}], "used_key": key}).encode()
        self.wfile.write(resp)

    @staticmethod
    def _is_stream(body):
        try:
            return bool(json.loads(body.decode("utf-8")).get("stream"))
        except Exception:
            return False

    def log_message(self, *args):
        pass


def start_upstream():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_port(port, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
            return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError("proxy did not start listening")


class TestJoinUpstreamURL(unittest.TestCase):

    def test_duplicate_v1_prefix(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("kiosapi", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(
            mod.join_upstream_url("https://kiosapi.com/v1", "/v1/chat/completions"),
            "https://kiosapi.com/v1/chat/completions")

    def test_no_duplicate_when_base_has_no_prefix(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("kiosapi", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(
            mod.join_upstream_url("https://kiosapi.com", "/v1/chat/completions"),
            "https://kiosapi.com/v1/chat/completions")

    def test_query_preserved(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("kiosapi", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(
            mod.join_upstream_url("https://kiosapi.com/v1", "/v1/models?x=1"),
            "https://kiosapi.com/v1/models?x=1")

    def test_base_query_kept_when_client_has_query(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("kiosapi", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(
            mod.join_upstream_url(
                "https://x.azure.com/openai/deployments/d?api-version=2024-02-01",
                "/openai/deployments/d/chat/completions?foo=bar"),
            "https://x.azure.com/openai/deployments/d/chat/completions"
            "?foo=bar&api-version=2024-02-01")

    def test_client_query_wins_over_base_query(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("kiosapi", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(
            mod.join_upstream_url(
                "https://x.invalid/v1?api-version=old",
                "/v1/models?api-version=new"),
            "https://x.invalid/v1/models?api-version=new")

    def test_base_query_keeps_repeated_params(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("kiosapi", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(
            mod.join_upstream_url(
                "https://x.invalid/v1?tag=a&tag=b",
                "/v1/models?x=1"),
            "https://x.invalid/v1/models?x=1&tag=a&tag=b")

    def test_trailing_slash_base(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("kiosapi", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(
            mod.join_upstream_url("https://kiosapi.com/v1/", "/v1/chat/completions"),
            "https://kiosapi.com/v1/chat/completions")

    def test_exact_v1_path(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("kiosapi", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(
            mod.join_upstream_url("https://kiosapi.com/v1", "/v1"),
            "https://kiosapi.com/v1")

    def test_non_v1_client_path_with_v1_base(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("kiosapi", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(
            mod.join_upstream_url("https://kiosapi.com/v1", "/chat/completions"),
            "https://kiosapi.com/v1/chat/completions")

    def test_root_base_url(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("kiosapi", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(
            mod.join_upstream_url("https://kiosapi.com/", "/v1/chat/completions"),
            "https://kiosapi.com/v1/chat/completions")


class TestCertificateRetry(unittest.TestCase):

    def _load_mod(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("kiosapi", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_cert_error_rotates_to_next_key(self):
        mod = self._load_mod()

        class FakeResp:
            status = 200
            headers = {"Content-Type": "application/json"}
            def release(self):
                pass
            async def read(self):
                return json.dumps({"ok": True, "used_key": self.auth}).encode()

        seen = []

        class FakeSession:
            async def request(self, method, url, data, headers, **kwargs):
                key = headers["Authorization"].replace("Bearer ", "")
                seen.append(key)
                if key == "KEY-A":
                    raise ssl.SSLCertVerificationError("unknown certificate verification error")
                r = FakeResp()
                r.auth = key
                return r

        cfg = type("Cfg", (), {"base_url": "https://up.example/v1", "model": "", "timeout": 10, "keys": ["KEY-A", "KEY-B"]})()
        engine = mod.ProxyEngine(cfg)
        engine.session = FakeSession()

        import asyncio
        kind, status, body, _ = asyncio.run(
            engine.forward("POST", "/v1/chat/completions", json.dumps({"model": "m"}).encode(), "application/json"))
        self.assertEqual(seen, ["KEY-A", "KEY-B"])
        self.assertEqual(kind, "plain")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["used_key"], "KEY-B")

    def test_cert_error_all_keys_fail_returns_last_error(self):
        mod = self._load_mod()

        class FakeSession:
            async def request(self, method, url, data, headers, **kwargs):
                raise ssl.SSLCertVerificationError("unknown certificate verification error")

        cfg = type("Cfg", (), {"base_url": "https://up.example/v1", "model": "", "timeout": 10, "keys": ["KEY-A", "KEY-B"]})()
        engine = mod.ProxyEngine(cfg)
        engine.session = FakeSession()

        import asyncio
        kind, status, body, _ = asyncio.run(
            engine.forward("POST", "/v1/chat/completions", json.dumps({"model": "m"}).encode(), "application/json"))
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"]["type"], "certificate_error")

    def test_non_cert_os_error_not_rotated(self):
        mod = self._load_mod()

        class FakeSession:
            async def request(self, method, url, data, headers, **kwargs):
                raise OSError("connection refused to SECRET-HOST")

        cfg = type("Cfg", (), {"base_url": "https://up.example/v1", "model": "", "timeout": 10, "keys": ["KEY-A", "KEY-B"]})()
        engine = mod.ProxyEngine(cfg)
        engine.session = FakeSession()

        import asyncio
        kind, status, body, _ = asyncio.run(
            engine.forward("POST", "/v1/chat/completions", json.dumps({"model": "m"}).encode(), "application/json"))
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"]["type"], "proxy_error")
        self.assertNotIn("connection refused", body.decode("utf-8"))
        self.assertNotIn("SECRET-HOST", body.decode("utf-8"))


class TestKeyRotation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.upstream = start_upstream()
        cls.upstream_url = f"http://127.0.0.1:{cls.upstream.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def setUp(self):
        FakeUpstream.limits = {}
        FakeUpstream.log = []

    def _start_proxy(self, keys, ini_port=None):
        import subprocess
        import sys
        import tempfile
        import os
        if ini_port is None:
            ini_port = free_port()
        ini = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False, encoding="utf-8")
        ini.write("[server]\nhost = 127.0.0.1\nport = %d\nbase_url = %s\n\n[keys]\n" % (ini_port, self.upstream_url))
        for i, k in enumerate(keys, 1):
            ini.write("key%d = %s\n" % (i, k))
        ini.close()
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kiosapi.py")
        proc = subprocess.Popen(
            [sys.executable, script, "--config", ini.name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        wait_port(ini_port)
        return proc, ini.name, ini_port

    def _stop_proxy(self, proc):
        proc.terminate()
        proc.wait(timeout=5)
        if proc.stdout:
            proc.stdout.close()

    def _chat(self, port, stream=False):
        payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        if stream:
            payload["stream"] = True
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=10)

    def test_normal_request_uses_first_key(self):
        proc, ini, port = self._start_proxy(["KEY-A", "KEY-B"])
        try:
            body = json.loads(self._chat(port).read())
            self.assertEqual(body["used_key"], "KEY-A")
        finally:
            self._stop_proxy(proc)

    def test_limited_key_rotates_to_next(self):
        FakeUpstream.limits = {"KEY-A"}
        proc, ini, port = self._start_proxy(["KEY-A", "KEY-B"])
        try:
            body = json.loads(self._chat(port).read())
            self.assertEqual(body["used_key"], "KEY-B")
        finally:
            self._stop_proxy(proc)

    def test_all_limited_returns_429(self):
        FakeUpstream.limits = {"KEY-A", "KEY-B"}
        proc, ini, port = self._start_proxy(["KEY-A", "KEY-B"])
        try:
            try:
                self._chat(port)
                self.fail("should have raised HTTPError")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 429)
                err = json.loads(e.read())
                e.close()
                self.assertIn("Too many requests", err["error"]["message"])
        finally:
            self._stop_proxy(proc)

    def test_non_429_error_passed_through(self):
        FakeUpstream.limits = {}
        proc, ini, port = self._start_proxy(["KEY-A", "KEY-B"])
        try:
            resp = self._chat(port)
            self.assertEqual(resp.status, 200)
        finally:
            self._stop_proxy(proc)

    def test_round_robin_rotates_between_keys(self):
        proc, ini, port = self._start_proxy(["KEY-A", "KEY-B", "KEY-C"])
        try:
            used = []
            for _ in range(3):
                used.append(json.loads(self._chat(port).read())["used_key"])
            self.assertEqual(used, ["KEY-A", "KEY-B", "KEY-C"])
        finally:
            self._stop_proxy(proc)

    def test_streaming_passthrough(self):
        proc, ini, port = self._start_proxy(["KEY-A"])
        try:
            resp = self._chat(port, stream=True)
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "text/event-stream")
            data = resp.read().decode()
            self.assertIn("data: [DONE]", data)
        finally:
            self._stop_proxy(proc)

    def test_429_advances_pointer_between_requests(self):
        """After A was limited and B succeeded, next request must start at C."""
        FakeUpstream.limits = {"KEY-A"}
        proc, ini, port = self._start_proxy(["KEY-A", "KEY-B", "KEY-C"])
        try:
            first = json.loads(self._chat(port).read())["used_key"]
            second = json.loads(self._chat(port).read())["used_key"]
            self.assertEqual(first, "KEY-B")
            self.assertEqual(second, "KEY-C")
        finally:
            self._stop_proxy(proc)

    def test_mixed_limit_sequence(self):
        """A and C limited: req1 -> B, req2 (A still limited) -> B, req3 -> ?"""
        FakeUpstream.limits = {"KEY-A", "KEY-C"}
        proc, ini, port = self._start_proxy(["KEY-A", "KEY-B", "KEY-C"])
        try:
            used = []
            for _ in range(3):
                used.append(json.loads(self._chat(port).read())["used_key"])
            # req1: A(429)->B(ok) ; req2: C(429)->A(429)->B(ok) ; req3: C(429)->A(429)->B(ok)
            self.assertEqual(used, ["KEY-B", "KEY-B", "KEY-B"])
        finally:
            self._stop_proxy(proc)

    def test_concurrent_requests_use_distinct_keys(self):
        """Concurrent requests must not all grab the same key; distribution must rotate."""
        proc, ini, port = self._start_proxy(["KEY-A", "KEY-B", "KEY-C", "KEY-D"])
        try:
            results = []
            def worker():
                results.append(json.loads(self._chat(port).read())["used_key"])
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            counter = Counter(results)
            # 8 requests over 4 keys -> each key used exactly 2 times
            self.assertEqual(counter, Counter({"KEY-A": 2, "KEY-B": 2, "KEY-C": 2, "KEY-D": 2}))
        finally:
            self._stop_proxy(proc)

    def test_concurrent_requests_all_succeed_despite_one_limited(self):
        FakeUpstream.limits = {"KEY-A"}
        proc, ini, port = self._start_proxy(["KEY-A", "KEY-B", "KEY-C", "KEY-D"])
        try:
            results = []
            def worker():
                results.append(json.loads(self._chat(port).read())["used_key"])
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(results), 8)
            self.assertNotIn("KEY-A", results)
        finally:
            self._stop_proxy(proc)


if __name__ == "__main__":
    unittest.main()
