import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANAGE = os.path.join(ROOT, "manage.py")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def load_manage():
    spec = importlib.util.spec_from_file_location("manage_under_test", MANAGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SAMPLE = """[server]
host = 127.0.0.1
port = 8787
base_url = https://example.invalid/v1
admin_key = adm-test

[keys]
key1 = sk-kiosapi-aaaa
key2 = sk-kiosapi-bbbb

[upstream:kenari]
base_url = https://kenari.id/v1
; keep this comment
keys = kn-aaa111, kn-bbb222
user_agent = test-agent

[upstream:demo]
base_url = https://demo.invalid/v1
keys = d-111
"""


class TestTextEditing(unittest.TestCase):

    def setUp(self):
        self.mod = load_manage()
        self.lines = SAMPLE.splitlines()

    def test_section_keys_default_and_upstream(self):
        self.assertEqual(self.mod.section_keys(self.lines, "keys"),
                         ["sk-kiosapi-aaaa", "sk-kiosapi-bbbb"])
        self.assertEqual(self.mod.section_keys(self.lines, "upstream:kenari"),
                         ["kn-aaa111", "kn-bbb222"])

    def test_section_keys_strip_inline_comments(self):
        lines = ("[keys]\n"
                 "keys = A1, A2 ; two keys\n"
                 "key1 = B1 ; first\n"
                 "key2 = B2#fragment\n"
                 "\n[upstream:alpha]\nbase_url = https://x.invalid/v1\n"
                 "keys = C1, C2 ; names\n").splitlines()
        self.assertEqual(self.mod.section_keys(lines, "keys"), ["A1", "A2"])
        self.assertEqual(self.mod.section_keys(lines, "upstream:alpha"),
                         ["C1", "C2"])

    def test_section_keys_space_separated(self):
        lines = ("[keys]\nkeys = A1 A2\n"
                 "\n[upstream:alpha]\nbase_url = https://x.invalid/v1\n"
                 "keys = B1 B2\n").splitlines()
        self.assertEqual(self.mod.section_keys(lines, "keys"), ["A1", "A2"])
        self.assertEqual(self.mod.section_keys(lines, "upstream:alpha"),
                         ["B1", "B2"])

    def test_normalize_upstream_strips_prefix_space(self):
        self.assertEqual(self.mod.normalize_upstream("upstream: kenari"),
                         "kenari")
        self.assertEqual(self.mod.normalize_upstream(" kenari "), "kenari")

    def test_set_section_keys_preserves_comments_and_fields(self):
        self.mod.set_section_keys(self.lines, "upstream:kenari", ["kn-aaa111", "kn-ccc333"])
        text = "\n".join(self.lines)
        self.assertIn("keys = kn-aaa111, kn-ccc333", text)
        self.assertIn("; keep this comment", text)
        self.assertIn("user_agent = test-agent", text)

    def test_set_section_keys_default(self):
        self.mod.set_section_keys(self.lines, "keys", ["sk-new"])
        text = "\n".join(self.lines)
        self.assertIn("key1 = sk-new", text)
        self.assertNotIn("key2", text)

    def test_all_upstreams(self):
        self.assertEqual(self.mod.all_upstreams(self.lines), ["default", "kenari", "demo"])

    def test_mask_key(self):
        self.assertEqual(self.mod.mask_key("abcd"), "...")
        self.assertEqual(self.mod.mask_key("short"), "...")
        self.assertEqual(self.mod.mask_key("abcdefgh"), "...")
        self.assertEqual(self.mod.mask_key("kn-aaa111"), "...a111")
        self.assertEqual(self.mod.mask_key("abcdefghij"), "...ghij")
        self.assertEqual(self.mod.mask_key("abcdefghijkl"), "...ijkl")
        self.assertEqual(self.mod.mask_key("abcdefghijklm"), "abcdef...jklm")
        self.assertEqual(self.mod.mask_key("kn-aaa111", reveal=True), "kn-aaa111")
        for key in ("abcd", "abcde", "abcdefghi", "abcdefghij", "abcdefghijklm"):
            self.assertNotEqual(self.mod.mask_key(key), key)
            self.assertNotIn(key, self.mod.mask_key(key))


class TestCli(unittest.TestCase):

    def setUp(self):
        self.mod = load_manage()
        self.tmp = tempfile.mkdtemp()
        self.ini = os.path.join(self.tmp, "proxy.ini")
        with open(self.ini, "w", encoding="utf-8") as fh:
            fh.write(SAMPLE)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.mod.main(["--config", self.ini] + list(args))
        return code, out.getvalue()

    def read_keys(self, section):
        return self.mod.section_keys(self.mod.read_lines(self.ini), section)

    def _serve(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_readme_sample_config_validates(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "README.md"), encoding="utf-8") as fh:
            text = fh.read()
        block = text.split("```ini", 1)[1].split("```", 1)[0]
        with open(self.ini, "w", encoding="utf-8") as fh:
            fh.write(block)
        code, out = self.run_cli("validate")
        self.assertEqual(code, 0, out)
        self.assertIn("config OK", out)

    def test_validate_rejects_malformed_upstream_sections(self):
        for header in ("[ upstream:alpha ]", "[Upstream:alpha]"):
            with open(self.ini, "w", encoding="utf-8") as fh:
                fh.write(SAMPLE.split("[upstream:kenari]")[0] + header +
                         "\nbase_url = https://a.invalid/v1\nkeys = k1\n")
            code, out = self.run_cli("validate")
            self.assertEqual(code, 1, header + out)
            self.assertIn("invalid upstream section", out)
            code, out = self.run_cli("add-key", "alpha", "k2", "--no-reload")
            self.assertEqual(code, 1, header + out)
            self.assertIn("invalid upstream section", out)

    def test_add_key(self):
        code, out = self.run_cli("add-key", "kenari", "kn-ccc333", "--no-reload")
        self.assertEqual(code, 0)
        self.assertEqual(self.read_keys("upstream:kenari"),
                         ["kn-aaa111", "kn-bbb222", "kn-ccc333"])
        self.assertNotIn("kn-ccc333", out)

    def test_add_key_refuses_invalid_config(self):
        with open(self.ini, "w", encoding="utf-8") as fh:
            fh.write(SAMPLE + "\n[server]\nhost = 127.0.0.1\n")
        code, out = self.run_cli("add-key", "kenari", "kn-ccc333", "--no-reload")
        self.assertEqual(code, 1)
        self.assertIn("invalid config", out)
        with open(self.ini, encoding="utf-8") as fh:
            self.assertNotIn("kn-ccc333", fh.read())

    def test_probe_does_not_follow_redirects(self):
        hits = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        target = self._serve(Target)
        target_port = target.server_address[1]

        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:%d/leak" % target_port)
                self.end_headers()

            def log_message(self, *args):
                pass

        source = self._serve(Redirect)
        upstream = self.mod.kiosapi.Upstream(
            "x", "http://127.0.0.1:%d/v1" % source.server_address[1], "/x", ["k"])
        status, detail = self.mod._probe(upstream, "k", 5)
        self.assertEqual(status, "HTTP 302")
        self.assertEqual(hits, [])

    def test_reload_does_not_follow_redirects(self):
        hits = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()

            def do_POST(self):
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        target = self._serve(Target)
        target_port = target.server_address[1]

        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:%d/leak" % target_port)
                self.end_headers()

            def log_message(self, *args):
                pass

        source = self._serve(Redirect)
        path = os.path.join(self.tmp, "redirect.ini")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("[server]\nhost = 127.0.0.1\nport = %d\n"
                     "admin_key = admin-1\n[keys]\nkey1 = k1\n"
                     % source.server_address[1])
        ok, detail = self.mod.reload_proxy(path)
        self.assertFalse(ok)
        self.assertIn("302", detail)
        self.assertEqual(hits, [])

    def test_reload_bypasses_environment_proxy(self):
        admin_hits = []

        class Admin(BaseHTTPRequestHandler):
            def do_POST(self):
                admin_hits.append(
                    (self.path, self.headers.get("Authorization")))
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        admin = self._serve(Admin)
        proxy_hits = []

        class Proxy(BaseHTTPRequestHandler):
            def do_POST(self):
                proxy_hits.append(self.path)
                self.send_response(502)
                self.end_headers()

            def log_message(self, *args):
                pass

        proxy = self._serve(Proxy)
        proxy_url = "http://127.0.0.1:%d" % proxy.server_address[1]
        keys = ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY")
        saved = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)
            os.environ["http_proxy"] = proxy_url
            os.environ["HTTP_PROXY"] = proxy_url
            mod = load_manage()
            path = os.path.join(self.tmp, "reload.ini")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("[server]\nhost = 127.0.0.1\nport = 1\n"
                         "admin_key = adm-proxy-test\n[keys]\nkey1 = k1\n")
            ok, detail = mod.reload_proxy(
                path, port_override=admin.server_address[1])
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertTrue(ok, detail)
        self.assertEqual(proxy_hits, [])
        self.assertEqual(admin_hits,
                         [("/_admin/reload", "Bearer adm-proxy-test")])

    def test_probe_joins_base_url_with_query(self):
        paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data":[]}')

            def log_message(self, *args):
                pass

        server = self._serve(Handler)
        upstream = self.mod.kiosapi.Upstream(
            "x", "http://127.0.0.1:%d/v1?api=1" % server.server_address[1],
            "/x", ["k"])
        status, detail = self.mod._probe(upstream, "k", 5)
        self.assertEqual(status, "OK")
        self.assertEqual(paths, ["/v1/models?api=1"])

    def test_probe_sets_content_type_for_models(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Content-Type", ""))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data":[]}')

            def log_message(self, *args):
                pass

        server = self._serve(Handler)
        upstream = self.mod.kiosapi.Upstream(
            "x", "http://127.0.0.1:%d/v1" % server.server_address[1],
            "/x", ["k"])
        status, detail = self.mod._probe(upstream, "k", 5)
        self.assertEqual(status, "OK")
        self.assertEqual(seen, ["application/json"])

    def test_add_duplicate_is_skipped(self):
        code, _ = self.run_cli("add-key", "kenari", "kn-aaa111", "--no-reload")
        self.assertEqual(code, 1)
        self.assertEqual(len(self.read_keys("upstream:kenari")), 2)

    def test_set_key(self):
        code, _ = self.run_cli("set-key", "kenari", "2", "kn-new222", "--no-reload")
        self.assertEqual(code, 0)
        self.assertEqual(self.read_keys("upstream:kenari"),
                         ["kn-aaa111", "kn-new222"])

    def test_set_key_duplicate_refused(self):
        code, out = self.run_cli("set-key", "kenari", "1", "kn-bbb222", "--no-reload")
        self.assertEqual(code, 1)
        self.assertIn("already exists", out)
        self.assertEqual(self.read_keys("upstream:kenari"),
                         ["kn-aaa111", "kn-bbb222"])

    def test_set_key_empty_refused(self):
        code, out = self.run_cli("set-key", "kenari", "1", "", "--no-reload")
        self.assertEqual(code, 1)
        self.assertIn("empty key", out)
        self.assertEqual(self.read_keys("upstream:kenari"),
                         ["kn-aaa111", "kn-bbb222"])

    def test_add_key_invalid_rejected(self):
        code, out = self.run_cli("add-key", "kenari", "bad key", "--no-reload")
        self.assertEqual(code, 1)
        self.assertIn("invalid key", out)
        self.assertEqual(len(self.read_keys("upstream:kenari")), 2)

    def test_set_key_invalid_rejected(self):
        code, out = self.run_cli("set-key", "kenari", "1", "a,b", "--no-reload")
        self.assertEqual(code, 1)
        self.assertIn("invalid key", out)
        self.assertEqual(self.read_keys("upstream:kenari"),
                         ["kn-aaa111", "kn-bbb222"])

    def test_list_directory_config(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.mod.main(["--config", self.tmp, "list"])
        self.assertEqual(code, 1)
        self.assertIn("config path is a directory", out.getvalue())

    def test_list_missing_config(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.mod.main(
                ["--config", os.path.join(self.tmp, "nope.ini"), "list"])
        self.assertEqual(code, 1)
        self.assertIn("config not found", out.getvalue())

    def test_list_invalid_utf8_config(self):
        path = os.path.join(self.tmp, "bad.ini")
        with open(path, "wb") as fh:
            fh.write(b"\xff\xfe[server]\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.mod.main(["--config", path, "list"])
        self.assertEqual(code, 1)
        self.assertIn("cannot decode config", out.getvalue())

    def test_remove_key_by_index_and_tail(self):
        code, _ = self.run_cli("remove-key", "kenari", "1", "--no-reload")
        self.assertEqual(code, 0)
        self.assertEqual(self.read_keys("upstream:kenari"), ["kn-bbb222"])
        code, _ = self.run_cli("remove-key", "kenari", "b222", "--no-reload")
        self.assertEqual(code, 0)
        self.assertEqual(self.read_keys("upstream:kenari"), [])

    def test_remove_keys_fuzzy_requires_yes_for_multiple(self):
        code, out = self.run_cli("remove-keys", "kenari", "kn-", "--no-reload")
        self.assertEqual(code, 1)
        self.assertIn("without --yes", out)
        self.assertEqual(len(self.read_keys("upstream:kenari")), 2)
        code, _ = self.run_cli("remove-keys", "kenari", "kn-", "--yes", "--no-reload")
        self.assertEqual(code, 0)
        self.assertEqual(self.read_keys("upstream:kenari"), [])

    def test_remove_keys_single_match_without_yes(self):
        code, _ = self.run_cli("remove-keys", "kenari", "aaa", "--no-reload")
        self.assertEqual(code, 0)
        self.assertEqual(self.read_keys("upstream:kenari"), ["kn-bbb222"])

    def test_clear_keys_requires_yes(self):
        code, out = self.run_cli("clear-keys", "kenari", "--no-reload")
        self.assertEqual(code, 1)
        self.assertIn("--yes", out)
        self.assertEqual(len(self.read_keys("upstream:kenari")), 2)
        code, _ = self.run_cli("clear-keys", "kenari", "--yes", "--no-reload")
        self.assertEqual(code, 0)
        self.assertEqual(self.read_keys("upstream:kenari"), [])

    def test_clear_all_keys(self):
        code, _ = self.run_cli("clear-all-keys", "--yes", "--no-reload")
        self.assertEqual(code, 0)
        self.assertEqual(self.read_keys("keys"), [])
        self.assertEqual(self.read_keys("upstream:kenari"), [])
        self.assertEqual(self.read_keys("upstream:demo"), [])

    def test_list_masks_keys(self):
        code, out = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("...a111", out)
        self.assertNotIn("kn-aaa111", out)
        self.assertNotIn("sk-kiosapi-aaaa", out)

    def test_show_reveal(self):
        code, out = self.run_cli("show", "kenari", "--reveal")
        self.assertEqual(code, 0)
        self.assertIn("kn-aaa111", out)

    def test_show_json_does_not_leak_full_keys(self):
        code, out = self.run_cli("show", "kenari", "--json")
        self.assertEqual(code, 0)
        self.assertNotIn("kn-aaa111", out)
        self.assertNotIn("kn-bbb222", out)
        self.assertIn("...a111", out)

    def test_validate(self):
        code, out = self.run_cli("validate")
        self.assertEqual(code, 0)
        self.assertIn("config OK", out)
        self.assertIn("kenari", out)

    def test_state_shows_persisted_order(self):
        import kiosapi
        state = {"version": 1, "upstreams": {
            "kenari": [kiosapi.key_fingerprint("kn-bbb222"),
                       kiosapi.key_fingerprint("kn-aaa111")],
        }}
        with open(self.ini + ".state.json", "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        code, out = self.run_cli("state")
        self.assertEqual(code, 0)
        self.assertIn("...b222 -> ...a111", out)

    def test_check_against_local_upstream(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data":[]}')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            with open(self.ini, "w", encoding="utf-8") as fh:
                fh.write(SAMPLE.replace("https://kenari.id/v1",
                                        "http://127.0.0.1:%d/v1" % port))
            code, out = self.run_cli("check", "kenari")
            self.assertEqual(code, 0)
            self.assertIn("OK", out)
        finally:
            server.shutdown()
            server.server_close()

    def test_check_400_auth_keyword_reports_auth(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    b'{"error":{"message":"Incorrect API key provided"}}')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            with open(self.ini, "w", encoding="utf-8") as fh:
                fh.write(SAMPLE.replace("https://kenari.id/v1",
                                        "http://127.0.0.1:%d/v1" % port))
            code, out = self.run_cli("check", "kenari")
            self.assertEqual(code, 0)
            self.assertIn("AUTH", out)
        finally:
            server.shutdown()
            server.server_close()

    def test_find_section_inline_comment(self):
        lines = ("""\
[server] ; server
host = 127.0.0.1
port = 8787
base_url = https://example.invalid/v1

[keys] ; default
key1 = sk-a

[upstream:alpha] ; alpha
base_url = https://alpha.invalid/v1
keys = A1, A2
""").splitlines()
        self.assertEqual(self.mod.section_keys(lines, "upstream:alpha"), ["A1", "A2"])
        self.assertEqual(self.mod.all_upstreams(lines), ["default", "alpha"])

    def test_remove_key_multiple_matches_refused(self):
        with open(self.ini, "w", encoding="utf-8") as fh:
            fh.write(SAMPLE.replace("kn-aaa111, kn-bbb222", "kn-111abc, kn-222abc"))
        code, out = self.run_cli("remove-key", "kenari", "abc", "--no-reload")
        self.assertEqual(code, 1)
        self.assertIn("matches 2", out)
        self.assertEqual(len(self.read_keys("upstream:kenari")), 2)

    def test_add_key_keeps_existing_keys_after_empty_keys_line(self):
        with open(self.ini, "w", encoding="utf-8") as fh:
            fh.write(SAMPLE.replace("key1 = sk-kiosapi-aaaa\nkey2 = sk-kiosapi-bbbb",
                                    "keys =\nkey1 = sk-kiosapi-aaaa\n"
                                    "key2 = sk-kiosapi-bbbb"))
        code, out = self.run_cli("add-key", "default", "sk-new", "--no-reload")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read_keys("keys"),
                         ["sk-kiosapi-aaaa", "sk-kiosapi-bbbb", "sk-new"])

    def test_add_key_removes_multiline_key_continuation(self):
        with open(self.ini, "w", encoding="utf-8") as fh:
            fh.write(SAMPLE.replace("keys = kn-aaa111, kn-bbb222",
                                    "keys = kn-aaa111,\n    kn-bbb222"))
        code, out = self.run_cli("add-key", "kenari", "kn-ccc333", "--no-reload")
        self.assertEqual(code, 0, out)
        code, out = self.run_cli("validate")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read_keys("upstream:kenari"),
                         ["kn-aaa111", "kn-bbb222", "kn-ccc333"])
        with open(self.ini, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text.count("kn-bbb222"), 1)

    def test_remove_key_drops_continuation_after_interleaved_comment(self):
        with open(self.ini, "w", encoding="utf-8") as fh:
            fh.write(SAMPLE.replace(
                "[upstream:kenari]\nbase_url = https://kenari.id/v1\n"
                "; keep this comment\nkeys = kn-aaa111, kn-bbb222",
                "[upstream:kenari]\nkeys = kn-aaa111,\n    kn-bbb222\n"
                "; note\n    kn-ccc333\nbase_url = https://kenari.id/v1"))
        code, out = self.run_cli("remove-key", "kenari", "kn-aaa111", "--no-reload")
        self.assertEqual(code, 0, out)
        code, out = self.run_cli("validate")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read_keys("upstream:kenari"),
                         ["kn-bbb222", "kn-ccc333"])
        with open(self.ini, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("kn-aaa111", text)

    def test_section_keys_prefers_keys_line_over_key_options(self):
        lines = ("[keys]\nkeys = A1, A2\nkey1 = B1\n"
                 "[upstream:alpha]\nbase_url = https://a.invalid/v1\n"
                 "keys = C1\nkey1 = D1\n").splitlines()
        self.assertEqual(self.mod.section_keys(lines, "keys"), ["A1", "A2"])
        self.assertEqual(self.mod.section_keys(lines, "upstream:alpha"), ["C1"])

    def test_cli_rejects_default_section(self):
        with open(self.ini, "w", encoding="utf-8") as fh:
            fh.write(SAMPLE.replace("[server]", "[DEFAULT]\nleak = SECRET\n[server]"))
        code, out = self.run_cli("list")
        self.assertEqual(code, 1)
        self.assertIn("DEFAULT", out)

    def test_section_keys_matches_proxy_key_option_rules(self):
        lines = ("[keys]\nkeys =\nkey1 = A\nkey_backup = B\nkey2 = C\n"
                 "[upstream:alpha]\nbase_url = https://a.invalid/v1\n"
                 "key1 = X\n").splitlines()
        self.assertEqual(self.mod.section_keys(lines, "keys"), ["A", "B", "C"])
        self.assertEqual(self.mod.section_keys(lines, "upstream:alpha"), ["X"])

    def test_section_keys_handles_colon_separator_and_multiline(self):
        lines = ("[keys]\nkey1: A\nkey2 = B\n  C\n").splitlines()
        self.assertEqual(self.mod.section_keys(lines, "keys"), ["A", "B", "C"])

    def test_section_keys_resolves_padded_upstream_header(self):
        lines = ("[keys]\nkey1 = A\n"
                 "[upstream: alpha ]\nbase_url = https://a.invalid/v1\n"
                 "keys = X, Y\n").splitlines()
        self.assertEqual(self.mod.section_keys(lines, "keys"), ["A"])
        self.assertEqual(self.mod.section_keys(lines, "upstream:alpha"),
                         ["X", "Y"])

    def test_section_keys_rejects_padded_default_header_like_proxy(self):
        lines = ("[ keys ]\nkey1 = A\n").splitlines()
        with self.assertRaises(KeyError):
            self.mod.section_keys(lines, "keys")

    def test_cli_rejects_duplicate_padded_upstream_sections(self):
        with open(self.ini, "w", encoding="utf-8") as fh:
            fh.write(SAMPLE.replace(
                "[upstream:demo]",
                "[upstream: kenari ]\nbase_url = https://other.invalid/v1\n"
                "keys = dup-1\n\n[upstream:demo]"))
        code, out = self.run_cli("list")
        self.assertEqual(code, 1)
        self.assertIn("duplicate", out.lower())
        code, out = self.run_cli("add-key", "kenari", "kn-new", "--no-reload")
        self.assertEqual(code, 1)
        self.assertIn("duplicate", out.lower())

    def test_cli_commands_accept_padded_upstream_header(self):
        with open(self.ini, "w", encoding="utf-8") as fh:
            fh.write(SAMPLE.replace(
                "[upstream:kenari]", "[upstream: kenari ]"))
        code, out = self.run_cli("add-key", "kenari", "kn-ccc333", "--no-reload")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read_keys("upstream:kenari"),
                         ["kn-aaa111", "kn-bbb222", "kn-ccc333"])
        code, out = self.run_cli("list", "--json")
        self.assertEqual(code, 0, out)
        rows = json.loads(out)
        kenari = [r for r in rows if r["upstream"] == "kenari"][0]
        self.assertEqual(kenari["base_url"], "https://kenari.id/v1")
        code, out = self.run_cli("show", "kenari", "--json")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["base_url"], "https://kenari.id/v1")
        code, out = self.run_cli("remove-key", "kenari", "kn-ccc333", "--no-reload")
        self.assertEqual(code, 0, out)
        self.assertNotIn("kn-ccc333", self.read_keys("upstream:kenari"))

    def test_write_lines_does_not_duplicate_keys_when_rewriting(self):
        code, _ = self.run_cli("add-key", "default", "sk-new", "--no-reload")
        self.assertEqual(code, 0)
        with open(self.ini, encoding="utf-8") as fh:
            self.assertIn("sk-new", fh.read())
        code, _ = self.run_cli("remove-key", "default", "sk-new", "--no-reload")
        self.assertEqual(code, 0)
        with open(self.ini, encoding="utf-8") as fh:
            second = fh.read()
        self.assertEqual(second.count("key1 ="), 1)
        self.assertNotIn("sk-new", second)
    def test_remove_key_empty_target_refused(self):
        with open(self.ini, "w", encoding="utf-8") as fh:
            fh.write(SAMPLE.replace("kn-aaa111, kn-bbb222", "kn-only"))
        code, out = self.run_cli("remove-key", "kenari", "", "--no-reload")
        self.assertEqual(code, 1)
        self.assertIn("empty target", out)
        self.assertEqual(self.read_keys("upstream:kenari"), ["kn-only"])

    def test_config_percent_key(self):
        with open(self.ini, "w", encoding="utf-8") as fh:
            fh.write(SAMPLE.replace("sk-kiosapi-aaaa", "sk-a%b"))
        code, out = self.run_cli("validate")
        self.assertEqual(code, 0)
        self.assertIn("config OK", out)

    def test_check_skips_placeholder_and_tries_all_keys(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data":[]}')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            text = ("""[server]
host = 127.0.0.1
port = 8787
base_url = https://example.invalid/v1

[keys]
key1 = sk-a

[upstream:alpha]
base_url = http://127.0.0.1:%d/v1
keys = REPLACE_ME, kn-real
""" % port)
            with open(self.ini, "w", encoding="utf-8") as fh:
                fh.write(text)
            code, out = self.run_cli("check", "alpha")
            self.assertEqual(code, 0)
            self.assertIn("OK", out)
        finally:
            server.shutdown()
            server.server_close()

    def test_state_relative_path(self):
        import kiosapi
        text = SAMPLE.replace("base_url = https://example.invalid/v1",
                              "base_url = https://example.invalid/v1\nstate_file = state.json")
        with open(self.ini, "w", encoding="utf-8") as fh:
            fh.write(text)
        state = {"version": 1, "upstreams": {
            "kenari": [kiosapi.key_fingerprint("kn-bbb222"),
                       kiosapi.key_fingerprint("kn-aaa111")],
        }}
        with open(os.path.join(self.tmp, "state.json"), "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        code, out = self.run_cli("state")
        self.assertEqual(code, 0)
        self.assertIn("...b222 -> ...a111", out)

    def test_state_non_object_file(self):
        with open(self.ini + ".state.json", "w", encoding="utf-8") as fh:
            fh.write("[]")
        code, out = self.run_cli("state")
        self.assertEqual(code, 1)
        self.assertIn("invalid state file", out)

    def test_state_malformed_upstreams_map(self):
        with open(self.ini + ".state.json", "w", encoding="utf-8") as fh:
            fh.write('{"version": 1, "upstreams": []}')
        code, out = self.run_cli("state")
        self.assertEqual(code, 0)
        self.assertNotIn("Traceback", out)

    def test_state_non_string_fingerprints_skipped(self):
        with open(self.ini + ".state.json", "w", encoding="utf-8") as fh:
            fh.write('{"version": 1, "upstreams": {"kenari": [["x"], 1]}}')
        code, out = self.run_cli("state")
        self.assertEqual(code, 0)
        self.assertNotIn("Traceback", out)
        self.assertIn("kenari", out)

    def test_chat_model_check(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"choices":[]}')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            with open(self.ini, "w", encoding="utf-8") as fh:
                fh.write(SAMPLE.replace("https://kenari.id/v1",
                                        "http://127.0.0.1:%d/v1" % port))
            code, out = self.run_cli("check", "kenari", "--chat-model", "m")
            self.assertEqual(code, 0)
            self.assertIn("OK", out)
        finally:
            server.shutdown()
            server.server_close()

    def test_write_lines_preserves_crlf(self):
        with open(self.ini, "w", encoding="utf-8", newline="") as fh:
            fh.write(SAMPLE.replace("\n", "\r\n"))
        code, _ = self.run_cli("add-key", "kenari", "kn-ccc333", "--no-reload")
        self.assertEqual(code, 0)
        with open(self.ini, "rb") as fh:
            raw = fh.read()
        self.assertIn(b"\r\n", raw)
        self.assertIn(b"kn-ccc333", raw)


if __name__ == "__main__":
    unittest.main()
