import asyncio
import aiohttp
import contextlib
import http.server
import io
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kiosapi.py")
MANAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "manage.py")


def load_module():
    spec = importlib.util.spec_from_file_location("kiosapi_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeUpstream(http.server.BaseHTTPRequestHandler):
    behavior = {}
    log = []

    def _handle(self):
        key = self.headers.get("Authorization", "").replace("Bearer ", "")
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            parsed = json.loads(raw.decode("utf-8"))
            body_model = parsed.get("model") if isinstance(parsed, dict) else None
        except Exception:
            body_model = None
        FakeUpstream.log.append({
            "path": self.path,
            "key": key,
            "body_model": body_model,
            "headers": {k.lower(): v for k, v in self.headers.items()},
        })
        spec = FakeUpstream.behavior.get(key, {})
        status = spec.get("status", 200)
        payload = spec.get("body", json.dumps({"ok": True, "used_key": key}))
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for name, value in spec.get("headers", {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        self._handle()

    def do_GET(self):
        self._handle()

    def log_message(self, *args):
        pass


def start_upstream():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def wait_port(host, port, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection((host, port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError("proxy did not listen on %s:%s" % (host, port))


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def write_ini(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def start_proxy(ini_text, port, host="127.0.0.1"):
    ini = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False, encoding="utf-8")
    ini.write(ini_text)
    ini.close()
    proc = subprocess.Popen(
        [sys.executable, SCRIPT, "--config", ini.name],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    wait_port(host, port)
    return proc, ini.name


def start_proxy_path(path, port, host="127.0.0.1"):
    proc = subprocess.Popen(
        [sys.executable, SCRIPT, "--config", path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    wait_port(host, port)
    return proc


def stop_proxy(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    if proc.stdout:
        proc.stdout.close()


def http_call(port, path, key=None, headers=None, body=None, host="127.0.0.1", method="POST"):
    status, _headers, payload = http_call_full(
        port, path, key=key, headers=headers, body=body, host=host, method=method)
    return status, payload


def http_call_full(port, path, key=None, headers=None, body=None,
                   host="127.0.0.1", method="POST"):
    if body is None:
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    if not isinstance(body, (bytes, str)):
        body = json.dumps(body)
    if isinstance(body, str):
        body = body.encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    if key is not None:
        hdrs["Authorization"] = "Bearer " + key
    url_host = "[%s]" % host if ":" in host else host
    req = urllib.request.Request(
        "http://%s:%s%s" % (url_host, port, path), data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as ex:
        payload = ex.read()
        result = (ex.code, dict(ex.headers.items()), payload)
        ex.close()
        return result


def server_block(port, base_url, host="127.0.0.1", auth_keys="", reload_interval=1):
    lines = ["[server]", "host = " + host, "port = " + str(port),
             "base_url = " + base_url, "timeout = 10",
             "reload_interval = %d" % reload_interval]
    if auth_keys:
        lines.append("auth_keys = " + auth_keys)
    lines += ["", "[keys]", "key1 = DEFAULT-KEY", ""]
    return "\n".join(lines)


def upstream_block(name, base_url, keys, prefix=None):
    return ("[upstream:%s]\nbase_url = %s\nprefix = /%s\nkeys = %s\n\n"
            % (name, base_url, prefix or name, ", ".join(keys)))


class TestConfigUnit(unittest.TestCase):

    def setUp(self):
        self.mod = load_module()

    def test_parse_hosts_supports_ipv4_and_ipv6(self):
        self.assertEqual(self.mod.parse_hosts("0.0.0.0, ::"), ["0.0.0.0", "::"])

    def test_parse_hosts_defaults_to_loopback(self):
        self.assertEqual(self.mod.parse_hosts(""), ["127.0.0.1"])

    def test_is_loopback(self):
        self.assertTrue(self.mod.is_loopback("127.0.0.1"))
        self.assertTrue(self.mod.is_loopback("::1"))
        self.assertTrue(self.mod.is_loopback("localhost"))
        self.assertFalse(self.mod.is_loopback("0.0.0.0"))
        self.assertFalse(self.mod.is_loopback("::"))

    def test_load_config_parses_upstream_section(self):
        ini = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False, encoding="utf-8")
        ini.write(server_block(8787, "https://kiosapi.com/v1", auth_keys="pk-1, pk-2"))
        ini.write(upstream_block("alpha", "https://alpha.example/v1", ["A1", "A2"]))
        ini.close()
        cfg = self.mod.load_config(ini.name)
        self.assertEqual(cfg.hosts, ["127.0.0.1"])
        self.assertEqual(cfg.auth_keys, ["pk-1", "pk-2"])
        self.assertEqual(cfg.default.keys, ["DEFAULT-KEY"])
        self.assertEqual(len(cfg.upstreams), 1)
        self.assertEqual(cfg.upstreams[0].name, "alpha")
        self.assertEqual(cfg.upstreams[0].prefix, "/alpha")
        self.assertEqual(cfg.upstreams[0].keys, ["A1", "A2"])

    def test_keypool_skips_cooling_key(self):
        pool = self.mod.KeyPool(["A", "B"])
        pool.mark_limited("A", "60")
        self.assertEqual(pool.candidates(), ["B", "A"])

    def test_keypool_all_cooling_still_returns_keys(self):
        pool = self.mod.KeyPool(["A", "B"])
        pool.mark_limited("A", "60")
        pool.mark_limited("B", "60")
        self.assertEqual(pool.candidates(), ["A", "B"])

    def test_keypool_advance_past(self):
        pool = self.mod.KeyPool(["A", "B", "C"])
        pool.advance_past("B")
        self.assertEqual(pool.candidates(), ["C", "A", "B"])

    def test_keypool_replace_keys_keeps_existing_cooldown(self):
        pool = self.mod.KeyPool(["A", "B"])
        pool.mark_limited("A", "60")
        pool.replace_keys(["A", "B", "C"])
        self.assertEqual(pool.keys, ["B", "A", "C"])
        self.assertEqual(pool.candidates()[0], "B")

    def test_keypool_ordered_for_persist_puts_cooling_last(self):
        pool = self.mod.KeyPool(["A", "B", "C"])
        pool.mark_limited("A", "60")
        self.assertEqual(pool.ordered_for_persist(), ["B", "C", "A"])

    def test_keypool_apply_order_matches_fingerprints(self):
        pool = self.mod.KeyPool(["A", "B", "C"])
        pool.apply_order([
            self.mod.key_fingerprint("C"),
            self.mod.key_fingerprint("A"),
        ])
        self.assertEqual(pool.keys, ["C", "A", "B"])
        self.assertEqual(pool.idx, 0)

    def test_limit_error_200_body_keyword_is_not_limit(self):
        self.assertFalse(self.mod.is_limit_error(200, b'{"text":"rate limit"}'))

    def test_limit_error_429_is_limit(self):
        self.assertTrue(self.mod.is_limit_error(429, b""))

    def test_limit_error_402_is_limit(self):
        self.assertTrue(self.mod.is_limit_error(402, b""))

    def test_limit_error_400_quota_keyword_is_limit(self):
        self.assertTrue(self.mod.is_limit_error(400, b'{"code":"free_quota_rpm"}'))

    def test_limit_error_401_is_not_limit(self):
        self.assertFalse(self.mod.is_limit_error(401, b'{"error":"invalid key"}'))

    def test_auth_error_detects_400_bad_key(self):
        self.assertTrue(self.mod.is_auth_error(
            400, b'{"error":{"message":"Incorrect API key provided"}}'))
        self.assertTrue(self.mod.is_auth_error(
            400, b'[{"error":{"message":"Please pass a valid API key"}}]'))
        self.assertTrue(self.mod.is_auth_error(401, b""))
        self.assertFalse(self.mod.is_auth_error(
            400, b'{"error":{"message":"invalid parameter"}}'))

    def test_auth_error_ignores_403_moderation(self):
        self.assertFalse(self.mod.is_auth_error(
            403, b'{"error":{"code":"inappropriate_content"}}'))
        self.assertTrue(self.mod.is_auth_error(
            403, b'{"error":{"code":"credit_limit_exceeded"}}'))

    def test_cooldown_for_quota_uses_long_cooldown(self):
        self.assertEqual(self.mod.cooldown_for(402, b"", None), self.mod.AUTH_COOLDOWN)
        self.assertEqual(self.mod.cooldown_for(402, b"", "30"), "30")
        self.assertEqual(
            self.mod.cooldown_for(429, b'{"code":"insufficient_quota"}', None),
            self.mod.AUTH_COOLDOWN)
        self.assertEqual(
            self.mod.cooldown_for(429, b'{"code":"QuotaExceeded"}', None),
            self.mod.AUTH_COOLDOWN)
        self.assertEqual(self.mod.cooldown_for(429, b"", "30"), "30")

    def test_parse_retry_after_seconds_and_http_date(self):
        self.assertEqual(self.mod.parse_retry_after("30"), 30.0)
        self.assertIsNone(self.mod.parse_retry_after(""))
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime
        future = datetime.now(timezone.utc) + timedelta(seconds=30)
        parsed = self.mod.parse_retry_after(format_datetime(future, usegmt=True))
        self.assertTrue(25 <= parsed <= 35)

    def test_load_config_rejects_bad_port_and_timeout(self):
        ini = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False, encoding="utf-8")
        ini.write(server_block(70000, "https://kiosapi.com/v1"))
        ini.close()
        with self.assertRaises(self.mod.ConfigError):
            self.mod.load_config(ini.name)
        ini2 = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False, encoding="utf-8")
        ini2.write(server_block(8787, "https://kiosapi.com/v1").replace("timeout = 10", "timeout = 0"))
        ini2.close()
        with self.assertRaises(self.mod.ConfigError):
            self.mod.load_config(ini2.name)

    def test_extract_body_error(self):
        self.assertEqual(
            self.mod.extract_body_error(
                b'{"base_resp":{"status_code":1002}}', "base_resp.status_code"),
            1002)
        self.assertIsNone(
            self.mod.extract_body_error(b'{"base_resp":{}}', "base_resp.status_code"))

    def test_keypool_dedupes_keys(self):
        pool = self.mod.KeyPool(["A", "A", "B"])
        self.assertEqual(pool.keys, ["A", "B"])

    def test_is_limit_error_ignores_5xx_body(self):
        self.assertFalse(self.mod.is_limit_error(503, b'{"error":"rate limit"}'))
        self.assertFalse(self.mod.is_limit_error(500, b'{"error":"quota exceeded"}'))
        self.assertTrue(self.mod.is_limit_error(429, b'{"error":"rate limit"}'))

    def test_parse_hosts_bracketed_ipv6(self):
        self.assertEqual(self.mod.parse_hosts("0.0.0.0, [::]"), ["0.0.0.0", "::"])
        self.assertEqual(self.mod.parse_hosts("[::1]"), ["::1"])
        self.assertEqual(self.mod.parse_hosts("[::]:8787"), ["::"])
        self.assertEqual(self.mod.parse_hosts("127.0.0.1:8787"), ["127.0.0.1"])

    def test_is_loopback_bracketed(self):
        self.assertTrue(self.mod.is_loopback("[::1]"))
        self.assertFalse(self.mod.is_loopback("[::]"))

    def _config_text(self, extra=""):
        return server_block(8787, "https://kiosapi.com/v1") + extra

    def _load_text(self, text):
        ini = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False, encoding="utf-8")
        ini.write(text)
        ini.close()
        try:
            return self.mod.load_config(ini.name)
        finally:
            os.unlink(ini.name)

    def test_load_config_percent_in_key(self):
        cfg = self._load_text(self._config_text().replace(
            "key1 = DEFAULT-KEY", "key1 = sk-abc%def"))
        self.assertEqual(cfg.default.keys, ["sk-abc%def"])

    def test_load_config_rejects_duplicate_prefix(self):
        text = self._config_text()
        text += upstream_block("a", "https://a.example/v1", ["A1"], prefix="same")
        text += upstream_block("b", "https://b.example/v1", ["B1"], prefix="same")
        with self.assertRaises(self.mod.ConfigError):
            self._load_text(text)

    def test_load_config_rejects_reserved_default_name(self):
        text = self._config_text()
        text += "[upstream:default]\nbase_url = https://a.example/v1\nkeys = A1\n"
        with self.assertRaises(self.mod.ConfigError):
            self._load_text(text)

    def test_load_config_rejects_invalid_base_url(self):
        text = self._config_text().replace(
            "base_url = https://kiosapi.com/v1", "base_url = ftp://kiosapi.com/v1")
        with self.assertRaises(self.mod.ConfigError):
            self._load_text(text)

    def test_load_config_rejects_invalid_prefix(self):
        text = self._config_text()
        text += "[upstream:a]\nbase_url = https://a.example/v1\nprefix = /a?x=1\nkeys = A1\n"
        with self.assertRaises(self.mod.ConfigError):
            self._load_text(text)

    def test_load_config_rejects_invalid_session_header(self):
        text = self._config_text()
        text += ("[upstream:a]\nbase_url = https://a.example/v1\n"
                 "session_header = bad header\nkeys = A1\n")
        with self.assertRaises(self.mod.ConfigError):
            self._load_text(text)

    def test_load_config_rejects_negative_upstream_timeout(self):
        text = self._config_text()
        text += "[upstream:a]\nbase_url = https://a.example/v1\ntimeout = -1\nkeys = A1\n"
        with self.assertRaises(self.mod.ConfigError):
            self._load_text(text)

    def test_load_config_rejects_admin_key_in_auth_keys(self):
        text = self._config_text().replace(
            "timeout = 10", "timeout = 10\nauth_keys = pk-1\nadmin_key = pk-1")
        with self.assertRaises(self.mod.ConfigError):
            self._load_text(text)

    def test_load_config_resolves_relative_state_file(self):
        text = self._config_text().replace(
            "timeout = 10", "timeout = 10\nstate_file = state.json")
        ini = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False, encoding="utf-8")
        ini.write(text)
        ini.close()
        try:
            cfg = self.mod.load_config(ini.name)
            self.assertEqual(
                cfg.state_file,
                os.path.join(os.path.dirname(os.path.abspath(ini.name)), "state.json"))
        finally:
            os.unlink(ini.name)

    def test_load_config_strips_inline_comments(self):
        text = (
            "[server]\n"
            "host = 127.0.0.1          ; loopback only\n"
            "port = 8787\n"
            "auth_keys = pk-a, pk-b ; clients\n"
            "reload_interval = 2 ; poll\n"
            "admin_allow_remote = false ; local\n"
            "max_body_size = 1048576 ; 1 MiB\n"
            "base_url = https://x.invalid/v1   ; default\n"
            "[keys]\n"
            "key1 = sk-a ; first\n"
            "key2 = sk-b#not-a-comment\n"
            "[upstream:alpha]\n"
            "base_url = https://a.invalid/v1 ; alpha\n"
            "keys = A1, A2 ; two\n"
        )
        cfg = self._config_from_text(text)
        self.assertEqual(cfg.hosts, ["127.0.0.1"])
        self.assertEqual(cfg.auth_keys, ["pk-a", "pk-b"])
        self.assertEqual(cfg.reload_interval, 2)
        self.assertFalse(cfg.admin_allow_remote)
        self.assertEqual(cfg.max_body_size, 1048576)
        self.assertEqual(cfg.default.base_url, "https://x.invalid/v1")
        self.assertEqual(cfg.default.keys, ["sk-a", "sk-b#not-a-comment"])
        self.assertEqual(cfg.upstreams[0].base_url, "https://a.invalid/v1")
        self.assertEqual(cfg.upstreams[0].keys, ["A1", "A2"])

    def test_load_config_dedupes_keys(self):
        text = (
            "[server]\nhost = 127.0.0.1\nport = 8787\n"
            "base_url = https://x.invalid/v1\n"
            "[keys]\nkeys = same, same, other\n"
        )
        cfg = self._config_from_text(text)
        self.assertEqual(cfg.default.keys, ["same", "other"])

    def test_load_config_default_keys_line(self):
        text = self._config_text().replace("key1 = DEFAULT-KEY", "keys = a, b")
        cfg = self._load_text(text)
        self.assertEqual(cfg.default.keys, ["a", "b"])

    def test_load_config_rejects_state_file_as_config(self):
        ini = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False, encoding="utf-8")
        ini.close()
        text = self._config_text().replace(
            "timeout = 10", "timeout = 10\nstate_file = " + ini.name)
        with open(ini.name, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            with self.assertRaises(self.mod.ConfigError):
                self.mod.load_config(ini.name)
        finally:
            os.unlink(ini.name)

    def test_load_config_rejects_state_file_directory(self):
        tmpdir = tempfile.mkdtemp()
        text = self._config_text().replace(
            "timeout = 10", "timeout = 10\nstate_file = " + tmpdir)
        ini = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False, encoding="utf-8")
        ini.write(text)
        ini.close()
        try:
            with self.assertRaises(self.mod.ConfigError):
                self.mod.load_config(ini.name)
        finally:
            os.unlink(ini.name)
            os.rmdir(tmpdir)

    def test_load_config_dedupes_hosts(self):
        text = self._config_text().replace("host = 127.0.0.1", "host = 127.0.0.1, 127.0.0.1")
        cfg = self._load_text(text)
        self.assertEqual(cfg.hosts, ["127.0.0.1"])

    def test_load_config_accepts_utf8_bom(self):
        ini = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False, encoding="utf-8")
        ini.write("\ufeff" + self._config_text())
        ini.close()
        try:
            cfg = self.mod.load_config(ini.name)
            self.assertEqual(cfg.port, 8787)
        finally:
            os.unlink(ini.name)

    def test_resolve_state_path_expanduser(self):
        expected = os.path.expanduser("~/state.json")
        self.assertEqual(
            self.mod.resolve_state_path("/x/proxy.ini", "~/state.json"),
            expected)

    def test_load_config_directory_rejected(self):
        tmpdir = tempfile.mkdtemp()
        try:
            with self.assertRaises(self.mod.ConfigError) as ctx:
                self.mod.load_config(tmpdir)
            self.assertIn("directory", str(ctx.exception))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_load_config_invalid_utf8_rejected(self):
        fd, path = tempfile.mkstemp(suffix=".ini")
        os.close(fd)
        try:
            with open(path, "wb") as fh:
                fh.write(b"\xff\xfe[server]\n")
            with self.assertRaises(self.mod.ConfigError) as ctx:
                self.mod.load_config(path)
            self.assertIn("decode", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_watch_config_stops_when_interval_zero(self):
        cfg = type("Cfg", (), {
            "base_url": "https://x.invalid/v1", "model": "", "timeout": 10,
            "keys": ["K"], "auth_keys": [], "state_file": "",
            "reload_interval": 0, "path": "nonexistent.ini"})()
        engine = self.mod.ProxyEngine(cfg)
        asyncio.run(asyncio.wait_for(engine.watch_config(), 0.5))

    def _config_from_text(self, text):
        fd, path = tempfile.mkstemp(suffix=".ini")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return self.mod.load_config(path)

    def test_upstream_name_keys_reserved(self):
        with self.assertRaises(self.mod.ConfigError) as ctx:
            self._config_from_text(
                "[server]\nhost = 127.0.0.1\nport = 8787\n"
                "[keys]\nkey1 = k1\n"
                "[upstream:keys]\nbase_url = https://x.invalid/v1\n"
                "keys = k2\n")
        self.assertIn("reserved", str(ctx.exception))

    def test_reload_warns_max_body_size_requires_restart(self):
        old = self._config_from_text(
            "[server]\nhost = 127.0.0.1\nport = 8787\n"
            "[keys]\nkey1 = k1\n")
        new = self._config_from_text(
            "[server]\nhost = 127.0.0.1\nport = 8787\n"
            "max_body_size = 1024\n[keys]\nkey1 = k1\n")
        engine = self.mod.ProxyEngine(old)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            engine.apply_config(new)
        self.assertIn("max_body_size changes require restart", out.getvalue())

    def test_reload_rejects_placeholder_auth_key_on_non_loopback(self):
        old = self._config_from_text(
            "[server]\nhost = 0.0.0.0\nport = 8787\n"
            "auth_keys = pk-1\nadmin_key = admin-1\n"
            "[keys]\nkey1 = up-1\n")
        new = self._config_from_text(
            "[server]\nhost = 0.0.0.0\nport = 8787\n"
            "auth_keys = REPLACE_ME\nadmin_key = admin-1\n"
            "[keys]\nkey1 = up-1\n")
        engine = self.mod.ProxyEngine(old)
        with self.assertRaises(self.mod.ConfigError):
            engine.apply_config(new)
        self.assertIs(engine.config, old)

    def test_reload_rejects_placeholder_admin_key_with_remote_admin(self):
        old = self._config_from_text(
            "[server]\nhost = 0.0.0.0\nport = 8787\n"
            "auth_keys = pk-1\nadmin_key = admin-1\n"
            "[keys]\nkey1 = up-1\n")
        new = self._config_from_text(
            "[server]\nhost = 0.0.0.0\nport = 8787\n"
            "auth_keys = pk-1\nadmin_key = REPLACE_ME\n"
            "admin_allow_remote = true\n"
            "[keys]\nkey1 = up-1\n")
        engine = self.mod.ProxyEngine(old)
        with self.assertRaises(self.mod.ConfigError):
            engine.apply_config(new)
        self.assertIs(engine.config, old)

    def test_load_config_rejects_default_section_inheritance(self):
        text = (
            "[DEFAULT]\n"
            "leak = SECRET\n"
            "[server]\nhost = 127.0.0.1\nport = 8787\n"
            "base_url = https://x.invalid/v1\n"
            "[keys]\nkey1 = K1\n"
        )
        with self.assertRaises(self.mod.ConfigError) as ctx:
            self._config_from_text(text)
        self.assertIn("DEFAULT", str(ctx.exception))

    def test_load_config_splits_multiline_key_values(self):
        text = (
            "[server]\nhost = 127.0.0.1\nport = 8787\n"
            "base_url = https://x.invalid/v1\n"
            "[keys]\nkey1 = A,\n  B\n"
        )
        cfg = self._config_from_text(text)
        self.assertEqual(cfg.default.keys, ["A", "B"])

    def test_load_config_accepts_key_prefixed_options(self):
        text = (
            "[server]\nhost = 127.0.0.1\nport = 8787\n"
            "base_url = https://x.invalid/v1\n"
            "[keys]\nkey1 = A\nkey_backup = B\n"
        )
        cfg = self._config_from_text(text)
        self.assertEqual(cfg.default.keys, ["A", "B"])

    def test_build_upstream_headers_strips_proxy_authorization(self):
        class Upstream:
            auth_style = "bearer"
            session_header = ""
            user_agent = ""
        headers = self.mod.build_upstream_headers(
            {"Proxy-Authorization": "Basic secret", "X-Test": "1"},
            Upstream(), "K", "application/json")
        self.assertNotIn("Proxy-Authorization", headers)
        self.assertEqual(headers["X-Test"], "1")

    def test_build_upstream_headers_strips_spoofed_client_ip(self):
        class Upstream:
            auth_style = "bearer"
            session_header = ""
            user_agent = ""
        headers = self.mod.build_upstream_headers(
            {
                "X-Real-IP": "1.2.3.4",
                "True-Client-IP": "1.2.3.4",
                "CF-Connecting-IP": "1.2.3.4",
                "X-Client-IP": "1.2.3.4",
                "X-Cluster-Client-IP": "1.2.3.4",
                "X-Test": "1",
            },
            Upstream(), "K", "application/json")
        for name in ("X-Real-IP", "True-Client-IP", "CF-Connecting-IP",
                     "X-Client-IP", "X-Cluster-Client-IP"):
            self.assertNotIn(name, headers)
        self.assertEqual(headers["X-Test"], "1")

    def test_mask_key_hides_short_keys(self):
        self.assertEqual(self.mod.mask_key(""), "...")
        self.assertEqual(self.mod.mask_key("abcd"), "...")
        self.assertEqual(self.mod.mask_key("abcdefgh"), "...")
        self.assertEqual(self.mod.mask_key("abcdefghi"), "...fghi")

    def test_is_loopback_ipv4_mapped(self):
        self.assertTrue(self.mod.is_loopback("::ffff:127.0.0.1"))
        self.assertFalse(self.mod.is_loopback("example.com"))

    def test_client_key_accepts_bearer_and_x_api_key(self):
        from multidict import CIMultiDict
        bearer = CIMultiDict()
        bearer["Authorization"] = "Bearer abc"
        self.assertEqual(self.mod.client_key(bearer), "abc")
        api_key = CIMultiDict()
        api_key["X-API-Key"] = "xyz"
        self.assertEqual(self.mod.client_key(api_key), "xyz")

    def test_route_prefers_longest_prefix_and_preserves_query(self):
        alpha = self.mod.Upstream("alpha", "https://a.invalid/v1", "/a", ["A"])
        beta = self.mod.Upstream("beta", "https://b.invalid/v1", "/a/b", ["B"])
        cfg = type("Cfg", (), {
            "base_url": "https://d.invalid/v1", "model": "", "timeout": 10,
            "keys": ["D"], "auth_keys": [], "state_file": "", "reload_interval": 0,
            "path": "nonexistent.ini", "log_requests": False,
            "upstreams": [alpha, beta]})()
        engine = self.mod.ProxyEngine(cfg)
        upstream, path = engine.route("/a/b/v1/chat/completions?x=1")
        self.assertIs(upstream, beta)
        self.assertEqual(path, "/v1/chat/completions?x=1")
        upstream, path = engine.route("/a/v1/models")
        self.assertIs(upstream, alpha)
        self.assertEqual(path, "/v1/models")
        upstream, path = engine.route("/kiosapi/v1/models?y=2")
        self.assertIs(upstream, engine.default)
        self.assertEqual(path, "/v1/models?y=2")
        upstream, path = engine.route("/elsewhere")
        self.assertIs(upstream, engine.default)
        self.assertEqual(path, "/elsewhere")

    def test_apply_model_only_overrides_dict(self):
        upstream = self.mod.Upstream(
            "u", "https://a.invalid/v1", "/u", ["K"], model="forced")
        cfg = type("Cfg", (), {
            "base_url": "https://d.invalid/v1", "model": "", "timeout": 10,
            "keys": ["D"], "auth_keys": [], "state_file": "", "reload_interval": 0,
            "path": "nonexistent.ini", "log_requests": False,
            "upstreams": []})()
        engine = self.mod.ProxyEngine(cfg)
        out = engine._apply_model(b'{"model":"orig","x":1}', upstream)
        self.assertEqual(json.loads(out)["model"], "forced")
        self.assertEqual(engine._apply_model(b"[1]", upstream), b"[1]")
        self.assertEqual(engine._apply_model(b"not json", upstream), b"not json")
        upstream.model = ""
        self.assertEqual(engine._apply_model(b'{"model":"orig"}', upstream),
                         b'{"model":"orig"}')

    def test_copy_response_headers_filters_unlisted(self):
        target = {}
        self.mod.copy_response_headers(target, {
            "content-type": "text/event-stream",
            "set-cookie": "sid=1",
            "x-request-id": "r1"})
        self.assertEqual(target, {"content-type": "text/event-stream",
                                  "x-request-id": "r1"})
        self.mod.copy_response_headers(target, None)
        self.assertEqual(target, {"content-type": "text/event-stream",
                                  "x-request-id": "r1"})

    def test_keypool_advance_past_unknown_key_is_noop(self):
        pool = self.mod.KeyPool(["A", "B"])
        pool.advance_past("missing")
        self.assertEqual(pool.candidates(), ["A", "B"])

    def test_keypool_mark_limited_invalid_retry_after_uses_default(self):
        pool = self.mod.KeyPool(["A"], default_cooldown=30)
        self.assertTrue(pool.mark_limited("A", "bogus"))
        self.assertEqual(pool.candidates(), ["A"])
        self.assertFalse(pool.mark_limited("A", None))

    def test_parse_retry_after_rejects_invalid(self):
        self.assertIsNone(self.mod.parse_retry_after(None))
        self.assertIsNone(self.mod.parse_retry_after(""))
        self.assertIsNone(self.mod.parse_retry_after("not-a-date"))

    def test_load_config_rejects_invalid_integer_and_boolean(self):
        for line in ("timeout = abc", "max_body_size = 0",
                     "log_requests = maybe", "max_response_size = -1"):
            text = ("[server]\nhost = 127.0.0.1\nport = 8787\n"
                    "base_url = https://x.invalid/v1\n" + line +
                    "\n[keys]\nkey1 = K\n")
            with self.assertRaises(self.mod.ConfigError):
                self._config_from_text(text)

    def test_load_config_accepts_max_response_size(self):
        text = ("[server]\nhost = 127.0.0.1\nport = 8787\n"
                "base_url = https://x.invalid/v1\nmax_response_size = 1024\n"
                "[keys]\nkey1 = K\n")
        cfg = self._config_from_text(text)
        self.assertEqual(cfg.max_response_size, 1024)
        disabled = ("[server]\nhost = 127.0.0.1\nport = 8787\n"
                    "base_url = https://x.invalid/v1\nmax_response_size = 0\n"
                    "[keys]\nkey1 = K\n")
        self.assertEqual(self._config_from_text(disabled).max_response_size, 0)

    def test_save_state_restricts_posix_permissions(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, True)
        state_path = os.path.join(tmpdir, "state.json")
        cfg = type("Cfg", (), {
            "base_url": "https://x.invalid/v1", "model": "", "timeout": 10,
            "keys": ["A"], "auth_keys": [], "state_file": state_path,
            "reload_interval": 0, "path": "nonexistent.ini",
            "log_requests": False, "upstreams": []})()
        calls = []
        original_flag = self.mod.POSIX_PERMISSIONS
        original_chmod = self.mod.os.chmod
        self.mod.POSIX_PERMISSIONS = True
        self.mod.os.chmod = lambda path, mode: calls.append((path, mode))
        try:
            engine = self.mod.ProxyEngine(cfg)
            engine.save_state()
        finally:
            self.mod.POSIX_PERMISSIONS = original_flag
            self.mod.os.chmod = original_chmod
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], state_path + ".tmp")
        self.assertEqual(calls[0][1], 0o600)

    def test_save_state_and_load_state_roundtrip(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, True)
        state_path = os.path.join(tmpdir, "state.json")
        cfg = type("Cfg", (), {
            "base_url": "https://x.invalid/v1", "model": "", "timeout": 10,
            "keys": ["A", "B"], "auth_keys": [], "state_file": state_path,
            "reload_interval": 0, "path": "nonexistent.ini",
            "log_requests": False, "upstreams": []})()
        engine = self.mod.ProxyEngine(cfg)
        engine.default.pool.candidates()
        engine.save_state()
        reloaded = self.mod.ProxyEngine(cfg)
        self.assertEqual(reloaded.default.pool.ordered_for_persist(), ["B", "A"])

    def test_reload_updates_max_response_size(self):
        old = self._config_from_text(
            "[server]\nhost = 127.0.0.1\nport = 8787\n"
            "base_url = https://x.invalid/v1\n"
            "[keys]\nkey1 = K\n")
        new = self._config_from_text(
            "[server]\nhost = 127.0.0.1\nport = 8787\n"
            "base_url = https://x.invalid/v1\nmax_response_size = 2048\n"
            "[keys]\nkey1 = K\n")
        engine = self.mod.ProxyEngine(old)
        self.assertEqual(engine.max_response_size, self.mod.DEFAULT_MAX_BODY)
        engine.apply_config(new)
        self.assertEqual(engine.max_response_size, 2048)

    def test_reload_keeps_bound_hosts_for_security_gate(self):
        old = self._config_from_text(
            "[server]\nhost = 0.0.0.0\nport = 8787\n"
            "auth_keys = pk-1\nadmin_key = admin-1\n"
            "[keys]\nkey1 = up-1\n")
        accepted = self._config_from_text(
            "[server]\nhost = 127.0.0.1\nport = 8787\n"
            "auth_keys = pk-2\nadmin_key = admin-1\n"
            "[keys]\nkey1 = up-1\n")
        engine = self.mod.ProxyEngine(old)
        engine.apply_config(accepted)
        self.assertEqual(engine.config.hosts, ["0.0.0.0"])
        dangerous = self._config_from_text(
            "[server]\nhost = 127.0.0.1\nport = 8787\n"
            "admin_key = admin-1\n[keys]\nkey1 = up-1\n")
        with self.assertRaises(self.mod.ConfigError):
            engine.apply_config(dangerous)
        self.assertEqual(engine.config.hosts, ["0.0.0.0"])
        self.assertTrue(engine.auth_keys)

    def _fake_engine(self, resp):
        class Session:
            async def request(self, *args, **kwargs):
                return resp
        cfg = type("Cfg", (), {
            "base_url": "https://x.invalid/v1", "model": "", "timeout": 10,
            "keys": ["K"], "auth_keys": [], "state_file": "",
            "reload_interval": 2, "path": "nonexistent.ini",
            "log_requests": False, "upstreams": []})()
        engine = self.mod.ProxyEngine(cfg)
        engine.session = Session()
        return engine

    def test_forward_closes_response_on_unexpected_error(self):
        class Resp:
            status = 200
            headers = {}

            def __init__(self):
                self.closed = False

            async def read(self):
                raise RuntimeError("boom")

            def close(self):
                self.closed = True

        resp = Resp()
        engine = self._fake_engine(resp)
        with self.assertRaises(RuntimeError):
            asyncio.run(engine.forward(
                "POST", "/v1/chat/completions", b"{}", "application/json"))
        self.assertTrue(resp.closed)

    def test_forward_closes_response_on_cancel(self):
        class Resp:
            status = 200
            headers = {}

            def __init__(self):
                self.closed = False

            async def read(self):
                await asyncio.sleep(10)

            def close(self):
                self.closed = True

        resp = Resp()
        engine = self._fake_engine(resp)

        async def scenario():
            task = asyncio.create_task(engine.forward(
                "POST", "/v1/chat/completions", b"{}", "application/json"))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        self.assertTrue(resp.closed)

    def test_forward_maps_network_errors_to_generic_502(self):
        class Session:
            def __init__(self, exc):
                self.exc = exc
                self.calls = 0

            async def request(self, *args, **kwargs):
                self.calls += 1
                raise self.exc

        cases = [asyncio.TimeoutError("secret timeout"),
                 aiohttp.ClientError("secret client error"),
                 ConnectionError("secret connection")]
        for exc in cases:
            cfg = type("Cfg", (), {
                "base_url": "https://x.invalid/v1", "model": "", "timeout": 10,
                "keys": ["K1", "K2"], "auth_keys": [], "state_file": "",
                "reload_interval": 0, "path": "nonexistent.ini",
                "log_requests": False, "upstreams": []})()
            engine = self.mod.ProxyEngine(cfg)
            session = Session(exc)
            engine.session = session
            kind, status, body, _ = asyncio.run(engine.forward(
                "POST", "/v1/chat/completions", b"{}", "application/json"))
            self.assertEqual(status, 502)
            self.assertEqual(json.loads(body)["error"]["type"], "proxy_error")
            self.assertNotIn("secret", body.decode("utf-8"))
            self.assertEqual(session.calls, 1)

    def test_forward_closes_response_on_payload_error(self):
        class Resp:
            status = 200
            headers = {}

            def __init__(self):
                self.closed = False

            async def read(self):
                raise aiohttp.ClientPayloadError("secret payload")

            def close(self):
                self.closed = True

        resp = Resp()
        engine = self._fake_engine(resp)
        kind, status, body, _ = asyncio.run(engine.forward(
            "POST", "/v1/chat/completions", b"{}", "application/json"))
        self.assertEqual(status, 502)
        self.assertNotIn("secret", body.decode("utf-8"))
        self.assertTrue(resp.closed)

    def test_read_body_limited_rejects_content_length_early(self):
        class Resp:
            status = 200
            headers = {"Content-Length": "101"}

            async def read(self):
                raise AssertionError("must not read an oversized body")

            def close(self):
                self.closed = True

        resp = Resp()
        with self.assertRaises(self.mod.ResponseTooLarge):
            asyncio.run(self.mod.read_body_limited(resp, 100))

    def test_read_body_limited_stops_at_limit(self):
        class Content:
            async def iter_chunked(self, size):
                yield b"a" * 80
                yield b"b" * 80

        class Resp:
            status = 200
            headers = {}

            def __init__(self):
                self.content = Content()

            def release(self):
                self.released = True

            def close(self):
                self.closed = True

        resp = Resp()
        with self.assertRaises(self.mod.ResponseTooLarge):
            asyncio.run(self.mod.read_body_limited(resp, 100))

    def test_read_body_limited_accepts_exact_limit(self):
        class Content:
            def __init__(self, total):
                self.total = total

            async def iter_chunked(self, size):
                yield b"a" * self.total

        class Resp:
            status = 200
            headers = {}

            def __init__(self, total):
                self.content = Content(total)

            def release(self):
                pass

        self.assertEqual(
            asyncio.run(self.mod.read_body_limited(Resp(100), 100)),
            b"a" * 100)
        with self.assertRaises(self.mod.ResponseTooLarge):
            asyncio.run(self.mod.read_body_limited(Resp(101), 100))

    def test_read_body_limited_falls_back_to_read(self):
        class Resp:
            status = 200
            headers = {}

            async def read(self):
                return b"12345"

        self.assertEqual(
            asyncio.run(self.mod.read_body_limited(Resp(), 100)), b"12345")

    def test_forward_rejects_oversized_response_body(self):
        class Content:
            async def iter_chunked(self, size):
                for _ in range(4):
                    yield b"x" * 64

        class Resp:
            status = 200
            headers = {}

            def __init__(self):
                self.content = Content()
                self.closed = False

            def close(self):
                self.closed = True

        resp = Resp()
        cfg = type("Cfg", (), {
            "base_url": "https://x.invalid/v1", "model": "", "timeout": 10,
            "keys": ["K"], "auth_keys": [], "state_file": "",
            "reload_interval": 2, "path": "nonexistent.ini",
            "log_requests": False, "upstreams": []})()
        cfg.max_response_size = 100

        class Session:
            async def request(self, *args, **kwargs):
                return resp

        engine = self.mod.ProxyEngine(cfg)
        engine.session = Session()
        with contextlib.redirect_stdout(io.StringIO()):
            kind, status, body, _ = asyncio.run(engine.forward(
                "POST", "/v1/chat/completions", b"{}", "application/json"))
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"]["type"], "proxy_error")
        self.assertTrue(resp.closed)

    def test_apply_config_during_inflight_requests_is_safe(self):
        old = self._config_from_text(
            "[server]\nhost = 127.0.0.1\nport = 8787\n"
            "base_url = https://old.invalid/v1\n"
            "auth_keys = pk-1\n"
            "[keys]\nkey1 = A\nkey2 = B\n")
        new = self._config_from_text(
            "[server]\nhost = 127.0.0.1\nport = 8787\n"
            "base_url = https://new.invalid/v1\n"
            "auth_keys = pk-1\n"
            "[keys]\nkey1 = C\n")
        started = asyncio.Event()
        release = asyncio.Event()
        counter = {"n": 0}

        class Resp:
            def __init__(self, status, body):
                self.status = status
                self.headers = {}
                self.body = body

            async def read(self):
                return self.body

            def release(self):
                pass

            def close(self):
                pass

        class Session:
            async def request(self, method, url, data, headers, **kwargs):
                counter["n"] += 1
                if counter["n"] == 2:
                    started.set()
                await release.wait()
                key = headers.get("Authorization", "").replace("Bearer ", "")
                if key == "A":
                    return Resp(429, b'{"error":{"message":"limited"}}')
                return Resp(200, b'{"ok":true}')

        async def scenario():
            engine = self.mod.ProxyEngine(old)
            engine.session = Session()
            tasks = [asyncio.create_task(engine.forward(
                "POST", "/v1/chat/completions", b"{}", "application/json"))
                for _ in range(2)]
            await asyncio.wait_for(started.wait(), 5)
            engine.apply_config(new)
            release.set()
            results = await asyncio.gather(*tasks)
            return engine, results

        engine, results = asyncio.run(scenario())
        self.assertEqual([result[1] for result in results], [200, 200])
        self.assertEqual(engine.default.pool.keys, ["C"])
        self.assertEqual(engine.config.default.keys, ["C"])

    def test_read_stream_prefix_has_total_deadline(self):
        class Content:
            def __init__(self):
                self.calls = 0

            async def read(self, n):
                self.calls += 1
                await asyncio.sleep(0.05)
                return b"{"

        class Resp:
            def __init__(self):
                self.content = Content()

        resp = Resp()
        start = time.monotonic()
        asyncio.run(self.mod.read_stream_prefix(
            resp, limit=100, timeout=0.12))
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.35)
        self.assertLess(resp.content.calls, 100)

    def test_stream_uses_read_timeout(self):
        class Resp:
            status = 200
            headers = {}
            content = object()

            def release(self):
                pass

            def close(self):
                pass

        class Session:
            def __init__(self):
                self.kwargs = None

            async def request(self, *args, **kwargs):
                self.kwargs = kwargs
                return Resp()

        cfg = type("Cfg", (), {
            "base_url": "https://x.invalid/v1", "model": "", "timeout": 10,
            "keys": ["K"], "auth_keys": [], "state_file": "",
            "reload_interval": 2, "path": "nonexistent.ini",
            "log_requests": False, "upstreams": []})()
        engine = self.mod.ProxyEngine(cfg)
        session = Session()
        engine.session = session
        asyncio.run(engine.forward(
            "POST", "/v1/chat/completions", b'{"stream": true}',
            "application/json"))
        timeout = session.kwargs["timeout"]
        self.assertIsNone(timeout.total)
        self.assertEqual(timeout.sock_connect, 10)
        self.assertEqual(timeout.sock_read, 10)

    def test_forward_disables_redirects(self):
        class Resp:
            status = 200
            headers = {}

            async def read(self):
                return b'{"ok":true}'

            def release(self):
                pass

        class Session:
            def __init__(self):
                self.kwargs = None

            async def request(self, *args, **kwargs):
                self.kwargs = kwargs
                return Resp()

        cfg = type("Cfg", (), {
            "base_url": "https://x.invalid/v1", "model": "", "timeout": 10,
            "keys": ["K"], "auth_keys": [], "state_file": "",
            "reload_interval": 2, "path": "nonexistent.ini",
            "log_requests": False, "upstreams": []})()
        engine = self.mod.ProxyEngine(cfg)
        session = Session()
        engine.session = session
        asyncio.run(engine.forward(
            "POST", "/v1/chat/completions", b"{}", "application/json"))
        self.assertIs(session.kwargs.get("allow_redirects"), False)

    def test_copy_response_headers_forwards_location(self):
        target = {}
        self.mod.copy_response_headers(target, {"location": "https://x/y"})
        self.assertEqual(target["location"], "https://x/y")

    def test_copy_response_headers_forwards_www_authenticate(self):
        target = {}
        self.mod.copy_response_headers(
            target, {"www-authenticate": 'Bearer realm="x"'})
        self.assertEqual(target["www-authenticate"], 'Bearer realm="x"')

    def test_upstream_error_is_not_limit(self):
        self.assertFalse(self.mod.is_limit_error(
            400, b'{"error":{"message":"upstream_error"}}'))
        self.assertTrue(self.mod.is_limit_error(
            429, b'{"error":{"message":"upstream_error"}}'))

    def test_usage_limit_error_gets_long_cooldown(self):
        self.assertEqual(self.mod.cooldown_for(
            429, b'{"error":{"message":"GoUsageLimitError"}}', None), 3600)
        self.assertEqual(self.mod.cooldown_for(
            429, b'{"error":{"message":"FreeUsageLimitError"}}', None), 3600)
        self.assertIsNone(self.mod.cooldown_for(
            429, b'{"error":{"message":"rate_limit_exceeded"}}', None))

    def test_no_key_request_does_not_mark_state_dirty(self):
        cfg = type("Cfg", (), {
            "base_url": "https://x.invalid/v1", "model": "", "timeout": 10,
            "keys": [], "auth_keys": [], "state_file": "",
            "reload_interval": 0, "path": "nonexistent.ini",
            "log_requests": False, "upstreams": []})()
        engine = self.mod.ProxyEngine(cfg)
        kind, status, body, headers = asyncio.run(engine.forward(
            "POST", "/v1/chat/completions", b"{}", "application/json"))
        self.assertEqual(status, 503)
        self.assertFalse(engine._state_dirty)

    def test_save_state_without_path_clears_dirty(self):
        cfg = type("Cfg", (), {
            "base_url": "https://x.invalid/v1", "model": "", "timeout": 10,
            "keys": ["K"], "auth_keys": [], "state_file": "",
            "reload_interval": 0, "path": "nonexistent.ini",
            "log_requests": False, "upstreams": []})()
        engine = self.mod.ProxyEngine(cfg)
        engine._state_dirty = True
        engine.save_state()
        self.assertFalse(engine._state_dirty)

    def test_session_does_not_store_upstream_cookies(self):
        cookies = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                cookies.append(self.headers.get("Cookie", ""))
                self.send_response(200)
                self.send_header("Set-Cookie", "sid=leak")
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            cfg = type("Cfg", (), {
                "base_url": "http://127.0.0.1:%d/v1" % server.server_address[1],
                "model": "", "timeout": 5, "keys": ["K"], "auth_keys": [],
                "state_file": "", "reload_interval": 0,
                "path": "nonexistent.ini", "log_requests": False,
                "upstreams": []})()

            async def scenario():
                engine = self.mod.ProxyEngine(cfg)
                await engine.start()
                try:
                    self.assertIsInstance(
                        engine.session.cookie_jar, aiohttp.DummyCookieJar)
                    await engine.forward(
                        "POST", "/v1/chat/completions", b"{}",
                        "application/json")
                    await engine.forward(
                        "POST", "/v1/chat/completions", b"{}",
                        "application/json")
                finally:
                    await engine.close()

            asyncio.run(scenario())
            self.assertEqual(cookies, ["", ""])
        finally:
            server.shutdown()
            server.server_close()

    def test_handle_hides_internal_error_details(self):
        class Engine:
            def authorized(self, key):
                return True

            async def forward(self, *args, **kwargs):
                raise RuntimeError("secret upstream key")

        class Request:
            def __init__(self):
                self.app = {"engine": Engine()}
                self.headers = {}
                self.method = "POST"
                self.raw_path = "/v1/chat/completions"

            async def read(self):
                return b"{}"

        with contextlib.redirect_stdout(io.StringIO()):
            resp = asyncio.run(self.mod.handle(Request()))
        self.assertEqual(resp.status, 500)
        text = resp.text
        self.assertNotIn("secret upstream key", text)
        self.assertIn("proxy internal error", text)

    def test_authorized_handles_non_ascii(self):
        cfg = type("Cfg", (), {
            "base_url": "https://x.invalid/v1", "model": "", "timeout": 10,
            "keys": ["K"], "auth_keys": ["pk-1"]})()
        engine = self.mod.ProxyEngine(cfg)
        self.assertTrue(engine.authorized("pk-1"))
        self.assertFalse(engine.authorized("é"))
        self.assertFalse(engine.authorized("密钥"))

    def test_join_url_preserves_client_query(self):
        self.assertEqual(
            self.mod.join_upstream_url("https://api.example/v1", "/v1/models?x=1"),
            "https://api.example/v1/models?x=1")

    def test_join_url_non_v1_base_path(self):
        self.assertEqual(
            self.mod.join_upstream_url(
                "https://open.bigmodel.cn/api/paas/v4",
                "/api/paas/v4/chat/completions"),
            "https://open.bigmodel.cn/api/paas/v4/chat/completions")


class TestMultiUpstreamIntegration(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.upstream = start_upstream()
        cls.base = "http://127.0.0.1:%d" % cls.upstream.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def setUp(self):
        FakeUpstream.behavior = {}
        FakeUpstream.log = []

    def _start(self, ini_text, host="127.0.0.1"):
        port = free_port()
        ini_text = ini_text.replace("{port}", str(port))
        proc, path = start_proxy(ini_text, port, host)
        self.addCleanup(stop_proxy, proc)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return proc, path, port

    def test_auth_rejects_missing_and_wrong_key(self):
        ini = server_block("{port}", self.base + "/default", auth_keys="pk-1, pk-2")
        _, _, port = self._start(ini)
        self.assertEqual(http_call(port, "/v1/chat/completions")[0], 401)
        self.assertEqual(http_call(port, "/v1/chat/completions", key="wrong")[0], 401)
        self.assertEqual(http_call(port, "/v1/chat/completions", key="pk-1")[0], 200)
        self.assertEqual(
            http_call(port, "/v1/chat/completions", headers={"x-api-key": "pk-2"})[0], 200)
        self.assertEqual(FakeUpstream.log[0]["key"], "DEFAULT-KEY")

    def test_non_ascii_client_key_returns_401(self):
        ini = server_block("{port}", self.base + "/default", auth_keys="pk-1")
        _, _, port = self._start(ini)
        self.assertEqual(http_call(port, "/v1/chat/completions", key="é")[0], 401)

    def test_non_ascii_admin_key_returns_401(self):
        ini = server_block("{port}", self.base + "/default")
        ini = ini.replace("timeout = 10", "timeout = 10\nadmin_key = admin-1")
        _, _, port = self._start(ini)
        self.assertEqual(
            http_call(port, "/_admin/reload", key="é", body={})[0], 401)

    def test_refuse_non_loopback_without_auth_keys(self):
        ini = server_block("{port}", self.base + "/default", host="0.0.0.0")
        ini = ini.replace("{port}", str(free_port()))
        ini_file = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False, encoding="utf-8")
        ini_file.write(ini)
        ini_file.close()
        try:
            proc = subprocess.Popen(
                [sys.executable, SCRIPT, "--config", ini_file.name],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            out, _ = proc.communicate(timeout=10)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("auth_keys", out.decode("utf-8", errors="ignore"))
        finally:
            os.unlink(ini_file.name)

    def test_refuse_public_placeholder_auth_keys(self):
        ini = server_block("{port}", self.base + "/default",
                           host="0.0.0.0", auth_keys="pk-xxxx")
        ini = ini.replace("{port}", str(free_port()))
        ini_file = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False, encoding="utf-8")
        ini_file.write(ini)
        ini_file.close()
        try:
            proc = subprocess.Popen(
                [sys.executable, SCRIPT, "--config", ini_file.name],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            out, _ = proc.communicate(timeout=10)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("placeholder", out.decode("utf-8", errors="ignore"))
        finally:
            os.unlink(ini_file.name)

    def test_path_prefix_routing(self):
        ini = server_block("{port}", self.base + "/default")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2"])
        _, _, port = self._start(ini)
        status, body = http_call(port, "/alpha/v1/chat/completions")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["used_key"], "A1")
        self.assertEqual(FakeUpstream.log[-1]["path"], "/alpha/v1/chat/completions")
        status, body = http_call(port, "/v1/chat/completions")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["used_key"], "DEFAULT-KEY")
        self.assertEqual(FakeUpstream.log[-1]["path"], "/default/v1/chat/completions")

    def test_kiosapi_alias_routes_to_default(self):
        ini = server_block("{port}", self.base + "/default")
        _, _, port = self._start(ini)
        status, body = http_call(port, "/kiosapi/v1/chat/completions")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["used_key"], "DEFAULT-KEY")
        self.assertEqual(FakeUpstream.log[-1]["path"], "/default/v1/chat/completions")

    def test_upstream_without_keys_returns_config_error(self):
        ini = server_block("{port}", self.base + "/default")
        ini += "[upstream:empty]\nbase_url = %s/empty/v1\nkeys =\n\n" % self.base
        _, _, port = self._start(ini)
        status, body = http_call(port, "/empty/v1/chat/completions")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"]["type"], "proxy_config_error")
        self.assertIn("empty", json.loads(body)["error"]["message"])

    def test_large_body_forwarded(self):
        ini = server_block("{port}", self.base + "/default")
        _, _, port = self._start(ini)
        big = {"model": "m", "messages": [{"role": "user", "content": "x" * (2 * 1024 * 1024)}]}
        status, body = http_call(port, "/v1/chat/completions", body=big)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["used_key"], "DEFAULT-KEY")

    def test_non_v1_base_path_routing(self):
        ini = server_block("{port}", self.base + "/default")
        ini += upstream_block(
            "zhipu", self.base + "/api/paas/v4", ["Z1"], prefix="zhipu")
        _, _, port = self._start(ini)
        status, body = http_call(port, "/zhipu/api/paas/v4/chat/completions")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["used_key"], "Z1")
        self.assertEqual(FakeUpstream.log[-1]["path"], "/api/paas/v4/chat/completions")

    def test_rotates_to_second_key_on_429(self):
        FakeUpstream.behavior = {"A1": {"status": 429, "body": '{"error":{"message":"too many requests"}}'}}
        ini = server_block("{port}", self.base + "/default")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2"])
        _, _, port = self._start(ini)
        status, body = http_call(port, "/alpha/v1/chat/completions")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["used_key"], "A2")
        self.assertEqual([r["key"] for r in FakeUpstream.log], ["A1", "A2"])

    def test_rotates_to_third_key_when_second_also_limited(self):
        FakeUpstream.behavior = {
            "A1": {"status": 429, "body": '{"error":{"message":"rate limit"}}'},
            "A2": {"status": 429, "body": '{"error":{"message":"rate limit"}}'},
        }
        ini = server_block("{port}", self.base + "/default")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2", "A3"])
        _, _, port = self._start(ini)
        status, body = http_call(port, "/alpha/v1/chat/completions")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["used_key"], "A3")
        self.assertEqual([r["key"] for r in FakeUpstream.log], ["A1", "A2", "A3"])

    def test_all_keys_limited_returns_last_429(self):
        FakeUpstream.behavior = {
            "A1": {"status": 429, "body": '{"error":{"message":"first"}}'},
            "A2": {"status": 429, "body": '{"error":{"message":"second"}}'},
        }
        ini = server_block("{port}", self.base + "/default")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2"])
        _, _, port = self._start(ini)
        status, body = http_call(port, "/alpha/v1/chat/completions")
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)["error"]["message"], "second")

    def test_cooldown_skips_limited_key_on_next_request(self):
        FakeUpstream.behavior = {
            "A1": {"status": 429, "body": '{"error":{"message":"rate limit"}}',
                   "headers": {"Retry-After": "60"}},
        }
        ini = server_block("{port}", self.base + "/default")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2"])
        _, _, port = self._start(ini)
        self.assertEqual(json.loads(http_call(port, "/alpha/v1/chat/completions")[1])["used_key"], "A2")
        self.assertEqual(json.loads(http_call(port, "/alpha/v1/chat/completions")[1])["used_key"], "A2")
        self.assertEqual([r["key"] for r in FakeUpstream.log], ["A1", "A2", "A2"])

    def test_402_rotates(self):
        FakeUpstream.behavior = {
            "A1": {"status": 402, "body": '{"error":{"code":"insufficient_balance"}}'},
        }
        ini = server_block("{port}", self.base + "/default")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2"])
        _, _, port = self._start(ini)
        status, body = http_call(port, "/alpha/v1/chat/completions")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["used_key"], "A2")

    def test_401_rotates_to_next_key(self):
        FakeUpstream.behavior = {
            "A1": {"status": 401, "body": "invalid key"},
        }
        ini = server_block("{port}", self.base + "/default")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2"])
        _, _, port = self._start(ini)
        status, body = http_call(port, "/alpha/v1/chat/completions")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["used_key"], "A2")
        self.assertEqual([r["key"] for r in FakeUpstream.log], ["A1", "A2"])

    def test_all_401_returns_last_401(self):
        FakeUpstream.behavior = {
            "A1": {"status": 401, "body": "invalid key one"},
            "A2": {"status": 401, "body": "invalid key two"},
        }
        ini = server_block("{port}", self.base + "/default")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2"])
        _, _, port = self._start(ini)
        status, body = http_call(port, "/alpha/v1/chat/completions")
        self.assertEqual(status, 401)
        self.assertEqual(body, b"invalid key two")

    def test_auth_error_passthrough_when_disabled(self):
        FakeUpstream.behavior = {
            "A1": {"status": 401, "body": "invalid key"},
        }
        ini = server_block("{port}", self.base + "/default")
        ini = ini.replace("timeout = 10", "timeout = 10\nrotate_auth_errors = false")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2"])
        _, _, port = self._start(ini)
        status, body = http_call(port, "/alpha/v1/chat/completions")
        self.assertEqual(status, 401)
        self.assertEqual([r["key"] for r in FakeUpstream.log], ["A1"])

    def test_400_bad_key_rotates(self):
        FakeUpstream.behavior = {
            "A1": {"status": 400,
                   "body": '{"error":{"message":"Incorrect API key provided"}}'},
        }
        ini = server_block("{port}", self.base + "/default")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2"])
        _, _, port = self._start(ini)
        status, body = http_call(port, "/alpha/v1/chat/completions")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["used_key"], "A2")

    def test_400_invalid_parameter_passes_through(self):
        FakeUpstream.behavior = {
            "A1": {"status": 400,
                   "body": '{"error":{"message":"invalid parameter"}}'},
        }
        ini = server_block("{port}", self.base + "/default")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2"])
        _, _, port = self._start(ini)
        status, _ = http_call(port, "/alpha/v1/chat/completions")
        self.assertEqual(status, 400)
        self.assertEqual([r["key"] for r in FakeUpstream.log], ["A1"])

    def test_200_body_error_rotates(self):
        FakeUpstream.behavior = {
            "A1": {"status": 200,
                   "body": '{"base_resp":{"status_code":1002,"status_msg":"rate limit"}}'},
        }
        ini = server_block("{port}", self.base + "/default")
        ini += ("[upstream:alpha]\nbase_url = %s/alpha/v1\nprefix = /alpha\n"
                "keys = A1, A2\nerror_body_path = base_resp.status_code\n"
                "error_body_rotate_codes = 1002, 1008\n"
                "error_body_auth_codes = 1004\n\n" % self.base)
        _, _, port = self._start(ini)
        status, body = http_call(port, "/alpha/v1/chat/completions")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["used_key"], "A2")

    def test_200_body_auth_error_rotates(self):
        FakeUpstream.behavior = {
            "A1": {"status": 200,
                   "body": '{"base_resp":{"status_code":1004,"status_msg":"auth"}}'},
        }
        ini = server_block("{port}", self.base + "/default")
        ini += ("[upstream:alpha]\nbase_url = %s/alpha/v1\nprefix = /alpha\n"
                "keys = A1, A2\nerror_body_path = base_resp.status_code\n"
                "error_body_rotate_codes = 1002, 1008\n"
                "error_body_auth_codes = 1004\n\n" % self.base)
        _, _, port = self._start(ini)
        status, body = http_call(port, "/alpha/v1/chat/completions")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["used_key"], "A2")

    def test_200_body_unknown_error_passes_through(self):
        FakeUpstream.behavior = {
            "A1": {"status": 200,
                   "body": '{"base_resp":{"status_code":2013,"status_msg":"param"}}'},
        }
        ini = server_block("{port}", self.base + "/default")
        ini += ("[upstream:alpha]\nbase_url = %s/alpha/v1\nprefix = /alpha\n"
                "keys = A1, A2\nerror_body_path = base_resp.status_code\n"
                "error_body_rotate_codes = 1002, 1008\n"
                "error_body_auth_codes = 1004\n\n" % self.base)
        _, _, port = self._start(ini)
        status, body = http_call(port, "/alpha/v1/chat/completions")
        self.assertEqual(status, 200)
        self.assertIn(b"2013", body)
        self.assertEqual([r["key"] for r in FakeUpstream.log], ["A1"])

    def test_200_body_keyword_does_not_rotate(self):
        FakeUpstream.behavior = {
            "A1": {"status": 200, "body": '{"text":"rate limit is a concept"}'},
        }
        ini = server_block("{port}", self.base + "/default")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2"])
        _, _, port = self._start(ini)
        status, _ = http_call(port, "/alpha/v1/chat/completions")
        self.assertEqual(status, 200)
        self.assertEqual([r["key"] for r in FakeUpstream.log], ["A1"])

    def test_headers_forwarded_and_client_auth_replaced(self):
        ini = server_block("{port}", self.base + "/default", auth_keys="pk-1")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1"])
        _, _, port = self._start(ini)
        status, _ = http_call(
            port, "/alpha/v1/messages", key="pk-1",
            headers={"anthropic-version": "2023-06-01"})
        self.assertEqual(status, 200)
        headers = FakeUpstream.log[-1]["headers"]
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(headers["authorization"], "Bearer A1")
        self.assertNotIn("x-api-key", headers)

        status, _ = http_call(
            port, "/alpha/v1/messages", headers={"x-api-key": "pk-1"})
        self.assertEqual(status, 200)
        headers = FakeUpstream.log[-1]["headers"]
        self.assertEqual(headers["authorization"], "Bearer A1")
        self.assertNotIn("x-api-key", headers)

    def test_query_string_preserved(self):
        ini = server_block("{port}", self.base + "/default")
        _, _, port = self._start(ini)
        status, _ = http_call(port, "/v1/models?x=1", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(FakeUpstream.log[-1]["path"], "/default/v1/models?x=1")

    def test_bind_0_0_0_0_reachable_via_loopback(self):
        ini = server_block("{port}", self.base + "/default", host="0.0.0.0", auth_keys="pk-1")
        _, _, port = self._start(ini, host="127.0.0.1")
        self.assertEqual(http_call(port, "/v1/chat/completions", key="pk-1")[0], 200)

    def test_bind_ipv6_reachable_via_loopback(self):
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            probe.bind(("::1", 0))
        except OSError:
            self.skipTest("IPv6 unavailable")
        finally:
            probe.close()
        ini = server_block("{port}", self.base + "/default", host="::", auth_keys="pk-1")
        _, _, port = self._start(ini, host="::1")
        self.assertEqual(http_call(port, "/v1/chat/completions", key="pk-1", host="::1")[0], 200)

    def test_hot_reload_adds_key(self):
        ini = server_block("{port}", self.base + "/default")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1"])
        _, path, port = self._start(ini)
        self.assertEqual(json.loads(http_call(port, "/alpha/v1/chat/completions")[1])["used_key"], "A1")

        time.sleep(0.05)
        write_ini(path, server_block(port, self.base + "/default")
                  + upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2"]))
        time.sleep(2.5)

        used = [json.loads(http_call(port, "/alpha/v1/chat/completions")[1])["used_key"]
                for _ in range(2)]
        self.assertEqual(used, ["A1", "A2"])

    def test_hot_reload_invalid_config_keeps_old(self):
        ini = server_block("{port}", self.base + "/default")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1"])
        _, path, port = self._start(ini)
        self.assertEqual(json.loads(http_call(port, "/alpha/v1/chat/completions")[1])["used_key"], "A1")

        time.sleep(0.05)
        write_ini(path, "[broken]\nnot a valid config\n")
        time.sleep(2.5)

        status, body = http_call(port, "/alpha/v1/chat/completions")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["used_key"], "A1")

    def test_hot_reload_adds_upstream(self):
        ini = server_block("{port}", self.base + "/default")
        _, path, port = self._start(ini)
        self.assertEqual(http_call(port, "/beta/v1/chat/completions")[0], 200)
        self.assertEqual(FakeUpstream.log[-1]["path"], "/default/beta/v1/chat/completions")

        time.sleep(0.05)
        write_ini(path, server_block(port, self.base + "/default")
                  + upstream_block("beta", self.base + "/beta/v1", ["B1"]))
        time.sleep(2.5)

        status, body = http_call(port, "/beta/v1/chat/completions")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["used_key"], "B1")
        self.assertEqual(FakeUpstream.log[-1]["path"], "/beta/v1/chat/completions")

    def test_hot_reload_rejects_clearing_auth_keys_on_public_bind(self):
        ini = server_block("{port}", self.base + "/default", host="0.0.0.0", auth_keys="pk-1")
        _, path, port = self._start(ini, host="127.0.0.1")
        self.assertEqual(http_call(port, "/v1/chat/completions", key="pk-1")[0], 200)

        time.sleep(0.05)
        write_ini(path, server_block(port, self.base + "/default", host="0.0.0.0"))
        time.sleep(2.5)

        self.assertEqual(http_call(port, "/v1/chat/completions")[0], 401)
        self.assertEqual(http_call(port, "/v1/chat/completions", key="pk-1")[0], 200)

    def test_bind_multiple_hosts(self):
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            probe.bind(("::1", 0))
        except OSError:
            self.skipTest("IPv6 unavailable")
        finally:
            probe.close()
        ini = server_block("{port}", self.base + "/default",
                           host="127.0.0.1, ::1", auth_keys="pk-1")
        _, _, port = self._start(ini, host="127.0.0.1")
        self.assertEqual(http_call(port, "/v1/chat/completions", key="pk-1")[0], 200)
        self.assertEqual(
            http_call(port, "/v1/chat/completions", key="pk-1", host="::1")[0], 200)

    def test_upstream_user_agent_override(self):
        ini = server_block("{port}", self.base + "/default")
        ini += ("[upstream:alpha]\nbase_url = %s/alpha/v1\nprefix = /alpha\n"
                "keys = A1\nuser_agent = opencode/9.9.9\n\n" % self.base)
        _, _, port = self._start(ini)
        self.assertEqual(http_call(port, "/alpha/v1/chat/completions")[0], 200)
        self.assertEqual(FakeUpstream.log[-1]["headers"]["user-agent"], "opencode/9.9.9")

    def test_upstream_model_override_only_applies_to_that_upstream(self):
        ini = server_block("{port}", self.base + "/default")
        ini += ("[upstream:alpha]\nbase_url = %s/alpha/v1\nprefix = /alpha\n"
                "keys = A1\nmodel = forced-model\n\n" % self.base)
        _, _, port = self._start(ini)
        http_call(port, "/alpha/v1/chat/completions", body={"model": "client-model"})
        self.assertEqual(FakeUpstream.log[-1]["body_model"], "forced-model")
        http_call(port, "/v1/chat/completions", body={"model": "client-model"})
        self.assertEqual(FakeUpstream.log[-1]["body_model"], "client-model")

    def test_upstream_auth_style_x_api_key(self):
        ini = server_block("{port}", self.base + "/default")
        ini += ("[upstream:alpha]\nbase_url = %s/alpha/v1\nprefix = /alpha\n"
                "keys = A1\nauth_style = x-api-key\n\n" % self.base)
        _, _, port = self._start(ini)
        self.assertEqual(http_call(port, "/alpha/v1/chat/completions")[0], 200)
        headers = FakeUpstream.log[-1]["headers"]
        self.assertEqual(headers["x-api-key"], "A1")
        self.assertNotIn("authorization", headers)

    def test_upstream_auth_style_api_key(self):
        ini = server_block("{port}", self.base + "/default")
        ini += ("[upstream:alpha]\nbase_url = %s/alpha/v1\nprefix = /alpha\n"
                "keys = A1\nauth_style = api-key\n\n" % self.base)
        _, _, port = self._start(ini)
        self.assertEqual(http_call(port, "/alpha/v1/chat/completions")[0], 200)
        headers = FakeUpstream.log[-1]["headers"]
        self.assertEqual(headers["api-key"], "A1")
        self.assertNotIn("authorization", headers)

    def test_all_keys_limited_forwards_retry_after(self):
        FakeUpstream.behavior = {
            "A1": {"status": 429, "body": '{"error":{"message":"limited"}}',
                   "headers": {"Retry-After": "30"}},
            "A2": {"status": 429, "body": '{"error":{"message":"limited"}}',
                   "headers": {"Retry-After": "30"}},
        }
        ini = server_block("{port}", self.base + "/default")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2"])
        _, _, port = self._start(ini)
        status, headers, _ = http_call_full(port, "/alpha/v1/chat/completions")
        self.assertEqual(status, 429)
        lowered = {k.lower(): v for k, v in headers.items()}
        self.assertEqual(lowered.get("retry-after"), "30")

    def test_log_requests_prints_masked_keys(self):
        FakeUpstream.behavior = {
            "LONGKEY-1111": {"status": 429, "body": '{"error":{"message":"limited"}}'},
        }
        ini = server_block("{port}", self.base + "/default")
        ini = ini.replace("timeout = 10", "timeout = 10\nlog_requests = true")
        ini += upstream_block("alpha", self.base + "/alpha/v1",
                              ["LONGKEY-1111", "SHORT"])
        proc, _, port = self._start(ini)
        self.assertEqual(http_call(port, "/alpha/v1/chat/completions")[0], 200)
        proc.terminate()
        proc.wait(timeout=5)
        output = proc.stdout.read().decode("utf-8", errors="ignore")
        self.assertIn("key=...1111", output)
        self.assertNotIn("LONGKEY-1111", output)
        self.assertNotIn("SHORT", output)

    def test_short_key_not_leaked_in_cooldown_log(self):
        FakeUpstream.behavior = {
            "AB": {"status": 429, "body": '{"error":{"message":"limited"}}'},
        }
        ini = server_block("{port}", self.base + "/default")
        ini += upstream_block("alpha", self.base + "/alpha/v1",
                              ["AB", "LONGKEY-2222"])
        proc, _, port = self._start(ini)
        self.assertEqual(http_call(port, "/alpha/v1/chat/completions")[0], 200)
        proc.terminate()
        proc.wait(timeout=5)
        output = proc.stdout.read().decode("utf-8", errors="ignore")
        self.assertNotIn("key=...AB", output)
        self.assertNotIn("= AB", output)

    def test_session_header_generated_and_stable(self):
        ini = server_block("{port}", self.base + "/default")
        ini += ("[upstream:alpha]\nbase_url = %s/alpha/v1\nprefix = /alpha\n"
                "keys = A1\nsession_header = x-opencode-session\n\n" % self.base)
        _, _, port = self._start(ini)
        self.assertEqual(http_call(port, "/alpha/v1/chat/completions")[0], 200)
        self.assertEqual(http_call(port, "/alpha/v1/chat/completions")[0], 200)
        first = FakeUpstream.log[-2]["headers"]["x-opencode-session"]
        second = FakeUpstream.log[-1]["headers"]["x-opencode-session"]
        self.assertTrue(first.startswith("ses_"))
        self.assertEqual(first, second)

    def test_client_session_header_forwarded(self):
        ini = server_block("{port}", self.base + "/default")
        ini += ("[upstream:alpha]\nbase_url = %s/alpha/v1\nprefix = /alpha\n"
                "keys = A1\nsession_header = x-opencode-session\n\n" % self.base)
        _, _, port = self._start(ini)
        self.assertEqual(http_call(
            port, "/alpha/v1/chat/completions",
            headers={"x-opencode-session": "ses_client"})[0], 200)
        self.assertEqual(
            FakeUpstream.log[-1]["headers"]["x-opencode-session"], "ses_client")

    def test_native_session_header_mapped_to_required(self):
        ini = server_block("{port}", self.base + "/default")
        ini += ("[upstream:alpha]\nbase_url = %s/alpha/v1\nprefix = /alpha\n"
                "keys = A1\nsession_header = x-opencode-session\n\n" % self.base)
        _, _, port = self._start(ini)
        self.assertEqual(http_call(
            port, "/alpha/v1/chat/completions",
            headers={"x-session-id": "native-123"})[0], 200)
        self.assertEqual(
            FakeUpstream.log[-1]["headers"]["x-opencode-session"], "native-123")

    def test_state_persists_key_order_across_restart(self):
        tmpdir = tempfile.mkdtemp()
        ini_path = os.path.join(tmpdir, "proxy.ini")
        port1 = free_port()
        write_ini(ini_path, server_block(port1, self.base + "/default")
                  + upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2", "A3"]))
        proc = start_proxy_path(ini_path, port1)
        try:
            used = [json.loads(http_call(port1, "/alpha/v1/chat/completions")[1])["used_key"]
                    for _ in range(2)]
            self.assertEqual(used, ["A1", "A2"])
            time.sleep(1.8)
        finally:
            stop_proxy(proc)

        state_path = ini_path + ".state.json"
        self.assertTrue(os.path.exists(state_path))
        with open(state_path, encoding="utf-8") as fh:
            state_text = fh.read()
        self.assertNotIn("A1", state_text)
        self.assertNotIn("A2", state_text)
        self.assertIn("version", state_text)

        port2 = free_port()
        write_ini(ini_path, server_block(port2, self.base + "/default")
                  + upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2", "A3"]))
        proc2 = start_proxy_path(ini_path, port2)
        try:
            used = json.loads(http_call(port2, "/alpha/v1/chat/completions")[1])["used_key"]
            self.assertEqual(used, "A3")
        finally:
            stop_proxy(proc2)
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_state_corrupt_falls_back_to_config_order(self):
        tmpdir = tempfile.mkdtemp()
        ini_path = os.path.join(tmpdir, "proxy.ini")
        port = free_port()
        write_ini(ini_path, server_block(port, self.base + "/default")
                  + upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2"]))
        with open(ini_path + ".state.json", "w", encoding="utf-8") as fh:
            fh.write("not json")
        proc = start_proxy_path(ini_path, port)
        try:
            used = json.loads(http_call(port, "/alpha/v1/chat/completions")[1])["used_key"]
            self.assertEqual(used, "A1")
        finally:
            stop_proxy(proc)
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_hot_reload_preserves_runtime_order_and_appends(self):
        ini = server_block("{port}", self.base + "/default")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2"])
        _, path, port = self._start(ini)
        self.assertEqual(json.loads(http_call(port, "/alpha/v1/chat/completions")[1])["used_key"], "A1")

        time.sleep(0.05)
        write_ini(path, server_block(port, self.base + "/default")
                  + upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2", "A3"]))
        time.sleep(2.5)

        used = [json.loads(http_call(port, "/alpha/v1/chat/completions")[1])["used_key"]
                for _ in range(3)]
        self.assertEqual(used, ["A2", "A1", "A3"])

    def test_admin_reload_requires_admin_key(self):
        ini = server_block("{port}", self.base + "/default")
        ini = ini.replace("timeout = 10", "timeout = 10\nadmin_key = admin-1")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1"])
        _, _, port = self._start(ini)
        self.assertEqual(http_call(port, "/_admin/reload", body={})[0], 401)
        self.assertEqual(http_call(port, "/_admin/reload", key="wrong", body={})[0], 401)
        self.assertEqual(http_call(port, "/_admin/reload", key="admin-1", body={})[0], 200)

    def test_admin_reload_applies_immediately(self):
        ini = server_block("{port}", self.base + "/default")
        ini = ini.replace("timeout = 10", "timeout = 10\nadmin_key = admin-1")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1"])
        _, path, port = self._start(ini)
        time.sleep(0.05)
        write_ini(path, server_block(port, self.base + "/default")
                  .replace("timeout = 10", "timeout = 10\nadmin_key = admin-1")
                  + upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2"]))
        self.assertEqual(http_call(port, "/_admin/reload", key="admin-1", body={})[0], 200)
        used = [json.loads(http_call(port, "/alpha/v1/chat/completions")[1])["used_key"]
                for _ in range(2)]
        self.assertEqual(used, ["A1", "A2"])

    def test_admin_reload_rejects_invalid_config_and_keeps_old(self):
        ini = server_block("{port}", self.base + "/default")
        ini = ini.replace("timeout = 10", "timeout = 10\nadmin_key = admin-1")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1"])
        _, path, port = self._start(ini)
        time.sleep(0.05)
        write_ini(path, "[broken]\n")
        self.assertEqual(http_call(port, "/_admin/reload", key="admin-1", body={})[0], 400)
        status, body = http_call(port, "/alpha/v1/chat/completions")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["used_key"], "A1")

    def test_manage_add_key_triggers_immediate_reload(self):
        ini = server_block("{port}", self.base + "/default")
        ini = ini.replace("timeout = 10", "timeout = 10\nadmin_key = admin-1")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1"])
        _, path, port = self._start(ini)
        result = subprocess.run(
            [sys.executable, MANAGE, "--config", path, "add-key", "alpha", "A2"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(result.returncode, 0)
        used = [json.loads(http_call(port, "/alpha/v1/chat/completions")[1])["used_key"]
                for _ in range(2)]
        self.assertEqual(used, ["A1", "A2"])

    def test_route_prefix_only_with_query(self):
        ini = server_block("{port}", self.base + "/default")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1"])
        _, _, port = self._start(ini)
        status, _ = http_call(port, "/alpha?x=1")
        self.assertEqual(status, 200)
        self.assertEqual(FakeUpstream.log[-1]["path"], "/alpha/v1/?x=1")

    def test_200_body_error_all_keys_returns_429(self):
        FakeUpstream.behavior = {
            "A1": {"status": 200, "body": '{"base_resp":{"status_code":1002}}'},
            "A2": {"status": 200, "body": '{"base_resp":{"status_code":1002}}'},
        }
        ini = server_block("{port}", self.base + "/default")
        ini += ("[upstream:alpha]\nbase_url = %s/alpha/v1\nprefix = /alpha\n"
                "keys = A1, A2\nerror_body_path = base_resp.status_code\n"
                "error_body_rotate_codes = 1002\n\n" % self.base)
        _, _, port = self._start(ini)
        status, body = http_call(port, "/alpha/v1/chat/completions")
        self.assertEqual(status, 429)
        self.assertIn(b"1002", body)

    def test_200_body_auth_all_keys_returns_401(self):
        FakeUpstream.behavior = {
            "A1": {"status": 200, "body": '{"base_resp":{"status_code":1004}}'},
            "A2": {"status": 200, "body": '{"base_resp":{"status_code":1004}}'},
        }
        ini = server_block("{port}", self.base + "/default")
        ini += ("[upstream:alpha]\nbase_url = %s/alpha/v1\nprefix = /alpha\n"
                "keys = A1, A2\nerror_body_path = base_resp.status_code\n"
                "error_body_auth_codes = 1004\n\n" % self.base)
        _, _, port = self._start(ini)
        status, body = http_call(port, "/alpha/v1/chat/completions")
        self.assertEqual(status, 401)
        self.assertIn(b"1004", body)

    def test_stream_200_body_error_rotates(self):
        FakeUpstream.behavior = {
            "A1": {"status": 200, "body": '{"base_resp":{"status_code":1002}}'},
        }
        ini = server_block("{port}", self.base + "/default")
        ini += ("[upstream:alpha]\nbase_url = %s/alpha/v1\nprefix = /alpha\n"
                "keys = A1, A2\nerror_body_path = base_resp.status_code\n"
                "error_body_rotate_codes = 1002\n\n" % self.base)
        _, _, port = self._start(ini)
        status, body = http_call(
            port, "/alpha/v1/chat/completions",
            body={"model": "m", "stream": True})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["used_key"], "A2")

    def test_503_body_keyword_does_not_rotate(self):
        FakeUpstream.behavior = {
            "A1": {"status": 503, "body": '{"error":"rate limit exceeded"}'},
        }
        ini = server_block("{port}", self.base + "/default")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1", "A2"])
        _, _, port = self._start(ini)
        status, _ = http_call(port, "/alpha/v1/chat/completions")
        self.assertEqual(status, 503)
        self.assertEqual([r["key"] for r in FakeUpstream.log], ["A1"])

    def test_cookie_not_forwarded(self):
        ini = server_block("{port}", self.base + "/default")
        ini += upstream_block("alpha", self.base + "/alpha/v1", ["A1"])
        _, _, port = self._start(ini)
        self.assertEqual(http_call(
            port, "/alpha/v1/chat/completions",
            headers={"Cookie": "session=secret"})[0], 200)
        self.assertNotIn("cookie", FakeUpstream.log[-1]["headers"])

    def test_bind_bracketed_ipv6(self):
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            probe.bind(("::1", 0))
        except OSError:
            self.skipTest("IPv6 unavailable")
        finally:
            probe.close()
        ini = server_block("{port}", self.base + "/default", host="[::]", auth_keys="pk-1")
        _, _, port = self._start(ini, host="::1")
        self.assertEqual(
            http_call(port, "/v1/chat/completions", key="pk-1", host="::1")[0], 200)


if __name__ == "__main__":
    unittest.main()
